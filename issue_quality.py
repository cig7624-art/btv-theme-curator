"""Pure, reproducible issue assessment; native metrics are never a 100-point score.

``sort_priority`` is an ordinal category (4: current popular list or observed change, 3: official
ranking, 2: dated content event, 1: initial observation), not a popularity or
confidence score. A video needs at least 20,000 observed views per day to enter
category 4; smaller measured changes remain reference evidence. Evidence counts
never increase that category.
"""
from __future__ import annotations

import html
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import pandas as pd

from issue_rules import (
    DAILY_COMMENT_DELTA_THRESHOLD, DAILY_VIEW_DELTA_THRESHOLD,
    GROWTH_MIN_DELTA, GROWTH_RATE_THRESHOLD, ISSUE_LOOKBACK_DAYS,
    NETFLIX_MAX_AGE_DAYS, YOUTUBE_PRIORITY_VIEW_DELTA,
)


YOUTUBE = "YouTube 반응"
BOXOFFICE = "극장·박스오피스"
OTT = "OTT 랭킹·신작"
NEWS = "온라인 화제·뉴스"
SOURCE_GROUPS = (YOUTUBE, BOXOFFICE, OTT, NEWS)
NETFLIX_REFERENCE_URL = "https://www.netflix.com/tudum/top10/south-korea"
_BASE_COLUMNS = ("date", "source", "issue_title", "related_content", "keywords", "description", "source_url", "image_url")
_META_COLUMNS = ("published_at", "collected_at", "reference_date", "reference_period", "publisher", "source_type", "content_id", "event_type", "metrics_json", "quality_status", "quality_notes")
_OUTPUT_COLUMNS = ("source_group", "issue_key", "eligible", "exclusion_reason", "strength_label", "evidence_status", "selection_reason", "reference_label", "metric_summary", "sort_priority", "confirmed_routes", "confirmed_route_count", "issue_count", "comparison_valid", "popular_valid")
_OFFICIAL_OTT_HOSTS = ("netflix.com", "tving.com", "wavve.com", "disneyplus.com", "coupangplay.com")
_GENERIC = {
    "", "작품", "영화", "드라마", "예능", "콘텐츠", "신작", "극장가", "한국영화", "한국드라마",
    "예고", "예고편", "공식예고편", "티저", "공식티저", "메이킹", "비하인드", "명작맛집",
    "인터뷰", "인터뷰1", "인터뷰①", "하이라이트", "선공개", "풀버전", "한국", "넷플릭스",
    "mv", "musicvideo", "officialmv", "official", "trailer", "teaser", "ott", "top10",
    "ten스타필드", "ten초점", "기자수첩", "취재수첩", "데스크칼럼", "연예프리즘",
    "영화역사상가장감동적인", "한국인기영상", "ep1full", "full", "스페셜클립",
}
_ALIASES = {"doctorxmafiainwhite": "닥터X", "doctorx": "닥터X", "닥터x": "닥터X"}
_GAME = re.compile(r"메이플|스타크래프트|로스트아크|발로란트|마인크래프트|로블록스|배틀그라운드|리그\s*오브\s*레전드|오버워치|\bLCK\b|\bHLE\s*vs\s*T1\b|배치부터|챌린저", re.I)
_ADAPTATIONS = re.compile(r"아케인|\barcane\b|엣지러너|edgerunners|마인크래프트\s*(?:무비|영화)|라스트\s*오브\s*어스|last of us|스트리트\s*파이터|전자오락수호대", re.I)
_SPORT_VARIETY = re.compile(r"신인\s*감독\s*김연경|최강\s*(?:야구|농구)|뭉쳐야\s*찬다|골\s*때리는\s*그녀들|골때녀")
_MUSIC = re.compile(r"\b(?:official\s*)?(?:mv|m/v|music\s*video)\b|뮤직비디오|컴백\s*무대|직캠|\bfancam\b|\bfeat\.?\s|HYBE LABELS|1theK|A2O LITE|NCT WISH|이수만.*감\s*안\s*죽", re.I)
_BUSINESS = re.compile(r"주가|목표주가|시가총액|공모주|환율|사막화|유엔서\s*수상|부동산|투자\s*유치|영업이익|매출\s*전망")
_EVENTS = ("개봉", "공개", "첫방", "캐스팅", "제작 확정", "제작확정", "출연 확정", "출연확정", "예매율", "시청률", "종영", "리메이크", "인터뷰", "영화제", "수상")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return str(value)
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(value)).strip()


def _key(value):
    return re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", _text(value)).lower())


