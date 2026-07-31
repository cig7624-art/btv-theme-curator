from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import pandas as pd
import requests

GRAPHQL_URL = "https://gateway.kinolights.com/graphql"
CACHE_PATH = Path("kinolights_content_cache.csv")
CACHE_TTL_DAYS = 14
CACHE_WRITE_LOCK = threading.Lock()
CACHE_COLUMNS = [
    "query",
    "content_id",
    "title",
    "title_en",
    "year",
    "source_url",
    "fetched_at",
]

KOREAN_STOPWORDS = {
    "영화", "드라마", "예능", "콘텐츠", "작품", "추천", "테마", "이야기",
    "보고", "보면", "좋은", "싶은", "관련", "한국", "이번주", "화제",
    "신작", "공개", "영상", "반응", "노출", "카피", "장르", "무드",
}


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_title(value: Any) -> str:
    text = _clean_text(value).lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _split_terms(value: Any) -> list[str]:
    raw = re.split(r"[,/|·\s]+", _clean_text(value))
    out: list[str] = []
    seen: set[str] = set()
    for token in raw:
        token = token.strip()
        if len(token) < 2 or token in KOREAN_STOPWORDS:
            continue
        key = token.lower()
        if key not in seen:
            seen.add(key)
            out.append(token)
    return out


def _safe_year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", _clean_text(value))
    return match.group(0) if match else ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_cache() -> pd.DataFrame:
    if not CACHE_PATH.exists():
        return pd.DataFrame(columns=CACHE_COLUMNS)
    try:
        df = pd.read_csv(CACHE_PATH, sep="|", dtype=str).fillna("")
    except Exception:
        return pd.DataFrame(columns=CACHE_COLUMNS)
    for column in CACHE_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[CACHE_COLUMNS]


def _save_cache(df: pd.DataFrame) -> None:
    if df.empty:
        return
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    normalized = df.copy()
    for column in CACHE_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[CACHE_COLUMNS].fillna("")
    normalized = normalized.drop_duplicates(
        subset=["query", "content_id", "title"], keep="last"
    )
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False,
        dir=str(CACHE_PATH.parent), suffix=".tmp"
    ) as tmp:
        temp_path = Path(tmp.name)
        normalized.to_csv(tmp, sep="|", index=False, quoting=csv.QUOTE_MINIMAL)
    temp_path.replace(CACHE_PATH)


def _cached_results(query: str, cache_df: pd.DataFrame) -> list[dict[str, Any]]:
    if cache_df.empty:
        return []
    rows = cache_df[cache_df["query"].astype(str) == query].copy()
    if rows.empty:
        return []
    fetched = pd.to_datetime(rows["fetched_at"], errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=CACHE_TTL_DAYS)
    rows = rows[fetched >= cutoff]
    if rows.empty:
        return []
    return rows.to_dict("records")


def _graphql_query(limit: int) -> str:
    safe_limit = max(1, min(int(limit), 20))
    return f"""
    query SearchContents($keyword: String!) {{
      contents(keyword: $keyword, limit: {safe_limit}) {{
        id
        titleKr
        titleEn
        openYear
      }}
    }}
    """


