"""B tv+ 외부 콘텐츠 이슈 통합 수집기.

수집 경로
- YouTube 반응: 한국 인기 영상, 주요 공식 채널 신규 업로드, 12개 외부 반응 검색어
- 극장·박스오피스: KOBIS 일별 박스오피스
- OTT 랭킹·신작: Netflix 공식 한국 Top 10과 OTT 공식 신작 관련 자료
- 온라인 화제·뉴스: 작품의 개봉·공개·제작 소식과 공식 발표

필수 환경변수는 YOUTUBE_API_KEY이며, 극장 데이터를 사용할 때 KOBIS_API_KEY를 추가합니다.
키가 없는 경로는 건너뛰고 나머지 수집은 계속 진행합니다.
"""

from __future__ import annotations

import csv
import argparse
from copy import deepcopy
import html
import json
import math
import os
import re
import time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, urlparse, parse_qsl, urlencode

import feedparser
import pandas as pd
import requests

from issue_rules import (
    DAILY_COMMENT_DELTA_THRESHOLD, DAILY_VIEW_DELTA_THRESHOLD,
    GROWTH_MIN_DELTA, GROWTH_RATE_THRESHOLD, ISSUE_LOOKBACK_DAYS,
    NETFLIX_MAX_AGE_DAYS, NEW_VIDEO_HIGH_VIEW_THRESHOLD,
    NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD,
)

try:
    from googlenewsdecoder import gnewsdecoder
except Exception:
    gnewsdecoder = None

try:
    import yt_dlp
except Exception:
    yt_dlp = None


ISSUE_PATH = Path("issue_feed.csv")
YOUTUBE_WATCHLIST_PATH = Path("youtube_video_watchlist.csv")
YOUTUBE_STATS_PATH = Path("youtube_video_stats.csv")
COLLECTION_STATUS_PATH = Path("collection_status.json")

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"
KOBIS_DAILY_ENDPOINT = "https://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
NETFLIX_COUNTRY_DATA_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.xlsx"

DAYS = ISSUE_LOOKBACK_DAYS
MAX_VIDEO_TRACK_DAYS = int(os.getenv("YOUTUBE_TRACK_DAYS", "30"))
MAX_TRACKING_VIDEOS = int(os.getenv("YOUTUBE_MAX_TRACKING_VIDEOS", "500"))
YOUTUBE_MAX_RESULTS_PER_QUERY = max(
    1, min(int(os.getenv("YOUTUBE_MAX_RESULTS_PER_QUERY", "8")), 50)
)
REQUEST_TIMEOUT_SECONDS = 20
ARTICLE_IMAGE_FETCH_LIMIT = max(0, int(os.getenv("ARTICLE_IMAGE_FETCH_LIMIT", "60")))
ARTICLE_IMAGE_TIMEOUT_SECONDS = max(3, int(os.getenv("ARTICLE_IMAGE_TIMEOUT_SECONDS", "8")))
NEWS_ENTRIES_PER_QUERY = 12
NEWS_MAX_CANDIDATES = 96

ISSUE_COLUMNS = [
    "date",
    "source",
    "issue_title",
    "related_content",
    "keywords",
    "description",
    "source_url",
    "image_url",
    "published_at", "collected_at", "reference_date", "reference_period",
    "publisher", "source_type", "content_id", "event_type", "metrics_json",
    "quality_status", "quality_notes",
]

WATCHLIST_COLUMNS = [
    "video_id",
    "title",
    "channel",
    "url",
    "first_seen_date",
    "upload_date",
    "related_content",
    "keywords",
    "query",
    "description",
    "duration_seconds",
    "video_type",
    "category_id", "channel_id", "last_discovered_at",
    "popular_snapshot_at", "popular_region", "popular_list_position",
    "popular_category_id", "popular_snapshot_complete",
]

STATS_COLUMNS = [
    "date",
    "video_id",
    "view_count",
    "like_count",
    "comment_count",
    "observed_at",
]

COLLECTION_STATUS = {"schema_version": 1, "sources": {}}
SOURCE_ERRORS = {}
OFFICIAL_DOMAINS = (
    "netflix.com", "tving.com", "wavve.com", "disneyplus.com",
    "thewaltdisneycompany.com", "coupangplay.com", "news.coupang.com",
    "sbs.co.kr", "imbc.com", "kbs.co.kr", "jtbc.co.kr", "cjenm.com",
)


def utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_error(error):
    """Keep service diagnostics useful without persisting credential-bearing URLs."""
    message = str(error)
    message = re.sub(r"([?&](?:key|api_key|token|access_token)=)[^&\s]+", r"\1[redacted]", message, flags=re.I)
    for name in ("YOUTUBE_API_KEY", "KOBIS_API_KEY"):
        value = os.getenv(name, "")
        if value:
            message = message.replace(value, "[redacted]")
    return message[:500]


def source_error(source, error):
    SOURCE_ERRORS.setdefault(source, []).append(safe_error(error))


def source_status(source, state, count=0, **details):
    previous = COLLECTION_STATUS["sources"].get(source, {})
    errors = SOURCE_ERRORS.get(source, [])
    if errors and state in {"ok", "empty"}:
        state = "partial" if count else "failed"
    entry = {
        **previous, "state": state, "count": int(count), "collected_at": utc_now_iso(),
        "reference_date": "", "reference_period": "", "error": "; ".join(errors[-5:]),
    }
    if previous.get("last_success_at"):
        entry["last_success_at"] = previous["last_success_at"]
    entry.update(details)
    if state in {"ok", "empty"}:
        entry["last_success_at"] = entry["collected_at"]
    COLLECTION_STATUS["sources"][source] = entry
    return entry


def source_key(row):
    source = str(row.get("source", ""))
    kind = str(row.get("source_type", ""))
    if "YouTube" in source or "유튜브" in source or kind == "youtube_reaction":
        return "youtube"
    if "KOBIS" in source or kind == "boxoffice":
        return "kobis"
    if "Netflix Top 10" in source or kind == "ott_ranking":
        return "netflix"
    return "news"


def is_official_url(url):
    host = (urlparse(str(url)).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS)


def metadata_row(row, **metadata):
    result = {column: row.get(column, "") for column in ISSUE_COLUMNS}
    result.update(metadata)
    if isinstance(result.get("metrics_json"), dict):
        result["metrics_json"] = json.dumps(result["metrics_json"], ensure_ascii=False, sort_keys=True)
    return result

NEWS_QUERY_GROUPS = [
    # 온라인 화제·뉴스: 큐레이션 기회로 연결하기 쉬운 사건성 신호 중심
    ("온라인 화제·뉴스", "드라마 (신작 OR 첫방 OR 공개)"),
    ("온라인 화제·뉴스", "예능 (신작 OR 첫방 OR 공개)"),
    ("온라인 화제·뉴스", "영화 (개봉 OR 신작)"),
    ("온라인 화제·뉴스", "(드라마 OR 예능 OR 영화) (캐스팅 OR 출연확정)"),
    ("온라인 화제·뉴스", "(시즌2 OR 후속편) (제작 OR 확정)"),
    ("온라인 화제·뉴스", "(웹툰 OR 웹소설) (영상화 OR 제작)"),
    ("온라인 화제·뉴스", "(드라마 OR 예능) 시청률"),
    ("온라인 화제·뉴스", "(드라마 OR 영화) 역주행"),
    ("온라인 화제·뉴스", "(영화 OR 드라마) (수상 OR 영화제)"),
    ("온라인 화제·뉴스", "(드라마 OR 예능) (종영 OR 결말)"),
    ("온라인 화제·뉴스", "(영화 OR 드라마) 리메이크"),
    ("온라인 화제·뉴스", "(배우 OR 감독) 신작 인터뷰"),

    # OTT 신작·공개 예정 관련 자료. 실제 순위는 Netflix 공식 데이터에서 별도 수집합니다.
    ("OTT 공식 신작", "넷플릭스 (신작 OR 공개 OR 제작)"),
    ("OTT 공식 신작", "티빙 (오리지널 OR 신작 OR 공개)"),
    ("OTT 공식 신작", "웨이브 (오리지널 OR 신작 OR 공개)"),
    ("OTT 공식 신작", "디즈니플러스 (신작 OR 공개 OR 제작)"),
    ("OTT 공식 신작", "쿠팡플레이 (오리지널 OR 신작 OR 공개)"),
]

YOUTUBE_QUERIES = [
    "한국 드라마 리뷰",
    "한국 영화 리뷰",
    "한국 예능 리뷰",
    "드라마 결말 해석",
    "영화 결말 해석",
    "드라마 몰아보기",
    "영화 요약",
    "드라마 명장면 쇼츠",
    "예능 명장면 쇼츠",
    "영화 명장면 쇼츠",
    "OTT 신작 반응",
    "배우 신작 인터뷰",
]

# handle이 바뀌거나 조회에 실패하면 채널명 검색으로 보완합니다.
OFFICIAL_YOUTUBE_CHANNELS = [
    {"name": "Netflix Korea", "handle": "@NetflixKorea"},
    {"name": "TVING", "handle": "@TVING_official"},
    {"name": "wavve", "handle": "@wavve"},
    {"name": "Disney Plus Korea", "handle": "@DisneyPlusKR"},
    {"name": "Coupang Play", "handle": "@CoupangPlay"},
    {"name": "SBS Drama", "handle": "@SBSdrama"},
    {"name": "MBC Drama", "handle": "@MBCdrama"},
    {"name": "KBS Drama", "handle": "@KBSdrama"},
    {"name": "tvN D ENT", "handle": "@tvNDENT"},
    {"name": "JTBC Drama", "handle": "@JTBCLove"},
    {"name": "ENA", "handle": "@ENA"},
]

CONTENT_VIDEO_CATEGORY_IDS = {"1", "23", "24", "43"}
CONTENT_RELEVANCE_TERMS = [
    "드라마", "영화", "예능", "시리즈", "넷플릭스", "티빙", "웨이브", "디즈니",
    "쿠팡플레이", "예고편", "티저", "하이라이트", "명장면", "리뷰", "인터뷰",
    "배우", "감독", "애니", "웹툰", "ott",
]

KEYWORD_LEXICON = [
    "드라마", "예능", "영화", "OTT", "넷플릭스", "티빙", "웨이브", "디즈니플러스",
    "키노라이츠", "왓챠피디아", "박스오피스", "시청률", "신작", "공개", "라인업",
    "쇼츠", "유튜브", "릴스", "SNS", "클립", "화제", "리뷰", "해석", "결말",
    "반전", "공포", "스릴러", "로맨스", "로코", "가족", "애니", "키즈",
    "요리", "먹방", "여행", "힐링", "서바이벌", "경쟁", "음악", "아이돌",
    "배우", "감독", "인터뷰", "출연", "합류", "복귀", "흥행", "랭킹",
    "명장면", "몰아보기", "세계관", "원작", "범죄", "수사", "오컬트",
]

GENERIC_TITLE_WORDS = {
    "여러분이", "몰랐던", "최고의", "수작", "탄탄한", "스토리", "충격적", "반전",
    "섬세한", "연출", "탄생한", "트리거", "관련", "영상", "리뷰", "요약", "해석",
    "결말", "총정리", "스포주의", "공식", "예고편", "하이라이트", "명장면", "몰아보기",
    "드라마", "영화", "예능", "한국", "넷플릭스", "코리아", "Netflix", "Korea",
}

# 이전 피드 정리와의 호환 설정. 현재 후보 조건은 issue_rules.py에서 가져옵니다.
NEW_VIDEO_COMMENT_THRESHOLD = int(os.getenv("YOUTUBE_NEW_VIDEO_COMMENT_THRESHOLD", "300"))
INITIAL_ISSUE_MAX_AGE_DAYS = int(os.getenv("YOUTUBE_INITIAL_ISSUE_MAX_AGE_DAYS", "14"))


def today_kst_date():
    return (datetime.now(timezone.utc) + timedelta(hours=9)).date()


