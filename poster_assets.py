"""Resolve real artwork for an explicitly requested batch of content candidates.

No lookup belongs in a normal Streamlit rerun. Call ``enrich_poster_candidates``
when the user loads or refreshes content, then persist its returned poster URLs.
Only exact work titles (and supplied release years) are accepted. A missing
poster stays missing instead of being filled with unrelated artwork.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import re
import time
import unicodedata
from urllib.parse import quote, urlparse

import requests


REQUEST_TIMEOUT = 4
_ITUNES_URL = "https://itunes.apple.com/search"
_USER_AGENT = "BtvThemeCurator/1.0 (film-poster metadata lookup)"


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


def _itunes_poster(title, year, content_type):
    # Movie artwork is never used for a TV series with the same title.
    if _is_tv(content_type):
        return {}
    response = requests.get(
        _ITUNES_URL,
        params={"term": title, "country": "KR", "media": "movie", "entity": "movie", "limit": 12, "lang": "ko_kr"},
        headers={"User-Agent": _USER_AGENT}, timeout=REQUEST_TIMEOUT,
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
        poster = next((_url(item.get(field), domains=("mzstatic.com",))
                       for field in ("artworkUrl600", "artworkUrl512", "artworkUrl100")
                       if _url(item.get(field), domains=("mzstatic.com",))), "")
        source = _url(item.get("trackViewUrl"), domains=("apple.com",))
        if poster:
            identity = (_year(item.get("releaseDate")), _text(item.get("trackId")) or source)
            matches.append((identity, {"poster_url": poster, "poster_source": "Apple 영화", "poster_source_url": source}))
    # An exact title alone cannot disambiguate a remake. With no requested year,
    # abstain when the provider returns distinct matching film identities.
    if not year and len({identity for identity, _ in matches}) > 1:
        return {}
    return matches[0][1] if matches else {}


def _wikipedia_poster(title, year, content_type):
    korean = bool(re.search(r"[가-힣]", title))
    language = "ko" if korean else "en"
    tv = _is_tv(content_type)
    if korean:
        suffixes = (["드라마", "텔레비전 프로그램"] if tv else ([f"{year}년 영화"] if year else []) + ["영화"])
    else:
        suffixes = (["TV series", "television series"] if tv else ([f"{year} film"] if year else []) + ["film"])
    titles = [f"{title} ({suffix})" for suffix in suffixes] + [title]
    response = requests.get(
        f"https://{language}.wikipedia.org/w/api.php",
        params={"action": "query", "format": "json", "redirects": 1, "titles": "|".join(titles),
                "prop": "pageimages|pageprops|extracts|categories", "piprop": "original|name", "pilicense": "any",
                "exintro": 1, "explaintext": 1, "exsentences": 2, "cllimit": 30},
        headers={"User-Agent": _USER_AGENT}, timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return {}
    matches = []
    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page or "disambiguation" in page.get("pageprops", {}):
            continue
        page_title = _text(page.get("title"))
        base_title = re.sub(r"\s*\([^()]+\)$", "", page_title)
        if _key(base_title) != _key(title):
            continue
        extract = _text(page.get("extract"))
        categories = " ".join(_text(item.get("title")) for item in page.get("categories", []) if isinstance(item, dict))
        qualifier = re.search(r"\(([^()]*)\)$", page_title)
        qualifier = qualifier.group(1) if qualifier else ""
        article_year = _year(qualifier)
        if year and article_year and article_year != year:
            continue
        title_tv = bool(re.search(r"드라마|텔레비전|television|TV series", qualifier, re.I))
        title_film = bool(re.search(r"영화|\bfilm\b", qualifier, re.I))
        if (title_tv and not tv) or (title_film and tv):
            continue
        # The intro can mention a film adapted into a TV show (or the reverse).
        # Use article identity/categories, not an incidental intro mention.
        evidence = " ".join((qualifier, categories))
        kind_pattern = r"드라마|텔레비전|television|TV series" if tv else r"영화|\bfilms?\b"
        if not re.search(kind_pattern, evidence, re.I):
            continue
        if year and not re.search(rf"\b{re.escape(year)}(?:년|\b)", extract):
            continue
        # An article's page image can also be a cast photo or logo. Require a
        # filename that explicitly identifies a poster rather than assuming it.
        if not re.search(r"poster|포스터", _text(page.get("pageimage")), re.I):
            continue
        original = page.get("original", {})
        poster = _url(original.get("source") if isinstance(original, dict) else "", domains=("wikimedia.org", "wikipedia.org"))
        if poster:
            matches.append({"poster_url": poster, "poster_source": "위키백과",
                            "poster_source_url": f"https://{language}.wikipedia.org/wiki/" + quote(page_title.replace(" ", "_"))})
    if not year and len({item["poster_source_url"] for item in matches}) > 1:
        return {}
    return matches[0] if matches else {}


@lru_cache(maxsize=2048)
def _cached_poster(title, year, content_type, hour_bucket):
    for provider in (_itunes_poster, _wikipedia_poster):
        try:
            poster = provider(title, year, content_type)
            if poster:
                return poster
        except (requests.RequestException, ValueError, TypeError, KeyError):
            continue
    return {}


def enrich_poster_candidates(contents, *, max_workers=4, limit=40):
    """Return copies with genuine posters, using at most ``limit`` unique lookups.

    Existing valid artwork and all candidate identity/status fields are preserved.
    Both successful and empty lookups are cached for the current hour. Explicit
    refresh can fill older Kinolights-cache entries whose poster fields are blank.
    """
    output = [dict(item) for item in contents if isinstance(item, dict)]
    pending = {}
    for index, item in enumerate(output):
        if _url(item.get("poster_url")):
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
        futures = {executor.submit(_cached_poster, *identity, bucket): indices for identity, indices in pending.items()}
        for future in as_completed(futures):
            try:
                artwork = future.result()
            except Exception:
                artwork = {}
            if artwork:
                for index in futures[future]:
                    output[index].update(artwork)
    return output
