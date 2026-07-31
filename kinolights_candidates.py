from __future__ import annotations

import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin

import pandas as pd
import requests

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - optional parser fallback
    BeautifulSoup = None

KINOLIGHTS_SEARCH_URL = "https://m.kinolights.com/search?keyword={keyword}"
CACHE_PATH = Path("kinolights_search_cache.csv")
CACHE_COLUMNS = [
    "query", "requested_title", "title", "content_type", "year", "source_url",
    "poster_url", "status", "matched_score", "fetched_at",
]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", _clean(value).lower())


def _year(value: Any) -> str:
    match = re.search(r"(?:19|20)\d{2}", _clean(value))
    return match.group(0) if match else ""


def _type(value: Any) -> str:
    text = _clean(value)
    for token in ["영화", "드라마", "예능", "애니메이션", "다큐", "시리즈"]:
        if token in text:
            return token
    return ""


def _search_url(query: str) -> str:
    return KINOLIGHTS_SEARCH_URL.format(keyword=quote(_clean(query)))


def _similarity(left: Any, right: Any) -> float:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) >= 3 and (a in b or b in a):
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


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


def _save_cache(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    current = _load_cache()
    combined = pd.concat([current, pd.DataFrame(rows)], ignore_index=True).fillna("")
    combined = combined.drop_duplicates(subset=["query", "requested_title"], keep="last")
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined[CACHE_COLUMNS].to_csv(CACHE_PATH, sep="|", index=False, encoding="utf-8-sig")


def _cached(query: str, requested_title: str) -> dict[str, Any] | None:
    df = _load_cache()
    if df.empty:
        return None
    mask = (
        df["query"].astype(str).map(_norm).eq(_norm(query))
        & df["requested_title"].astype(str).map(_norm).eq(_norm(requested_title))
    )
    rows = df[mask]
    if rows.empty:
        return None
    return rows.iloc[-1].to_dict()


def _candidate_from_mapping(node: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    title = ""
    for key in ["titleKr", "title_kr", "title", "nameKr", "name_kr", "name"]:
        value = node.get(key)
        if isinstance(value, str) and 1 < len(_clean(value)) < 100:
            title = _clean(value)
            break
    if not title:
        return None

    content_id = _clean(node.get("id") or node.get("contentId") or node.get("content_id") or node.get("uuid"))
    url = _clean(node.get("url") or node.get("href") or node.get("path"))
    if url:
        url = urljoin(base_url, url)
    elif content_id:
        url = f"https://m.kinolights.com/title/{quote(content_id)}"
    poster = _clean(
        node.get("posterUrl") or node.get("poster_url") or node.get("poster")
        or node.get("imageUrl") or node.get("image_url") or node.get("image")
    )
    if poster:
        poster = urljoin(base_url, poster)
    metadata = " ".join(_clean(node.get(key)) for key in ["type", "contentType", "openYear", "year", "releaseYear"])
    return {
        "title": title,
        "content_type": _type(metadata),
        "year": _year(metadata),
        "source_url": url,
        "poster_url": poster,
    }


def _walk_json(value: Any, base_url: str, out: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        candidate = _candidate_from_mapping(value, base_url)
        if candidate:
            out.append(candidate)
        for child in value.values():
            _walk_json(child, base_url, out)
    elif isinstance(value, list):
        for child in value:
            _walk_json(child, base_url, out)


def _parse_html(html_text: str, base_url: str) -> list[dict[str, Any]]:
    if not html_text or BeautifulSoup is None:
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    rows: list[dict[str, Any]] = []

    # Next/React payloads and JSON-LD are the most stable path when present.
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        script_id = script.get("id") or ""
        if script_type not in {"application/json", "application/ld+json"} and script_id != "__NEXT_DATA__":
            continue
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        _walk_json(payload, base_url, rows)

    # Server-rendered anchors, if the site exposes them.
    for anchor in soup.find_all("a", href=True):
        href = _clean(anchor.get("href"))
        if not href or not any(token in href for token in ["/title/", "/content/", "/contents/", "/movie/", "/tv/"]):
            continue
        img = anchor.find("img")
        title = _clean((img.get("alt") if img else "") or anchor.get_text(" ", strip=True))
        if not title:
            continue
        text = _clean(anchor.get_text(" ", strip=True))
        rows.append({
            "title": title,
            "content_type": _type(text),
            "year": _year(text),
            "source_url": urljoin(base_url, href),
            "poster_url": urljoin(base_url, _clean(img.get("src"))) if img and img.get("src") else "",
        })

    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{_norm(row.get('title'))}:{row.get('year','')}"
        if key and key not in unique:
            unique[key] = row
    return list(unique.values())


def _requests_search(query: str, timeout: int = 15) -> tuple[list[dict[str, Any]], str]:
    url = _search_url(query)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6",
    }
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return _parse_html(response.text, response.url), ""
    except Exception as exc:
        return [], str(exc)


def _chromium_path() -> str:
    for path in [
        shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome"),
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    ]:
        if path and Path(path).exists():
            return str(path)
    return ""


def _browser_search(query: str, timeout_ms: int = 18000) -> tuple[list[dict[str, Any]], str]:
    """Optional fallback. It is used only when a system Chromium is available."""
    executable = _chromium_path()
    if not executable:
        return [], "system Chromium unavailable"
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return [], f"playwright unavailable: {exc}"

    url = _search_url(query)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path=executable,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = browser.new_page(locale="ko-KR", user_agent="Mozilla/5.0 Chrome/150 Safari/537.36")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2500)
            raw = page.locator("a[href]").evaluate_all(
                """els => els.map(a => ({
                    href: a.href || '',
                    text: (a.innerText || '').trim(),
                    alt: (a.querySelector('img')?.alt || '').trim(),
                    src: (a.querySelector('img')?.src || '').trim()
                }))"""
            )
            browser.close()
    except Exception as exc:
        return [], str(exc)

    rows: list[dict[str, Any]] = []
    for item in raw:
        href = _clean(item.get("href"))
        if not href or not any(token in href for token in ["/title/", "/content/", "/contents/", "/movie/", "/tv/"]):
            continue
        text = _clean(item.get("text"))
        title = _clean(item.get("alt") or text)
        if not title:
            continue
        rows.append({
            "title": title,
            "content_type": _type(text),
            "year": _year(text),
            "source_url": href,
            "poster_url": _clean(item.get("src")),
        })
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{_norm(row.get('title'))}:{row.get('year','')}"
        if key and key not in unique:
            unique[key] = row
    return list(unique.values()), ""


def verify_title_on_kinolights(
    requested_title: str,
    *,
    requested_year: str = "",
    requested_type: str = "",
    allow_browser_fallback: bool = False,
) -> dict[str, Any]:
    title = _clean(requested_title)
    query = title
    cached = _cached(query, title)
    if cached:
        return cached

    rows, request_error = _requests_search(query)
    browser_error = ""
    if not rows and allow_browser_fallback:
        rows, browser_error = _browser_search(query)

    best: dict[str, Any] | None = None
    best_score = 0.0
    for row in rows:
        score = _similarity(title, row.get("title"))
        if requested_year and row.get("year") == requested_year:
            score += 0.05
        if requested_type and row.get("content_type") == requested_type:
            score += 0.03
        if score > best_score:
            best_score = score
            best = row

    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if best is not None and best_score >= 0.72:
        result = {
            "query": query,
            "requested_title": title,
            "title": best.get("title") or title,
            "content_type": best.get("content_type") or requested_type,
            "year": best.get("year") or requested_year,
            "source_url": best.get("source_url") or _search_url(title),
            "poster_url": best.get("poster_url", ""),
            "status": "verified",
            "matched_score": round(min(best_score, 1.0), 3),
            "fetched_at": fetched_at,
        }
    else:
        # Even if Kinolights blocks server-side parsing, the user still gets a working exact-title search link.
        result = {
            "query": query,
            "requested_title": title,
            "title": title,
            "content_type": requested_type,
            "year": requested_year,
            "source_url": _search_url(title),
            "poster_url": "",
            "status": "search_link_only",
            "matched_score": 0,
            "fetched_at": fetched_at,
            "error": request_error or browser_error,
        }
    _save_cache([result])
    return result


def validate_content_suggestions(
    theme_suggestions: dict[str, list[dict[str, Any]]],
    *,
    max_workers: int = 8,
    allow_browser_fallback: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Validate LLM title suggestions against Kinolights search pages in parallel."""
    tasks: list[tuple[str, dict[str, Any]]] = []
    for theme_key, suggestions in theme_suggestions.items():
        for suggestion in suggestions:
            if _clean(suggestion.get("title")):
                tasks.append((theme_key, suggestion))

    output: dict[str, list[dict[str, Any]]] = {key: [] for key in theme_suggestions}
    verified = 0
    link_only = 0
    errors: list[str] = []

    def worker(theme_key: str, suggestion: dict[str, Any]):
        result = verify_title_on_kinolights(
            suggestion.get("title", ""),
            requested_year=_year(suggestion.get("year", "")),
            requested_type=_type(suggestion.get("content_type", "")) or _clean(suggestion.get("content_type", "")),
            allow_browser_fallback=allow_browser_fallback,
        )
        result["reason"] = _clean(suggestion.get("reason", ""))
        result["theme_key"] = theme_key
        return theme_key, result

    workers = max(1, min(max_workers, len(tasks) or 1))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, key, item) for key, item in tasks]
        for future in as_completed(futures):
            try:
                theme_key, result = future.result()
                output.setdefault(theme_key, []).append(result)
                if result.get("status") == "verified":
                    verified += 1
                else:
                    link_only += 1
                    if result.get("error"):
                        errors.append(str(result.get("error")))
            except Exception as exc:
                errors.append(str(exc))

    # Keep the LLM order as much as possible.
    for theme_key, original in theme_suggestions.items():
        order = {_norm(item.get("title")): idx for idx, item in enumerate(original)}
        output[theme_key].sort(key=lambda item: order.get(_norm(item.get("requested_title") or item.get("title")), 999))

    return output, {
        "status": "ok" if tasks else "empty",
        "requested_count": len(tasks),
        "verified_count": verified,
        "search_link_only_count": link_only,
        "errors": list(dict.fromkeys(errors))[:5],
    }


def content_rows_to_frame(
    run_id: str,
    recommendations: Iterable[dict[str, Any]],
    validated: dict[str, list[dict[str, Any]]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    recommendation_list = list(recommendations)
    for index, rec in enumerate(recommendation_list, start=1):
        theme = rec.get("theme", {}) or {}
        theme_id = _clean(theme.get("theme_id"))
        theme_name = _clean(theme.get("theme_name"))
        theme_key = theme_id or f"R{index:02d}"
        for rank, content in enumerate(validated.get(theme_key, []), start=1):
            rows.append({
                "run_id": run_id,
                "theme_key": theme_key,
                "theme_id": theme_id,
                "theme_name": theme_name,
                "rank": rank,
                "title": _clean(content.get("title")),
                "content_type": _clean(content.get("content_type")),
                "year": _year(content.get("year")),
                "reason": _clean(content.get("reason")),
                "source_url": _clean(content.get("source_url")),
                "poster_url": _clean(content.get("poster_url")),
                "kinolights_status": _clean(content.get("status")),
                "matched_score": content.get("matched_score", 0),
                "fetched_at": _clean(content.get("fetched_at")),
            })
    return pd.DataFrame(rows)
