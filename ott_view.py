"""Public OTT home comparison. Rendering never fetches or invents content."""

from __future__ import annotations

import html
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo


OTT_SERVICES = (
    ("netflix", "NETFLIX", "https://www.netflix.com/kr/"),
    ("tving", "TVING", "https://www.tving.com/"),
    ("coupang", "쿠팡플레이", "https://www.coupangplay.com/"),
    ("disney", "Disney+", "https://www.disneyplus.com/ko-kr"),
    ("wavve", "wavve", "https://www.wavve.com/"),
)
KIND_LABELS = {
    "theme": "기획 테마", "new": "신작", "ranking": "순위",
    "genre": "장르", "live": "라이브", "shorts": "쇼츠", "clips": "클립",
    "promotion": "작품 소개", "collection": "컬렉션 바로가기", "other": "홈 목록",
}
_LOGGER = logging.getLogger(__name__)


def _text(value, limit=2000):
    return str(value).strip()[:limit] if value is not None else ""


def _escape(value, limit=2000):
    return html.escape(_text(value, limit), quote=True)


def _url(value):
    value = _text(value, 6000)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        # Accessing port rejects malformed URLs that should never reach HTML.
        parsed.port
    except ValueError:
        return ""
    return value


def _position(value, fallback):
    try:
        number = int(value)
        return number if number > 0 else fallback
    except (TypeError, ValueError, OverflowError):
        return fallback


def _items(value, limit=100):
    if not isinstance(value, list):
        return []
    return [item for item in value[:limit] if isinstance(item, Mapping)]


def _ordered(value, limit=100):
    rows = [dict(item, position=_position(item.get("position"), index + 1))
            for index, item in enumerate(_items(value, limit))
            if _text(item.get("title"))]
    return sorted(rows, key=lambda item: item["position"])


def _display_time(value):
    value = _text(value, 100)
    if not value:
        return ""
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y.%m.%d %H:%M") + " KST"
    except (TypeError, ValueError, OverflowError):
        return ""


def _platform(raw, fallback):
    service_id, name, url = fallback
    result = dict(raw) if isinstance(raw, Mapping) else {}
    # Platform identity and default links come from the fixed official sources.
    result.update(id=service_id, name=name, url=_url(result.get("url")) or url)
    if result.get("status") not in {"ok", "promotion_only", "unavailable"}:
        result["status"] = "unavailable"
    result["promotions"] = _ordered(result.get("promotions"), 20)
    result["sections"] = _ordered(result.get("sections"), 100)
    result["public_lists"] = _ordered(result.get("public_lists"), 100)
    return result


def snapshot_platforms(snapshot):
    """Return the five configured services, including honest missing states."""
    rows = _items(snapshot.get("platforms")) if isinstance(snapshot, Mapping) else []
    by_id = {_text(row.get("id")): dict(row) for row in rows}
    if isinstance(snapshot, Mapping):
        for row in by_id.values():
            for field in ("observed_environment", "country_verified"):
                if field not in row and field in snapshot:
                    row[field] = snapshot[field]
    return [_platform(by_id.get(service[0]), service) for service in OTT_SERVICES]


def _known_platform(platform):
    service_id = _text(platform.get("id")) if isinstance(platform, Mapping) else ""
    service = next((row for row in OTT_SERVICES if row[0] == service_id), OTT_SERVICES[0])
    return _platform(platform, service)


def _has_saved_content(platform):
    return bool(platform.get("promotions") or platform.get("sections") or platform.get("public_lists"))


def _stale(platform):
    return (platform.get("status") == "unavailable"
            and bool(_display_time(platform.get("last_success_at")))
            and _has_saved_content(platform))


def _promotion_only(platform):
    return (platform.get("status") == "promotion_only"
            or (_stale(platform) and platform.get("last_success_status") == "promotion_only"))


def platform_status(platform):
    """Human-readable status without interpreting failed access as zero themes."""
    if _stale(platform):
        return "이전 확인 자료", "최근 화면을 확인하지 못해 마지막으로 확인한 자료를 보여드립니다."
    if platform.get("status") == "promotion_only":
        return "공개 홍보 화면", "공개 페이지의 홍보작과 작품 목록입니다. 콘텐츠 홈의 기획 테마와 구분해 보여드립니다."
    if platform.get("status") == "ok":
        return "공개 홈 확인", "원래 홈에 배치된 순서대로 보여드립니다."
    if platform.get("checked_at"):
        return "홈 확인 대기", "이번에는 공개 홈을 확인하지 못했습니다. 다음 확인 후 자료가 표시됩니다."
    return "자료 확인 전", "공개 홈을 확인하면 홍보작과 콘텐츠 목록이 여기에 표시됩니다."


