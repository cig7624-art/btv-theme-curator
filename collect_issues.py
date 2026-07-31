"""B tv+ 외부 콘텐츠 이슈 통합 수집기.

수집 경로
- YouTube 반응: 한국 인기 영상, 주요 공식 채널 신규 업로드, 12개 외부 반응 검색어
- 극장·박스오피스: KOBIS 일별 박스오피스
- OTT 랭킹·신작: Netflix 공식 한국 Top 10과 OTT 공식 신작 관련 자료
- 온라인 화제·뉴스: 최근 기사·공식 발표와 반복 보도량을 통한 화제 신호

필수 환경변수는 YOUTUBE_API_KEY이며, 극장 데이터를 사용할 때 KOBIS_API_KEY를 추가합니다.
키가 없는 경로는 건너뛰고 나머지 수집은 계속 진행합니다.
"""

from __future__ import annotations

import csv
import html
import os
import re
import time
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, urlparse

import feedparser
import pandas as pd
import requests

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

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
YOUTUBE_CHANNELS_ENDPOINT = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_PLAYLIST_ITEMS_ENDPOINT = "https://www.googleapis.com/youtube/v3/playlistItems"
KOBIS_DAILY_ENDPOINT = "https://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
NETFLIX_COUNTRY_DATA_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.xlsx"

DAYS = int(os.getenv("ISSUE_LOOKBACK_DAYS", "7"))
MAX_VIDEO_TRACK_DAYS = int(os.getenv("YOUTUBE_TRACK_DAYS", "30"))
MAX_TRACKING_VIDEOS = int(os.getenv("YOUTUBE_MAX_TRACKING_VIDEOS", "500"))
YOUTUBE_MAX_RESULTS_PER_QUERY = max(
    1, min(int(os.getenv("YOUTUBE_MAX_RESULTS_PER_QUERY", "8")), 50)
)
REQUEST_TIMEOUT_SECONDS = 20
ARTICLE_IMAGE_FETCH_LIMIT = max(0, int(os.getenv("ARTICLE_IMAGE_FETCH_LIMIT", "60")))
ARTICLE_IMAGE_TIMEOUT_SECONDS = max(3, int(os.getenv("ARTICLE_IMAGE_TIMEOUT_SECONDS", "8")))

ISSUE_COLUMNS = [
    "date",
    "source",
    "issue_title",
    "related_content",
    "keywords",
    "description",
    "source_url",
    "image_url",
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
]

STATS_COLUMNS = [
    "date",
    "video_id",
    "view_count",
    "like_count",
    "comment_count",
]

