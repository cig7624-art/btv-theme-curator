import csv
import re
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import quote

import feedparser
import pandas as pd

try:
    import yt_dlp
except Exception:
    yt_dlp = None


ISSUE_PATH = Path("issue_feed.csv")
YOUTUBE_WATCHLIST_PATH = Path("youtube_video_watchlist.csv")
YOUTUBE_STATS_PATH = Path("youtube_video_stats.csv")

DAYS = 7
MAX_VIDEO_TRACK_DAYS = 30
MAX_TRACKING_VIDEOS = 250

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
]

STATS_COLUMNS = [
    "date",
    "video_id",
    "view_count",
    "like_count",
    "comment_count",
]

NEWS_QUERIES = [
    "한국 드라마 화제",
    "예능 화제",
    "OTT 신작 공개",
    "넷플릭스 한국 드라마",
    "티빙 신작 예능",
    "웨이브 오리지널",
    "디즈니플러스 한국 콘텐츠",
    "박스오피스 영화 흥행",
    "키노라이츠 트렌드 랭킹",
    "왓챠피디아 영화 드라마",
    "드라마 시청률 상승",
    "예능 새 멤버 합류",
    "콘텐츠 라인업 공개",
]

YOUTUBE_QUERIES = [
    "한국 영화 결말 해석",
    "반전 영화 요약",
    "드라마 명장면 쇼츠",
    "예능 클립 화제",
    "넷플릭스 한국 드라마 리뷰",
    "티빙 예능 클립",
    "웨이브 드라마 리뷰",
    "런닝맨 쇼츠",
    "나혼자산다 쇼츠",
    "놀면 뭐하니 쇼츠",
    "출발 비디오 여행 영화 소개",
    "접속 무비월드 영화 소개",
    "드라마 리뷰",
    "영화 리뷰",
    "예능 쇼츠",
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

# 첫 실행 또는 기준점이 부족할 때도 잡을 수 있는 조건
NEW_VIDEO_HIGH_VIEW_THRESHOLD = 100_000

# 전일 대비 급등 조건
DAILY_VIEW_DELTA_THRESHOLD = 50_000
DAILY_COMMENT_DELTA_THRESHOLD = 100

# 성장률 조건
GROWTH_RATE_THRESHOLD = 2.0
GROWTH_MIN_DELTA = 20_000


def today_kst_date():
    return (datetime.utcnow() + timedelta(hours=9)).date()


def normalize_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def parse_int(value):
    try:
        if value is None or value == "":
            return 0
        return int(value)
    except Exception:
        return 0


def extract_keywords(text):
    text = normalize_text(text)
    found = []

    for kw in KEYWORD_LEXICON:
        if kw.lower() in text.lower():
            found.append(kw)

    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)

    for token in tokens[:10]:
        if token not in found and len(found) < 14:
            found.append(token)

    return ",".join(found[:14])


def guess_related_content(title):
    title = normalize_text(title)

    m = re.search(r"《([^》]+)》", title)
    if m:
        return m.group(1)

    m = re.search(r"[\"“']([^\"”']+)[\"”']", title)
    if m:
        return m.group(1)

    title = re.sub(r"\[[^\]]+\]", "", title).strip()

    for sep in [" - ", " | ", "…", ":", "：", "ㅣ"]:
        if sep in title:
            return title.split(sep)[0].strip()[:30]

    return title[:30]


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
    except Exception:
        return pd.DataFrame(columns=columns)


def save_csv(df, path, columns):
    df = df.copy()

    for col in columns:
        if col not in df.columns:
            df[col] = ""

    df = df[columns]

    df.to_csv(
        path,
        sep="|",
        index=False,
        encoding="utf-8",
        quoting=csv.QUOTE_MINIMAL,
    )


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
            summary = normalize_text(entry.get("summary", ""))

            if not title or not link:
                continue

            related = guess_related_content(title)
            keywords = extract_keywords(f"{query} {title} {summary}")

            desc = summary[:180] if summary else f"최근 뉴스/공식자료에서 '{title}' 관련 이슈가 확인됨."

            rows.append({
                "date": published_date.strftime("%Y-%m-%d"),
                "source": "뉴스/공식자료",
                "issue_title": title[:120],
                "related_content": related,
                "keywords": keywords,
                "description": desc,
                "source_url": link,
            })

    return rows


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
            return datetime.utcfromtimestamp(timestamp).date().strftime("%Y-%m-%d")
        except Exception:
            pass

    return ""


