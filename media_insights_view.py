"""Source-grounded media business research UI; rendering never fetches or calls AI."""
from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import html
import io
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import streamlit as st

from media_insights_analysis import MediaInsightError
from media_insights_storage import MediaInsightsStorageError
from media_insights_editorial import select_major_insights
from media_insights_localization import valid_localization, MediaLocalizationError
from media_insights_topics import media_primary_topic, media_topic_ids
from public_media_monitoring import public_documents, public_url, compare_public_document, comparison_focus
from page_header import page_header_html, page_header_styles_html


_SELECTED = "media_insight_selected_id"
_READING = "media_insight_reading_id"
_NOTES = "media_insight_session_notes"
_COMPANIES = {"Netflix": "넷플릭스", "Disney": "디즈니", "Amazon": "아마존", "Comcast": "컴캐스트",
              "SKT": "SK텔레콤", "SK Broadband": "SK브로드밴드", "KT": "KT", "LG U+": "LG유플러스",
              "TVING": "티빙", "Wavve": "웨이브", "Coupang Play": "쿠팡플레이"}
_TOPIC_ORDER = ("광고·요금제", "스포츠·콘텐츠", "제휴·유통", "AI·기술", "실적·사업 전략", "시장·정책")
_TOPIC_ALIASES = {
    "advertising_pricing": "광고·요금제", "advertising": "광고·요금제", "pricing": "광고·요금제",
    "sports_content": "스포츠·콘텐츠", "sports": "스포츠·콘텐츠", "content": "스포츠·콘텐츠",
    "partnership_distribution": "제휴·유통", "partnership": "제휴·유통", "distribution": "제휴·유통",
    "ai_technology": "AI·기술", "ai": "AI·기술", "technology": "AI·기술",
    "earnings_strategy": "실적·사업 전략", "earnings": "실적·사업 전략", "strategy": "실적·사업 전략",
    "market_policy": "시장·정책", "market": "시장·정책", "policy": "시장·정책",
}
_COUNTRIES = {"KR": "한국", "US": "미국", "GB": "영국", "JP": "일본", "GLOBAL": "글로벌", "global": "글로벌"}
_PERIODS = {"전체 기간": None, "최근 30일": 30, "최근 90일": 90, "최근 1년": 365}
_UNPREPARED = object()


def _text(value, limit=3000):
    return str(value).strip()[:limit] if value is not None else ""


def _escape(value, limit=3000):
    return html.escape(_text(value, limit), quote=True)


def _preview(value, limit):
    text = _text(value, limit + 100)
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[:cut if cut > limit * 0.7 else limit].rstrip() + "…"


def _show_action_error(error):
    st.warning(str(error))
    if isinstance(error, MediaInsightError):
        code = getattr(error, "code", "validation_failed")
        if not re.fullmatch(r"[a-z_]{1,64}", code):
            code = "validation_failed"
        st.markdown('<span class="media-action-error" hidden aria-hidden="true" data-code="'
                    + _escape(code) + '" data-engine-version="' + _escape(error.engine_revision)
                    + '" data-diagnostics="' + _escape(json.dumps(error.diagnostics), 4000) + '"></span>',
                    unsafe_allow_html=True)


def _url(value):
    value = _text(value, 6000)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
        return value
    except ValueError:
        return ""


def _rows(value, limit=300):
    return [row for row in value[:limit] if isinstance(row, Mapping)] if isinstance(value, list) else []


