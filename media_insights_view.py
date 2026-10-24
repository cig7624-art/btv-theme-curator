"""Source-grounded media business research UI; rendering never fetches or calls AI."""
from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import streamlit as st

from media_insights_analysis import MediaInsightError
from media_insights_storage import MediaInsightsStorageError


_SELECTED = "media_insight_selected_id"
_NOTES = "media_insight_session_notes"
_QUESTIONS = "media_insight_questions"
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
    value = _text(item.get("topic"), 120)
    return _TOPIC_ALIASES.get(value, value or "사업 동향")


def _country(item):
    value = _text(item.get("country"), 80)
    return _COUNTRIES.get(value, value)


def insight_items(snapshot):
    """Only real, named source links become stories. Inputs remain unchanged."""
    rows = _rows(snapshot.get("items")) if isinstance(snapshot, Mapping) else []
    return [dict(row) for row in rows if _text(row.get("id")) and _text(row.get("title")) and _url(row.get("url"))]


def filter_insight_items(items, *, topic="전체", country="전체", lookback_days=None, now=None):
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    result = []
    for item in items:
        if topic != "전체" and _topic(item) != topic:
            continue
        if country != "전체" and _country(item) != country:
            continue
        if lookback_days is not None:
            published = _date(item.get("published_at"))
            if published is None or not observed - timedelta(days=lookback_days) <= published <= observed:
                continue
        result.append(item)
    return result


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


def _toggle_questions():
    st.session_state["media_insight_research_open"] = not st.session_state.get("media_insight_research_open", False)


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


def _render_story(item, *, index, selected, analysis=None):
    key = hashlib.sha256(_text(item["id"]).encode()).hexdigest()[:16]
    lead = index == 0
    with st.container(key="media_story_" + key):
        classes = "media-story-meta" + (" media-story-selected" if selected else "")
        scope = [_topic(item), _text(item.get("company")), _country(item)]
        st.markdown('<div class="' + classes + '"><span class="media-story-index">' + f'{index + 1:02d}'
                    + '</span><span>' + _escape(' · '.join(value for value in scope if value)) + '</span>'
                    + '<time>' + _escape(_date_label(item.get("published_at"))) + '</time></div>', unsafe_allow_html=True)
        headline = analysis.get("headline") if analysis else ""
        st.button(headline or _text(item["title"], 600), key="media_select_" + key, type="tertiary", width="stretch",
                  help=_text(item["title"], 600) if headline and headline != item["title"] else None,
                  on_click=_select_item, args=(item["id"],))
        excerpt = _preview(analysis.get("summary") if analysis else item.get("excerpt"), 350 if lead else 180)
        if excerpt:
            st.markdown('<p class="media-story-excerpt">' + _escape(excerpt) + '</p>', unsafe_allow_html=True)
        st.markdown('<div class="media-story-source">' + _escape(item.get("source_name") or item.get("source_type") or "공식 자료")
                    + (' · ' + _escape(item.get("period")) if _text(item.get("period")) else '')
                    + (' · 원문 수정 ' + _date_label(item["source_updated_at"]) if item.get("source_updated_at") else '')
                    + '</div>', unsafe_allow_html=True)
        if selected:
            st.markdown('<a class="media-mobile-detail-link" href="#media-insight-detail">이 자료의 분석 노트 보기 ↓</a>',
                        unsafe_allow_html=True)


def _render_comparison(item, analysis):
    if not analysis or not analysis["comparison"]:
        return
    st.markdown('<div class="media-comparison-heading"><h3>같은 기준으로 보는 비교</h3></div>', unsafe_allow_html=True)
    rows = ''.join('<tr><td>' + _escape(row["company"]) + '</td><td>' + _escape(row["aspect"])
                   + '</td><td>' + _escape(row["detail"]) + '</td></tr>' for row in analysis["comparison"])
    st.markdown('<div class="media-comparison-scroll"><table class="media-comparison-table"><thead><tr>'
                '<th>사업자</th><th>비교 관점</th><th>확인한 내용</th></tr></thead><tbody>' + rows + '</tbody></table></div>',
                unsafe_allow_html=True)
    st.download_button("비교표 내보내기", comparison_csv(analysis), "media-comparison.csv", "text/csv",
                       key="media_comparison_export", type="tertiary")


