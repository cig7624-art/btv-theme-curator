"""Select a small, source-backed edition from collected media business material.

Collection is an archive, not an assertion that every document is a major issue.
This module never translates, fetches, generates a score, or alters source dates.
Its ordering is a published-period / business-change / diversity decision, and
every selected copy carries the actual reason for inclusion.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from domestic_product_sources import domestic_product_topic


NORMAL_DAYS = 90
# Product launches are less frequent than general media news. This wider
# archive window applies only to the dedicated domestic product edition.
DOMESTIC_DAYS = 180
EARNINGS_DAYS = 180
REVISION_DAYS = 30
MAX_PER_COMPANY = 3
MAX_PER_TOPIC = 4

_SCHEDULE = re.compile(
    r"to (?:announce|webcast)|conference call|participate.{0,45}(?:conference|session)|"
    r"mini-tender|annual meeting|schedules? (?:a )?special meeting|"
    r"실적\s*발표\s*(?:예정|일정|안내)|설명회\s*(?:개최|일정)|주주총회\s*(?:소집|안내|일정)", re.I)
_DISCLOSURE = re.compile(
    r"reconciliations?|non[- ]GAAP|financial tables|proxy statement|"
    r"개인정보|재무\s*조정표|비GAAP|공시\s*첨부|주주총회\s*소집", re.I)
_GENERIC = re.compile(
    r"^(?:home|news(?:room)?|entertainment|investor relations|financials?|"
    r"quarterly (?:results|earnings)|(?:press|news) release(?:s| details)?|"
    r"홈|뉴스룸|공지사항|보도자료|투자정보)(?:\s*[|–—-].*)?$", re.I)
_MEDIA = re.compile(
    r"Prime\s*Video|Disney\s*\+|Disney\s*Plus|ESPN|Hulu|Netflix|SVOD|DTC|"
    r"IPTV|OTT|스트리밍|방송|중계|콘텐츠|B\s*tv|Genie\s*TV|지니\s*TV|U\s*\+\s*tv|"
    r"티빙|웨이브|쿠팡플레이|넷플릭스|디즈니|streaming|viewership|studio|box office", re.I)
_MEDIA_EARNINGS = re.compile(
    r"viewership|viewers|streaming|subscriber|SVOD|DTC|broadcast|box office|"
    r"operating (?:income|profit)|revenue|rights|audience|시청|가입자|매출|영업|중계|광고", re.I)
_PRODUCT = re.compile(
    r"요금|월정액|구독료|결합|번들|패스|상품|출시|가입|혜택|프로모션|할인|"
    r"pricing|price|subscription|plans?|bundle|package|benefit|promotion|launch", re.I)
_PRICE = re.compile(r"요금|구독료|월정액|가격|pricing|prices?|ad[- ]supported|ad tier|광고형", re.I)
_BUNDLE = re.compile(r"결합|번들|제휴\s*상품|bundle|package|distribution|유통", re.I)
_SPORTS = re.compile(r"NBA|NFL|Super Bowl|Premier League|스포츠|중계권|스포츠\s*중계", re.I)
_SPORTS_CHANGE = re.compile(
    r"stream|livestream|broadcast|rights|available|how to watch|중계|스트리밍|독점|시청|제공", re.I)
_AI = re.compile(r"AI[- ]powered|AI[- ]assisted|AI\s*(?:search|검색)|generative AI|인공지능|생성형|자연어", re.I)
_BETA = re.compile(r"beta|pilot|experiment|베타|시범|실험|테스트", re.I)
_PARTNERSHIP = re.compile(r"partnership|partners? with|acquisition|acquire|merger|transaction|offer.{0,30}warner|제휴|인수|합병", re.I)
_LAUNCH = re.compile(r"launch|introduc|debut|release|출시|도입|신설|개편|확대|변경", re.I)
_CORPORATE_ONLY = re.compile(r"기부|봉사|사회공헌|보이스피싱|휴대폰|아이폰|iPhone|갤럭시|채용|cruise ship", re.I)
_SEASON = re.compile(r"\b(20\d{2})\s*[-–/]\s*(20\d{2}|\d{2})\b")


def _text(value, limit=100000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _date(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _company(item):
    value = re.sub(r"[^a-z0-9가-힣]", "", _text(item.get("company")).lower())
    return {"넷플릭스": "netflix", "디즈니": "disney", "disneyplus": "disney",
            "thewaltdisneycompany": "disney", "아마존": "amazon", "primevideo": "amazon",
            "sk브로드밴드": "skb", "skbroadband": "skb", "sk브로드밴드btv": "skb",
            "kt지니tv": "kt", "lg유플러스": "lguplus", "lgu": "lguplus"}.get(value, value)


def _canonical_url(value):
    try:
        parsed = urlsplit(_text(value, 6000))
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        query = [(key, value) for key, value in parse_qsl(parsed.query)
                 if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/",
                           urlencode(query), ""))
    except ValueError:
        return ""


def _evidence(item):
    rows = item.get("evidence")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping) and _text(row.get("id"))
            and len(_text(row.get("text"))) >= 30]


def _domestic_product(item, title):
    if item.get("market_scope") != "domestic_product":
        return False
    # The registry identifies the intended market, while positive headline
    # classification still prevents movie premieres and phone-only plans from
    # becoming product news. A confirmed feature/account change need not use
    # the literal word "상품" in its headline.
    country = _text(item.get("country")).upper()
    if re.search(r"기부|봉사|사회공헌|보이스피싱|채용", title):
        return False
    if re.search(r"휴대폰|아이폰|iPhone|갤럭시|인터넷", title, re.I) and not (
            _MEDIA.search(title) or re.search(r"TV\s*결합", title, re.I)):
        return False
    return country in {"", "KR", "한국"} and bool(domestic_product_topic(
        title, item.get("body_text", "")))


def _media_earnings_evidence(item, rows):
    selected = []
    for row in rows:
        value = _text(row["text"])
        if _DISCLOSURE.search(value[:180]) or re.search(r"forward-looking|financial guidance|safe harbor", value, re.I):
            continue
        if _MEDIA.search(value) and _MEDIA_EARNINGS.search(value):
            selected.append(row["id"])
        elif _company(item) == "netflix" and re.search(r"revenue|operating (?:income|margin)|paid memberships", value, re.I):
            selected.append(row["id"])
    return selected


def _classification(item, title, rows, domestic):
    topic = _text(item.get("topic"))
    body = " ".join(_text(row["text"]) for row in rows)
    if domestic:
        if _PRICE.search(title):
            return "상품·요금제", 0, "이용 요금·상품 조건에 영향을 주는 국내 공식 안내", []
        if _BUNDLE.search(title):
            return "결합·제휴 상품", 0, "가입 경로·결합 조건을 확인할 수 있는 국내 공식 안내", []
        topic = domestic_product_topic(title, item.get("body_text", ""))
        if topic == "ai_technology":
            return "시청 기능·기기", 1, "국내 서비스의 시청 기능·기기 변화를 확인한 공식 안내", []
        if re.search(r"동시\s*(?:시청|접속)|계정|이용\s*정책|해지", title):
            return "이용 조건·정책", 0, "국내 서비스의 계정·시청 조건 변화를 확인한 공식 안내", []
        return "상품 출시·혜택", 1, "국내 서비스의 상품 출시·이용 혜택을 확인한 공식 안내", []
    if _PRICE.search(title) and (_MEDIA.search(title + " " + body) or topic == "advertising_pricing"):
        return "광고·요금제", 0, "광고형 상품·구독 요금에 관한 공식 발표", []
    if _SPORTS.search(title) and _SPORTS_CHANGE.search(title):
        return "스포츠·중계 유통", 0, "중계 제공 경로·시청 범위를 확인할 수 있는 공식 자료", []
    if _AI.search(title) and (_MEDIA.search(title + " " + body) or topic == "ai_technology"):
        beta = bool(_BETA.search(title + " " + body))
        return "AI·서비스 실험" if beta else "AI·서비스 출시", 2 if beta else 1, (
            "미디어 탐색·이용 경험을 바꾸는 베타 실험에 관한 공식 발표" if beta
            else "미디어 서비스의 AI 기능 도입에 관한 공식 발표"), []
    if _PARTNERSHIP.search(title) and (_MEDIA.search(title + " " + body) or topic == "partnership_distribution"):
        return "제휴·유통 전략", 1, "콘텐츠 제작·유통·사업 구조에 영향을 주는 공식 제휴 발표", []
    if topic == "earnings_strategy" or re.search(r"earnings|quarter.{0,30}results|shareholder letter|분기.{0,15}실적", title, re.I):
        ids = _media_earnings_evidence(item, rows)
        if ids:
            return "미디어 사업 실적", 3, "실적 원문에서 미디어·광고·시청 사업의 구체적 근거를 확인한 자료", ids
        return None
    if _LAUNCH.search(title) and _MEDIA.search(title) and _PRODUCT.search(title):
        return "서비스·상품 변화", 1, "미디어 상품의 도입·이용 조건 변화를 확인한 공식 발표", []
    return None


def _current_season(title, observed):
    if not re.search(r"season|시즌", title, re.I):
        return False
    for match in _SEASON.finditer(title):
        start, end = int(match[1]), int(match[2])
        if end < 100:
            end += (start // 100) * 100
        if end not in {start, start + 1}:
            continue
        if start == observed.year or (end == observed.year and observed.month <= 6):
            return True
    return bool(re.search(r"\b" + str(observed.year) + r"\b", title) and not _SEASON.search(title))


def _recency(item, title, observed, category, *, domestic_scope=False):
    published = _date(item.get("published_at"))
    if published is None or published > observed or item.get("published_at_source") == "pdf_publication_unconfirmed":
        return None
    age = (observed - published).total_seconds() / 86400
    window = (DOMESTIC_DAYS if domestic_scope else
              EARNINGS_DAYS if category == "미디어 사업 실적" else NORMAL_DAYS)
    if age <= window:
        return published, "published", 0 if age <= 30 else 1 if age <= NORMAL_DAYS else 2
    revised = _date(item.get("source_updated_at"))
    if (category == "스포츠·중계 유통" and _current_season(title, observed) and revised is not None
            and published <= revised <= observed and observed - revised <= timedelta(days=REVISION_DAYS)):
        return revised, "source_updated", 0
    return None


def _event_key(item, title, category):
    explicit = _text(item.get("event_id"), 300)
    if explicit:
        return (category, explicit)
    bowl = re.search(r"super\s*bowl\s+([ivxlcdm]+|\d{1,3})\b", title, re.I)
    if bowl and category == "스포츠·중계 유통":
        return (category, "super-bowl-" + bowl[1].lower(), _company(item))
    normalized = re.sub(r"[^a-z0-9가-힣]", "", title.lower())
    return (category, normalized)


def select_major_insights(items, *, scope="all", now=None, limit=6):
    """Return at most six material, dated stories; retain every original input.

    ``scope='all'`` means all *major topics*, not the entire collected archive.
    The domestic page uses the same relevance rules and an explicit source
    ``market_scope='domestic_product'`` flag, with a 180-day product-announcement
    window. The general edition keeps its 90-day news window. Inclusion records
    the historical announcement, not current product availability or terms.
    No inferred home country is used.
    """
    if scope not in {"all", "domestic"}:
        raise ValueError("scope must be all or domestic")
    observed = _date(now) if now is not None else datetime.now(timezone.utc)
    if observed is None:
        raise ValueError("now must be a valid date or timestamp")
    limit = max(0, min(12, int(limit)))
    if not limit:
        return []
    candidates = []
    for item in items if isinstance(items, (list, tuple)) else []:
        if not isinstance(item, Mapping):
            continue
        title, url, rows = _text(item.get("title"), 1000), _canonical_url(item.get("url")), _evidence(item)
        if not _text(item.get("id")) or not title or not url or not rows or not _company(item):
            continue
        if _GENERIC.fullmatch(title) or _SCHEDULE.search(title) or _DISCLOSURE.search(title):
            continue
        domestic = _domestic_product(item, title)
        if item.get("market_scope") == "domestic_product" and not domestic:
            continue
        if scope == "domestic" and not domestic:
            continue
        classification = _classification(item, title, rows, domestic)
        if classification is None:
            continue
        category, priority, reason, evidence_ids = classification
        recent = _recency(item, title, observed, category,
                          domestic_scope=scope == "domestic" and domestic)
        if recent is None:
            continue
        event_date, date_basis, band = recent
        if date_basis == "source_updated":
            reason = "이번 시즌의 공식 중계 안내가 최근 수정됨 · 신규 발표가 아닌 원문 수정 자료"
        candidates.append({"item": item, "url": url, "category": category, "priority": priority,
                           "reason": reason, "evidence_ids": evidence_ids, "date": event_date,
                           "date_basis": date_basis, "band": band, "company": _company(item),
                           "event": _event_key(item, title, category)})
    # Only the latest eligible earnings statement for a company enters an
    # edition. Earlier quarters and detailed attachments remain in the archive.
    latest_earnings = {}
    for candidate in candidates:
        if candidate["category"] == "미디어 사업 실적":
            key = candidate["company"]
            if key not in latest_earnings or candidate["date"] > latest_earnings[key]:
                latest_earnings[key] = candidate["date"]
    candidates = [row for row in candidates if row["category"] != "미디어 사업 실적"
                  or row["date"] == latest_earnings[row["company"]]]
    selected, companies, topics = [], Counter(), Counter()
    urls, hashes, events = set(), set(), set()
    while candidates and len(selected) < limit:
        candidates.sort(key=lambda row: (row["band"], row["priority"], companies[row["company"]],
                                        topics[row["category"]], -row["date"].timestamp(), row["url"]))
        candidate = candidates.pop(0)
        item, company, category = candidate["item"], candidate["company"], candidate["category"]
        fingerprint = _text(item.get("content_hash"))
        if (companies[company] >= MAX_PER_COMPANY or topics[category] >= MAX_PER_TOPIC
                or candidate["url"] in urls or candidate["event"] in events
                or (fingerprint and fingerprint in hashes)):
            continue
        copied = deepcopy(dict(item))
        copied.update(editorial_reason=candidate["reason"], editorial_category=category,
                      editorial_date_basis=candidate["date_basis"],
                      editorial_date=candidate["date"].date().isoformat())
        if candidate["evidence_ids"]:
            copied["editorial_evidence_ids"] = list(candidate["evidence_ids"])
        selected.append(copied)
        companies[company] += 1
        topics[category] += 1
        urls.add(candidate["url"])
        events.add(candidate["event"])
        if fingerprint:
            hashes.add(fingerprint)
    return selected