def _date(value):
    try:
        result = datetime.fromisoformat(_text(value, 100).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _date_label(value):
    parsed = _date(value)
    return parsed.strftime("%Y.%m.%d") if parsed else "발표일 확인 필요"


def _locator_label(value):
    value = _text(value, 300)
    pdf = re.fullmatch(r"page(\d+)-p(\d+)", value, flags=re.IGNORECASE)
    if pdf:
        return f"PDF {int(pdf[1])}쪽 · {int(pdf[2])}문단"
    paragraph = re.fullmatch(r"p(\d+)", value, flags=re.IGNORECASE)
    return f"본문 {int(paragraph[1])}문단" if paragraph else value


def _topic(item):
    value = _text(media_primary_topic(item), 120)
    return _TOPIC_ALIASES.get(value, value or "사업 동향")


def _country(item):
    value = _text(item.get("country"), 80)
    return _COUNTRIES.get(value, value)


def insight_items(snapshot):
    """Only real, named source links become stories. Inputs remain unchanged."""
    rows = snapshot.get("items", []) if isinstance(snapshot, Mapping) else []
    return [dict(row, topic=media_primary_topic(row)) for row in rows if isinstance(row, Mapping) and _text(row.get("id"))
            and _text(row.get("title")) and public_url(row.get("url"))] if isinstance(rows, list) else []


def _is_domestic_item(item):
    return (_text(item.get("country")).upper() in {"KR", "한국"}
            or item.get("market_scope") in {"domestic_media", "domestic_product"})


def filter_insight_items(items, *, topic="전체", country="전체", lookback_days=None, now=None):
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    result = []
    for item in items:
        if topic != "전체" and topic not in {_TOPIC_ALIASES.get(value, value) for value in media_topic_ids(item)}:
            continue
        if country != "전체" and _country(item) != country:
            continue
        if lookback_days is not None:
            published = _date(item.get("published_at"))
            if published is None or not observed - timedelta(days=lookback_days) <= published <= observed:
                continue
        result.append(item)
    return result


def resolve_insight_archive(items, *, topic="전체", country="전체", domestic=False,
                            lookback_days=30, minimum=3, now=None):
    """Widen a sparse category's dated public archive, retaining every other filter.

    The requested period remains a preference, so returning to the whole feed
    uses it again. Future/undated documents and duplicate originals never count
    toward the minimum. This function never fetches, edits or creates articles.
    """
    observed = now or datetime.now(timezone.utc)
    observed = observed.replace(tzinfo=timezone.utc) if observed.tzinfo is None else observed.astimezone(timezone.utc)
    eligible = []
    seen_ids, seen_urls = set(), set()
    for item in items:
        published = _date(item.get("published_at"))
        url = public_url(item.get("url"))
        if not published or published > observed or not url or (domestic and not _is_domestic_item(item)):
            continue
        eligible.append(item)
    eligible.sort(key=lambda row: (_date(row.get("published_at")), _text(row.get("id"))), reverse=True)
    distinct = []
    for item in eligible:
        parsed = urlsplit(item["url"])
        query = [(key, value) for key, value in parse_qsl(parsed.query)
                 if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
        canonical = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urlencode(query), ""))
        identifier = _text(item.get("id"))
        if not identifier or identifier in seen_ids or canonical in seen_urls:
            continue
        distinct.append(item)
        seen_ids.add(identifier)
        seen_urls.add(canonical)
    matching = filter_insight_items(distinct, topic=topic, country=country)
    def in_period(days):
        return filter_insight_items(matching, lookback_days=days, now=observed)
    result = in_period(lookback_days)
    initial_count, effective_days = len(result), lookback_days
    targeted = domestic or topic != "전체"
    if targeted and lookback_days is not None and initial_count < minimum:
        for days in [day for day in (90, 365) if day > lookback_days] + [None]:
            result, effective_days = in_period(days), days
            if len(result) >= minimum:
                break
    return {"items": result, "lookback_days": effective_days, "initial_count": initial_count,
            "expanded": effective_days != lookback_days, "available_count": len(matching)}


def _evidence(rows, fallback_url="", fallback_title=""):
    result = {}
    for row in _rows(rows, 500):
        key, text = _text(row.get("id"), 180), _text(row.get("text"), 12000)
        url = _url(row.get("url")) or _url(fallback_url)
        if key and text and url:
            result[key] = {"id": key, "text": text, "url": url,
                           "locator": _text(row.get("locator"), 300),
                           "title": _text(row.get("title") or fallback_title, 500)}
    return result


def _all_evidence(items):
    result, ambiguous = {}, set()
    for item in items:
        for identifier, evidence in _evidence(item.get("evidence"), item.get("url"), item.get("title")).items():
            if identifier in result or identifier in ambiguous:
                ambiguous.add(identifier)
                result.pop(identifier, None)
            else:
                result[identifier] = evidence
    return result


def validated_analysis(item, analysis, context_items=None):
    """Every statement must cite saved evidence whose document version still matches."""
    if not isinstance(analysis, Mapping) or not _date(analysis.get("generated_at")) or not _text(analysis.get("model")):
        return None
    current_hash = _text(item.get("content_hash"))
    if not current_hash or _text(analysis.get("source_hash")) != current_hash:
        return None
    context = context_items or [item]
    hashes = {_text(row.get("id")): _text(row.get("content_hash")) for row in context}
    context_hashes = analysis.get("context_hashes", {})
    if not isinstance(context_hashes, Mapping):
        return None
    if any(key not in hashes or hashes[key] != value for key, value in context_hashes.items()):
        return None
    references = _all_evidence(context)
    owners = {identifier: row.get("id") for row in context
              for identifier in _evidence(row.get("evidence"), row.get("url"), row.get("title"))}
    def usable(ref):
        owner = owners.get(ref) if isinstance(ref, str) else None
        return ref in references and (owner == item.get("id") or context_hashes.get(owner) == hashes.get(owner)) if owner else False
    raw_facts, raw_comparisons = analysis.get("facts"), analysis.get("comparison", [])
    if (not isinstance(raw_facts, list) or not raw_facts or not isinstance(raw_comparisons, list)
            or any(not isinstance(row, Mapping) for row in [*raw_facts, *raw_comparisons])):
        return None
    facts = raw_facts[:30]
    if not _text(analysis.get("summary")):
        return None
    for fact in facts:
        refs = fact.get("evidence_ids")
        if (not _text(fact.get("text")) or not isinstance(refs, list) or not refs
                or any(not usable(ref) for ref in refs)):
            return None
    comparisons = []
    for row in raw_comparisons[:30]:
        refs = row.get("evidence_ids")
        if (all(_text(row.get(key)) for key in ("company", "aspect", "detail"))
                and isinstance(refs, list) and refs
                and all(usable(ref) for ref in refs)):
            comparisons.append(dict(row))
        else:
            return None
    used = {ref for row in [*facts, *comparisons] for ref in row.get("evidence_ids", [])}
    options, questions = analysis.get("btv_options"), analysis.get("questions")
    return {
        "headline": _text(analysis.get("headline"), 600),
        "summary": _text(analysis.get("summary")), "facts": [dict(fact) for fact in facts],
        "interpretation": _text(analysis.get("interpretation"), 10000),
        "btv_options": [_text(value) for value in options if isinstance(value, str) and _text(value)][:12] if isinstance(options, list) else [],
        "questions": [_text(value) for value in questions if isinstance(value, str) and _text(value)][:12] if isinstance(questions, list) else [],
        "comparison": comparisons, "evidence": [value for key, value in references.items() if key in used],
        "generated_at": _text(analysis.get("generated_at")),
    }