def _render_detail(item, analysis, *, on_analyze, context_items):
    headline = analysis.get("headline") if analysis else ""
    st.markdown('<header class="media-detail-heading" id="media-insight-detail"><span>분석 노트</span><h3>' + _escape(headline or item["title"])
                + '</h3><p>' + _escape(' · '.join(value for value in [_text(item.get("company")), _country(item),
                                                                   _date_label(item.get("published_at"))] if value)) + '</p></header>',
                unsafe_allow_html=True)
    if headline and headline != item["title"]:
        st.caption("원문 제목 · " + _text(item["title"], 600))
    st.link_button("공식 원문 열기 ↗", _url(item["url"]), width="stretch")
    can_analyze = bool(on_analyze and _evidence(item.get("evidence"), item.get("url"), item.get("title")))
    label = "분석 노트 다시 만들기" if analysis else "분석 노트 만들기"
    if st.button(label, key="media_insights_analyze", disabled=not can_analyze, type="secondary", width="stretch"):
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
        st.markdown('<p class="media-detail-summary">' + _escape(analysis["summary"]) + '</p>', unsafe_allow_html=True)
        facts = _list_html(fact["text"] for fact in analysis["facts"])
        st.markdown(_section_html("01", "확인한 사실", facts), unsafe_allow_html=True)
        if analysis["interpretation"]:
            st.markdown(_section_html("02", "무엇을 의미하나", '<p>' + _escape(analysis["interpretation"]) + '</p>'), unsafe_allow_html=True)
        if analysis["btv_options"]:
            st.markdown(_section_html("03", "B tv에서 검토할 선택지", _list_html(analysis["btv_options"])), unsafe_allow_html=True)
        if analysis["questions"]:
            st.markdown(_section_html("?", "추가로 확인할 질문", _list_html(analysis["questions"])), unsafe_allow_html=True)
        st.download_button("검토 메모 내려받기", analysis_markdown(item, analysis), "media-insight-note.md", "text/markdown",
                           key="media_note_export", width="stretch")
    else:
        st.markdown('<div class="media-source-first"><h4>원문에서 확인한 내용</h4><p>'
                    + _escape(item.get("excerpt") or "공식 원문과 아래 근거를 확인할 수 있습니다.")
                    + '</p></div>', unsafe_allow_html=True)
        st.caption("분석 노트를 만들면 원문을 바탕으로 해석과 검토할 선택지를 함께 볼 수 있어요.")
    evidence = analysis["evidence"] if analysis else list(_evidence(item.get("evidence"), item.get("url"), item.get("title")).values())
    with st.expander("원문·근거", expanded=not bool(analysis)):
        for row in evidence[:20]:
            st.markdown('<div class="media-evidence-row"><div>' + _escape(_locator_label(row["locator"]) or "원문 인용") + '</div>'
                        + '<blockquote>' + _escape(row["text"], 5000) + '</blockquote>'
                        + _link(row["url"], (row["title"] or "공식 자료") + ' ↗') + '</div>', unsafe_allow_html=True)
        if not evidence:
            st.caption("인용할 본문을 확인하면 이곳에 근거가 표시됩니다.")
    return analysis


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


def _render_research(on_ask, selected_id, items):
    if not st.session_state.get("media_insight_research_open"):
        return
    with st.container(key="media_research_panel", border=True):
        st.markdown("**공식 자료에 질문하기**")
        st.caption("선택한 주제와 저장된 공식 자료에서 근거를 찾아 답합니다.")
        with st.form("media_research_form", clear_on_submit=True):
            question = st.text_input("조사할 질문", placeholder="예: 이 발표에 한국도 포함되나요? 지난 발표와 무엇이 달라졌나요?",
                                     key="media_research_question")
            submitted = st.form_submit_button("근거 찾아 답하기", disabled=on_ask is None, width="stretch")
        if submitted and _text(question):
            try:
                with st.spinner("공식 자료에서 답의 근거를 찾고 있어요…"):
                    answer = on_ask(_text(question), selected_id)
                checked = validated_answer(answer, items, selected_id)
                if checked:
                    st.session_state.setdefault(_QUESTIONS, []).append({"question": _text(question), "answer": checked})
                    st.session_state[_QUESTIONS] = st.session_state[_QUESTIONS][-5:]
                else:
                    st.info("저장된 자료에서 답의 근거를 확인하지 못했어요. 질문을 더 구체적으로 적어 주세요.")
            except (MediaInsightError, MediaInsightsStorageError) as error:
                _show_action_error(error)
            except Exception:
                st.warning("질문을 처리하지 못했어요. 기존 자료는 계속 확인할 수 있습니다.")
        for result in reversed(st.session_state.get(_QUESTIONS, [])[-3:]):
            answer = validated_answer(result.get("answer"), items)
            if answer is None:
                result["answer"] = None
                st.markdown('<div class="media-research-answer"><strong>' + _escape(result.get("question"))
                            + '</strong><p>공식 자료가 갱신됐어요. 현재 자료로 다시 질문해 주세요.</p></div>', unsafe_allow_html=True)
                continue
            st.markdown('<div class="media-research-answer" data-engine-version="'
                        + _escape(answer.get("source_engine_revision", "")) + '"><strong>' + _escape(result["question"]) + '</strong><p>'
                        + _escape(answer.get("answer"), 10000) + '</p></div>', unsafe_allow_html=True)
            links = []
            for source in _rows(answer.get("sources"), 12):
                url = _url(source.get("url"))
                if url:
                    links.append(_link(url, _text(source.get("title") or source.get("locator") or "공식 원문") + " ↗"))
            if links:
                st.markdown('<div class="media-answer-sources">' + ''.join(links) + '</div>', unsafe_allow_html=True)


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
            st.markdown('<div class="media-source-status"><strong>' + _escape(source.get("name") or source.get("id"))
                        + '</strong><span>' + _escape(label) + '</span><span>' + _escape(date) + '</span></div>', unsafe_allow_html=True)