def search_kinolights(
    keyword: str,
    *,
    limit: int = 12,
    timeout: int = 15,
    cache_df: pd.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Return (rows, cache_hit, error_message)."""
    query = _clean_text(keyword)
    if not query:
        return [], False, "빈 검색어"

    cache_df = _load_cache() if cache_df is None else cache_df
    cached = _cached_results(query, cache_df)
    if cached:
        return cached[:limit], True, ""

    payload = {
        "operationName": "SearchContents",
        "variables": {"keyword": query},
        "query": _graphql_query(limit),
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://m.kinolights.com",
        "Referer": "https://m.kinolights.com/search",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
    }

    try:
        response = requests.post(
            GRAPHQL_URL,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        return [], False, f"키노라이츠 호출 실패: {exc}"

    if data.get("errors"):
        compact = json.dumps(data.get("errors"), ensure_ascii=False)[:350]
        return [], False, f"키노라이츠 응답 오류: {compact}"

    raw_rows = data.get("data", {}).get("contents", []) or []
    fetched_at = _utc_now_iso()
    results: list[dict[str, Any]] = []
    for row in raw_rows:
        title = _clean_text(row.get("titleKr") or row.get("titleEn"))
        content_id = _clean_text(row.get("id"))
        if not title or not content_id:
            continue
        results.append({
            "query": query,
            "content_id": content_id,
            "title": title,
            "title_en": _clean_text(row.get("titleEn")),
            "year": _safe_year(row.get("openYear")),
            # 과거부터 사용해온 키노라이츠 통합 상세 경로입니다. 사이트가 변경되면 검색 페이지로 보완합니다.
            "source_url": f"https://m.kinolights.com/title/{quote(content_id)}",
            "fetched_at": fetched_at,
        })

    if results:
        try:
            # 병렬 검색 중에도 다른 검색어의 캐시가 덮어써지지 않도록 최신 파일을 다시 합칩니다.
            with CACHE_WRITE_LOCK:
                latest_cache = _load_cache()
                combined = pd.concat([latest_cache, pd.DataFrame(results)], ignore_index=True)
                _save_cache(combined)
        except Exception:
            # 캐시 저장 실패는 실시간 검색 결과 노출을 막지 않습니다.
            pass

    return results[:limit], False, ""


def _append_query(
    target: list[dict[str, Any]],
    seen: set[str],
    text: Any,
    origin: str,
    weight: float,
) -> None:
    query = _clean_text(text)
    key = query.lower()
    if len(query) < 2 or key in seen:
        return
    seen.add(key)
    target.append({"query": query, "origin": origin, "weight": float(weight)})


def build_search_queries(
    theme: Any,
    matched_issues: Iterable[dict[str, Any]] | None = None,
    *,
    max_queries: int = 7,
) -> list[dict[str, Any]]:
    """Build title-oriented Kinolights queries from the selected theme and its issues."""
    matched_issues = list(matched_issues or [])
    queries: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) 실제 이슈에 잡힌 작품명은 가장 강한 앵커입니다.
    for issue in matched_issues[:4]:
        related = _clean_text(issue.get("related_content"))
        if related and related.lower() not in {"영화", "드라마", "예능", "콘텐츠", "신작"}:
            _append_query(queries, seen, related, "최근 이슈 작품명", 32)

    # 2) 테마명은 문장형일 수 있지만 현재 키노 검색이 어디까지 인식하는지 시험합니다.
    _append_query(queries, seen, theme.get("theme_name", ""), "테마명", 22)

    # 3) 핵심 키워드 단독 검색. 제목 검색 성격상 결과가 없을 수 있으므로 적은 수만 사용합니다.
    keywords = _split_terms(theme.get("trigger_keywords", ""))
    for keyword in keywords[:4]:
        _append_query(queries, seen, keyword, "테마 키워드", 12)

    # 4) 장르·무드는 후보가 부족할 때만 보조합니다.
    if len(queries) < max_queries:
        for term in _split_terms(theme.get("genre", ""))[:2]:
            _append_query(queries, seen, term, "장르", 8)
            if len(queries) >= max_queries:
                break
    if len(queries) < max_queries:
        for term in _split_terms(theme.get("mood", ""))[:1]:
            _append_query(queries, seen, term, "무드", 6)

    return queries[:max_queries]


def _candidate_key(row: dict[str, Any]) -> str:
    content_id = _clean_text(row.get("content_id"))
    if content_id:
        return f"id:{content_id}"
    return f"title:{_normalize_title(row.get('title'))}:{_safe_year(row.get('year'))}"


def _franchise_stem(title: str) -> str:
    value = re.sub(r"(?:시즌\s*\d+|season\s*\d+|\d+)$", "", _clean_text(title).lower())
    value = re.sub(r"[^0-9a-z가-힣]+", " ", value).strip()
    tokens = value.split()
    return " ".join(tokens[:2]) if tokens else value


def _diversify(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    decade_counts: dict[str, int] = {}
    franchise_counts: dict[str, int] = {}

    for row in sorted(rows, key=lambda item: item.get("score", 0), reverse=True):
        if len(selected) >= limit:
            break
        year = _safe_year(row.get("year"))
        decade = f"{year[:3]}0" if year else "unknown"
        stem = _franchise_stem(_clean_text(row.get("title")))

        # 특정 시리즈·연대가 결과를 독점하지 않도록 느슨하게 제한합니다.
        if stem and franchise_counts.get(stem, 0) >= 2:
            continue
        if decade != "unknown" and decade_counts.get(decade, 0) >= max(3, limit // 3):
            continue

        selected.append(row)
        franchise_counts[stem] = franchise_counts.get(stem, 0) + 1
        decade_counts[decade] = decade_counts.get(decade, 0) + 1

    # 제한 때문에 수가 모자라면 순수 점수순으로 채웁니다.
    if len(selected) < limit:
        used = {_candidate_key(row) for row in selected}
        for row in sorted(rows, key=lambda item: item.get("score", 0), reverse=True):
            key = _candidate_key(row)
            if key in used:
                continue
            selected.append(row)
            used.add(key)
            if len(selected) >= limit:
                break

    return selected[:limit]


def collect_kinolights_candidates(
    theme: Any,
    matched_issues: Iterable[dict[str, Any]] | None = None,
    *,
    limit: int = 12,
    max_queries: int = 7,
    per_query_limit: int = 12,
    max_workers: int = 6,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query_specs = build_search_queries(theme, matched_issues, max_queries=max_queries)
    if not query_specs:
        return [], {
            "status": "no_query",
            "query_count": 0,
            "raw_count": 0,
            "unique_count": 0,
            "cache_hits": 0,
            "errors": ["검색어를 만들지 못했습니다."],
            "queries": [],
        }

    cache_df = _load_cache()
    results_by_query: dict[str, tuple[list[dict[str, Any]], bool, str]] = {}
    worker_count = max(1, min(max_workers, len(query_specs)))

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                search_kinolights,
                spec["query"],
                limit=per_query_limit,
                cache_df=cache_df,
            ): spec
            for spec in query_specs
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                results_by_query[spec["query"]] = future.result()
            except Exception as exc:
                results_by_query[spec["query"]] = ([], False, str(exc))

    aggregate: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    cache_hits = 0
    raw_count = 0

    for spec in query_specs:
        query = spec["query"]
        rows, cache_hit, error = results_by_query.get(query, ([], False, "결과 없음"))
        if cache_hit:
            cache_hits += 1
        if error:
            errors.append(f"{query}: {error}")
        raw_count += len(rows)

        for rank, row in enumerate(rows, start=1):
            key = _candidate_key(row)
            item = aggregate.get(key)
            rank_score = max(per_query_limit - rank + 1, 1)
            path_score = float(spec["weight"]) + rank_score
            if item is None:
                item = {
                    "content_id": row.get("content_id", ""),
                    "title": row.get("title", ""),
                    "title_en": row.get("title_en", ""),
                    "type": "키노라이츠",
                    "genre": "",
                    "year": row.get("year", ""),
                    "tags": "",
                    "source_url": row.get("source_url", ""),
                    "source": "키노라이츠 실시간 검색",
                    "score": 0.0,
                    "matched_queries": [],
                    "matched_origins": [],
                }
                aggregate[key] = item
            item["score"] += path_score
            if query not in item["matched_queries"]:
                item["matched_queries"].append(query)
            if spec["origin"] not in item["matched_origins"]:
                item["matched_origins"].append(spec["origin"])

    candidates = list(aggregate.values())
    for item in candidates:
        # 서로 다른 검색 경로에서 반복 발견된 작품을 우선합니다.
        item["score"] += max(len(item["matched_queries"]) - 1, 0) * 14
        item["tags"] = ",".join(item["matched_queries"][:4])
        item["match_reason"] = " · ".join(item["matched_origins"][:3])
        item["score"] = round(float(item["score"]), 1)

    selected = _diversify(candidates, max(1, int(limit)))
    status = "ok" if selected else ("error" if errors else "empty")
    meta = {
        "status": status,
        "query_count": len(query_specs),
        "raw_count": raw_count,
        "unique_count": len(candidates),
        "selected_count": len(selected),
        "cache_hits": cache_hits,
        "errors": errors[:6],
        "queries": query_specs,
        "note": (
            "키노라이츠 검색은 제목 중심 검색이라 테마 문장·추상 키워드는 결과가 적을 수 있습니다. "
            "최근 이슈의 실제 작품명이 있을 때 후보 품질이 가장 높습니다."
        ),
    }
    return selected, meta