def _link(url, label):
    safe = _url(url)
    return ('<a href="' + _escape(safe, 6000) + '" target="_blank" rel="noopener noreferrer">'
            + _escape(label) + '</a>') if safe else ''


def _list_html(values):
    return '<ul>' + ''.join('<li>' + _escape(value) + '</li>' for value in values) + '</ul>'


def _section_html(number, title, body):
    return ('<section class="media-note-section"><div class="media-note-label"><span>' + number
            + '</span><h4>' + _escape(title) + '</h4></div>' + body + '</section>')


def _fingerprint(item):
    return (_text(item.get("content_hash")) or hashlib.sha256(
        json.dumps(item.get("evidence", []), ensure_ascii=False, sort_keys=True).encode()).hexdigest())


def _item_analysis(item, context_items):
    cached = st.session_state.get(_NOTES, {}).get(item["id"], {})
    raw = (cached.get("analysis") if cached.get("hash") == _fingerprint(item) else None) or item.get("analysis")
    return validated_analysis(item, raw, context_items)


def _select_item(item_id):
    st.session_state[_SELECTED] = item_id
    _close_reading()


def _open_reading(item_id):
    st.session_state[_SELECTED] = item_id
    st.session_state[_READING] = item_id


def _close_reading():
    st.session_state.pop(_READING, None)


def _toggle_public_comparison():
    st.session_state["media_insight_comparison_open"] = not st.session_state.get("media_insight_comparison_open", False)


def _csv_cell(value):
    text = _text(value, 12000)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def comparison_csv(analysis):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["사업자", "비교 관점", "확인한 내용", "근거 ID"])
    for row in analysis.get("comparison", []):
        writer.writerow([_csv_cell(row.get("company")), _csv_cell(row.get("aspect")),
                         _csv_cell(row.get("detail")), ", ".join(row.get("evidence_ids", []))])
    return stream.getvalue().encode("utf-8-sig")


def analysis_markdown(item, analysis):
    lines = ["# " + _text(analysis.get("headline") or item.get("title")), "", "원문 제목: " + _text(item.get("title")),
             "원문: " + _url(item.get("url")),
             "발표일: " + _date_label(item.get("published_at")), "", analysis["summary"], "", "## 확인한 사실", ""]
    for fact in analysis["facts"]:
        lines.append("- " + _text(fact.get("text")) + " [" + ", ".join(fact.get("evidence_ids", [])) + "]")
    for title, key in [("무엇을 의미하나", "interpretation"), ("B tv 검토 선택지", "btv_options"), ("추가로 확인할 질문", "questions")]:
        if analysis.get(key):
            lines.extend(["", "## " + title, ""])
            lines.extend(["- " + value for value in analysis[key]] if isinstance(analysis[key], list) else [analysis[key]])
    lines.extend(["", "## 원문 근거", ""])
    used = {ref for fact in analysis["facts"] for ref in fact.get("evidence_ids", [])}
    used.update(ref for row in analysis["comparison"] for ref in row.get("evidence_ids", []))
    for evidence in analysis["evidence"]:
        if evidence["id"] in used:
            lines.extend(["### " + evidence["id"] + " · " + _locator_label(evidence["locator"]), evidence["text"], evidence["url"], ""])
    return "\n".join(lines).encode("utf-8")


def _company(item):
    value = _text(item.get("company"), 120)
    return _COMPANIES.get(value, value)


def _source_label(item):
    kind = "공식 실적 자료" if item.get("source_type") == "earnings" else "공식 발표"
    return " · ".join(value for value in (_company(item), kind) if value)


def _korean(value):
    return bool(re.search(r"[가-힣]", _text(value)))


def display_copy(item, analysis=None, *, localization=_UNPREPARED):
    """Show Korean saved copy; original English stays inside the source drawer."""
    localized = valid_localization(item) if localization is _UNPREPARED else localization
    localized = localized if isinstance(localized, Mapping) else {}
    headline = _text(localized.get("title"), 600)
    summary = _text(localized.get("summary"), 1800)
    if not _korean(headline) and analysis and _korean(analysis.get("headline")):
        headline = _text(analysis.get("headline"), 600)
    if not _korean(summary) and analysis and _korean(analysis.get("summary")):
        summary = _text(analysis.get("summary"), 1800)
    if not _korean(headline):
        headline = _text(item.get("title"), 600) if _korean(item.get("title")) else f"{_company(item)}의 {_topic(item)} 발표"
    if not _korean(summary):
        summary = _text(item.get("excerpt"), 1800) if _korean(item.get("excerpt")) else "공식 발표의 한국어 요약을 준비하고 있습니다. 원문과 확인한 근거는 아래에서 볼 수 있습니다."
    return {"title": headline, "summary": summary}


