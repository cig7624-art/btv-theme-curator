"""Encrypted capability-scoped research projects, independent of source data.

A random recovery code is shown once to the caller, never stored or used in a
remote URL. Local JSON files use its SHA256 digest. Optional GitHub persistence
stores the same authenticated ciphertext, even in the existing public app repo.
All local and remote files are authenticated ciphertext. A recovery code is
required to derive the AES256-GCM key. An event union preserves simultaneous
conversations and decisions without storing the code alongside the ciphertext.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tempfile
import threading
from urllib.parse import quote
import uuid
from weakref import WeakValueDictionary

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DEFAULT_DIR = Path(__file__).with_name(".research_projects")
MAX_PROJECT_BYTES = 16 * 1024 * 1024
_CODE = re.compile(r"rp_[A-Za-z0-9_-]{43}\Z")
_IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PROJECT_LOCKS = WeakValueDictionary()
_PROJECT_LOCKS_GUARD = threading.Lock()
_HELD_PROJECT_LOCKS = threading.local()


class ResearchStorageError(RuntimeError):
    """Only safe, fixed user-facing messages; never provider responses."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(code):
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise ResearchStorageError("프로젝트 복구 코드를 확인해 주세요.")
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ResearchStorageError("입력 내용의 형식이나 길이를 확인해 주세요.")
    return value.strip()


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _document(value, identifier):
    try:
        if (not isinstance(value, dict) or value.get("schema_version") != 1
                or value.get("id") != identifier or not _DIGEST.fullmatch(identifier)
                or not isinstance(value.get("events"), list)):
            raise ValueError
        _timestamp(value["created_at"])
        seen = set()
        for event in value["events"]:
            if (not isinstance(event, dict) or not isinstance(event.get("id"), str)
                    or not _IDENTIFIER.fullmatch(event["id"]) or event["id"] in seen
                    or not isinstance(event.get("kind"), str)):
                raise ValueError
            _timestamp(event["created_at"])
            seen.add(event["id"])
            kind = event["kind"]
            if kind == "create":
                _text(event["title"], 200)
                _text(event.get("brief", ""), 20000)
            elif kind == "brief":
                _text(event["text"], 20000)
            elif kind == "title":
                _text(event["text"], 200)
            elif kind == "turn":
                _text(event["question"], 12000)
                if not isinstance(event.get("answer"), dict):
                    raise ValueError
            elif kind == "decision":
                _text(event["text"], 6000)
                if not _IDENTIFIER.fullmatch(event.get("turn_id", "")):
                    raise ValueError
            elif kind in {"decision_update", "decision_remove"}:
                if not _IDENTIFIER.fullmatch(event.get("decision_id", "")):
                    raise ValueError
                if kind == "decision_update":
                    _text(event["text"], 6000)
        return deepcopy(value)
    except (KeyError, ValueError, TypeError, ResearchStorageError):
        raise ResearchStorageError("프로젝트 저장 내용을 확인하지 못했습니다. 기존 자료는 유지했습니다.") from None


