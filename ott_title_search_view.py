"""Explicit title lookup, kept separate from the public OTT home collection."""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ott_provider_branding import subscription_provider_cards, render_provider_logos_html


_LOGGER = logging.getLogger(__name__)
_STATE_KEY = "ott_title_search_state"
_CHOICE_KEY = "ott_title_search_choice"


def _text(value, limit=500):
    return str(value).strip()[:limit] if value is not None else ""


def _escape(value):
    return html.escape(_text(value, 6000), quote=True)


def _url(value, *, kinolights=False):
    value = _text(value, 6000)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
        if kinolights and not (parsed.hostname == "kinolights.com" or parsed.hostname.endswith(".kinolights.com")):
            return ""
    except ValueError:
        return ""
    return value


def _display_time(value):
    try:
        when = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo("Asia/Seoul")).strftime("%m.%d %H:%M") + " KST"
    except (TypeError, ValueError, OverflowError):
        return ""


def _candidate_label(candidate):
    title = _text(candidate.get("title"), 200)
    year = _text(candidate.get("year"), 20)
    kind = _text(candidate.get("content_type"), 30)
    kind = {"movie": "영화", "film": "영화", "tv": "시리즈", "series": "시리즈", "drama": "드라마"}.get(kind, kind)
    return title + (" (" + year + ")" if year else "") + (" · " + kind if kind else "")


def _normalized_title(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", _text(value, 200))).strip().casefold()


def _source_meta(result):
    parts = []
    when = _display_time(result.get("fetched_at"))
    if when:
        parts.append(_escape(when) + (" 확인 시도" if result.get("status") == "fetch_error" else " 확인"))
    source = _url(result.get("source_url"), kinolights=True)
    if source:
        parts.append('<a href="' + _escape(source) + '" target="_blank" rel="noopener noreferrer">키노라이츠 원문 ↗</a>')
    return '<p class="ott-title-lookup-meta">' + ' · '.join(parts) + '</p>' if parts else ""


def title_availability_html(result):
    """Render source-confirmed offers without treating missing data as absence."""
    result = result if isinstance(result, Mapping) else {}
    status = _text(result.get("status"))
    title = _candidate_label(result)
    providers = subscription_provider_cards(result) if status == "available" else []
    heading = '<h3 class="ott-title-result-title">' + _escape(title) + '</h3>' if title else ""
    if providers:
        body = '<p class="ott-title-lookup-note">구독으로 볼 수 있는 곳</p><div class="ott-title-services" aria-label="구독 서비스">' + render_provider_logos_html(providers) + '</div>'
    else:
        message = {
            "available": "확인된 구독 서비스가 없습니다.",
            "unavailable": "키노라이츠에 표시된 시청 서비스가 없습니다.",
            "fetch_error": "시청 정보를 불러오지 못했습니다. 잠시 후 다시 검색해 주세요.",
            "identity_mismatch": "선택한 작품과 원문 정보가 달라 시청 정보를 표시하지 않았습니다.",
        }.get(status, "시청 서비스 정보를 확인하지 못했습니다. 키노라이츠 원문에서 확인해 주세요.")
        body = '<p class="ott-title-lookup-note" role="status">' + _escape(message) + '</p>'
    return '<div class="ott-title-result" data-status="' + _escape(status) + '" data-provider-count="' + str(len(providers)) + '">' + heading + body + _source_meta(result) + '</div>'


def _search_titles(query):
    from kinolights_availability import search_titles
    return search_titles(query)


def _get_title_availability(candidate):
    from kinolights_availability import get_title_availability
    return get_title_availability(candidate)


def _lookup(candidate):
    try:
        result = _get_title_availability(candidate)
        if not isinstance(result, Mapping):
            result = {"status": "unknown"}
    except Exception:
        _LOGGER.exception("Kinolights title availability lookup failed")
        result = {"status": "fetch_error", "fetched_at": datetime.now(timezone.utc).isoformat()}
    return {**candidate, **result}


def _reset_search():
    import streamlit as st
    st.session_state.pop(_STATE_KEY, None)
    st.session_state.pop(_CHOICE_KEY, None)
    st.session_state["ott_title_search_query"] = ""


def _choose_title():
    """A direct dropdown selection is an explicit source lookup."""
    import streamlit as st
    state = st.session_state.get(_STATE_KEY, {})
    candidates = state.get("candidates", [])
    choice = st.session_state.get(_CHOICE_KEY)
    if isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < len(candidates):
        state["choice"] = choice
        state["result"] = _lookup(candidates[choice])
        state.pop("message", None)
        st.session_state[_STATE_KEY] = state


def render_ott_title_search():
    """Only submitted search and explicit dropdown selection query the source."""
    import streamlit as st

    state = st.session_state.get(_STATE_KEY, {})
    if "ott_title_search_query" not in st.session_state:
        st.session_state["ott_title_search_query"] = state.get("query", "")
    with st.container(key="ott_title_search"):
        with st.form("ott_title_search_form", border=False):
            entry, action, reset = st.columns([4, 1, 1.2], gap="small", vertical_alignment="bottom")
            with entry:
                query = st.text_input("작품으로 OTT 찾기", placeholder="영화·시리즈 제목을 입력하세요", max_chars=160,
                                      key="ott_title_search_query", label_visibility="collapsed")
            with action:
                submitted = st.form_submit_button("검색", key="ott_title_search_submit", width="stretch")
            with reset:
                st.form_submit_button("초기화", key="ott_title_search_reset", on_click=_reset_search, width="stretch")
        if submitted:
            query = query.strip()
            state = {"query": query}
            st.session_state.pop(_CHOICE_KEY, None)
            if not query:
                state["message"] = "검색할 작품 제목을 입력해 주세요."
            else:
                try:
                    with st.spinner("작품을 찾고 있습니다…"):
                        found = _search_titles(query)
                        candidates = [dict(item) for item in found[:30] if isinstance(item, Mapping) and _text(item.get("title"))] if isinstance(found, list) else None
                        if candidates is None:
                            raise ValueError("Invalid Kinolights search response")
                        exact = [item for item in candidates if _normalized_title(item["title"]) == _normalized_title(query)]
                        if exact:
                            candidates = exact
                        state["candidates"] = candidates
                        if len(candidates) == 1:
                            state["result"] = _lookup(candidates[0])
                        elif not candidates:
                            state["message"] = "일치하는 작품을 찾지 못했습니다. 제목을 확인해 주세요."
                except Exception:
                    _LOGGER.exception("Kinolights title search failed")
                    state["message"] = "검색 결과를 불러오지 못했습니다. 잠시 후 다시 검색해 주세요."
            st.session_state[_STATE_KEY] = state
        candidates = state.get("candidates", [])
        if len(candidates) > 1:
            if _CHOICE_KEY not in st.session_state and isinstance(state.get("choice"), int):
                st.session_state[_CHOICE_KEY] = state["choice"]
            st.selectbox("검색 결과", options=list(range(len(candidates))), index=None,
                         placeholder=f"검색 결과 {len(candidates)}건 · 작품 선택",
                         format_func=lambda index: _candidate_label(candidates[index]),
                         key=_CHOICE_KEY, on_change=_choose_title, label_visibility="collapsed")
        if state.get("message"):
            st.markdown('<p class="ott-title-lookup-note" role="status">' + _escape(state["message"]) + '</p>', unsafe_allow_html=True)
        if state.get("result"):
            st.markdown(title_availability_html(state["result"]), unsafe_allow_html=True)