class _RenderPresentation:
    """Validate source versions once during this render, never across sessions."""

    def __init__(self, items):
        self.items = items
        self.localizations = {item["id"]: valid_localization(item) for item in items}
        self._analyses = {}

    def pending(self):
        return [item for item in self.items if self.localizations[item["id"]] is None]

    def analysis(self, item):
        key = item["id"]
        if key not in self._analyses:
            self._analyses[key] = _item_analysis(item, self.items)
        return self._analyses[key]

    def copy(self, item, analysis=None):
        return display_copy(item, analysis, localization=self.localizations[item["id"]])


def _image_url(value):
    value = _url(value)
    if not value or urlsplit(value).scheme != "https" or urlsplit(value).port not in {None, 443}:
        return ""
    host = (urlsplit(value).hostname or "").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    return value


def artwork_html(item, *, lead=False):
    """Only official collected artwork is photographed; missing art uses typography."""
    image_url = _image_url(item.get("image_url"))
    classes = "media-story-art media-story-art-lead" if lead else "media-story-art"
    brand = _company(item) or "미디어 인사이트"
    fallback = ('<div class="media-art-fallback"><span>' + _escape(_topic(item))
                + '</span><strong>' + _escape(brand) + '</strong><small>공식 발표</small></div>')
    # Background images fail transparently, so typography remains visible even
    # when a CDN is unavailable. Streamlit strips inline JavaScript handlers.
    image_class = "media-story-image" + (" media-story-image-logo" if item.get("image_kind") == "company_logo" else "")
    visual_class = "media-story-visual" + (" media-story-visual-logo" if item.get("image_kind") == "company_logo" else "")
    background_url = quote(image_url, safe=":/?#[]@!$&*+,;=%~-_.")
    image = ('<div class="' + visual_class + '" role="img" aria-label="'
             + _escape(brand + " 공식 발표 관련 이미지") + '" style="background-image:url(&quot;'
             + _escape(background_url, 8000) + '&quot;)"></div>'
             + '<img class="' + image_class + '" src="' + _escape(image_url, 6000)
             + '" alt="" aria-hidden="true" loading="lazy" decoding="async" referrerpolicy="no-referrer">') if image_url else ''
    attribution = _text(item.get("image_attribution") or item.get("image_source_name") or _source_label(item), 200)
    return '<div class="' + classes + '">' + fallback + image + ('<span class="media-image-credit">' + _escape(attribution) + '</span>' if image else '') + '</div>'


def _editorial_date(item):
    value = item.get("editorial_date") or item.get("published_at")
    prefix = "원문 수정 " if item.get("editorial_date_basis") == "source_updated" else ""
    return prefix + _date_label(value)


def _render_story(item, *, index, selected, analysis=None, archive=False, copy=None):
    key = hashlib.sha256(_text(item["id"]).encode()).hexdigest()[:16]
    lead = index == 0 and not archive
    copy = display_copy(item, analysis) if copy is None else copy
    container_key = ("media_archive_story_" if archive else "media_story_") + key
    with st.container(key=container_key):
        classes = "media-story-meta" + (" media-story-selected" if selected else "")
        scope = [_topic(item), _company(item), _country(item)]
        st.markdown('<div class="' + classes + '" data-item-id="' + _escape(item["id"])
                    + '" data-topics="' + _escape(' '.join(media_topic_ids(item)))
                    + '" data-published-at="' + _escape(item.get("published_at")) + '"><span class="media-story-index">' + f'{index + 1:02d}'
                    + '</span><span>' + _escape(' · '.join(value for value in scope if value)) + '</span>'
                    + '<time>' + _escape(_editorial_date(item)) + '</time></div>', unsafe_allow_html=True)
        if not archive:
            st.markdown(artwork_html(item, lead=lead), unsafe_allow_html=True)
        st.button(copy["title"], key=("media_archive_select_" if archive else "media_select_") + key,
                  type="tertiary", width="stretch", on_click=_select_item, args=(item["id"],))
        if not archive:
            st.markdown('<p class="media-story-excerpt">' + _escape(_preview(copy["summary"], 260 if lead else 140)) + '</p>', unsafe_allow_html=True)
            reason = _text(item.get("editorial_reason"), 300)
            if reason:
                st.markdown('<p class="media-editorial-reason"><span>주목한 이유</span>' + _escape(reason) + '</p>', unsafe_allow_html=True)
        st.markdown('<div class="media-story-source">' + _escape(_source_label(item))
                    + (' · ' + _escape(item.get("period")) if _text(item.get("period")) else '') + '</div>', unsafe_allow_html=True)


def _render_comparison(item, analysis, *, key_prefix="media"):
    if not analysis or not analysis["comparison"]:
        return
    st.markdown('<div class="media-comparison-heading"><h3>같은 기준으로 보는 비교</h3></div>', unsafe_allow_html=True)
    rows = ''.join('<tr><td>' + _escape(row["company"]) + '</td><td>' + _escape(row["aspect"])
                   + '</td><td>' + _escape(row["detail"]) + '</td></tr>' for row in analysis["comparison"])
    st.markdown('<div class="media-comparison-scroll"><table class="media-comparison-table"><thead><tr>'
                '<th>사업자</th><th>비교 관점</th><th>확인한 내용</th></tr></thead><tbody>' + rows + '</tbody></table></div>',
                unsafe_allow_html=True)
    st.download_button("비교표 내보내기", comparison_csv(analysis), "media-comparison.csv", "text/csv",
                       key=key_prefix + "_comparison_export", type="tertiary")