def _encode(document, code):
    try:
        payload = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_PROJECT_BYTES:
            raise ValueError
        identifier = _identifier(code)
        salt, nonce = os.urandom(16), os.urandom(12)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                   info=b"btv-media-research-v1:" + identifier.encode("ascii")).derive(code.encode("ascii"))
        ciphertext = AESGCM(key).encrypt(nonce, payload, identifier.encode("ascii"))
        envelope = {"schema_version": 1, "encryption": "AES-256-GCM-HKDF-SHA256",
                    "salt": base64.b64encode(salt).decode("ascii"),
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii")}
        return (json.dumps(envelope, separators=(",", ":")) + "\n").encode("ascii")
    except (ValueError, TypeError):
        raise ResearchStorageError("프로젝트 내용이 저장 가능한 형식이나 크기를 벗어났습니다.") from None


def _decode(payload, identifier, code):
    try:
        if len(payload) > MAX_PROJECT_BYTES * 2:
            raise ValueError
        envelope = json.loads(payload.decode("ascii"))
        if (not isinstance(envelope, dict) or envelope.get("schema_version") != 1
                or envelope.get("encryption") != "AES-256-GCM-HKDF-SHA256"
                or _identifier(code) != identifier):
            raise ValueError
        salt, nonce, ciphertext = [base64.b64decode(envelope[key], validate=True)
                                   for key in ("salt", "nonce", "ciphertext")]
        if len(salt) != 16 or len(nonce) != 12 or len(ciphertext) > MAX_PROJECT_BYTES + 16:
            raise ValueError
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                   info=b"btv-media-research-v1:" + identifier.encode("ascii")).derive(code.encode("ascii"))
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, identifier.encode("ascii"))
        return _document(json.loads(plaintext.decode("utf-8")), identifier)
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, InvalidTag):
        raise ResearchStorageError("프로젝트 저장 내용을 읽지 못했습니다. 기존 자료는 유지했습니다.") from None


def merge_project_documents(current, incoming):
    """Union immutable event IDs; refuse ambiguous IDs rather than lose edits."""
    identifier = current.get("id") if isinstance(current, dict) else ""
    current, incoming = _document(current, identifier), _document(incoming, identifier)
    result = {**incoming, **current}
    result["created_at"] = min((current["created_at"], incoming["created_at"]), key=_timestamp)
    events = {row["id"]: row for row in current["events"]}
    for row in incoming["events"]:
        if row["id"] in events and events[row["id"]] != row:
            raise ResearchStorageError("프로젝트 변경 내용이 충돌했습니다. 다시 열어 확인해 주세요.")
        events[row["id"]] = deepcopy(row)
    result["events"] = sorted(events.values(), key=lambda row: (_timestamp(row["created_at"]), row["id"]))
    return result


def _project(document):
    result = {"id": document["id"], "title": "새 사업 리서치", "brief": "",
              "created_at": document["created_at"], "updated_at": document["created_at"],
              "turns": [], "decisions": []}
    for event in sorted(document["events"], key=lambda row: (_timestamp(row["created_at"]), row["id"])):
        kind = event["kind"]
        result["updated_at"] = event["created_at"]
        if kind == "create":
            result["title"] = event["title"]
            result["brief"] = event.get("brief", "")
        elif kind == "title":
            result["title"] = event["text"]
        elif kind == "brief":
            result["brief"] = event["text"]
        elif kind == "turn":
            result["turns"].append({key: deepcopy(event[key]) for key in ("id", "question", "answer", "created_at")})
        elif kind == "decision":
            result["decisions"].append({key: event[key] for key in ("id", "turn_id", "text", "created_at")})
        elif kind == "decision_update":
            for decision in result["decisions"]:
                if decision["id"] == event["decision_id"]:
                    decision["text"] = event["text"]
                    decision["updated_at"] = event["created_at"]
        elif kind == "decision_remove":
            result["decisions"] = [row for row in result["decisions"] if row["id"] != event["decision_id"]]
    return result


def _path(storage_dir, identifier):
    directory = Path(storage_dir)
    if directory.is_symlink():
        raise ResearchStorageError("프로젝트 저장 경로를 확인해 주세요.")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (identifier + ".json")
    if path.is_symlink():
        raise ResearchStorageError("프로젝트 저장 경로를 확인해 주세요.")
    return path