def _date(value):
    if not _text(value):
        return None
    try:
        result = pd.Timestamp(value)
        if pd.isna(result):
            return None
        return result.tz_localize("Asia/Seoul") if result.tzinfo is None else result.tz_convert("Asia/Seoul")
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _count(text, pattern):
    match = re.search(pattern, text, re.I)
    return _number(match.group(1)) if match else None


def _host(url):
    try:
        return (urlparse(_text(url)).hostname or "").lower()
    except ValueError:
        return ""


def _domain(host, domain):
    return host == domain or host.endswith("." + domain)


def _valid_url(url):
    try:
        parsed = urlparse(_text(url))
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _bad_url(url):
    path = urlparse(_text(url)).path.lower() if _valid_url(url) else ""
    return bool(re.search(r"unsupportedbrowser|(?:^|/)(?:404|error|notfound|login|signin|accessdenied)(?:/|$)", path))


def classify_source(row):
    """Classify actual provenance, never an RSS query's self-declared 'official' label."""
    host, source, kind = _host(row.get("source_url")), _text(row.get("source")), _text(row.get("source_type")).lower()
    if _domain(host, "youtube.com") or host == "youtu.be" or "youtube" in source.lower() or "유튜브" in source or kind in {"youtube", "youtube_reaction"}:
        return YOUTUBE
    if any(word in source.upper() for word in ("KOBIS", "KOFIC")) or kind == "boxoffice" or _domain(host, "kobis.or.kr"):
        return BOXOFFICE
    if "netflix top 10" in source.lower() or "넷플릭스 top 10" in source.lower() or kind == "ott_ranking":
        return OTT
    if any(_domain(host, domain) for domain in _OFFICIAL_OTT_HOSTS):
        return OTT
    return NEWS


def _clean_title(value):
    value = html.unescape(unicodedata.normalize("NFKC", _text(value)))
    value = re.sub(r"[\U0001F000-\U0001FFFF\u2600-\u27FF]", "", value).strip(" #\"'`‘’“”《》〈〉≪≫[]")
    value = re.sub(r"\s+관련\s*(?:YouTube|유튜브)?\s*(?:화제 영상|반응.*)$", "", value, flags=re.I)
    value = re.sub(r"\s*:\s*(?:Limited Series|Season\s*\d+|Part\s*\d+).*$", "", value, flags=re.I)
    value = re.sub(r"\s*(?:시즌\s*\d+|season\s*\d+)$", "", value, flags=re.I).strip()
    return _ALIASES.get(_key(value), value)


def _title_valid(value):
    normalized = _key(value)
    if normalized in _GENERIC or not normalized or len(value) > 48:
        return False
    if re.fullmatch(r"(?:제?\d+(?:회|화|위)|ep\d+)\s*(?:예고|선공개|full|하이라이트)?", normalized, re.I):
        return False
    if re.search(r"영화\s*역사상|관련.*화제\s*영상|배우들의.*사실|누리꾼|^중국\s*사막화", value):
        return False
    if "#" in value or len(value.split()) > 6:
        return False
    if re.search(r"(?:습니다|는데요|뽑았습니다|드립니다)[.!?…]*$", value):
        return False
    return True


def canonical_title(row):
    """Prefer explicit title delimiters over captions; translate only curated aliases."""
    related = _text(row.get("related_content"))
    title = _text(row.get("issue_title"))
    description = _text(row.get("description"))
    publisher = _text(row.get("publisher"))
    if classify_source(row) == NEWS:
        # A film mentioned as the record being beaten is not the article's
        # subject. Keep the collector's corrected subject through assessment.
        quoted = r"(?:‘[^’]+’|“[^”]+”|'[^']+'|\"[^\"]+\"|`[^`]+`|《[^》]+》|≪[^≫]+≫|〈[^〉]+〉|\[[^\]]+\])"
        comparison = quoted + r"\s*(?:[을를]\s*)?(?:넘어선|넘었다|넘고|넘는|넘어|제치고|제쳤다|앞선|따라잡은)"
        targets = [_key(_clean_title(re.match(quoted, match.group()).group()))
                   for match in re.finditer(comparison, title)]
        title = re.sub(comparison, " ", title)
        description = re.sub(comparison, " ", description)
        if _key(_clean_title(related)) in targets:
            related = ""
    if classify_source(row) == YOUTUBE:
        promotion = re.search(r"(?:^|[|｜])\s*([^|｜\[\]‘’\"']+?)\s+홍보하러\s+온", title + " " + description)
        if promotion:
            candidate = _clean_title(promotion.group(1))
            if _title_valid(candidate):
                return candidate
        # Channel names and advertising text are not a work title.
        parts = re.split(r"채널\s*[:：]", description, maxsplit=1)
        description = parts[0]
        publisher += " " + (parts[1] if len(parts) > 1 else "")
    patterns = [r"《([^》]+)》", r"≪([^≫]+)≫", r"〈([^〉]+)〉", r"\[([^\]]+)\]"]
    # YouTube descriptions contain quoted video headlines; only strict inner title
    # delimiters are accepted there, not the whole quoted caption.
    for pattern in patterns:
        for text in (related, title, description):
            for match in re.finditer(pattern, text):
                candidate = _clean_title(match.group(1))
                if _title_valid(candidate):
                    return candidate
    if classify_source(row) != YOUTUBE:
        for match in re.finditer(r"[‘'\"“`]([^’'\"”`]+)[’'\"”`]", title):
            candidate = _clean_title(match.group(1))
            if _title_valid(candidate):
                return candidate
    candidate = _clean_title(related)
    if classify_source(row) == YOUTUBE and any(_key(candidate) == _key(label) for label in re.findall(r"\[([^\]]+)\]", publisher)):
        return ""
    return candidate if _title_valid(candidate) else ""