def _render_detail(item, analysis, *, on_analyze, context_items, copy=None, key_prefix="media"):
    copy = display_copy(item, analysis) if copy is None else copy
    headline = copy["title"]
    anchor = "media-insight-detail" if key_prefix == "media" else "media-insight-reading-detail"
    st.markdown('<header class="media-detail-heading" id="' + anchor + '" data-item-id="' + _escape(item["id"])
                + '"><span>분석 노트</span><h3>' + _escape(headline)
                + '</h3><p>' + _escape(' · '.join(value for value in [_company(item), _country(item),
                                                                   _date_label(item.get("published_at"))] if value)) + '</p></header>',
                unsafe_allow_html=True)
    st.link_button("공식 원문 열기 ↗", _url(item["url"]), width="stretch")
    can_analyze = bool(on_analyze and _evidence(item.get("evidence"), item.get("url"), item.get("title")))
    label = "분석 노트 다시 만들기" if analysis else "분석 노트 만들기"
    if st.button(label, key=key_prefix + "_insights_analyze", disabled=not can_analyze, type="secondary", width="stretch"):
        try:
            with st.spinner("원문 근거를 확인하며 분석 노트를 만들고 있어요…"):
                result = on_analyze(item["id"])
            raw = result.get("analysis") if isinstance(result, Mapping) and "analysis" in result else result
            updated = validated_analysis(item, raw, context_items)
            if updated:
                st.session_state.setdefault(_NOTES, {})[item["id"]] = {"hash": _fingerprint(item), "analysis": raw}
                st.rerun()
            else:
                st.warning("분석 근거를 확인하지 못했어요. 원문은 계속 확인할 수 있습니다.")
        except (MediaInsightError, MediaInsightsStorageError) as error:
            _show_action_error(error)
        except Exception:
            st.warning("분석 노트를 만들지 못했어요. 잠시 후 다시 시도해 주세요.")
    if analysis:
        st.markdown('<p class="media-detail-summary">' + _escape(copy["summary"]) + '</p>', unsafe_allow_html=True)
        facts = _list_html(fact["text"] for fact in analysis["facts"])
        st.markdown(_section_html("01", "확인한 사실", facts), unsafe_allow_html=True)
        if analysis["interpretation"]:
            st.markdown(_section_html("02", "무엇을 의미하나", '<p>' + _escape(analysis["interpretation"]) + '</p>'), unsafe_allow_html=True)
        if analysis["btv_options"]:
            st.markdown(_section_html("03", "B tv에서 검토할 선택지", _list_html(analysis["btv_options"])), unsafe_allow_html=True)
        if analysis["questions"]:
            st.markdown(_section_html("?", "추가로 확인할 질문", _list_html(analysis["questions"])), unsafe_allow_html=True)
        st.download_button("검토 메모 내려받기", analysis_markdown(item, analysis), "media-insight-note.md", "text/markdown",
                           key=key_prefix + "_note_export", width="stretch")
    else:
        st.markdown('<div class="media-source-first"><h4>원문에서 확인한 내용</h4><p>'
                    + _escape(copy["summary"])
                    + '</p></div>', unsafe_allow_html=True)
        st.caption("분석 노트를 만들면 원문을 바탕으로 해석과 검토할 선택지를 함께 볼 수 있어요.")
    evidence = analysis["evidence"] if analysis else list(_evidence(item.get("evidence"), item.get("url"), item.get("title")).values())
    with st.expander("원문·근거", expanded=False):
        st.caption("원문 제목 · " + _text(item["title"], 600))
        st.markdown("원문 내용은 번역 전 문장을 그대로 보관합니다.")
        if _text(item.get("excerpt")):
            st.markdown('<p class="media-original-excerpt">' + _escape(item.get("excerpt"), 5000) + '</p>', unsafe_allow_html=True)
        for row in evidence[:20]:
            st.markdown('<div class="media-evidence-row"><div>' + _escape(_locator_label(row["locator"]) or "원문 인용") + '</div>'
                        + '<blockquote>' + _escape(row["text"], 5000) + '</blockquote>'
                        + _link(row["url"], _source_label(item) + ' ↗') + '</div>', unsafe_allow_html=True)
        if not evidence:
            st.caption("인용할 본문을 확인하면 이곳에 근거가 표시됩니다.")
    return analysis


@st.dialog("자료 읽기", width="large", on_dismiss=_close_reading)
def _render_reading_dialog(item, analysis, *, on_analyze, context_items, copy):
    """The dialog keeps the background list and its scroll position in place."""
    with st.container(key="media_insight_reading"):
        if st.button("← 목록으로 돌아가기", key="media_reading_close", type="tertiary"):
            _close_reading()
            st.rerun()
        _render_detail(item, analysis, on_analyze=on_analyze, context_items=context_items,
                       copy=copy, key_prefix="media_reading")
        _render_comparison(item, analysis, key_prefix="media_reading")


