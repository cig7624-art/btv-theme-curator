"""Read-only comparisons of public documents, without questions or model calls.

Different announcements form a timeline. A before/after comparison requires
two actually saved versions of the same original document; it never infers a
price change by subtracting amounts from unrelated products or markets.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
import re
from urllib.parse import urlsplit


def public_url(value):
    if not isinstance(value, str) or any(ord(character) <= 32 for character in value):
        return ""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password
                or host == "localhost" or host.endswith((".local", ".internal")) or "." not in host):
            return ""
        parsed.port
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        return value
    except ValueError:
        return ""


def _date(value):
    try:
        result = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _paragraphs(value):
    rows = value.get("evidence", []) if isinstance(value, Mapping) else []
    seen, result = set(), []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or not isinstance(row.get("text"), str):
            continue
        text = re.sub(r"\s+", " ", row["text"]).strip()
        if len(text) >= 12 and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def public_documents(snapshot, company=None):
    """Return source rows only; no private notes/project state is considered."""
    rows = snapshot.get("items", []) if isinstance(snapshot, Mapping) else []
    result, seen = [], set()
    for item in rows if isinstance(rows, list) else []:
        if (not isinstance(item, Mapping) or not isinstance(item.get("id"), str)
                or not item.get("company") or not public_url(item.get("url")) or not _paragraphs(item)
                or (company and item.get("company") != company)):
            continue
        identity = (item["id"], item["url"])
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(result, key=lambda item: (_date(item.get("published_at")) or epoch,
                                           _date(item.get("collected_at")) or epoch, item["id"]), reverse=True)


def compare_public_document(item):
    """Compare actual source excerpts from the last saved earlier observation."""
    empty = {"status": "no_previous", "label": "비교할 이전 기록 없음", "before_at": "", "after_at": "",
             "added": [], "removed": [], "source_url": public_url(item.get("url")) if isinstance(item, Mapping) else ""}
    if not isinstance(item, Mapping) or not empty["source_url"] or not _paragraphs(item):
        return empty
    observed = _date(item.get("collected_at"))
    versions = item.get("versions", [])
    previous = []
    for row in versions if isinstance(versions, list) else []:
        if not isinstance(row, Mapping) or not row.get("content_hash") or not _paragraphs(row):
            continue
        if ((row.get("url") and row["url"] != item.get("url"))
                or (row.get("id") and row["id"] != item.get("id"))
                or (row.get("company") and row["company"] != item.get("company"))):
            continue
        when = _date(row.get("collected_at"))
        if (not observed or not when or when >= observed
                or row.get("content_hash") == item.get("content_hash")):
            continue
        previous.append((when, row))
    if not previous:
        return empty
    _, before = max(previous, key=lambda row: row[0])
    earlier, latest = _paragraphs(before), _paragraphs(item)
    added = [text for text in latest if text not in set(earlier)]
    removed = [text for text in earlier if text not in set(latest)]
    return {"status": "changed" if added or removed else "unchanged",
            "label": "동일 원문에서 달라진 문장" if added or removed else "확인한 본문은 동일합니다",
            "before_at": before["collected_at"], "after_at": item["collected_at"],
            "added": added[:4], "removed": removed[:4], "source_url": empty["source_url"]}


def comparison_focus(topic):
    """Public comparison dimensions, never fabricated competitor strategies."""
    return {
        "advertising_pricing": "국가·광고 포함 여부·결합 조건과 적용 시점을 함께 확인하세요.",
        "partnership_distribution": "콘텐츠를 만나는 경로와 제휴·묶음 상품의 범위를 확인하세요.",
        "branding_strategy": "공식 메시지와 실제 제공하는 콘텐츠·혜택을 함께 확인하세요.",
        "sports_content": "독점 여부·공개 일정·중계 대상과 지역을 확인하세요.",
        "ai_technology": "실제 제공 기능과 시범 적용의 범위를 확인하세요.",
        "earnings_strategy": "같은 사업 부문·기간·지표 정의인지 확인하세요.",
    }.get(topic, "공식 발표의 대상·적용 지역·시행일을 확인하세요.")
