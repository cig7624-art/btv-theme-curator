"""외부 콘텐츠 이슈 수집기.

수집 경로
- Google News RSS: 뉴스/공식자료, OTT, 극장, 온라인 화제 기사
- YouTube Data API v3: 최근 영상 검색 + 조회/좋아요/댓글 통계

YouTube API 키는 환경변수 ``YOUTUBE_API_KEY`` 로 받는다. 키가 없으면 기존
저장소와의 호환을 위해 yt-dlp를 보조 수단으로 사용하지만, GitHub Actions에서는
API 키 사용을 권장한다.
"""

from __future__ import annotations

import csv
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import feedparser
import pandas as pd
import requests

try:
    import yt_dlp
except Exception:
    yt_dlp = None


ISSUE_PATH = Path("issue_feed.csv")
YOUTUBE_WATCHLIST_PATH = Path("youtube_video_watchlist.csv")
YOUTUBE_STATS_PATH = Path("youtube_video_stats.csv")

YOUTUBE_SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

DAYS = int(os.getenv("ISSUE_LOOKBACK_DAYS", "7"))
MAX_VIDEO_TRACK_DAYS = int(os.getenv("YOUTUBE_TRACK_DAYS", "30"))
MAX_TRACKING_VIDEOS = int(os.getenv("YOUTUBE_MAX_TRACKING_VIDEOS", "250"))
YOUTUBE_MAX_RESULTS_PER_QUERY = max(
    1, min(int(os.getenv("YOUTUBE_MAX_RESULTS_PER_QUERY", "8")), 50)
)
REQUEST_TIMEOUT_SECONDS = 20

