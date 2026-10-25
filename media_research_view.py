"""Project-scoped business research conversation; models run only on submission."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import html
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

import streamlit as st

from media_insights_analysis import MediaInsightError
import media_research_storage as store


PROJECT_KEY = "media_research_project"
CODE_KEY = "media_research_recovery_code"
STATUS_KEY = "media_research_storage_status"
_PENDING = "media_research_unsaved_turn"
_ERROR = "media_research_error"
_FAILED = "media_research_failed_question"
_SEARCH_ERRORS = frozenset({
    "empty_question", "search_not_configured", "search_unavailable", "search_incomplete",
    "no_primary_citations", "primary_fetch_unavailable", "primary_http_unavailable",
    "primary_timeout", "primary_redirect_refused", "primary_too_large", "primary_document_unusable",
})


def _text(value, limit=20000):
    return str(value or "").strip()[:limit]


def _plain(value, limit=20000):
    """Keep model/user text as text rather than executable HTML or Markdown."""
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(_text(value, limit)))


def _list(value):
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _key(value):
    return hashlib.sha256(_text(value).encode()).hexdigest()[:16]


def _bounded_count(value, maximum=100):
    return min(maximum, max(0, value)) if type(value) is int else 0


def _safe_url(value):
    value = _text(value, 6000)
    try:
        if any(ord(character) < 33 for character in value):
            return ""
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
                or host == "localhost" or host.endswith((".localhost", ".local"))):
            return ""
        parsed.port
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return value
    except (ValueError, TypeError):
        return ""


def _date(value):
    try:
        return datetime.fromisoformat(_text(value).replace("Z", "+00:00")).strftime("%Y.%m.%d")
    except (ValueError, TypeError):
        return "발표일 확인 필요"


def _store_options(config):
    config = dict(config or {})
    directory = config.pop("storage_dir", store.DEFAULT_DIR)
    return {"storage_dir": directory, "github_config": config.pop("github_config", config)}


def _accept(result):
    st.session_state[PROJECT_KEY] = deepcopy(result["project"])
    st.session_state[STATUS_KEY] = result.get("status", "local_only")
    if result.get("recovery_code"):
        st.session_state[CODE_KEY] = result["recovery_code"]
    st.session_state.pop(_ERROR, None)


def _reset_project():
    for key in (PROJECT_KEY, CODE_KEY, STATUS_KEY, _PENDING, _ERROR, _FAILED,
                "media_research_project_title", "media_research_brief_editor",
                "media_research_open_code", "media_research_show_all"):
        st.session_state.pop(key, None)


def _open_project(config):
    try:
        code = _text(st.session_state.get("media_research_open_code"), 200)
        result = store.open_project(code, **_store_options(config))
        _reset_project()
        _accept(result)
        st.session_state[CODE_KEY] = code
    except store.ResearchStorageError as error:
        st.session_state[_ERROR] = str(error)
    except Exception:
        st.session_state[_ERROR] = "프로젝트를 열지 못했습니다. 복구 코드와 연결을 확인해 주세요."


def _ensure_project(config, question=""):
    if st.session_state.get(PROJECT_KEY) and st.session_state.get(CODE_KEY):
        return
    title = _text(st.session_state.get("media_research_project_title"), 200) or _text(question, 60) or "사업 리서치"
    brief = _text(st.session_state.get("media_research_brief_editor"), 20000)
    _accept(store.create_project(title, brief, **_store_options(config)))


def _save_brief(config):
    try:
        _ensure_project(config)
        brief = _text(st.session_state.get("media_research_brief_editor"), 20000)
        _accept(store.update_brief(st.session_state[CODE_KEY], brief, **_store_options(config)))
    except store.ResearchStorageError as error:
        st.session_state[_ERROR] = str(error)
    except Exception:
        st.session_state[_ERROR] = "사업 조건을 저장하지 못했습니다. 작성한 내용은 그대로 유지했습니다."


def _persist_pending(config):
    pending = st.session_state.get(_PENDING)
    if not pending:
        return
    try:
        _accept(store.append_turn(st.session_state[CODE_KEY], pending["question"], pending["answer"],
                                  turn_id=pending["id"], **_store_options(config)))
        st.session_state.pop(_PENDING, None)
        st.session_state.pop(_FAILED, None)
    except store.ResearchStorageError as error:
        st.session_state[_ERROR] = str(error)
    except Exception:
        st.session_state[_ERROR] = "답변을 저장하지 못했습니다. 답변은 현재 접속에 보관했습니다."


def _run_question(question, on_ask, config):
    try:
        _ensure_project(config, question)
        # A second browser may have added a decision or revised the brief.
        # Refresh the project, while leaving unsaved editor widget text intact.
        _accept(store.open_project(st.session_state[CODE_KEY], **_store_options(config)))
        with st.spinner("질문에 맞는 자료와 이전 논의를 연결하고 있어요…"):
            answer = on_ask(question, deepcopy(st.session_state[PROJECT_KEY]))
        if not isinstance(answer, Mapping) or not any(answer.get(key) for key in ("headline", "facts", "insights", "options", "recommendation", "missing_information")):
            raise ValueError("empty_answer")
        st.session_state[_PENDING] = {"id": uuid.uuid4().hex, "question": question, "answer": deepcopy(dict(answer))}
        st.session_state.pop(_FAILED, None)
        _persist_pending(config)
    except (MediaInsightError, store.ResearchStorageError) as error:
        st.session_state[_ERROR] = str(error)
        st.session_state[_FAILED] = question
    except Exception:
        st.session_state[_ERROR] = "답변을 마무리하지 못했습니다. 기존 대화는 유지했습니다. 잠시 후 다시 보내 주세요."
        st.session_state[_FAILED] = question


def _decision_action(operation, identifier, input_key, config):
    try:
        code = st.session_state[CODE_KEY]
        options = _store_options(config)
        if operation == "remove":
            result = store.remove_decision(code, identifier, **options)
        elif operation == "update":
            result = store.update_decision(code, identifier, _text(st.session_state.get(input_key), 6000), **options)
        else:
            result = store.approve_decision(code, identifier, _text(st.session_state.get(input_key), 6000), **options)
        _accept(result)
    except store.ResearchStorageError as error:
        st.session_state[_ERROR] = str(error)
    except Exception:
        st.session_state[_ERROR] = "결정을 저장하지 못했습니다. 기존 기록은 유지했습니다."


def _render_sources(answer):
    sources = [source for source in _list(answer.get("sources")) if _safe_url(source.get("url"))]
    if not sources:
        return
    with st.expander(f"근거 자료 · {len(sources)}건", expanded=False):
        for number, source in enumerate(sources, 1):
            title = _text(source.get("title"), 300) or "공식 원문"
            st.link_button(str(number) + ". " + _plain(title, 300) + " ↗", _safe_url(source["url"]),
                           width="stretch")
            published = _date(source.get("published_at"))
            date_label = "발표일 미표기" if published == "발표일 확인 필요" else "발표일 " + published
            checked = source.get("reference_checked_at") or source.get("collected_at")
            if checked and _date(checked) != "발표일 확인 필요":
                date_label += " · 확인일 " + _date(checked)
            st.caption(_plain(date_label + " · " + _text(source.get("locator"), 120)))
            if source.get("excerpt"):
                st.markdown("> " + _plain(source["excerpt"], 1200))


def _render_answer(answer, snapshot):
    sources = [source for source in _list(answer.get("sources")) if _safe_url(source.get("url"))]
    source_numbers = {_text(source.get("id")): number for number, source in enumerate(sources, 1)}
    search = answer.get("source_search") if isinstance(answer.get("source_search"), Mapping) else {}
    status = answer.get("status") if answer.get("status") in {"grounded", "conditional"} else "unknown"
    search_status = search.get("status") if search.get("status") in {"archive_only", "enriched"} else "not_attempted"
    model = _text(answer.get("model"), 80)
    if not re.fullmatch(r"(?:gpt|o[0-9]|chatgpt|test)[A-Za-z0-9_.:-]{0,70}", model):
        model = ""
    revision = _text(answer.get("source_engine_revision"), 40)
    if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,40}", revision):
        revision = ""
    added = search.get("documents_added")
    added = min(100, max(0, added)) if isinstance(added, int) else 0
    search_completed = "true" if search.get("search_completed") is True else "false"
    citations_found = _bounded_count(search.get("citations_found"))
    fetch_attempted = _bounded_count(search.get("fetch_attempted"), 4)
    errors = search.get("errors") if isinstance(search.get("errors"), list) else []
    search_errors = ",".join(sorted({error for error in errors if isinstance(error, str) and error in _SEARCH_ERRORS}))
    provider = search.get("provider_code")
    if not isinstance(provider, str) or not re.fullmatch(r"(?:not_called|none|unknown|request_timeout|api_connection|invalid_api_key|insufficient_quota|model_not_found|unsupported_parameter|invalid_value|rate_limit_exceeded|http_(?:400|401|403|404|408|409|422|429|500|502|503|504))", provider):
        provider = "unknown"
    fallback = "true" if search.get("fallback_used") is True else "false"
    http_statuses = search.get("fetch_http_statuses")
    http_statuses = ",".join(str(number) for number in sorted(set(
        value for value in http_statuses if type(value) is int and 100 <= value <= 599))) if isinstance(http_statuses, list) else ""
    st.markdown('<span class="media-research-result" hidden aria-hidden="true" data-fact-count="'
                + str(len(_list(answer.get("facts")))) + '" data-source-count="' + str(len(sources))
                + '" data-option-count="' + str(len(_list(answer.get("options")))) + '" data-status="'
                + status + '" data-engine-version="' + html.escape(revision, quote=True)
                + '" data-model="' + html.escape(model, quote=True) + '" data-search-status="'
                + search_status + '" data-added-documents="' + str(added)
                + '" data-search-completed="' + search_completed + '" data-citations-found="' + str(citations_found)
                + '" data-fetch-attempted="' + str(fetch_attempted)
                + '" data-search-errors="' + html.escape(search_errors, quote=True)
                + '" data-search-provider="' + provider + '" data-fallback-used="' + fallback
                + '" data-fetch-statuses="' + http_statuses + '"></span>', unsafe_allow_html=True)
    headline = _text(answer.get("headline"), 300)
    if headline:
        st.markdown("**" + _plain(headline) + "**")
    facts = _list(answer.get("facts"))
    if facts:
        st.markdown("**확인한 사실**")
        for fact in facts:
            st.markdown("- " + _plain(fact.get("text")))
            identifiers = fact.get("evidence_ids")
            if isinstance(identifiers, list) and identifiers:
                numbers = list(dict.fromkeys(source_numbers[identifier] for identifier in identifiers if identifier in source_numbers))
                if numbers:
                    st.caption("근거 자료 " + " · ".join(str(number) for number in numbers))
    insights = _list(answer.get("insights"))
    if insights:
        st.markdown("**사업에 주는 시사점**")
        for insight in insights:
            st.markdown("- " + _plain(insight.get("text")))
    options = _list(answer.get("options"))
    for index, option in enumerate(options, 1):
        with st.container(border=True):
            st.markdown("**" + str(index) + ". " + _plain(option.get("title") or "검토안") + "**")
            for label, field in (("제안", "proposal"), ("이유", "rationale"), ("고려할 점", "tradeoffs"), ("검증 방법", "validation")):
                value = option.get(field)
                if isinstance(value, list):
                    value = "\n".join(_text(item) for item in value)
                if _text(value):
                    st.markdown("**" + label + "**  \n" + _plain(value))
    recommendation = answer.get("recommendation")
    recommendation = recommendation.get("text") if isinstance(recommendation, Mapping) else recommendation
    if _text(recommendation):
        st.markdown("**추천하는 방향**")
        st.markdown(_plain(recommendation))
    for label, field in (("제안의 가정", "assumptions"), ("더 확인할 정보", "missing_information"), ("다음에 할 일", "next_steps")):
        values = answer.get(field)
        if isinstance(values, list) and any(_text(value) for value in values):
            with st.expander(label, expanded=field == "missing_information"):
                for value in values:
                    if _text(value):
                        st.markdown("- " + _plain(value))
    context = answer.get("context_hashes")
    if isinstance(context, Mapping):
        current = {_text(item.get("id")): item.get("content_hash") for item in _list(snapshot.get("items"))}
        if any(identifier in current and current[identifier] != source_hash for identifier, source_hash in context.items()):
            st.caption("이 답변 이후 원문이 갱신됐습니다. 최신 자료를 반영하려면 이어서 질문해 주세요.")
    if isinstance(search, Mapping) and search.get("attempted") and not search.get("documents_added"):
        st.caption("최신 원문을 추가로 확보하지 못해 저장된 자료를 참고했습니다.")
    _render_sources(answer)


def _render_management(config):
    project = st.session_state.get(PROJECT_KEY) or {}
    # Stable widget defaults preserve a draft when a new model response or a
    # concurrently saved project changes the materialized business brief.
    st.session_state.setdefault("media_research_project_title", _text(project.get("title"), 200))
    st.session_state.setdefault("media_research_brief_editor", _text(project.get("brief")))
    with st.expander("사업 조건·프로젝트 관리", expanded=False):
        with st.form("media_research_brief_form"):
            st.text_input("프로젝트 이름", max_chars=200,
                          key="media_research_project_title", disabled=bool(project))
            st.text_area("답변에 반영할 사업 조건", max_chars=20000,
                         key="media_research_brief_editor", height=130,
                         placeholder="상품 구성, 목표 고객, 현재 메시지, 원가·예산, 검토 중인 조건 등을 적어 주세요.")
            st.form_submit_button("사업 조건 저장", on_click=_save_brief, args=(config,))
        if project and st.session_state.get(CODE_KEY):
            st.caption("복구 코드는 암호화된 기록을 다시 여는 열쇠입니다. 잃으면 복구할 수 없습니다.")
            st.code(st.session_state[CODE_KEY], language=None)
            st.download_button("대화 기록 내려받기", json.dumps(project, ensure_ascii=False, indent=2),
                               file_name="media-research-project.json", mime="application/json",
                               key="media_research_download")
        with st.form("media_research_open_form", clear_on_submit=True):
            st.text_input("이전 프로젝트 복구 코드", type="password", key="media_research_open_code")
            st.form_submit_button("프로젝트 이어가기", on_click=_open_project, args=(config,))
        decisions = _list(project.get("decisions"))
        if decisions:
            st.markdown("**채택한 결정**")
            st.caption("다음 질문에서 참고합니다. 바뀐 결정은 수정하거나 제외할 수 있습니다.")
            for decision in decisions:
                identifier = _text(decision.get("id"))
                input_key = "media_research_decision_edit_" + _key(identifier)
                with st.form("media_research_decision_form_" + _key(identifier)):
                    st.text_area("채택한 내용", value=_text(decision.get("text"), 6000), key=input_key,
                                 max_chars=6000, height=90)
                    save, remove = st.columns(2)
                    with save:
                        st.form_submit_button("결정 수정", on_click=_decision_action,
                                              args=("update", identifier, input_key, config))
                    with remove:
                        st.form_submit_button("결정 제외", on_click=_decision_action,
                                              args=("remove", identifier, input_key, config))


def render_media_research(snapshot, *, on_ask=None, storage_config=None):
    """Render a private project without reading any other viewer's projects."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    st.markdown('<style>' + Path(__file__).with_name("media_research_styles.css").read_text(encoding="utf-8") + '</style>',
                unsafe_allow_html=True)
    with st.container(key="media_research_panel", border=True):
        with st.container(key="media_research_header"):
            heading, action = st.columns([4, 1], vertical_alignment="center", gap="small")
            with heading:
                st.markdown('<header class="media-research-heading"><span>BUSINESS RESEARCH</span>'
                            '<h3>질문에서 다음 결정까지</h3>'
                            '<p>국내외 자료와 이전 논의를 연결해, 사업에 필요한 답을 함께 검토합니다.</p></header>',
                            unsafe_allow_html=True)
            with action:
                st.button("새 대화", key="media_research_new", on_click=_reset_project, width="stretch")
        _render_management(storage_config)
        project = st.session_state.get(PROJECT_KEY) or {}
        storage_status = st.session_state.get(STATUS_KEY)
        storage_status = storage_status if storage_status in {"local_only", "remote_synced"} else "not_started"
        st.markdown('<span class="media-research-state" hidden aria-hidden="true" data-turn-count="'
                    + str(len(_list(project.get("turns")))) + '" data-decision-count="'
                    + str(len(_list(project.get("decisions")))) + '" data-storage-status="' + storage_status
                    + '"></span>', unsafe_allow_html=True)
        if project:
            status = st.session_state.get(STATUS_KEY)
            st.caption(_plain(project.get("title") or "사업 리서치") + " · " +
                       ("암호화 백업 완료" if status == "remote_synced" else "현재 앱에 저장 · 영구 백업 미연결"))
        turns = _list(project.get("turns"))
        if len(turns) > 10:
            show_all = st.checkbox(f"이전 대화 포함 전체 {len(turns)}개 보기", key="media_research_show_all")
            if not show_all:
                turns = turns[-10:]
        for turn in turns:
            identifier = _text(turn.get("id"))
            with st.chat_message("user"):
                st.markdown(_plain(turn.get("question")))
            with st.chat_message("assistant"):
                answer = turn.get("answer") if isinstance(turn.get("answer"), Mapping) else {}
                _render_answer(answer, snapshot)
                recommendation = answer.get("recommendation")
                recommendation = recommendation.get("text") if isinstance(recommendation, Mapping) else recommendation
                if _text(recommendation):
                    adopted = any(_text(decision.get("turn_id")) == identifier for decision in _list(project.get("decisions")))
                    with st.expander("채택한 결정" if adopted else "이 제안을 결정으로 저장", expanded=False):
                        input_key = "media_research_approve_text_" + _key(identifier)
                        with st.form("media_research_approve_form_" + _key(identifier)):
                            st.text_area("기억할 결론", value=_text(recommendation, 6000), max_chars=6000,
                                         key=input_key, height=100, disabled=adopted)
                            st.form_submit_button("채택 완료" if adopted else "이 결론 채택", disabled=adopted,
                                                  on_click=_decision_action,
                                                  args=("approve", identifier, input_key, storage_config))
        pending = st.session_state.get(_PENDING)
        if pending:
            with st.chat_message("user"):
                st.markdown(_plain(pending["question"]))
            with st.chat_message("assistant"):
                _render_answer(pending["answer"], snapshot)
            st.warning("답변이 아직 저장되지 않았습니다. 저장을 다시 시도해 주세요.")
            st.button("답변 저장 다시 시도", key="media_research_retry_save", on_click=_persist_pending,
                      args=(storage_config,))
        if st.session_state.get(_ERROR):
            st.warning(st.session_state[_ERROR])
        failed = st.session_state.get(_FAILED)
        if failed and on_ask and not pending:
            st.caption("보내지 못한 질문: " + _plain(failed, 1000))
            if st.button("질문 다시 보내기", key="media_research_retry_question"):
                _run_question(failed, on_ask, storage_config)
                st.rerun()
        if on_ask is None:
            st.caption("리서치 대화는 AI 연결 후 사용할 수 있습니다. 저장한 기록은 계속 확인할 수 있습니다.")
        else:
            st.caption("질문을 보내면 최신 공개 자료 검색과 AI 분석을 실행합니다.")
        with st.container(key="media_research_composer"):
            question = st.chat_input("사업에 필요한 질문을 자유롭게 적어 주세요.", key="media_research_question",
                                     max_chars=10000, disabled=on_ask is None or bool(pending))
        if question and on_ask:
            _run_question(_text(question, 10000), on_ask, storage_config)
            st.rerun()
