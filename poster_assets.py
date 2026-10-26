"""Resolve real artwork for an explicitly requested batch of content candidates.

No lookup belongs in a normal Streamlit rerun. Call ``enrich_poster_candidates``
when the user loads or refreshes content, then persist its returned poster URLs.
Only exact work titles (and supplied release years) are accepted. A missing
poster stays missing instead of being filled with unrelated artwork.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from functools import lru_cache
from io import BytesIO
import re
from threading import Lock
import time
import unicodedata
from urllib.parse import quote, unquote, urlparse

import requests
from PIL import Image, UnidentifiedImageError


REQUEST_TIMEOUT = 6
MAX_POSTER_BYTES = 8 * 1024 * 1024
MAX_POSTER_PIXELS = 32 * 1024 * 1024
_ITUNES_URL = "https://itunes.apple.com/search"
_USER_AGENT = "BtvThemeCurator/1.0 (https://github.com/cig7624-art/btv-theme-curator; public poster metadata)"
_WIKIPEDIA_INTERVAL = .5
_WIKIPEDIA_RETRY_WAIT = 3
_WIKIPEDIA_HOSTS_LOCK = Lock()
_WIKIPEDIA_HOSTS = {}


class _IncompleteImageCheck(requests.RequestException):
    """A bounded probe cannot establish whether the image is usable."""


def _retry_after(response):
    """Return the server's delay, with a short default for missing headers."""
    value = _text(response.headers.get("Retry-After"))
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 1


def _wikipedia_get(language, params):
    host = f"{language}.wikipedia.org"
    with _WIKIPEDIA_HOSTS_LOCK:
        state = _WIKIPEDIA_HOSTS.setdefault(host, {"lock": Lock(), "next": 0})
    for attempt in range(2):
        # Hold the host lock through the request so four artwork workers cannot
        # burst requests at Wikipedia. Other providers/hosts stay independent.
        with state["lock"]:
            delay = max(0, state["next"] - time.monotonic())
            if delay > _WIKIPEDIA_RETRY_WAIT:
                raise requests.exceptions.RetryError("Wikipedia Retry-After exceeds the bounded wait")
            if delay:
                time.sleep(delay)
            state["next"] = time.monotonic() + _WIKIPEDIA_INTERVAL
            response = requests.get(
                f"https://{host}/w/api.php", params=params,
                headers={"User-Agent": _USER_AGENT}, timeout=REQUEST_TIMEOUT,
            )
            if response.status_code in {429, 503}:
                retry_delay = _retry_after(response)
                state["next"] = max(state["next"], time.monotonic() + retry_delay)
                if attempt == 0 and retry_delay <= _WIKIPEDIA_RETRY_WAIT:
                    continue
            response.raise_for_status()
            return response


def _text(value):
    return "" if value is None else re.sub(r"\s+", " ", str(value)).strip()


def _key(value):
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", _text(value)).lower())


def _url(value, *, domains=()):
    value = _text(value)
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return ""
        if domains and not any(host == domain or host.endswith("." + domain) for domain in domains):
            return ""
        return value
    except ValueError:
        return ""


def _year(value):
    match = re.search(r"(?:19|20)\d{2}", _text(value))
    return match.group() if match else ""


def _is_tv(content_type):
    return bool(re.search(r"드라마|예능|시리즈|\btv\b|series", _text(content_type), re.I))


