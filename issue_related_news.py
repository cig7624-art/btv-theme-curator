"""Attach dated reporting about the same work without changing issue selection.

The input news rows must already have passed ``assess_issues``. Exact work keys
keep seasons and parts separate; article counts never affect the ranking.
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd

from issue_quality import NEWS
from issue_rules import ISSUE_LOOKBACK_DAYS


def _text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def _date(value):
    try:
        result = pd.Timestamp(value) if _text(value) else None
        if result is None or pd.isna(result):
            return None
        return result.tz_convert("Asia/Seoul") if result.tzinfo else result.tz_localize("Asia/Seoul")
    except (TypeError, ValueError, OverflowError):
        return None


def _valid_url(value):
    try:
        parsed = urlparse(_text(value))
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not re.search(r"(?:^|/)(?:404|error|notfound|login|signin|accessdenied)(?:/|$)", parsed.path, re.I))
    except ValueError:
        return False


def _is_relay(url):
    host = (urlparse(url).hostname or "").lower() if _valid_url(url) else ""
    return host == "news.google.com" or host.endswith(".news.google.com")


def _url_key(url):
    if not _valid_url(url):
        return ""
    parsed = urlparse(url)
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "ref"}]
    return urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", urlencode(sorted(query)), ""))


def _headline(title, publisher=""):
    title = html.unescape(_text(title))
    # The feed's publisher suffix is display metadata, not part of the headline.
    if publisher and title.endswith(" - " + publisher):
        title = title[:-(len(publisher) + 3)].rstrip()
    return title


def _headline_key(title):
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", title).lower())


def _metrics(row):
    raw = row.get("metrics_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        result = json.loads(_text(raw) or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _article(row, now):
    if _text(row.get("source_group")) != NEWS or _text(row.get("eligible")).lower() != "true":
        return None
    if _text(row.get("quality_status")).lower() == "rejected":
        return None
    published = _date(row.get("published_at"))
    # A collection date cannot stand in for a missing article publication date.
    if published is None or published > now + pd.Timedelta(minutes=1):
        return None
    if (now.normalize() - published.normalize()).days >= ISSUE_LOOKBACK_DAYS:
        return None
    publisher = _text(row.get("publisher"))
    if len(publisher) < 2 or publisher.lower() in {"미상", "알 수 없음", "뉴스", "google news", "google 뉴스", "구글 뉴스", NEWS}:
        return None
    url = _text(row.get("source_url"))
    if not _valid_url(url):
        return None
    relay = _is_relay(url)
    if relay:
        metrics = _metrics(row)
        # Only an actual dated feed entry can support a relay link. Old rows with
        # inferred publisher names are not upgraded to verified reporting.
        publisher_url = _text(metrics.get("publisher_url"))
        if (metrics.get("news_provenance") != "google_news_rss"
                or not _valid_url(publisher_url) or _is_relay(publisher_url)
                or _url_key(metrics.get("rss_url", "")) != _url_key(url)
                or _text(row.get("event_type")) in {"", "content_news"}):
            return None
    title = _headline(row.get("issue_title"), publisher)
    if not title or not _headline_key(title):
        return None
    return {"title": title, "url": url, "publisher": publisher,
            "published_at": published.strftime("%Y-%m-%d"),
            "via": "Google 뉴스" if relay else "",
            "_published": published, "_url_key": _url_key(url),
            "_headline_key": _headline_key(title), "_direct": not relay}


def attach_related_news(ranked, assessed, *, now=None, max_articles=3):
    """Return ranked works with at most three same-work news links per card.

    Work keys must come from assessment, so an unspecified season is never
    silently matched to a particular season. The returned copy preserves row
    order, index, selection explanations and source counts.
    """
    result = ranked.copy()
    result["related_news"] = pd.Series([[] for _ in range(len(result))], index=result.index, dtype=object)
    if result.empty or assessed.empty or max_articles <= 0:
        return result
    clock = _date(now) if now is not None else pd.Timestamp.now(tz="Asia/Seoul")
    if clock is None:
        raise ValueError("now must be a valid date or timestamp")
    by_work = {}
    for row in assessed.to_dict("records"):
        work_key = _text(row.get("issue_key"))
        if not work_key.startswith("work:") or work_key == "work:":
            continue
        article = _article(row, clock)
        if article is not None:
            by_work.setdefault(work_key, []).append(article)
    for index, row in result.iterrows():
        articles = by_work.get(_text(row.get("issue_key")), [])
        primary_url = _url_key(_text(row.get("source_url")))
        primary_title = (_headline_key(_headline(row.get("issue_title"), _text(row.get("publisher"))))
                         if _text(row.get("source_group")) == NEWS else "")
        seen_urls, seen_titles, selected = set(), set(), []
        # Prefer the publisher's own URL when a feed and original refer to the
        # same headline. Sort the final, deduplicated articles by date below.
        for article in sorted(articles, key=lambda item: (item["_direct"], item["_published"]), reverse=True):
            url_key, title_key = article["_url_key"], article["_headline_key"]
            if url_key == primary_url or title_key == primary_title or url_key in seen_urls or title_key in seen_titles:
                continue
            seen_urls.add(url_key)
            seen_titles.add(title_key)
            selected.append(article)
        selected.sort(key=lambda item: item["_published"], reverse=True)
        result.at[index, "related_news"] = [{key: value for key, value in article.items() if not key.startswith("_")}
                                            for article in selected[:max_articles]]
    return result