def _date_label(platform):
    when = platform.get("last_success_at") or platform.get("checked_at")
    formatted = _display_time(when)
    if not formatted:
        return ""
    if _stale(platform):
        return formatted + " 자료"
    if platform.get("status") == "unavailable":
        return formatted + " 확인 시도"
    return formatted + " 확인"


def _link(url, label, class_name=""):
    safe = _url(url)
    if not safe:
        return ""
    css = ' class="' + _escape(class_name) + '"' if class_name else ""
    return ('<a' + css + ' href="' + _escape(safe) + '" target="_blank" '
            'rel="noopener noreferrer">' + _escape(label) + '</a>')


def _source_url(platform):
    return _url(platform.get("source_url")) or _url(platform.get("url"))


def _artwork(item, *, banner=False):
    title = _text(item.get("title"))
    image_url = _url(item.get("image_url") or item.get("poster_url"))
    shape = "ott-banner-art" if banner else "ott-poster-art"
    image = ""
    if image_url:
        # Background layers leave the existing title card visible on load error.
        css_url = quote(image_url, safe=":/?=&%+#@,;")
        style = html.escape('background-image:url("' + css_url + '")', quote=True)
        image = '<span class="ott-art-image" aria-hidden="true" style="' + style + '"></span>'
    contents = ('<span class="' + shape + '" role="img" aria-label="'
                + _escape(title + (" 홍보 이미지" if banner else " 이미지")) + '">'
                '<span class="ott-art-fallback" aria-hidden="true">' + _escape(title)
                + '</span>' + image)
    if banner:
        contents += '<span class="ott-banner-title">' + _escape(title) + '</span>'
    contents += '</span>'
    if not banner:
        contents += '<span class="ott-work-title">' + _escape(title) + '</span>'
    safe = _url(item.get("url"))
    if safe:
        return ('<a class="ott-art-tile" href="' + _escape(safe)
                + '" target="_blank" rel="noopener noreferrer">' + contents + '</a>')
    return '<div class="ott-art-tile">' + contents + '</div>'


def _evidence_html(section, platform, *, promotion=False):
    position = _position(section.get("position"), 1)
    title = _text(section.get("title"))
    source = _source_url(platform)
    row_url = _url(section.get("source_url") or section.get("url"))
    date = _date_label(platform) or "확인 날짜 없음"
    if promotion:
        placement = _text(section.get("placement"), 150)
        location = f"{placement} {position}번째" if placement else f"공개 홍보작 {position}번째"
    elif section.get("_public_list"):
        placement = _text(section.get("placement"), 150) or "공개 홍보 화면의 목록"
        location = f"{placement} {position}번째"
    else:
        location = f"홈 {position}번째 콘텐츠 영역"
    partial = _text(platform.get("capture_scope"), 300) or ("공개 홈의 확인 범위" if platform.get("complete") else "공개 홈에서 확인한 일부 영역")
    environment = _text(platform.get("observed_environment"), 150)
    environment_html = ''
    if platform.get("country_verified") is False and environment:
        environment_label = "해외 서버 · 한국 지역 미확인" if environment == "github_actions_global" else "서버 확인 · 한국 지역 미확인"
        environment_html = '<div><dt>확인 환경</dt><dd>' + environment_label + '</dd></div>'
    row_link = _link(row_url, "목록 원문 열기 ↗", "ott-source-link") if row_url and row_url != source else ""
    home_link = _link(source, "홈 원문 열기 ↗", "ott-source-link")
    return ('<details class="ott-evidence"><summary>확인 근거</summary><dl>'
            '<div><dt>원래 이름</dt><dd>' + _escape(title) + '</dd></div>'
            '<div><dt>홈 위치</dt><dd>' + _escape(location) + '</dd></div>'
            '<div><dt>자료 시각</dt><dd>' + _escape(date) + '</dd></div>'
            '<div><dt>확보 범위</dt><dd>' + _escape(partial) + '</dd></div>'
            + environment_html + '</dl><div class="ott-source-links">' + row_link + home_link + '</div></details>')


