"""Pure topic membership backed by paragraphs from the same media document.

An earnings document can contain a concrete streaming-advertising announcement
or a film-production AI statement. Such a paragraph supports an additional
filter topic while the document keeps its original primary category.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
from urllib.parse import urlsplit

from domestic_product_sources import DOMESTIC_MEDIA_SOURCES, domestic_media_topic


_DOMESTIC_SOURCE_IDS = frozenset(source["id"] for source in DOMESTIC_MEDIA_SOURCES)
_MEDIA_CONTEXT = re.compile(
    r"\bNetflix\b|Disney\s*(?:\+|Plus)|\bHulu\b|\bESPN\b|Prime\s*Video|"
    r"\bstreaming\b|connected\s*TV|\btelevision\b|\b(?:films?|movies?)\b|"
    r"\bstorytell(?:ers?|ing)\b|\b(?:pre|post)[- ]production\b|"
    r"(?<![a-z])B\s*tv(?![a-z])|IPTV|OTT|넷플릭스|티빙|웨이브|쿠팡\s*플레이|"
    r"디즈니\s*(?:\+|플러스)|지니\s*TV|U\s*\+\s*tv|방송|영화|영상|시청", re.I)
_ADVERTISING = re.compile(
    r"\badvertis(?:ing|ers?|ements?)\b|\bvideo\s+ads\b|\bad[- ]supported\b|"
    r"\bad\s+(?:tier|suite|plan|creative|campaigns?)\b|광고(?:형|주|매출|수익|요금|서비스|플랫폼|판매)|"
    r"광고\s*(?:제작|인벤토리|사업|상품|제휴|공급|도입)", re.I)
_PLAN_PRICING = re.compile(
    r"\bsubscription\s+plans?\b|\bsubscription\s+(?:price|fee|pricing)\b|"
    r"\bmonthly\s+(?:price|fee)\b|요금제|구독료|월정액|구독\s*상품|이용권", re.I)
_AI = re.compile(r"\bAI\b|\bOpenAI\b|artificial\s+intelligence|machine\s+learning|생성형|인공지능", re.I)
_AI_MEDIA_USE = re.compile(
    r"search|discover|recommend|\b(?:pre|post)[- ]production\b|creativ(?:e|ity)|storytell|"
    r"\bvideo\s+(?:ads|production|generation|creation)\b|advertis|검색|추천|화질|"
    r"음성\s*검색|자막|영상\s*(?:제작|생성|편집)|방송\s*(?:제작|솔루션)", re.I)


def media_primary_topic(item):
    """Use current domestic rules for legacy rows without rewriting storage."""
    if not isinstance(item, Mapping):
        return ""
    primary = str(item.get("topic") or "").strip()
    if item.get("source_id") in _DOMESTIC_SOURCE_IDS and item.get("country") == "KR":
        current = domestic_media_topic(item.get("title"), item.get("body_text", ""))
        if current:
            return current
    return primary


def _document_identity(value):
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        parsed.port
        return parsed.scheme, parsed.hostname.lower(), parsed.port, parsed.path.rstrip("/")
    except ValueError:
        return None


def topic_evidence_matches(item):
    """Map supported ad/AI topics to the exact saved paragraph evidence IDs.

    Each matched paragraph must itself establish both the topic and its media
    use. A separate AWS/retail paragraph and a Prime Video paragraph cannot be
    combined into a media AI claim. Headlines and body-only text are not proof.
    """
    if not isinstance(item, Mapping) or not isinstance(item.get("evidence"), list):
        return {}
    document = _document_identity(item.get("url"))
    if not document:
        return {}
    matches = {}
    for paragraph in item["evidence"][:64]:
        if not isinstance(paragraph, Mapping) or _document_identity(paragraph.get("url")) != document:
            continue
        identifier = str(paragraph.get("id") or "").strip()
        text = str(paragraph.get("text") or "").strip()[:4000]
        if not identifier or not _MEDIA_CONTEXT.search(text):
            continue
        topics = []
        if _ADVERTISING.search(text) or _PLAN_PRICING.search(text):
            topics.append("advertising_pricing")
        if _AI.search(text) and _AI_MEDIA_USE.search(text):
            topics.append("ai_technology")
        for topic in topics:
            ids = matches.setdefault(topic, [])
            if identifier not in ids:
                ids.append(identifier)
    return matches


def secondary_topic_matches(item):
    """Return only additional topics, with the source evidence for each one."""
    primary = media_primary_topic(item)
    return {topic: identifiers for topic, identifiers in topic_evidence_matches(item).items() if topic != primary}


def media_topic_ids(item):
    """Return the primary category followed by any supported secondary topics."""
    primary = media_primary_topic(item)
    return tuple(dict.fromkeys(([primary] if primary else []) + list(secondary_topic_matches(item))))
