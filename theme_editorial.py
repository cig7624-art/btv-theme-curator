"""Pure, traceable theme cards with actual candidate artwork and source links."""
from __future__ import annotations

from collections.abc import Mapping
import html
import json
import re
import unicodedata
from urllib.parse import quote, urlparse

import pandas as pd


def _text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def _key(value):
    return re.sub(r"[^a-z0-9가-힣]", "", unicodedata.normalize("NFKC", _text(value)).lower())


def _url(value):
    value = _text(value)
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return ""


def _date(value):
    try:
        parsed = pd.Timestamp(value) if _text(value) else None
        if parsed is None or pd.isna(parsed):
            return None
        return parsed.tz_convert("Asia/Seoul") if parsed.tzinfo else parsed.tz_localize("Asia/Seoul")
    except (TypeError, ValueError, OverflowError):
        return None


def _source_kind(row):
    source = " ".join(_text(row.get(key)) for key in ("source_group", "source", "source_type")).lower()
    if "youtube" in source or "유튜브" in source:
        return "youtube", "YouTube"
    if "netflix" in source or "넷플릭스" in source:
        return "netflix", "Netflix"
    if any(word in source for word in ("kobis", "kofic", "박스오피스", "boxoffice", "극장")):
        return "theater", "극장"
    if "ott" in source:
        return "ott", "OTT"
    return "news", _text(row.get("publisher")) or "뉴스"


def _mentioned_sources(summary):
    text = summary.lower()
    sources = set()
    for kind, words in (("youtube", ("youtube", "유튜브")), ("netflix", ("netflix", "넷플릭스")),
                        ("theater", ("극장", "박스오피스", "kobis", "kofic")), ("news", ("기사", "보도", "뉴스"))):
        if any(word in text for word in words):
            sources.add(kind)
    return sources


def _work_mentioned(title, summary):
    """Match named works, never a short word embedded in another work's name."""
    title = _text(title)
    if len(_key(title)) < 2:
        return False
    quoted = [match.group(1) for pattern in (r"‘([^’]+)’", r"'([^']+)'", r'"([^"]+)"',
                                            r"“([^”]+)”", r"〈([^〉]+)〉", r"《([^》]+)》", r"≪([^≫]+)≫")
              for match in re.finditer(pattern, summary)]
    if any(_key(title) == _key(value) for value in quoted):
        return True
    # A quoted longer title is not evidence for a shorter prefix work or season.
    without_quotes = summary
    for pattern in (r"‘[^’]+’", r"'[^']+'", r'"[^"]+"', r"“[^”]+”", r"〈[^〉]+〉", r"《[^》]+》", r"≪[^≫]+≫"):
        without_quotes = re.sub(pattern, " ", without_quotes)
    literal = r"\s*".join(re.escape(part) for part in title.split())
    # Korean topic/subject particles are allowed, other suffixes are not.
    return bool(re.search(r"(?<![0-9a-z가-힣])" + literal + r"(?:(?:은|는|이|가|을|를|의|와|과|도|에서|으로|로))?(?![0-9a-z가-힣])",
                          without_quotes, re.I))


def _historical_issue_allowed(row):
    """Only age and replacement can explain a historical source's exclusion."""
    reason = _text(row.get("exclusion_reason"))
    if not reason:
        return True
    allowed = {
        _key("최근 7일 범위 밖"), _key("최근 7일 범위 밖의 게시물"),
        _key("집계 기준일이 오래됨"), _key("이전 집계(최신 집계로 대체)"),
    }
    return all(_key(part) in allowed for part in reason.split(";") if _text(part))


def linked_issue_for_theme(rec, issue_pool):
    """Find a same-work source for a stored summary without inventing provenance.

    Historical records may link to their original stored issue. A later source
    snapshot cannot replace the generation's source when its time is known.
    Internal generation IDs such as I01 are intentionally not mapped to today's
    issue list, whose positions may have changed.
    """
    theme = rec.get("theme") if isinstance(rec.get("theme"), Mapping) else {}
    summary = _text(rec.get("source_issue_summary") or theme.get("source_issue"))
    if not summary or issue_pool is None:
        return None
    if hasattr(issue_pool, "to_dict"):
        rows = issue_pool.to_dict("records")
    else:
        rows = list(issue_pool)
    cutoff = _date(rec.get("recommended_at") or rec.get("generated_at"))
    wanted_sources = _mentioned_sources(summary)
    matches = []
    for row in rows:
        if not isinstance(row, Mapping) or _text(row.get("quality_status")).lower() == "rejected":
            continue
        if not _historical_issue_allowed(row):
            continue
        title = _text(row.get("related_content"))
        url = _url(row.get("source_url"))
        if not url or not _work_mentioned(title, summary):
            continue
        kind, label = _source_kind(row)
        if wanted_sources and kind not in wanted_sources:
            continue
        observed = _date(row.get("collected_at")) or _date(row.get("date"))
        try:
            raw_metrics = row.get("metrics_json", "")
            metrics = dict(raw_metrics) if isinstance(raw_metrics, Mapping) else json.loads(_text(raw_metrics) or "{}")
            end = _date(metrics.get("observation_end")) if isinstance(metrics, Mapping) else None
            if end is not None and (observed is None or end > observed):
                observed = end
        except (TypeError, ValueError):
            pass
        if cutoff is not None and (observed is None or observed > cutoff):
            continue
        matches.append((observed or pd.Timestamp("1900-01-01", tz="Asia/Seoul"), len(_key(title)),
                        {"url": url, "source_label": label, "title": title,
                         "issue_title": _text(row.get("issue_title")),
                         "date": observed.strftime("%Y-%m-%d") if observed is not None else ""}))
    return max(matches, key=lambda item: item[:2])[2] if matches else None


