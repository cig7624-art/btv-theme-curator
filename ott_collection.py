"""Collect visible, unauthenticated OTT home pages without inventing collections.

The extractor accepts a home heading and its title artwork, an explicit official
CollectionPage, or matched public home layout/catalog responses. Search results, marketing JSON-LD, arbitrary hydration JSON and
personal rows are deliberately not used as substitutes for a public home.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from ott_source_adapters import enrich_wavve_public_responses, extract_provider


PLATFORMS = (
    {"id": "netflix", "name": "NETFLIX", "url": "https://www.netflix.com/kr/"},
    {"id": "tving", "name": "TVING", "url": "https://www.tving.com/"},
    {"id": "coupang", "name": "쿠팡플레이", "url": "https://www.coupangplay.com/"},
    {"id": "disney", "name": "Disney+", "url": "https://www.disneyplus.com/ko-kr"},
    {"id": "wavve", "name": "wavve", "url": "https://www.wavve.com/"},
)
SNAPSHOT_PATH = Path("ott_home_snapshot.json")
MAX_SECTIONS = 10
MAX_WORKS = 8
MAX_PROMOTIONS = 5
MAX_HTML_BYTES = 12 * 1024 * 1024
TIMEOUT = (6, 18)
PERSONAL_RE = re.compile(r"이어\s*보기|시청\s*중|찜한|내\s*(?:리스트|보관함)|시청\s*기록|님을?\s*위한|continue watching|my list|because you watched", re.I)
IGNORE_HEADING_RE = re.compile(r"자주\s*묻는|고객\s*센터|이용\s*약관|개인\s*정보|FAQ|privacy|cookie|contact us", re.I)
CONTENT_PATHS = {
    "netflix": re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:title|watch)/[0-9]+", re.I),
    "tving": re.compile(r"/(?:contents|player|movie|series|program|live|clips|shorts)/[^/?#]+", re.I),
    "coupang": re.compile(r"/(?:play|titles?|contents?|movie|series)/[^/?#]+", re.I),
    "disney": re.compile(r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?(?:browse/entity-|movies/|series/|play/|video/)[^?#]+", re.I),
    "wavve": re.compile(r"/(?:player/(?:vod|movie|live)|vod|movie)(?:[/?]|$)", re.I),
}


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _observation_metadata():
    return {"observed_environment": "github_actions_global" if os.getenv("GITHUB_ACTIONS", "").lower() == "true" else "runtime", "country_verified": False, "locale_requested": "ko-KR"}


def _text(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def safe_http_url(value, base=""):
    value = str(value or "").strip()
    if not value or any(ord(c) < 32 for c in value):
        return ""
    try:
        parts = urlsplit(urljoin(base, value))
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            return ""
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        # Accessing .port also rejects malformed ports.
        _ = parts.port
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except (ValueError, TypeError):
        return ""


def _official_url(value, platform, base=""):
    url = safe_http_url(value, base or platform["url"])
    domain = urlsplit(platform["url"]).hostname.removeprefix("www.")
    host = (urlsplit(url).hostname or "").lower()
    return url if host == domain or host.endswith("." + domain) else ""


def _content_url(value, platform, base=""):
    url = _official_url(value, platform, base)
    return url if url and CONTENT_PATHS[platform["id"]].search(urlsplit(url).path) else ""


def _hidden(node):
    for parent in [node, *node.parents]:
        if not getattr(parent, "attrs", None):
            continue
        style = str(parent.get("style", "")).replace(" ", "").lower()
        if parent.has_attr("hidden") or parent.get("aria-hidden") == "true" or "display:none" in style or "visibility:hidden" in style:
            return True
    return False


def _art_url(img, base):
    for key in ("data-src", "data-original", "data-lazy-src", "src"):
        value = safe_http_url(img.get(key, ""), base)
        if value and not re.search(r"(?:blank|placeholder|transparent|loading)\.(?:gif|svg|png)(?:\?|$)", value, re.I):
            return value
    srcset = img.get("srcset") or img.get("data-srcset", "")
    return next((url for part in str(srcset).split(",") if (url := safe_http_url(part.strip().split(" ")[0], base))), "")


def _work_cards(region, platform, base):
    works, seen = [], set()
    for anchor in region.find_all("a", href=True):
        if _hidden(anchor):
            continue
        url = _content_url(anchor.get("href"), platform, base)
        img = anchor.find("img")
        if not url or img is None:
            continue
        title = _text(img.get("alt") or anchor.get("aria-label") or anchor.get("title"), 180)
        title = re.sub(r"(?:\s*포스터|\s*poster|\s*thumbnail|\s*썸네일)$", "", title, flags=re.I).strip()
        if not title or title.lower() in {"netflix", "tving", "wavve", "disney+", "logo", "이미지", "image"}:
            continue
        identity = urlsplit(url)._replace(query="", fragment="").geturl() if platform["id"] != "wavve" else url
        if identity in seen:
            continue
        seen.add(identity)
        works.append({"title": title, "url": url, "image_url": _art_url(img, base)})
    return works


def classify_section(title):
    if re.search(r"TOP\s*\d+|인기|순위|많이\s*보|지금\s*뜨|trending|popular", title, re.I):
        return "ranking"
    if re.search(r"신작|새로|새로운|신규|공개\s*예정|NEW|recently|coming soon", title, re.I):
        return "new"
    if re.search(r"오직|오리지널|originals?|독점", title, re.I):
        return "originals"
    if re.search(r"요일별|오늘의 편성|편성표", title):
        return "calendar"
    if re.search(r"포인트샵|개별구매", title):
        return "rental"
    if re.search(r"무료.*보기|무료 공개", title):
        return "free"
    if re.fullmatch(r"(?:영화|드라마|예능|애니메이션|액션|코미디|로맨스|스릴러|다큐멘터리|키즈|공포)(?:\s*(?:영화|드라마|추천))?", title):
        return "genre"
    return "theme"


def _home_regions(main, platform, base):
    """Keep heading/card relationships, source order and distinct collections."""
    rows, seen = [], set()
    for heading in main.select("h1,h2,h3,[role='heading'],.rowTitle,.section-title,.section_title"):
        if _hidden(heading):
            continue
        if any(re.search(r"hero|billboard|banner", " ".join(parent.get("class", [])), re.I) for parent in heading.parents if getattr(parent, "attrs", None)):
            continue
        title = _text(heading.get_text(" ", strip=True), 200)
        if not title or PERSONAL_RE.search(title) or IGNORE_HEADING_RE.search(title):
            continue
        region = heading.parent
        cards = []
        # The smallest ancestor containing this heading and multiple linked
        # artworks must not include another, unrelated home heading.
        for _ in range(5):
            if region is None or region == main or region.name in {"body", "html"}:
                break
            peer_headings = [h for h in region.select("h1,h2,h3,[role='heading'],.rowTitle,.section-title,.section_title") if not _hidden(h)]
            if any(h is not heading and _text(h.get_text(" ", strip=True)) != title for h in peer_headings):
                break
            cards = _work_cards(region, platform, base)
            if len(cards) >= 2:
                break
            region = region.parent
        if len(cards) < 2:
            continue
        link = next((a for a in heading.find_all("a", href=True) if _official_url(a["href"], platform, base)), None)
        if link is None:
            link = next((a for a in region.find_all("a", href=True) if re.search(r"전체|더\s*보|모두|view all|see all", a.get_text(" ", strip=True), re.I)), None)
        collection_url = _official_url(link.get("href"), platform, base) if link else ""
        key = (title.casefold(), collection_url)
        if key in seen:
            continue
        seen.add(key)
        desc_node = region.select_one(".description,.section-description,.section_description,[data-role='description']")
        rows.append({
            "id": hashlib.sha256((platform["id"] + "|" + title.casefold() + "|" + collection_url).encode()).hexdigest()[:16],
            "title": title, "description": _text(desc_node.get_text(" ", strip=True)) if desc_node else "",
            "kind": classify_section(title), "position": len(rows) + 1, "url": collection_url,
            "works": cards[:MAX_WORKS], "works_complete": False,
        })
        if len(rows) >= MAX_SECTIONS:
            break
    return rows


def _promotions(main, platform, base):
    result, seen = [], set()
    for hero in main.select("[class*='hero'],[class*='Hero'],[class*='billboard'],[class*='banner'],[class*='Banner']"):
        if _hidden(hero):
            continue
        for card in _work_cards(hero, platform, base):
            if card["url"] in seen:
                continue
            seen.add(card["url"])
            result.append({**card, "position": len(result) + 1})
            if len(result) >= MAX_PROMOTIONS:
                return result
    return result


def parse_public_home(html_text, platform, *, source_url="", checked_at="", public_responses=None):
    """Parse actual HTML, returning no rows when evidence is insufficient."""
    source_url = _official_url(source_url or platform["url"], platform)
    if not source_url:
        raise ValueError("unexpected_source_url")
    soup = BeautifulSoup(str(html_text)[:MAX_HTML_BYTES], "html.parser")
    adapted = extract_provider(soup, platform, source_url)
    if platform["id"] == "wavve":
        adapted = enrich_wavve_public_responses(adapted, public_responses, source_url)
    for node in soup.select("script,style,noscript,nav,footer,aside,[role='navigation'],[role='contentinfo']"):
        node.decompose()
    main = soup.select_one("main,[role='main'],#main,#content") or soup.body or soup
    sections = adapted["sections"] if adapted else _home_regions(main, platform, source_url)
    promotions = adapted["promotions"] if adapted else _promotions(main, platform, source_url)
    public_lists = adapted.get("public_lists", []) if adapted else []
    for section in sections:
        if section.get("kind") in {"theme", "other"}:
            section["kind"] = classify_section(section["title"])
    text = _text(main.get_text(" ", strip=True), 20000)
    signup = bool(main.select_one("input[type='email'],input[autocomplete='email']")) and bool(re.search(r"멤버십|가입|시작하기|구독|membership|sign up|subscribe", text, re.I))
    marketing_only = bool(adapted and adapted.get("marketing")) or (signup and (platform["id"] in {"netflix", "disney"} or not sections)) or (not sections and bool(re.search(r"구독하기|지금\s*가입|멤버십\s*가입|sign up|subscribe now", text, re.I)))
    # Marketing landing-page strips do not prove that the content home is public.
    if marketing_only:
        sections = []
    auth_page = bool(main.select_one("input[type='password']")) or bool(re.search(r"/(?:login|signin)(?:[/?]|$)", urlsplit(source_url).path, re.I))
    region_restricted = bool(re.search(r"not available in your (?:region|country)|서비스 지역이 아닙니다|해당 국가에서.*이용", text, re.I)) or urlsplit(source_url).path.rstrip("/").endswith("/not-available")
    status = "ok" if sections else "promotion_only" if promotions or public_lists or marketing_only else "unavailable"
    code = "" if status == "ok" else "public_home_unavailable" if status == "promotion_only" else "region_restricted" if region_restricted else "authentication_required" if auth_page else "no_rows"
    return {
        **platform, "source_url": source_url, "scope": "public_home", "status": status,
        **_observation_metadata(), "last_success_status": status if status != "unavailable" else "",
        "freshness": "current" if status != "unavailable" else "none", "checked_at": checked_at or utc_now_iso(),
        "last_success_at": (checked_at or utc_now_iso()) if status != "unavailable" else "",
        "complete": False, "capture_scope": ("공개 홍보 화면의 목록 · 확인한 항목 최대 8개" if marketing_only else "공개 홈에서 확인한 콘텐츠 영역 최대 10개 · 영역별 작품 최대 8편"),
        "promotions": promotions, "sections": sections, "public_lists": public_lists, "error_code": code,
        "access_mode": "public" if status != "unavailable" else "unconfirmed",
        "page_title": _text(soup.title.get_text()) if soup.title else "",
        "section_count": len(sections) if status == "ok" else None,
    }


def _http_fetch(platform):
    response = requests.get(platform["url"], timeout=TIMEOUT, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
        "Accept": "text/html,application/xhtml+xml",
    })
    response.raise_for_status()
    if len(response.content) > MAX_HTML_BYTES:
        raise ValueError("response_too_large")
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return {"html": response.text, "url": response.url, "http_status": response.status_code}


class PublicBrowser:
    """Optional public-browser fetcher. It never loads account storage or cookies."""

    def __init__(self, *, evidence_dir=None, executable_path=None):
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.executable_path = executable_path

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.driver = sync_playwright().start()
        kwargs = {"headless": True}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self.browser = self.driver.chromium.launch(**kwargs)
        return self

    def __exit__(self, *args):
        self.browser.close()
        self.driver.stop()

    def __call__(self, platform):
        context = self.browser.new_context(viewport={"width": 1440, "height": 1000}, locale="ko-KR", timezone_id="Asia/Seoul")
        try:
            page = context.new_page()
            public_responses = []
            pending_responses = []
            def observe(response):
                parts = urlsplit(response.url)
                if (platform["id"] == "wavve" and parts.hostname == "apis.wavve.com"
                        and parts.path in {"/v1/home", "/v1/catalog"} and response.ok
                        and len(pending_responses) < 40):
                    pending_responses.append(response)
            page.on("response", observe)
            # Preserve an explicitly localized public landing document when the
            # interactive marketing app would redirect it to another locale.
            if platform["id"] == "disney":
                try:
                    raw = _http_fetch(platform)
                    if "/ko-kr" in urlsplit(raw["url"]).path:
                        parsed = parse_public_home(raw["html"], platform, source_url=raw["url"])
                        if parsed.get("promotions") or parsed.get("public_lists"):
                            return raw
                except requests.RequestException:
                    pass
            response = page.goto(platform["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2200)
            if response and response.status >= 400:
                return {"html": "", "url": page.url, "http_status": response.status}
            # Trigger visible home lazy-loading without clicking titles, logging in,
            # accepting marketing offers or using any non-public API.
            for _ in range(7):
                page.mouse.wheel(0, 750)
                page.wait_for_timeout(450)
            page.evaluate("window.scrollTo(0,0)")
            for captured in pending_responses:
                try:
                    if len(captured.body()) > 2_000_000:
                        continue
                    parts = urlsplit(captured.url)
                    public_responses.append({"url_path": parts.path,
                        "catalog_code": parse_qs(parts.query).get("uicode", [""])[0],
                        "data": captured.json()})
                except Exception:
                    continue
            result = {"html": page.content(), "url": page.url, "http_status": response.status if response else 200, "public_responses": public_responses}
            if self.evidence_dir:
                self.evidence_dir.mkdir(parents=True, exist_ok=True)
                stem = self.evidence_dir / platform["id"]
                stem.with_suffix(".html").write_text(result["html"], encoding="utf-8")
                page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
                result["evidence_file"] = stem.name + ".png"
            return result
        finally:
            context.close()


def _unavailable(platform, checked_at, error_code):
    return {**platform, **_observation_metadata(), "source_url": platform["url"], "scope": "public_home", "status": "unavailable", "last_success_status": "",
            "freshness": "none", "checked_at": checked_at, "last_success_at": "", "complete": False,
            "promotions": [], "sections": [], "error_code": error_code, "access_mode": "unconfirmed", "section_count": None}


def collect_public_homes(previous=None, platform_ids=None, *, fetcher=None, use_browser=False, browser=None, now=None):
    """Refresh independently; failed attempts keep clearly dated previous material."""
    checked_at = now or utc_now_iso()
    if use_browser and browser is None and fetcher is None:
        with PublicBrowser() as active_browser:
            return collect_public_homes(previous, platform_ids, fetcher=active_browser, now=checked_at)
    old = {p["id"]: p for p in _clean_snapshot(previous).get("platforms", [])}
    wanted = set(platform_ids) if platform_ids else {p["id"] for p in PLATFORMS}
    chosen_fetcher = fetcher or browser or _http_fetch
    if fetcher is None and browser is None:
        responses = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            pending = {pool.submit(_http_fetch, dict(p)): p["id"] for p in PLATFORMS if p["id"] in wanted}
            for future in as_completed(pending):
                try:
                    responses[pending[future]] = future.result()
                except Exception as exc:
                    responses[pending[future]] = exc
        def chosen_fetcher(platform):
            response = responses[platform["id"]]
            if isinstance(response, Exception):
                raise response
            return response
    results = []
    for platform in PLATFORMS:
        if platform["id"] not in wanted:
            results.append(old.get(platform["id"], _unavailable(platform, "", "not_collected")))
            continue
        try:
            response = chosen_fetcher(dict(platform))
            if isinstance(response, str):
                response = {"html": response, "url": platform["url"], "http_status": 200}
            if int(response.get("http_status", 200)) >= 400:
                entry = _unavailable(platform, checked_at, "http_error")
                entry["http_status"] = int(response["http_status"])
            else:
                entry = parse_public_home(response["html"], platform, source_url=response.get("url", platform["url"]), checked_at=checked_at, public_responses=response.get("public_responses"))
                if response.get("evidence_file"):
                    entry["evidence_file"] = Path(response["evidence_file"]).name
        except requests.exceptions.ProxyError:
            entry = _unavailable(platform, checked_at, "network_blocked")
        except requests.exceptions.HTTPError as exc:
            entry = _unavailable(platform, checked_at, "http_error")
            entry["http_status"] = getattr(exc.response, "status_code", None)
        except (requests.exceptions.RequestException, TimeoutError):
            entry = _unavailable(platform, checked_at, "network_error")
        except Exception as exc:
            # Error classes are enough for diagnostics; arbitrary exception text
            # can contain URL credentials or machine-local paths.
            code = "network_error" if "Timeout" in type(exc).__name__ or "net::ERR_" in str(exc) else "parse_error"
            entry = _unavailable(platform, checked_at, code)
        previous_entry = old.get(platform["id"], {})
        if entry["status"] == "unavailable" and previous_entry.get("last_success_at"):
            for key in ("promotions", "sections", "public_lists", "last_success_at", "last_success_status", "source_url", "capture_scope", "page_title", "evidence_file", "observed_environment", "country_verified", "locale_requested"):
                if key in previous_entry:
                    entry[key] = previous_entry[key]
            entry["freshness"] = "stale"
            entry["complete"] = False
            entry["attempt_environment"] = _observation_metadata()["observed_environment"]
        results.append(entry)
    return {"schema_version": 1, "updated_at": checked_at, **_observation_metadata(), "platforms": results}


def _clean_snapshot(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("platforms"), list):
        return {"schema_version": 1, "updated_at": "", "platforms": []}
    valid, seen = [], set()
    by_id = {p["id"]: p for p in PLATFORMS}
    for raw in data["platforms"]:
        if not isinstance(raw, dict) or raw.get("id") not in by_id or raw.get("id") in seen or raw.get("status") not in {"ok", "promotion_only", "unavailable"}:
            continue
        seen.add(raw["id"])
        spec = by_id[raw["id"]]
        entry = {**raw, **spec, "source_url": _official_url(raw.get("source_url", spec["url"]), spec) or spec["url"]}
        for key in ("checked_at", "last_success_at", "capture_scope", "error_code", "page_title"):
            entry[key] = _text(raw.get(key), 500 if key in {"capture_scope", "page_title"} else 80)
        entry["scope"] = "public_home"
        entry["complete"] = bool(raw.get("complete") is True)
        entry["freshness"] = raw.get("freshness") if raw.get("freshness") in {"current", "stale", "none"} else "none"
        entry["access_mode"] = raw.get("access_mode") if raw.get("access_mode") in {"public", "unconfirmed"} else "unconfirmed"
        entry["observed_environment"] = _text(raw.get("observed_environment"), 80) or "unknown"
        entry["country_verified"] = raw.get("country_verified") is True
        entry["locale_requested"] = _text(raw.get("locale_requested"), 20)
        entry["last_success_status"] = raw.get("last_success_status") if raw.get("last_success_status") in {"ok", "promotion_only"} else (raw["status"] if raw["status"] in {"ok", "promotion_only"} and entry["last_success_at"] else "")
        entry["promotions"], entry["sections"], entry["public_lists"] = [], [], []
        def position(item, fallback):
            try:
                number = int(item.get("position", fallback))
                return number if 0 < number <= 1000 else fallback
            except (TypeError, ValueError, OverflowError):
                return fallback
        for promo in raw.get("promotions", [])[:MAX_PROMOTIONS] if isinstance(raw.get("promotions"), list) else []:
            if not isinstance(promo, dict) or not _text(promo.get("title")):
                continue
            entry["promotions"].append({"title": _text(promo["title"], 180), "url": _content_url(promo.get("url"), spec), "image_url": safe_http_url(promo.get("image_url")), "position": position(promo, len(entry["promotions"])+1), "placement": _text(promo.get("placement"), 100)})
        for field in ("sections", "public_lists"):
            for row in raw.get(field, [])[:MAX_SECTIONS] if isinstance(raw.get(field), list) else []:
                if not isinstance(row, dict) or not _text(row.get("title")):
                    continue
                works = []
                items_kind = "collections" if row.get("items_kind") == "collections" else "works"
                for work in row.get("works", [])[:MAX_WORKS] if isinstance(row.get("works"), list) else []:
                    if isinstance(work, dict) and _text(work.get("title")):
                        target = _official_url(work.get("url"), spec) if items_kind == "collections" else _content_url(work.get("url"), spec)
                        works.append({"title": _text(work["title"], 180), "url": target if work.get("url") else "", "image_url": safe_http_url(work.get("image_url"))})
                entry[field].append({"id": _text(row.get("id"), 80), "title": _text(row["title"], 200), "description": _text(row.get("description")), "position": position(row, len(entry[field])+1), "kind": row.get("kind") if row.get("kind") in {"theme", "new", "ranking", "genre", "live", "clips", "shorts", "promotion", "collection", "originals", "calendar", "rental", "free", "other"} else "other", "url": _official_url(row.get("url"), spec) if row.get("url") else "", "works": works, "works_complete": False, "items_kind": items_kind, "placement": _text(row.get("placement"), 100), "works_status": _text(row.get("works_status"), 40)})
        entry["section_count"] = len(entry["sections"]) if entry["status"] == "ok" else None
        valid.append(entry)
    return {"schema_version": 1, "updated_at": _text(data.get("updated_at"), 80), "observed_environment": _text(data.get("observed_environment"), 80) or "unknown", "country_verified": data.get("country_verified") is True, "locale_requested": _text(data.get("locale_requested"), 20), "platforms": valid}


def load_snapshot(path=SNAPSHOT_PATH):
    try:
        if Path(path).stat().st_size > 5 * 1024 * 1024:
            return {"schema_version": 1, "updated_at": "", "platforms": []}
        return _clean_snapshot(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return {"schema_version": 1, "updated_at": "", "platforms": []}


def save_snapshot(snapshot, path=SNAPSHOT_PATH):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_clean_snapshot(snapshot), ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