def _metrics(row, group):
    raw = row.get("metrics_json")
    try:
        metrics = dict(raw) if isinstance(raw, Mapping) else json.loads(_text(raw) or "{}")
    except (ValueError, TypeError):
        metrics = {}
    if not isinstance(metrics, dict):
        metrics = {}
    desc = _text(row.get("description"))
    if group == YOUTUBE:
        for output, names in {"views_total": ("view_count", "views"), "comments_total": ("comment_count", "comments"), "views_change": ("daily_view_delta", "views_delta"), "comments_change": ("daily_comment_delta",)}.items():
            if output not in metrics:
                for name in names:
                    if name in metrics:
                        metrics[output] = metrics[name]
                        break
        for name, pattern in {"views_total": r"(?<!평균 )조회수\s*(?:약\s*)?([0-9,]+)", "comments_total": r"댓글\s*([0-9,]+)", "age_days": r"공개\s*([0-9,]+)일"}.items():
            if metrics.get(name) is None:
                metrics[name] = _count(desc, pattern)
    elif group == BOXOFFICE:
        for name, pattern in {"rank": r"박스오피스\s*(\d+)위", "daily_audience": r"일일\s*관객\s*([0-9,]+)", "rank_change": r"전일\s*대비\s*순위\s*변화\s*([+-]?[0-9,]+)"}.items():
            if metrics.get(name) is None:
                metrics[name] = _count(desc, pattern)
    elif group == OTT:
        if metrics.get("rank") is None:
            metrics["rank"] = _count(desc, r"한국\s*(\d+)위") or _count(_text(row.get("issue_title")), r"(?:Top\s*10|박스오피스)\s*(\d+)위")
        if not metrics.get("category"):
            match = re.search(r"카테고리:\s*([^.;]+)", desc)
            metrics["category"] = match.group(1).strip() if match else ""
    return metrics


def read_issue_metrics(row):
    """Return the source's stored measurements, including supported legacy formats."""
    return _metrics(row, classify_source(row))


def _current_popular_snapshot(metrics, now):
    """Only a genuine, fresh API list entry proves current list membership.

    Search origins and last_discovered_at are deliberately insufficient.  A
    position is scoped to the returned category list, never a YouTube-wide rank.
    """
    raw = _text(metrics.get("popular_snapshot_at"))
    snapshot = _date(raw)
    position = _number(metrics.get("popular_list_position"))
    complete = metrics.get("popular_snapshot_complete")
    if (snapshot is None or ":" not in raw or _text(metrics.get("popular_region")).upper() != "KR"
            or _text(metrics.get("popular_category_id")) not in {"all", "1", "24"}
            or position is None or not position.is_integer() or not 1 <= position <= 50
            or str(complete).lower() not in {"true", "1"}):
        return None
    if snapshot.normalize() != now.normalize() or snapshot > now + pd.Timedelta(minutes=1) or now - snapshot > pd.Timedelta(hours=36):
        return None
    return snapshot


def _youtube_comparison(metrics, now):
    start, end = _date(metrics.get("observation_start")), _date(metrics.get("observation_end"))
    valid = start is not None and end is not None and end > start and end <= now + pd.Timedelta(minutes=1)
    days = (end - start).total_seconds() / 86400 if valid else None
    views, comments = _number(metrics.get("views_change")), _number(metrics.get("comments_change"))
    daily_views = views / days if views is not None and days else None
    daily_comments = comments / days if comments is not None and days else None
    growth = _number(metrics.get("growth_rate"))
    qualifies = bool(valid and (
        (daily_views is not None and daily_views >= DAILY_VIEW_DELTA_THRESHOLD)
        or (daily_comments is not None and daily_comments >= DAILY_COMMENT_DELTA_THRESHOLD)
        or (growth is not None and growth >= GROWTH_RATE_THRESHOLD and daily_views is not None and daily_views >= GROWTH_MIN_DELTA)
    ))
    return start, end, valid, daily_views, daily_comments, qualifies