def normalize_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def clean_html_text(text):
    value = html.unescape(str(text or ""))
    value = re.sub(r"<script[^>]*>.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_text(value)


def extract_entry_image(entry):
    """RSS 항목에 포함된 대표 이미지 URL을 가능한 범위에서 추출합니다."""
    candidates = []

    for attr in ["media_content", "media_thumbnail"]:
        for item in entry.get(attr, []) or []:
            if isinstance(item, dict):
                candidates.append(item.get("url", ""))

    for item in entry.get("enclosures", []) or []:
        if isinstance(item, dict) and str(item.get("type", "")).startswith("image/"):
            candidates.append(item.get("href", "") or item.get("url", ""))

    for item in entry.get("links", []) or []:
        if isinstance(item, dict) and str(item.get("type", "")).startswith("image/"):
            candidates.append(item.get("href", ""))

    summary_html = str(entry.get("summary", "") or entry.get("description", ""))
    match = re.search(r'<img[^>]+src=["\']([^"\']+)', summary_html, flags=re.I)
    if match:
        candidates.append(html.unescape(match.group(1)))

    for value in candidates:
        url = normalize_text(value)
        if url.startswith("https://") or url.startswith("http://"):
            return url
    return ""


def _html_attributes(tag):
    """HTML 태그 한 개에서 따옴표로 감싼 속성값을 소문자 키로 추출합니다."""
    attrs = {}
    for key, value in re.findall(r"([:\w-]+)\s*=\s*[\"']([^\"']*)[\"']", tag, flags=re.I):
        attrs[key.lower()] = html.unescape(value).strip()
    return attrs


def extract_page_image(page_html, base_url):
    """기사 HTML의 Open Graph/Twitter/JSON-LD 대표 이미지를 추출합니다."""
    if not page_html:
        return ""

    candidates = []
    for tag in re.findall(r"<meta\b[^>]*>", page_html, flags=re.I):
        attrs = _html_attributes(tag)
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in {
            "og:image", "og:image:url", "og:image:secure_url",
            "twitter:image", "twitter:image:src", "image",
        }:
            candidates.append(attrs.get("content", ""))

    for tag in re.findall(r"<link\b[^>]*>", page_html, flags=re.I):
        attrs = _html_attributes(tag)
        if attrs.get("rel", "").lower() in {"image_src", "preload"}:
            href = attrs.get("href", "")
            if href and (attrs.get("as", "").lower() in {"", "image"}):
                candidates.append(href)

    # JSON-LD에서 가장 흔한 image / thumbnailUrl 문자열도 보조로 확인합니다.
    for pattern in [
        r'["\'](?:image|thumbnailUrl)["\']\s*:\s*["\']([^"\']+)',
        r'["\']image["\']\s*:\s*\[\s*["\']([^"\']+)',
    ]:
        match = re.search(pattern, page_html, flags=re.I)
        if match:
            candidates.append(html.unescape(match.group(1)))

    for candidate in candidates:
        candidate = normalize_text(candidate)
        if not candidate or candidate.startswith("data:"):
            continue
        absolute = urljoin(base_url, candidate)
        if absolute.startswith("https://") or absolute.startswith("http://"):
            return absolute
    return ""


def resolve_article_url(url):
    """Google News RSS 중계 링크를 가능한 경우 실제 언론사 기사 URL로 바꿉니다."""
    url = normalize_text(url)
    if not url:
        return ""

    host = urlparse(url).netloc.lower()
    if "news.google.com" not in host:
        return url

    # Google News RSS 링크는 일반 requests 리다이렉트만으로 원문 URL이
    # 노출되지 않는 경우가 많아 전용 디코더를 우선 사용합니다.
    if gnewsdecoder is not None:
        try:
            decoded = gnewsdecoder(url, interval=None)
            if isinstance(decoded, dict) and decoded.get("status"):
                candidate = normalize_text(decoded.get("decoded_url", ""))
                if candidate.startswith(("http://", "https://")):
                    return candidate
        except Exception:
            pass

    # 디코더 실패 시 일반 리다이렉트와 canonical/meta refresh를 보조로 확인합니다.
    try:
        response = requests.get(
            url,
            timeout=ARTICLE_IMAGE_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            },
        )
        final_url = normalize_text(response.url)
        response.raise_for_status()
        if final_url and "news.google.com" not in urlparse(final_url).netloc.lower():
            return final_url

        page_html = response.text[:500_000]
        for tag in re.findall(r"<link\b[^>]*>", page_html, flags=re.I):
            attrs = _html_attributes(tag)
            if attrs.get("rel", "").lower() == "canonical":
                candidate = urljoin(final_url or url, attrs.get("href", ""))
                if candidate.startswith(("http://", "https://")) and "news.google.com" not in urlparse(candidate).netloc.lower():
                    return candidate
        match = re.search(
            r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*url=([^"\']+)',
            page_html,
            flags=re.I,
        )
        if match:
            candidate = urljoin(final_url or url, html.unescape(match.group(1)).strip())
            if candidate.startswith(("http://", "https://")):
                return candidate
    except Exception:
        pass

    return url


def fetch_article_image_and_url(url):
    """원문 URL과 대표 이미지를 함께 반환합니다."""
    resolved_url = resolve_article_url(url)
    target_url = resolved_url or normalize_text(url)
    error_path = r"unsupportedbrowser|(?:^|/)(?:login|signin|consent|error|404|accessdenied)(?:/|$)"
    if re.search(error_path, urlparse(target_url).path.lower()):
        target_url = normalize_text(url)
    if not target_url:
        return "", ""
    try:
        response = requests.get(
            target_url,
            timeout=ARTICLE_IMAGE_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            },
        )
        response.raise_for_status()
        final_url = normalize_text(response.url) or target_url
        if re.search(error_path, urlparse(final_url).path.lower()):
            return target_url, ""
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type and not response.text.lstrip().startswith("<"):
            return final_url, ""
        page_html = response.text[:1_500_000]
        return final_url, extract_page_image(page_html, final_url)
    except Exception:
        return target_url, ""


def fetch_article_image(url):
    """호환용 래퍼: 기사 원문을 해석한 뒤 대표 이미지만 반환합니다."""
    _, image_url = fetch_article_image_and_url(url)
    return image_url


def is_google_news_url(url):
    host = (urlparse(str(url)).hostname or "").lower()
    return host == "news.google.com" or host.endswith(".news.google.com")


def is_article_url(url):
    parsed = urlparse(str(url))
    return bool(parsed.scheme in {"http", "https"} and parsed.hostname and not re.search(
        r"unsupportedbrowser|(?:^|/)(?:login|signin|consent|error|404|accessdenied)(?:/|$)",
        parsed.path.lower(),
    ))


def enrich_news_images(rows):
    """모든 기사 링크를 먼저 해석하고, 이미지 수집만 별도 예산으로 제한합니다."""
    if not rows:
        return rows

    # A thumbnail budget must never determine which articles have usable sources.
    # Resolve once per unique relay URL even when image collection is disabled.
    relay_urls = {
        normalize_text(row.get("source_url", "")) for row in rows
        if source_key(row) == "news" and is_google_news_url(row.get("source_url", ""))
    }
    resolved_urls = {}
    if relay_urls:
        with ThreadPoolExecutor(max_workers=min(4, len(relay_urls))) as executor:
            future_map = {executor.submit(resolve_article_url, url): url for url in sorted(relay_urls)}
            for future in as_completed(future_map):
                original_url = future_map[future]
                try:
                    resolved_url = future.result()
                except Exception:
                    resolved_url = original_url
                if is_article_url(resolved_url) and not is_google_news_url(resolved_url):
                    resolved_urls[original_url] = resolved_url

    resolved_count = 0
    for row in rows:
        original_url = normalize_text(row.get("source_url", ""))
        if source_key(row) == "news" and original_url in resolved_urls:
            row["source_url"] = resolved_urls[original_url]
            resolved_count += 1

    targets = []
    for idx, row in enumerate(rows):
        source_url = normalize_text(row.get("source_url", ""))
        if not source_url or source_key(row) not in {"news"}:
            continue
        missing_image = not normalize_text(row.get("image_url", ""))
        if missing_image and not is_google_news_url(source_url) and is_article_url(source_url):
            targets.append((idx, source_url))

    # 최신 피드가 먼저 보강되도록 최근 날짜순으로 제한합니다.
    targets = sorted(
        targets,
        key=lambda item: normalize_text(rows[item[0]].get("date", "")),
        reverse=True,
    )[:ARTICLE_IMAGE_FETCH_LIMIT]
    if not targets:
        return rows

    image_count = 0
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
        future_map = {executor.submit(fetch_article_image_and_url, url): idx for idx, url in targets}
        for future in as_completed(future_map):
            idx = future_map[future]
            original_url = normalize_text(rows[idx].get("source_url", ""))
            try:
                resolved_url, image_url = future.result()
            except Exception:
                resolved_url, image_url = original_url, ""

            if resolved_url and resolved_url != original_url:
                rows[idx]["source_url"] = resolved_url
                resolved_count += 1
            if image_url and not normalize_text(rows[idx].get("image_url", "")):
                rows[idx]["image_url"] = image_url
                image_count += 1

    print(f"기사 원문 URL 해석: {resolved_count}개 / 대표 이미지 보강: {image_count}개")
    return rows


def parse_int(value):
    try:
        if value is None or value == "":
            return 0
        return int(float(str(value).strip().replace(",", "")))
    except (TypeError, ValueError, OverflowError):
        return 0


def optional_count(value):
    """A missing or malformed measurement must not turn into a measured zero."""
    try:
        if value is None or str(value).strip() == "":
            return None
        number = float(str(value).replace(",", ""))
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None
    except (TypeError, ValueError, OverflowError):
        return None


def extract_keywords(text):
    text = normalize_text(text)
    found = []

    for kw in KEYWORD_LEXICON:
        if kw.lower() in text.lower():
            found.append(kw)

    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    for token in tokens:
        cleaned = token.strip("_-·.,!?'\"“”‘’()[]{}")
        if not cleaned or cleaned in GENERIC_TITLE_WORDS:
            continue
        if cleaned.lower() in {"the", "and", "with", "official", "trailer", "review"}:
            continue
        if cleaned not in found:
            found.append(cleaned)
        if len(found) >= 16:
            break

    return ",".join(found[:16])


GENERIC_CONTENT_LABEL = re.compile(
    r"^(?:제?\d+\s*(?:위|화|회|부|편)(?:\s*(?:예고|선공개|클립|미리보기|하이라이트))?"
    r"|(?:ep\.?\s*)?\d+\s*(?:full|clip)?|[🔔\s]*ep\.?\s*\d+\s*full"
    r"|메이킹|하이라이트|명장면|예고편?|선공개|미리보기|공식|단독|속보"
    r"|인터뷰[①-⑳\d]*|eng|sub|자막|스포(?:주의)?|티저|명작맛집"
    r"|(?:한국\s*)?(?:영화|드라마|예능)|시리즈|작품|콘텐츠|신작|극장가|방송|연예|OTT|속보|단독)$", re.I,
)


def _clean_content_candidate(value):
    value = normalize_text(value)
    value = re.sub(r"^[#'\"“”‘’`]+|[#'\"“”‘’`]+$", "", value).strip()
    value = value.strip("[]《》〈〉≪≫")
    value = re.sub(r"\s*(?:공식\s*)?(?:예고편|티저|리뷰|요약|해석|결말|총정리|하이라이트|명장면|몰아보기).*$", "", value, flags=re.I).strip()
    return value


def is_reliable_related_content(value):
    value = _clean_content_candidate(value)
    if not value or not 2 <= len(value) <= 40 or GENERIC_CONTENT_LABEL.fullmatch(value):
        return False
    if re.search(r"(?:역사상|가장\s*감동|역대급|스펙업|날먹|최강\s*유산|인간쓰레기|일어나는\s*일|벌어지는\s*일)", value):
        return False
    if re.search(r"(?:습니다|는데요|했다|이었다|드립니다|나왔다|떴다)[.!?…]*$", value):
        return False
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", value)
    if not tokens or sum(token in GENERIC_TITLE_WORDS for token in tokens) >= max(2, len(tokens) // 2):
        return False
    return len(tokens) <= 7


def guess_related_content(title):
    """Extract a named work, never an episode marker or an arbitrary headline."""
    title = normalize_text(title)
    promotion = re.search(r"(?:^|[|｜])\s*([^|｜\[\]‘’\"']+?)\s+홍보하러\s+온", title)
    if promotion:
        candidate = _clean_content_candidate(promotion.group(1))
        if is_reliable_related_content(candidate):
            return candidate
    for pattern in [r"《([^》]+)》", r"≪([^≫]+)≫", r"〈([^〉]+)〉", r"「([^」]+)」", r"『([^』]+)』", r"<([^>]+)>", r"\[([^\]]+)\]"]:
        for match in re.finditer(pattern, title):
            raw = normalize_text(match.group(1))
            # Labels like '[김유신 티저]' describe a character, not a work.
            if GENERIC_CONTENT_LABEL.fullmatch(raw) or re.search(r"(?:티저|인터뷰[①-⑳\d]*|선공개|메이킹)$", raw):
                continue
            candidate = _clean_content_candidate(raw)
            if is_reliable_related_content(candidate):
                return candidate
    for pattern in [r"[\"“']([^\"”']+)[\"”']", r"‘([^’]+)’", r"`([^`]+)`"]:
        for match in re.finditer(pattern, title):
            candidate = _clean_content_candidate(match.group(1))
            before = title[max(0, match.start() - 20):match.start()]
            after = title[match.end():match.end() + 20]
            context = bool(re.search(r"영화|드라마|예능|시리즈|작품|신작|원작|개봉|첫방|방영", before)) or bool(re.match(r"\s*[,·:…]?\s*(?:시즌|공식|예고|리뷰|몰아보기|공개|개봉|제작|첫방)", after))
            if context and is_reliable_related_content(candidate):
                return candidate
    for hashtag in re.findall(r"#([가-힣A-Za-z0-9]+)", title):
        if hashtag.lower() in {"sbscatch", "sbs", "kbs", "mbc", "넷플릭스", "드라마", "예능", "영화", "shorts"}:
            continue
        if is_reliable_related_content(hashtag):
            return hashtag
    # A leading work name followed by an explicit format is usable; free prose is not.
    for sep in [" | ", "ㅣ", " - ", " 공식 예고편", " 예고편", " 공식 티저", " 리뷰", " 몰아보기"]:
        if sep in title:
            candidate = _clean_content_candidate(title.split(sep)[0])
            if is_reliable_related_content(candidate):
                return candidate
    return ""


def guess_news_related_content(headline):
    """기사의 주제 작품만 선택하고, 흥행 비교 대상으로 언급된 작품은 건너뜁니다."""
    headline = normalize_text(headline)
    # Editorial columns and report labels are not the work being reported on.
    # Keep bracketed actual work names, but remove known newspaper corner tags.
    from news_subjects import strip_news_editorial_labels
    headline, _ = strip_news_editorial_labels(headline)
    # "'암살자' 넘었다" describes the comparison target, not the unnamed film
    # that actually made news. Preserve any subsequent explicitly named work.
    quote_pattern = r"(?:‘[^’]+’|“[^”]+”|'[^']+'|\"[^\"]+\"|`[^`]+`|《[^》]+》|〈[^〉]+〉)"
    candidate_headline = re.sub(
        quote_pattern + r"\s*(?:[을를]\s*)?(?:넘어선|넘었다|넘고|넘는|넘어|제치고|제쳤다|앞선|따라잡은)",
        " ", headline,
    )
    related = guess_related_content(candidate_headline)
    if related:
        return related
    # A regional desk prefix may precede a work named before its format noun.
    explicit_work = re.search(r"[‘'\"“`]([^’'\"”`]+)[’'\"”`]\s*(?:동화영화|영화|드라마|예능|시리즈|작품)", candidate_headline)
    if explicit_work and is_reliable_related_content(explicit_work.group(1)):
        return _clean_content_candidate(explicit_work.group(1))
    # A leading quoted work followed by an actor's interview is a useful
    # editorial event; requiring both structure and interview avoids quotes.
    leading_quote = re.match(
        r"^\s*(?:‘([^’]+)’|“([^”]+)”|'([^']+)'|\"([^\"]+)\"|`([^`]+)`)"
        r"\s*[,·:…]?\s*(.*)$", candidate_headline,
    )
    if leading_quote:
        candidate = _clean_content_candidate(next(value for value in leading_quote.groups()[:5] if value))
        after = leading_quote.group(6)
        interview_context = "인터뷰" in after and re.match(r"[가-힣]{2,4}(?:[,·]|\s+인터뷰)", after)
        news_context = re.match(r"(?:한국\s*영화|예매율|시청률|누적\s*관객|캐스팅|출연\s*확정|제작\s*확정|영화제|수상|종영|결말)", after)
        if (interview_context or news_context) and is_reliable_related_content(candidate):
            return candidate
    return ""


def parse_entry_timestamp(entry):
    # A feed's modification date cannot establish when an article was first
    # published, nor renew an old article as a newly published story.
    parsed = entry.get("published_parsed") if isinstance(entry, dict) else getattr(entry, "published_parsed", None)
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def parse_entry_date(entry):
    stamp = parse_entry_timestamp(entry)
    return stamp.astimezone(timezone(timedelta(hours=9))).date() if stamp else None


def load_csv(path, columns):
    if not path.exists():
        return pd.DataFrame(columns=columns)

    try:
        df = pd.read_csv(path, sep="|").fillna("")
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        return df[columns]
    except Exception as exc:
        # An unreadable existing file is not an empty successful collection.
        raise RuntimeError(f"기존 CSV 읽기 실패: {path.name}: {safe_error(exc)}") from exc


def save_csv(df, path, columns):
    df = df.copy()
    for col in columns:
        if col not in df.columns:
            df[col] = ""

    df[columns].to_csv(
        path,
        sep="|",
        index=False,
        encoding="utf-8",
        quoting=csv.QUOTE_MINIMAL,
    )


def collect_google_news():
    rows = []
    today = today_kst_date()
    start_date = today - timedelta(days=DAYS - 1)
    missing_dates = 0
    rejected = 0
    for requested_source, query in NEWS_QUERY_GROUPS:
        timed_query = query if re.search(r"\bwhen:", query) else f"{query} when:{DAYS}d"
        url = "https://news.google.com/rss/search?" + f"q={quote(timed_query)}&hl=ko&gl=KR&ceid=KR:ko"
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            if getattr(feed, "bozo", False) and not feed.entries:
                raise ValueError("RSS 파싱 실패")
        except Exception as exc:
            source_error("news", f"RSS 수집 실패 ({query}): {safe_error(exc)}")
            continue
        # Google orders search RSS by relevance. Examine newest actual dated
        # entries before limiting qualified candidates, not the first 12 hits.
        entries = sorted(feed.entries, key=lambda entry: parse_entry_timestamp(entry) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        accepted = 0
        query_event_counts = {}
        for entry in entries[:100]:
            published_stamp = parse_entry_timestamp(entry)
            published_date = parse_entry_date(entry)
            if published_date is None:
                missing_dates += 1
                continue
            if not start_date <= published_date <= today:
                continue
            title = normalize_text(entry.get("title", ""))
            link = normalize_text(entry.get("link", ""))
            summary = clean_html_text(entry.get("summary", ""))
            publisher_info = entry.get("source", {}) or {}
            publisher = normalize_text(publisher_info.get("title", ""))
            # RSS appends the publisher to the headline; it is not part of a work name.
            headline = title.rsplit(" - " + publisher, 1)[0] if publisher else title
            related = guess_news_related_content(headline)
            relevant = is_content_relevant_resource({"snippet": {"title": headline, "description": summary}})
            # A news candidate is a dated article about an identifiable work
            # and a concrete event, not a general keyword mention.
            event = infer_news_event(headline)
            if not event and related and related in summary:
                event = infer_news_event(summary)
            if not title or not is_article_url(link) or not related or not relevant or not event or not publisher:
                rejected += 1
                continue
            discovery_key = (normalize_dedup_text(related), event)
            if query_event_counts.get(discovery_key, 0) >= 2:
                continue
            rows.append(metadata_row({
                "date": published_date.isoformat(), "source": "온라인 화제·뉴스",
                "issue_title": headline[:140], "related_content": related,
                "keywords": extract_keywords(f"{headline} {summary}"),
                "description": summary[:300] or headline,
                "source_url": link, "image_url": extract_entry_image(entry),
            }, published_at=published_stamp.isoformat(), collected_at=utc_now_iso(),
                reference_date=published_date.isoformat(), reference_period=published_date.isoformat(),
                publisher=publisher, source_type="news", content_id="", event_type=event,
                metrics_json={"news_provenance": "google_news_rss",
                              "publisher_url": normalize_text(publisher_info.get("href", "")),
                              "rss_url": link},
                quality_status="candidate", quality_notes="RSS의 발행일·매체·작품 소식을 확인한 기사 후보"))
            accepted += 1
            query_event_counts[discovery_key] = query_event_counts.get(discovery_key, 0) + 1
            if accepted >= NEWS_ENTRIES_PER_QUERY:
                break
    # Search queries often return the same article. It remains one source,
    # independently of how many discovery queries matched it.
    rows = list({(row["source_url"], row["related_content"]): row for row in rows}.values())
    rows.sort(key=lambda row: row["published_at"], reverse=True)
    bounded, event_counts = [], {}
    for row in rows:
        key = (normalize_dedup_text(row["related_content"]), row["event_type"])
        if event_counts.get(key, 0) >= 2:
            continue
        bounded.append(row)
        event_counts[key] = event_counts.get(key, 0) + 1
        if len(bounded) == NEWS_MAX_CANDIDATES:
            break
    rows = bounded
    rows = enrich_news_images(rows)
    unresolved = 0
    for row in rows:
        metrics = json.loads(row["metrics_json"])
        relay = is_google_news_url(row["source_url"])
        metrics["original_url_status"] = "relay" if relay else "resolved"
        metrics["article_url"] = "" if relay else row["source_url"]
        row["metrics_json"] = json.dumps(metrics, ensure_ascii=False, sort_keys=True)
        if is_official_url(row["source_url"]):
            host = urlparse(row["source_url"]).hostname or ""
            ott_domains = ("netflix.com", "tving.com", "wavve.com", "disneyplus.com", "coupangplay.com")
            row["source"] = "OTT 공식 신작" if any(host == domain or host.endswith("." + domain) for domain in ott_domains) else "온라인 화제·뉴스"
            row["source_type"] = "official_announcement"
            row["quality_notes"] = "공식 도메인의 발표; 작품명 자동 추출 후보"
        elif relay:
            row["quality_notes"] += "; Google 뉴스 기사 링크(원문 본문 미확인)"
            unresolved += 1
    if unresolved:
        source_error("news", f"원문 URL 미해석 {unresolved}건: RSS의 발행일·매체·기사 제목 기준")
    if missing_dates:
        source_error("news", f"실제 발행일 미확인 {missing_dates}건 제외")
    source_status("news", "ok" if rows else "empty", len(rows),
                  reference_date=today.isoformat(), reference_period=f"{start_date.isoformat()}/{today.isoformat()}",
                  rejected_count=rejected, missing_publication_date_count=missing_dates,
                  unresolved_original_count=unresolved)
    return rows


def infer_news_event(text):
    for pattern, event in ((r"캐스팅|출연\s*확정", "casting"),
                           (r"제작\s*확정|영상화|리메이크", "production"),
                           (r"수상|영화제", "award"), (r"종영|결말", "finale"), (r"인터뷰", "interview"),
                           (r"시청률|역주행|예매율|박스오피스|누적\s*관객", "audience"),
                           (r"개봉|공개|첫방|방영", "release")):
        if re.search(pattern, text):
            return event
    return ""


def api_key():
    return normalize_text(os.getenv("YOUTUBE_API_KEY", ""))


def youtube_get(endpoint, params, attempts=3):
    """YouTube API GET with bounded retry and actionable errors."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(endpoint, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 200:
                return response.json()

            message = ""
            try:
                message = response.json().get("error", {}).get("message", "")
            except Exception:
                message = response.text[:180]

            # 잘못된 키·할당량 초과는 재시도해도 해결되지 않는다.
            if response.status_code in {400, 401, 403}:
                raise RuntimeError(f"YouTube API {response.status_code}: {message}")

            last_error = RuntimeError(f"YouTube API {response.status_code}: {message}")
        except requests.RequestException as exc:
            last_error = exc

        if attempt < attempts:
            time.sleep(attempt * 1.5)

    raise RuntimeError(f"YouTube API 요청 실패: {last_error}")


def rfc3339_days_ago(days):
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601_duration(value):
    """Parse the subset of ISO 8601 duration used by YouTube (PT#H#M#S)."""
    text = normalize_text(value)
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        text,
    )
    if not match:
        return 0
    values = {key: int(val or 0) for key, val in match.groupdict().items()}
    return values["days"] * 86400 + values["hours"] * 3600 + values["minutes"] * 60 + values["seconds"]


def video_type_from_duration(duration_seconds, text=""):
    # API 응답만으로는 세로형 여부를 확인할 수 없다. 60초 이하는 숏폼 후보로,
    # 3분 이하는 제목/설명에 Shorts 표기가 있을 때만 숏폼 후보로 본다.
    lowered = normalize_text(text).lower()
    has_short_marker = any(marker in lowered for marker in ["#shorts", " shorts", "쇼츠", "숏츠", "shorts/"])
    if 0 < duration_seconds <= 60:
        return "쇼츠/숏폼 후보"
    if 60 < duration_seconds <= 180 and has_short_marker:
        return "쇼츠/숏폼 후보"
    return "일반 영상"


def chunked(values, size=50):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def fetch_youtube_video_resources(video_ids, parts="snippet,statistics,contentDetails,status"):
    key = api_key()
    if not key or not video_ids:
        return []

    resources = []
    for batch in chunked(list(dict.fromkeys(video_ids)), 50):
        payload = youtube_get(
            YOUTUBE_VIDEOS_ENDPOINT,
            {
                "part": parts,
                "id": ",".join(batch),
                "maxResults": 50,
                "key": key,
            },
        )
        resources.extend(payload.get("items", []))
    return resources


def is_content_relevant_resource(resource):
    """Require film/TV evidence for every discovery route; query text is not evidence."""
    snippet = resource.get("snippet", {}) or {}
    title = normalize_text(snippet.get("title", ""))
    description = normalize_text(snippet.get("description", ""))
    category = str(snippet.get("categoryId", ""))
    text = " ".join([title, description, " ".join(snippet.get("tags", [])[:15])]).lower()
    title_lower = title.lower()
    explicit_work = bool(re.search(r"영화|드라마|예능|애니메이션|다큐멘터리|시트콤", title_lower))
    if re.search(r"메이플스토리|로스트아크|스타크래프트|리그\s*오브\s*레전드|발로란트|게임\s*실황|스펙업|대리방송", title_lower) and not explicit_work:
        return False
    if category == "20" and not explicit_work:
        return False
    if re.search(r"\bmlb\b|\bnba\b|\bkbo\b|프리미어리그|uefa|챔피언스리그", title_lower) and not explicit_work:
        return False
    music = re.search(r"\bmv\b|m/v|뮤직비디오|music\s*video|퍼포먼스|1thekillpo|직캠|dance\s*practice|a2o|이수만|케이팝|k팝", title_lower)
    if (music or category == "10") and not explicit_work:
        return False
    strong = ("영화", "드라마", "예능", "시리즈", "넷플릭스", "티빙", "웨이브", "디즈니플러스", "디즈니+", "쿠팡플레이", "애니메이션", "다큐멘터리", "시트콤", "tving", "netflix")
    # Entertainment/category24 and generic 'review/highlight/interview' are insufficient.
    return any(term in text for term in strong)


def resolve_official_channel(channel):
    key = api_key()
    if not key:
        return None

    handle = normalize_text(channel.get("handle", ""))
    if handle:
        try:
            payload = youtube_get(
                YOUTUBE_CHANNELS_ENDPOINT,
                {"part": "snippet,contentDetails", "forHandle": handle, "key": key},
            )
            if payload.get("items"):
                return payload["items"][0]
        except Exception as exc:
            source_error("youtube", f"공식 채널 handle 조회 실패 ({channel['name']}): {safe_error(exc)}")

    try:
        search = youtube_get(
            YOUTUBE_SEARCH_ENDPOINT,
            {
                "part": "snippet",
                "q": channel["name"],
                "type": "channel",
                "maxResults": 3,
                "regionCode": "KR",
                "relevanceLanguage": "ko",
                "key": key,
            },
        )
        ids = [normalize_text(item.get("id", {}).get("channelId", "")) for item in search.get("items", [])]
        ids = [value for value in ids if value]
        if not ids:
            return None
        details = youtube_get(
            YOUTUBE_CHANNELS_ENDPOINT,
            {"part": "snippet,contentDetails", "id": ",".join(ids), "key": key},
        )
        expected = channel["name"].lower().replace(" ", "")
        items = details.get("items", [])
        items.sort(
            key=lambda item: expected in normalize_text(item.get("snippet", {}).get("title", "")).lower().replace(" ", ""),
            reverse=True,
        )
        return items[0] if items else None
    except Exception as exc:
        source_error("youtube", f"공식 채널 검색 실패 ({channel['name']}): {safe_error(exc)}")
        return None


def discover_youtube_videos_api():
    key = api_key()
    if not key:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    today = today_kst_date().strftime("%Y-%m-%d")
    published_after = rfc3339_days_ago(DAYS)
    candidates = {}

    def add_candidate(video_id, origin, snippet=None, popular_evidence=None):
        video_id = normalize_text(video_id)
        if not video_id:
            return
        current = candidates.setdefault(video_id, {"origins": [], "seed_snippet": snippet or {}})
        if origin not in current["origins"]:
            current["origins"].append(origin)
        if snippet and not current.get("seed_snippet"):
            current["seed_snippet"] = snippet
        if popular_evidence and not current.get("popular_evidence"):
            current["popular_evidence"] = popular_evidence

    # 1) 외부 반응 탐색용 12개 검색어
    for query in YOUTUBE_QUERIES:
        try:
            payload = youtube_get(
                YOUTUBE_SEARCH_ENDPOINT,
                {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": "relevance",
                    "publishedAfter": published_after,
                    "maxResults": YOUTUBE_MAX_RESULTS_PER_QUERY,
                    "regionCode": "KR",
                    "relevanceLanguage": "ko",
                    "safeSearch": "moderate",
                    "key": key,
                },
            )
            for item in payload.get("items", []):
                add_candidate(item.get("id", {}).get("videoId", ""), f"검색:{query}", item.get("snippet", {}))
        except Exception as exc:
            source_error("youtube", f"검색 실패 ({query}): {safe_error(exc)}")

    # 2) 실제 한국 인기 영상 응답. 검색·채널 출처 문자열은 인기의 증거가 아니다.
    # 영화/애니메이션·엔터테인먼트 목록도 살펴 전체 목록의 음악/게임 편중을 줄인다.
    popular_lists = (("", "전체"), ("1", "영화·애니메이션"), ("24", "엔터테인먼트"))
    successful_lists, failed_lists, popular_snapshots, popular_video_ids = [], [], {}, set()
    for category_id, category_name in popular_lists:
        try:
            params = {
                "part": "snippet,statistics,contentDetails,status",
                "chart": "mostPopular", "regionCode": "KR", "maxResults": 50, "key": key,
            }
            if category_id:
                params["videoCategoryId"] = category_id
            popular = youtube_get(YOUTUBE_VIDEOS_ENDPOINT, params)
            if not isinstance(popular.get("items"), list):
                raise ValueError("인기 영상 응답의 items 목록 누락")
            snapshot_at = utc_now_iso()
            successful_lists.append(category_id or "all")
            popular_snapshots[category_id or "all"] = snapshot_at
            for position, item in enumerate(popular["items"], 1):
                if is_content_relevant_resource(item):
                    popular_video_ids.add(normalize_text(item.get("id", "")))
                    add_candidate(item.get("id", ""), f"한국 인기 영상:{category_name}", item.get("snippet", {}), {
                        "popular_snapshot_at": snapshot_at, "popular_region": "KR",
                        "popular_list_position": position, "popular_category_id": category_id or "all",
                        "popular_snapshot_complete": True,
                    })
        except Exception as exc:
            failed_lists.append(category_id or "all")
            source_error("youtube", f"인기 영상 수집 실패 ({category_name}): {safe_error(exc)}")
    source_status("youtube", "ok" if len(successful_lists) == len(popular_lists) else "partial",
                  popular_list_state="ok" if len(successful_lists) == len(popular_lists) else "partial" if successful_lists else "failed",
                  popular_successful_lists=successful_lists, popular_candidate_count=len(popular_video_ids),
                  popular_snapshots=popular_snapshots, popular_failed_lists=failed_lists,
                  popular_checked_at=utc_now_iso())

    # 3) 주요 OTT·방송사 공식 채널 신규 업로드
    cutoff = today_kst_date() - timedelta(days=DAYS - 1)
    for channel in OFFICIAL_YOUTUBE_CHANNELS:
        resource = resolve_official_channel(channel)
        if not resource:
            continue
        uploads = resource.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
        if not uploads:
            continue
        try:
            playlist = youtube_get(
                YOUTUBE_PLAYLIST_ITEMS_ENDPOINT,
                {
                    "part": "snippet,contentDetails",
                    "playlistId": uploads,
                    "maxResults": 15,
                    "key": key,
                },
            )
            for item in playlist.get("items", []):
                snippet = item.get("snippet", {}) or {}
                published = normalize_text(snippet.get("publishedAt", ""))[:10]
                try:
                    if datetime.strptime(published, "%Y-%m-%d").date() < cutoff:
                        continue
                except Exception:
                    pass
                video_id = item.get("contentDetails", {}).get("videoId", "") or snippet.get("resourceId", {}).get("videoId", "")
                add_candidate(video_id, f"공식채널:{channel['name']}", snippet)
        except Exception as exc:
            source_error("youtube", f"공식 채널 업로드 실패 ({channel['name']}): {safe_error(exc)}")

    if not candidates:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    resources = fetch_youtube_video_resources(list(candidates))
    rows = []
    for resource in resources:
        video_id = normalize_text(resource.get("id", ""))
        if video_id not in candidates:
            continue
        status = resource.get("status", {}) or {}
        snippet = resource.get("snippet", {}) or {}
        details = resource.get("contentDetails", {}) or {}
        if status.get("privacyStatus") not in {None, "public"} or status.get("embeddable") is False:
            continue
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
            continue

        origins = candidates[video_id]["origins"]
        # Search and official-channel origins are provenance, not relevance evidence.
        if not is_content_relevant_resource(resource):
            continue

        title = normalize_text(snippet.get("title", ""))
        channel_title = normalize_text(snippet.get("channelTitle", ""))
        description = normalize_text(snippet.get("description", ""))[:500]
        duration_seconds = parse_iso8601_duration(details.get("duration", ""))
        origin_text = " / ".join(origins[:6])
        metadata_text = " ".join([
            title, channel_title, description,
            " ".join(snippet.get("tags", [])[:10]),
        ])
        rows.append({
            "video_id": video_id,
            "title": title[:180],
            "channel": channel_title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "first_seen_date": today,
            "upload_date": normalize_text(snippet.get("publishedAt", ""))[:10],
            "related_content": youtube_related_content({"title": title, "description": snippet.get("description", "")}),
            "keywords": extract_keywords(metadata_text),
            "query": origin_text,
            "description": description,
            "duration_seconds": duration_seconds,
            "video_type": video_type_from_duration(duration_seconds, metadata_text),
            "category_id": str(snippet.get("categoryId", "")),
            "channel_id": normalize_text(snippet.get("channelId", "")),
            "last_discovered_at": utc_now_iso(),
            **candidates[video_id].get("popular_evidence", {}),
        })

    return pd.DataFrame(rows, columns=WATCHLIST_COLUMNS).fillna("").drop_duplicates("video_id")

def extract_upload_date(info):
    upload_date = info.get("upload_date", "")
    if upload_date and len(str(upload_date)) == 8:
        try:
            return datetime.strptime(str(upload_date), "%Y%m%d").date().strftime("%Y-%m-%d")
        except Exception:
            pass

    timestamp = info.get("timestamp")
    if timestamp:
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).date().strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def discover_youtube_videos_ytdlp():
    """API 키가 없을 때만 사용하는 호환용 보조 수집기."""
    if yt_dlp is None:
        source_error("youtube", "yt-dlp import 실패")
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    today = today_kst_date().strftime("%Y-%m-%d")
    rows = []
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "extract_flat": False,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for query in YOUTUBE_QUERIES:
            try:
                result = ydl.extract_info(
                    f"ytsearch{min(YOUTUBE_MAX_RESULTS_PER_QUERY, 5)}:{query}",
                    download=False,
                )
            except Exception as exc:
                source_error("youtube", f"yt-dlp 검색 실패 ({query}): {safe_error(exc)}")
                continue

            for item in (result or {}).get("entries", []):
                if not item:
                    continue
                video_id = normalize_text(item.get("id", ""))
                title = normalize_text(item.get("title", ""))
                channel = normalize_text(item.get("channel", "") or item.get("uploader", ""))
                if not video_id or not title:
                    continue
                if not is_content_relevant_resource({"snippet": {"title": title, "description": item.get("description", ""), "tags": item.get("tags", [])}}):
                    continue

                upload_date = extract_upload_date(item)
                if upload_date:
                    try:
                        uploaded = datetime.strptime(upload_date, "%Y-%m-%d").date()
                        if uploaded < today_kst_date() - timedelta(days=DAYS - 1):
                            continue
                    except Exception:
                        pass

                duration_seconds = parse_int(item.get("duration"))
                rows.append({
                    "video_id": video_id,
                    "title": title[:180],
                    "channel": channel,
                    "url": normalize_text(item.get("webpage_url", "")) or f"https://www.youtube.com/watch?v={video_id}",
                    "first_seen_date": today,
                    "upload_date": upload_date,
                    "related_content": youtube_related_content({"title": title, "description": item.get("description", "")}),
                    "keywords": extract_keywords(f"{title} {item.get('description', '')}"),
                    "query": query,
                    "description": normalize_text(item.get("description", ""))[:500],
                    "duration_seconds": duration_seconds,
                    "video_type": video_type_from_duration(duration_seconds, f"{title} {item.get('description', '')}"),
                    "last_discovered_at": utc_now_iso(),
                })

    return pd.DataFrame(rows, columns=WATCHLIST_COLUMNS).fillna("").drop_duplicates("video_id")


def discover_youtube_videos():
    if api_key():
        return discover_youtube_videos_api()
    if os.getenv("ALLOW_YTDLP_FALLBACK", "").lower() in {"1", "true", "yes"}:
        source_error("youtube", "공식 API 키 미설정: 보조 검색 경로만 사용")
        return discover_youtube_videos_ytdlp()
    source_status("youtube", "skipped", error="YOUTUBE_API_KEY 미설정: 공식 API 수집을 실행하지 않음")
    return pd.DataFrame(columns=WATCHLIST_COLUMNS)


def update_youtube_watchlist(new_videos):
    existing = load_csv(YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)
    # Membership is a snapshot, never a sticky property of a previously found video.
    # Clear old membership on every run, including a failed/empty popular response.
    for column in ("popular_snapshot_at", "popular_region", "popular_list_position", "popular_category_id", "popular_snapshot_complete"):
        existing[column] = ""
    if new_videos.empty:
        if not existing.empty:
            save_csv(existing, YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)
        return existing

    # 기존 최초 발견일은 유지하되, 제목·설명·검색어 등 메타데이터는 최신값으로 갱신한다.
    old_first_seen = existing.set_index("video_id")["first_seen_date"].to_dict() if not existing.empty else {}
    new_videos = new_videos.copy()
    new_videos["first_seen_date"] = new_videos.apply(
        lambda row: old_first_seen.get(str(row["video_id"]), row["first_seen_date"]),
        axis=1,
    )

    merged = pd.concat([existing, new_videos], ignore_index=True)
    merged = merged.drop_duplicates(subset=["video_id"], keep="last")
    save_csv(merged, YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)
    return merged


def fetch_video_stats_api(video_ids):
    rows = []
    today = today_kst_date().strftime("%Y-%m-%d")
    for item in fetch_youtube_video_resources(video_ids, parts="statistics,status"):
        status = item.get("status", {}) or {}
        if status and status.get("privacyStatus") != "public":
            continue
        statistics = item.get("statistics", {}) or {}
        views = optional_count(statistics.get("viewCount"))
        if views is None:
            source_error("youtube", "조회수 미제공 영상은 관측에서 제외")
            continue
        rows.append({
            "date": today,
            "video_id": normalize_text(item.get("id", "")),
            "view_count": views,
            "like_count": optional_count(statistics.get("likeCount")),
            "comment_count": optional_count(statistics.get("commentCount")),
            "observed_at": utc_now_iso(),
        })
    return rows


def fetch_video_stats_ytdlp(video_ids):
    if yt_dlp is None:
        return []

    rows = []
    today = today_kst_date().strftime("%Y-%m-%d")
    ydl_opts = {"quiet": True, "skip_download": True, "ignoreerrors": True, "noplaylist": True}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for video_id in video_ids:
            try:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            except Exception:
                continue
            if not info:
                continue
            views = optional_count(info.get("view_count"))
            if views is None:
                source_error("youtube", "조회수 미제공 영상은 보조 관측에서 제외")
                continue
            rows.append({
                "date": today,
                "video_id": video_id,
                "view_count": views,
                "like_count": optional_count(info.get("like_count")),
                "comment_count": optional_count(info.get("comment_count")),
                "observed_at": utc_now_iso(),
            })
    return rows


def fetch_video_stats(video_ids):
    if api_key():
        return fetch_video_stats_api(video_ids)
    return fetch_video_stats_ytdlp(video_ids)


def youtube_meta_relevant(meta):
    return is_content_relevant_resource({"snippet": {
        "title": meta.get("title", ""), "description": meta.get("description", ""),
        "channelTitle": meta.get("channel", ""), "categoryId": meta.get("category_id", ""),
    }})


def youtube_related_content(meta):
    """Use an explicit work title, with a narrow description fallback for scene clips."""
    related = guess_related_content(meta.get("title", ""))
    if related:
        return related
    description = str(meta.get("description", "") or "")
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    first_line = normalize_text(lines[0] if lines else "")[:180]
    if not first_line or re.search(r"추천작|추천\s*작품|함께\s*보면|다른\s*작품|ALTUBE|알튜브", first_line, re.I):
        return ""
    # Require the opening attribution to say what this clip is from. A bracket
    # anywhere in a description can be a recommendation, channel or sponsor.
    match = re.match(
        r"^(?:(?:SBS|KBS|MBC|JTBC|tvN|ENA|OCN|넷플릭스|Netflix|티빙|TVING)\s*)?"
        r"(?:(?:금토|월화|수목|주말|일일|새|방영중인)\s*)?"
        r"(?:드라마|예능|영화|시리즈)\s*[:：]?\s*[《〈≪\[‘'\"]?"
        r"(.+?)[》〉≫\]’'\"]?(?:의\s*한\s*장면|의\s*명장면|의\s*클립|\s+중\s*한\s*장면)",
        first_line, flags=re.I,
    )
    if not match:
        return ""
    candidate = _clean_content_candidate(match.group(1))
    return candidate if is_reliable_related_content(candidate) else ""


def youtube_popular_evidence(meta, as_of_date=None):
    """Return only genuine current-day API membership, never an origin-string guess."""
    snapshot = pd.to_datetime(meta.get("popular_snapshot_at", ""), errors="coerce", utc=True)
    complete = str(meta.get("popular_snapshot_complete", "")).lower() in {"true", "1", "1.0"}
    position = optional_count(meta.get("popular_list_position"))
    category = str(meta.get("popular_category_id", "all"))
    if pd.isna(snapshot) or not complete or meta.get("popular_region") != "KR" or not position or category not in {"all", "1", "24"}:
        return {}
    as_of = pd.Timestamp(as_of_date or today_kst_date()).date()
    if snapshot.tz_convert("Asia/Seoul").date() != as_of:
        return {}
    # Date-based historical replay deliberately uses the end of that KST day.
    # Real-time assessment additionally checks that a timestamp is not in future.
    cutoff = pd.Timestamp(as_of + timedelta(days=1), tz="Asia/Seoul").tz_convert("UTC")
    if snapshot >= cutoff or (cutoff - snapshot).total_seconds() > 36 * 3600:
        return {}
    return {"popular_snapshot_at": snapshot.isoformat(), "popular_region": "KR",
            "popular_list_position": position, "popular_category_id": category,
            "popular_snapshot_complete": True}


def build_youtube_metrics(meta, history, as_of_date=None):
    """Use observations at or before as_of_date; legacy dates never become fake timestamps."""
    as_of = as_of_date or today_kst_date()
    as_of = pd.Timestamp(as_of).date()
    observed = history.copy()
    if "video_id" in observed and meta.get("video_id"):
        observed = observed[observed["video_id"].astype(str).eq(str(meta["video_id"]))]
    observed = observed[observed["view_count"].map(optional_count).notna()].copy()
    observed["_date"] = pd.to_datetime(observed["date"], errors="coerce").dt.date
    observed = observed[observed["_date"].notna() & observed["_date"].le(as_of)].copy()
    if "observed_at" not in observed:
        observed["observed_at"] = ""
    observed["_timestamp"] = pd.to_datetime(observed["observed_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp(as_of + timedelta(days=1), tz="Asia/Seoul").tz_convert("UTC")
    observed = observed[observed["_timestamp"].isna() | observed["_timestamp"].lt(cutoff)]
    observed = observed.sort_values(["_date", "_timestamp"], na_position="first").drop_duplicates("_date", keep="last")
    if observed.empty:
        return {}
    current = observed.iloc[-1]
    upload = pd.to_datetime(meta.get("upload_date", ""), errors="coerce")
    age = (current["_date"] - upload.date()).days + 1 if pd.notna(upload) else None
    metrics = {
        "views_total": max(0, parse_int(current.get("view_count"))),
        "likes_total": optional_count(current.get("like_count")),
        "comments_total": optional_count(current.get("comment_count")),
        "age_days": age, "reaction_basis": "initial", "observation_precision": "timestamp" if pd.notna(current["_timestamp"]) else "date",
        "observation_start": "", "observation_end": normalize_text(str(current.get("observed_at", ""))) or current["_date"].isoformat(),
        "reference_date": current["_date"].isoformat(),
        "observation_days": None, "views_change": None, "comments_change": None,
        "views_change_per_day": None, "comments_change_per_day": None,
        "views_per_day_since_upload": round(max(0, parse_int(current.get("view_count"))) / age, 2) if age and age > 0 else None,
        **youtube_popular_evidence(meta, as_of),
    }
    if len(observed) < 2:
        return metrics
    previous = observed.iloc[-2]
    elapsed = (current["_date"] - previous["_date"]).days
    precision = "date"
    if pd.notna(current["_timestamp"]) and pd.notna(previous["_timestamp"]):
        timestamp_days = (current["_timestamp"] - previous["_timestamp"]).total_seconds() / 86400
        if timestamp_days > 0:
            elapsed, precision = timestamp_days, "timestamp"
    if elapsed <= 0:
        return metrics
    views_change = parse_int(current.get("view_count")) - parse_int(previous.get("view_count"))
    current_comments, previous_comments = optional_count(current.get("comment_count")), optional_count(previous.get("comment_count"))
    comments_change = current_comments - previous_comments if current_comments is not None and previous_comments is not None else None
    metrics.update({
        "reaction_basis": "observed_change", "observation_precision": precision,
        "observation_start": normalize_text(str(previous.get("observed_at", ""))) or previous["_date"].isoformat(),
        "observation_days": round(float(elapsed), 6),
        "views_change": views_change, "comments_change": comments_change,
        "views_change_per_day": round(max(0, views_change) / elapsed, 2),
        "comments_change_per_day": round(max(0, comments_change) / elapsed, 2) if comments_change is not None else None,
        "growth_rate": round(max(0, views_change) / max(parse_int(previous.get("view_count")), 1), 6),
        "counter_decreased": views_change < 0 or (comments_change is not None and comments_change < 0),
    })
    return metrics


def select_youtube_tracking(watchlist, stats, limit=MAX_TRACKING_VIDEOS):
    """Balance new work discovery with already-observed reactions instead of newest-only."""
    today = today_kst_date()
    eligible = []
    for meta in watchlist.to_dict("records"):
        popular = bool(youtube_popular_evidence(meta, today))
        upload = pd.to_datetime(meta.get("upload_date", ""), errors="coerce")
        if pd.isna(upload) or (today - upload.date()).days < 0 or (not popular and (today - upload.date()).days >= MAX_VIDEO_TRACK_DAYS):
            continue
        related = youtube_related_content(meta)
        if not related or not youtube_meta_relevant(meta):
            continue
        metrics = build_youtube_metrics(meta, stats, today)
        impact = (metrics.get("views_change_per_day") or 0) if metrics.get("reaction_basis") == "observed_change" else (metrics.get("views_per_day_since_upload") or 0) * .25
        eligible.append({**meta, "_upload": upload.date(), "_impact": impact, "_observed": bool(metrics), "_popular": popular})
    recent = sorted(eligible, key=lambda item: (item["_upload"], str(item["video_id"])), reverse=True)
    impact = sorted((item for item in eligible if item["_observed"]), key=lambda item: (item["_impact"], item["_upload"], str(item["video_id"])), reverse=True)
    chosen, seen = [], set()
    def take(items, number):
        for item in items:
            if len(chosen) >= number:
                break
            if item["video_id"] not in seen:
                chosen.append(item["video_id"])
                seen.add(item["video_id"])
    take((item for item in eligible if item["_popular"]), limit)
    take(impact, max(len(chosen), limit // 2))
    take(recent, limit)
    source_status("youtube", "ok", tracking_strategy="current-popular/observed-impact/recent-upload",
                  tracking_eligible_count=len(eligible), tracking_selected_count=len(chosen),
                  tracking_omitted_count=max(0, len(eligible) - len(chosen)))
    return chosen


def update_youtube_stats(watchlist):
    existing = load_csv(YOUTUBE_STATS_PATH, STATS_COLUMNS)
    if watchlist.empty:
        return existing
    today = today_kst_date().isoformat()
    selected = select_youtube_tracking(watchlist, existing)
    already = set(existing.loc[existing["date"].astype(str).eq(today), "video_id"].astype(str))
    video_ids = [video_id for video_id in selected if video_id not in already]
    new_stats = fetch_video_stats(video_ids) if video_ids else []
    if video_ids and not new_stats:
        raise RuntimeError("YouTube 선택 대상의 통계 응답이 비어 있음")
    received = {str(row["video_id"]) for row in new_stats}
    missing = len(set(video_ids) - received)
    if missing:
        source_error("youtube", f"선택 대상 {len(video_ids)}개 중 {missing}개 통계 미수신")
    merged = pd.concat([existing, pd.DataFrame(new_stats, columns=STATS_COLUMNS).fillna("")], ignore_index=True)
    merged = merged.drop_duplicates(["date", "video_id"], keep="last")
    save_csv(merged, YOUTUBE_STATS_PATH, STATS_COLUMNS)
    entry = COLLECTION_STATUS["sources"].get("youtube", {})
    eligible_count = entry.get("tracking_eligible_count", 0)
    today_count = len(set(merged.loc[merged["date"].astype(str).eq(today), "video_id"].astype(str)) & set(selected))
    entry.update({"observation_missing_count": missing, "tracking_observed_count": today_count,
                  "tracking_unobserved_count": max(0, eligible_count - today_count)})
    return merged


def youtube_issue_row(meta, metrics, date_text=None):
    related = youtube_related_content(meta)
    if not related or not youtube_meta_relevant(meta) or not metrics:
        return None
    age = metrics.get("age_days")
    as_of = pd.Timestamp(date_text or today_kst_date()).date()
    observed_date = pd.to_datetime(metrics.get("reference_date", ""), errors="coerce")
    if age is None or age < 1 or pd.isna(observed_date) or not 0 <= (as_of - observed_date.date()).days < DAYS:
        return None
    popular = bool(youtube_popular_evidence(metrics, as_of))
    confirmed = metrics["reaction_basis"] == "observed_change"
    if confirmed:
        qualifies = (metrics["views_change_per_day"] >= DAILY_VIEW_DELTA_THRESHOLD
                     or (metrics["comments_change_per_day"] or 0) >= DAILY_COMMENT_DELTA_THRESHOLD
                     or (metrics.get("growth_rate", 0) >= GROWTH_RATE_THRESHOLD and metrics["views_change_per_day"] >= GROWTH_MIN_DELTA))
        signal = metrics["views_change_per_day"]
        comments_text = f"댓글 {metrics['comments_change']:+,}개 변화" if metrics['comments_change'] is not None else "댓글 변화 미측정"
        observation = f"{metrics['observation_start']} → {metrics['observation_end']} 관측 사이 조회 {metrics['views_change']:+,}회, {comments_text}."
        precision_note = "날짜 단위 관측간 증가(정확한 24시간 증가 아님)" if metrics["observation_precision"] == "date" else "기록된 시각 사이 관측 증가"
    else:
        qualifies = age <= DAYS and (metrics["views_total"] >= NEW_VIDEO_HIGH_VIEW_THRESHOLD or (metrics["views_per_day_since_upload"] or 0) >= NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD)
        signal = (metrics["views_per_day_since_upload"] or 0) * .35
        observation = "최초 관측: 이전 관측값이 없어 증가량은 확인되지 않음."
        precision_note = "초기 누적 반응; 상승/급등으로 확인된 신호 아님"
    if not qualifies and not popular:
        return None
    if popular:
        signal += 1_000_000_000_000
        category_name = {"all": "전체", "1": "영화·애니메이션", "24": "엔터테인먼트"}[metrics["popular_category_id"]]
        observation = f"YouTube API 한국 인기 영상 목록({category_name})에 포함. {observation}"
    reference = pd.Timestamp(metrics["popular_snapshot_at"]).tz_convert("Asia/Seoul").date().isoformat() if popular else metrics["reference_date"]
    period = (metrics["observation_start"] + "/" + metrics["observation_end"]) if confirmed else reference
    return metadata_row({
        "date": date_text or reference, "source": "YouTube 반응",
        "issue_title": f"{related} 관련 YouTube {'한국 인기 영상' if popular else '관측 반응 증가' if confirmed else '초기 반응 후보'}",
        "related_content": related, "keywords": extract_keywords(f"{related} {meta.get('title', '')}"),
        "description": f"{meta.get('title', '')} · {observation} 누적 조회 {metrics['views_total']:,}회. 채널: {meta.get('channel', '')}.",
        "source_url": meta.get("url", ""), "image_url": f"https://i.ytimg.com/vi/{meta.get('video_id', '')}/hqdefault.jpg",
    }, published_at=meta.get("upload_date", ""), collected_at=metrics["popular_snapshot_at"] if popular else metrics["observation_end"] if metrics["observation_precision"] == "timestamp" else "",
        reference_date=reference, reference_period=period, publisher=meta.get("channel", ""), source_type="youtube_reaction",
        content_id=meta.get("video_id", ""), event_type="popular_video" if popular else "observed_reaction" if confirmed else "initial_reaction",
        metrics_json=metrics, quality_status="candidate", quality_notes=precision_note + "; 작품명 자동 추출 후보", _signal=signal)


def make_youtube_issues(watchlist, stats):
    if watchlist.empty or stats.empty:
        source_status("youtube", "empty", 0, error="관측할 영상 또는 통계가 없음")
        return []
    today = today_kst_date()
    today_stats = stats[stats["date"].astype(str).eq(today.isoformat())]
    if today_stats.empty:
        source_status("youtube", "failed", 0, error="오늘 수집한 YouTube 관측 통계가 없음")
        return []
    rows = []
    for meta in watchlist[watchlist["video_id"].astype(str).isin(today_stats["video_id"].astype(str))].to_dict("records"):
        row = youtube_issue_row(meta, build_youtube_metrics(meta, stats, today))
        if row:
            rows.append(row)
    rows.sort(key=lambda row: row["_signal"], reverse=True)
    cleaned, seen = [], set()
    for row in rows:
        identity = normalize_dedup_text(row["related_content"])
        if identity in seen:
            continue
        seen.add(identity)
        row.pop("_signal", None)
        cleaned.append(row)
    entry = COLLECTION_STATUS["sources"].get("youtube", {})
    omissions = entry.get("tracking_unobserved_count", 0)
    source_status("youtube", "partial" if omissions else ("ok" if cleaned else "empty"), len(cleaned),
                  reference_date=today.isoformat(), reference_period=today.isoformat())
    return cleaned


def collect_kobis_boxoffice():
    key = normalize_text(os.getenv("KOBIS_API_KEY", ""))
    target = today_kst_date() - timedelta(days=1)
    if not key:
        source_status("kobis", "skipped", error="KOBIS_API_KEY 미설정", reference_date=target.isoformat())
        return []
    try:
        response = requests.get(KOBIS_DAILY_ENDPOINT,
            params={"key": key, "targetDt": target.strftime("%Y%m%d")}, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if data.get("faultInfo") or "boxOfficeResult" not in data:
            raise ValueError("KOBIS 오류 응답 또는 boxOfficeResult 누락")
        payload = data["boxOfficeResult"]
        items = payload.get("dailyBoxOfficeList")
        if not isinstance(items, list):
            raise ValueError("KOBIS dailyBoxOfficeList 형식 오류")
        show_range = normalize_text(payload.get("showRange", ""))
        if show_range and not show_range.startswith(target.strftime("%Y%m%d")):
            raise ValueError("KOBIS 응답 기준일이 요청일과 다름")
    except Exception as exc:
        source_error("kobis", exc)
        source_status("kobis", "failed", reference_date=target.isoformat())
        return []
    rows = []
    for item in items:
        movie = normalize_text(item.get("movieNm", ""))
        movie_id = normalize_text(item.get("movieCd", ""))
        rank = parse_int(item.get("rank"))
        if not movie or not movie_id or not 1 <= rank <= 10:
            source_error("kobis", "영화 ID·영화명·순위가 없는 항목 제외")
            continue
        rank_change = parse_int(item.get("rankInten"))
        new_entry = normalize_text(item.get("rankOldAndNew", "")) == "NEW"
        daily = optional_count(item.get("audiCnt"))
        cumulative = optional_count(item.get("audiAcc"))
        if daily is None or cumulative is None:
            source_error("kobis", "필수 관객 수가 누락되거나 숫자 형식이 잘못됨")
            continue
        headline = f"{movie} 일일 박스오피스 {rank}위" + (" · 신규 진입" if new_entry else "")
        rows.append(metadata_row({
            "date": target.isoformat(), "source": "KOBIS 박스오피스", "issue_title": headline,
            "related_content": movie, "keywords": extract_keywords(f"영화 박스오피스 {movie}"),
            "description": f"{target.isoformat()} KOBIS {rank}위. 일일 관객 {daily:,}명, 누적 {cumulative:,}명. 순위 변화 {rank_change:+d}.",
            "source_url": "https://www.kobis.or.kr/kobis/business/stat/boxs/findDailyBoxOfficeList.do",
            "image_url": "",
        }, published_at="", collected_at=utc_now_iso(), reference_date=target.isoformat(),
            reference_period=target.isoformat(), publisher="영화진흥위원회 KOBIS", source_type="boxoffice",
            content_id=movie_id, event_type="boxoffice_ranking", quality_status="verified", quality_notes="공식 일별 집계",
            metrics_json={"rank": rank, "rank_change": rank_change, "is_new_entry": new_entry,
                          "daily_audience": daily, "cumulative_audience": cumulative,
                          "sales_share": normalize_text(item.get("salesShare", ""))}))
    if items and not rows:
        source_error("kobis", "유효한 영화 ID·순위가 없는 응답")
    source_status("kobis", "ok" if rows else "empty", len(rows),
                  reference_date=target.isoformat(), reference_period=target.isoformat(),
                  received_count=len(items), valid_count=len(rows))
    return rows


def normalize_netflix_columns(df):
    return {str(column).strip().lower().replace(" ", "_"): column for column in df.columns}


def collect_netflix_top10():
    try:
        response = requests.get(NETFLIX_COUNTRY_DATA_URL, timeout=45)
        response.raise_for_status()
        df = pd.read_excel(BytesIO(response.content), engine="openpyxl")
        columns = normalize_netflix_columns(df)
        def col(*names):
            return next((columns[name] for name in names if name in columns), None)
        country_col, iso_col = col("country_name", "country"), col("country_iso2", "country_code")
        week_col, rank_col = col("week"), col("weekly_rank", "rank")
        title_col, season_col = col("show_title", "title"), col("season_title", "season")
        category_col, weeks_col = col("category"), col("cumulative_weeks_in_top_10", "cumulative_weeks")
        if not all([week_col, rank_col, title_col, category_col]) or not (country_col or iso_col):
            raise ValueError("Netflix 필수 컬럼 누락")
        mask = pd.Series(False, index=df.index)
        if country_col:
            mask |= df[country_col].astype(str).str.lower().isin(["south korea", "korea, republic of", "대한민국"])
        if iso_col:
            mask |= df[iso_col].astype(str).str.upper().eq("KR")
        korea = df[mask].copy()
        korea["_week"] = pd.to_datetime(korea[week_col], errors="coerce")
        if korea.empty or korea["_week"].isna().all():
            raise ValueError("Netflix 한국 집계 또는 유효 주간 없음")
        latest = korea["_week"].max().date()
        if latest > today_kst_date() or (today_kst_date() - latest).days >= NETFLIX_MAX_AGE_DAYS:
            source_status("netflix", "stale", error=f"최신 제공 집계 {latest.isoformat()}: 허용 범위 {NETFLIX_MAX_AGE_DAYS}일 초과 또는 미래일",
                          reference_date=latest.isoformat(), reference_period=f"{(latest-timedelta(days=6)).isoformat()}/{latest.isoformat()}")
            return []
        korea = korea[korea["_week"].dt.date.eq(latest)].copy()
        korea["_rank"] = pd.to_numeric(korea[rank_col], errors="coerce")
        korea = korea[korea["_rank"].between(1, 10)].sort_values([category_col, "_rank"])
        if korea.empty:
            raise ValueError("Netflix 한국 Top 10 순위 없음")
    except Exception as exc:
        source_error("netflix", exc)
        source_status("netflix", "failed")
        return []
    period = f"{(latest-timedelta(days=6)).isoformat()}/{latest.isoformat()}"
    rows = []
    for _, item in korea.iterrows():
        title = normalize_text(item.get(title_col, ""))
        season = normalize_text(item.get(season_col, "")) if season_col else ""
        if season.lower() == "nan":
            season = ""
        category = normalize_text(item.get(category_col, ""))
        if not title or not category:
            continue
        rank = int(item["_rank"])
        weeks = parse_int(item.get(weeks_col, 0)) if weeks_col else 0
        identity = "netflix:KR:" + ":".join(normalize_dedup_text(v) for v in (category, title, season))
        rows.append(metadata_row({
            "date": latest.isoformat(), "source": "Netflix Top 10", "issue_title": f"{season or title} 넷플릭스 한국 {category} {rank}위",
            "related_content": title, "keywords": extract_keywords(f"넷플릭스 랭킹 {title} {season} {category}"),
            "description": f"Netflix 한국 {category} 주간 {rank}위. 집계 {period}. 누적 Top 10 {weeks}주.",
            "source_url": "https://www.netflix.com/tudum/top10/south-korea", "image_url": "",
        }, published_at="", collected_at=utc_now_iso(), reference_date=latest.isoformat(), reference_period=period,
            publisher="Netflix Tudum", source_type="ott_ranking", content_id=identity, event_type="weekly_ranking",
            quality_status="verified", quality_notes="공식 한국 주간 순위; 실시간 순위 아님",
            metrics_json={"rank": rank, "category": category, "season": season, "cumulative_weeks": weeks, "country": "KR"}))
    category_ranks = {}
    for row in rows:
        values = json.loads(row["metrics_json"])
        category_ranks.setdefault(values["category"], []).append(values["rank"])
    for category, ranks in category_ranks.items():
        if len(ranks) != 10 or set(ranks) != set(range(1, 11)):
            source_error("netflix", f"{category} Top 10 집계 누락 또는 순위 중복: {len(ranks)}건")
    if len(category_ranks) < 2:
        source_error("netflix", "영화·TV 중 집계 카테고리가 누락됨")
    source_status("netflix", "ok" if rows else "empty", len(rows), reference_date=latest.isoformat(), reference_period=period,
                  category_counts={category: len(ranks) for category, ranks in category_ranks.items()})
    return rows


def extract_youtube_video_id(url):
    """YouTube URL 형식이 달라도 동일 영상 ID를 반환합니다."""
    value = normalize_text(url)
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except Exception:
        return ""

    host = parsed.netloc.lower().split(":")[0]
    path = parsed.path.strip("/")

    if host in {"youtu.be", "www.youtu.be"} and path:
        return path.split("/")[0]

    if host.endswith("youtube.com"):
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if query.get("v"):
            return normalize_text(query.get("v"))
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
            return parts[1]
    return ""


def canonical_content_url(url):
    """추적 파라미터를 제거해 동일 원문 URL을 안정적으로 비교합니다."""
    value = normalize_text(url)
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except Exception:
        return value

    host = parsed.netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    ignored_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "igshid", "ref", "source", "feature", "si",
    }
    query_pairs = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in ignored_params
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    return parsed._replace(
        scheme=(parsed.scheme or "https").lower(),
        netloc=host,
        path=path,
        query=urlencode(sorted(query_pairs)),
        fragment="",
    ).geturl()


def normalize_dedup_text(value):
    """작품명 비교용으로 공백·기호·대소문자 차이를 정리합니다."""
    text = normalize_text(value).lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^0-9a-z가-힣]", "", text)
    return text


def parse_metrics(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def ranking_reference(row):
    """Recover explicitly documented reference dates without treating capture dates as publication."""
    reference = normalize_text(row.get("reference_date", ""))
    if reference:
        return reference[:10]
    description = str(row.get("description", ""))
    pattern = r"(\d{4}-\d{2}-\d{2})\s+KOBIS" if source_key(row) == "kobis" else r"집계\s*주간\s*:\s*(\d{4}-\d{2}-\d{2})"
    match = re.search(pattern, description)
    if not match and source_key(row) == "netflix":
        match = re.search(r"집계\s+(?:\d{4}-\d{2}-\d{2}/)?(\d{4}-\d{2}-\d{2})", description)
    return match.group(1) if match else ""


def netflix_identity(row, metrics=None):
    metrics = metrics or parse_metrics(row.get("metrics_json", ""))
    description = str(row.get("description", ""))
    category = normalize_text(metrics.get("category", ""))
    if not category:
        match = re.search(r"카테고리:\s*([^.;]+)", description)
        category = normalize_text(match.group(1)) if match else ""
    season = normalize_text(metrics.get("season", ""))
    if not season:
        leading = str(row.get("issue_title", "")).split(" 넷플릭스 한국")[0]
        title = normalize_text(row.get("related_content", ""))
        season = normalize_text(leading) if normalize_text(leading) != title else ""
    identity = "netflix:KR:" + ":".join(normalize_dedup_text(v) for v in (category, row.get("related_content", ""), season))
    return identity, category, season


def annotate_existing_issues(df, watchlist, stats):
    """Pure offline migration: preserve capture date, title, row order and arbitrary caller columns.

    Historical YouTube observations are bounded by each row's original capture date.
    Unknown publication/collection times stay unknown; no network or file writes occur.
    """
    result = df.copy().fillna("")
    for column in ISSUE_COLUMNS:
        if column not in result:
            result[column] = ""
    metas = {str(row["video_id"]): row for row in watchlist.to_dict("records")} if "video_id" in watchlist else {}
    records = []
    for original in result.to_dict("records"):
        row = dict(original)
        route = source_key(row)
        metrics = parse_metrics(row.get("metrics_json", ""))
        notes = []
        row["quality_status"] = row.get("quality_status") or "candidate"
        if route == "youtube":
            video_id = extract_youtube_video_id(row.get("source_url", ""))
            meta = metas.get(video_id)
            row.update(source_type="youtube_reaction", content_id=video_id)
            if meta:
                as_of = pd.to_datetime(original.get("date", ""), errors="coerce")
                metrics = build_youtube_metrics(meta, stats, as_of.date()) if pd.notna(as_of) else {}
                related = guess_related_content(meta.get("title", ""))
                row.update(published_at=meta.get("upload_date", ""), publisher=meta.get("channel", ""),
                           related_content=related or row.get("related_content", ""))
                if not related or not youtube_meta_relevant(meta):
                    row["quality_status"] = "rejected"
                    notes.append("영화·TV 작품 관련성 또는 작품 식별 근거 부족")
                if metrics:
                    confirmed = metrics.get("reaction_basis") == "observed_change"
                    row.update(reference_date=metrics["reference_date"],
                               reference_period=f"{metrics['observation_start']}/{metrics['observation_end']}" if confirmed else metrics["reference_date"],
                               event_type="observed_reaction" if confirmed else "initial_reaction")
                    notes.append("날짜 단위 관측간 증분; 정확한 24시간 증가는 아님" if confirmed and metrics["observation_precision"] == "date" else
                                 "기록된 시각 사이 증분" if confirmed else "초기 누적 반응; 상승 확인 전")
                else:
                    row["quality_status"] = "rejected"
                    notes.append("포착일 이전 통계 관측 없음")
            else:
                row["quality_status"] = "rejected"
                notes.append("영상 메타데이터 없음; 작품·게시일 검증 불가")
        elif route == "kobis":
            reference = ranking_reference(row)
            description = str(row.get("description", ""))
            for key, pattern in (("rank", r"박스오피스\s*(\d+)위"), ("daily_audience", r"일일 관객\s*([\d,]+)"),
                                 ("cumulative_audience", r"누적(?:\s*관객)?\s*([\d,]+)"), ("rank_change", r"순위 변화\s*([+-]?\d+)")):
                match = re.search(pattern, description)
                if match:
                    metrics[key] = parse_int(match.group(1))
            row.update(source_type="boxoffice", publisher="영화진흥위원회 KOBIS", event_type="boxoffice_ranking",
                       reference_date=reference, reference_period=reference)
            notes.append("기존 설명의 공식 일별 집계 기준일 복구; 영화 ID 미기록" if not row.get("content_id") else "공식 일별 집계")
            if not reference:
                row["quality_status"] = "rejected"
                notes.append("집계 기준일 미확인")
        elif route == "netflix":
            reference = ranking_reference(row)
            identity, category, season = netflix_identity(row, metrics)
            match = re.search(r"(\d+)위", str(row.get("description", "")))
            if match:
                metrics.setdefault("rank", int(match.group(1)))
            metrics.update(category=category, season=season, country="KR")
            period = f"{(pd.Timestamp(reference)-timedelta(days=6)).date().isoformat()}/{reference}" if reference else ""
            row.update(source_type="ott_ranking", publisher="Netflix Tudum", event_type="weekly_ranking",
                       reference_date=reference, reference_period=period, content_id=identity,
                       source_url="https://www.netflix.com/tudum/top10/south-korea")
            notes.append("기존 설명의 한국 주간 집계 복구; 공식 데이터 URL 복원")
            if not reference or not category:
                row["quality_status"] = "rejected"
                notes.append("집계 주간 또는 카테고리 미확인")
        else:
            official = is_official_url(row.get("source_url", ""))
            row["source_type"] = "official_announcement" if official else "news"
            if not official:
                row["source"] = "온라인 화제·뉴스"
            headline = str(row.get("issue_title", ""))
            related = guess_related_content(headline)
            if related:
                row["related_content"] = related
            # Legacy extracted text is evidence only when it is a plausible work title
            # and the article independently contains film/TV context.
            relevant = is_content_relevant_resource({"snippet": {"title": headline, "description": row.get("description", "")}})
            if not relevant or not is_reliable_related_content(row.get("related_content", "")):
                row["quality_status"] = "rejected"
                notes.append("작품 관련성 또는 식별 근거 부족")
            if not row.get("publisher"):
                suffix = re.search(r"\s+-\s+([^–—]+)$", headline)
                row["publisher"] = suffix.group(1).strip() if suffix else (urlparse(str(row.get("source_url", ""))).hostname or "")
            row["event_type"] = row.get("event_type") or "content_news"
            if not row.get("published_at"):
                notes.append("실제 기사 발행일 미확인; 기존 date는 포착일")
            if "news.google.com" in str(row.get("source_url", "")):
                notes.append("Google News 중계 URL; 원문 미확인")
        row["metrics_json"] = json.dumps(metrics, ensure_ascii=False, sort_keys=True) if metrics else ""
        all_notes = [part.strip() for note in [row.get("quality_notes", ""), *notes] for part in str(note).split(";") if part.strip()]
        row["quality_notes"] = "; ".join(dict.fromkeys(all_notes))
        records.append(row)
    return pd.DataFrame(records, columns=result.columns, index=result.index)


def exact_content_key(row):
    """수집 경로별로 같은 콘텐츠를 하나의 키로 묶습니다.

    - YouTube: 동일 video_id
    - 뉴스·공식자료: 동일 원문 URL
    - KOBIS: 영화 ID/작품명 + 실제 일별 집계일
    - Netflix Top 10: 작품/카테고리/시즌 + 실제 주간 집계

    KOBIS와 Netflix는 여러 작품이 같은 대표 URL을 공유하므로 URL 대신
    작품명을 사용합니다.
    """
    source = normalize_text(row.get("source", ""))
    source_url = normalize_text(row.get("source_url", ""))
    related_content = normalize_text(row.get("related_content", ""))

    video_id = extract_youtube_video_id(source_url)
    if video_id:
        return f"youtube:{video_id}"

    content_key = normalize_dedup_text(related_content)
    if source_key(row) == "kobis":
        return f"kobis:{row.get('content_id') or content_key}:{ranking_reference(row) or row.get('date', '')}" if content_key else ""
    if source_key(row) == "netflix":
        identity, _, _ = netflix_identity(row)
        return f"netflix:{identity}:{ranking_reference(row) or row.get('date', '')}" if content_key else ""

    canonical_url = canonical_content_url(source_url)
    if canonical_url:
        return f"url:{canonical_url}"
    return ""


def collapse_exact_content_duplicates(df):
    """동일 콘텐츠는 한 행만 유지합니다.

    최초 포착 날짜는 보존하고, 조회수·설명·이미지 등 내용은 가장 최근
    수집값으로 갱신합니다. 따라서 같은 영상이 매일 새 카드로 생기지 않고,
    시간이 지나며 최근성 점수가 낮아져 새로운 이슈가 상위로 교체됩니다.
    """
    if df.empty:
        return df

    working = df.copy().reset_index(drop=True)
    working["_exact_content_key"] = working.apply(exact_content_key, axis=1)
    working["_date_dt"] = pd.to_datetime(working["date"], errors="coerce")
    working["_row_order"] = range(len(working))

    no_key = working[working["_exact_content_key"].eq("")].copy()
    keyed = working[~working["_exact_content_key"].eq("")].copy()
    collapsed_rows = []

    for content_key, group in keyed.groupby("_exact_content_key", sort=False):
        valid_dates = group["_date_dt"].dropna()
        first_date = valid_dates.min() if not valid_dates.empty else pd.NaT
        # 최신 수집값을 대표로 사용합니다. 날짜가 같으면 뒤에 수집된 행을 우선합니다.
        latest = group.sort_values(["_date_dt", "_row_order"], ascending=[True, True]).iloc[-1].copy()

        # 동일 영상·기사는 최초 포착일을 유지해 시간이 지나며 자연스럽게 순위가 내려가게 합니다.
        # KOBIS·Netflix는 일별/주별 순위 데이터이므로 최신 상태와 날짜를 유지합니다.
        if not str(content_key).startswith(("kobis:", "netflix:")) and pd.notna(first_date):
            latest["date"] = first_date.strftime("%Y-%m-%d")
        collapsed_rows.append(latest)

    collapsed = pd.DataFrame(collapsed_rows, columns=working.columns) if collapsed_rows else working.iloc[0:0].copy()
    result = pd.concat([no_key, collapsed], ignore_index=True)
    return result.drop(columns=["_exact_content_key", "_date_dt", "_row_order"], errors="ignore")


def load_existing_issues():
    return load_csv(ISSUE_PATH, ISSUE_COLUMNS)


def normalize_legacy_issue_sources(df):
    """과거 Google News 기반 행을 실제 SNS 수집으로 오인하지 않도록 정정한다."""
    if df.empty:
        return df
    df = df.copy()
    news_link = df["source_url"].astype(str).str.contains("news.google.com", case=False, na=False)
    mislabeled = df["source"].astype(str).eq("SNS/숏폼") & news_link
    df.loc[mislabeled, "source"] = "온라인 화제·뉴스"
    return df


def cleanup_legacy_youtube_issues(df):
    """첫 실행에서 오래된 누적 조회수를 급상승으로 저장한 행을 정리한다."""
    if df.empty:
        return df
    df = df.copy()

    def should_drop(row):
        source = str(row.get("source", ""))
        description = str(row.get("description", ""))
        if "유튜브" not in source and "YouTube" not in source:
            return False
        match = re.search(r"최근\s+(\d+)일\s+내\s+조회수", description)
        return bool(match and int(match.group(1)) > INITIAL_ISSUE_MAX_AGE_DAYS)

    mask = df.apply(should_drop, axis=1)
    return df[~mask].copy()


def save_issue_feed(new_rows, *, updated_routes=None):
    """Merge only successful routes; failed or partial routes retain their prior rows."""
    existing = normalize_legacy_issue_sources(load_existing_issues())
    today = today_kst_date().isoformat()
    states = COLLECTION_STATUS.get("sources", {})
    if not existing.empty:
        def replace_row(row):
            if updated_routes is not None and source_key(row) not in updated_routes:
                return False
            state = states.get(source_key(row), {})
            if state.get("state") not in {"ok", "empty"}:
                return False
            # Ranking histories are keyed by their real reference day/week below.
            # News/YouTube reruns replace only their own capture-day slice.
            return source_key(row) in {"youtube", "news"} and str(row.get("date", "")) == today
        existing = existing[~existing.apply(replace_row, axis=1)].copy()
    untouched = existing.iloc[0:0].copy()
    if updated_routes is not None and not existing.empty:
        current_routes = existing.apply(lambda row: source_key(row) in updated_routes, axis=1)
        untouched = existing[~current_routes].copy()
        existing = existing[current_routes].copy()
    new_df = pd.DataFrame(new_rows, columns=ISSUE_COLUMNS).fillna("")
    if updated_routes is not None and not new_df.empty:
        if not new_df.apply(lambda row: source_key(row) in updated_routes, axis=1).all():
            raise ValueError("Requested refresh contains another source route")
    merged = pd.concat([existing, new_df], ignore_index=True)
    if not merged.empty:
        merged = collapse_exact_content_duplicates(merged)
        merged = merged.drop_duplicates(subset=["date", "source", "issue_title", "source_url"], keep="last")
        dates = pd.to_datetime(merged["date"], errors="coerce")
        cutoff = pd.Timestamp(today_kst_date() - timedelta(days=60))
        merged = merged[dates.isna() | dates.ge(cutoff)].sort_values("date", ascending=False, kind="stable")
    if not untouched.empty:
        merged = pd.concat([untouched, merged], ignore_index=True).sort_values("date", ascending=False, kind="stable")
    save_csv(merged, ISSUE_PATH, ISSUE_COLUMNS)
    for route, entry in states.items():
        if updated_routes is not None and route not in updated_routes:
            continue
        entry["retained_count"] = sum(source_key(row) == route for row in merged.to_dict("records"))
    return len(new_df), len(merged)


def main(*, news_only=False):
    # Preserve success history, while clearing errors/diagnostics from prior runs.
    global COLLECTION_STATUS
    previous = {}
    if COLLECTION_STATUS_PATH.exists():
        try:
            previous = json.loads(COLLECTION_STATUS_PATH.read_text(encoding="utf-8")).get("sources", {})
        except (ValueError, OSError):
            previous = {}
    prior_sources = deepcopy(previous) if news_only else {
        route: {"last_success_at": entry["last_success_at"]}
        for route, entry in previous.items() if entry.get("last_success_at")
    }
    if news_only:
        prior_sources["news"] = ({"last_success_at": previous["news"]["last_success_at"]}
                                 if previous.get("news", {}).get("last_success_at") else {})
    COLLECTION_STATUS = {"schema_version": 1, "collected_at": utc_now_iso(), "sources": prior_sources,
                         "collection_scope": "news_only" if news_only else "full",
                         "updated_sources": ["news"] if news_only else ["news", "kobis", "netflix", "youtube"]}
    SOURCE_ERRORS.clear()
    print("외부 이슈 통합 수집 시작")
    all_rows = []
    for route, collector in (("news", collect_google_news), ("kobis", collect_kobis_boxoffice), ("netflix", collect_netflix_top10)):
        if news_only and route != "news":
            continue
        try:
            rows = collector()
            all_rows.extend(rows)
            if not COLLECTION_STATUS["sources"].get(route, {}).get("state"):
                source_status(route, "ok" if rows else "empty", len(rows))
        except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
            source_error(route, exc)
            source_status(route, "failed")
    if not news_only:
        try:
            discovered = discover_youtube_videos()
            if COLLECTION_STATUS["sources"].get("youtube", {}).get("state") != "skipped":
                watchlist = update_youtube_watchlist(discovered)
                stats = update_youtube_stats(watchlist)
                all_rows.extend(make_youtube_issues(watchlist, stats))
        except (requests.RequestException, RuntimeError, ValueError, OSError) as exc:
            source_error("youtube", exc)
            source_status("youtube", "failed")
        if not COLLECTION_STATUS["sources"].get("youtube", {}).get("state"):
            source_status("youtube", "empty")
    new_count, total_count = save_issue_feed(all_rows, updated_routes={"news"}) if news_only else save_issue_feed(all_rows)
    successful = sum(entry.get("state") in {"ok", "empty"} for entry in COLLECTION_STATUS["sources"].values())
    COLLECTION_STATUS["overall_state"] = "ok" if successful == 4 else "partial" if successful or all_rows else "failed"
    COLLECTION_STATUS_PATH.write_text(json.dumps(COLLECTION_STATUS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"issue_feed 반영 {new_count}개 / 보존 포함 {total_count}개 / 상태 {COLLECTION_STATUS['overall_state']}")
    # Expected provider failures are persisted for the app and CI to publish.
    return COLLECTION_STATUS


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news-only", action="store_true", help="Refresh dated public news without calling paid/quota-based APIs")
    main(news_only=parser.parse_args().news_only)