def discover_youtube_videos():
    if yt_dlp is None:
        print("yt-dlp import 실패. 유튜브 수집을 건너뜁니다.")
        return pd.DataFrame(columns=WATCHLIST_COLUMNS), []

    today = today_kst_date().strftime("%Y-%m-%d")
    rows = []
    raw_infos = []

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
                result = ydl.extract_info(f"ytsearch5:{query}", download=False)
            except Exception as e:
                print(f"유튜브 검색 실패: {query} / {e}")
                continue

            entries = result.get("entries", []) if result else []

            for item in entries:
                if not item:
                    continue

                video_id = normalize_text(item.get("id", ""))
                title = normalize_text(item.get("title", ""))
                channel = normalize_text(item.get("channel", "") or item.get("uploader", ""))
                url = normalize_text(item.get("webpage_url", ""))

                if not url and video_id:
                    url = f"https://www.youtube.com/watch?v={video_id}"

                if not video_id or not title or not url:
                    continue

                upload_date = extract_upload_date(item)
                related = guess_related_content(title)
                keywords = extract_keywords(f"{query} {title} {channel}")

                rows.append({
                    "video_id": video_id,
                    "title": title[:160],
                    "channel": channel,
                    "url": url,
                    "first_seen_date": today,
                    "upload_date": upload_date,
                    "related_content": related,
                    "keywords": keywords,
                    "query": query,
                })

                raw_infos.append(item)

    new_df = pd.DataFrame(rows, columns=WATCHLIST_COLUMNS).fillna("")
    return new_df, raw_infos


def update_youtube_watchlist(new_videos):
    existing = load_csv(YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)

    if new_videos.empty:
        return existing

    merged = pd.concat([existing, new_videos], ignore_index=True)
    merged = merged.drop_duplicates(subset=["video_id"], keep="first")

    save_csv(merged, YOUTUBE_WATCHLIST_PATH, WATCHLIST_COLUMNS)

    return merged


def fetch_video_stats(video_ids):
    if yt_dlp is None:
        return []

    rows = []
    today = today_kst_date().strftime("%Y-%m-%d")

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for video_id in video_ids:
            url = f"https://www.youtube.com/watch?v={video_id}"

            try:
                info = ydl.extract_info(url, download=False)
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


def update_youtube_stats(watchlist):
    existing_stats = load_csv(YOUTUBE_STATS_PATH, STATS_COLUMNS)

    if watchlist.empty:
        return existing_stats

    today = today_kst_date()
    cutoff = today - timedelta(days=MAX_VIDEO_TRACK_DAYS)

    watchlist = watchlist.copy()
    watchlist["first_seen_dt"] = pd.to_datetime(
        watchlist["first_seen_date"],
        errors="coerce"
    ).dt.date

    active = watchlist[
        (watchlist["first_seen_dt"].isna())
        | (watchlist["first_seen_dt"] >= cutoff)
    ].copy()

    active = active.head(MAX_TRACKING_VIDEOS)

    today_str = today.strftime("%Y-%m-%d")
    already_today = set(
        existing_stats[
            existing_stats["date"].astype(str) == today_str
        ]["video_id"].astype(str).tolist()
    )

    video_ids = [
        vid for vid in active["video_id"].astype(str).tolist()
        if vid not in already_today
    ]

    new_stats = fetch_video_stats(video_ids)

    if not new_stats:
        return existing_stats

    new_df = pd.DataFrame(new_stats, columns=STATS_COLUMNS).fillna("")

    merged = pd.concat([existing_stats, new_df], ignore_index=True)
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
    stats["view_count"] = pd.to_numeric(stats["view_count"], errors="coerce").fillna(0).astype(int)
    stats["like_count"] = pd.to_numeric(stats["like_count"], errors="coerce").fillna(0).astype(int)
    stats["comment_count"] = pd.to_numeric(stats["comment_count"], errors="coerce").fillna(0).astype(int)

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

        prev_stats = stats[
            (stats["video_id"].astype(str) == video_id)
            & (stats["date"].astype(str) != today_str)
        ].sort_values("date_dt")

        prev = prev_stats.tail(1)

        view_count = parse_int(cur["view_count"])
        comment_count = parse_int(cur["comment_count"])

        delta_views = 0
        delta_comments = 0
        growth_rate = 0.0
        reason = ""

        if not prev.empty:
            prev_row = prev.iloc[0]
            prev_views = parse_int(prev_row["view_count"])
            prev_comments = parse_int(prev_row["comment_count"])

            delta_views = max(view_count - prev_views, 0)
            delta_comments = max(comment_count - prev_comments, 0)

            if prev_views > 0:
                growth_rate = delta_views / prev_views

            if delta_views >= DAILY_VIEW_DELTA_THRESHOLD:
                reason = f"전일 대비 조회수 {delta_views:,}회 증가"
            elif growth_rate >= GROWTH_RATE_THRESHOLD and delta_views >= GROWTH_MIN_DELTA:
                reason = f"전일 대비 조회수 {growth_rate:.1f}배 성장, 증가량 {delta_views:,}회"
            elif delta_comments >= DAILY_COMMENT_DELTA_THRESHOLD:
                reason = f"전일 대비 댓글 {delta_comments:,}개 증가"

        else:
            upload_date_text = str(meta.get("upload_date", ""))
            is_recent_upload = False

            if upload_date_text:
                try:
                    upload_dt = datetime.strptime(upload_date_text, "%Y-%m-%d").date()
                    is_recent_upload = upload_dt >= today - timedelta(days=7)
                except Exception:
                    pass

            if is_recent_upload and view_count >= NEW_VIDEO_HIGH_VIEW_THRESHOLD:
                reason = f"최근 업로드 영상이 조회수 {view_count:,}회 기록"

        if not reason:
            continue

        title = normalize_text(meta.get("title", ""))
        related = normalize_text(meta.get("related_content", ""))
        keywords = normalize_text(meta.get("keywords", ""))
        url = normalize_text(meta.get("url", ""))
        channel = normalize_text(meta.get("channel", ""))

        desc = (
            f"유튜브 영상 '{title}'이 {reason}. "
            f"채널: {channel}. "
            f"조회수 {view_count:,}회, 댓글 {comment_count:,}개 기준으로 실제 반응 증가 신호가 확인됨."
        )

        rows.append({
            "date": today_str,
            "source": "유튜브",
            "issue_title": f"{related or title} 관련 유튜브 반응 급등",
            "related_content": related,
            "keywords": keywords,
            "description": desc[:260],
            "source_url": url,
            "sort_delta": delta_views,
            "sort_views": view_count,
        })

    rows = sorted(
        rows,
        key=lambda x: (x["sort_delta"], x["sort_views"]),
        reverse=True
    )

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

    return cleaned[:10]