def _section_html(section, platform, *, compact=False):
    position = _position(section.get("position"), 1)
    kind = KIND_LABELS.get(_text(section.get("kind")), KIND_LABELS["other"])
    works = [item for item in _items(section.get("works"), 100) if _text(item.get("title"))]
    visible = works[:3 if compact else 4]
    cards = ''.join(_artwork(work) for work in visible)
    description = _text(section.get("description"), 1200)
    description_html = '<p class="ott-original-description">' + _escape(description) + '</p>' if description else ''
    posters = '<div class="ott-work-grid">' + cards + '</div>' if cards else '<p class="ott-original-description">이 영역의 작품 목록은 아직 확인하지 못했습니다.</p>'
    more_works = ''
    if not compact and len(works) > len(visible):
        links = ''.join('<li>' + (_link(work.get("url"), _text(work.get("title"))) or _escape(work.get("title"))) + '</li>' for work in works)
        noun = '컬렉션 ' if section.get('items_kind') == 'collections' else '작품 '
        unit = '개' if section.get('items_kind') == 'collections' else '편'
        more_works = ('<details class="ott-work-list"><summary>확인한 ' + noun + str(len(works))
                      + unit + ' 보기</summary><ul>' + links + '</ul></details>')
    css = "ott-compact-section" if compact else "ott-home-section"
    placement = f"공개 홍보 목록 {position}번째" if section.get("_public_list") else f"홈 {position}번째 영역"
    return ('<article class="' + css + '"><div class="ott-section-meta"><span>'
            + _escape(placement) + '</span><span>' + _escape(kind) + '</span></div>'
            '<h4 class="ott-section-title">' + _escape(section.get("title")) + '</h4>'
            + description_html + posters + more_works + _evidence_html(section, platform) + '</article>')


def _promotion_html(promotion, platform, *, compact=False):
    label = _text(promotion.get("placement"), 150) or ("공개 홍보작" if _promotion_only(platform) else "홈에서 소개한 작품")
    if not compact:
        label += ' · ' + str(_position(promotion.get("position"), 1)) + '번째'
    return ('<article class="ott-promotion"><p class="ott-small-label">' + _escape(label) + '</p>'
            + _artwork(promotion, banner=True) + _evidence_html(promotion, platform, promotion=True)
            + '</article>')


def _status_html(platform, *, compact=False):
    label, message = platform_status(platform)
    date = _date_label(platform)
    css = "ott-status ott-status-stale" if _stale(platform) else "ott-status"
    # Keep the date visible even when a platform has no retrievable homepage.
    return ('<div class="' + css + '"><span class="ott-status-badge">' + _escape(label) + '</span>'
            + ('<span class="ott-date">' + _escape(date) + '</span>' if date else '')
            + ('<p class="ott-status-note">' + _escape(message) + '</p>' if not compact else '')
            + '</div>')


def _visible_lists(platform):
    public_lists = [dict(row, _public_list=True) for row in platform["public_lists"]]
    if _promotion_only(platform):
        return public_lists
    if platform.get("status") == "ok" or _stale(platform):
        sections = [dict(row, _public_list=False) for row in platform["sections"]]
        return sorted(sections + public_lists, key=lambda row: row["position"])
    return []


def _empty_html(platform, *, include_message=True):
    label, message = platform_status(platform)
    return ('<div class="ott-empty">' + ('<p>' + _escape(message) + '</p>' if include_message else '')
            + _link(_source_url(platform), '공식 홈에서 보기 ↗', 'ott-source-link') + '</div>')


def ott_comparison_html(snapshot):
    """Render only sourced rows; unavailable services never become zero counts."""
    columns = []
    for platform in snapshot_platforms(snapshot):
        sections = _visible_lists(platform)
        promotions = platform["promotions"] if platform.get("status") in {"ok", "promotion_only"} or _stale(platform) else []
        promotion = _promotion_html(promotions[0], platform, compact=True) if promotions else ''
        rows = ''.join(_section_html(section, platform, compact=True) for section in sections[:3])
        if not rows and not promotion:
            rows = _empty_html(platform)
        elif sections:
            scope = '공개 홍보 목록 ' if _promotion_only(platform) else '확인한 '
            rows += '<p class="ott-column-foot">' + scope + str(len(sections)) + '개 영역 · 서비스 선택에서 더 보기</p>'
        elif _promotion_only(platform):
            rows = '<p class="ott-public-scope">공개 페이지에서 확인한 홍보작입니다.</p>'
        columns.append('<section class="ott-compare-column ott-brand-' + platform["id"] + '">'
                       '<header class="ott-column-heading"><h3 class="ott-platform-name">'
                       + _escape(platform["name"]) + '</h3>'
                       + _link(_source_url(platform), '공식 홈 ↗', 'ott-source-link') + '</header>'
                       + _status_html(platform, compact=True) + promotion + rows + '</section>')
    return '<div class="ott-compare-grid">' + ''.join(columns) + '</div>'


