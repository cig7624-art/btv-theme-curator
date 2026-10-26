"""Fresh, dated content reporting independent of the work popularity carousel.

News is selected by actual publication time, not by repeated collection time
or a work's audience/YouTube ranking. Different articles about the same event
remain one story; a genuinely different event can fill a remaining slot.
"""
from __future__ import annotations

import re
import hashlib
import json
import unicodedata

import pandas as pd

from issue_related_news import _article, _url_key, attach_related_news


def news_digest_revision(frame):
    """Fingerprint visible article identities, not their repeated capture time."""
    rows = []
    for item in frame.to_dict("records"):
        published = pd.to_datetime(item.get("published_at"), errors="coerce", utc=True)
        rows.append({"title": str(item.get("issue_title", "")).strip(),
                     "work": str(item.get("related_content", "")).strip(),
                     "url": _url_key(item.get("source_url", "")),
                     "published_at": published.isoformat() if pd.notna(published) else ""})
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _key(value):
    return re.sub(r"[^a-z0-9가-힣]", "", unicodedata.normalize("NFKC", str(value or "")).lower())


def _event(row):
    headline = str(row.get("issue_title", ""))
    event = str(row.get("event_type", ""))
    if event == "audience":
        for pattern, detail in ((r"예매율|예매량|사전\s*예매", "advance_booking"),
                                (r"시청률|시청\s*시간", "viewing"),
                                (r"박스오피스", "boxoffice"),
                                (r"누적\s*관객|관객\s*돌파", "attendance")):
            if re.search(pattern, headline):
                return event + ":" + detail
    if event == "release" and re.search(r"예고편|티저|선공개", headline):
        return "release:preview"
    return event or str(row.get("metric_summary", ""))


def select_news_digest(assessed, *, limit=4, now=None):
    """Return up to four newest eligible work/event stories without padding.

The caller supplies the complete assessed feed, not the popularity carousel or
its work-level aggregation. Existing article provenance/date guards are reused.
Unknown/future/stale publication dates never become fresh via a new collection.
"""
    limit = max(0, min(4, int(limit)))
    if assessed.empty or not limit:
        return assessed.iloc[0:0].copy()
    clock = pd.Timestamp.now(tz="Asia/Seoul") if now is None else pd.Timestamp(now)
    if pd.isna(clock):
        raise ValueError("now must be a valid timestamp")
    clock = clock.tz_localize("Asia/Seoul") if clock.tzinfo is None else clock.tz_convert("Asia/Seoul")
    candidates = []
    for row in assessed.to_dict("records"):
        article = _article(row, clock)
        if article is None:
            continue
        work = str(row.get("issue_key") or _key(row.get("related_content")))
        if not work:
            continue
        candidates.append((article["_published"], bool(article["_direct"]), article["_url_key"],
                           article["_headline_key"], work, _event(row), row))
    candidates.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    unique, seen_urls, seen_titles, seen_events = [], set(), set(), set()
    for candidate in candidates:
        _, _, url, title, work, event, _ = candidate
        if url in seen_urls or title in seen_titles or (work, event) in seen_events:
            continue
        unique.append(candidate)
        seen_urls.add(url); seen_titles.add(title); seen_events.add((work, event))
    chosen, works = [], set()
    for candidate in unique:
        if candidate[4] not in works:
            chosen.append(candidate)
            works.add(candidate[4])
            if len(chosen) == limit:
                break
    if len(chosen) < limit:
        counts = {work: 1 for work in works}
        selected_keys = {(candidate[4], candidate[5]) for candidate in chosen}
        for candidate in unique:
            work, event = candidate[4:6]
            if (work, event) in selected_keys or counts.get(work, 0) >= 2:
                continue
            chosen.append(candidate)
            selected_keys.add((work, event)); counts[work] = counts.get(work, 0) + 1
            if len(chosen) == limit:
                break
    if not chosen:
        return assessed.iloc[0:0].copy()
    chosen.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    result = pd.DataFrame([candidate[-1] for candidate in chosen]).reset_index(drop=True)
    return attach_related_news(result, assessed, now=clock)
