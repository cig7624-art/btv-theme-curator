"""Resolve draft contents without saving themes, candidates, or lookup files.

The local content catalogue confirms identity, not B tv service rights. Existing
verified metadata and poster resolution are shared with the normal theme flow.
Draft resolution deliberately avoids ``verify_title_on_kinolights`` because that
legacy entry point writes its search cache, even for unverified search links.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import ipaddress
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from kinolights_candidates import _clean, _search_url, _type, _year
from poster_assets import _key, enrich_poster_candidates


AVAILABILITY_LABEL = "B tv 서비스 여부 미확인"
_METADATA_DOMAINS = ("kinolights.com", "themoviedb.org", "imdb.com", "tv.apple.com", "wikipedia.org")
_ART_FIELDS = ("poster_url", "poster_source", "poster_source_url")


def _rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, pd.DataFrame):
        return value.fillna("").to_dict("records")
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, (list, tuple)) else []


def _kind(value: Any) -> str:
    raw = _clean(value)
    aliases = {"movie": "영화", "film": "영화", "tv": "드라마", "tv_series": "드라마",
               "series": "드라마", "drama": "드라마", "시리즈": "드라마", "애니": "애니메이션",
               "animation": "애니메이션", "documentary": "다큐", "다큐멘터리": "다큐"}
    return aliases.get(raw.casefold(), _type(raw) or raw)


def _identity(row: Mapping) -> tuple[str, str, str]:
    return (_key(row.get("title")), _year(row.get("year")), _kind(row.get("content_type") or row.get("type")))


def _url(value: Any) -> str:
    text = _clean(value)
    try:
        parsed = urlsplit(text)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (not text or any(ord(char) < 32 for char in text) or parsed.scheme not in {"http", "https"}
                or not host or parsed.username or parsed.password or host == "localhost"
                or host.endswith((".localhost", ".local", ".internal")) or "." not in host):
            return ""
        parsed.port
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return text
    except (TypeError, ValueError):
        return ""


def _metadata_source(value: Any) -> str:
    source = _url(value)
    if not source:
        return ""
    parsed = urlsplit(source)
    host = parsed.hostname or ""
    if not any(host == domain or host.endswith("." + domain) for domain in _METADATA_DOMAINS):
        return ""
    # A search link is useful navigation, but cannot prove a work's identity.
    if not parsed.path.strip("/") or any(part in parsed.path.casefold().split("/") for part in ("search", "find")):
        return ""
    return source


def _matches(row: Mapping, requested: Mapping) -> bool:
    title, year, kind = _identity(requested)
    candidate_title, candidate_year, candidate_kind = _identity(row)
    return (bool(title) and title == candidate_title and (not year or year == candidate_year)
            and (not kind or kind == candidate_kind))


def _unique(rows: list[dict[str, Any]], requested: Mapping) -> tuple[dict[str, Any] | None, bool]:
    """Return an exact identity only; never pick the first ambiguous remake."""
    matches = [row for row in rows if _matches(row, requested)]
    identities = {_identity(row) for row in matches}
    if len(identities) != 1:
        return None, len(identities) > 1
    # Multiple saved recommendations of the same work can contain different art
    # coverage. Their shared identity is fixed before preferring useful artwork.
    return max(matches, key=lambda row: (bool(_url(row.get("poster_url"))), bool(_metadata_source(row.get("source_url"))))), False


def _title_index(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Narrow exact-identity checks while preserving row order and remakes."""
    index: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        index.setdefault(_key(row.get("title")), []).append(row)
    return index


def _verified(row: Mapping) -> bool:
    return (_clean(row.get("kinolights_status") or row.get("status")) == "verified"
            and bool(_metadata_source(row.get("source_url"))))


def _excluded(values: Any) -> tuple[set[str], set[str]]:
    if isinstance(values, str):
        values = [values]
    titles, identifiers = set(), set()
    for value in values or []:
        if isinstance(value, Mapping):
            titles.add(_key(value.get("title")))
            identifiers.add(_clean(value.get("content_id")))
        else:
            titles.add(_key(value))
    return titles - {""}, identifiers - {""}


