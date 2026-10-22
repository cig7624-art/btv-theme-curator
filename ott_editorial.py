"""Select a short editorial digest from observed public OTT homes only.

Scores express this app's editorial preference and home placement, never measured
audience interest. Rendering and selection do not fetch data or mutate snapshots.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
import re

from ott_collection import PERSONAL_RE, safe_http_url


EDITORIAL_CRITERIA = (
    "최근 96시간 안에 확인한 공개 홈·홍보 목록에서 이미지가 있는 항목만 고릅니다.",
    "메인 배너와 홈 상단의 기획·무료 공개·오리지널·신작을 우선합니다.",
    "같은 작품·기획의 반복을 줄이고 서비스당 최대 2건으로 묶습니다. 모든 OTT를 채우지는 않습니다.",
    "홈 배치를 요약한 편집 선정이며 조회수·시청률이나 실제 화제성 순위가 아닙니다.",
)
SERVICES = {
    "netflix": ("NETFLIX", "https://www.netflix.com/kr/"),
    "tving": ("TVING", "https://www.tving.com/"),
    "coupang": ("쿠팡플레이", "https://www.coupangplay.com/"),
    "disney": ("Disney+", "https://www.disneyplus.com/ko-kr"),
    "wavve": ("wavve", "https://www.wavve.com/"),
}
KIND_LABELS = {
    "theme": "기획 테마", "free": "무료 공개", "originals": "독점·오리지널",
    "new": "신작", "promotion": "메인 홍보작", "ranking": "공개 인기 목록",
    "clips": "클립 기획", "live": "라이브", "shorts": "쇼츠", "genre": "장르",
    "calendar": "요일별", "rental": "개별구매", "other": "홈 목록",
}
_KIND_SCORE = {"free": 94, "theme": 90, "originals": 84, "promotion": 90,
               "new": 80, "ranking": 66, "clips": 58, "shorts": 48,
               "genre": 36, "live": 38, "calendar": 28, "rental": 24, "other": 25}
_PLACEHOLDER = re.compile(r"(?:placeholder|transparent|loading|blank)(?:[._/-]|$)", re.I)
_GENERIC_COLLECTION = re.compile(r"^(?:컬렉션|collections?|브랜드|brands?|카테고리|categories|전체\s*보기)$", re.I)
_OPERATIONAL_TITLE = re.compile(r"^(?:방송|영플|일드|중드|tvn|tvING)[_:]", re.I)
_PERSONAL_TITLE = re.compile(r"맞춤\s*(?:추천|콘텐츠)|(?:회원|고객)님|당신을?\s*위한|너를\s*위한|for\s+you|personalized", re.I)


def _text(value, limit=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _rows(value):
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _identity(value):
    return re.sub(r"[\W_]+", "", _text(value).casefold())


def _position(value, fallback):
    try:
        return max(1, int(value))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _image(value):
    url = safe_http_url(value)
    return url if url and not _PLACEHOLDER.search(url.split("?", 1)[0]) else ""


def _works(row):
    result, seen = [], set()
    for raw in _rows(row.get("works")):
        title = _text(raw.get("title"), 180)
        key = _identity(title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"title": title, "url": safe_http_url(raw.get("url")),
                       "image_url": _image(raw.get("image_url") or raw.get("poster_url"))})
    return result


def _summary(kind, title, works, *, promotion, public):
    if promotion:
        return "공개 메인에서 소개하는 작품." if public else "메인 배너로 선보이는 작품."
    names = "·".join(f"〈{work['title']}〉" for work in works[:2])
    if not names:
        return ""
    suffix = " 등" if len(works) > 2 else ""
    if kind == "free" and re.search(r"1\s*화|첫\s*화", title):
        return f"{names}{suffix} 첫 화 무료 보기."
    if kind == "free":
        return f"{names}{suffix} 무료 공개 목록."
    if kind == "originals":
        return f"{names}{suffix} 독점·오리지널 라인업."
    if kind == "new":
        timing = "공개 예정 라인업." if re.search(r"예정|coming\s*soon", title, re.I) else "새로 소개한 라인업."
        return f"{names}{suffix} {timing}"
    if kind == "ranking":
        return f"{names}{suffix} 공개 인기 목록에 소개."
    if kind == "theme":
        return f"{names}{suffix} 한데 묶은 기획."
    return f"{names}{suffix} 홈에서 함께 소개."


def _candidates(snapshot, now, max_age_hours):
    for platform in _rows(snapshot.get("platforms")):
        service_id = _text(platform.get("id"))
        if service_id not in SERVICES or platform.get("status") not in {"ok", "promotion_only"}:
            continue
        if platform.get("freshness") not in {None, "", "current"}:
            continue
        observed = platform.get("last_success_at") or platform.get("checked_at") or snapshot.get("updated_at")
        when = _date(observed)
        if when is None or not -6 <= (now - when).total_seconds() / 3600 <= max_age_hours:
            continue
        public = platform.get("status") == "promotion_only"
        name, official = SERVICES[service_id]
        home = safe_http_url(platform.get("source_url")) or official
        sources = [(row, True, index + 1) for index, row in enumerate(_rows(platform.get("promotions")))]
        # Marketing pages cannot supply verified content-home sections.
        key = "public_lists" if public else "sections"
        sources.extend((row, False, index + 1) for index, row in enumerate(_rows(platform.get(key))))
        for row, promotion, fallback in sources:
            title = _text(row.get("title"), 220)
            kind = "promotion" if promotion else _text(row.get("kind")) or "other"
            if (not title or PERSONAL_RE.search(title) or _PERSONAL_TITLE.search(title) or kind == "collection"
                    or _GENERIC_COLLECTION.fullmatch(title)
                    or row.get("items_kind") in {"collection", "collections", "links"}):
                continue
            works = _works(row)
            if not promotion and not works:
                continue
            image = _image(row.get("image_url") or row.get("poster_url")) if promotion else ""
            if not image:
                image = next((work["image_url"] for work in works if work["image_url"]), "")
            if not image:
                continue
            position = _position(row.get("position"), fallback)
            prominence = max(0, 20 - position * 2)
            # Keep operational campaign labels as evidence, but favor clearer
            # public-facing headlines when a site exposes internal banner names.
            score = (_KIND_SCORE.get(kind, _KIND_SCORE["other"]) + prominence
                     - (6 if public else 0) - (16 if _OPERATIONAL_TITLE.search(title) else 0))
            if score < 70:
                continue
            yield {
                "id": f"{service_id}:{'promotion' if promotion else 'section'}:{_text(row.get('id')) or position}",
                "platform_id": service_id, "platform_name": name,
                "headline": title, "summary": _summary(kind, title, works, promotion=promotion, public=public),
                "kind": kind, "kind_label": KIND_LABELS.get(kind, KIND_LABELS["other"]),
                "image_url": image, "image_shape": "banner" if promotion else "poster",
                "works": works[:4], "source_title": title,
                "source_url": safe_http_url(row.get("source_url") or row.get("url")) or home,
                "home_url": home, "position": position, "placement": _text(row.get("placement")),
                "checked_at": str(observed), "scope": "public_promotion" if public else "public_home",
                "selection_reason": f"{KIND_LABELS.get(kind, '홈 목록')} · 원문 {position}번째 · 이미지 확인",
                "selection_score": score,
            }


def _duplicates(candidate, selected):
    title = _identity(candidate["headline"])
    work_ids = {_identity(work["title"]) for work in candidate["works"]}
    if candidate["kind"] == "promotion":
        work_ids.add(title)
    for previous in selected:
        if title == _identity(previous["headline"]) or candidate["image_url"] == previous["image_url"]:
            return True
        previous_ids = {_identity(work["title"]) for work in previous["works"]}
        if previous["kind"] == "promotion":
            previous_ids.add(_identity(previous["headline"]))
        # A many-title collection may still add useful context around a banner.
        if work_ids and previous_ids and max(len(work_ids), len(previous_ids)) <= 2:
            if len(work_ids & previous_ids) / min(len(work_ids), len(previous_ids)) >= .75:
                return True
    return False


def select_editorial_highlights(snapshot, limit=6, *, now=None, max_age_hours=96):
    """Return at most ``limit`` actual highlights; no service quota or padding.

    Select a source main-banner hero when its score is close to the best story,
    then favor new providers/categories. The per-provider cap is two. Freshness
    uses real observation dates and can be frozen with ``now`` in verification.
    """
    if not isinstance(snapshot, Mapping) or limit <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    candidates = list(_candidates(snapshot, now, max_age_hours))
    selected, provider_counts, kind_counts = [], Counter(), Counter()
    while candidates and len(selected) < min(limit, 6):
        candidates = [row for row in candidates if provider_counts[row["platform_id"]] < 2
                      and not _duplicates(row, selected)]
        if not candidates:
            break
        def priority(row):
            return (row["selection_score"] - provider_counts[row["platform_id"]] * 20 - kind_counts[row["kind"]] * 14,
                    -row["position"], row["platform_id"], row["id"])
        best = max(candidates, key=priority)
        if not selected:
            banners = [row for row in candidates if row["image_shape"] == "banner"
                       and row["selection_score"] >= best["selection_score"] - 15]
            if banners:
                best = max(banners, key=priority)
        selected.append(best)
        provider_counts[best["platform_id"]] += 1
        kind_counts[best["kind"]] += 1
        candidates.remove(best)
    return selected