@contextmanager
def _locked(storage_dir, identifier):
    # Only this project is serialized. Its network sync must never hold the
    # theme/CSV write lock or block another independent research project.
    key = (str(Path(storage_dir).absolute()), identifier)
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_LOCKS[key] = lock
    with lock:
        try:
            path = _path(storage_dir, identifier)
            held = getattr(_HELD_PROJECT_LOCKS, "keys", None)
            if held is None:
                held = _HELD_PROJECT_LOCKS.keys = set()
            if key in held:
                yield path
                return
            lock_path = path.with_suffix(".lock")
            if lock_path.is_symlink():
                raise ResearchStorageError("프로젝트 저장 경로를 확인해 주세요.")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(descriptor, "a+b") as stream:
                try:
                    import fcntl
                except ImportError:
                    fcntl = None
                if fcntl:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                held.add(key)
                try:
                    yield path
                finally:
                    held.remove(key)
                    if fcntl:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            raise ResearchStorageError("프로젝트를 저장하지 못했습니다. 저장 공간을 확인해 주세요.") from None


def _local(path, identifier, code):
    try:
        if not path.exists():
            return None
        return _decode(path.read_bytes(), identifier, code)
    except OSError:
        raise ResearchStorageError("프로젝트를 읽지 못했습니다. 기존 자료는 유지했습니다.") from None


def _atomic(path, document, code):
    payload, temporary = _encode(document, code), None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".research-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise ResearchStorageError("프로젝트를 저장하지 못했습니다. 기존 자료는 유지했습니다.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _remote_config(value):
    value = dict(value or {})
    repo = value.get("research_repo") or value.get("repo")
    token = value.get("research_token") or value.get("token")
    if not repo or not token:
        return None
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    directory = str(value.get("research_path") or "research_projects")
    path = PurePosixPath(directory)
    if (path.is_absolute() or ".." in path.parts or "\\" in directory
            or not path.parts or path.suffix or directory.startswith(".")):
        return None
    branch = str(value.get("research_branch") or value.get("branch") or "main")
    if not branch or any(ord(char) < 32 for char in branch):
        return None
    return {"repo": repo, "token": str(token), "branch": branch, "directory": str(path)}