NEWS_QUERY_GROUPS = [
    # 온라인 화제·뉴스: 큐레이션 기회로 연결하기 쉬운 사건성 신호 중심
    ("온라인 화제·뉴스", "한국 드라마 신작 첫방 공개"),
    ("온라인 화제·뉴스", "한국 예능 신작 첫방 공개"),
    ("온라인 화제·뉴스", "한국 영화 개봉 신작"),
    ("온라인 화제·뉴스", "드라마 예능 캐스팅 출연 확정"),
    ("온라인 화제·뉴스", "시즌2 후속편 제작 확정"),
    ("온라인 화제·뉴스", "웹툰 웹소설 원작 영상화 제작"),
    ("온라인 화제·뉴스", "드라마 예능 시청률 상승"),
    ("온라인 화제·뉴스", "드라마 영화 역주행 화제"),
    ("온라인 화제·뉴스", "콘텐츠 수상 해외 반응"),
    ("온라인 화제·뉴스", "드라마 예능 종영 결말 화제"),
    ("온라인 화제·뉴스", "콘텐츠 리메이크 제작 확정"),
    ("온라인 화제·뉴스", "배우 감독 인터뷰 신작"),

    # OTT 신작·공개 예정 관련 자료. 실제 순위는 Netflix 공식 데이터에서 별도 수집합니다.
    ("OTT 공식 신작", "넷플릭스 한국 신작 공개 예정"),
    ("OTT 공식 신작", "티빙 오리지널 신작 공개"),
    ("OTT 공식 신작", "웨이브 오리지널 신작 공개"),
    ("OTT 공식 신작", "디즈니플러스 코리아 신작 공개"),
    ("OTT 공식 신작", "쿠팡플레이 오리지널 신작 공개"),
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

# 첫 수집일에도 화제 영상을 선별하기 위한 기준
NEW_VIDEO_HIGH_VIEW_THRESHOLD = int(os.getenv("YOUTUBE_NEW_VIDEO_VIEW_THRESHOLD", "100000"))
NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD = int(
    os.getenv("YOUTUBE_NEW_VIDEO_VELOCITY_THRESHOLD", "30000")
)
NEW_VIDEO_COMMENT_THRESHOLD = int(os.getenv("YOUTUBE_NEW_VIDEO_COMMENT_THRESHOLD", "300"))
# 최초 관측 시에는 누적 조회수만으로 오래된 영상을 이슈로 오인하지 않도록
# 업로드 후 최대 허용 일수를 별도로 둔다. 이후 관측부터는 업로드일과 무관하게
# 전일 대비 증가량으로 급등 여부를 판단한다.
INITIAL_ISSUE_MAX_AGE_DAYS = int(os.getenv("YOUTUBE_INITIAL_ISSUE_MAX_AGE_DAYS", "14"))

# 전일 대비 급등 조건
DAILY_VIEW_DELTA_THRESHOLD = int(os.getenv("YOUTUBE_DAILY_VIEW_DELTA", "50000"))
DAILY_COMMENT_DELTA_THRESHOLD = int(os.getenv("YOUTUBE_DAILY_COMMENT_DELTA", "100"))

# 성장률 조건. 기존 코드의 2.0은 '200% 증가'를 뜻한다.
GROWTH_RATE_THRESHOLD = float(os.getenv("YOUTUBE_GROWTH_RATE_THRESHOLD", "2.0"))
GROWTH_MIN_DELTA = int(os.getenv("YOUTUBE_GROWTH_MIN_DELTA", "20000"))


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


def enrich_news_images(rows):
    """Google News 중계 URL을 원문 URL로 바꾸고 대표 이미지를 병렬 보강합니다."""
    if not rows or ARTICLE_IMAGE_FETCH_LIMIT <= 0:
        return rows

    targets = []
    for idx, row in enumerate(rows):
        source_url = normalize_text(row.get("source_url", ""))
        if not source_url:
            continue
        missing_image = not normalize_text(row.get("image_url", ""))
        is_google_news = "news.google.com" in urlparse(source_url).netloc.lower()
        if missing_image or is_google_news:
            targets.append((idx, source_url))

    # 최신 피드가 먼저 보강되도록 최근 날짜순으로 제한합니다.
    targets = sorted(
        targets,
        key=lambda item: normalize_text(rows[item[0]].get("date", "")),
        reverse=True,
    )[:ARTICLE_IMAGE_FETCH_LIMIT]
    if not targets:
        return rows

    resolved_count = 0
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
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _clean_content_candidate(value):
    value = normalize_text(value)
    value = re.sub(r"^[#'\"“”‘’]+|[#'\"“”‘’]+$", "", value).strip()
    value = re.sub(r"\s*(공식\s*)?(예고편|티저|리뷰|요약|해석|결말|총정리|하이라이트|명장면|몰아보기).*?$", "", value, flags=re.I).strip()
    return value[:60]


def is_reliable_related_content(value):
    value = _clean_content_candidate(value)
    if not value or len(value) < 2 or len(value) > 32:
        return False
    tokens = re.findall(r"[가-힣A-Za-z0-9]+", value)
    if not tokens:
        return False
    generic_count = sum(token in GENERIC_TITLE_WORDS for token in tokens)
    if generic_count >= max(2, len(tokens) // 2):
        return False
    # 완성된 설명문처럼 긴 표현은 작품명으로 사용하지 않는다.
    if len(tokens) >= 6 and any(word in value for word in ["탄생", "스토리", "연출", "충격", "최고"]):
        return False
    return True


def guess_related_content(title):
    title = normalize_text(title)

    # 작품명을 명시하는 괄호 표기를 가장 신뢰한다.
    for pattern in [r"《([^》]+)》", r"〈([^〉]+)〉", r"<([^>]+)>", r"\[([^\]]+)\]"]:
        match = re.search(pattern, title)
        if match:
            candidate = _clean_content_candidate(match.group(1))
            if is_reliable_related_content(candidate):
                return candidate

    # 따옴표는 문장 전체를 감싸는 경우가 많으므로 짧은 작품명일 때만 채택한다.
    for pattern in [r"[\"“']([^\"”']+)[\"”']", r"[‘]([^’]+)[’]"]:
        match = re.search(pattern, title)
        if match:
            candidate = _clean_content_candidate(match.group(1))
            if is_reliable_related_content(candidate):
                return candidate

    cleaned = re.sub(r"\[[^\]]+\]", "", title).strip()
    # 공식 예고편처럼 구분자로 작품명이 앞에 오는 제목을 처리한다.
    for sep in [" - ", " | ", "…", ":", "：", "ㅣ"]:
        if sep in cleaned:
            candidate = _clean_content_candidate(cleaned.split(sep)[0])
            if is_reliable_related_content(candidate):
                return candidate

    # '작품명 리뷰/예고편' 형태를 처리하되 설명문이면 비워 둔다.
    candidate = _clean_content_candidate(cleaned)
    if is_reliable_related_content(candidate):
        return candidate
    return ""


def parse_entry_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6]).date()
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6]).date()
    return today_kst_date()


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
        print(f"CSV 읽기 실패: {path} / {exc}")
        return pd.DataFrame(columns=columns)


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

    for source, query in NEWS_QUERY_GROUPS:
        url = "https://news.google.com/rss/search?" + f"q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)

        for entry in feed.entries[:12]:
            published_date = parse_entry_date(entry)
            if not (start_date <= published_date <= today):
                continue

            title = normalize_text(entry.get("title", ""))
            link = normalize_text(entry.get("link", ""))
            summary = clean_html_text(entry.get("summary", ""))
            if not title or not link:
                continue

            related = guess_related_content(title)
            keywords = extract_keywords(f"{query} {title} {summary}")
            desc = summary[:220] if summary else f"최근 {source}에서 '{title}' 관련 자료가 확인됨."
            rows.append({
                "date": published_date.strftime("%Y-%m-%d"),
                "source": source,
                "issue_title": title[:140],
                "related_content": related,
                "keywords": keywords,
                "description": desc,
                "source_url": link,
                "image_url": extract_entry_image(entry),
            })

    return enrich_news_images(rows)

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
    snippet = resource.get("snippet", {}) or {}
    category_id = str(snippet.get("categoryId", ""))
    text = " ".join([
        normalize_text(snippet.get("title", "")),
        normalize_text(snippet.get("description", "")),
        normalize_text(snippet.get("channelTitle", "")),
        " ".join(snippet.get("tags", [])[:15]),
    ]).lower()
    return category_id in CONTENT_VIDEO_CATEGORY_IDS or any(term.lower() in text for term in CONTENT_RELEVANCE_TERMS)


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
            print(f"공식 채널 handle 조회 실패: {channel['name']} / {exc}")

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
        print(f"공식 채널 검색 실패: {channel['name']} / {exc}")
        return None