def _rss_news_provenance(row, metrics):
    """RSS provides article attribution and publication, not body verification."""
    publisher_host = _host(metrics.get("publisher_url"))
    rss_url = _text(metrics.get("rss_url"))
    return (metrics.get("news_provenance") == "google_news_rss"
            and bool(_text(row.get("publisher"))) and _date(row.get("published_at")) is not None
            and _valid_url(metrics.get("publisher_url")) and not _domain(publisher_host, "news.google.com")
            and _valid_url(rss_url) and _domain(_host(rss_url), "news.google.com")
            and urlparse(rss_url).path.startswith(("/rss/articles/", "/articles/")))


def _reference(row, group, metrics):
    desc = _text(row.get("description"))
    explicit = _date(row.get("reference_date"))
    extracted = None
    if group == OTT and ("top 10" in _text(row.get("source")).lower() or _text(row.get("source_type")) == "ott_ranking"):
        match = re.search(r"집계\s*(?:주간|기간|주)\s*[:：]?\s*([0-9\-~ ./]+)", desc)
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", match.group(1)) if match else []
        extracted = _date(dates[-1]) if dates else _date(metrics.get("week"))
    elif group == BOXOFFICE:
        match = re.search(r"(\d{4}-\d{2}-\d{2})\s*KOBIS", desc)
        extracted = _date(match.group(1)) if match else None
    mismatch = bool(explicit is not None and extracted is not None and explicit.date() != extracted.date())
    published = _date(row.get("published_at"))
    collected = _date(row.get("collected_at")) or _date(row.get("date"))
    if group == YOUTUBE and published is None:
        age = _number(metrics.get("age_days"))
        if age is not None and age >= 1 and collected is not None:
            published = collected.normalize() - pd.Timedelta(days=int(age) - 1)
    if group == YOUTUBE:
        reference = published or explicit or collected
    elif group in {BOXOFFICE, OTT}:
        reference = extracted or explicit or published or collected
    else:
        reference = published or explicit or collected
    return reference, published, collected, mismatch


def _format_count(value):
    return f"{int(value):,}"


def _season(row, metrics):
    """Preserve explicit seasons/parts; never infer an unknown season as season 1."""
    raw = _text(metrics.get("season"))
    if raw.isdigit():
        return "season", int(raw)
    for text in (raw, _text(row.get("related_content")), _text(row.get("issue_title"))):
        match = re.search(r"(?:시즌|season)\s*(\d+)", text, re.I)
        if match:
            return "season", int(match.group(1))
        match = re.search(r"\bpart\s*(\d+)", text, re.I)
        if match:
            return "part", int(match.group(1))
    return "", None