def validated_answer(answer, items, selected_id=None):
    if not isinstance(answer, Mapping):
        return None
    documents = {_text(item.get("id")): item for item in items}
    hashes = {key: _text(item.get("content_hash")) for key, item in documents.items()}
    selected_id = selected_id or answer.get("selected_item_id")
    raw_hashes = answer.get("context_hashes")
    if raw_hashes is not None:
        if (not isinstance(raw_hashes, Mapping)
                or any(not isinstance(key, str) or not isinstance(value, str) or not value
                       or hashes.get(key) != value for key, value in raw_hashes.items())):
            return None
        context_hashes = dict(raw_hashes)
    else:
        context_hashes = {}
    source_hash = _text(answer.get("source_hash"))
    if source_hash and selected_id and hashes.get(selected_id) != source_hash:
        return None
    if selected_id:
        if not hashes.get(selected_id):
            return None
        context_hashes[selected_id] = hashes[selected_id]
    metadata = {"context_hashes": context_hashes}
    revision = _text(answer.get("source_engine_revision"), 40)
    if re.fullmatch(r"\d+\.\d+\.\d+", revision):
        metadata["source_engine_revision"] = revision
    if source_hash:
        metadata["source_hash"] = source_hash
    if selected_id:
        metadata["selected_item_id"] = selected_id
    if answer.get("status") == "not_found":
        return {"status": "not_found", "answer": "현재 확보된 공식 자료에서 질문에 답할 근거를 확인하지 못했습니다.",
                "evidence_ids": [], "sources": [], **metadata}
    references = _all_evidence(items)
    ids = answer.get("evidence_ids")
    if (not _text(answer.get("answer")) or not isinstance(ids, list) or not ids
            or any(not isinstance(ref, str) or ref not in references for ref in ids)):
        return None
    owners = {identifier: item["id"] for item in items
              for identifier in _evidence(item.get("evidence"), item.get("url"), item.get("title"))}
    for ref in ids:
        owner = owners[ref]
        if not hashes.get(owner) or (raw_hashes is not None and raw_hashes.get(owner) != hashes[owner]):
            return None
        context_hashes[owner] = hashes[owner]
    sources, urls = [], set()
    for ref in ids:
        evidence = references[ref]
        if evidence["url"] not in urls:
            sources.append({"url": evidence["url"], "title": evidence["title"], "locator": evidence["locator"]})
            urls.add(evidence["url"])
    return {"status": "supported", "answer": _text(answer["answer"], 10000),
            "evidence_ids": list(ids), "sources": sources, **metadata}


def _render_public_monitoring(snapshot):
    documents = public_documents(snapshot)
    if not documents:
        return
    with st.container(key="media_public_comparison", border=True):
        st.markdown('<section class="public-media-monitoring" data-public-only="true"><h3>공개 자료 비교</h3>'
                    '<p>누적된 공식 발표의 흐름과 같은 원문의 이전 확인본을 비교합니다.</p></section>', unsafe_allow_html=True)
        # Start with the company that has the newest dated announcement.
        companies = list(dict.fromkeys(item["company"] for item in documents))
        company_key, document_key = "public_media_company", "public_media_document"
        if st.session_state.get(company_key) not in companies:
            st.session_state[company_key] = companies[0]
        company = st.selectbox("비교할 사업자", companies, key=company_key,
                               format_func=lambda name: _COMPANIES.get(name, name))
        flow = public_documents(snapshot, company)
        identifiers = [item["id"] for item in flow]
        copies = {item["id"]: display_copy(item) for item in flow}
        if st.session_state.get(document_key) not in identifiers:
            st.session_state[document_key] = identifiers[0]
        selected_id = st.selectbox("비교할 공식 발표", identifiers, key=document_key,
                                   format_func=lambda identifier: copies[identifier]["title"])
        selected = next(item for item in flow if item["id"] == selected_id)
        st.caption(comparison_focus(selected.get("topic")))
        comparison = compare_public_document(selected)
        st.markdown("**" + comparison["label"] + "**")
        if comparison["status"] == "no_previous":
            st.caption("같은 원문의 이전 본문이 보관되면 전후 문장을 함께 보여드립니다.")
        else:
            st.caption("이전 확인 " + _date_label(comparison["before_at"]) + " · 최근 확인 "
                       + _date_label(comparison["after_at"]))
            if comparison["status"] == "changed":
                with st.expander("달라진 원문 문장", expanded=False):
                    earlier, current = st.columns(2, gap="medium")
                    for column, label, field in ((earlier, "이전 확인본", "removed"), (current, "최근 확인본", "added")):
                        with column:
                            st.markdown("**" + label + "**")
                            for text in comparison[field]:
                                st.text(text[:1200])
                            if not comparison[field]:
                                st.caption("달라진 문장이 없습니다.")
        st.link_button("비교한 공식 원문 열기 ↗", comparison["source_url"], width="stretch")
        st.markdown("**관련 발표 흐름**")
        for item in flow[:6]:
            st.markdown(_link(item["url"], copies[item["id"]]["title"] + " ↗"), unsafe_allow_html=True)
            st.caption("발표 " + _date_label(item.get("published_at")) + " · 확인 " + _date_label(item.get("collected_at")))


def _render_collection_sources(snapshot):
    sources = _rows(snapshot.get("sources"), 100)
    if not sources:
        return
    with st.expander(f"자료 출처 · 확인 날짜 ({len(sources)}곳)", expanded=False):
        for source in sources:
            status = _text(source.get("status"))
            when = source.get("last_success_at")
            label = "확인 완료" if status in {"ok", "success"} else ("이전 자료 보관" if when else "자료를 확인하지 못함")
            if status == "partial":
                label = "일부 자료 확인"
            elif source.get("error_code") == "no_business_documents":
                label = "선정할 사업 자료 없음"
            date = _date_label(when) if when else "아직 확인한 자료가 없습니다"
            name = _text(source.get("name") or source.get("id"))
            if not _korean(name):
                name = _company(source) or next((ko + " 공식 자료" for en, ko in _COMPANIES.items() if en.lower() in name.lower()), "공식 자료")
            st.markdown('<div class="media-source-status"><strong>' + _escape(name)
                        + '</strong><span>' + _escape(label) + '</span><span>' + _escape(date) + '</span></div>', unsafe_allow_html=True)


