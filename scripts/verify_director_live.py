"""Exercise the real public AI Director without adopting or saving any draft.

Only counts, booleans, release markers and fixed error codes leave this process.
Conversation, titles, raw HTML, screenshots, credentials and browser storage are
never recorded. Actual LLM calls run through the deployed app's normal UI.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import (  # noqa: E402
    APP_URL, VerificationIssue, _load_version, has_app_exception,
    no_horizontal_overflow, normalized_version,
)

TAB_ORDER = ['지금 주목할 이야기', '추천 테마', 'OTT는 지금', 'AI 디렉터']
CREATE_REQUEST = ('네가 제안한 2번 방향으로 한국 영화와 드라마를 섞어서 큐레이션 테마 3개 만들어줘. '
                  '각 테마는 서로 다른 관점으로 기획하고 실제 콘텐츠 DB에서 확인되는 작품 5~8편과 추천 이유를 붙여줘.')
TITLE_REQUEST = ('2번 테마명만 더 감각적인 제목으로 바꿔줘. 서브카피, 기획 의도, 장르, 분위기, '
                 '추천 작품과 추천 이유는 모두 그대로 유지해줘.')


def _key(value):
    return re.sub(r'[^0-9a-z가-힣]+', '', str(value or '').lower())


def valid_saved_matches(matches, known_names):
    return bool(matches) and all(_key(re.sub(r'^\s*\d+\.\s*', '', name)) in known_names for name in matches)


def verified_titles(root=ROOT):
    known = set()
    for filename, external in [('content_db.csv', False), ('theme_content_candidates.csv', True)]:
        path = Path(root) / filename
        if not path.exists():
            continue
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream, delimiter='|'):
                if not external or row.get('kinolights_status') == 'verified':
                    if _key(row.get('title')):
                        known.add(_key(row['title']))
    return known


def valid_drafts(rows, known, *, expected_count=3):
    if len(rows) != expected_count:
        return False
    for row in rows:
        items = row.get('contents', [])
        if (not all(row.get(field) for field in ('id', 'name', 'copy', 'rationale'))
                or not 5 <= len(items) <= 10 or not any(str(item.get('poster', '')).startswith('url(') for item in items)):
            return False
        names = [_key(item.get('title')) for item in items]
        if len(set(names)) != len(names) or not all(name in known for name in names):
            return False
        if not all(item.get('reason') for item in items):
            return False
    return True


def title_edit_is_scoped(before, after, target=1):
    if len(before) != len(after) or not 0 <= target < len(before):
        return False
    for index, (old, new) in enumerate(zip(before, after)):
        if index != target:
            if old != new:
                return False
        elif not new.get('name') or old.get('name') == new.get('name'):
            return False
        elif {k: v for k, v in old.items() if k != 'name'} != {k: v for k, v in new.items() if k != 'name'}:
            return False
    return True


def content_edit_is_scoped(before, after, excluded, target=1):
    if len(before) != len(after) or not 0 <= target < len(before):
        return False
    for index, (old, new) in enumerate(zip(before, after)):
        if index != target:
            if old != new:
                return False
        else:
            if {k: v for k, v in old.items() if k != 'contents'} != {k: v for k, v in new.items() if k != 'contents'}:
                return False
            if old.get('contents') == new.get('contents') or _key(excluded) in {_key(item.get('title')) for item in new.get('contents', [])}:
                return False
    return True


def _snapshot(frame):
    return frame.locator('.director-draft-card').evaluate_all('''nodes => nodes.map(card => ({
      id: card.dataset.draftId,
      name: card.querySelector('h3')?.textContent?.trim() || '',
      copy: card.querySelector('.director-draft-copy')?.textContent?.trim() || '',
      rationale: card.querySelector('.director-rationale p')?.textContent?.trim() || '',
      style: card.querySelector('.director-draft-top span:last-child')?.textContent?.trim() || '',
      contents: Array.from(card.querySelectorAll('.director-content')).map(item => ({
        title: item.querySelector('.theme-poster-caption')?.textContent?.trim() || '',
        meta: item.querySelector('.theme-poster-meta')?.textContent?.trim() || '',
        reason: item.querySelector('.director-content-reason')?.textContent?.trim() || '',
        poster: item.querySelector('.theme-poster-image') ? getComputedStyle(item.querySelector('.theme-poster-image')).backgroundImage : ''
      }))
    }))''')


def _overflow(frame):
    return not no_horizontal_overflow(frame.evaluate('''() => {
      const main = document.querySelector('[data-testid="stMain"]');
      return {viewport: innerWidth, document_scroll: document.documentElement.scrollWidth,
              main_width: main ? main.clientWidth : innerWidth,
              main_scroll: main ? main.scrollWidth : document.documentElement.scrollWidth};
    }'''))


def _assert(value, code):
    if not value:
        raise VerificationIssue(code)


def _chat(frame):
    return frame.locator('.st-key-director_composer textarea').first


def _submit(frame, request, timeout, *, allow_empty=False):
    old_count = frame.locator('.st-key-director_conversation [data-testid="stChatMessage"]').count()
    field = _chat(frame)
    _assert(field.is_enabled(), 'director_not_configured')
    field.fill(request)
    field.press('Enter')
    return _wait_reply(frame, old_count, timeout, allow_empty=allow_empty)


def _wait_reply(frame, old_count, timeout, *, allow_empty=False):
    deadline = time.monotonic() + timeout
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        _assert(not has_app_exception(frame.page), 'app_exception')
        if frame.locator('.st-key-ai_director_section [data-testid="stAlert"]').count():
            raise VerificationIssue('director_request_failed')
        if frame.locator('.st-key-director_conversation [data-testid="stChatMessage"]').count() >= old_count + 2:
            current = _snapshot(frame)
            stable = stable + 1 if (current or allow_empty) and current == previous else 0
            if stable >= 2 and not frame.locator('[data-stale="true"]').count():
                return current
            previous = current
        frame.page.wait_for_timeout(500)
    raise VerificationIssue('director_request_timeout')


def _poster_loading(frame):
    return frame.evaluate(r'''async () => {
      return await Promise.all(Array.from(document.querySelectorAll('.director-draft-card')).map(async card => {
        const sources = Array.from(card.querySelectorAll('.theme-poster-image')).map(node => {
          const value = getComputedStyle(node).backgroundImage.match(/^url\((["']?)(.*?)\1\)$/);
          return value ? value[2] : '';
        }).filter(Boolean);
        const results = await Promise.all(sources.map(source => new Promise(resolve => {
          const image = new Image(); let done = false;
          const finish = result => { if (done) return; done = true; clearTimeout(timer); resolve(result); };
          const timer = setTimeout(() => finish(false), 10000);
          image.onload = () => finish(image.naturalWidth > 0);
          image.onerror = () => finish(false);
          image.src = source;
          if (image.complete && image.naturalWidth > 0) finish(true);
        })));
        return {checked: results.length, loaded: results.filter(Boolean).length};
      }));
    }''')


def _enter_director(page, args, report, label):
    frame = _load_version(page, args, report, label)
    _assert(frame.get_by_role('tab').all_text_contents() == TAB_ORDER, 'four_tab_order_mismatch')
    frame.get_by_role('tab', name='AI 디렉터', exact=True).click()
    frame.locator('.director-heading').wait_for(timeout=15000)
    _assert(not _overflow(frame), label + '_horizontal_overflow')
    _assert(_chat(frame).is_enabled(), 'director_not_configured')
    report['tests'][label + '_empty_director'] = True
    return frame


def verify(args):
    report = {'schema_version': 1, 'status': 'failed', 'expected_version': args.expected_version,
              'observed_version': None, 'started_at': datetime.now(timezone.utc).isoformat(),
              'finished_at': None, 'tests': {}, 'errors': [], 'warnings': [], 'counts': {}}
    commit = os.environ.get('GITHUB_SHA', '')
    if re.fullmatch('[0-9a-f]{40}', commit):
        report['source_commit'] = commit
    browser = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            launch = {'headless': True, 'args': ['--disable-dev-shm-usage']}
            if args.executable:
                launch['executable_path'] = args.executable
            browser = p.chromium.launch(**launch)
            context = browser.new_context(viewport={'width': 1366, 'height': 1000})
            page = context.new_page()
            deadline = time.monotonic() + args.deploy_timeout
            while True:
                try:
                    frame = _enter_director(page, args, report, 'desktop')
                    break
                except VerificationIssue as error:
                    if error.blocked or not error.retry or time.monotonic() >= deadline:
                        raise
                    page.wait_for_timeout(15000)
                except Exception:
                    if time.monotonic() >= deadline:
                        raise VerificationIssue('release_browser_not_ready')
                    page.wait_for_timeout(15000)
            mobile_context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            try:
                _enter_director(mobile_context.new_page(), args, report, 'mobile')
            finally:
                mobile_context.close()
            known = verified_titles()
            brainstorm = _submit(frame, '뭐 만들어볼까?', args.turn_timeout, allow_empty=True)
            reply = frame.locator('.st-key-director_conversation [data-testid="stChatMessage"]').last.inner_text().strip()
            report['tests']['open_ideation_without_forced_drafts'] = not brainstorm and len(reply) > 30
            _assert(report['tests']['open_ideation_without_forced_drafts'], 'open_ideation_failed')
            created = _submit(frame, CREATE_REQUEST, args.turn_timeout)
            report['tests']['free_prompt_three_drafts'] = valid_drafts(created, known)
            _assert(report['tests']['free_prompt_three_drafts'], 'invalid_generated_drafts')
            report['counts']['drafts'] = len(created)
            report['counts']['contents_per_draft'] = [len(row['contents']) for row in created]
            report['counts']['poster_loading'] = _poster_loading(frame)
            _assert(all(row['loaded'] > 0 for row in report['counts']['poster_loading']), 'draft_posters_unavailable')
            if any(row['loaded'] < row['checked'] for row in report['counts']['poster_loading']):
                report['warnings'].append('some_draft_posters_unavailable')
            renamed = _submit(frame, TITLE_REQUEST, args.turn_timeout)
            report['tests']['second_title_only_preserved_others'] = title_edit_is_scoped(created, renamed)
            _assert(report['tests']['second_title_only_preserved_others'], 'title_edit_scope_violation')
            excluded = renamed[1]['contents'][0]['title']
            replaced = _submit(frame, '2번 테마에서 추천 작품 〈' + excluded + '〉을 제외하고 다른 작품으로 교체해줘.', args.turn_timeout)
            report['tests']['content_exclusion_preserved_metadata'] = content_edit_is_scoped(renamed, replaced, excluded) and valid_drafts(replaced, known)
            _assert(report['tests']['content_exclusion_preserved_metadata'], 'content_edit_scope_violation')
            restored = _submit(frame, '이전 버전으로 되돌려줘', min(30, args.turn_timeout))
            report['tests']['undo_restored_previous_drafts'] = restored == renamed
            _assert(report['tests']['undo_restored_previous_drafts'], 'undo_draft_mismatch')
            searched = _submit(frame, '기존에 저장된 라이브러리에서 공포와 반전 분위기의 유사 테마를 찾아줘. 초안은 수정하지 마.', args.turn_timeout)
            saved_names = set()
            for path in (ROOT / 'theme_db.csv', ROOT / 'theme_pool.csv'):
                if path.exists():
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        saved_names.update(_key(row.get('theme_name')) for row in csv.DictReader(stream, delimiter='|'))
            search_expander = frame.locator('summary').filter(has_text='라이브러리에서 찾은 테마')
            if search_expander.count():
                search_expander.first.click()
            report['tests']['library_search_section'] = frame.locator('.st-key-director_library_matches').count() > 0
            try:
                frame.locator('.director-library-card').first.wait_for(state='visible', timeout=15000)
            except Exception:
                pass
            matches = frame.locator('.director-library-card h4').all_text_contents()
            report['tests']['library_search_moved_into_conversation'] = valid_saved_matches(matches, saved_names) and searched == restored
            report['counts']['matched_saved_themes'] = len(matches)
            _assert(report['tests']['library_search_moved_into_conversation'], 'saved_theme_search_failed')
            old_count = frame.locator('.st-key-director_conversation [data-testid="stChatMessage"]').count()
            frame.get_by_role('button', name='이 테마로 새롭게 기획', exact=True).first.click()
            reworked = _wait_reply(frame, old_count, args.turn_timeout)
            report['tests']['saved_theme_rework_adds_draft_preserves_others'] = len(reworked) == 4 and reworked[:3] == restored and valid_drafts(reworked[3:], known, expected_count=1)
            _assert(report['tests']['saved_theme_rework_adds_draft_preserves_others'], 'saved_theme_rework_failed')
            report['counts']['final_drafts'] = len(reworked)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_timeout(500)
            report['tests']['mobile_drafts_no_overflow'] = not _overflow(frame) and _snapshot(frame) == reworked
            _assert(report['tests']['mobile_drafts_no_overflow'], 'mobile_drafts_changed_or_overflowed')
            report['tests']['no_app_exception'] = not has_app_exception(page)
            _assert(report['tests']['no_app_exception'], 'app_exception')
            report['tests']['adoption_not_invoked'] = True
            report['counts']['submitted_requests'] = 7
            report['status'] = 'passed'
            context.close()
            browser.close(); browser = None
    except VerificationIssue as error:
        report['status'] = 'blocked' if error.blocked else 'failed'
        report['errors'] = [error.code]
    except Exception:
        report['errors'] = ['live_director_browser_check_failed']
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=APP_URL)
    parser.add_argument('--expected-version')
    parser.add_argument('--deploy-timeout', type=float, default=480)
    parser.add_argument('--turn-timeout', type=float, default=240)
    parser.add_argument('--executable')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/director_live_validation.json')
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / 'VERSION').read_text())
    parsed = urlsplit(args.url)
    if (not args.expected_version or not 0 < args.deploy_timeout <= 900 or not 0 < args.turn_timeout <= 300
            or parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password):
        parser.error('Use an exact release version and bounded positive timeouts')
    result = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 2 if result['status'] == 'blocked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