def discover_youtube_videos_api():
    key = api_key()
    if not key:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    today = today_kst_date().strftime("%Y-%m-%d")
    published_after = rfc3339_days_ago(DAYS)
    candidates = {}

    def add_candidate(video_id, origin, snippet=None):
        video_id = normalize_text(video_id)
        if not video_id:
            return
        current = candidates.setdefault(video_id, {"origins": [], "seed_snippet": snippet or {}})
        if origin not in current["origins"]:
            current["origins"].append(origin)
        if snippet and not current.get("seed_snippet"):
            current["seed_snippet"] = snippet

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
            print(f"유튜브 검색 실패: {query} / {exc}")

    # 2) 검색어 없이 한국 인기 영상
    try:
        popular = youtube_get(
            YOUTUBE_VIDEOS_ENDPOINT,
            {
                "part": "snippet,statistics,contentDetails,status",
                "chart": "mostPopular",
                "regionCode": "KR",
                "maxResults": 50,
                "key": key,
            },
        )
        for item in popular.get("items", []):
            if is_content_relevant_resource(item):
                add_candidate(item.get("id", ""), "한국 인기 영상", item.get("snippet", {}))
    except Exception as exc:
        print(f"한국 인기 영상 수집 실패: {exc}")

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
            print(f"공식 채널 업로드 수집 실패: {channel['name']} / {exc}")

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
        # 인기 영상은 콘텐츠 관련성 필터를 통과한 것만 유지합니다.
        if origins == ["한국 인기 영상"] and not is_content_relevant_resource(resource):
            continue

        title = normalize_text(snippet.get("title", ""))
        channel_title = normalize_text(snippet.get("channelTitle", ""))
        description = normalize_text(snippet.get("description", ""))[:500]
        duration_seconds = parse_iso8601_duration(details.get("duration", ""))
        origin_text = " / ".join(origins[:6])
        metadata_text = " ".join([
            origin_text, title, channel_title, description,
            " ".join(snippet.get("tags", [])[:10]),
        ])
        rows.append({
            "video_id": video_id,
            "title": title[:180],
            "channel": channel_title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "first_seen_date": today,
            "upload_date": normalize_text(snippet.get("publishedAt", ""))[:10],
            "related_content": guess_related_content(title),
            "keywords": extract_keywords(metadata_text),
            "query": origin_text,
            "description": description,
            "duration_seconds": duration_seconds,
            "video_type": video_type_from_duration(duration_seconds, metadata_text),
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
        print("yt-dlp import 실패. 유튜브 수집을 건너뜁니다.")
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
                print(f"yt-dlp 검색 실패: {query} / {exc}")
                continue

            for item in (result or {}).get("entries", []):
                if not item:
                    continue
                video_id = normalize_text(item.get("id", ""))
                title = normalize_text(item.get("title", ""))
                channel = normalize_text(item.get("channel", "") or item.get("uploader", ""))
                if not video_id or not title:
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
                    "related_content": guess_related_content(title),
                    "keywords": extract_keywords(f"{query} {title} {channel}"),
                    "query": query,
                    "description": normalize_text(item.get("description", ""))[:500],
                    "duration_seconds": duration_seconds,
                    "video_type": video_type_from_duration(duration_seconds, f"{title} {item.get('description', '')}"),
                })

    return pd.DataFrame(rows, columns=WATCHLIST_COLUMNS).fillna("").drop_duplicates("video_id")