def load_existing_issues():
    return load_csv(ISSUE_PATH, ISSUE_COLUMNS)


def save_issue_feed(new_rows):
    existing = load_existing_issues()

    if not new_rows:
        save_csv(existing, ISSUE_PATH, ISSUE_COLUMNS)
        return 0, len(existing)

    new_df = pd.DataFrame(new_rows, columns=ISSUE_COLUMNS).fillna("")

    merged = pd.concat([existing, new_df], ignore_index=True)

    merged["dedup_key"] = merged.apply(
        lambda r: str(r["source_url"]).strip() if str(r["source_url"]).strip() else str(r["issue_title"]).strip(),
        axis=1
    )

    merged = merged.drop_duplicates(subset=["dedup_key"], keep="last")
    merged = merged.drop(columns=["dedup_key"])

    merged["date_dt"] = pd.to_datetime(merged["date"], errors="coerce")
    cutoff = pd.Timestamp(today_kst_date() - timedelta(days=60))

    merged = merged[
        (merged["date_dt"].isna())
        | (merged["date_dt"] >= cutoff)
    ].copy()

    merged = merged.drop(columns=["date_dt"])
    merged = merged.sort_values("date", ascending=False)

    save_csv(merged, ISSUE_PATH, ISSUE_COLUMNS)

    return len(new_df), len(merged)


def main():
    print("외부 이슈 수집 시작")

    news_rows = collect_google_news()
    print(f"뉴스/공식자료 후보: {len(news_rows)}개")

    new_videos, _ = discover_youtube_videos()
    print(f"유튜브 신규 후보: {len(new_videos)}개")

    watchlist = update_youtube_watchlist(new_videos)
    print(f"유튜브 watchlist 전체: {len(watchlist)}개")

    stats = update_youtube_stats(watchlist)
    print(f"유튜브 stats 전체: {len(stats)}개")

    youtube_issue_rows = make_youtube_issues(watchlist, stats)
    print(f"유튜브 급등 이슈: {len(youtube_issue_rows)}개")

    all_rows = []
    all_rows.extend(news_rows)
    all_rows.extend(youtube_issue_rows)

    new_count, total_count = save_issue_feed(all_rows)

    print(f"issue_feed 신규 반영: {new_count}개")
    print(f"issue_feed 전체 누적: {total_count}개")


if __name__ == "__main__":
    main()