def render_media_insights_page(snapshot, *, on_refresh=None, on_analyze=None, on_ask=None):
    """Render real saved items; only explicit buttons call the supplied callbacks."""
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    st.markdown('<style>' + Path(__file__).with_name("media_insights_styles.css").read_text(encoding="utf-8") + '</style>',
                unsafe_allow_html=True)
    with st.container(key="media_insights_section"):
        with st.container(key="media_insights_header"):
            title, question_action, refresh_action = st.columns([4, 1.5, 1], gap="small", vertical_alignment="center")
            with title:
                st.markdown('<header class="media-insights-heading"><h2>미디어 인사이트</h2><p>사업의 변화에서, 다음 선택의 근거까지.</p></header>', unsafe_allow_html=True)
            with question_action:
                st.button("리서치 질문하기", key="media_insights_research_toggle", on_click=_toggle_questions,
                          disabled=not bool(on_ask and insight_items(snapshot)), width="stretch")
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
        items = insight_items(snapshot)
        if not items:
            st.info("공식 발표와 실적 자료를 확인하면 주요 변화와 원문이 이곳에 표시됩니다.")
            _render_collection_sources(snapshot)
            return
        topic_values = {_topic(item) for item in items}
        topics = ["전체", *[topic for topic in _TOPIC_ORDER if topic in topic_values],
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
                period = st.selectbox("발표 기간", list(_PERIODS), key="media_insight_period", label_visibility="collapsed")
        filtered = filter_insight_items(items, topic=topic, country=country, lookback_days=_PERIODS[period])
        if not filtered:
            st.info("선택한 조건에 맞는 발표가 없습니다. 기간이나 주제를 바꾸면 저장된 자료를 확인할 수 있어요.")
            _render_collection_sources(snapshot)
            return
        known_ids = {item["id"] for item in filtered}
        if st.session_state.get(_SELECTED) not in known_ids:
            st.session_state[_SELECTED] = filtered[0]["id"]
        selected = next(item for item in filtered if item["id"] == st.session_state[_SELECTED])
        _render_research(on_ask, selected["id"], items)
        analysis = _item_analysis(selected, items)
        with st.container(key="media_insights_workspace"):
            stories, detail = st.columns([1.45, 1], gap="large")
            with stories:
                st.markdown('<div class="media-section-label">주요 변화'
                            + '<span>' + str(len(filtered)) + '건</span></div>', unsafe_allow_html=True)
                for index, item in enumerate(filtered[:6]):
                    _render_story(item, index=index, selected=item["id"] == selected["id"], analysis=_item_analysis(item, items))
                if len(filtered) > 6:
                    with st.expander(f"자료 {len(filtered) - 6}건 더 보기", expanded=selected["id"] not in {item["id"] for item in filtered[:6]}):
                        for index, item in enumerate(filtered[6:], start=6):
                            _render_story(item, index=index, selected=item["id"] == selected["id"], analysis=_item_analysis(item, items))
                _render_comparison(selected, analysis)
            with detail:
                with st.container(key="media_insights_detail"):
                    _render_detail(selected, analysis, on_analyze=on_analyze, context_items=items)
        _render_collection_sources(snapshot)
