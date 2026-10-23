"""Merge public research notes into their own snapshot without touching theme data.

GitHub writes use a file SHA compare-and-swap. A concurrent collector commit is
read again before one retry; an analysis of changed source material is refused.
Only the separate insights JSON is written. API credentials and error bodies
are never included in messages returned to the UI.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from urllib.parse import quote

import requests

from data_storage_lock import DATA_WRITE_LOCK


DEFAULT_REMOTE_PATH = "media_insights_snapshot.json"
MAX_REMOTE_SNAPSHOT_BYTES = 24 * 1024 * 1024
_PROTECTED_JSON = {"ott_home_snapshot.json", "collection_status.json", "release_state.json"}


class MediaInsightsStorageError(RuntimeError):
    """Safe, user-facing storage failure; never includes remote response text."""


def _empty_snapshot():
    return {"schema_version": 1, "collected_at": "", "sources": [], "items": []}


def _snapshot(value):
    if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
        raise MediaInsightsStorageError("미디어 자료의 저장 형식을 확인하지 못했습니다. 새로고침해 주세요.")
    result = deepcopy(value)
    result.setdefault("schema_version", 1)
    for key in ("items", "sources"):
        rows = result.setdefault(key, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise MediaInsightsStorageError("미디어 자료를 읽지 못했습니다. 기존 저장 내용은 유지했습니다.")
        identifiers = [row.get("id") for row in rows]
        if any(not isinstance(identifier, str) or not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
            raise MediaInsightsStorageError("미디어 자료의 식별 정보를 확인하지 못했습니다. 새로고침해 주세요.")
    return result


def _decode(payload):
    if payload is None:
        return _empty_snapshot()
    try:
        return _snapshot(json.loads(payload.decode("utf-8-sig")))
    except MediaInsightsStorageError:
        raise
    except Exception:
        raise MediaInsightsStorageError("미디어 자료를 읽지 못했습니다. 기존 저장 내용은 유지했습니다.") from None


def _encode(snapshot):
    try:
        return (json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise MediaInsightsStorageError("분석 내용을 저장 형식으로 변환하지 못했습니다.") from None


def _time(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _document_time(row):
    return max(_time(row.get(key)) for key in ("collected_at", "last_checked_at", "last_seen_at", "updated_at"))


def _evidence_index(snapshot):
    result = {}
    for item in snapshot.get("items", []):
        for evidence in item.get("evidence", []) or []:
            if not isinstance(evidence, dict) or not isinstance(evidence.get("id"), str) or not evidence["id"]:
                continue
            identifier = evidence["id"]
            # Ambiguous citations must not be attributed to an arbitrary item.
            result[identifier] = None if identifier in result else item
    return result


def _valid_analysis(item, analysis, snapshot):
    if (not isinstance(analysis, dict) or not item.get("content_hash")
            or analysis.get("source_hash") != item["content_hash"]
            or not analysis.get("generated_at") or not analysis.get("model")):
        return False
    facts = analysis.get("facts")
    if not isinstance(facts, list) or not facts:
        return False
    comparisons = analysis.get("comparison", [])
    if not isinstance(comparisons, list):
        return False
    evidence_index = _evidence_index(snapshot)
    documents = {row["id"]: row for row in snapshot["items"]}
    context_hashes = analysis.get("context_hashes", {})
    if not isinstance(context_hashes, dict):
        return False
    for identifier, content_hash in context_hashes.items():
        if documents.get(identifier, {}).get("content_hash") != content_hash:
            return False
    for statement in [*facts, *comparisons]:
        if not isinstance(statement, dict):
            return False
        if not str(statement.get("text") or statement.get("detail") or "").strip():
            return False
        identifiers = statement.get("evidence_ids")
        if not isinstance(identifiers, list) or not identifiers:
            return False
        for identifier in identifiers:
            try:
                document = evidence_index.get(identifier)
            except TypeError:
                return False
            if document is None:
                return False
            if document["id"] != item["id"] and context_hashes.get(document["id"]) != document.get("content_hash"):
                return False
    return True


def merge_media_snapshots(current, incoming):
    """Union source/document updates, keeping current or newer valid analyses.

    Unknown top-level metadata is retained. Missing items do not mean deletions;
    a failed or limited collection must not erase earlier source material.
    """
    current, incoming = _snapshot(current), _snapshot(incoming)
    result = deepcopy(current)
    if _time(incoming.get("collected_at")) >= _time(current.get("collected_at")):
        result.update({key: deepcopy(value) for key, value in incoming.items()
                       if key not in {"items", "sources", "notes"}})
    source_rows = {row["id"]: deepcopy(row) for row in current["sources"]}
    for source in incoming["sources"]:
        prior = source_rows.get(source["id"])
        if prior is None or _time(source.get("last_checked_at")) >= _time(prior.get("last_checked_at")):
            merged = {**deepcopy(prior or {}), **deepcopy(source)}
            if prior and _time(prior.get("last_success_at")) > _time(merged.get("last_success_at")):
                merged["last_success_at"] = prior["last_success_at"]
            source_rows[source["id"]] = merged
    result["sources"] = list(source_rows.values())
    documents = {row["id"]: deepcopy(row) for row in current["items"]}
    for item in incoming["items"]:
        previous = documents.get(item["id"])
        if previous is None:
            documents[item["id"]] = deepcopy(item)
            continue
        same_source = previous.get("content_hash") == item.get("content_hash")
        newer = _document_time(item) > _document_time(previous)
        chosen = deepcopy(item if newer else previous)
        if same_source:
            # Both writers may have independent analyses of other documents.
            # Preserve metadata fields from either side, prefer newer values.
            chosen = {**deepcopy(previous if newer else item), **chosen}
            analyses = [row.get("analysis") for row in (previous, item) if isinstance(row.get("analysis"), dict)]
            if analyses:
                chosen["analysis"] = deepcopy(max(analyses, key=lambda note: _time(note.get("generated_at"))))
        elif not newer:
            chosen = deepcopy(previous)
        documents[item["id"]] = chosen
    # A scheduled collection appends IDs to the merge map. Display documents
    # by actual publication date instead of that historical insertion order;
    # unknown publication dates stay last, even when freshly collected.
    result["items"] = sorted(documents.values(),
                             key=lambda row: (_time(row.get("published_at")), _document_time(row)),
                             reverse=True)
    if "notes" in current or "notes" in incoming:
        # Explicitly saved public research notes can survive collector snapshots.
        notes = []
        seen = set()
        for row in [*(current.get("notes") or []), *(incoming.get("notes") or [])]:
            fingerprint = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if fingerprint not in seen:
                notes.append(deepcopy(row))
                seen.add(fingerprint)
        result["notes"] = notes
    for item in result["items"]:
        if "analysis" in item and not _valid_analysis(item, item["analysis"], result):
            item.pop("analysis")
    return result


def _configured(config):
    return bool(config.get("token") and config.get("repo"))


def _remote_path(config):
    value = str(config.get("media_insights_path") or DEFAULT_REMOTE_PATH)
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or "\\" in value or not path.parts
            or path.suffix.lower() != ".json" or path.name in _PROTECTED_JSON):
        raise MediaInsightsStorageError("미디어 자료의 GitHub 저장 경로를 확인해 주세요.")
    return value


def _request(method, config, path, *, accept="application/vnd.github+json", **kwargs):
    repo = str(config.get("repo", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise MediaInsightsStorageError("GitHub 저장소 연결 설정을 확인해 주세요.")
    try:
        return requests.request(method, "https://api.github.com/repos/" + repo + "/contents/" + quote(path, safe="/"),
            headers={"Authorization": "Bearer " + str(config["token"]), "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=(5, 25), **kwargs)
    except Exception:
        raise MediaInsightsStorageError("GitHub 저장 결과를 확인하지 못했습니다. 분석 내용은 유지했습니다.") from None


def _read_remote(config, path):
    response = _request("GET", config, path, params={"ref": config.get("branch") or "main"})
    if response.status_code == 404:
        return {"snapshot": None, "sha": None}
    if response.status_code != 200:
        raise MediaInsightsStorageError("GitHub 미디어 자료를 읽지 못했습니다. 연결 설정을 확인해 주세요.")
    try:
        data = response.json()
        if not isinstance(data.get("sha"), str) or not data["sha"]:
            raise ValueError
        # Contents metadata omits base64 for files larger than 1 MiB. Fetch
        # raw bytes from the same authenticated endpoint, never download_url.
        if data.get("encoding") == "none":
            raw = _request("GET", config, path, params={"ref": config.get("branch") or "main"},
                           accept="application/vnd.github.raw+json", stream=True)
            try:
                if raw.status_code != 200:
                    raise ValueError
                declared = raw.headers.get("Content-Length")
                if declared and int(declared) > MAX_REMOTE_SNAPSHOT_BYTES:
                    raise ValueError
                chunks, size = [], 0
                for chunk in raw.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_REMOTE_SNAPSHOT_BYTES:
                        raise ValueError
                    chunks.append(chunk)
                payload = b"".join(chunks)
            finally:
                raw.close()
        else:
            payload = base64.b64decode(data["content"].replace("\n", ""), validate=True)
        if len(payload) > MAX_REMOTE_SNAPSHOT_BYTES:
            raise ValueError
        return {"snapshot": _decode(payload), "sha": data["sha"]}
    except MediaInsightsStorageError:
        raise
    except Exception:
        raise MediaInsightsStorageError("GitHub 미디어 자료의 저장 형식을 확인하지 못했습니다.") from None


def _write_remote(snapshot, *, config, path, sha):
    body = {"message": "Update grounded media business insights", "branch": config.get("branch") or "main",
            "content": base64.b64encode(_encode(snapshot)).decode("ascii")}
    if sha:
        body["sha"] = sha
    response = _request("PUT", config, path, json=body)
    if response.status_code in {409, 422}:
        return {"status": "conflict"}
    if response.status_code not in {200, 201}:
        raise MediaInsightsStorageError("GitHub 미디어 자료를 저장하지 못했습니다. 분석 내용은 유지했습니다.")
    try:
        value = response.json()
        return {"status": "success", "commit_url": value.get("commit", {}).get("html_url", "")}
    except Exception:
        raise MediaInsightsStorageError("GitHub 저장 결과를 확인하지 못했습니다. 새로고침해 주세요.") from None


def _bytes(path):
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        raise MediaInsightsStorageError("로컬 미디어 자료를 읽지 못했습니다.") from None


def _mirror(path, snapshot, before):
    payload = _encode(snapshot)
    if before == payload:
        return True
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", prefix=path.name + ".media-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if _bytes(path) != before:
            return False
        os.replace(temporary, path)
        return True
    except (OSError, MediaInsightsStorageError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _persist(snapshot_path, config, transform, *, remote_reader=None, remote_writer=None):
    path = Path(snapshot_path)
    if path.suffix.lower() != ".json" or path.name in _PROTECTED_JSON:
        raise MediaInsightsStorageError("미디어 자료의 저장 경로를 확인해 주세요.")
    with DATA_WRITE_LOCK:
        before = _bytes(path)
        local = _decode(before)
        if not _configured(config):
            updated = transform(local)
            mirrored = _mirror(path, updated, before)
            return {"status": "local_only", "snapshot": updated, "commit_url": "", "refresh_required": not mirrored}
        remote_path = _remote_path(config)
        read, write = remote_reader or _read_remote, remote_writer or _write_remote
        for attempt in range(2):
            try:
                remote = read(config, remote_path)
                latest = _snapshot(remote["snapshot"]) if remote.get("snapshot") is not None else local
                updated = transform(latest)
                if updated == latest and remote.get("snapshot") is not None:
                    result = {"status": "success", "commit_url": ""}
                else:
                    result = write(updated, config=config, path=remote_path, sha=remote.get("sha"))
            except MediaInsightsStorageError:
                raise
            except Exception:
                raise MediaInsightsStorageError("미디어 자료 저장을 확인하지 못했습니다. 분석 내용은 유지했습니다.") from None
            if result.get("status") == "conflict":
                if attempt == 0:
                    continue
                raise MediaInsightsStorageError("다른 작업이 미디어 자료를 갱신했습니다. 새로고침 후 다시 저장해 주세요.")
            if result.get("status") != "success":
                raise MediaInsightsStorageError("미디어 자료 저장을 확인하지 못했습니다. 분석 내용은 유지했습니다.")
            mirrored = _mirror(path, updated, before)
            return {"status": "success", "snapshot": updated,
                    "commit_url": result.get("commit_url", ""), "refresh_required": not mirrored}
    raise MediaInsightsStorageError("미디어 자료를 저장하지 못했습니다.")


def persist_media_analysis(item_id, analysis, *, snapshot_path, github_config,
                           remote_reader=None, remote_writer=None):
    """Persist one explicit analysis against latest matching source evidence."""
    def apply(snapshot):
        updated = _snapshot(snapshot)
        item = next((row for row in updated["items"] if row["id"] == item_id), None)
        if item is None:
            raise MediaInsightsStorageError("분석할 원문을 찾지 못했습니다. 자료를 새로고침해 주세요.")
        if not _valid_analysis(item, analysis, updated):
            raise MediaInsightsStorageError("분석 근거가 현재 원문과 맞지 않습니다. 최신 자료로 다시 분석해 주세요.")
        item["analysis"] = deepcopy(analysis)
        return updated
    return _persist(snapshot_path, github_config or {}, apply, remote_reader=remote_reader, remote_writer=remote_writer)


def persist_media_snapshot(snapshot, *, snapshot_path, github_config,
                           remote_reader=None, remote_writer=None):
    """Merge an explicit collection refresh into the independent public dataset."""
    incoming = _snapshot(snapshot)
    return _persist(snapshot_path, github_config or {}, lambda current: merge_media_snapshots(current, incoming),
                    remote_reader=remote_reader, remote_writer=remote_writer)