def ott_platform_html(platform):
    """Render one service in source order, including a dated last-good snapshot."""
    platform = _known_platform(platform)
    sections = _visible_lists(platform)
    promotions = platform["promotions"] if platform.get("status") in {"ok", "promotion_only"} or _stale(platform) else []
    heading = ('<header class="ott-platform-heading ott-brand-' + platform["id"] + '">'
               '<h3 class="ott-platform-name">' + _escape(platform["name"]) + '</h3>'
               + _link(_source_url(platform), '공식 홈 열기 ↗', 'ott-source-link') + '</header>')
    content = _status_html(platform)
    if promotions:
        content += '<div class="ott-promotion-rail" aria-label="메인 홍보작">' + ''.join(_promotion_html(promotion, platform) for promotion in promotions[:5]) + '</div>'
    if sections:
        heading_text = '공개 홍보 화면의 목록' if _promotion_only(platform) else '공개 홈에 배치된 목록'
        content += ('<div class="ott-rows-heading"><h3>' + heading_text + '</h3><span>확인한 '
                    + str(len(sections)) + '개 영역 · 홈 순서대로</span></div>'
                    '<div class="ott-section-grid">' + ''.join(_section_html(section, platform) for section in sections[:6]) + '</div>')
        if len(sections) > 6:
            content += ('<details class="ott-more-sections"><summary>추가 ' + str(len(sections) - 6)
                        + '개 영역 보기</summary><div class="ott-section-grid">'
                        + ''.join(_section_html(section, platform) for section in sections[6:]) + '</div></details>')
    elif not promotions and not _promotion_only(platform):
        content += _empty_html(platform, include_message=False)
    if _promotion_only(platform):
        content += '<div class="ott-scope-note"><strong>공개 홍보 페이지에서 확인한 자료입니다.</strong><p>공개된 홍보작과 작품 목록을 보여드립니다. 콘텐츠 홈의 기획 테마와 구분해 집계합니다.</p></div>'
    return '<section class="ott-platform-detail">' + heading + content + '</section>'


def render_ott_page(snapshot, *, on_refresh: Callable | None = None):
    """Streamlit entry point. Refresh is explicit, never a render side effect.

    ``on_refresh`` may return the new snapshot. The caller owns collection,
    storage and any cache invalidation. Without a callback this view is read-only.
    """
    import streamlit as st

    styles = Path(__file__).with_name("ott_styles.css").read_text(encoding="utf-8")
    st.markdown('<style>' + styles + '</style>', unsafe_allow_html=True)
    with st.container(key="ott_now_section"):
        st.markdown('<header class="ott-page-heading"><h2>OTT는 지금</h2><p>공개 홈의 홍보작과 콘텐츠 목록을 원래 제목·배치 순서로 비교합니다.</p></header>', unsafe_allow_html=True)
        if on_refresh is not None and st.button("공개 홈 새로고침", key="refresh_ott_public_homes", help="공개 메인 홈을 확인합니다. 최근 확인 결과는 15분간 공유합니다."):
            try:
                with st.spinner("공개 홈을 확인하고 있습니다…"):
                    updated = on_refresh()
                if isinstance(updated, Mapping):
                    snapshot = updated
            except Exception:
                _LOGGER.exception("Public OTT homepage refresh failed")
                st.warning("이번에는 홈 자료를 갱신하지 못했습니다. 저장된 확인 자료를 보여드립니다.")
        labels = {"all": "5사 비교", **{service_id: name for service_id, name, _ in OTT_SERVICES}}
        with st.container(key="ott_platform_selector"):
            selected = st.segmented_control("OTT 선택", options=list(labels), format_func=labels.get,
                                            default="all", required=True, wrap=False,
                                            key="ott_selected_platform", label_visibility="collapsed")
        platforms = snapshot_platforms(snapshot)
        if selected and selected != "all":
            platform = next((item for item in platforms if item["id"] == selected), platforms[0])
            st.markdown(ott_platform_html(platform), unsafe_allow_html=True)
        else:
            st.markdown(ott_comparison_html(snapshot), unsafe_allow_html=True)
        with st.expander("공개 홈 확인 기준"):
            st.markdown("메인 배너와 공개 홈의 콘텐츠 목록을 확인합니다. 원래 제목과 작품 구성, 홈 위치, 확인 날짜를 함께 남기며 개인 시청 기록이나 맞춤 목록은 포함하지 않습니다.")
            st.markdown("기획 테마·신작·순위를 구분해 홈 순서대로 보여줍니다. 일부 영역만 확인한 경우에는 확인한 범위를 표시합니다. 공개 홍보 페이지의 작품 목록도 함께 보여주며 콘텐츠 홈의 기획 테마와 구분합니다. 접속 실패는 테마가 없는 것으로 집계하지 않습니다.")