ISSUE_COLUMNS = [
    "date",
    "source",
    "issue_title",
    "related_content",
    "keywords",
    "description",
    "source_url",
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

NEWS_QUERIES = [
    # 기본 뉴스/공식자료
    "한국 드라마 화제",
    "예능 화제",
    "OTT 신작 공개",
    "드라마 시청률 상승",
    "예능 새 멤버 합류",
    "콘텐츠 라인업 공개",
    "배우 인터뷰 화제",
    "웹툰 원작 드라마",
    "일본 애니메이션 극장판",
    "중국 드라마 화제",

    # OTT/랭킹
    "넷플릭스 한국 드라마",
    "티빙 신작 예능",
    "웨이브 오리지널",
    "디즈니플러스 한국 콘텐츠",
    "쿠팡플레이 오리지널",
    "OTT 랭킹 화제작",
    "OTT 공개 예정작",
    "OTT 신작 라인업",

    # 네이버 이슈
    "site:entertain.naver.com 드라마 화제",
    "site:entertain.naver.com 예능 화제",
    "site:entertain.naver.com 영화 화제",
    "site:entertain.naver.com 배우 인터뷰",
    "site:n.news.naver.com OTT 신작",
    "site:n.news.naver.com 넷플릭스 티빙 웨이브 디즈니플러스",

    # 극장/박스오피스
    "박스오피스 영화 흥행",
    "박스오피스 순위",
    "영화진흥위원회 박스오피스",
    "CGV 예매율",
    "롯데시네마 예매율",
    "메가박스 예매율",
    "개봉 영화 흥행",

    # SNS·숏폼 자체 데이터가 아니라 관련 보도를 수집하는 쿼리
    "SNS 화제 드라마",
    "쇼츠 화제 예능",
    "릴스 화제 영화",
    "유튜브 쇼츠 드라마 명장면",
]

YOUTUBE_QUERIES = [
    # 리뷰/해석
    "한국 영화 결말 해석",
    "반전 영화 요약",
    "드라마 리뷰",
    "영화 리뷰",
    "드라마 몰아보기",
    "영화 리뷰 급상승",

    # 쇼츠/클립
    "드라마 명장면 쇼츠",
    "예능 클립 화제",
    "예능 쇼츠",
    "예능 하이라이트",
    "아이돌 예능 클립",
    "배우 인터뷰",

    # OTT 공식/예고편
    "넷플릭스 코리아 공식 예고편",
    "넷플릭스 한국 드라마 리뷰",
    "티빙 공식 예고편",
    "티빙 예능 클립",
    "웨이브 공식 예고편",
    "웨이브 드라마 리뷰",
    "디즈니플러스 코리아 예고편",
    "쿠팡플레이 예고편",

    # 방송사/채널 클립
    "SBS 드라마 공식 클립",
    "MBC 예능 공식 클립",
    "KBS 드라마 공식 클립",
    "tvN 드라마 공식 클립",
    "JTBC 드라마 공식 클립",
    "ENA 드라마 공식 클립",

    # 강한 고정 쿼리
    "런닝맨 쇼츠",
    "나혼자산다 쇼츠",
    "놀면 뭐하니 쇼츠",
    "출발 비디오 여행 영화 소개",
    "접속 무비월드 영화 소개",
    "영화 예고편 한국",
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


def guess_news_source(query):
    query = normalize_text(query)

    if "site:entertain.naver.com" in query or "site:n.news.naver.com" in query or "네이버" in query:
        return "네이버 이슈"

    if any(keyword in query for keyword in [
        "박스오피스", "영화진흥위원회", "CGV", "롯데시네마", "메가박스", "개봉 영화"
    ]):
        return "극장/박스오피스"

    # 직접 SNS 데이터를 수집한 것이 아니므로 명칭을 분리한다.
    if any(keyword in query for keyword in ["SNS", "쇼츠", "릴스"]):
        return "온라인 화제 기사"

    if any(keyword in query for keyword in [
        "OTT", "넷플릭스", "티빙", "웨이브", "디즈니", "쿠팡플레이"
    ]):
        return "OTT/랭킹"

    return "뉴스/공식자료"


def collect_google_news():
    rows = []
    today = today_kst_date()
    start = today - timedelta(days=DAYS - 1)

    for query in NEWS_QUERIES:
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
        )
        feed = feedparser.parse(url)

        for entry in feed.entries[:12]:
            published_date = parse_entry_date(entry)
            if not (start <= published_date <= today):
                continue

            title = normalize_text(entry.get("title", ""))
            link = normalize_text(entry.get("link", ""))
            summary = clean_html_text(entry.get("summary", ""))
            if not title or not link:
                continue

            source = guess_news_source(query)
            related = guess_related_content(title)
            keywords = extract_keywords(f"{query} {title} {summary}")
            desc = summary[:220] if summary else f"최근 {source}에서 '{title}' 관련 이슈가 확인됨."

            rows.append({
                "date": published_date.strftime("%Y-%m-%d"),
                "source": source,
                "issue_title": title[:140],
                "related_content": related,
                "keywords": keywords,
                "description": desc,
                "source_url": link,
            })

    return rows


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


def discover_youtube_videos_api():
    key = api_key()
    if not key:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    today = today_kst_date().strftime("%Y-%m-%d")
    discovered = {}
    published_after = rfc3339_days_ago(DAYS)

    for query in YOUTUBE_QUERIES:
        try:
            payload = youtube_get(
                YOUTUBE_SEARCH_ENDPOINT,
                {
                    "part": "snippet",
                    "q": query,
                    "type": "video",
                    "order": "date",
                    "publishedAfter": published_after,
                    "maxResults": YOUTUBE_MAX_RESULTS_PER_QUERY,
                    "regionCode": "KR",
                    "relevanceLanguage": "ko",
                    "safeSearch": "moderate",
                    "key": key,
                },
            )
        except Exception as exc:
            print(f"유튜브 API 검색 실패: {query} / {exc}")
            continue

        for item in payload.get("items", []):
            video_id = normalize_text(item.get("id", {}).get("videoId", ""))
            snippet = item.get("snippet", {}) or {}
            title = normalize_text(snippet.get("title", ""))
            if not video_id or not title:
                continue

            current = discovered.get(video_id, {
                "video_id": video_id,
                "title": title,
                "channel": normalize_text(snippet.get("channelTitle", "")),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "first_seen_date": today,
                "upload_date": normalize_text(snippet.get("publishedAt", ""))[:10],
                "related_content": guess_related_content(title),
                "keywords": "",
                "query": "",
                "description": normalize_text(snippet.get("description", ""))[:500],
                "duration_seconds": 0,
                "video_type": "",
                "queries": [],
            })
            if query not in current["queries"]:
                current["queries"].append(query)
            discovered[video_id] = current

    if not discovered:
        return pd.DataFrame(columns=WATCHLIST_COLUMNS)

    resources = fetch_youtube_video_resources(list(discovered))
    resource_map = {item.get("id", ""): item for item in resources}
    rows = []

    for video_id, row in discovered.items():
        resource = resource_map.get(video_id, {})
        status = resource.get("status", {}) or {}
        snippet = resource.get("snippet", {}) or {}
        content_details = resource.get("contentDetails", {}) or {}

        # 삭제·비공개·라이브 스트림은 후보에서 제외한다.
        if status and status.get("privacyStatus") != "public":
            continue
        if status and status.get("embeddable") is False:
            continue
        if snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
            continue

        duration_seconds = parse_iso8601_duration(content_details.get("duration", ""))
        queries = row.pop("queries", [])
        query_text = " / ".join(queries[:4])
        metadata_text = " ".join([
            query_text,
            row["title"],
            row["channel"],
            row["description"],
            " ".join(snippet.get("tags", [])[:10]),
        ])
        row["query"] = query_text
        row["keywords"] = extract_keywords(metadata_text)
        row["duration_seconds"] = duration_seconds
        row["video_type"] = video_type_from_duration(duration_seconds, metadata_text)
        rows.append(row)

    return pd.DataFrame(rows, columns=WATCHLIST_COLUMNS).fillna("")


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
    if watchlist.empty or stats.empty:
        return []

    today = today_kst_date()
    today_str = today.strftime("%Y-%m-%d")
    stats = stats.copy()
    stats["date_dt"] = pd.to_datetime(stats["date"], errors="coerce")
    for column in ["view_count", "like_count", "comment_count"]:
        stats[column] = pd.to_numeric(stats[column], errors="coerce").fillna(0).astype(int)

    today_stats = stats[stats["date"].astype(str) == today_str].copy()
    if today_stats.empty:
        return []

    rows = []
    watchlist_map = watchlist.set_index("video_id").to_dict("index")

    for _, cur in today_stats.iterrows():
        video_id = str(cur["video_id"])
        if video_id not in watchlist_map:
            continue

        meta = watchlist_map[video_id]
        prev = stats[
            (stats["video_id"].astype(str) == video_id)
            & (stats["date"].astype(str) != today_str)
        ].sort_values("date_dt").tail(1)

        view_count = parse_int(cur["view_count"])
        comment_count = parse_int(cur["comment_count"])
        delta_views = 0
        delta_comments = 0
        growth_ratio = 0.0
        reason = ""

        if not prev.empty:
            prev_row = prev.iloc[0]
            prev_views = parse_int(prev_row["view_count"])
            prev_comments = parse_int(prev_row["comment_count"])
            delta_views = max(view_count - prev_views, 0)
            delta_comments = max(comment_count - prev_comments, 0)
            if prev_views > 0:
                growth_ratio = delta_views / prev_views

            if delta_views >= DAILY_VIEW_DELTA_THRESHOLD:
                reason = f"전일 대비 조회수 {delta_views:,}회 증가"
            elif growth_ratio >= GROWTH_RATE_THRESHOLD and delta_views >= GROWTH_MIN_DELTA:
                reason = f"전일 대비 조회 증가율 {growth_ratio * 100:.0f}%, 증가량 {delta_views:,}회"
            elif delta_comments >= DAILY_COMMENT_DELTA_THRESHOLD:
                reason = f"전일 대비 댓글 {delta_comments:,}개 증가"
        else:
            upload_date_text = normalize_text(meta.get("upload_date", ""))
            try:
                upload_dt = datetime.strptime(upload_date_text, "%Y-%m-%d").date()
            except Exception:
                upload_dt = today

            age_days = max((today - upload_dt).days + 1, 1)
            views_per_day = view_count / age_days
            # 최초 관측에서는 최근 업로드만 이슈 후보로 인정한다. 오래된 영상의
            # 누적 조회수를 오늘의 급상승으로 오인하지 않기 위함이다.
            if age_days <= INITIAL_ISSUE_MAX_AGE_DAYS:
                if view_count >= NEW_VIDEO_HIGH_VIEW_THRESHOLD and views_per_day >= 10000:
                    reason = f"공개 {age_days}일 만에 조회수 {view_count:,}회 기록"
                elif views_per_day >= NEW_VIDEO_VIEWS_PER_DAY_THRESHOLD:
                    reason = f"공개 후 일평균 조회수 약 {views_per_day:,.0f}회로 빠르게 확산"
                elif comment_count >= NEW_VIDEO_COMMENT_THRESHOLD:
                    reason = f"공개 {age_days}일 만에 댓글 {comment_count:,}개로 높은 반응 확인"

        if not reason:
            continue

        title = normalize_text(meta.get("title", ""))
        related = normalize_text(meta.get("related_content", ""))
        # 작품명이나 프로그램명을 신뢰할 수 없는 일반 리뷰 문장은 큐레이션
        # 트리거로 쓰지 않는다. 통계 이력에는 남기되 issue_feed에서는 제외한다.
        if not is_reliable_related_content(related):
            continue
        keywords = normalize_text(meta.get("keywords", ""))
        url = normalize_text(meta.get("url", ""))
        channel = normalize_text(meta.get("channel", ""))
        video_type = normalize_text(meta.get("video_type", "")) or "일반 영상"
        source = "유튜브/숏폼 후보" if video_type == "쇼츠/숏폼 후보" else "유튜브"

        desc = (
            f"'{title}' 영상이 {reason}. 채널: {channel}. "
            f"현재 조회수 {view_count:,}회, 댓글 {comment_count:,}개. 영상 유형: {video_type}."
        )
        rows.append({
            "date": today_str,
            "source": source,
            "issue_title": f"{related or title} 관련 유튜브 반응 상승",
            "related_content": related or title[:40],
            "keywords": keywords,
            "description": desc[:300],
            "source_url": url,
            "sort_delta": delta_views,
            "sort_views": view_count,
        })

    rows.sort(key=lambda row: (row["sort_delta"], row["sort_views"]), reverse=True)
    cleaned = []
    seen = set()
    for row in rows:
        key = row["source_url"] or row["issue_title"]
        if key in seen:
            continue
        seen.add(key)
        row.pop("sort_delta", None)
        row.pop("sort_views", None)
        cleaned.append(row)
    return cleaned[:15]


def load_existing_issues():
    return load_csv(ISSUE_PATH, ISSUE_COLUMNS)


def normalize_legacy_issue_sources(df):
    """과거 Google News 기반 행을 실제 SNS 수집으로 오인하지 않도록 정정한다."""
    if df.empty:
        return df
    df = df.copy()
    news_link = df["source_url"].astype(str).str.contains("news.google.com", case=False, na=False)
    mislabeled = df["source"].astype(str).eq("SNS/숏폼") & news_link
    df.loc[mislabeled, "source"] = "온라인 화제 기사"
    return df


def cleanup_legacy_youtube_issues(df):
    """첫 실행에서 오래된 누적 조회수를 급상승으로 저장한 행을 정리한다."""
    if df.empty:
        return df
    df = df.copy()

    def should_drop(row):
        source = str(row.get("source", ""))
        description = str(row.get("description", ""))
        if not source.startswith("유튜브"):
            return False
        match = re.search(r"최근\s+(\d+)일\s+내\s+조회수", description)
        return bool(match and int(match.group(1)) > INITIAL_ISSUE_MAX_AGE_DAYS)

    mask = df.apply(should_drop, axis=1)
    return df[~mask].copy()


def save_issue_feed(new_rows):
    existing = cleanup_legacy_youtube_issues(normalize_legacy_issue_sources(load_existing_issues()))
    if not new_rows:
        save_csv(existing, ISSUE_PATH, ISSUE_COLUMNS)
        return 0, len(existing)

    new_df = pd.DataFrame(new_rows, columns=ISSUE_COLUMNS).fillna("")
    merged = pd.concat([existing, new_df], ignore_index=True)
    merged["dedup_key"] = merged.apply(
        lambda row: str(row["source_url"]).strip() or str(row["issue_title"]).strip(),
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
    print("외부 이슈 수집 시작")

    news_rows = collect_google_news()
    print(f"뉴스/RSS 후보: {len(news_rows)}개")

    new_videos = discover_youtube_videos()
    print(f"유튜브 신규 후보: {len(new_videos)}개")

    watchlist = update_youtube_watchlist(new_videos)
    print(f"유튜브 watchlist 전체: {len(watchlist)}개")

    stats = update_youtube_stats(watchlist)
    print(f"유튜브 stats 전체: {len(stats)}개")

    youtube_issue_rows = make_youtube_issues(watchlist, stats)
    print(f"유튜브 반응 상승 이슈: {len(youtube_issue_rows)}개")

    new_count, total_count = save_issue_feed(news_rows + youtube_issue_rows)
    print(f"issue_feed 신규 반영: {new_count}개")
    print(f"issue_feed 전체 누적: {total_count}개")


if __name__ == "__main__":
    main()
