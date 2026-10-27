"""Local provider identity images for source-confirmed subscription offers.

This presentation layer never fetches a source, calls a model, or changes the
availability backend's rental/purchase records.
"""
from __future__ import annotations

import base64
from collections.abc import Mapping
from functools import lru_cache
import html
from pathlib import Path
import re
from urllib.parse import urlsplit


_ASSETS = Path(__file__).resolve().parent / "assets" / "ott-providers"
_BRANDS = {
    "netflix": "넷플릭스", "tving": "티빙", "wavve": "웨이브",
    "watcha-play": "왓챠", "coupang-play": "쿠팡플레이", "disney": "디즈니+", "apple": "Apple TV",
}
_ALIASES = {"watcha": "watcha-play", "coupang": "coupang-play", "coupangplay": "coupang-play",
            "disney-plus": "disney", "disney+": "disney", "apple-tv": "apple", "appletv": "apple"}
_NAME_IDS = {name.casefold(): key for key, name in _BRANDS.items()}
_NAME_IDS.update({"netflix": "netflix", "tving": "tving", "wavve": "wavve", "watcha": "watcha-play",
                  "coupang play": "coupang-play", "disney+": "disney", "disney plus": "disney"})


def _text(value, limit=500):
    return str(value).strip()[:limit] if value is not None else ""


def _url(value):
    value = _text(value, 6000)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        parsed.port
        return value
    except ValueError:
        return ""


def _identity(provider):
    raw = _text(provider.get("provider_id"), 100).casefold()
    identifier = _ALIASES.get(raw, raw)
    if not identifier:
        identifier = _NAME_IDS.get(_text(provider.get("name"), 100).casefold(), "")
    return identifier


@lru_cache(maxsize=16)
def _logo_data_url(identifier):
    # Only fixed registered names may become local file paths.
    if identifier not in _BRANDS:
        return ""
    try:
        payload = (_ASSETS / (identifier + ".png")).read_bytes()
    except OSError:
        return ""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) > 128_000:
        return ""
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def subscription_provider_cards(result):
    """Deduplicate only explicitly confirmed subscriptions, preserving order.

    Provider identity comes from the source ID when present. An unregistered
    provider keeps its name; matching somebody else's name cannot borrow a logo.
    """
    if not isinstance(result, Mapping) or result.get("status") != "available":
        return []
    rows = result.get("providers")
    if not isinstance(rows, list):
        return []
    cards, positions = [], {}
    for row in rows[:100]:
        if not isinstance(row, Mapping) or _text(row.get("offer_type")).casefold() != "subscription":
            continue
        identifier = _identity(row)
        name = _BRANDS.get(identifier) or _text(row.get("name"), 100)
        if not name:
            continue
        key = (identifier or "unknown", name.casefold() if identifier not in _BRANDS else "")
        url = _url(row.get("url"))
        if key in positions:
            existing = cards[positions[key]]
            if not existing["url"] and url:
                existing["url"] = url
            continue
        positions[key] = len(cards)
        data_identifier = identifier if identifier in _BRANDS else re.sub(r"[^a-z0-9_-]+", "-", identifier).strip("-") or "unknown"
        cards.append({"provider_id": data_identifier, "name": name, "url": url,
                      "offer_type": "subscription", "logo_data_url": _logo_data_url(identifier)})
    return cards


def render_provider_logos_html(providers):
    """Return inner logo links for the caller's ``.ott-title-services`` wrapper."""
    cards = subscription_provider_cards({"status": "available", "providers": providers})
    elements = []
    for card in cards:
        name = html.escape(card["name"], quote=True)
        identifier = html.escape(card["provider_id"], quote=True)
        logo = _logo_data_url(card["provider_id"])
        body = ('<img class="ott-title-service-logo" src="' + logo + '" alt="' + name
                + '" width="40" height="40">') if logo else '<span class="ott-title-service-name">' + name + '</span>'
        attributes = ' class="ott-title-service" data-provider-id="' + identifier + '" title="' + name + '"'
        if card["url"]:
            elements.append('<a' + attributes + ' href="' + html.escape(card["url"], quote=True)
                            + '" target="_blank" rel="noopener noreferrer" aria-label="' + name
                            + '에서 구독으로 보기">' + body + '</a>')
        else:
            elements.append('<span' + attributes + ' aria-label="' + name + ' 구독">' + body + '</span>')
    return ''.join(elements)
