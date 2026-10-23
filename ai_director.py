"""Conversation and reversible draft state for the AI Creative Director.

This module deliberately has no persistence calls. A turn returns a new state
only after the model plan and content identities have been validated. Adoption
belongs to the separate storage boundary.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import re
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from llm_theme_generator import (
    ThemeGenerationError, ThemeSearchIntent, _clean_short_text, _dedupe_strings, _existing_match,
    ensure_theme_schema, generate_director_plan, normalize_theme_text,
)


EDIT_SCOPES = {"auto", "title", "copy", "contents", "style", "all"}
SCOPE_FIELDS = {
    "title": {"theme_name"}, "copy": {"copy"},
    "contents": {"contents", "excluded_titles"},
    "style": {"genre", "mood", "keywords", "content_search_terms"},
    "all": {"theme_name", "copy", "rationale", "genre", "mood", "keywords", "content_search_terms",
            "creation_angle", "source_issue_keys", "contents", "excluded_titles"},
}
MAX_VERSIONS = 40


def _now():
    return datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")


def new_director_session():
    return {"schema_version": 1, "messages": [], "drafts": [], "versions": [],
            "current_version_id": None, "version_sequence": 0, "last_usage": {},
            "library_search_intent": None,
            "library_search_results": [],
            "session_id": "director-" + uuid4().hex[:16]}


def _session(session):
    if not isinstance(session, dict) or session.get("schema_version") != 1:
        raise ThemeGenerationError("대화 상태를 읽지 못했습니다. 새 대화를 시작해 주세요.")
    return deepcopy(session)


def _version(state, label, *, kind="revise", restored_from=None, restored_target_draft_id=None):
    state["version_sequence"] = int(state.get("version_sequence", 0)) + 1
    version_id = f"v{state['version_sequence']:04d}"
    state["versions"].append({"version_id": version_id, "label": _clean_short_text(label, 100),
        "kind": kind, "created_at": _now(), "parent_version_id": state.get("current_version_id"),
        "restored_from": restored_from, "restored_target_draft_id": restored_target_draft_id,
        "drafts": deepcopy(state["drafts"])})
    state["versions"] = state["versions"][-MAX_VERSIONS:]
    state["current_version_id"] = version_id
    return state


def _find_draft(state, draft_id):
    return next((row for row in state.get("drafts", []) if row.get("draft_id") == draft_id), None)


def _requested_target(state, request, selected):
    if selected and not _find_draft(state, selected):
        raise ThemeGenerationError("선택한 테마를 찾지 못했습니다. 수정할 테마를 다시 선택해 주세요.")
    match = re.search(r"(?<!\d)([1-9][0-9]?)\s*번(?:째)?(?:\s*테마)?", request)
    if match:
        # A conversational suggestion is not a saved draft. For example,
        # '2번 방향으로 3개 만들어줘' must reach the model with its prior reply.
        after_number = request[match.end():].lstrip()
        if not state["drafts"] or re.match(r"(?:방향|아이디어|제안|컨셉|콘셉트|안)(?:으로|로|을|를|이|가|은|는|\s|$)", after_number):
            return selected
        library_reference = bool(re.search(r"라이브러리|저장(?:된)?\s*테마|찾은\s*테마|검색\s*결과", request))
        from_recent_search = (state.get("library_search_results") and
                              (re.search(r"그\s*중", request) or
                               (state.get("library_search_intent") and re.search(r"기획|만들|생성", request))))
        if library_reference or from_recent_search:
            return selected
        number = int(match.group(1))
        if number > len(state["drafts"]):
            raise ThemeGenerationError(f"{number}번 테마가 없습니다. 현재 초안 번호를 확인해 주세요.")
        return state["drafts"][number - 1]["draft_id"]
    return selected


def _library_search_intent(raw):
    if raw is None:
        return None
    try:
        intent = ThemeSearchIntent.model_validate(raw).model_dump()
    except Exception as exc:
        raise ThemeGenerationError("저장된 테마의 검색 조건을 읽지 못했습니다. 다시 요청해 주세요.") from exc
    intent["interpreted_request"] = _clean_short_text(intent["interpreted_request"], 140)
    for key in ("anchor_titles", "genres", "moods", "subjects", "narrative_elements",
                "positive_keywords", "negative_keywords"):
        intent[key] = _dedupe_strings(intent.get(key) or [], 14)
    # The director returns search intent, never unverified database matches or
    # model-made cards disguised as library results. Ranking stays local.
    intent["suggested_themes"] = []
    return intent


def _requested_scope(request, selected):
    if selected != "auto":
        return selected
    # Protect narrow, explicit requests even if the model proposes a broad edit.
    has_content_change = bool(re.search(r"(?:작품|콘텐츠).{0,20}(?:바꿔|변경|교체|제외|빼|수정|추가)", request))
    if re.search(r"(?:제목|테마명|네이밍|이름).{0,20}(?:만|바꿔|변경|수정)", request) and not has_content_change:
        return "title"
    if re.search(r"(?:서브\s*카피|카피).{0,10}만", request) and not has_content_change:
        return "copy"
    if re.search(r"(?:작품|콘텐츠).{0,10}만.{0,10}(?:바꿔|변경|교체|수정)", request):
        return "contents"
    metadata = re.search(r"제목|테마명|네이밍|카피|장르|분위기|무드|기획\s*의도", request)
    if has_content_change and not metadata:
        return "contents"
    if (re.search(r"(?:장르|분위기|무드).{0,20}(?:바꿔|변경|수정)", request)
            and not has_content_change and not re.search(r"제목|테마명|카피|기획\s*의도", request)):
        return "style"
    return "auto"


def _duplicate_matches(draft, themes):
    candidate = dict(draft, trigger_keywords=",".join(draft.get("keywords", [])))
    row, score = _existing_match(candidate, ensure_theme_schema(themes))
    if row is None or score < .78:
        return []
    return [{"theme_id": str(row.get("theme_id", "")), "theme_name": str(row.get("theme_name", "")),
             "similarity": round(score, 3), "exact": normalize_theme_text(draft["theme_name"]) == normalize_theme_text(row.get("theme_name"))}]


def _source_fields(keys, issue_records):
    by_key = {str(item.get("issue_key") or f"I{index:02d}"): item
              for index, item in enumerate(issue_records, 1)}
    verified = [key for key in _dedupe_strings(keys or [], 5) if key in by_key]
    summary = " · ".join(str(by_key[key].get("issue_title") or by_key[key].get("title") or "") for key in verified)
    return verified, _clean_short_text(summary, 300)


def _resolve(suggestions, contents, existing, excluded, resolver):
    if resolver is None:
        from content_pipeline import resolve_director_contents
        resolver = resolve_director_contents
    # Actual title identity/poster checks are shared with the existing content
    # pipeline. They do not authorize or infer B tv availability.
    rows = resolver(suggestions, contents, existing_candidates=existing, limit=10, excluded_titles=excluded)
    if not isinstance(rows, list):
        raise ThemeGenerationError("추천 작품의 확인 결과를 읽지 못했습니다. 다시 시도해 주세요.")
    return deepcopy(rows[:10])


def _proposal(raw, themes, contents, issues, resolver, existing_candidates):
    draft = {"draft_id": "draft-" + uuid4().hex[:12],
             "theme_name": _clean_short_text(raw.get("theme_name"), 80),
             "copy": _clean_short_text(raw.get("copy"), 180),
             "rationale": _clean_short_text(raw.get("rationale"), 800),
             "genre": _clean_short_text(raw.get("genre"), 80), "mood": _clean_short_text(raw.get("mood"), 80),
             "keywords": _dedupe_strings(raw.get("keywords") or [], 12),
             "content_search_terms": _dedupe_strings(raw.get("content_search_terms") or [], 8),
             "creation_angle": _clean_short_text(raw.get("creation_angle"), 40), "excluded_titles": []}
    if not draft["theme_name"]:
        raise ThemeGenerationError("테마명이 비어 있어 초안을 만들지 못했습니다.")
    draft["source_issue_keys"], draft["source_issue"] = _source_fields(raw.get("source_issue_keys"), issues)
    draft["contents"] = _resolve(raw.get("contents") or [], contents, existing_candidates, [], resolver)
    draft["content_warning"] = "" if len(draft["contents"]) >= 5 else f"실제 정보를 확인한 작품은 {len(draft['contents'])}편입니다. 확인되지 않은 작품으로 채우지 않았습니다."
    draft["duplicate_matches"] = _duplicate_matches(draft, themes)
    return draft


def _updated_exclusions(draft, raw, request):
    existing = _dedupe_strings(draft.get("excluded_titles") or [], 30)
    incoming = _dedupe_strings(raw.get("excluded_titles") or [], 30)
    clear_all = bool(re.search(r"제외\s*(?:조건|목록|설정).{0,15}(?:초기화|해제|비워|삭제)", request))
    if clear_all:
        return incoming
    result = _dedupe_strings([*existing, *incoming], 30)
    # A model's omitted or empty list never clears earlier constraints. A
    # selective re-inclusion also needs the actual title in the user's request;
    # the model cannot silently lift another work's exclusion.
    if re.search(r"다시\s*(?:포함|추천|넣)|제외.{0,8}해제", request):
        normalized_request = normalize_theme_text(request)
        allowed = {normalize_theme_text(title) for title in raw.get("reincluded_titles") or []
                   if normalize_theme_text(title) and normalize_theme_text(title) in normalized_request}
        result = [title for title in result if normalize_theme_text(title) not in allowed]
    return result


def _explicit_append(request):
    if re.search(r"추가\s*(?:하지|말고|없이)|기존.{0,10}(?:지우|삭제|버리|없애)|(?:전부|모두)\s*새로|처음부터", request):
        return False
    return bool(re.search(r"(?:하나|한\s*(?:개|가지)|\d+\s*개)\s*더|추가(?:해|하|로)|더\s*만들", request))


def _apply_edit(draft, raw, *, scope, themes, contents, issues, resolver, existing_candidates, request):
    result = deepcopy(draft)
    allowed = SCOPE_FIELDS[scope]
    for key in allowed - {"contents", "source_issue_keys", "excluded_titles"}:
        if raw.get(key) is None:
            continue
        if key in {"keywords", "content_search_terms"}:
            result[key] = _dedupe_strings(raw[key], 12)
        else:
            result[key] = _clean_short_text(raw[key], 800 if key == "rationale" else 180)
    if "excluded_titles" in allowed:
        result["excluded_titles"] = _updated_exclusions(draft, raw, request)
    if not result.get("theme_name"):
        raise ThemeGenerationError("테마명은 비워둘 수 없습니다.")
    if "source_issue_keys" in allowed and raw.get("source_issue_keys") is not None:
        result["source_issue_keys"], result["source_issue"] = _source_fields(raw["source_issue_keys"], issues)
    if "contents" in allowed and raw.get("contents") is not None:
        saved = list(draft.get("contents", []))
        if isinstance(existing_candidates, list):
            saved.extend(existing_candidates)
        elif isinstance(existing_candidates, pd.DataFrame):
            saved.extend(existing_candidates.fillna("").to_dict("records"))
        result["contents"] = _resolve(raw["contents"], contents, saved, result.get("excluded_titles", []), resolver)
        result["content_warning"] = "" if len(result["contents"]) >= 5 else f"실제 정보를 확인한 작품은 {len(result['contents'])}편입니다."
    elif "excluded_titles" in allowed:
        banned = {normalize_theme_text(name) for name in result["excluded_titles"]}
        result["contents"] = [row for row in result.get("contents", []) if normalize_theme_text(row.get("title")) not in banned]
    if "theme_name" in allowed:
        result["duplicate_matches"] = _duplicate_matches(result, themes)
    return result


def restore_director_version(session, version_id, *, target_draft_id=None):
    state = _session(session)
    version = next((row for row in state["versions"] if row.get("version_id") == version_id), None)
    if not version:
        raise ThemeGenerationError("복원할 버전을 찾지 못했습니다.")
    if target_draft_id:
        original = next((row for row in version["drafts"] if row.get("draft_id") == target_draft_id), None)
        current = _find_draft(state, target_draft_id)
        if original is None or current is None:
            raise ThemeGenerationError("선택한 버전에 이 테마의 이전 초안이 없습니다.")
        state["drafts"] = [deepcopy(original) if row.get("draft_id") == target_draft_id else row for row in state["drafts"]]
        _version(state, "선택한 테마의 이전 버전 복원", kind="restore", restored_from=version_id,
                 restored_target_draft_id=target_draft_id)
    else:
        state["drafts"] = deepcopy(version["drafts"])
        state["current_version_id"] = version_id
    state["messages"].append({"role": "assistant", "content": f"{version_id}의 초안으로 되돌렸습니다. 채택해 저장한 테마는 변경하지 않았습니다."})
    state["last_usage"] = {"api_call_count": 0}
    return state


def _undo_version(state, target_draft_id=None):
    by_id = {row["version_id"]: row for row in state["versions"]}
    current = by_id.get(state.get("current_version_id"))
    while current:
        # A per-theme restore keeps other themes at the current global version.
        # Continue that theme's history from its restored snapshot, rather than
        # treating the discarded edit as the next "previous" version.
        if target_draft_id and current.get("restored_target_draft_id") == target_draft_id:
            current = by_id.get(current.get("restored_from"))
            continue
        prior = by_id.get(current.get("parent_version_id"))
        if prior is None:
            break
        if not target_draft_id:
            return prior["version_id"]
        old = next((row for row in prior["drafts"] if row.get("draft_id") == target_draft_id), None)
        if old and old != _find_draft(state, target_draft_id):
            return prior["version_id"]
        current = prior
    raise ThemeGenerationError("더 이전의 수정 버전이 없습니다.")


def run_director_turn(session, request, *, themes, contents, issue_records=None, api_key="", model="gpt-5.6-luna",
                      target_draft_id=None, edit_scope="auto", content_resolver=None, client=None,
                      existing_candidates=None, operation="auto"):
    """Run one real conversational turn, independently of collected issues.

    ``issue_records`` is accepted for callers from the previous release; issues
    are deliberately not used as a hidden brief for the creative director.
    """
    state = _session(session)
    request = str(request or "").strip()
    if not request or len(request) > 6000:
        raise ThemeGenerationError("요청을 1~6,000자 이내로 입력해 주세요.")
    if edit_scope not in EDIT_SCOPES:
        raise ThemeGenerationError("수정 범위를 다시 선택해 주세요.")
    if operation not in {"auto", "create"}:
        raise ThemeGenerationError("기획 작업을 다시 선택해 주세요.")
    # A dedicated new-planning button carries an explicit intent. Quoted source
    # copy (e.g. '제목만 바꾼 관계') must never be parsed as an edit command.
    target = None if operation == "create" else _requested_target(state, request, target_draft_id)
    scope = "auto" if operation == "create" else _requested_scope(request, edit_scope)
    undo_request = re.sub(r"^\s*[1-9][0-9]?\s*번(?:째)?(?:\s*테마)?\s*", "", request)
    if operation == "auto" and re.fullmatch(r"(?:(?:이전|앞선|직전)\s*(?:버전|초안)(?:으로)?\s*(?:되돌려(?:줘)?|돌아가(?:줘)?|돌려줘|복원해(?:줘)?)?|되돌리기|실행\s*취소|undo)[.!?\s]*", undo_request, re.I):
        state["messages"].append({"role": "user", "content": request})
        return restore_director_version(state, _undo_version(state, target), target_draft_id=target)
    plan, usage = generate_director_plan(request=request, messages=state["messages"], drafts=state["drafts"],
                    themes=themes, contents=contents, issue_records=[], api_key=api_key, model=model,
                    target_draft_id=target, edit_scope=scope, client=client,
                    saved_theme_matches=state.get("library_search_results", []), operation=operation)
    raw = plan.model_dump(by_alias=True) if hasattr(plan, "model_dump") else deepcopy(plan)
    action = raw.get("action")
    if operation == "create" and action != "create":
        raise ThemeGenerationError("새 기획 요청에 맞는 초안이 반환되지 않았습니다. 기존 초안은 유지했습니다.")
    search_intent = _library_search_intent(raw.get("library_search"))
    if search_intent is not None and action != "reply":
        raise ThemeGenerationError("검색과 초안 변경이 함께 반환되어 적용하지 않았습니다. 다시 요청해 주세요.")
    if action == "create":
        if raw.get("edits") or not 1 <= len(raw.get("themes", [])) <= 10:
            raise ThemeGenerationError("신규 테마 결과가 올바르지 않습니다. 다시 요청해 주세요.")
        if state["drafts"] and scope != "auto":
            raise ThemeGenerationError("새 테마를 만들려면 수정 범위를 전체 대화로 바꿔 주세요.")
        requested = re.search(r"테마\s*(\d{1,2})\s*개", request)
        if requested and min(int(requested.group(1)), 10) != len(raw["themes"]):
            raise ThemeGenerationError("요청한 개수의 테마를 만들지 못했습니다. 다시 시도해 주세요.")
        generated = [_proposal(item, themes, contents, [], content_resolver, existing_candidates) for item in raw["themes"]]
        keep = state["drafts"] if operation == "create" or _explicit_append(request) or raw.get("creation_mode") == "append" else []
        names = [normalize_theme_text(row["theme_name"]) for row in [*keep, *generated]]
        if len(set(names)) != len(names):
            raise ThemeGenerationError("새 초안의 테마명이 서로 겹칩니다. 다른 관점으로 다시 요청해 주세요.")
        state["drafts"] = [*keep, *generated]
    elif action == "revise":
        edits = raw.get("edits", [])
        if raw.get("themes") or not edits:
            raise ThemeGenerationError("수정할 초안을 확인하지 못했습니다.")
        touched = set()
        for edit in edits:
            draft_id = edit.get("target_draft_id")
            if target and draft_id != target:
                raise ThemeGenerationError("요청한 테마와 다른 초안의 수정 결과가 와서 적용하지 않았습니다.")
            if draft_id in touched or not _find_draft(state, draft_id):
                raise ThemeGenerationError("수정 대상이 올바르지 않아 초안을 유지했습니다.")
            touched.add(draft_id)
            effective = scope if scope != "auto" else edit.get("scope")
            if effective not in SCOPE_FIELDS:
                raise ThemeGenerationError("수정 범위를 확인하지 못했습니다.")
            current = _find_draft(state, draft_id)
            updated = _apply_edit(current, edit, scope=effective, themes=themes, contents=contents,
                                  issues=[], resolver=content_resolver, existing_candidates=existing_candidates, request=request)
            state["drafts"] = [updated if row["draft_id"] == draft_id else row for row in state["drafts"]]
    elif action != "reply":
        raise ThemeGenerationError("AI 응답 형식을 읽지 못했습니다. 초안은 변경하지 않았습니다.")
    # Preserve paragraph/list breaks in a useful conversation rather than
    # flattening several proposed directions into one unreadable paragraph.
    message = str(raw.get("message") or "").strip()[:2000]
    if not message:
        raise ThemeGenerationError("AI의 답변이 비어 있습니다. 초안을 유지했으니 다시 요청해 주세요.")
    state["messages"].extend([{"role": "user", "content": request},
                              {"role": "assistant", "content": message}])
    state["last_usage"] = usage
    state["library_search_intent"] = search_intent
    if search_intent is not None:
        state["library_search_results"] = []
    if action in {"create", "revise"}:
        _version(state, request, kind=action)
    # No arbitrary model objects or DataFrame values may leak into session state.
    json.dumps(state, ensure_ascii=False, allow_nan=False)
    return state
