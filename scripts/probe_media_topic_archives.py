"""Read bounded official archives for dated advertising and media AI documents.

This probe never modifies the production snapshot, calls a model, or persists
remote data. Its separate report contains source evidence for human review.
An inspected URL is not automatically a verified article or a selected item.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
from urllib.parse import urlencode

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import media_insights_collection as collection


TARGET_TOPICS = {"advertising_pricing", "ai_technology"}
_AI_SUBJECT = re.compile(r"\bAI\b|OpenAI|\bSora\b|artificial intelligence|machine learning|search|discovery|인공지능|생성형|기술|셋톱|화질|멀티뷰|음성|자막", re.I)
_MEDIA_SUBJECT = re.compile(r"streaming|Disney\+|Disney Plus|Netflix|Hulu|ESPN|Prime Video|B\s*tv|IPTV|OTT|movie|film|video|content|storytell|영상|영화|방송|미디어|콘텐츠|유플러스|구독", re.I)


def archive_sources():
    """Search only registered publishers and three explicitly inspected URLs."""
    registry = {source["id"]: source for source in collection.SOURCES}
    result = []
    searches = [
        ("disney_news", "https://thewaltdisneycompany.com/feed/", term)
        for term in ("advertising", "ad-supported", "pricing", "AI-powered", "artificial intelligence", "OpenAI")
    ] + [
        ("skb_products", "https://blog.bworld.co.kr/feed/", "AI"),
        ("skb_products", "https://blog.bworld.co.kr/feed/", "B tv 광고"),
        ("lgu_products", "https://news.lguplus.com/feed/", "U+tv AI"),
        ("lgu_products", "https://news.lguplus.com/feed/", "U+tv 요금제"),
    ]
    for number, (source_id, base, term) in enumerate(searches, 1):
        source = {**registry[source_id], "url": base + "?" + urlencode({"s": term})}
        source.pop("discovery_urls", None)
        source.pop("document_urls", None)
        source["fetch_article_images"] = False
        result.append({"probe_id": "archive-" + str(number), "source": source, "query": term})
    netflix = {"id": "netflix_news", "name": "Netflix 공식 뉴스룸", "company": "Netflix",
               "source_type": "official_announcement", "country": "",
               "allowed_hosts": ["about.netflix.com"], "image_hosts": ["about.netflix.com"]}
    # Their status and content must be observed; these are not seeded findings.
    for slug in ("netflix-advertising-one-year-in", "netflix-upfront-2025", "netflix-upfront-2026"):
        source = {**netflix, "url": "https://about.netflix.com/en/news/" + slug}
        result.append({"probe_id": slug, "source": source, "query": ""})
    return result


def _eligible(item, observed):
    published = collection._date(item.get("published_at"))
    if not published or published > observed[:10] or item.get("topic") not in TARGET_TOPICS:
        return False
    title, body = item.get("title", ""), item.get("body_text", "")
    if not item.get("evidence") or not _MEDIA_SUBJECT.search(title + " " + body[:8000]):
        return False
    # '접근성 넓힌다' can describe festival sponsorship rather than technology.
    if item["topic"] == "ai_technology" and not _AI_SUBJECT.search(title):
        return False
    return True


def _document_diagnostic(payload):
    """Keep actual text for reviewing an unrecognized direct headline."""
    content = payload.get("content", b"")
    if re.search(br"<(?:rss|feed)[\s>]", content[:1500], re.I):
        return {}
    soup = BeautifulSoup(content, "html.parser")
    heading = soup.find("h1")
    title = collection._text(heading.get_text(" ", strip=True) if heading else "", 300)
    dates = []
    for data in collection._json_articles(soup):
        if data.get("datePublished"):
            dates.append(collection._date(data["datePublished"]))
    for node in soup.select('meta[property="article:published_time"],meta[name="date"],time')[:5]:
        dates.append(collection._date(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)))
    region = soup.find("article") or soup.find("main")
    paragraphs = []
    if region:
        for node in region.select("script,style,nav,footer,header,aside"):
            node.decompose()
        paragraphs = [collection._text(node.get_text(" ", strip=True), 1500) for node in region.find_all("p")]
    return {"document_title": title, "publication_dates_observed": sorted(set(filter(None, dates))),
            "source_paragraphs_for_review": [text for text in paragraphs if len(text) >= 35][:10]}


def run_probe(*, previous=None, max_requests=80, fetcher=None):
    """Return evidence only; the caller chooses whether to write the report."""
    observed = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = previous if isinstance(previous, dict) else {}
    previous_urls = {item.get("url") for item in previous.get("items", []) if isinstance(item, dict)}
    request_count, request_lock = 0, threading.Lock()

    def inspect(entry):
        source, root_payload = entry["source"], {}
        def bounded_fetch(url):
            nonlocal request_count
            with request_lock:
                if request_count >= max_requests:
                    raise collection.CollectionError("probe_request_budget_exhausted")
                request_count += 1
            payload = collection._fetch(url, source, fetcher) if fetcher else collection._http_fetch(url, source)
            if url == source["url"]:
                root_payload.update(payload)
            return payload
        state, items = collection._collect_source(source, {}, fetcher=bounded_fetch, observed=observed,
                                                  limit=8 if entry["query"] else 1)
        selected = [item for item in items if _eligible(item, observed)]
        row = {"id": entry["probe_id"], "source_id": source["id"], "url": source["url"],
               "query": entry["query"], "status": state["status"], "error_code": state["error_code"],
               "parsed_documents": len(items), "dated_relevant_documents": len(selected)}
        if entry["query"] == "" and root_payload:
            row.update(_document_diagnostic(root_payload))
        return row, selected

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(inspect, archive_sources()))
    by_url = {}
    for _, items in results:
        for item in items:
            by_url.setdefault(item["url"], item)
    all_items = sorted(by_url.values(), key=lambda item: (item["published_at"], item["url"]), reverse=True)
    new_items = [item for item in all_items if item["url"] not in previous_urls]
    counts = Counter(item["topic"] for item in all_items)
    existing_counts = Counter(item.get("topic") for item in previous.get("items", [])
                              if isinstance(item, dict) and _eligible(item, observed))
    combined_counts = existing_counts + Counter(item["topic"] for item in new_items)
    return {"checked_at": observed, "mode": "read_only_official_archive_probe", "requests": request_count,
            "request_budget": max_requests, "sources": [row for row, _ in results],
            "confirmed_topic_counts": dict(counts), "new_confirmed_topic_counts": dict(Counter(item["topic"] for item in new_items)),
            "combined_eligible_topic_counts": dict(combined_counts),
            "minimum_three_reached": {topic: combined_counts[topic] >= 3 for topic in sorted(TARGET_TOPICS)},
            "new_confirmed_items": new_items}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/topic_archive_probe.json")
    parser.add_argument("--max-requests", type=int, choices=range(1, 121), default=80, metavar="1..120")
    args = parser.parse_args(argv)
    if args.output.resolve() == collection.SNAPSHOT_PATH.resolve():
        parser.error("The archive probe cannot overwrite the production snapshot.")
    report = run_probe(previous=collection.load_snapshot(), max_requests=args.max_requests)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "requests": report["requests"],
                      "new_confirmed_topic_counts": report["new_confirmed_topic_counts"],
                      "minimum_three_reached": report["minimum_three_reached"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