def _candidate(item):
    title = _text(item.get("title"))
    if not title:
        return None
    return {"title": title, "year": _text(item.get("year")),
            "type": _text(item.get("type") or item.get("content_type")),
            "url": _url(item.get("source_url")), "poster": _url(item.get("poster_url")),
            "reason": _text(item.get("reason"))}


def _poster_html(item, index):
    title = html.escape(item["title"])
    placeholder = '<span class="theme-poster-placeholder" aria-hidden="true"><span>' + title + '</span></span>'
    image = ''
    if item["poster"]:
        # Streamlit sanitizes inline event handlers. An image background covers
        # the title only when it loads, so a failed request reveals the existing
        # title card without JavaScript, a broken-image icon or a hidden fallback.
        css_url = quote(item["poster"], safe=":/?=&%+#@,;")
        style = html.escape('background-image:url("' + css_url + '")', quote=True)
        image = '<span class="theme-poster-image" aria-hidden="true" style="' + style + '"></span>'
    contents = ('<span class="theme-poster-art tone-' + str(index % 4) + '" role="img" aria-label="' + title + ' 포스터">' + placeholder + image + '</span>'
                '<span class="theme-poster-caption">' + title + '</span>'
                '<span class="theme-poster-meta">' + html.escape(' · '.join(value for value in (item["type"], item["year"]) if value)) + '</span>')
    if item["url"]:
        return ('<a class="theme-poster-tile" href="' + html.escape(item["url"], quote=True) + '" target="_blank" rel="noopener noreferrer">' + contents + '</a>')
    return '<div class="theme-poster-tile">' + contents + '</div>'


def theme_card_html(index, rec, issue_pool=None):
    """Render one theme and its actual candidates without network or mutation."""
    theme = rec.get("theme") if isinstance(rec.get("theme"), Mapping) else {}
    name = _text(theme.get("theme_name")) or "추천 테마"
    summary = _text(rec.get("source_issue_summary") or theme.get("source_issue"))
    reason = _text(rec.get("rationale") or theme.get("copy"))
    linked = linked_issue_for_theme(rec, issue_pool)
    source = '<span class="theme-issue-source">' + html.escape(linked["source_label"]) + '</span>' if linked else ''
    source_link = ('<a class="theme-issue-link" href="' + html.escape(linked["url"], quote=True) + '" target="_blank" rel="noopener noreferrer" title="'
                   + html.escape(linked["issue_title"] + (' · ' + linked["date"] if linked["date"] else ''), quote=True) + '">이슈 보기 ↗</a>') if linked else ''
    raw_contents = rec.get("contents") or rec.get("matched_contents") or []
    candidates, seen = [], set()
    for item in raw_contents if isinstance(raw_contents, (list, tuple)) else []:
        candidate = _candidate(item) if isinstance(item, Mapping) else None
        if candidate is None:
            continue
        identity = (_key(candidate["title"]), candidate["year"])
        if identity not in seen:
            candidates.append(candidate)
            seen.add(identity)
    posters = ''.join(_poster_html(item, position) for position, item in enumerate(candidates[:5]))
    more = ''
    if len(candidates) > 5:
        links = []
        for item in candidates:
            label = html.escape(item["title"] + (' · ' + item["year"] if item["year"] else ''))
            link = ('<a href="' + html.escape(item["url"], quote=True) + '" target="_blank" rel="noopener noreferrer">' + label + ' ↗</a>') if item["url"] else '<span>' + label + '</span>'
            reason_html = '<span class="theme-candidate-reason">' + html.escape(item["reason"]) + '</span>' if item["reason"] else ''
            links.append('<li>' + link + reason_html + '</li>')
        more = ('<details class="theme-more-candidates"><summary>전체 ' + str(len(candidates)) + '편 보기</summary>'
                '<ul class="theme-candidate-list">' + ''.join(links) + '</ul></details>')
    if candidates:
        poster_area = ('<div class="theme-poster-head">추천 작품 <strong>' + str(len(candidates)) + '편</strong></div>'
                       '<div class="theme-poster-rail" tabindex="0" aria-label="' + html.escape(name, quote=True) + ' 추천 작품">' + posters + '</div>' + more)
    else:
        poster_area = '<div class="theme-content-empty">아직 연결된 작품이 없어요.<br>상단의 ‘콘텐츠 채우기’로 추천 작품을 불러오세요.</div>'
    return (
        '<article class="theme-editorial-card"><div class="theme-editorial-copy">'
        '<div class="theme-issue-label"><span>연결된 이슈</span>' + source_link + '</div>'
        '<p class="theme-issue-summary">' + source + html.escape(summary or '연결된 이슈가 저장되지 않은 테마예요.') + '</p>'
        '<span class="theme-editorial-index">' + f'{index:02d}' + '</span>'
        '<h3 class="theme-editorial-title">' + html.escape(name) + '</h3>'
        + ('<p class="theme-editorial-reason">' + html.escape(reason) + '</p>' if reason else '')
        + '<p class="theme-editorial-meta">' + html.escape(' · '.join(_text(theme.get(key)) for key in ("genre", "mood") if _text(theme.get(key)))) + '</p>'
        '</div><div class="theme-poster-area">' + poster_area + '</div></article>'
    )