def resolve_director_contents(
    suggestions: list[dict[str, Any]],
    local_contents: pd.DataFrame,
    existing_candidates: pd.DataFrame | None = None,
    *,
    limit: int = 10,
    excluded_titles=None,
    enrich_posters: bool = True,
    provider: Callable | None = None,
    poster_enricher: Callable | None = None,
) -> list[dict[str, Any]]:
    """Resolve LLM suggestions to confirmed identities and genuine poster URLs.

    A ``provider`` is optional and must be a read-only metadata lookup callable
    compatible with ``(title, requested_year=..., requested_type=...)``. Its exact
    returned title/year/type and verified deep source are checked again. Without
    such evidence, unknown suggestions are omitted rather than padded to a count.
    All returned availability fields remain unconfirmed, including local matches.
    """
    limit = max(0, min(int(limit), 10))
    if not limit:
        return []
    catalog, saved = _rows(local_contents), _rows(existing_candidates)
    verified = [row for row in saved if _verified(row)]
    catalog_by_title = _title_index(catalog)
    saved_by_title = _title_index(saved)
    verified_by_title = _title_index(verified)
    catalog_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in catalog:
        catalog_by_id.setdefault(_clean(row.get("content_id")), []).append(row)
    excluded, excluded_ids = _excluded(excluded_titles)
    output, seen = [], set()
    for raw in _rows(suggestions)[:100]:
        requested = dict(raw)
        content_id = _clean(requested.get("content_id"))
        if content_id:
            id_matches = catalog_by_id.get(content_id, [])
            if not id_matches or content_id in excluded_ids:
                continue
            if not _clean(requested.get("title")):
                requested["title"] = id_matches[0].get("title", "")
            matched, ambiguous = _unique(id_matches, requested)
            if matched is None:
                continue
        else:
            matched, ambiguous = _unique(catalog_by_title.get(_key(requested.get("title")), []), requested)
        if _key(requested.get("title")) in excluded or ambiguous:
            continue
        catalog_status = "confirmed_local"
        if matched is None:
            matched, ambiguous = _unique(verified_by_title.get(_key(requested.get("title")), []), requested)
            catalog_status = "verified_external"
            if ambiguous:
                continue
            if matched is None and provider is not None:
                try:
                    result = provider(_clean(requested.get("title")), requested_year=_year(requested.get("year")),
                                      requested_type=_kind(requested.get("content_type") or requested.get("type")))
                    if isinstance(result, Mapping) and _verified(result) and _matches(result, requested):
                        matched = dict(result)
                except Exception:
                    matched = None
            if matched is None:
                continue
        identity = _identity(matched)
        if identity in seen or identity[0] in excluded or _clean(matched.get("content_id")) in excluded_ids:
            continue
        seen.add(identity)
        metadata, _ = _unique(saved_by_title.get(_key(matched.get("title")), []), matched)
        source = _metadata_source((metadata or {}).get("source_url")) or _metadata_source(matched.get("source_url"))
        poster = _url(matched.get("poster_url")) or _url((metadata or {}).get("poster_url"))
        kino_verified = _verified(metadata or {}) or _verified(matched)
        candidate = {
            "content_id": _clean(matched.get("content_id")) if catalog_status == "confirmed_local" else "",
            "title": _clean(matched.get("title")), "content_type": identity[2], "type": identity[2],
            "year": identity[1], "genre": _clean(matched.get("genre")), "tags": _clean(matched.get("tags")),
            "reason": _clean(requested.get("reason"))[:500],
            "source_url": source or _search_url(matched.get("title", "")), "poster_url": poster,
            "kinolights_status": "verified" if kino_verified else "catalog_confirmed",
            "catalog_status": catalog_status, "catalog_label": "콘텐츠 DB 확인" if catalog_status == "confirmed_local" else "외부 작품 정보 확인",
            "availability_status": "not_checked", "availability_label": AVAILABILITY_LABEL,
            "matched_score": 1.0, "fetched_at": _clean((metadata or matched).get("fetched_at")),
        }
        output.append(candidate)
        if len(output) == limit:
            break
    if enrich_posters and output:
        enrich = poster_enricher or enrich_poster_candidates
        try:
            enriched = enrich([dict(item) for item in output], limit=len(output))
        except Exception:
            enriched = []
        by_identity = {_identity(row): row for row in _rows(enriched)}
        for item in output:
            row = by_identity.get(_identity(item), {})
            if not _url(row.get("poster_url")):
                continue
            for field in _ART_FIELDS:
                if field == "poster_source":
                    item[field] = _clean(row.get(field))
                else:
                    item[field] = _url(row.get(field))
    return output