def _assess_row(original, now, allow_popular=True):
    row = dict(original)
    group = classify_source(row)
    metrics = _metrics(row, group)
    popular_snapshot = _current_popular_snapshot(metrics, now) if group == YOUTUBE and allow_popular else None
    comparison = _youtube_comparison(metrics, now) if group == YOUTUBE else None
    raw_url = _text(row.get("source_url"))
    original_title = _text(row.get("related_content"))
    title = canonical_title(row)
    notes, excluded = [], []
    if _text(row.get("quality_notes")):
        notes.append(_text(row["quality_notes"]))
    restored = False
    if group == OTT and "netflix" in _text(row.get("source")).lower() and _domain(_host(raw_url), "netflix.com") and _bad_url(raw_url):
        row["source_url"] = NETFLIX_REFERENCE_URL
        restored = True
        notes.append("Netflix 오류 페이지를 공식 집계 안내 주소로 복원; 실시간 원문 재대조 전")
    url = _text(row.get("source_url"))
    host = _host(url)
    official = (group == BOXOFFICE and (_domain(host, "kobis.or.kr") or _domain(host, "kofic.or.kr"))) or (group == OTT and any(_domain(host, domain) for domain in _OFFICIAL_OTT_HOSTS))
    if group == NEWS and "OTT 공식" in _text(row.get("source")):
        row["source_original"] = row["source"]
        row["source"] = "OTT 관련 뉴스"
        notes.append("공식 플랫폼 원문이 아닌 기사로 분류")
    if not _valid_url(url):
        excluded.append("원문 URL 없음 또는 유효하지 않음")
    elif _bad_url(url):
        excluded.append("근거 URL이 오류 또는 로그인 페이지")
    relay_news = group == NEWS and _domain(host, "news.google.com")
    rss_news = group == NEWS and _rss_news_provenance(row, metrics)
    if _domain(host, "news.google.com") and not rss_news:
        excluded.append("뉴스 중계 링크만 있어 원문 미확인")
    if not title:
        excluded.append("작품명이 없거나 일반 문구라 작품 특정 불가")
    content_text = " ".join((_text(row.get("issue_title")), original_title, _text(row.get("description"))))
    if group == YOUTUBE:
        if _GAME.search(content_text) and not _ADAPTATIONS.search(title):
            excluded.append("게임 플레이·게임 방송 이슈")
        if _MUSIC.search(content_text):
            excluded.append("일반 음악·뮤직비디오 이슈")
        if re.search(r"\b(?:MLB|NLCS|NBA|EPL)\b|경기의 흐름|배구\s*경기|경기\s*하이라이트", content_text, re.I) and not _SPORT_VARIETY.search(title):
            excluded.append("일반 스포츠 경기 이슈")
    if group == NEWS and _BUSINESS.search(content_text):
        excluded.append("영상 콘텐츠 기획과 무관한 업계·일반 뉴스")
    if _text(row.get("quality_status")).lower() == "rejected":
        excluded.append("수집 단계 품질 검토에서 제외")
    season_kind, season_number = _season(row, metrics)
    title_key = _key(title)
    if title and season_number is not None:
        title += f" {'시즌' if season_kind == 'season' else '파트'} {season_number}"
    reference, published, collected, mismatch = _reference(row, group, metrics)
    if group == YOUTUBE:
        # Upload date must not hide an older clip that is popular or receiving a
        # qualifying measured reaction now.  A mere new collection is not enough.
        if popular_snapshot is not None:
            reference = popular_snapshot
        elif comparison[5]:
            reference = comparison[1]
        elif published is None:
            excluded.append("초기 반응 영상의 공개일을 확인할 수 없음")
    weekly = group == OTT and ("top 10" in _text(row.get("source")).lower() or _text(row.get("source_type")) == "ott_ranking")
    if reference is None:
        excluded.append("기준일을 확인할 수 없음")
    elif reference.normalize() > now.normalize():
        excluded.append("기준일이 미래 날짜")
    elif (now.normalize() - reference.normalize()).days >= (NETFLIX_MAX_AGE_DAYS if weekly else ISSUE_LOOKBACK_DAYS):
        excluded.append("집계 기준일이 오래됨" if weekly or group == BOXOFFICE else "최근 7일 범위 밖의 게시물")
    if collected is not None and collected.normalize() > now.normalize():
        excluded.append("수집일이 미래 날짜")
    if published is not None and published.normalize() > now.normalize():
        excluded.append("게시일이 미래 날짜")
    if group == NEWS:
        if published is None:
            excluded.append("기사의 실제 발행일을 확인할 수 없음")
        if not _text(row.get("publisher")):
            excluded.append("기사 발행처를 확인할 수 없음")
    if mismatch:
        excluded.append("메타데이터와 설명의 집계 기준일 불일치")
    if weekly and not re.search(r"집계\s*(?:주간|기간|주)", _text(row.get("description"))) and not _text(row.get("reference_date")) and not metrics.get("week"):
        excluded.append("주간 순위의 실제 집계주 미확인")
    if group == BOXOFFICE and not official:
        excluded.append("공식 박스오피스 출처 미확인")
    if weekly and not official:
        excluded.append("공식 주간 순위 출처 미확인")

    ref_text = reference.strftime("%Y-%m-%d") if reference is not None else "미확인"
    evidence = "공식 출처 기록" if official else "원문 링크 있음 · 내용 재대조 전"
    if rss_news:
        evidence = "기사 제목·발행처·발행일 기록" + (" · Google 뉴스 연결" if relay_news else "")
    if restored or "복원" in _text(row.get("quality_notes")):
        evidence = "공식 집계 기록 · 원문 재대조 전"
    strength, summary, reason, priority, magnitude, rank = "사건 정보", "반응 수치 미측정", "최근 콘텐츠 사건의 원문 근거", 1, 0.0, 0.0
    reference_label = f"게시 기준 {ref_text}"
    if group == YOUTUBE:
        total, comments = _number(metrics.get("views_total")), _number(metrics.get("comments_total"))
        change = _number(metrics.get("views_change"))
        start, end, valid_period, daily_views, daily_comments, measured_candidate = comparison
        if change is not None and change > 0 and valid_period:
            days = (end - start).total_seconds() / 86400
            magnitude = change / days
            priority = 4 if magnitude >= YOUTUBE_PRIORITY_VIEW_DELTA else 1
            strength = "관측기간 증가 확인" if priority == 4 else "관측 증가 확인 · 참고"
            precision = _text(metrics.get("observation_precision"))
            approximate = precision == "date" or ("T" not in _text(metrics.get("observation_start")) and ":" not in _text(metrics.get("observation_start")))
            period = f"{start.strftime('%Y-%m-%d')}→{end.strftime('%Y-%m-%d')}"
            summary = f"관측기간 조회수 +{_format_count(change)}"
            if approximate:
                summary += " · 날짜 단위 관측(정확한 24시간 증가 아님)"
            else:
                summary += f" · {days * 24:g}시간 관측"
            reason = "동일 영상의 서로 다른 관측치에서 실제 조회수 증가가 확인됨"
            reason += "; 관측기간 평균 하루 2만 회 이상인 우선 반응 후보" if priority == 4 else "; 관측기간 평균 하루 2만 회 미만으로 참고 반응"
            public_date = published.strftime("%Y-%m-%d") if published is not None else "미확인"
            reference_label = f"공개 {public_date} · 관측 {period}"
        else:
            strength = "초기 반응 · 증가 미확인"
            parts = []
            if total is not None and total >= 0:
                parts.append(f"누적 조회수 {_format_count(total)}")
            if comments is not None and comments >= 0:
                parts.append(f"누적 댓글 {_format_count(comments)}")
            summary = " · ".join(parts) or "반응 수치 미측정"
            reason = "현재 누적 관측값만 있음; 일일 증가나 급등으로 해석하지 않음"
            if not parts and popular_snapshot is None:
                excluded.append("YouTube 반응 수치를 확인할 수 없음")
            if change is not None and change < 0:
                notes.append("관측 수치 감소: 증가 신호로 사용하지 않음")
                strength = "관측 수치 감소 · 참고"
                reason = "관측값이 감소하여 증가 신호로 사용하지 않음"
            elif change == 0 and valid_period:
                strength = "관측 증가 없음 · 참고"
                reason = "관측 사이 조회수 증가가 없음"
            reference_label = f"공개 기준 {ref_text} · 증가 비교 미확인"
        if measured_candidate and daily_comments is not None and daily_comments >= DAILY_COMMENT_DELTA_THRESHOLD:
            priority = 4
            if daily_views is None or daily_views < YOUTUBE_PRIORITY_VIEW_DELTA:
                strength = "댓글 증가 확인"
                summary = f"관측기간 댓글 +{_format_count(_number(metrics.get('comments_change')))}"
                reason = f"동일 영상의 서로 다른 집계에서 하루 평균 댓글 {_format_count(DAILY_COMMENT_DELTA_THRESHOLD)}개 이상 증가"
                reference_label = f"댓글 비교 {start.strftime('%Y-%m-%d')}→{end.strftime('%Y-%m-%d')}"
        if popular_snapshot is not None:
            category = {"all": "전체", "1": "영화·애니메이션", "24": "엔터테인먼트"}[_text(metrics.get("popular_category_id"))]
            priority, strength = 4, "YouTube 한국 인기 영상 목록 포함"
            summary = f"한국 {category} 인기 영상 목록 포함"
            reason = "YouTube가 제공한 한국 인기 영상 목록에 실제 포함된 작품 관련 영상"
            reference_label = f"인기 목록 확인 {popular_snapshot.strftime('%Y-%m-%d %H:%M')} (한국 시간)"
    elif group == BOXOFFICE:
        audience, rank_value = _number(metrics.get("daily_audience")), _number(metrics.get("rank"))
        reference_label = f"일일 관객 집계 {ref_text}"
        if audience is not None and audience >= 0 and rank_value is not None and 1 <= rank_value <= 10:
            priority, rank, strength = 3, rank_value, "공식 일일 순위"
            summary = f"일일 관객 {_format_count(audience)}명 · {int(rank_value)}위"
            reason = "유효한 기준일의 KOBIS 실제 일일 관객·순위 기록"
        else:
            excluded.append("일일 관객 또는 공식 순위 수치 미확인")
    elif weekly:
        rank_value = _number(metrics.get("rank"))
        reference_label = f"주간 집계 {ref_text}"
        if rank_value is not None and 1 <= rank_value <= 10:
            priority, rank, strength = 3, rank_value, "공식 주간 순위"
            category = _text(metrics.get("category"))
            summary = f"한국 주간 Top 10 {int(rank_value)}위" + (f" · {category}" if category else "")
            reason = "주간 집계 기록; 실시간 인기나 오늘의 순위로 해석하지 않음"
        else:
            excluded.append("주간 순위 수치 미확인")
    else:
        event_text = _text(row.get("issue_title")) or content_text
        event = {"new_release": "신작 공개", "release": "개봉" if "개봉" in event_text else "공개", "casting": "캐스팅", "production_confirmed": "제작 확정", "production": "제작", "audience": "예매율" if "예매율" in event_text else "시청률" if "시청률" in event_text else "흥행", "award": "수상·영화제", "finale": "종영", "interview": "인터뷰"}.get(_text(row.get("event_type")), "")
        if not event:
            event = next((word for word in _EVENTS if word in content_text), "")
        if event:
            priority = 2
            strength = "공식 발표" if official else "콘텐츠 사건 보도"
            summary = f"{event} 관련 사건 · 반응 수치 미측정"
            reason = "게시일이 유효한 콘텐츠 사건; 기사 반복 수를 인기 점수로 사용하지 않음"
        else:
            excluded.append("구체적인 콘텐츠 사건을 확인할 수 없음")
        if group == NEWS and published is None:
            reference_label = f"기록 기준 {ref_text} · 발행일 미대조"
    if excluded:
        evidence = "검토 필요"
    observed_at = popular_snapshot or (comparison[1] if comparison is not None and comparison[2] else None) or collected or reference
    row.update({
        "related_content_original": original_title, "related_content": title,
        "source_group": group, "issue_key": f"work:{title_key}" + (f":{season_kind}:{season_number}" if season_number is not None else "") if title else "",
        "eligible": not excluded, "exclusion_reason": "; ".join(dict.fromkeys(excluded)),
        "strength_label": strength, "evidence_status": evidence, "selection_reason": reason,
        "reference_label": reference_label, "metric_summary": summary,
        "sort_priority": priority if not excluded else 0, "quality_notes": "; ".join(dict.fromkeys(notes)),
        "comparison_valid": valid_period if group == YOUTUBE else False,
        "popular_valid": popular_snapshot is not None,
        "confirmed_routes": group, "confirmed_route_count": 1, "issue_count": 1,
        "_quality_reference": reference.isoformat() if reference is not None else "",
        "_quality_observed_at": observed_at.isoformat() if observed_at is not None else "",
        "_quality_magnitude": magnitude, "_quality_rank": rank,
        "_quality_snapshot_group": f"{group}:{_key(metrics.get('category', ''))}" if weekly else BOXOFFICE if group == BOXOFFICE else "",
    })
    return row


