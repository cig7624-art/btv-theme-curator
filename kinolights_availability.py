"""Explicit, logged-out title lookups, separate from theme-candidate storage.

Availability describes the source's current public offer information. Missing
or unreadable information is never treated as proof that a work is unavailable.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from kinolights_candidates import _candidate_from_mapping, _clean, _norm, _type, _year

CACHE_TTL_SECONDS = 10 * 60
CACHE_MAX_ENTRIES = 128
GRAPHQL_URL = "https://gateway.kinolights.com/graphql"
# Confirmed against the logged-out public schema and responses on 2026-10-26.
# Keep this list explicit: content also exposes viewer-specific fields.
SEARCH_QUERY = """query PublicTitleSearch($keyword: String!) {
  searchContentsByKeyword(keyword: $keyword, limit: 20) {
    items { contentId content { id titleKr titleEn openYear contentTypes } }
    totalCount
  }
}"""
DETAIL_QUERY = """query PublicTitleOffers($movieId: Int!) {
  content(movieId: $movieId) {
    id titleKr titleEn openYear contentTypes
    vodOfferItems {
      id title retailPrice monetizationType url openDate expireDate
      isActive isDone isRemove providerTechnicalName
    }
  }
}"""
_PROVIDER_NAMES = {
    "netflix": "넷플릭스", "tving": "티빙", "wavve": "웨이브",
    "watcha-play": "왓챠", "coupang-play": "쿠팡플레이", "apple": "Apple TV",
    "disney-plus": "디즈니+", "disney": "디즈니+",
}
_OFFER_TYPES = {"streaming": "subscription", "rental": "rent", "buy": "buy"}
_CACHE: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    "Accept": "application/json",
}


class AvailabilityLookupError(RuntimeError):
    """The source could not be read; this is different from zero search hits."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clear_cache() -> None:
    """Clear only the short-lived availability cache, never candidate CSV files."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cached(key: tuple) -> Any:
    with _CACHE_LOCK:
        now = time.monotonic()
        for expired in [item for item, (deadline, _) in _CACHE.items() if deadline <= now]:
            del _CACHE[expired]
        if key not in _CACHE:
            return None
        _CACHE.move_to_end(key)
        return deepcopy(_CACHE[key][1])


def _remember(key: tuple, value: Any, *, ttl: float = CACHE_TTL_SECONDS) -> Any:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic() + min(CACHE_TTL_SECONDS, max(0, ttl)), deepcopy(value))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return deepcopy(value)


def _title_url(value: Any) -> tuple[str, str]:
    """Accept public Kinolights title identity URLs, never arbitrary fetch targets."""
    try:
        parsed = urlsplit(_clean(value))
        port = parsed.port
    except ValueError:
        return "", ""
    if parsed.scheme != "https" or parsed.hostname not in {"m.kinolights.com", "kinolights.com", "www.kinolights.com"} or parsed.username or parsed.password or port:
        return "", ""
    match = re.fullmatch(r"/title/(\d+)/?", parsed.path)
    if not match:
        return "", ""
    content_id = match.group(1)
    return f"https://m.kinolights.com/title/{content_id}", content_id


def _graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                                 headers={**_HEADERS, "Origin": "https://kinolights.com", "Referer": "https://kinolights.com/"},
                                 timeout=(5, 15))
        response.raise_for_status()
        if len(response.content) > 3_000_000:
            raise ValueError("oversized content response")
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AvailabilityLookupError("키노라이츠 공개 정보에 연결하지 못했습니다. 잠시 후 다시 검색해 주세요.") from exc
    if not isinstance(payload, dict) or payload.get("errors") or not isinstance(payload.get("data"), dict):
        raise AvailabilityLookupError("키노라이츠 공개 응답을 읽지 못했습니다. 원문에서 정보를 확인해 주세요.")
    return payload["data"]


def _content_type(row: dict[str, Any]) -> str:
    values = row.get("contentTypes")
    if not isinstance(values, list):
        return ""
    text = " · ".join(_clean(value) for value in values if isinstance(value, str))
    return _type(text) or {"movie": "영화", "film": "영화", "tv": "시리즈", "series": "시리즈"}.get(text.lower(), text)


def _parse_search_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    group = data.get("searchContentsByKeyword")
    if not isinstance(group, dict) or not isinstance(group.get("items"), list):
        raise AvailabilityLookupError("키노라이츠 검색 결과를 읽지 못했습니다.")
    candidates, seen = [], set()
    for item in group["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("content"), dict):
            continue
        raw = item["content"]
        title = _clean(raw.get("titleKr") or raw.get("titleEn"))
        if not title or not str(raw.get("id", "")).isdigit():
            continue
        row = _candidate_from_mapping({**raw, "title": title}, "https://kinolights.com") or {
            "title": title, "year": _year(raw.get("openYear")), "poster_url": "",
            "source_url": "https://m.kinolights.com/title/" + str(raw["id"]),
        }
        url, content_id = _title_url(row.get("source_url"))
        if not content_id or content_id in seen or str(item.get("contentId")) != content_id:
            continue
        seen.add(content_id)
        candidates.append({**row, "id": content_id, "content_id": content_id,
                           "title_en": _clean(raw.get("titleEn")),
                           "source_url": url, "content_type": _content_type(raw)})
    if group["items"] and not candidates:
        raise AvailabilityLookupError("키노라이츠 작품 정보를 읽지 못했습니다.")
    if not group["items"] and group.get("totalCount") not in {0, "0"}:
        raise AvailabilityLookupError("키노라이츠 검색 결과가 비어 있어 다시 확인이 필요합니다.")
    return candidates[:20]


def search_titles(query: str) -> list[dict[str, Any]]:
    """Search only when called; return distinct title IDs for explicit selection.

    An empty list means the public API explicitly returned zero results.
    Fetch failures and unrecognized source structures raise a readable error.
    """
    query = _clean(query)[:100]
    if not query:
        return []
    key = ("search", query.casefold())
    cached = _cached(key)
    if cached is not None:
        return cached
    rows = _parse_search_data(_graphql(SEARCH_QUERY, {"keyword": query}))
    fetched_at = _now()
    for row in rows:
        row["fetched_at"] = fetched_at
    return _remember(key, rows)


def _offer_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("invalid offer date")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("offer date has no source timezone")
    return result.astimezone(timezone.utc)


def _service_url(value: Any) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
    except ValueError:
        return ""
    return value


def _detail_cache_ttl(data: dict[str, Any], now: datetime) -> float:
    """A known offer opening/expiry must also end its cached observation."""
    raw = data.get("content")
    offers = raw.get("vodOfferItems") if isinstance(raw, dict) else None
    ttl = float(CACHE_TTL_SECONDS)
    for offer in offers if isinstance(offers, list) else []:
        if not isinstance(offer, dict):
            continue
        for field in ("openDate", "expireDate"):
            try:
                boundary = _offer_date(offer.get(field))
            except (TypeError, ValueError, OverflowError):
                continue
            if boundary and boundary > now:
                ttl = min(ttl, (boundary - now).total_seconds())
    return ttl


def _parse_detail_data(data: dict[str, Any], selected: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    raw = data.get("content")
    if not isinstance(raw, dict):
        return {"status": "unknown", "providers": [], "reason": "선택한 작품의 공개 정보를 확인하지 못했습니다."}
    _, content_id = _title_url(selected.get("source_url"))
    title = _clean(raw.get("titleKr") or raw.get("titleEn"))
    if not raw.get("id") or not title:
        return {"status": "unknown", "providers": [], "reason": "선택한 작품의 제목과 번호를 원문에서 확인하지 못했습니다."}
    if str(raw["id"]) != content_id or _norm(title) != _norm(selected.get("title")):
        return {"status": "identity_mismatch", "providers": [], "reason": "선택한 작품과 원문의 제목 또는 작품 번호가 다릅니다."}
    year = _year(selected.get("year"))
    if year and not _year(raw.get("openYear")):
        return {"status": "unknown", "providers": [], "reason": "선택한 작품의 제작 연도를 원문에서 확인하지 못했습니다."}
    if year and _year(raw.get("openYear")) != year:
        return {"status": "identity_mismatch", "providers": [], "reason": "선택한 작품과 원문의 제작 연도가 다릅니다."}
    offers = raw.get("vodOfferItems")
    if not isinstance(offers, list):
        return {"status": "unknown", "providers": [], "reason": "공개 시청 제공 목록을 읽지 못했습니다."}
    now = now or datetime.now(timezone.utc)
    providers, uncertain = {}, False
    for offer in offers:
        if not isinstance(offer, dict):
            uncertain = True
            continue
        if offer.get("isActive") is False or offer.get("isRemove") is True:
            continue
        if offer.get("isActive") is not True or offer.get("isRemove") is not False or offer.get("isDone") is not True:
            uncertain = True
            continue
        try:
            opened, expires = _offer_date(offer.get("openDate")), _offer_date(offer.get("expireDate"))
        except (TypeError, ValueError, OverflowError):
            uncertain = True
            continue
        if (opened and opened > now) or (expires and expires <= now):
            continue
        technical_name = _clean(offer.get("providerTechnicalName"))
        if not technical_name:
            uncertain = True
            continue
        offer_type = _OFFER_TYPES.get(_clean(offer.get("monetizationType")), "unknown")
        provider = {"name": _PROVIDER_NAMES.get(technical_name, technical_name), "provider_id": technical_name,
                    "offer_type": offer_type, "url": _service_url(offer.get("url"))}
        price = offer.get("retailPrice")
        if isinstance(price, (int, float)) and not isinstance(price, bool) and price >= 0:
            provider["price"] = price
        key = (technical_name, offer_type)
        # Collapse quality/subtitle variants to one service/offer. Prefer the
        # source's lower quoted price, then preserve its original offer order.
        previous = providers.get(key)
        if previous is None or ("price" in provider and provider["price"] < previous.get("price", float("inf"))):
            providers[key] = provider
    status = "available" if providers else "unknown" if uncertain else "unavailable"
    return {"status": status, "providers": list(providers.values()), "content_type": _content_type(raw),
            "reason": "일부 제공 정보를 읽지 못했습니다." if uncertain else ""}


def get_title_availability(selected: dict[str, Any]) -> dict[str, Any]:
    """Read offers for the exact selected title ID; never auto-pick a remake."""
    url, content_id = _title_url(selected.get("source_url"))
    base = {"title": _clean(selected.get("title")), "year": _year(selected.get("year")),
            "content_type": _clean(selected.get("content_type")), "source_url": url,
            "fetched_at": _now(), "providers": []}
    if not url or not base["title"]:
        return {**base, "status": "identity_mismatch", "reason": "유효한 키노라이츠 작품을 다시 선택해 주세요."}
    key = ("detail", content_id, _norm(base["title"]), base["year"])
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        data = _graphql(DETAIL_QUERY, {"movieId": int(content_id)})
    except AvailabilityLookupError as exc:
        return {**base, "status": "fetch_error", "reason": str(exc)}
    now = datetime.now(timezone.utc)
    result = {**base, **_parse_detail_data(data, selected, now=now), "fetched_at": now.replace(microsecond=0).isoformat()}
    return _remember(key, result, ttl=_detail_cache_ttl(data, now))