def render_media_insights_page(snapshot, *, on_refresh=None, on_analyze=None, on_localize=None):
    """Render real saved items; only explicit buttons call the supplied callbacks."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    st.html('<style>' + Path(__file__).with_name("media_insights_styles.css").read_text(encoding="utf-8")
            + '</style>' + page_header_styles_html())
    with st.container(key="media_insights_section"):
        with st.container(key="media_insights_header"):
            title, question_action, refresh_action = st.columns([4, 1.5, 1], gap="small", vertical_alignment="center")
            with title:
                st.markdown(page_header_html(
                    'media', '미디어 인사이트',
                    '사업의 변화에서, 다음 선택의 근거까지.',
                    eyebrow='THE MEDIA BRIEF',
                ), unsafe_allow_html=True)
            with question_action:
                st.button("공개 자료 비교", key="media_insights_compare_toggle", on_click=_toggle_public_comparison,
                          disabled=not bool(public_documents(snapshot)), width="stretch")
            with refresh_action:
                if st.button("자료 새로고침", key="media_insights_refresh", disabled=on_refresh is None, width="stretch"):
                    try:
                        with st.spinner("공식 자료의 새 발표를 확인하고 있어요…"):
                            refreshed = on_refresh()
                        if isinstance(refreshed, Mapping) and "items" in refreshed:
                            snapshot = refreshed
                    except (MediaInsightError, MediaInsightsStorageError) as error:
                        _show_action_error(error)
                    except Exception:
                        st.warning("새 자료를 확인하지 못했어요. 마지막으로 저장된 자료를 보여드립니다.")
        if st.session_state.get("media_insight_comparison_open"):
            _render_public_monitoring(snapshot)
        items = insight_items(snapshot)
        if not items:
            st.info("공식 발표와 실적 자료를 확인하면 주요 변화와 원문이 이곳에 표시됩니다.")
            _render_collection_sources(snapshot)
            return
        domestic = st.session_state.get("media_insight_topic", "전체") == "국내 이슈"
        scope_items = [item for item in items if not domestic or _is_domestic_item(item)]
        presentation = _RenderPresentation(items)
        pending_translations = presentation.pending()
        pending_marker = st.empty()
        if pending_translations:
            pending_marker.markdown(''.join('<span class="media-pending-translation" hidden aria-hidden="true" data-item-id="'
                                + _escape(item["id"], 500) + '"></span>' for item in pending_translations), unsafe_allow_html=True)
        with st.container(key="media_insights_edition_caption"):
            if pending_translations:
                explanation, translate = st.columns([4, 1], gap="small", vertical_alignment="center")
            else:
                explanation, translate = st.container(), None
            with explanation:
                st.caption("국내 콘텐츠·상품·미디어의 주요 소식을 공식 자료에서 추립니다. 상품·혜택 조건은 발표 당시 기준입니다." if domestic else
                           "최근 공식 발표 중 상품·유통·AI·사업 실적의 주요 변화를 추립니다.")
            if pending_translations:
                with translate:
                    if st.button("한글 정리", key="media_insights_localize", disabled=on_localize is None,
                                 width="stretch", help="새 영문 자료의 제목과 요약을 한 번 정리해 저장합니다."):
                        try:
                            with st.spinner("새 자료의 제목과 요약을 한국어로 정리하고 있어요…"):
                                refreshed = on_localize()
                            if isinstance(refreshed, Mapping) and "items" in refreshed:
                                snapshot = refreshed
                                items = insight_items(snapshot)
                                scope_items = [item for item in items if not domestic or _is_domestic_item(item)]
                                presentation = _RenderPresentation(items)
                                pending_translations = presentation.pending()
                                if pending_translations:
                                    pending_marker.markdown(''.join('<span class="media-pending-translation" hidden aria-hidden="true" data-item-id="'
                                        + _escape(item["id"], 500) + '"></span>' for item in pending_translations), unsafe_allow_html=True)
                                else:
                                    pending_marker.empty()
                        except (MediaLocalizationError, MediaInsightsStorageError) as error:
                            st.warning(str(error))
                        except Exception:
                            st.warning("한글 정리를 완료하지 못했습니다. 저장된 자료와 원문은 유지됩니다.")
        topic_values = {_TOPIC_ALIASES.get(topic, topic) for item in items for topic in media_topic_ids(item)}
        topics = ["전체", "국내 이슈", *[topic for topic in _TOPIC_ORDER if topic in topic_values],
                  *sorted(topic_values - set(_TOPIC_ORDER))]
        countries = ["전체", *sorted({_country(item) for item in items if _country(item)})]
        with st.container(key="media_insights_filters"):
            topic_col, country_col, period_col = st.columns([2.8, 1, 1], gap="small")
            with topic_col:
                if st.session_state.get("media_insight_topic", "전체") not in topics:
                    st.session_state["media_insight_topic"] = "전체"
                topic = st.radio("주제", topics, horizontal=True, label_visibility="collapsed", key="media_insight_topic")
            with country_col:
                if st.session_state.get("media_insight_country", "전체") not in countries:
                    st.session_state["media_insight_country"] = "전체"
                country = st.selectbox("지역", countries, key="media_insight_country", label_visibility="collapsed",
                                       format_func=lambda value: "전체 지역" if value == "전체" else value)
            with period_col:
                period = st.selectbox("발표 기간", list(_PERIODS), index=list(_PERIODS).index("최근 30일"),
                                      key="media_insight_period", label_visibility="collapsed")
        domestic = topic == "국내 이슈"
        scope_items = [item for item in items if not domestic or _is_domestic_item(item)]
        archive = resolve_insight_archive(scope_items, topic="전체" if domestic else topic,
                                          country=country, domestic=domestic, lookback_days=_PERIODS[period])
        filtered = archive["items"]
        major = select_major_insights(filtered, scope="domestic" if domestic else "all", limit=6)
        if topic != "전체":
            # Historical source material remains useful in a chosen category,
            # even when it is too old for the current editorial edition.
            selected_ids = {item["id"] for item in major}
            for item in filtered:
                if len(major) >= min(3, len(filtered)):
                    break
                if item["id"] not in selected_ids:
                    major.append({**item, "editorial_reason": "주제와 일치하는 이전 공식 자료 · 발표 당시 기준",
                                  "editorial_date": item["published_at"], "editorial_date_basis": "published"})
                    selected_ids.add(item["id"])
        if archive["expanded"]:
            effective_period = next(label for label, days in _PERIODS.items() if days == archive["lookback_days"])
            explanation = (f'{period}의 자료가 {archive["initial_count"]}건이라 {effective_period}으로 넓혀 '
                           f'{len(filtered)}건을 보여드립니다. 발표일은 원문 기준입니다.')
            if len(filtered) < 3:
                explanation += f' 이 조건으로 확인한 전체 보관 자료는 {archive["available_count"]}건입니다.'
            st.markdown('<p class="media-archive-expansion" role="status" data-effective-days="'
                        + (str(archive["lookback_days"]) if archive["lookback_days"] is not None else "all")
                        + '">' + _escape(explanation) + '</p>', unsafe_allow_html=True)
        if not filtered:
            _close_reading()
            st.info("선택한 조건에 맞는 국내 소식이 아직 없습니다. 공식 출처에서 확인한 콘텐츠·상품·미디어 소식을 이곳에 추가합니다." if domestic else
                    "선택한 조건에 맞는 발표가 없습니다. 기간이나 주제를 바꾸면 저장된 자료를 확인할 수 있어요.")
            _render_collection_sources(snapshot)
            return
        known_ids = {item["id"] for item in filtered}
        if st.session_state.get(_SELECTED) not in known_ids:
            st.session_state[_SELECTED] = (major or filtered)[0]["id"]
        if st.session_state.get(_READING) not in {None, st.session_state[_SELECTED]}:
            _close_reading()
        selected = next(item for item in [*major, *filtered] if item["id"] == st.session_state[_SELECTED])
        analysis = presentation.analysis(selected)
        with st.container(key="media_insights_workspace"):
            stories, detail = st.columns([1.7, 1], gap="large")
            with stories:
                st.markdown('<div class="media-section-label">' + ("국내에서 주목할 소식" if domestic else "이번에 주목할 변화")
                            + '<span>' + str(len(major)) + '건</span></div>', unsafe_allow_html=True)
                if major:
                    _render_story(major[0], index=0, selected=major[0]["id"] == selected["id"],
                                  copy=presentation.copy(major[0], presentation.analysis(major[0])))
                    for offset in range(1, len(major), 2):
                        with st.container(key="media_edition_row_" + str(offset)):
                            columns = st.columns(2, gap="medium")
                            for index, item in enumerate(major[offset:offset + 2], start=offset):
                                with columns[index - offset]:
                                    _render_story(item, index=index, selected=item["id"] == selected["id"],
                                                  copy=presentation.copy(item, presentation.analysis(item)))
                else:
                    st.info("이 조건에서 주요 이슈로 추릴 새 발표가 없습니다. 수집 자료는 보관함에서 확인할 수 있습니다.")
                with st.expander(f"수집 자료 보관함 · {len(filtered)}건", key="media_insights_archive"):
                    st.caption("보관함에는 주요 이슈에 선정되지 않은 과거 자료와 실적 첨부도 포함됩니다.")
                    for index, item in enumerate(filtered):
                        _render_story(item, index=index, selected=item["id"] == selected["id"],
                                      copy=presentation.copy(item, presentation.analysis(item)), archive=True)
            with detail:
                with st.container(key="media_insights_detail"):
                    st.button("분석 노트 크게 읽기 ↗", key="media_insights_read_selected", type="tertiary",
                              on_click=_open_reading, args=(selected["id"],), width="stretch")
                    _render_detail(selected, analysis, on_analyze=on_analyze, context_items=items,
                                   copy=presentation.copy(selected, analysis))
                    _render_comparison(selected, analysis)
        _render_collection_sources(snapshot)
        if st.session_state.get(_READING) == selected["id"]:
            _render_reading_dialog(selected, analysis, on_analyze=on_analyze, context_items=items,
                                   copy=presentation.copy(selected, analysis))