def assess_issues(df, now=None):
    """Preserve every input row/index and add quality decisions (KST calendar windows).

    Daily/news/reaction evidence must be within seven calendar dates. A current
    popular-list entry must be from today's actual API snapshot; an older upload
    can qualify through this list or a fresh measured reaction. Weekly Netflix
    evidence gets a fourteen-date window for its weekly publication cadence; the
    actual reference week is mandatory, so a fresh collection date cannot renew it.
    """
    frame = df.copy()
    for column in _BASE_COLUMNS + _META_COLUMNS:
        if column not in frame:
            frame[column] = ""
    clock = _date(now) if now is not None else pd.Timestamp.now(tz="Asia/Seoul")
    if clock is None:
        raise ValueError("now must be a valid date or timestamp")
    if frame.empty:
        for column in _OUTPUT_COLUMNS:
            frame[column] = pd.Series(index=frame.index, dtype=bool if column in {"eligible", "comparison_valid", "popular_valid"} else object)
        return frame
    originals = frame.to_dict("records")
    latest_popular = {}
    for row in originals:
        if classify_source(row) != YOUTUBE:
            continue
        metrics = _metrics(row, YOUTUBE)
        snapshot = _current_popular_snapshot(metrics, clock)
        if snapshot is not None:
            category = _text(metrics.get("popular_category_id"))
            latest_popular[category] = max(latest_popular.get(category, snapshot), snapshot)
    decisions = []
    for row in originals:
        metrics = _metrics(row, YOUTUBE) if classify_source(row) == YOUTUBE else {}
        snapshot = _current_popular_snapshot(metrics, clock) if metrics else None
        # A complete newer response supersedes older membership in the same
        # category, even when a previously listed work has disappeared.
        current_list = snapshot is None or snapshot >= latest_popular[_text(metrics.get("popular_category_id"))]
        decisions.append(_assess_row(row, clock, allow_popular=current_list))
    assessed = pd.DataFrame(decisions, index=frame.index)
    assessed["eligible"] = assessed["eligible"].astype(bool)
    # An old Top 10 entry must not masquerade as a current rank after it exits
    # the latest available source/category snapshot. Daily data and each weekly
    # category are independent; no cross-source quota or feed-count bonus exists.
    candidates = assessed[assessed["eligible"] & assessed["_quality_snapshot_group"].ne("")]
    latest = candidates.groupby("_quality_snapshot_group")["_quality_reference"].max().to_dict()
    for index, row in assessed.iterrows():
        group = row.get("_quality_snapshot_group", "")
        if row["eligible"] and group in latest and row["_quality_reference"] < latest[group]:
            assessed.at[index, "eligible"] = False
            assessed.at[index, "exclusion_reason"] = "이전 집계(최신 집계로 대체)"
            assessed.at[index, "evidence_status"] = "검토 필요"
            assessed.at[index, "sort_priority"] = 0
    return assessed