def _get_with_retry(url, **kwargs):
    """Retry one transient request; never wait through a long provider outage."""
    for attempt in range(2):
        try:
            response = requests.get(url, headers={"User-Agent": _USER_AGENT},
                                    timeout=REQUEST_TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt:
                raise
            time.sleep(.25)
            continue
        if response.status_code in {429, 500, 502, 503, 504} and not attempt:
            delay = _retry_after(response)
            if delay <= _WIKIPEDIA_RETRY_WAIT:
                response.close()
                time.sleep(delay)
                continue
        return response


def _obvious_nonposter(url):
    # Reject explicit cast/event photographs already present in old saved data.
    # A filename without "poster" remains eligible: many genuine posters omit it.
    filename = unquote(urlparse(url).path).rsplit("/", 1)[-1]
    return bool(re.search(r"cast[ _-]*photo|headshot|signature|(?:Lumi[èe]res|Cannes|Berlinale)[ _-]*20\d{2}", filename, re.I))


def is_poster_url(url):
    """Pure render-time guard; actual image downloads belong to explicit loads."""
    return bool(_url(url)) and not _obvious_nonposter(_url(url))


@lru_cache(maxsize=2048)
def _cached_image_health(url, quarter_hour):
    response = _get_with_retry(url, stream=True)
    try:
        if response.status_code in {404, 410}:
            return {"status": "invalid", "reason": f"http_{response.status_code}"}
        # Authentication, hotlink blocks and outages do not prove a saved image
        # has disappeared. Raised exceptions are deliberately never cached.
        response.raise_for_status()
        media_type = _text(response.headers.get("Content-Type")).split(";", 1)[0].lower()
        if not media_type.startswith("image/"):
            return {"status": "invalid", "reason": "not_an_image"}
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_POSTER_BYTES:
                raise _IncompleteImageCheck("image_exceeds_probe_budget")
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width * height > MAX_POSTER_PIXELS:
                    raise _IncompleteImageCheck("image_exceeds_pixel_budget")
                image.verify()
            # JPEG verify() checks headers only. Decode pixels too so truncated
            # files cannot be persisted as healthy posters.
            with Image.open(BytesIO(data)) as image:
                image.load()
            if min(width, height) < 48:
                return {"status": "invalid", "reason": "image_too_small"}
        except _IncompleteImageCheck:
            raise
        except Image.DecompressionBombError as error:
            raise _IncompleteImageCheck("image_exceeds_pixel_budget") from error
        except (UnidentifiedImageError, OSError, ValueError):
            return {"status": "invalid", "reason": "undecodable_image"}
        return {"status": "available", "reason": "decoded_image", "width": width, "height": height}
    finally:
        response.close()


def inspect_poster_url(url):
    """Check actual image bytes on an explicit load/repair, never a page rerun.

    Network failures are retryable, not proof that existing artwork is invalid.
    Successful and definite-invalid probes share a bounded 15 minute cache.
    """
    url = _url(url)
    if not url:
        return {"status": "invalid", "reason": "invalid_url"}
    if _obvious_nonposter(url):
        return {"status": "invalid", "reason": "cast_or_event_photo"}
    try:
        return dict(_cached_image_health(url, int(time.time() // 900)))
    except requests.RequestException as error:
        return {"status": "retryable", "reason": str(error) if isinstance(error, _IncompleteImageCheck) else type(error).__name__}


def _verified_artwork(artwork):
    health = inspect_poster_url(artwork.get("poster_url"))
    if health["status"] == "retryable":
        raise requests.ConnectionError("Poster image check is temporarily unavailable")
    return artwork if health["status"] == "available" else {}


def _itunes_poster(title, year, content_type, *, country="KR"):
    # Movie artwork is never used for a TV series with the same title.
    if _is_tv(content_type):
        return {}
    response = _get_with_retry(
        _ITUNES_URL,
        params={"term": title, "country": country, "media": "movie", "entity": "movie", "limit": 12, "lang": "ko_kr" if country == "KR" else "en_us"},
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results", []) if isinstance(payload, dict) else []
    matches = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or item.get("kind") != "feature-movie":
            continue
        if _key(item.get("trackName")) != _key(title):
            continue
        if year and _year(item.get("releaseDate")) != year:
            continue
        # Use only artwork URLs actually returned by the provider. Do not invent
        # larger URL variants or infer a poster from a search-result thumbnail.
        posters = list(dict.fromkeys(_url(item.get(field), domains=("mzstatic.com",))
                       for field in ("artworkUrl600", "artworkUrl512", "artworkUrl100")
                       if _url(item.get(field), domains=("mzstatic.com",))))
        source = _url(item.get("trackViewUrl"), domains=("apple.com",))
        if posters:
            identity = (_year(item.get("releaseDate")), _text(item.get("trackId")) or source)
            matches.append((identity, [{"poster_url": poster, "poster_source": "Apple 영화", "poster_source_url": source} for poster in posters]))
    # An exact title alone cannot disambiguate a remake. With no requested year,
    # abstain when the provider returns distinct matching film identities.
    if not year and len({identity for identity, _ in matches}) > 1:
        return {}
    unavailable = None
    for _, artworks in matches:
        for artwork in artworks:
            try:
                if verified := _verified_artwork(artwork):
                    return verified
            except requests.RequestException as error:
                unavailable = error
    if unavailable is not None:
        raise unavailable
    return {}


def _english_work_title(title):
    # Preserve meaningful parenthetical title text; remove media qualifiers only.
    return re.sub(r"\s*\((?=[^()]*\b(?:TV series|TV program|television series|film|television program|game show|reality show)\b)[^()]+\)$", "", _text(title), flags=re.I)


def _tvmaze_poster(verified_english_title, year):
    """Resolve only a verified Wikipedia title bridge with an exact premiere."""
    if not year:
        return {}
    title = _english_work_title(verified_english_title)
    response = _get_with_retry("https://api.tvmaze.com/search/shows", params={"q": title})
    response.raise_for_status()
    payload = response.json()
    matches = {}
    for result in payload if isinstance(payload, list) else []:
        show = result.get("show", {}) if isinstance(result, dict) else {}
        if not isinstance(show, dict) or _key(show.get("name")) != _key(title) or _year(show.get("premiered")) != year:
            continue
        if _text(show.get("type")).lower() not in {"scripted", "reality", "game show", "talk show", "documentary", "animation", "variety", "panel show", "award show", "sports"}:
            continue
        identifier = show.get("id")
        if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0:
            matches[identifier] = show
    # A same-title/same-year collision is still ambiguous, even if only one
    # result has artwork. Never choose the first search result by score.
    if len(matches) != 1:
        return {}
    show = next(iter(matches.values()))
    image = show.get("image")
    if not isinstance(image, dict):
        return {}
    unavailable = None
    for field in ("original", "medium"):
        poster = _url(image.get(field), domains=("tvmaze.com",))
        if not poster:
            continue
        health = inspect_poster_url(poster)
        if health["status"] == "retryable":
            unavailable = requests.ConnectionError("TVmaze poster image is temporarily unavailable")
            continue
        width, height = health.get("width", 0), health.get("height", 0)
        if health["status"] == "available" and height and .45 <= width / height <= .85:
            return {"poster_url": poster, "poster_source": "TVmaze", "poster_source_url": _url(show.get("url"), domains=("tvmaze.com",))}
    if unavailable is not None:
        raise unavailable
    return {}


def _wikipedia_pages(language, titles, *, english_links=False, search=False):
    params = {
        "action": "query", "format": "json", "redirects": 1, "titles": "|".join(titles),
        "prop": "pageimages|pageprops|extracts|categories" + ("|langlinks" if english_links else ""),
        "piprop": "original|name", "pilicense": "any", "exintro": 1,
        "explaintext": 1, "exsentences": 3, "cllimit": 50,
    }
    if english_links:
        # lllimit applies to the whole batch, not each page. Disambiguation or
        # missing-title alternatives must not consume the media page's link.
        params.update(lllang="en", lllimit=10)
    if search:
        # Search finds canonical spelling/qualifiers only. Its returned pages
        # still have to pass the exact normalized title, year and media checks.
        query = _text(titles[0]).replace('"', " ")
        params.pop("titles")
        params.update(generator="search", gsrsearch=f'intitle:"{query}"',
                      gsrnamespace=0, gsrlimit=5)
    response = _wikipedia_get(language, params)
    payload = response.json()
    query = payload.get("query", {}) if isinstance(payload, dict) else {}
    if not isinstance(query, dict):
        return []
    pages = query.get("pages", {}) if isinstance(query, dict) else {}
    pages = [dict(page) for page in pages.values() if isinstance(page, dict)] if isinstance(pages, dict) else []
    if search:
        return pages
    # Only the server's explicit normalization/redirect chain can bridge a
    # requested alias to a differently named canonical work. Search hits and
    # unrelated aliases in a response never establish that identity.
    edges = {}
    for field in ("normalized", "redirects"):
        for mapping in query.get(field, []):
            if not isinstance(mapping, dict) or mapping.get("tofragment"):
                continue
            source, target = _text(mapping.get("from")), _text(mapping.get("to"))
            if source and target:
                edges.setdefault(source, set()).add(target)
    for page in pages:
        aliases = []
        for requested in titles:
            current, seen = _text(requested), set()
            for _ in range(8):
                if current in seen or len(edges.get(current, ())) != 1:
                    break
                seen.add(current)
                current = next(iter(edges[current]))
                if current == _text(page.get("title")):
                    aliases.append(_text(requested))
                    break
        if aliases:
            page["_requested_aliases"] = aliases
    return pages


def _wikipedia_release_years(page, tv):
    """Use release/debut categories, never a story's setting or a decade."""
    years = set()
    for category in page.get("categories", []):
        label = _text(category.get("title")) if isinstance(category, dict) else ""
        match = re.match(r"^(?:Category|분류):\s*((?:19|20)\d{2})(?=\s|년|$)(.*)$", label, re.I)
        if not match:
            continue
        tail = match.group(2)
        release = (bool(re.search(r"television series debuts|TV series debuts|년에?\s*(?:시작한|방영을\s*시작한)", tail, re.I))
                   or bool(re.fullmatch(r"년\s*(?:드라마|텔레비전\s*드라마)", tail))) if tv else bool(re.fullmatch(r"\s*films?\s*|년\s*영화", tail, re.I))
        if release:
            years.add(match.group(1))
    return years


def _verified_wikipedia_page(page, requested_titles, year, tv):
    if not isinstance(page, dict) or "missing" in page or "disambiguation" in page.get("pageprops", {}):
        return False
    page_title = _text(page.get("title"))
    base_title = re.sub(r"\s*\([^()]+\)$", "", page_title)
    page_titles = [base_title, *page.get("_requested_aliases", [])]
    if not ({_key(re.sub(r"\s*\([^()]+\)$", "", title)) for title in page_titles}
            & {_key(re.sub(r"\s*\([^()]+\)$", "", title)) for title in requested_titles}):
        return False
    qualifier = re.search(r"\(([^()]*)\)$", page_title)
    qualifier = qualifier.group(1) if qualifier else ""
    nonmedia = r"소설|웹툰|만화|\b(?:novels?|books?|comics?|webtoons?|manga|manhwa)\b"
    if re.search(nonmedia, qualifier, re.I):
        return False
    # A novel article can also carry categories for its planned TV adaptation.
    # The original work's explicit lead identity must not become TV artwork.
    lead = _text(page.get("extract")).split(".", 1)[0]
    if re.search(r"(?:소설|웹툰|만화)(?:\s*시리즈)?(?:이다|이며|이고|다)(?:\s|$)", lead):
        return False
    article_year = _year(qualifier)
    if year and article_year and article_year != year:
        return False
    title_tv = bool(re.search(r"드라마|텔레비전|television|TV series", qualifier, re.I))
    title_film = bool(re.search(r"영화|\bfilm\b", qualifier, re.I))
    if (title_tv and not tv) or (title_film and tv):
        return False
    category_labels = [_text(item.get("title")) for item in page.get("categories", []) if isinstance(item, dict)]
    categories = " ".join(category_labels)
    media_categories = [label for label in category_labels
                                if not re.search(r"(?:소설|웹툰|만화|novels?|books?|comics?|webtoons?|manga|manhwa)\s*$", label, re.I)
                                and not re.search(r"\b(?:novels?|books?|comics?|webtoons?)\b.*\badapted (?:into|for)\b", label, re.I)]
    kind_pattern = r"드라마(?!\s*영화)|텔레비전|television|TV series" if tv else r"영화\s*$|\bfilms?\s*$"
    if not any(re.search(kind_pattern, text, re.I) for text in [qualifier, *media_categories]):
        return False
    release_years = _wikipedia_release_years(page, tv)
    # A sequel/next season's year in the intro cannot override an explicitly
    # classified release or first-broadcast year for this article's work.
    if year and release_years and year not in release_years:
        return False
    # Premiere/release categories can establish the year when a short intro
    # omits it. A year mentioned in an actor biography cannot establish identity.
    if year and not re.search(rf"\b{re.escape(year)}(?:년|\b)", " ".join((_text(page.get("extract")), categories))):
        return False
    return True


def _infobox_poster(language, page):
    """Read the verified media article's actual artwork slot, not a filename guess."""
    from bs4 import BeautifulSoup

    params = {"action": "parse", "format": "json", "prop": "text", "disableeditsection": 1}
    if isinstance(page.get("pageid"), int) and page["pageid"] > 0:
        params["pageid"] = page["pageid"]
    else:
        params["page"] = _text(page.get("title"))
    response = _wikipedia_get(language, params)
    payload = response.json()
    parsed = payload.get("parse", {}) if isinstance(payload, dict) else {}
    if not isinstance(parsed, dict):
        return ""
    if parsed.get("title") and _key(parsed["title"]) != _key(page.get("title")):
        return ""
    markup = parsed.get("text", {})
    markup = markup.get("*", "") if isinstance(markup, dict) else markup
    if not isinstance(markup, str) or not markup:
        return ""
    soup = BeautifulSoup(markup, "html.parser")
    box = soup.select_one("table.infobox")
    if box is None:
        return ""
    # The main artwork cell is distinct from cast photos and unrelated images
    # elsewhere in the article. Korean infoboxes often use a colspan cell.
    images = box.select(".infobox-image img") or box.select("tr td[colspan] img")
    for image in images[:2]:
        slot = image.find_parent("td")
        caption = slot.get_text(" ", strip=True) if slot is not None else ""
        caption += " " + " ".join(item.get_text(" ", strip=True) for item in box.select(".infobox-caption"))
        hint = " ".join((_text(image.get("alt")), _text(image.get("src")), caption))
        if re.search(r"logo|로고|cast photo|출연진|배우.{0,5}사진|인물.{0,5}사진|headshot|portrait|signature|book cover|원작.{0,5}표지", hint, re.I):
            continue
        try:
            width, height = float(image.get("width", 0)), float(image.get("height", 0))
            portrait = width > 0 and height > 0 and .45 <= width / height <= .85
        except (TypeError, ValueError):
            portrait = False
        explicit_poster = bool(re.search(r"poster|포스터|promotional", hint, re.I))
        if not portrait and not explicit_poster:
            continue
        raw = _text(image.get("src"))
        poster = _url("https:" + raw if raw.startswith("//") else raw, domains=("wikimedia.org", "wikipedia.org"))
        if poster:
            return poster
    return ""


def _wikipedia_artwork(language, page):
    original = page.get("original", {})
    poster = _url(original.get("source") if isinstance(original, dict) else "", domains=("wikimedia.org", "wikipedia.org"))
    source = f"https://{language}.wikipedia.org/wiki/" + quote(_text(page.get("title")).replace(" ", "_"))
    unavailable = None
    if poster and re.search(r"poster|포스터", _text(page.get("pageimage")), re.I):
        try:
            artwork = _verified_artwork({"poster_url": poster, "poster_source": "위키백과", "poster_source_url": source})
            if artwork:
                return artwork
        except requests.RequestException as error:
            unavailable = error
    # PageImages may point at a removed file or at a cast image. A verified
    # article's actual infobox can still supply a working poster for that work.
    try:
        poster = _infobox_poster(language, page)
    except requests.RequestException as error:
        unavailable, poster = error, ""
    except (ValueError, TypeError, KeyError):
        poster = ""
    if not poster:
        if unavailable is not None:
            raise unavailable
        return {}
    return _verified_artwork({"poster_url": poster, "poster_source": "위키백과", "poster_source_url": source})


def _wikipedia_poster(title, year, content_type):
    korean = bool(re.search(r"[가-힣]", title))
    language = "ko" if korean else "en"
    tv = _is_tv(content_type)
    if korean:
        suffixes = ((([f"{year}년 드라마"] if year else []) + ["드라마", "텔레비전 프로그램"])
                    if tv else ([f"{year}년 영화"] if year else []) + ["영화"])
    else:
        suffixes = (["TV series", "television series"] if tv else ([f"{year} film"] if year else []) + ["film"])
    titles = [f"{title} ({suffix})" for suffix in suffixes] + [title]
    pages = [page for page in _wikipedia_pages(language, titles, english_links=korean)
             if _verified_wikipedia_page(page, [title], year, tv)]
    if not pages:
        # One bounded search request repairs spacing/punctuation and documented
        # qualifiers. Missing artwork on an already verified page is not a
        # reason to broaden the work's identity or keep searching.
        pages = [page for page in _wikipedia_pages(language, [title], english_links=korean, search=True)
                 if _verified_wikipedia_page(page, [title], year, tv)]
        if len({(_text(page.get("pageid")), _text(page.get("title"))) for page in pages}) > 1:
            return {}
    if not year and len({_text(page.get("title")) for page in pages}) > 1:
        return {}
    unavailable = None
    for page in pages:
        try:
            artwork = _wikipedia_artwork(language, page)
            if artwork:
                return artwork
        except requests.RequestException as error:
            unavailable = error
    if not korean:
        if unavailable is not None:
            raise unavailable
        return {}
    # A Korean article's declared language link is an identity bridge. Do not
    # translate or guess English titles, which can map to a different work.
    english_titles = list(dict.fromkeys(
        _text(link.get("*")) for page in pages for link in page.get("langlinks", [])
        if isinstance(link, dict) and link.get("lang") == "en" and _text(link.get("*")) and "|" not in _text(link.get("*"))
    ))
    if not english_titles:
        if unavailable is not None:
            raise unavailable
        return {}
    english = [page for page in _wikipedia_pages("en", english_titles)
               if _verified_wikipedia_page(page, english_titles, year, tv)]
    if len({_text(page.get("title")) for page in english}) > 1:
        return {}
    for page in english:
        try:
            artwork = _wikipedia_artwork("en", page)
            if artwork:
                return artwork
        except requests.RequestException as error:
            unavailable = error
    # This bridge comes only from the verified Korean work's declared language
    # link and an English page that retained its year and media identity.
    if year:
        for page in english:
            try:
                base_title = _english_work_title(page.get("title"))
                artwork = (_tvmaze_poster(page["title"], year) if tv
                           else _itunes_poster(base_title, year, content_type, country="US"))
                if artwork:
                    return artwork
            except requests.RequestException as error:
                unavailable = error
    if unavailable is not None:
        raise unavailable
    return {}


@lru_cache(maxsize=2048)
def _cached_poster(title, year, content_type, hour_bucket):
    unavailable = None
    for provider in (_itunes_poster, _wikipedia_poster):
        try:
            poster = provider(title, year, content_type)
            if poster:
                return poster
        except requests.RequestException as error:
            unavailable = error
        except (ValueError, TypeError, KeyError):
            continue
    # lru_cache does not cache raised exceptions. An outage/rate limit must not
    # turn into an hour-long "no artwork" result; a confirmed empty match can.
    if unavailable is not None:
        raise unavailable
    return {}


def _poster_for_refresh(identity, bucket):
    artwork = _cached_poster(*identity, bucket)
    if not artwork:
        return {}
    verified = _verified_artwork(artwork)
    if verified:
        return verified
    # Metadata can be cached for an hour while a provider removes an image.
    # Re-run identity verification instead of re-saving that dead cached URL.
    return _cached_poster.__wrapped__(*identity, bucket)


def enrich_poster_candidates(contents, *, max_workers=4, limit=100, repair_existing=False):
    """Return copies with genuine posters, using at most ``limit`` unique lookups.

    Existing artwork and all candidate identity/status fields are preserved.
    ``repair_existing`` verifies saved image bytes too, replacing definite bad
    images only; an outage leaves their saved addresses intact for another try.
    Successful and confirmed empty lookups are cached for the current hour.
    Transient provider failures remain retryable on the next explicit refresh,
    which can fill older Kinolights-cache entries whose poster fields are blank.
    """
    output = [dict(item) for item in contents if isinstance(item, dict)]
    health = {}
    if repair_existing:
        urls = list(dict.fromkeys(_url(item.get("poster_url")) for item in output if _url(item.get("poster_url"))))
        if urls:
            with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(urls), 8))) as executor:
                health = dict(zip(urls, executor.map(inspect_poster_url, urls)))
    pending = {}
    for index, item in enumerate(output):
        original = _url(item.get("poster_url"))
        if original and (not repair_existing or health[original]["status"] != "invalid"):
            continue
        item["poster_url"] = ""
        title = _text(item.get("title"))
        if not title:
            continue
        identity = (title, _year(item.get("year")), _text(item.get("content_type") or item.get("type")))
        if identity not in pending and len(pending) >= max(0, int(limit)):
            continue
        pending.setdefault(identity, []).append(index)
    if not pending:
        return output
    bucket = int(time.time() // 3600)
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), len(pending), 8))) as executor:
        futures = {executor.submit(_poster_for_refresh, identity, bucket): indices for identity, indices in pending.items()}
        for future in as_completed(futures):
            try:
                artwork = future.result()
            except Exception:
                artwork = {}
            if artwork:
                for index in futures[future]:
                    output[index].update(artwork)
    return output
