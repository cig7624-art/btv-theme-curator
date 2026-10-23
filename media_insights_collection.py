"""Collect official media-business documents and their actual source paragraphs.

This module never generates an analysis or infers a price change from a first
observation. Publication dates, document evidence and failed-source state are
kept separately from the time at which collection was attempted.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


SNAPSHOT_PATH = Path(__file__).with_name("media_insights_snapshot.json")
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_PDF_PAGES = 30
MAX_EVIDENCE = 64
MAX_ITEMS = 160
TIMEOUT = (5, 18)

SOURCES = (
    {"id": "netflix_ir", "name": "Netflix Investor Relations", "company": "Netflix",
     "url": "https://ir.netflix.net/rss/pressrelease.aspx",
     "source_type": "earnings", "country": "global",
     "allowed_hosts": ["ir.netflix.net", "s22.q4cdn.com"]},
    {"id": "disney_ir", "name": "The Walt Disney Company Investor Relations", "company": "Disney",
     "url": "https://thewaltdisneycompany.com/investor-relations/",
     "source_type": "earnings", "country": "global",
     "allowed_hosts": ["thewaltdisneycompany.com", "www.thewaltdisneycompany.com", "investors.thewaltdisneycompany.com", "s206.q4cdn.com"]},
    {"id": "amazon_ir", "name": "Amazon Investor Relations", "company": "Amazon",
     "url": "https://ir.aboutamazon.com/rss/pressrelease.aspx",
     "source_type": "earnings", "country": "global",
     "allowed_hosts": ["ir.aboutamazon.com", "s2.q4cdn.com"]},
    {"id": "comcast_ir", "name": "Comcast Investor Relations", "company": "Comcast",
     "url": "https://www.cmcsa.com/financial-information/quarterly-results",
     "source_type": "earnings", "country": "global",
     "allowed_hosts": ["www.cmcsa.com", "cmcsa.com"]},
    {"id": "disney_news", "name": "The Walt Disney Company News", "company": "Disney",
     "url": "https://thewaltdisneycompany.com/feed/", "source_type": "official_announcement", "country": "",
     "allowed_hosts": ["thewaltdisneycompany.com", "www.thewaltdisneycompany.com"]},
    {"id": "amazon_news", "name": "About Amazon: Entertainment", "company": "Amazon",
     "url": "https://www.aboutamazon.com/news/entertainment", "source_type": "official_announcement", "country": "",
     "allowed_hosts": ["www.aboutamazon.com", "aboutamazon.com"]},
    {"id": "skt_news", "name": "SK텔레콤 뉴스룸", "company": "SKT",
     "url": "https://news.sktelecom.com/feed", "source_type": "official_announcement", "country": "KR",
     "require_media_subject": True, "allowed_hosts": ["news.sktelecom.com", "news-static.sktelecom.com"]},
)

TOPIC_LABELS = {
    "advertising_pricing": "광고·요금제", "partnership_distribution": "제휴·유통",
    "earnings_strategy": "실적·사업 전략", "sports_content": "스포츠·콘텐츠",
    "ai_technology": "AI·기술",
}
_TOPICS = (
    ("advertising_pricing", re.compile(r"advertis|ad[- ]supported|ad tier|ad suite|ads suite|pricing|price|subscription plan|요금|광고|구독료", re.I)),
    ("sports_content", re.compile(r"sports? rights?|broadcast rights?|streaming rights?|live sports?|NFL|NBA|premier league|super bowl|스포츠|중계권", re.I)),
    ("ai_technology", re.compile(r"generative ai|artificial intelligence|AI search|AI-powered|machine learning|생성형|인공지능|AI\s*검색", re.I)),
    ("partnership_distribution", re.compile(r"partners? with|partnership|bundle|distribution|acquisition|acquire|merger|transaction|offer.*warner|제휴|유통|합병|인수|번들", re.I)),
    ("earnings_strategy", re.compile(r"earnings|quarter|financial results|shareholder|revenue|annual report|engagement report|what we watched|분기|실적|사업보고서|감사보고서|매출", re.I)),
)
_DOCUMENT_LINK = re.compile(r"\.pdf(?:$|\?)|/news/[^/]+|/news-releases/|/press-release|/press-releases/|earnings|shareholder|financial-results|reports?-.*quarter|quarter.*results|실적|보도자료", re.I)
_EXCLUDED_LINK = re.compile(r"privacy|cookie|terms|contact|accessibility|login|sign.?in|unsubscribe|stock-information|email-alert|/events-and-presentations/.*calendar", re.I)
_GENERIC_TITLES = re.compile(r"^(?:investor relations|quarterly (?:earnings|results)|financials?|news(?:room)?|entertainment|press releases?|home|Netflix Newsroom|about amazon)(?:\s*[|–—-].*)?$", re.I)
_MONTHS = "January February March April May June July August September October November December".split()
_MONTH_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})\b", re.I)
_ISO_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_SECRET_QUERY = frozenset({"key", "api_key", "apikey", "token", "access_token", "authorization", "signature", "password"})
_SCHEDULE_ONLY = re.compile(r"to (?:announce|webcast)|conference call|participate.*(?:conference|session)|mini-tender|annual meeting|연구|보이스피싱", re.I)
_MEDIA_SUBJECT = re.compile(r"B\s*tv|IPTV|OTT|스트리밍|미디어|중계권|영상\s*서비스|콘텐츠\s*(?:제휴|유통|사업)|구독\s*상품|방송|media|streaming|Prime Video", re.I)


class CollectionError(RuntimeError):
    """A safe fixed code, never the HTTP response or an exception containing keys."""


def _text(value, limit=2400):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _timestamp(now=None):
    if isinstance(now, str):
        observed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    else:
        observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _official_url(value, source, base=""):
    try:
        url = urljoin(base or source["url"], str(value or "").strip())
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"https", "http"} or parts.username or parts.password or not host:
            return ""
        if host not in {str(h).lower() for h in source.get("allowed_hosts", [])}:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        if parts.port not in {None, 80, 443}:
            return ""
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if any(key.lower() in _SECRET_QUERY for key, _ in pairs):
            return ""
        pairs = [(key, value) for key, value in pairs if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", urlencode(pairs), ""))
    except (ValueError, TypeError):
        return ""


def _http_fetch(url, source):
    """Check every redirect before requesting it, and bound response size."""
    for _ in range(5):
        if not _official_url(url, source):
            raise CollectionError("redirect_not_allowed")
        response = requests.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                                headers={"User-Agent": "BtvMediaInsights/1.0 (public official document research)",
                                         "Accept": "text/html,application/pdf,application/xml;q=0.9,*/*;q=0.7"})
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                target = _official_url(response.headers.get("Location", ""), source, url)
                if not target:
                    raise CollectionError("redirect_not_allowed")
                url = target
                continue
            if response.status_code >= 400:
                raise CollectionError("http_" + str(response.status_code))
            content = bytearray()
            for chunk in response.iter_content(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_DOCUMENT_BYTES:
                    raise CollectionError("document_too_large")
            return {"content": bytes(content), "url": url,
                    "content_type": response.headers.get("Content-Type", ""), "status": response.status_code}
        finally:
            response.close()
    raise CollectionError("redirect_limit")


def _fetch(url, source, fetcher):
    if not _official_url(url, source):
        raise CollectionError("url_not_allowed")
    value = fetcher(url) if fetcher else _http_fetch(url, source)
    if isinstance(value, str):
        value = {"content": value.encode(), "url": url, "content_type": "text/html"}
    elif isinstance(value, bytes):
        value = {"content": value, "url": url}
    elif hasattr(value, "content"):
        value = {"content": value.content, "url": getattr(value, "url", url),
                 "status": getattr(value, "status_code", 200),
                 "content_type": getattr(value, "headers", {}).get("Content-Type", "")}
    if not isinstance(value, dict):
        raise CollectionError("invalid_response")
    status = value.get("status", value.get("http_status", 200))
    if not isinstance(status, int) or status >= 400:
        raise CollectionError("http_" + str(status) if isinstance(status, int) else "invalid_response")
    final = _official_url(value.get("url") or url, source)
    if not final:
        raise CollectionError("redirect_not_allowed")
    content = value.get("content", value.get("html", value.get("text", b"")))
    if isinstance(content, str):
        content = content.encode("utf-8")
    if not isinstance(content, bytes) or len(content) > MAX_DOCUMENT_BYTES:
        raise CollectionError("document_too_large" if isinstance(content, bytes) else "invalid_response")
    if not content.strip():
        raise CollectionError("empty_response")
    return {"content": content, "url": final, "content_type": str(value.get("content_type", ""))}


def _date(value):
    value = _text(value, 150)
    if not value:
        return ""
    match = _ISO_DATE.search(value)
    if match:
        try:
            return datetime(*map(int, match.groups())).date().isoformat()
        except ValueError:
            return ""
    match = _MONTH_DATE.search(value)
    if match:
        try:
            month = next(i + 1 for i, name in enumerate(_MONTHS) if name.lower() == match[1].lower())
            return datetime(int(match[3]), month, int(match[2])).date().isoformat()
        except ValueError:
            return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (ValueError, TypeError, OverflowError):
        return ""


def _publication_dateline(value):
    """Accept explicit publication datelines, not dates in financial prose."""
    for line in str(value or "").splitlines()[:12]:
        line = _text(line, 180)
        match = _MONTH_DATE.search(line) or _ISO_DATE.search(line)
        if not match:
            continue
        prefix = line[:match.start()].strip(" ,–—-|")
        if not prefix or re.fullmatch(r"(?:Published|Date|Dated)(?:\s+on)?[:\s]*", prefix, re.I):
            return _date(match.group())
        # Official press releases use city datelines, e.g. SEATTLE--July 30.
        if len(prefix) < 65 and re.fullmatch(r"[A-Z\s,.()]+", prefix) and re.search(r"[-–—]{1,2}\s*$", line[:match.start()]):
            return _date(match.group())
    return ""


def _json_articles(soup):
    found = []
    def walk(value):
        if isinstance(value, dict):
            kind = value.get("@type", [])
            kind = [kind] if isinstance(kind, str) else kind
            if any(x in {"Article", "NewsArticle", "Report", "BlogPosting", "PressRelease"} for x in kind):
                found.append(value)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    for node in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            walk(json.loads(node.string or node.get_text()))
        except (TypeError, ValueError):
            continue
    return found


def _classify(title, body, source):
    if _SCHEDULE_ONLY.search(title):
        return ""
    if source.get("require_media_subject") and not _MEDIA_SUBJECT.search(title):
        return ""
    for topic, pattern in _TOPICS:
        if pattern.search(title):
            return topic
    if source.get("source_type") == "earnings":
        return "earnings_strategy"
    # A newsroom article needs an actual business subject in its headline;
    # footer words and film descriptions cannot turn it into business evidence.
    return ""


def _period(title):
    match = re.search(r"\b(Q[1-4])\s*[- ]?\s*(20\d{2})\b|\b(20\d{2})\s*[- ]?\s*(Q[1-4])\b", title, re.I)
    if match:
        return f"{match[2] or match[3]} {(match[1] or match[4]).upper()}"
    return ""


def _make_item(source, url, title, paragraphs, published_at, collected_at, *, image_url=""):
    title = _text(title, 300)
    if (not title or _GENERIC_TITLES.fullmatch(title)
            or re.search(r"(?:^|[|–—-])\s*(?:investor relations|quarterly earnings|financials?|overview)\s*$", title, re.I)):
        return None
    paragraphs = [(locator, _text(text, 1500)) for locator, text in paragraphs if len(_text(text)) >= 35]
    # A heading, navigation block or metadata summary alone is not a document.
    if len(paragraphs) < 2 or sum(len(text) for _, text in paragraphs) < 180:
        return None
    topic = _classify(title, " ".join(text for _, text in paragraphs), source)
    if not topic:
        return None
    identity = "mi-" + sha256(url.encode()).hexdigest()[:18]
    evidence = [{"id": identity + "-" + re.sub(r"[^a-z0-9]+", "-", locator.lower()).strip("-"),
                 "text": text, "locator": locator,
                 "url": url + "#page=" + match[1] if (match := re.match(r"^page(\d+)-p\d+$", locator)) else url}
                for locator, text in paragraphs[:MAX_EVIDENCE]]
    body = "\n".join(row["text"] for row in evidence)
    return {"id": identity, "source_id": source["id"], "company": source["company"],
            "topic": topic, "title": title, "url": url, "published_at": published_at,
            "collected_at": collected_at, "country": source.get("country", ""), "period": _period(title),
            "source_name": source["name"], "source_type": source.get("source_type", "official_announcement"),
            "excerpt": body[:420], "body_text": body, "image_url": image_url,
            "evidence": evidence, "content_hash": sha256((title + "\n" + body).encode()).hexdigest()}


def parse_html_document(html, source, *, url=None, collected_at=None):
    """Extract real article paragraphs; publication date can remain unknown."""
    url = _official_url(url or source["url"], source)
    if not url:
        raise CollectionError("url_not_allowed")
    soup = BeautifulSoup(html, "html.parser")
    articles = _json_articles(soup)
    def current_article(item):
        candidate_url = item.get("url") or item.get("mainEntityOfPage", "")
        if isinstance(candidate_url, dict):
            candidate_url = candidate_url.get("@id", "")
        return not candidate_url or _official_url(candidate_url, source, url) == url
    structured = next((item for item in articles if (item.get("headline") or item.get("name")) and current_article(item)), {})
    title_node = soup.find("h1")
    og = soup.find("meta", property="og:title")
    title = structured.get("headline") or structured.get("name") or (title_node.get_text(" ", strip=True) if title_node else "") or (og.get("content", "") if og else "")
    if not title:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
    published = _date(structured.get("datePublished"))
    if not published:
        for attrs in ({"property": "article:published_time"}, {"name": "date"}, {"name": "pubdate"}, {"name": "DC.date.issued"}):
            node = soup.find("meta", attrs=attrs)
            if node and (published := _date(node.get("content", ""))):
                break
    if not published:
        node = soup.find("time")
        published = _date((node.get("datetime") or node.get_text(" ", strip=True)) if node else "")
    if not published:
        node = soup.select_one("[itemprop='datePublished'],.module_date-text,.module-news-details_date,.entry-date")
        if node:
            published = _date(node.get("content") or node.get("datetime") or node.get_text(" ", strip=True))
    region = (soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
              or soup.find(class_=re.compile(r"article-body|article-content|press-release|news-release|entry-content|module_body", re.I)))
    if region is None:
        # An explicit article body in structured data is also source text.
        body = structured.get("articleBody")
        if not isinstance(body, str):
            return None
        paragraphs = [(f"p{i:03d}", text) for i, text in enumerate(re.split(r"\n+", body), 1)]
    else:
        for node in region.select("script,style,nav,footer,header,form,button,aside,[aria-hidden='true']"):
            node.decompose()
        if not published:
            # Only a dateline near the start of an actual article, not a year
            # found in financial discussion or a copyright footer.
            published = _publication_dateline(region.get_text("\n", strip=True))
        paragraphs = []
        seen = set()
        for node in region.find_all(["p", "li", "td"]):
            text = _text(node.get_text(" ", strip=True), 1500)
            if text and text not in seen:
                seen.add(text)
                paragraphs.append((f"p{len(paragraphs) + 1:03d}", text))
    image = soup.find("meta", property="og:image")
    # Artwork is optional and never used as factual evidence. Accept only an
    # official-host asset; unknown CDN artwork remains absent.
    image_url = _official_url(image.get("content", ""), source, url) if image else ""
    return _make_item(source, url, title, paragraphs, published, _timestamp(collected_at), image_url=image_url)


def parse_pdf_document(content, source, *, url, collected_at=None, title_hint="", pdf_reader=None):
    url = _official_url(url, source)
    if not url:
        raise CollectionError("url_not_allowed")
    if pdf_reader is None:
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise CollectionError("pdf_parser_unavailable") from error
        pdf_reader = PdfReader
    try:
        document = pdf_reader(BytesIO(content))
        paragraphs = []
        page_texts = []
        for page_number, page in enumerate(document.pages[:MAX_PDF_PAGES], 1):
            text = page.extract_text() or ""
            page_texts.append(text)
            # PDFs often have visual line wraps rather than paragraph breaks.
            for index, part in enumerate(re.split(r"\n\s*\n|(?<=\.)\s*\n", text), 1):
                if len(_text(part)) >= 35:
                    paragraphs.append((f"page{page_number}-p{index:03d}", part))
        first = page_texts[0] if page_texts else ""
        title = _text(title_hint, 300) or _text(" ".join(first.splitlines()[:3]), 300)
        published = _publication_dateline(first[:1000])
        return _make_item(source, url, title, paragraphs, published, _timestamp(collected_at))
    except CollectionError:
        raise
    except Exception as error:
        raise CollectionError("pdf_parse_failed") from error


def _discover_documents(payload, source):
    html = payload["content"].decode("utf-8", errors="replace")
    links, seen = [], set()
    # Parse XML first: HTML parsers treat <link> as void and can mistake an
    # embedded article's inline links for the feed's canonical document URL.
    if re.search(r"<(?:rss|feed)[\s>]", html[:1500], re.I):
        try:
            tree = ET.fromstring(html)
            for entry in list(tree.findall(".//item")) + list(tree.findall(".//{*}entry")):
                title = _text(entry.findtext("title") or entry.findtext("{*}title"), 300)
                nodes = list(entry.findall("link")) + list(entry.findall("{*}link"))
                node = next((node for node in nodes if node.get("rel", "alternate") == "alternate"), None)
                raw = (node.get("href") or node.text) if node is not None else ""
                url = _official_url(raw, source, payload["url"])
                if url and url not in seen and _classify(title, "", source):
                    published = _date(entry.findtext("pubDate") or entry.findtext("{*}published") or entry.findtext("{*}updated"))
                    body_node = entry.find("{http://purl.org/rss/1.0/modules/content/}encoded")
                    if body_node is None:
                        body_node = entry.find("{*}content")
                    inline_html = body_node.text if body_node is not None else ""
                    links.append({"url": url, "title": title, "published_at": published, "inline_html": inline_html or ""})
                    seen.add(url)
        except ET.ParseError:
            pass
        return links
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("header,footer,nav,[role='navigation']"):
        node.decompose()
    for node in soup.find_all("a", href=True):
        url = _official_url(node.get("href"), source, payload["url"])
        title = _text(node.get_text(" ", strip=True) or node.get("aria-label", ""), 300)
        if not url or url in seen or url == payload["url"] or _EXCLUDED_LINK.search(url + " " + title):
            continue
        if not (_DOCUMENT_LINK.search(url) or any(pattern.search(title) for _, pattern in _TOPICS)):
            continue
        if not _classify(title, "", source):
            continue
        seen.add(url)
        links.append({"url": url, "title": title})
    return links


def _collect_source(source, previous_source, *, fetcher, observed, limit):
    state = {"id": source["id"], "name": source["name"], "company": source["company"], "url": source["url"],
             "status": "unavailable", "last_checked_at": observed,
             "last_success_at": previous_source.get("last_success_at", ""), "error_code": "", "item_count": 0}
    items, errors = [], []
    def parse(payload, title=""):
        if payload["content"].lstrip().startswith(b"%PDF") or "application/pdf" in payload["content_type"].lower():
            return parse_pdf_document(payload["content"], source, url=payload["url"], collected_at=observed, title_hint=title)
        return parse_html_document(payload["content"].decode("utf-8", errors="replace"), source,
                                   url=payload["url"], collected_at=observed)
    try:
        root = _fetch(source["url"], source, fetcher)
        is_feed = re.search(br"<(?:rss|feed)[\s>]", root["content"][:1500], re.I)
        direct = None if is_feed else parse(root)
        if direct:
            items.append(direct)
        links = _discover_documents(root, source)
        for link in links[:max(0, limit - len(items))]:
            try:
                item = None
                if link.get("inline_html"):
                    # WordPress supplies its actual full article in the official
                    # feed. Keep the article URL and record the fetched feed too.
                    inline = BeautifulSoup(link["inline_html"], "html.parser")
                    paragraphs = [(f"p{i:03d}", node.get_text(" ", strip=True))
                                  for i, node in enumerate(inline.find_all("p"), 1)]
                    item = _make_item(source, link["url"], link["title"], paragraphs,
                                      link.get("published_at", ""), observed)
                    if item:
                        item["fetched_from"] = root["url"]
                if item is None:
                    item = parse(_fetch(link["url"], source, fetcher), link["title"])
                    if item and not item["published_at"] and link.get("published_at"):
                        item["published_at"] = link["published_at"]
                if item:
                    items.append(item)
            except CollectionError as error:
                errors.append(str(error))
            except requests.RequestException:
                errors.append("network_unavailable")
            except Exception:
                errors.append("parse_failed")
        if items:
            state.update(status="partial" if errors else "ok", last_success_at=observed,
                         item_count=len(items), error_code="some_documents_unavailable" if errors else "")
        else:
            state["error_code"] = errors[0] if errors else "no_business_documents"
    except CollectionError as error:
        state["error_code"] = str(error)
    except requests.Timeout:
        state["error_code"] = "timeout"
    except requests.RequestException:
        state["error_code"] = "network_unavailable"
    except Exception:
        state["error_code"] = "parse_failed"
    return state, items


def collect_media_insights(previous=None, *, fetcher=None, now=None, sources=None, max_documents=24):
    """Collect only configured official sources and preserve previous good items.

    A custom fetcher accepts one URL and returns HTML, bytes, a Response, or a
    dict containing content/html, final url and status. It never receives keys.
    This function makes no LLM calls and never writes a snapshot itself.
    """
    previous = previous if isinstance(previous, dict) else {}
    configured = list(SOURCES if sources is None else sources)
    observed = _timestamp(now)
    source_states = {row.get("id"): row for row in previous.get("sources", []) if isinstance(row, dict)}
    old_items = {row.get("id"): deepcopy(row) for row in previous.get("items", []) if isinstance(row, dict) and row.get("id")}
    limit = max(1, min(12, int(max_documents) // max(1, len(configured))))
    def collect(source):
        return _collect_source(source, source_states.get(source["id"], {}), fetcher=fetcher, observed=observed, limit=limit)
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(configured)))) as pool:
        results = list(pool.map(collect, configured))
    seen_urls = {row.get("url"): key for key, row in old_items.items()}
    for _, items in results:
        for item in items:
            old_id = seen_urls.get(item["url"])
            old = old_items.get(old_id or item["id"], {})
            if old and old.get("content_hash") == item["content_hash"]:
                merged = {**old, **item}
                # Collection time means when this version was first acquired;
                # repeated observations have their own last-checked timestamp.
                merged["collected_at"] = old.get("collected_at") or item["collected_at"]
                merged["published_at"] = item.get("published_at") or old.get("published_at", "")
                merged["last_checked_at"] = observed
                old_items[item["id"]] = merged
            else:
                item["last_checked_at"] = observed
                if old:
                    versions = deepcopy(old.get("versions", []))
                    versions.append({key: deepcopy(old.get(key)) for key in
                                     ("content_hash", "title", "published_at", "collected_at", "evidence", "analysis") if key in old})
                    item["versions"] = versions[-3:]
                old_items[item["id"]] = item
            seen_urls[item["url"]] = item["id"]
    retained, hashes = [], set()
    for row in sorted(old_items.values(), key=lambda row: (row.get("published_at", ""), row.get("collected_at", "")), reverse=True):
        fingerprint = row.get("content_hash") or row.get("url") or row.get("id")
        if fingerprint not in hashes:
            hashes.add(fingerprint)
            retained.append(row)
        if len(retained) >= MAX_ITEMS:
            break
    for state, _ in results:
        state["retained_item_count"] = sum(row.get("source_id") == state["id"] for row in retained)
    return {"schema_version": 1, "collected_at": observed, "sources": [state for state, _ in results], "items": retained}


def load_snapshot(path=SNAPSHOT_PATH):
    try:
        snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
        if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("items"), list) or not isinstance(snapshot.get("sources"), list):
            return {"schema_version": 1, "collected_at": "", "sources": [], "items": []}
        return snapshot
    except (OSError, ValueError, TypeError, AttributeError):
        return {"schema_version": 1, "collected_at": "", "sources": [], "items": []}


def save_snapshot(path, snapshot):
    """Atomically replace only the dedicated insight snapshot."""
    target = Path(path)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("items"), list) or not isinstance(snapshot.get("sources"), list):
        raise ValueError("invalid_media_insights_snapshot")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".media-insights-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
