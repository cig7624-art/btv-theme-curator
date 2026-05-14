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


OUTPUT_PATH = Path("issue_feed.csv")
DAYS = 7

COLUMNS = [
    "date",
    "source",
    "issue_title",
    "related_content",
    "keywords",
    "description",
    "source_url",
]

# 실제 이슈를 찾을 검색 축
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
    "유튜브 예능 클립 화제",
    "쇼츠 영화 리뷰 화제",
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
]

KEYWORD_LEXICON = [
    "드라마", "예능", "영화", "OTT", "넷플릭스", "티빙", "웨이브", "디즈니플러스",
    "키노라이츠", "왓챠피디아", "박스오피스", "시청률", "신작", "공개", "라인업",
    "쇼츠", "유튜브", "릴스", "SNS", "클립", "화제", "리뷰", "해석", "결말",
    "반전", "공포", "스릴러", "로맨스", "로코", "가족", "애니", "키즈",
    "요리", "먹방", "여행", "힐링", "서바이벌", "경쟁", "음악", "아이돌",
    "배우", "감독", "인터뷰", "출연", "합류", "복귀", "흥행", "랭킹",
]

SOURCE_MAP = {
    "youtube": "유튜브",
    "news": "뉴스/공식자료",
}


def today_kst_date():
    # GitHub Actions는 UTC라서 단순 날짜보다 KST 기준이 안전함
    return (datetime.utcnow() + timedelta(hours=9)).date()


def normalize_text(text):
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", " ", text).strip()


def extract_keywords(text):
    text = normalize_text(text)
    found = []

    for kw in KEYWORD_LEXICON:
        if kw.lower() in text.lower():
            found.append(kw)

    # 너무 적으면 제목에서 의미 단어 일부 보강
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    for token in tokens[:8]:
        if token not in found and len(found) < 12:
            found.append(token)

    return ",".join(found[:12])


def guess_related_content(title):
    title = normalize_text(title)

    # 《작품명》 형태 우선 추출
    m = re.search(r"《([^》]+)》", title)
    if m:
        return m.group(1)

    # 따옴표 작품명 형태 추출
    m = re.search(r"[\"“']([^\"”']+)[\"”']", title)
    if m:
        return m.group(1)

    # 쉼표/하이픈 앞쪽을 후보로 사용
    title = re.sub(r"\[[^\]]+\]", "", title).strip()
    for sep in [" - ", " | ", "…", ":", "："]:
        if sep in title:
            return title.split(sep)[0].strip()[:30]

    return title[:30]


def make_description(source_type, title, summary=""):
    title = normalize_text(title)
    summary = normalize_text(summary)

    if source_type == "유튜브":
        return f"유튜브 검색 결과에서 '{title}' 관련 영상이 노출됨. 쇼츠/클립/리뷰/해석형 소비 신호로 활용 가능."

    if summary:
        return summary[:180]

    return f"외부 뉴스/공식자료에서 '{title}' 관련 이슈가 확인됨."


def parse_entry_date(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6]).date()

    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        return datetime(*entry.updated_parsed[:6]).date()

    return today_kst_date()


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

            rows.append({
                "date": published_date.strftime("%Y-%m-%d"),
                "source": "뉴스/공식자료",
                "issue_title": title[:120],
                "related_content": related,
                "keywords": keywords,
                "description": make_description("뉴스/공식자료", title, summary),
                "source_url": link,
            })

    return rows


def collect_youtube():
    rows = []

    if yt_dlp is None:
        return rows

    today = today_kst_date()

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": True,
        "ignoreerrors": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for query in YOUTUBE_QUERIES:
            try:
                result = ydl.extract_info(f"ytsearch8:{query}", download=False)
            except Exception:
                continue

            entries = result.get("entries", []) if result else []

            for item in entries:
                if not item:
                    continue

                title = normalize_text(item.get("title", ""))
                url = item.get("url", "")

                if url and not str(url).startswith("http"):
                    url = f"https://www.youtube.com/watch?v={url}"

                if not title or not url:
                    continue

                channel = normalize_text(item.get("channel", "") or item.get("uploader", ""))
                related = guess_related_content(title)
                keywords = extract_keywords(f"{query} {title} {channel}")

                rows.append({
                    "date": today.strftime("%Y-%m-%d"),
                    "source": "유튜브",
                    "issue_title": title[:120],
                    "related_content": related,
                    "keywords": keywords,
                    "description": make_description("유튜브", title),
                    "source_url": url,
                })

    return rows


def load_existing():
    if not OUTPUT_PATH.exists():
        return pd.DataFrame(columns=COLUMNS)

    try:
        df = pd.read_csv(OUTPUT_PATH, sep="|").fillna("")
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    except Exception:
        return pd.DataFrame(columns=COLUMNS)


def save_issue_feed(new_rows):
    existing = load_existing()
    new_df = pd.DataFrame(new_rows, columns=COLUMNS).fillna("")

    merged = pd.concat([existing, new_df], ignore_index=True)

    # URL이 있으면 URL 기준, 없으면 제목 기준 중복 제거
    merged["dedup_key"] = merged.apply(
        lambda r: r["source_url"] if str(r["source_url"]).strip() else r["issue_title"],
        axis=1
    )

    merged = merged.drop_duplicates(subset=["dedup_key"], keep="last")
    merged = merged.drop(columns=["dedup_key"])

    # 너무 오래된 데이터는 일단 60일만 보관
    merged["date_dt"] = pd.to_datetime(merged["date"], errors="coerce")
    cutoff = pd.Timestamp(today_kst_date() - timedelta(days=60))

    merged = merged[
        (merged["date_dt"].isna())
        | (merged["date_dt"] >= cutoff)
    ].copy()

    merged = merged.drop(columns=["date_dt"])
    merged = merged.sort_values("date", ascending=False)

    merged.to_csv(
        OUTPUT_PATH,
        sep="|",
        index=False,
        encoding="utf-8",
        quoting=csv.QUOTE_MINIMAL,
    )

    return len(new_df), len(merged)


def main():
    rows = []
    rows.extend(collect_google_news())
    rows.extend(collect_youtube())

    if not rows:
        print("수집된 신규 이슈가 없습니다.")
        return

    new_count, total_count = save_issue_feed(rows)

    print(f"신규 수집 후보: {new_count}개")
    print(f"issue_feed.csv 전체 누적: {total_count}개")


if __name__ == "__main__":
    main()