def _request(method, config, suffix="", *, accept="application/vnd.github+json", **kwargs):
    try:
        return requests.request(method, "https://api.github.com/repos/" + config["repo"] + suffix,
            headers={"Authorization": "Bearer " + config["token"], "Accept": accept,
                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=(5, 25), **kwargs)
    except Exception:
        raise ResearchStorageError("암호화 기록 저장소의 연결을 확인하지 못했습니다. 현재 앱의 기록은 유지했습니다.") from None


def _remote_read(config, identifier, code):
    remote_path = config["directory"] + "/" + identifier + ".json"
    response = _request("GET", config, "/contents/" + quote(remote_path, safe="/"),
                        params={"ref": config["branch"]})
    if response.status_code == 404:
        return None, None
    if response.status_code != 200:
        raise ResearchStorageError("저장소의 암호화 프로젝트를 읽지 못했습니다. 현재 앱의 기록은 유지했습니다.")
    try:
        value = response.json()
        sha = value["sha"]
        if not isinstance(sha, str) or not sha:
            raise ValueError
        if value.get("encoding") == "none":
            raw = _request("GET", config, "/contents/" + quote(remote_path, safe="/"),
                           params={"ref": config["branch"]}, accept="application/vnd.github.raw+json", stream=True)
            try:
                if raw.status_code != 200:
                    raise ValueError
                declared = raw.headers.get("Content-Length")
                if declared and int(declared) > MAX_PROJECT_BYTES * 2:
                    raise ValueError
                chunks, total = [], 0
                for chunk in raw.iter_content(chunk_size=65536):
                    if chunk:
                        total += len(chunk)
                        if total > MAX_PROJECT_BYTES * 2:
                            raise ValueError
                        chunks.append(chunk)
                payload = b"".join(chunks)
            finally:
                raw.close()
        elif value.get("encoding") == "base64":
            if not isinstance(value.get("content"), str) or len(value["content"]) > MAX_PROJECT_BYTES * 3:
                raise ValueError
            payload = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        else:
            raise ValueError
        return _decode(payload, identifier, code), sha
    except (KeyError, TypeError, ValueError):
        raise ResearchStorageError("암호화 프로젝트의 저장 형식을 확인하지 못했습니다.") from None


def _remote_write(config, identifier, document, sha, code):
    remote_path = config["directory"] + "/" + identifier + ".json"
    body = {"message": "Update private research project", "branch": config["branch"],
            "content": base64.b64encode(_encode(document, code)).decode("ascii")}
    if sha:
        body["sha"] = sha
    response = _request("PUT", config, "/contents/" + quote(remote_path, safe="/"), json=body)
    if response.status_code in {409, 422}:
        return False
    if response.status_code not in {200, 201}:
        raise ResearchStorageError("암호화 기록 동기화를 확인하지 못했습니다. 현재 앱의 기록은 유지했습니다.")
    return True


def _sync(document, path, config, code):
    identifier = document["id"]
    remote = _remote_config(config)
    if remote is None:
        return document, "local_only", "remote_not_configured"
    try:
        for _ in range(3):
            other, sha = _remote_read(remote, identifier, code)
            merged = merge_project_documents(document, other) if other else document
            if other == merged or _remote_write(remote, identifier, merged, sha, code):
                _atomic(path, merged, code)
                return merged, "remote_synced", ""
        return document, "local_only", "remote_conflict"
    except ResearchStorageError:
        return document, "local_only", "remote_sync_failed"


def _result(document, status, reason, **extra):
    return {"status": status, "storage_reason": reason, "encrypted": True,
            "project": _project(document), **extra}


def create_project(title="", brief="", *, storage_dir=DEFAULT_DIR, github_config=None):
    title, brief = _text(title, 200) or "새 사업 리서치", _text(brief, 20000)
    code = "rp_" + secrets.token_urlsafe(32)
    identifier, when = _identifier(code), _now()
    document = {"schema_version": 1, "id": identifier, "created_at": when,
                "events": [{"id": uuid.uuid4().hex, "kind": "create", "created_at": when,
                            "title": title, "brief": brief}]}
    with _locked(storage_dir, identifier) as path:
        if path.exists():
            raise ResearchStorageError("프로젝트를 만들지 못했습니다. 다시 시도해 주세요.")
        _atomic(path, document, code)
        document, status, reason = _sync(document, path, github_config, code)
        return _result(document, status, reason, recovery_code=code)


def open_project(code, *, storage_dir=DEFAULT_DIR, github_config=None):
    identifier = _identifier(code)
    with _locked(storage_dir, identifier) as path:
        document = _local(path, identifier, code)
        remote = _remote_config(github_config)
        if document is None:
            if remote is None:
                raise ResearchStorageError("프로젝트를 찾지 못했습니다. 복구 코드와 저장소 연결을 확인해 주세요.")
            try:
                document, _ = _remote_read(remote, identifier, code)
                if document is None:
                    raise ResearchStorageError("프로젝트를 찾지 못했습니다. 복구 코드를 확인해 주세요.")
                _atomic(path, document, code)
                return _result(document, "remote_synced", "")
            except ResearchStorageError:
                raise ResearchStorageError("프로젝트를 불러오지 못했습니다. 복구 코드와 저장소 연결을 확인해 주세요.") from None
        document, status, reason = _sync(document, path, github_config, code)
        return _result(document, status, reason)


def _append(code, event, *, storage_dir, github_config):
    identifier = _identifier(code)
    # Recover a remote-only project before appending, then merge against the
    # current local document under the same shared/process lock.
    if not _path(storage_dir, identifier).exists():
        open_project(code, storage_dir=storage_dir, github_config=github_config)
    with _locked(storage_dir, identifier) as path:
        document = _local(path, identifier, code)
        if document is None:
            raise ResearchStorageError("프로젝트를 찾지 못했습니다. 다시 열어 주세요.")
        if event["kind"] == "decision" and not any(row.get("id") == event["turn_id"]
                                                  and row.get("kind") == "turn" for row in document["events"]):
            raise ResearchStorageError("채택할 답변을 찾지 못했습니다. 프로젝트를 다시 열어 주세요.")
        if event["kind"] in {"decision_update", "decision_remove"} and not any(
                row["id"] == event["decision_id"] for row in _project(document)["decisions"]):
            raise ResearchStorageError("수정할 결정을 찾지 못했습니다. 프로젝트를 다시 열어 주세요.")
        prior = next((row for row in document["events"] if row["id"] == event["id"]), None)
        if prior and event["kind"] == "turn":
            if prior.get("question") != event["question"] or prior.get("answer") != event["answer"]:
                raise ResearchStorageError("같은 답변 식별자의 내용이 달라 저장하지 않았습니다.")
            updated, status, reason = _sync(document, path, github_config, code)
            return _result(updated, status, reason)
        updated = merge_project_documents(document, {**document, "events": [event]})
        _atomic(path, updated, code)
        updated, status, reason = _sync(updated, path, github_config, code)
        return _result(updated, status, reason)


def append_turn(code, question, answer, *, turn_id=None, storage_dir=DEFAULT_DIR, github_config=None):
    question = _text(question, 12000)
    if not question or not isinstance(answer, dict):
        raise ResearchStorageError("질문과 답변의 저장 형식을 확인해 주세요.")
    identifier = turn_id or uuid.uuid4().hex
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise ResearchStorageError("답변의 저장 식별자를 확인해 주세요.")
    event = {"id": identifier, "kind": "turn", "created_at": _now(),
             "question": question, "answer": deepcopy(answer)}
    result = _append(code, event, storage_dir=storage_dir, github_config=github_config)
    result["turn_id"] = identifier
    return result


def update_brief(code, brief, *, storage_dir=DEFAULT_DIR, github_config=None):
    event = {"id": uuid.uuid4().hex, "kind": "brief", "created_at": _now(), "text": _text(brief, 20000)}
    return _append(code, event, storage_dir=storage_dir, github_config=github_config)


def rename_project(code, title, *, storage_dir=DEFAULT_DIR, github_config=None):
    title = _text(title, 200)
    if not title:
        raise ResearchStorageError("프로젝트 이름을 입력해 주세요.")
    event = {"id": uuid.uuid4().hex, "kind": "title", "created_at": _now(), "text": title}
    return _append(code, event, storage_dir=storage_dir, github_config=github_config)


def approve_decision(code, turn_id, text, *, storage_dir=DEFAULT_DIR, github_config=None):
    text = _text(text, 6000)
    if not text or not isinstance(turn_id, str) or not _IDENTIFIER.fullmatch(turn_id):
        raise ResearchStorageError("채택할 결론과 답변을 확인해 주세요.")
    event = {"id": uuid.uuid4().hex, "kind": "decision", "created_at": _now(),
             "turn_id": turn_id, "text": text}
    return _append(code, event, storage_dir=storage_dir, github_config=github_config)


def update_decision(code, decision_id, text, *, storage_dir=DEFAULT_DIR, github_config=None):
    text = _text(text, 6000)
    if not text or not isinstance(decision_id, str) or not _IDENTIFIER.fullmatch(decision_id):
        raise ResearchStorageError("수정할 결정과 내용을 확인해 주세요.")
    event = {"id": uuid.uuid4().hex, "kind": "decision_update", "created_at": _now(),
             "decision_id": decision_id, "text": text}
    return _append(code, event, storage_dir=storage_dir, github_config=github_config)


def remove_decision(code, decision_id, *, storage_dir=DEFAULT_DIR, github_config=None):
    if not isinstance(decision_id, str) or not _IDENTIFIER.fullmatch(decision_id):
        raise ResearchStorageError("제외할 결정을 확인해 주세요.")
    event = {"id": uuid.uuid4().hex, "kind": "decision_remove", "created_at": _now(),
             "decision_id": decision_id}
    return _append(code, event, storage_dir=storage_dir, github_config=github_config)