def _canonical_url(url):
    if not _valid_url(url):
        return ""
    parsed = urlparse(url)
    host = _host(url)
    if host == "youtu.be":
        return "youtube:" + parsed.path.strip("/").split("/")[0]
    if _domain(host, "youtube.com"):
        video = parse_qs(parsed.query).get("v", [""])[0]
        if not video and parsed.path.startswith(("/shorts/", "/embed/")):
            video = parsed.path.strip("/").split("/")[-1]
        if video:
            return "youtube:" + video
    query = [(key, value) for key, values in parse_qs(parsed.query).items() if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "ref"} for value in values]
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", urlencode(sorted(query)), ""))


def _evidence_key(row):
    group = row.get("source_group")
    if group in {BOXOFFICE, OTT}:
        return f"{group}:{row.get('issue_key')}:{_host(row.get('source_url'))}"
    if group == NEWS:
        headline = re.sub(r"\s+-\s+[^-]+$", "", _text(row.get("issue_title")))
        if headline:
            return "news:" + _key(headline)
    return _canonical_url(_text(row.get("source_url")))


def _rank_tuple(row):
    priority = int(row.get("sort_priority", 0))
    rank = _number(row.get("_quality_rank")) or 0
    magnitude = _number(row.get("_quality_magnitude")) or 0
    return (priority, magnitude if priority == 4 else -rank if priority == 3 else 0, _text(row.get("_quality_reference")), _text(row.get("issue_key")))


