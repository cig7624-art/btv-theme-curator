from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pandas as pd

CONTENT_COLUMNS = [
    "run_id", "theme_key", "theme_id", "theme_name", "rank", "title",
    "content_type", "year", "reason", "source_url", "poster_url",
    "kinolights_status", "matched_score", "fetched_at",
]


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[|,;/·]+", _clean(value))
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean(item)
        key = re.sub(r"[^0-9a-z가-힣]+", "", text.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def ensure_content_schema(frame: pd.DataFrame | None) -> pd.DataFrame:
    df = pd.DataFrame() if frame is None else frame.copy()
    for column in CONTENT_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[CONTENT_COLUMNS].fillna("")


def load_content_frame(path: Path) -> pd.DataFrame:
    if not path.exists():
        return ensure_content_schema(None)
    try:
        return ensure_content_schema(pd.read_csv(path, sep="|", dtype=str).fillna(""))
    except Exception:
        return ensure_content_schema(None)


def upsert_content_frame(path: Path, new_rows: pd.DataFrame) -> pd.DataFrame:
    incoming = ensure_content_schema(new_rows)
    current = load_content_frame(path)
    if incoming.empty:
        return current

    keys = set(
        zip(
            incoming["run_id"].astype(str),
            incoming["theme_key"].astype(str),
        )
    )
    if not current.empty:
        keep_mask = [
            (str(run_id), str(theme_key)) not in keys
            for run_id, theme_key in zip(current["run_id"], current["theme_key"])
        ]
        current = current.loc[keep_mask]

    combined = pd.concat([current, incoming], ignore_index=True).fillna("")
    combined["_rank_num"] = pd.to_numeric(combined["rank"], errors="coerce").fillna(9999)
    combined = combined.sort_values(["run_id", "theme_key", "_rank_num", "title"], kind="stable")
    combined = combined.drop(columns=["_rank_num"])
    path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(path, sep="|", index=False, encoding="utf-8-sig")
    return combined


def theme_rows_to_recommendations(theme_rows: pd.DataFrame) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for index, (_, row) in enumerate(theme_rows.iterrows(), start=1):
        theme_id = _clean(row.get("theme_id")) or f"DB{index:04d}"
        recommendations.append({
            "recommendation_source": _clean(row.get("source_status")) or "EXISTING_DB",
            "source_label": "테마 DB",
            "source_issue_summary": _clean(row.get("source_issue")),
            "rationale": "테마명·카피·장르·무드·키워드에 맞는 실제 작품 후보",
            "theme": {
                "theme_id": theme_id,
                "theme_name": _clean(row.get("theme_name")),
                "copy": _clean(row.get("copy")),
                "genre": _clean(row.get("genre")),
                "mood": _clean(row.get("mood")),
                "trigger_keywords": "|".join(_keywords(row.get("trigger_keywords"))),
                "source_issue": _clean(row.get("source_issue")),
            },
        })
    return recommendations


def ai_suggestions_to_recommendations(intent: dict[str, Any], query: str = "") -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    for index, item in enumerate((intent or {}).get("suggested_themes", []) or [], start=1):
        name = _clean(item.get("theme_name"))
        if not name:
            continue
        digest = hashlib.sha1(f"{query}|{name}".encode("utf-8")).hexdigest()[:10]
        recommendations.append({
            "recommendation_source": "AI_SUGGESTION",
            "source_label": "AI 신규 제안",
            "source_issue_summary": _clean((intent or {}).get("interpreted_request")),
            "rationale": "사용자의 자연어 요청에 맞춰 제안된 임시 테마",
            "theme": {
                "theme_id": f"AIS-{digest}",
                "theme_name": name,
                "copy": _clean(item.get("copy") or item.get("theme_copy")),
                "genre": _clean(item.get("genre")),
                "mood": _clean(item.get("mood")),
                "trigger_keywords": "|".join(_keywords(item.get("keywords"))),
                "source_issue": _clean((intent or {}).get("interpreted_request")),
            },
        })
    return recommendations


def content_map_for_scope(frame: pd.DataFrame, run_id: str) -> dict[str, list[dict[str, str]]]:
    df = ensure_content_schema(frame)
    if df.empty:
        return {}
    scoped = df[df["run_id"].astype(str) == str(run_id)].copy()
    if scoped.empty:
        return {}
    scoped["_rank_num"] = pd.to_numeric(scoped["rank"], errors="coerce").fillna(9999)
    scoped = scoped.sort_values(["theme_key", "_rank_num"], kind="stable")
    result: dict[str, list[dict[str, str]]] = {}
    for _, row in scoped.iterrows():
        key = _clean(row.get("theme_key"))
        if not key:
            continue
        result.setdefault(key, []).append({column: _clean(row.get(column)) for column in CONTENT_COLUMNS})
    return result


def missing_theme_rows(themes: pd.DataFrame, frame: pd.DataFrame, *, limit: int = 10) -> pd.DataFrame:
    content_map = content_map_for_scope(frame, "THEME_DB")
    missing = themes[~themes["theme_id"].astype(str).isin(set(content_map))].copy()
    return missing.head(max(1, int(limit)))


def stale_theme_rows(
    themes: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    limit: int = 10,
    stale_days: int = 30,
) -> pd.DataFrame:
    df = ensure_content_schema(frame)
    scoped = df[df["run_id"].astype(str) == "THEME_DB"].copy()
    if scoped.empty:
        return themes.head(max(1, int(limit))).copy()
    scoped["_fetched"] = pd.to_datetime(scoped["fetched_at"], errors="coerce", utc=True)
    latest = scoped.groupby("theme_key", as_index=False)["_fetched"].max()
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max(1, int(stale_days)))
    stale_ids = set(latest.loc[latest["_fetched"].isna() | (latest["_fetched"] < cutoff), "theme_key"].astype(str))
    missing_ids = set(themes["theme_id"].astype(str)) - set(latest["theme_key"].astype(str))
    target_ids = stale_ids | missing_ids
    selected = themes[themes["theme_id"].astype(str).isin(target_ids)].copy()
    return selected.head(max(1, int(limit)))


def content_coverage(themes: pd.DataFrame, frame: pd.DataFrame) -> dict[str, int]:
    content_map = content_map_for_scope(frame, "THEME_DB")
    connected = int(themes["theme_id"].astype(str).isin(set(content_map)).sum())
    return {
        "total_themes": int(len(themes)),
        "connected_themes": connected,
        "missing_themes": max(0, int(len(themes)) - connected),
        "candidate_rows": sum(len(items) for items in content_map.values()),
    }