def discover_youtube_videos():
    if api_key():
        print("유튜브 수집 방식: YouTube Data API v3")
        return discover_youtube_videos_api()

    print("경고: YOUTUBE_API_KEY가 없어 yt-dlp 보조 수집을 사용합니다.")
    return discover_youtube_videos_ytdlp()


def update_youtube_watchlist(new_videos):
    existing = load_csv(YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)
    if new_videos.empty:
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
        rows.append({
            "date": today,
            "video_id": normalize_text(item.get("id", "")),
            "view_count": parse_int(statistics.get("viewCount")),
            "like_count": parse_int(statistics.get("likeCount")),
            "comment_count": parse_int(statistics.get("commentCount")),
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
            rows.append({
                "date": today,
                "video_id": video_id,
                "view_count": parse_int(info.get("view_count")),
                "like_count": parse_int(info.get("like_count")),
                "comment_count": parse_int(info.get("comment_count")),
            })
    return rows


def fetch_video_stats(video_ids):
    if api_key():
        return fetch_video_stats_api(video_ids)
    return fetch_video_stats_ytdlp(video_ids)


def update_youtube_stats(watchlist):
    existing_stats = load_csv(YOUTUBE_STATS_PATH, STATS_COLUMNS)
    if watchlist.empty:
        return existing_stats

    today = today_kst_date()
    cutoff = today - timedelta(days=MAX_VIDEO_TRACK_DAYS)
    active = watchlist.copy()
    active["first_seen_dt"] = pd.to_datetime(active["first_seen_date"], errors="coerce").dt.date
    active = active[
        active["first_seen_dt"].isna() | (active["first_seen_dt"] >= cutoff)
    ].copy()

    # 최근 발견 영상부터 추적해 오래된 영상이 상한을 독식하지 않게 한다.
    active = active.sort_values("first_seen_dt", ascending=False, na_position="last").head(MAX_TRACKING_VIDEOS)
    today_str = today.strftime("%Y-%m-%d")
    already_today = set(
        existing_stats[existing_stats["date"].astype(str) == today_str]["video_id"].astype(str)
    )
    video_ids = [video_id for video_id in active["video_id"].astype(str) if video_id not in already_today]
    new_stats = fetch_video_stats(video_ids)
    if not new_stats:
        return existing_stats

    merged = pd.concat([
        existing_stats,
        pd.DataFrame(new_stats, columns=STATS_COLUMNS).fillna(""),
    ], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "video_id"], keep="last")
    save_csv(merged, YOUTUBE_STATS_PATH, STATS_COLUMNS)
    return merged


def make_youtube_issues(watchlist, stats):
    """최근 화제 영상 후보를 조회 규모·댓글·공개 후 속도로 선별합니다.

    일일 급등 계산은 핵심 선정 기준에서 제외하고, 한국 인기 영상·공식 채널·12개
    검색어로 발견된 후보 중 최근 반응이 큰 영상을 선택합니다.
    """
    if watchlist.empty or stats.empty:
        return []

    today = today_kst_date()
    today_str = today.strftime("%Y-%m-%d")
    stats = stats.copy()
    for column in ["view_count", "like_count", "comment_count"]:
        stats[column] = pd.to_numeric(stats[column], errors="coerce").fillna(0).astype(int)
    current = stats[stats["date"].astype(str) == today_str].copy()
    if current.empty:
        return []

    watchlist_map = watchlist.set_index("video_id").to_dict("index")
    rows = []
    for _, metric in current.iterrows():
        video_id = str(metric["video_id"])
        meta = watchlist_map.get(video_id)
        if not meta:
            continue

        upload_text = normalize_text(meta.get("upload_date", ""))
        try:
            upload_date = datetime.strptime(upload_text, "%Y-%m-%d").date()
        except Exception:
            upload_date = today
        age_days = max((today - upload_date).days + 1, 1)
        origin = normalize_text(meta.get("query", ""))
        is_popular = "한국 인기 영상" in origin
        if age_days > DAYS and not is_popular:
            continue

        view_count = parse_int(metric.get("view_count"))
        like_count = parse_int(metric.get("like_count"))
        comment_count = parse_int(metric.get("comment_count"))
        views_per_day = view_count / age_days
        official = "공식채널:" in origin

        # 공식 신규 영상은 절대 조회수가 다소 낮아도 후보로 남기고,
        # 일반 검색 영상은 반응 기준을 더 엄격하게 적용합니다.
        qualifies = (
            is_popular
            or view_count >= 100000
            or views_per_day >= 20000
            or comment_count >= 200
            or (official and (view_count >= 30000 or comment_count >= 50))
        )
        if not qualifies:
            continue

        title = normalize_text(meta.get("title", ""))
        related = normalize_text(meta.get("related_content", ""))
        if not is_reliable_related_content(related):
            continue

        video_type = normalize_text(meta.get("video_type", "")) or "일반 영상"
        channel = normalize_text(meta.get("channel", ""))
        url = normalize_text(meta.get("url", ""))
        keywords = normalize_text(meta.get("keywords", ""))
        desc = (
            f"'{title}' 영상이 공개 {age_days}일 기준 조회수 {view_count:,}회, "
            f"좋아요 {like_count:,}개, 댓글 {comment_count:,}개를 기록. "
            f"일평균 조회수 약 {views_per_day:,.0f}회. 채널: {channel}. "
            f"수집 경로: {origin}. 영상 유형: {video_type}."
        )
        signal = views_per_day + comment_count * 250 + (30000 if is_popular else 0) + (15000 if official else 0)
        rows.append({
            "date": today_str,
            "source": "YouTube 반응",
            "issue_title": f"{related} 관련 YouTube 화제 영상",
            "related_content": related,
            "keywords": keywords,
            "description": desc[:420],
            "source_url": url,
            "image_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "_signal": signal,
        })

    rows.sort(key=lambda row: row["_signal"], reverse=True)
    cleaned = []
    seen_content = set()
    for row in rows:
        key = normalize_text(row["related_content"]).lower()
        if key in seen_content:
            continue
        seen_content.add(key)
        row.pop("_signal", None)
        cleaned.append(row)
    return cleaned[:15]


def collect_kobis_boxoffice():
    key = normalize_text(os.getenv("KOBIS_API_KEY", ""))
    if not key:
        print("KOBIS_API_KEY 없음: 극장·박스오피스 수집을 건너뜁니다.")
        return []

    target = today_kst_date() - timedelta(days=1)
    try:
        response = requests.get(
            KOBIS_DAILY_ENDPOINT,
            params={"key": key, "targetDt": target.strftime("%Y%m%d")},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json().get("boxOfficeResult", {})
        items = payload.get("dailyBoxOfficeList", [])
    except Exception as exc:
        print(f"KOBIS 수집 실패: {exc}")
        return []

    rows = []
    for item in items:
        rank = parse_int(item.get("rank"))
        rank_inten = parse_int(item.get("rankInten"))
        new_entry = normalize_text(item.get("rankOldAndNew", "")) == "NEW"
        # KOBIS 일별 박스오피스가 제공하는 Top 10을 모두 수집합니다.
        # 신규 진입·순위 상승 여부는 제목과 점수 계산에서 별도로 강조합니다.
        movie = normalize_text(item.get("movieNm", ""))
        if not movie:
            continue
        daily_audience = parse_int(item.get("audiCnt"))
        cumulative = parse_int(item.get("audiAcc"))
        sales_share = normalize_text(item.get("salesShare", ""))
        if new_entry:
            headline = f"{movie} 박스오피스 신규 진입 {rank}위"
        elif rank == 1:
            headline = f"{movie} 일일 박스오피스 1위"
        elif rank_inten >= 2:
            headline = f"{movie} 박스오피스 {rank_inten}계단 상승해 {rank}위"
        else:
            headline = f"{movie} 일일 박스오피스 {rank}위"
        rows.append({
            "date": today_kst_date().strftime("%Y-%m-%d"),
            "source": "KOBIS 박스오피스",
            "issue_title": headline,
            "related_content": movie,
            "keywords": extract_keywords(f"영화 박스오피스 흥행 {movie}"),
            "description": (
                f"{target.strftime('%Y-%m-%d')} KOBIS 일별 박스오피스 {rank}위. "
                f"일일 관객 {daily_audience:,}명, 누적 관객 {cumulative:,}명, "
                f"매출 점유율 {sales_share}%. "
                + ("신규 진입." if new_entry else f"전일 대비 순위 변화 {rank_inten:+d}.")
            ),
            "source_url": "https://www.kobis.or.kr/kobis/business/stat/boxs/findDailyBoxOfficeList.do",
            "image_url": "",
        })
    return rows


def normalize_netflix_columns(df):
    return {str(column).strip().lower().replace(" ", "_"): column for column in df.columns}


def collect_netflix_top10():
    try:
        response = requests.get(NETFLIX_COUNTRY_DATA_URL, timeout=45)
        response.raise_for_status()
        df = pd.read_excel(BytesIO(response.content), engine="openpyxl")
    except Exception as exc:
        print(f"Netflix Top 10 수집 실패: {exc}")
        return []

    columns = normalize_netflix_columns(df)
    def col(*names):
        for name in names:
            if name in columns:
                return columns[name]
        return None

    country_col = col("country_name", "country")
    iso_col = col("country_iso2", "country_code")
    week_col = col("week")
    rank_col = col("weekly_rank", "rank")
    title_col = col("show_title", "title")
    season_col = col("season_title", "season")
    category_col = col("category")
    weeks_col = col("cumulative_weeks_in_top_10", "cumulative_weeks")
    if not all([week_col, rank_col, title_col]) or not (country_col or iso_col):
        print(f"Netflix Top 10 컬럼 확인 실패: {list(df.columns)}")
        return []

    mask = pd.Series(False, index=df.index)
    if country_col:
        mask |= df[country_col].astype(str).str.lower().isin(["south korea", "korea, republic of", "대한민국"])
    if iso_col:
        mask |= df[iso_col].astype(str).str.upper().eq("KR")
    korea = df[mask].copy()
    if korea.empty:
        print("Netflix Top 10에서 한국 데이터를 찾지 못했습니다.")
        return []
    korea["_week"] = pd.to_datetime(korea[week_col], errors="coerce")
    latest_week = korea["_week"].max()
    korea = korea[korea["_week"] == latest_week]
    korea["_rank"] = pd.to_numeric(korea[rank_col], errors="coerce").fillna(99).astype(int)
    korea = korea[korea["_rank"] <= 10].sort_values("_rank")

    rows = []
    for _, item in korea.iterrows():
        title = normalize_text(item.get(title_col, ""))
        season = normalize_text(item.get(season_col, "")) if season_col else ""
        related = title
        display = season if season and season.lower() != "nan" else title
        rank = int(item["_rank"])
        category = normalize_text(item.get(category_col, "")) if category_col else ""
        weeks = parse_int(item.get(weeks_col, 0)) if weeks_col else 0
        rows.append({
            "date": today_kst_date().strftime("%Y-%m-%d"),
            "source": "Netflix Top 10",
            "issue_title": f"{display} 넷플릭스 한국 Top 10 {rank}위",
            "related_content": related,
            "keywords": extract_keywords(f"OTT 넷플릭스 랭킹 신작 {title} {season} {category}"),
            "description": (
                f"Netflix 공식 국가별 주간 Top 10에서 한국 {rank}위. "
                f"카테고리: {category or '미분류'}. 누적 Top 10 진입 {weeks}주. "
                f"집계 주간: {latest_week.strftime('%Y-%m-%d') if pd.notna(latest_week) else ''}."
            ),
            "source_url": "https://www.netflix.com/tudum/top10",
            "image_url": "",
        })
    return rows


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


def save_issue_feed(new_rows):
    existing = cleanup_legacy_youtube_issues(normalize_legacy_issue_sources(load_existing_issues()))
    today_str = today_kst_date().strftime("%Y-%m-%d")

    # 같은 날 워크플로를 다시 실행하면 오늘 수집분을 새 결과로 교체합니다.
    if not existing.empty:
        existing = existing[existing["date"].astype(str) != today_str].copy()

    new_df = pd.DataFrame(new_rows, columns=ISSUE_COLUMNS).fillna("")
    merged = pd.concat([existing, new_df], ignore_index=True)
    if merged.empty:
        save_csv(merged, ISSUE_PATH, ISSUE_COLUMNS)
        return 0, 0

    # 이전 실행에서 Google News 중계 URL만 저장된 최근 기사도 함께 보강합니다.
    # 이렇게 해야 새 수집분뿐 아니라 이미 화면에 노출 중인 카드에도 썸네일이 채워집니다.
    merged = merged.sort_values("date", ascending=False)
    merged_rows = enrich_news_images(merged.to_dict("records"))
    merged = pd.DataFrame(merged_rows, columns=ISSUE_COLUMNS).fillna("")

    # KOBIS·Netflix처럼 여러 콘텐츠가 같은 원본 URL을 공유하는 데이터원이 있습니다.
    # URL만으로 중복 제거하면 서로 다른 작품이 한 건으로 합쳐지므로,
    # 콘텐츠명과 이슈 제목까지 포함해 실제 동일 피드만 제거합니다.
    merged["dedup_key"] = merged.apply(
        lambda row: "|".join([
            str(row.get("date", "")).strip(),
            str(row.get("source", "")).strip(),
            str(row.get("related_content", "")).strip(),
            str(row.get("issue_title", "")).strip(),
            str(row.get("source_url", "")).strip(),
        ]),
        axis=1,
    )
    merged = merged.drop_duplicates(subset=["dedup_key"], keep="last").drop(columns=["dedup_key"])
    merged["date_dt"] = pd.to_datetime(merged["date"], errors="coerce")
    cutoff = pd.Timestamp(today_kst_date() - timedelta(days=60))
    merged = merged[merged["date_dt"].isna() | (merged["date_dt"] >= cutoff)].copy()
    merged = merged.drop(columns=["date_dt"]).sort_values("date", ascending=False)
    save_csv(merged, ISSUE_PATH, ISSUE_COLUMNS)
    return len(new_df), len(merged)


def main():
    print("외부 이슈 통합 수집 시작")

    news_rows = collect_google_news()
    print(f"온라인 화제·뉴스/OTT 신작 후보: {len(news_rows)}개")

    new_videos = discover_youtube_videos()
    print(f"YouTube 신규 후보: {len(new_videos)}개")
    watchlist = update_youtube_watchlist(new_videos)
    print(f"YouTube watchlist 전체: {len(watchlist)}개")
    stats = update_youtube_stats(watchlist)
    print(f"YouTube stats 전체: {len(stats)}개")
    youtube_rows = make_youtube_issues(watchlist, stats)
    print(f"YouTube 화제 영상: {len(youtube_rows)}개")

    kobis_rows = collect_kobis_boxoffice()
    print(f"KOBIS 박스오피스: {len(kobis_rows)}개")

    netflix_rows = collect_netflix_top10()
    print(f"Netflix 한국 Top 10: {len(netflix_rows)}개")

    all_rows = news_rows + youtube_rows + kobis_rows + netflix_rows
    new_count, total_count = save_issue_feed(all_rows)
    print(f"issue_feed 오늘 반영: {new_count}개")
    print(f"issue_feed 전체 누적: {total_count}개")


if __name__ == "__main__":
    main()