def build_ranked_issues(assessed):
    """One row per canonical work, with unique traceable evidence and no feed bonus."""
    if assessed.empty or "eligible" not in assessed:
        result = assessed.iloc[0:0].copy()
        result["evidence_records"] = pd.Series(dtype=object)
        return result
    eligible = assessed[assessed["eligible"].fillna(False).astype(bool)].copy()
    if eligible.empty:
        eligible["evidence_records"] = pd.Series(index=eligible.index, dtype=object)
        return eligible
    representatives = []
    for _, group in eligible.groupby("issue_key", sort=False):
        unique = {}
        for row in group.to_dict("records"):
            identity = _evidence_key(row)
            previous = unique.get(identity)
            # A source's newer observation replaces its older rank, never vice versa.
            freshness = row.get("_quality_observed_at") if row.get("source_group") == YOUTUBE else row.get("_quality_reference")
            previous_freshness = (previous.get("_quality_observed_at") if previous.get("source_group") == YOUTUBE else previous.get("_quality_reference")) if previous else ""
            if previous is None or (_text(freshness), _rank_tuple(row)) > (_text(previous_freshness), _rank_tuple(previous)):
                unique[identity] = row
        rows = sorted(unique.values(), key=_rank_tuple, reverse=True)
        representative = dict(rows[0])
        routes = [route for route in SOURCE_GROUPS if any(row.get("source_group") == route for row in rows)]
        representative.update({"confirmed_routes": " · ".join(routes), "confirmed_route_count": len(routes), "issue_count": len(rows)})
        representative["evidence_records"] = [{
            "title": _text(row.get("issue_title")), "url": _text(row.get("source_url")),
            "source": _text(row.get("source")), "reference_label": _text(row.get("reference_label")),
            "metric_summary": _text(row.get("metric_summary")), "evidence_status": _text(row.get("evidence_status")),
        } for row in rows]
        if not _valid_url(representative.get("image_url", "")):
            image_row = next((row for row in rows if _valid_url(row.get("image_url", ""))), None)
            if image_row:
                representative["image_url"] = image_row["image_url"]
                representative["image_source_url"] = image_row["source_url"]
        representatives.append(representative)
    return pd.DataFrame(sorted(representatives, key=_rank_tuple, reverse=True)).reset_index(drop=True)
