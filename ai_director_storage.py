"""Explicit adoption of director drafts; existing themes and history are append-only.

The normal path reads one Git revision, creates one three-file Git commit, and
updates its branch without force before updating local CSVs. Draft generation
never calls this module. Provider availability is never inferred from identity.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from data_storage_lock import DATA_WRITE_LOCK, with_data_write_lock
from content_pipeline import _kind, _metadata_source
from kinolights_candidates import content_rows_to_frame
from llm_theme_generator import (
    HISTORY_COLUMNS, _existing_match, ensure_theme_schema, github_writeback_configured,
    normalize_theme_text, persist_recommendations_locally,
)
from theme_content_manager import CONTENT_COLUMNS

class DirectorStorageError(RuntimeError):
    """An actionable error whose message never includes tokens or API bodies."""


class DirectorDuplicateError(DirectorStorageError):
    def __init__(self, theme_id, theme_name, similarity):
        self.theme_id, self.theme_name, self.similarity = str(theme_id), str(theme_name), float(similarity)
        super().__init__(f"기존 테마 ‘{self.theme_name}’와 중복됩니다. 다른 테마명으로 수정한 뒤 채택해 주세요.")


def director_adoption_key(draft):
    """Stable across retries, but changes when the meaningful draft changes."""
    fields = ('theme_name', 'copy', 'rationale', 'genre', 'mood', 'keywords',
              'content_search_terms', 'source_issue', 'creation_angle')
    meaningful = {key: draft.get(key, '') for key in fields}
    meaningful['draft_id'] = str(draft.get('draft_id', ''))
    meaningful['contents'] = [{key: item.get(key, '') for key in
                              ('content_id', 'title', 'content_type', 'type', 'year', 'reason')}
                             for item in draft.get('contents', []) if isinstance(item, dict)]
    return hashlib.sha256(json.dumps(meaningful, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), default=str).encode()).hexdigest()[:24]


def _text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _bytes(frame):
    return frame.fillna('').to_csv(sep='|', index=False).encode('utf-8-sig')


def _frame(data, columns):
    if data is None:
        return pd.DataFrame(columns=columns)
    try:
        result = pd.read_csv(io.BytesIO(data), sep='|', dtype=str).fillna('')
    except Exception as exc:
        raise DirectorStorageError('저장된 CSV를 읽지 못했습니다. 원본 파일을 확인한 뒤 다시 시도해 주세요.') from exc
    if result.columns.duplicated().any():
        raise DirectorStorageError('저장된 CSV 열이 중복돼 채택을 중단했습니다.')
    for column in columns:
        if column not in result:
            result[column] = ''
    return result


def _merge(local, remote, keys):
    """Preserve new rows on either side; reject edited shared records."""
    columns = list(dict.fromkeys([*remote.columns, *local.columns]))
    shared = set(local.columns) & set(remote.columns)
    result = remote.reindex(columns=columns, fill_value='').copy()
    records = result.to_dict('records')
    by_key = {tuple(str(row.get(k, '')) for k in keys): row for row in records}
    for row in local.to_dict('records'):
        identity = tuple(str(row.get(k, '')) for k in keys)
        existing = by_key.get(identity)
        if existing is not None:
            if any(str(existing.get(k, '')) != str(row.get(k, '')) for k in shared):
                raise DirectorStorageError('다른 화면에서 같은 저장 자료가 변경됐습니다. 새로고침한 뒤 다시 채택해 주세요.')
            for column in set(local.columns) - set(remote.columns):
                existing[column] = row.get(column, '')
        else:
            appended = {column: row.get(column, '') for column in columns}
            records.append(appended)
            by_key[identity] = appended
    return pd.DataFrame(records, columns=columns)


def _validate_draft(draft, catalog):
    if not isinstance(draft, dict) or not _text(draft.get('draft_id')):
        raise DirectorStorageError('채택할 초안을 선택해 주세요.')
    if not all(_text(draft.get(k)) for k in ('theme_name', 'copy', 'rationale')):
        raise DirectorStorageError('테마명·서브카피·기획 의도를 완성한 뒤 채택해 주세요.')
    contents = draft.get('contents')
    if not isinstance(contents, list) or not 5 <= len(contents) <= 10:
        raise DirectorStorageError('실제 확인된 추천 작품 5~10편을 연결한 뒤 채택해 주세요.')
    known = {str(row.get('content_id', '')): row for row in catalog.to_dict('records')} if catalog is not None else {}
    seen = set()
    for item in contents:
        if not isinstance(item, dict) or not _text(item.get('title')) or not _text(item.get('reason')):
            raise DirectorStorageError('작품명과 추천 이유가 확인된 후보만 채택할 수 있습니다.')
        identity = (normalize_theme_text(item['title']), _text(item.get('year')))
        if identity in seen:
            raise DirectorStorageError('중복된 작품을 정리한 뒤 채택해 주세요.')
        seen.add(identity)
        status = item.get('catalog_status')
        if status == 'confirmed_local':
            row = known.get(str(item.get('content_id', '')))
            if (row is None or normalize_theme_text(row.get('title')) != identity[0]
                    or (item.get('year') and str(row.get('year', '')) != str(item['year']))
                    or _kind(item.get('content_type') or item.get('type')) != _kind(row.get('type') or row.get('content_type'))):
                raise DirectorStorageError('원본 콘텐츠 DB와 연결되지 않는 작품이 있습니다. 작품을 다시 확인해 주세요.')
        elif status == 'verified_external':
            try:
                source = urlsplit(str(item.get('source_url', '')))
                valid = source.scheme == 'https' and bool(_metadata_source(item.get('source_url')))
            except ValueError:
                valid = False
            if item.get('kinolights_status') != 'verified' or not valid:
                raise DirectorStorageError('외부 작품의 확인 근거가 부족합니다. 작품을 다시 연결해 주세요.')
        else:
            raise DirectorStorageError('실제 콘텐츠가 확인되지 않은 후보는 채택할 수 없습니다.')


def _prepare(draft, themes, history, candidates, now, model, allow_similar):
    key = director_adoption_key(draft)
    run_id = 'AI-DIRECTOR-' + key
    prior = history[history['run_id'].astype(str).eq(run_id)]
    if not prior.empty:
        theme_id = str(prior.iloc[0]['theme_id'])
        rows = themes[themes['theme_id'].astype(str).eq(theme_id)]
        if rows.empty:
            raise DirectorStorageError('기존 채택 이력과 테마 DB가 일치하지 않습니다. 저장 자료를 확인해 주세요.')
        return themes, history, candidates, rows.iloc[0].to_dict(), run_id, True
    candidate = {**draft, 'trigger_keywords': ','.join(draft.get('keywords', []))}
    match, similarity = _existing_match(candidate, themes)
    if match is not None and (normalize_theme_text(match.get('theme_name')) == normalize_theme_text(draft['theme_name'])
                              or (similarity >= .86 and not allow_similar)):
        raise DirectorDuplicateError(match.get('theme_id'), match.get('theme_name'), similarity)
    rec = {'recommendation_source': 'AI_GENERATED', 'source_label': 'AI 디렉터 채택',
           'theme': {'theme_name': draft['theme_name'], 'copy': draft['copy'], 'genre': draft.get('genre', ''),
                     'mood': draft.get('mood', ''), 'trigger_keywords': candidate['trigger_keywords'],
                     'source_issue': draft.get('source_issue', ''), 'creation_angle': draft.get('creation_angle', ''),
                     'content_search_terms': ','.join(draft.get('content_search_terms', []))},
           'source_issue_summary': draft.get('source_issue', ''), 'rationale': draft['rationale'],
           'creation_angle': draft.get('creation_angle', ''), 'content_search_terms': draft.get('content_search_terms', []),
           'contents': deepcopy(draft['contents'])}
    # Reuse established ID allocation and CSV mappings, but never its replace-
    # history behavior on live files. Only temporary files are touched here.
    with TemporaryDirectory() as folder:
        updated, new_history, _ = persist_recommendations_locally([rec], themes, Path(folder)/'themes.csv',
                              Path(folder)/'history.csv', {'run_id': run_id, 'model': model})
    theme_id = str(rec['theme']['theme_id'])
    stamp = now.isoformat(timespec='seconds')
    day = now.date().isoformat()
    mask = updated['theme_id'].astype(str).eq(theme_id)
    updated.loc[mask, 'approved_status'] = 'ADOPTED'
    updated.loc[mask, ['created_date', 'last_recommended_date']] = day
    updated.loc[mask, 'director_draft_id'] = str(draft['draft_id'])
    updated.loc[mask, 'director_adoption_key'] = key
    updated = updated.fillna('')
    new_history['recommended_date'] = day
    new_history['recommended_at'] = stamp
    new_history['director_draft_id'] = str(draft['draft_id'])
    new_history['director_adoption_key'] = key
    combined_history = pd.concat([history, new_history], ignore_index=True).fillna('')
    validated = []
    for item in draft['contents']:
        validated.append({**item, 'content_type': item.get('content_type') or item.get('type', ''),
                          'status': item.get('kinolights_status', 'catalog_confirmed')})
    frame = content_rows_to_frame(run_id, [rec], {theme_id: validated})
    for column in ('content_id', 'catalog_status', 'availability_status', 'availability_label'):
        frame[column] = [str(item.get(column, '')) for item in validated]
    # Identity/metadata verification does not confirm B tv rights.
    frame['availability_status'] = 'not_checked'
    frame['availability_label'] = 'B tv 서비스 여부 미확인'
    library = frame.copy(); library['run_id'] = 'THEME_DB'
    combined_contents = pd.concat([candidates, frame, library], ignore_index=True).fillna('')
    return updated, combined_history, combined_contents, updated.loc[mask].iloc[0].to_dict(), run_id, False


def _remote_request(method, config, suffix, **kwargs):
    repo = str(config.get('repo', ''))
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise DirectorStorageError('GitHub 저장소 연결 설정을 확인해 주세요.')
    try:
        response = requests.request(method, 'https://api.github.com/repos/' + repo + suffix,
             headers={'Authorization': 'Bearer ' + str(config['token']), 'Accept': 'application/vnd.github+json',
                      'X-GitHub-Api-Version': '2022-11-28'}, timeout=25, **kwargs)
    except requests.RequestException as exc:
        raise DirectorStorageError('GitHub 저장 결과를 확인하지 못했습니다. 초안은 유지되며 다시 채택할 수 있습니다.') from exc
    return response


def _json_response(response, expected):
    if response.status_code not in expected:
        raise DirectorStorageError('GitHub 저장 중 변경 충돌 또는 연결 오류가 발생했습니다. 새로고침 후 다시 채택해 주세요.')
    try:
        return response.json()
    except Exception as exc:
        raise DirectorStorageError('GitHub 저장 결과를 확인하지 못했습니다. 다시 채택해 주세요.') from exc


def _read_remote_snapshot(config, paths):
    branch = str(config.get('branch') or 'main')
    ref = _json_response(_remote_request('GET', config, '/git/ref/heads/' + quote(branch, safe='')), {200})
    head = ref['object']['sha']
    commit = _json_response(_remote_request('GET', config, '/git/commits/' + head), {200})
    result = {}
    for name, path in paths.items():
        response = _remote_request('GET', config, '/contents/' + quote(path, safe='/'), params={'ref': head})
        if response.status_code == 404:
            result[name] = None
        else:
            data = _json_response(response, {200})
            try:
                result[name] = base64.b64decode(data['content'].replace('\n', ''), validate=True)
            except Exception as exc:
                raise DirectorStorageError('GitHub 저장 파일을 읽지 못했습니다. 다시 시도해 주세요.') from exc
    return {'head': head, 'tree': commit['tree']['sha'], 'files': result}


def _commit_remote_snapshot(payloads, *, config, remote_paths, head, tree, run_id):
    entries = []
    for name, payload in payloads.items():
        blob = _json_response(_remote_request('POST', config, '/git/blobs',
                     json={'encoding': 'base64', 'content': base64.b64encode(payload).decode()}), {201})
        entries.append({'path': remote_paths[name], 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    new_tree = _json_response(_remote_request('POST', config, '/git/trees', json={'base_tree': tree, 'tree': entries}), {201})
    commit = _json_response(_remote_request('POST', config, '/git/commits',
                  json={'message': f'Adopt AI director theme ({run_id})', 'tree': new_tree['sha'], 'parents': [head]}), {201})
    branch = str(config.get('branch') or 'main')
    _json_response(_remote_request('PATCH', config, '/git/refs/heads/' + quote(branch, safe=''),
                   json={'sha': commit['sha'], 'force': False}), {200})
    return {'status': 'success', 'commit_sha': commit['sha'], 'commit_url': commit.get('html_url', '')}


def _local_payloads(paths):
    return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}


@with_data_write_lock
def _write_local(paths, payloads, before):
    if _local_payloads(paths) != before:
        return 'refresh_required'
    # Stage every file before replacing any destination. Git's atomic commit is
    # authoritative if an OS failure interrupts the local mirror update.
    staged = {}
    try:
        for name, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + '.director-' + hashlib.sha256(os.urandom(16)).hexdigest()[:12] + '.tmp')
            temporary.write_bytes(payloads[name]); staged[name] = temporary
        # A process-external writer may have changed a CSV while staging. All
        # app writers share the lock, but this second check also protects
        # changes made without that process-local lock during staging.
        if _local_payloads(paths) != before:
            return 'refresh_required'
        for name, path in paths.items():
            os.replace(staged[name], path)
        return 'success'
    except OSError:
        return 'refresh_required'
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def adopt_director_draft(draft, *, themes, theme_path, history_path, content_path, github_config,
                         allow_similar=False, model='', now=None, allow_local=False,
                         catalog=None, remote_reader=None, remote_writer=None):
    """Adopt one complete draft, preserving all previous themes and runs.

    Normal UI use requires configured GitHub storage. ``allow_local`` exists for
    explicitly local operation and tests, and never reports durable success.
    """
    with DATA_WRITE_LOCK:
        paths = {'themes': Path(theme_path), 'history': Path(history_path), 'contents': Path(content_path)}
        before = _local_payloads(paths)
        if len({p.resolve() for p in paths.values()}) != 3:
            raise DirectorStorageError('테마·이력·콘텐츠 저장 경로는 서로 달라야 합니다.')
        ready = github_writeback_configured(github_config)
        if not ready and not allow_local:
            raise DirectorStorageError('채택한 테마를 영구 저장하려면 GitHub 연결 설정이 필요합니다.')
        if catalog is None:
            catalog_path = paths['themes'].parent / 'content_db.csv'
            catalog = _frame(catalog_path.read_bytes(), ['content_id', 'title', 'type', 'year']) if catalog_path.exists() else None
        _validate_draft(draft, catalog)
        current = {'themes': ensure_theme_schema(_frame(before['themes'], list(ensure_theme_schema(themes).columns))) if before['themes'] is not None else ensure_theme_schema(themes),
                   'history': _frame(before['history'], HISTORY_COLUMNS), 'contents': _frame(before['contents'], CONTENT_COLUMNS)}
        remote = None
        remote_paths = {'themes': github_config.get('theme_path') or paths['themes'].name,
                        'history': github_config.get('history_path') or paths['history'].name,
                        'contents': github_config.get('content_path') or paths['contents'].name}
        if ready:
            if len(set(remote_paths.values())) != 3 or any(not p or p.startswith('/') or '..' in Path(p).parts for p in remote_paths.values()):
                raise DirectorStorageError('GitHub의 테마·이력·콘텐츠 저장 경로를 확인해 주세요.')
            remote = (remote_reader or _read_remote_snapshot)(github_config, remote_paths)
            schemas = {'themes': list(ensure_theme_schema(pd.DataFrame()).columns), 'history': HISTORY_COLUMNS, 'contents': CONTENT_COLUMNS}
            keys = {'themes': ['theme_id'], 'history': ['run_id', 'theme_id', 'rank'],
                    'contents': ['run_id', 'theme_key', 'rank', 'title', 'year']}
            for name in current:
                if remote['files'].get(name) is not None:
                    other = _frame(remote['files'][name], schemas[name])
                    if name == 'themes':
                        other = ensure_theme_schema(other)
                    current[name] = _merge(current[name], other, keys[name])
        now = now or datetime.now(ZoneInfo('Asia/Seoul'))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo('Asia/Seoul'))
        else:
            now = now.astimezone(ZoneInfo('Asia/Seoul'))
        updated, history, contents, theme, run_id, already = _prepare(draft, current['themes'], current['history'], current['contents'], now, model, allow_similar)
        payloads = {'themes': _bytes(updated), 'history': _bytes(history), 'contents': _bytes(contents)}
        github_result = {'status': 'not_configured'}
        remotely_adopted = remote is not None and run_id in set(_frame(remote['files'].get('history'), HISTORY_COLUMNS)['run_id'].astype(str))
        if ready and not remotely_adopted:
            github_result = (remote_writer or _commit_remote_snapshot)(payloads, config=github_config, remote_paths=remote_paths,
                       head=remote['head'], tree=remote['tree'], run_id=run_id)
            if github_result.get('status') != 'success':
                raise DirectorStorageError('GitHub 저장을 완료하지 못했습니다. 초안은 유지되며 다시 채택할 수 있습니다.')
        elif remotely_adopted:
            github_result = {'status': 'already_saved'}
        local_status = _write_local(paths, payloads, before)
        status = 'already_adopted' if remotely_adopted or (already and not ready) else 'success' if ready else 'local_only'
        recommendation = {'recommendation_source': 'AI_GENERATED', 'source_label': 'AI 디렉터 채택',
                          'theme': theme, 'rationale': draft['rationale'], 'source_issue_summary': draft.get('source_issue', ''),
                          'contents': deepcopy(draft['contents'])}
        return {'status': status, 'local_status': local_status, 'durable': bool(ready), 'theme_id': str(theme['theme_id']),
                'run_id': run_id, 'theme': theme, 'recommendations': [recommendation], 'github_result': github_result}
