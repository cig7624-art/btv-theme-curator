from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import requests
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field


THEME_OPTIONAL_DEFAULTS: dict[str, str] = {
    "source_status": "LEGACY_UNVERIFIED",
    "created_date": "",
    "source_issue": "",
    "creation_angle": "",
    "approved_status": "UNVERIFIED",
    "last_recommended_date": "",
    "content_search_terms": "",
    "generation_model": "",
}

HISTORY_COLUMNS = [
    "run_id",
    "recommended_date",
    "theme_id",
    "theme_name",
    "recommendation_source",
    "source_issue",
    "creation_angle",
    "generation_model",
]


class ThemeGenerationError(RuntimeError):
    """Raised when an LLM theme generation run cannot be completed."""


class GeneratedTheme(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    theme_name: str = Field(description="B tv+ 화면에 노출할 한국어 큐레이션 테마명")
    display_copy: str = Field(alias="copy", description="테마를 보조하는 짧은 한국어 카피")
    genre: str = Field(description="대표 장르. 복수 장르는 /로 구분")
    mood: str = Field(description="대표 감정 또는 분위기")
    keywords: list[str] = Field(description="테마를 설명하는 핵심 키워드 5~10개")
    creation_angle: str = Field(description="소재형/상황형/감정형/인물형/관계형/공간형/해석형/장르변주형 중 하나")
    source_issue_keys: list[str] = Field(description="생성 근거가 된 이슈 ID 목록")
    source_issue_summary: str = Field(description="이 테마가 어떤 최근 이슈에서 나왔는지 한 문장 설명")
    content_search_terms: list[str] = Field(description="추후 콘텐츠 검색에 사용할 검색어 5~8개")
    rationale: str = Field(description="여러 작품을 묶는 테마로서 유효한 이유")


class GeneratedThemeBatch(BaseModel):
    themes: list[GeneratedTheme]


class SelectedThemeDecision(BaseModel):
    source: Literal["NEW", "EXISTING"]
    candidate_index: int | None = Field(description="NEW인 경우 1부터 시작하는 신규 후보 번호, EXISTING이면 null")
    existing_theme_id: str | None = Field(description="EXISTING인 경우 기존 테마 ID, NEW이면 null")
    relevance_score: int = Field(ge=0, le=100)
    novelty_score: int = Field(ge=0, le=100)
    quality_score: int = Field(ge=0, le=100)
    reason: str


class ThemeSelectionBatch(BaseModel):
    selected: list[SelectedThemeDecision]


def normalize_theme_text(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return text


def _clean_short_text(value: Any, max_length: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_length].strip()


def _dedupe_strings(values: list[Any], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_short_text(value, 60)
        key = normalize_theme_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def ensure_theme_schema(themes: pd.DataFrame) -> pd.DataFrame:
    df = themes.copy()
    required = ["theme_id", "theme_name", "trigger_keywords", "genre", "mood", "copy"]
    for column in required:
        if column not in df.columns:
            df[column] = ""
    for column, default in THEME_OPTIONAL_DEFAULTS.items():
        if column not in df.columns:
            df[column] = default
        else:
            df[column] = df[column].replace("", default) if column in {"source_status", "approved_status"} else df[column]
    return df.fillna("")


def load_recommendation_history(path: Path, days: int = 56) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    try:
        history = pd.read_csv(path, sep="|").fillna("")
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    for column in HISTORY_COLUMNS:
        if column not in history.columns:
            history[column] = ""
    dates = pd.to_datetime(history["recommended_date"], errors="coerce")
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    return history[(dates >= cutoff) | dates.isna()].copy()


def _issue_payload(issue_records: list[dict[str, Any]], max_issues: int = 12) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for index, issue in enumerate(issue_records[:max_issues], start=1):
        issue_key = str(issue.get("issue_key") or f"I{index:02d}")
        payload.append({
            "issue_key": issue_key,
            "title": _clean_short_text(issue.get("issue_title", ""), 160),
            "related_content": _clean_short_text(issue.get("related_content", ""), 100),
            "source_group": _clean_short_text(issue.get("source_group", ""), 50),
            "confirmed_routes": _clean_short_text(issue.get("confirmed_routes", ""), 120),
            "issue_score": int(float(issue.get("issue_score", 0) or 0)),
            "keywords": _dedupe_strings(str(issue.get("keywords", "")).replace("/", ",").split(","), 16),
            "description": _clean_short_text(issue.get("description", ""), 500),
        })
    return payload


def _sanitize_generated(themes: list[GeneratedTheme], candidate_count: int) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for theme in themes:
        name = _clean_short_text(theme.theme_name, 42)
        key = normalize_theme_text(name)
        if len(key) < 4 or key in seen_names:
            continue
        seen_names.add(key)
        keywords = _dedupe_strings(theme.keywords, 10)
        search_terms = _dedupe_strings(theme.content_search_terms, 8)
        cleaned.append({
            "theme_name": name,
            "copy": _clean_short_text(theme.display_copy, 58),
            "genre": _clean_short_text(theme.genre, 40),
            "mood": _clean_short_text(theme.mood, 30),
            "trigger_keywords": ",".join(keywords),
            "keywords": keywords,
            "creation_angle": _clean_short_text(theme.creation_angle, 20),
            "source_issue_keys": _dedupe_strings(theme.source_issue_keys, 5),
            "source_issue_summary": _clean_short_text(theme.source_issue_summary, 180),
            "content_search_terms": search_terms,
            "rationale": _clean_short_text(theme.rationale, 240),
        })
        if len(cleaned) >= candidate_count:
            break
    return cleaned


def _generate_candidates(
    client: OpenAI,
    model: str,
    issues_payload: list[dict[str, Any]],
    recent_history: pd.DataFrame,
    candidate_count: int,
) -> list[dict[str, Any]]:
    recent_names = []
    if not recent_history.empty:
        recent_names = _dedupe_strings(recent_history["theme_name"].astype(str).tolist(), 80)

    system_prompt = """
당신은 한국 IPTV의 콘텐츠 편성·큐레이션을 담당하는 시니어 에디터다.
최근 콘텐츠 이슈를 해석해, 여러 작품을 묶을 수 있는 신선한 큐레이션 테마를 새로 만든다.
이 단계에서는 기존 테마 DB를 전혀 보지 않는다. 최근 이슈 자체에서만 아이디어를 발산한다.
결과는 한국어로 작성하고, 작품 하나의 홍보 문구가 아니라 최소 8편 이상의 영화·드라마·예능을 묶을 수 있는 테마여야 한다.
"요즘 인기작", "주말에 보기 좋은 영화"처럼 너무 넓거나 뻔한 표현, 최근 이슈 제목을 그대로 붙인 표현, 서로 말만 바꾼 유사 테마를 피한다.
소재형·상황형·감정형·인물형·관계형·공간형·해석형·장르변주형을 고르게 활용한다.
테마명은 실제 B tv+ 화면에 노출할 수 있도록 짧고 매력적으로 쓴다.
""".strip()

    user_payload = {
        "requested_candidate_count": candidate_count,
        "recent_issues": issues_payload,
        "avoid_recently_recommended_theme_names": recent_names,
        "requirements": [
            "각 테마는 최근 이슈와 연결되지만 특정 작품 하나에 종속되지 않을 것",
            "테마끼리 관점과 어휘가 충분히 다를 것",
            "테마명, 짧은 카피, 장르, 무드, 키워드, 생성 관점, 근거 이슈 ID, 콘텐츠 검색어를 모두 작성할 것",
            "콘텐츠 검색어는 추후 키노라이츠 등에서 작품 후보를 찾기 쉬운 구체적인 표현일 것",
            f"가능한 한 정확히 {candidate_count}개를 생성할 것",
        ],
    }

    try:
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            text_format=GeneratedThemeBatch,
        )
    except Exception as exc:
        raise ThemeGenerationError(f"신규 테마 생성 API 호출 실패: {exc}") from exc

    parsed = response.output_parsed
    if parsed is None or not parsed.themes:
        raise ThemeGenerationError("LLM이 신규 테마를 반환하지 않았습니다.")
    return _sanitize_generated(parsed.themes, candidate_count)


def _existing_theme_payload(themes: pd.DataFrame) -> list[dict[str, Any]]:
    df = ensure_theme_schema(themes)
    result: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        result.append({
            "theme_id": str(row.get("theme_id", "")),
            "theme_name": _clean_short_text(row.get("theme_name", ""), 60),
            "copy": _clean_short_text(row.get("copy", ""), 80),
            "keywords": _dedupe_strings(str(row.get("trigger_keywords", "")).split(","), 12),
            "genre": _clean_short_text(row.get("genre", ""), 40),
            "mood": _clean_short_text(row.get("mood", ""), 30),
            "source_status": str(row.get("source_status", "LEGACY_UNVERIFIED")),
            "approved_status": str(row.get("approved_status", "UNVERIFIED")),
        })
    return result


def _review_and_select(
    client: OpenAI,
    model: str,
    candidates: list[dict[str, Any]],
    themes: pd.DataFrame,
    top_n: int,
) -> tuple[list[SelectedThemeDecision], str | None]:
    existing_payload = _existing_theme_payload(themes)
    numbered_candidates = [dict(candidate_index=index, **item) for index, item in enumerate(candidates, start=1)]

    system_prompt = """
당신은 콘텐츠 큐레이션 테마의 편집장이다.
1차 LLM이 기존 DB를 보지 않고 만든 신규 후보를 검수하고 최종 추천 목록을 고른다.
기존 DB는 검증된 정답지가 아니라 대부분 임시 장난감 데이터다. 따라서 기존 테마를 억지로 섞지 않는다.
기존 테마는 신규 후보와 사실상 같은 아이디어인데 문구와 완성도가 명백히 더 좋고 최근 이슈와도 정확히 맞을 때만 선택한다.
신규 후보가 더 신선하거나 완성도가 높으면 기존 DB와 비슷해도 신규 후보를 선택할 수 있다.
최종 목록은 최근 이슈와의 연결성, 실제 편성 가능성, 표현의 매력, 테마 간 다양성을 기준으로 정한다.
같은 이슈와 같은 관점에 과도하게 몰리지 않게 한다.
""".strip()

    user_payload = {
        "requested_final_count": top_n,
        "new_candidates": numbered_candidates,
        "existing_theme_db_unverified": existing_payload,
        "selection_rules": [
            f"최종 {top_n}개를 순서대로 선택",
            "기존 DB 활용은 0개여도 됨",
            "NEW이면 candidate_index를, EXISTING이면 existing_theme_id를 정확히 기입",
            "완전히 같은 아이디어를 두 번 선택하지 않음",
            "가능하면 서로 다른 최근 이슈와 생성 관점을 분산",
        ],
    }

    try:
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            text_format=ThemeSelectionBatch,
        )
        parsed = response.output_parsed
        if parsed is None:
            return [], "LLM 검수 결과가 비어 있습니다."
        return parsed.selected, None
    except Exception as exc:
        return [], f"사후 중복·품질 검수 호출 실패: {exc}"


def _candidate_to_recommendation(
    candidate: dict[str, Any],
    decision: SelectedThemeDecision | None,
    model: str,
) -> dict[str, Any]:
    return {
        "recommendation_source": "AI_GENERATED",
        "source_label": "AI 신규 생성",
        "theme": {
            "theme_id": "",
            "theme_name": candidate["theme_name"],
            "copy": candidate["copy"],
            "genre": candidate["genre"],
            "mood": candidate["mood"],
            "trigger_keywords": candidate["trigger_keywords"],
            "source_status": "AI_GENERATED",
            "created_date": date.today().isoformat(),
            "source_issue": candidate["source_issue_summary"],
            "creation_angle": candidate["creation_angle"],
            "approved_status": "UNVERIFIED",
            "last_recommended_date": date.today().isoformat(),
            "content_search_terms": ",".join(candidate["content_search_terms"]),
            "generation_model": model,
        },
        "source_issue_keys": candidate["source_issue_keys"],
        "source_issue_summary": candidate["source_issue_summary"],
        "creation_angle": candidate["creation_angle"],
        "rationale": candidate["rationale"],
        "content_search_terms": candidate["content_search_terms"],
        "relevance_score": decision.relevance_score if decision else 0,
        "novelty_score": decision.novelty_score if decision else 0,
        "quality_score": decision.quality_score if decision else 0,
        "selection_reason": decision.reason if decision else "LLM 1차 생성 후보 중 순서대로 보완 선택",
        "contents": [],
    }


def _existing_to_recommendation(
    row: pd.Series,
    decision: SelectedThemeDecision,
) -> dict[str, Any]:
    theme = {key: str(row.get(key, "")) for key in ensure_theme_schema(pd.DataFrame([row])).columns}
    return {
        "recommendation_source": "EXISTING_DB",
        "source_label": "기존 DB 활용",
        "theme": theme,
        "source_issue_keys": [],
        "source_issue_summary": str(row.get("source_issue", "")),
        "creation_angle": str(row.get("creation_angle", "기존 테마")) or "기존 테마",
        "rationale": "기존 테마가 최근 이슈와 직접 연결되고 신규 후보보다 표현 완성도가 높다고 판단되어 활용했습니다.",
        "content_search_terms": _dedupe_strings(str(row.get("content_search_terms", "")).split(","), 8),
        "relevance_score": decision.relevance_score,
        "novelty_score": decision.novelty_score,
        "quality_score": decision.quality_score,
        "selection_reason": decision.reason,
        "contents": [],
    }


def generate_weekly_themes(
    issue_records: list[dict[str, Any]],
    themes: pd.DataFrame,
    top_n: int,
    api_key: str,
    model: str = "gpt-5.6-terra",
    recent_history: pd.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not api_key:
        raise ThemeGenerationError("OPENAI_API_KEY가 설정되지 않았습니다.")
    if not issue_records:
        raise ThemeGenerationError("신규 테마를 만들 최근 핵심 이슈가 없습니다.")

    top_n = max(1, min(int(top_n), 30))
    candidate_count = min(max(top_n * 2, 30), 60)
    history = recent_history if recent_history is not None else pd.DataFrame(columns=HISTORY_COLUMNS)
    issues_payload = _issue_payload(issue_records)
    client = OpenAI(api_key=api_key)

    candidates = _generate_candidates(
        client=client,
        model=model,
        issues_payload=issues_payload,
        recent_history=history,
        candidate_count=candidate_count,
    )
    if len(candidates) < top_n:
        raise ThemeGenerationError(f"신규 테마 후보가 {len(candidates)}개만 생성되어 최종 {top_n}개를 만들 수 없습니다.")

    decisions, review_warning = _review_and_select(
        client=client,
        model=model,
        candidates=candidates,
        themes=themes,
        top_n=top_n,
    )

    candidate_map = {index: candidate for index, candidate in enumerate(candidates, start=1)}
    theme_df = ensure_theme_schema(themes)
    theme_map = {str(row.get("theme_id", "")): row for _, row in theme_df.iterrows()}

    recommendations: list[dict[str, Any]] = []
    used_new: set[int] = set()
    used_existing: set[str] = set()
    used_names: set[str] = set()

    for decision in decisions:
        if len(recommendations) >= top_n:
            break
        if decision.source == "NEW" and decision.candidate_index in candidate_map:
            idx = int(decision.candidate_index or 0)
            candidate = candidate_map[idx]
            name_key = normalize_theme_text(candidate["theme_name"])
            if idx in used_new or not name_key or name_key in used_names:
                continue
            recommendations.append(_candidate_to_recommendation(candidate, decision, model))
            used_new.add(idx)
            used_names.add(name_key)
        elif decision.source == "EXISTING" and decision.existing_theme_id in theme_map:
            theme_id = str(decision.existing_theme_id or "")
            row = theme_map[theme_id]
            name_key = normalize_theme_text(row.get("theme_name", ""))
            if theme_id in used_existing or not name_key or name_key in used_names:
                continue
            recommendations.append(_existing_to_recommendation(row, decision))
            used_existing.add(theme_id)
            used_names.add(name_key)

    # 검수 응답이 부족하거나 실패했을 때 신규 후보로만 안전하게 채웁니다.
    if len(recommendations) < top_n:
        for idx, candidate in candidate_map.items():
            if idx in used_new:
                continue
            name_key = normalize_theme_text(candidate["theme_name"])
            if not name_key or name_key in used_names:
                continue
            recommendations.append(_candidate_to_recommendation(candidate, None, model))
            used_new.add(idx)
            used_names.add(name_key)
            if len(recommendations) >= top_n:
                break

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(
        "|".join(rec["theme"]["theme_name"] for rec in recommendations).encode("utf-8")
    ).hexdigest()[:8]

    meta = {
        "run_id": run_id,
        "model": model,
        "candidate_count_requested": candidate_count,
        "candidate_count_received": len(candidates),
        "selected_count": len(recommendations),
        "new_count": sum(1 for rec in recommendations if rec["recommendation_source"] == "AI_GENERATED"),
        "existing_count": sum(1 for rec in recommendations if rec["recommendation_source"] == "EXISTING_DB"),
        "review_warning": review_warning or "",
        "issue_count": len(issues_payload),
    }
    return recommendations[:top_n], meta


def _next_theme_ids(themes: pd.DataFrame, count: int) -> list[str]:
    numbers: list[int] = []
    for value in themes.get("theme_id", pd.Series(dtype=str)).astype(str):
        match = re.search(r"(\d+)$", value)
        if match:
            numbers.append(int(match.group(1)))
    start = max(numbers, default=0) + 1
    return [f"T{number:03d}" for number in range(start, start + count)]


def persist_recommendations_locally(
    recommendations: list[dict[str, Any]],
    themes: pd.DataFrame,
    theme_path: Path,
    history_path: Path,
    run_meta: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict[str, Any]]]:
    df = ensure_theme_schema(themes)
    existing_names = {normalize_theme_text(value): index for index, value in enumerate(df["theme_name"].astype(str))}
    new_recs = [rec for rec in recommendations if rec["recommendation_source"] == "AI_GENERATED"]
    ids = _next_theme_ids(df, len(new_recs))
    added_rows: list[dict[str, Any]] = []
    id_cursor = 0

    for rec in recommendations:
        theme = rec["theme"]
        name_key = normalize_theme_text(theme.get("theme_name", ""))
        if rec["recommendation_source"] == "AI_GENERATED":
            if name_key in existing_names:
                existing_row = df.iloc[existing_names[name_key]]
                rec["recommendation_source"] = "EXISTING_DB"
                rec["source_label"] = "기존 DB 활용"
                rec["theme"] = existing_row.to_dict()
                continue
            theme_id = ids[id_cursor]
            id_cursor += 1
            row = {column: "" for column in df.columns}
            row.update(theme)
            row["theme_id"] = theme_id
            row["source_status"] = "AI_GENERATED"
            row["approved_status"] = "UNVERIFIED"
            row["created_date"] = date.today().isoformat()
            row["last_recommended_date"] = date.today().isoformat()
            row["generation_model"] = str(run_meta.get("model", ""))
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            existing_names[name_key] = len(df) - 1
            rec["theme"] = row
            added_rows.append(row)
        else:
            theme_id = str(theme.get("theme_id", ""))
            if theme_id:
                mask = df["theme_id"].astype(str) == theme_id
                df.loc[mask, "last_recommended_date"] = date.today().isoformat()

    theme_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(theme_path, sep="|", index=False, encoding="utf-8-sig")

    if history_path.exists():
        try:
            history = pd.read_csv(history_path, sep="|").fillna("")
        except Exception:
            history = pd.DataFrame(columns=HISTORY_COLUMNS)
    else:
        history = pd.DataFrame(columns=HISTORY_COLUMNS)
    for column in HISTORY_COLUMNS:
        if column not in history.columns:
            history[column] = ""

    history_rows = []
    for rec in recommendations:
        theme = rec["theme"]
        history_rows.append({
            "run_id": str(run_meta.get("run_id", "")),
            "recommended_date": date.today().isoformat(),
            "theme_id": str(theme.get("theme_id", "")),
            "theme_name": str(theme.get("theme_name", "")),
            "recommendation_source": rec["recommendation_source"],
            "source_issue": str(rec.get("source_issue_summary", "")),
            "creation_angle": str(rec.get("creation_angle", "")),
            "generation_model": str(run_meta.get("model", "")),
        })
    history = pd.concat([history, pd.DataFrame(history_rows)], ignore_index=True)
    history.to_csv(history_path, sep="|", index=False, encoding="utf-8-sig")
    return df, history, added_rows


def github_writeback_configured(config: dict[str, str]) -> bool:
    return bool(config.get("token") and config.get("repo") and config.get("theme_path"))


def _github_put_file(
    *,
    token: str,
    repo: str,
    branch: str,
    path: str,
    content: bytes,
    message: str,
) -> dict[str, Any]:
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    existing = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=20)
    sha = ""
    if existing.status_code == 200:
        sha = str(existing.json().get("sha", ""))
    elif existing.status_code != 404:
        raise ThemeGenerationError(f"GitHub 기존 파일 조회 실패({existing.status_code}): {existing.text[:300]}")

    payload: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    response = requests.put(api_url, headers=headers, json=payload, timeout=30)
    if response.status_code not in {200, 201}:
        raise ThemeGenerationError(f"GitHub 파일 저장 실패({response.status_code}): {response.text[:500]}")
    return response.json()


def persist_files_to_github(
    theme_path: Path,
    history_path: Path,
    config: dict[str, str],
    run_id: str,
) -> dict[str, Any]:
    if not github_writeback_configured(config):
        return {"status": "not_configured"}
    branch = config.get("branch") or "main"
    theme_result = _github_put_file(
        token=config["token"],
        repo=config["repo"],
        branch=branch,
        path=config["theme_path"],
        content=theme_path.read_bytes(),
        message=f"Add AI generated themes ({run_id})",
    )
    history_result: dict[str, Any] | None = None
    history_remote_path = config.get("history_path", "theme_recommendation_history.csv")
    if history_path.exists() and history_remote_path:
        history_result = _github_put_file(
            token=config["token"],
            repo=config["repo"],
            branch=branch,
            path=history_remote_path,
            content=history_path.read_bytes(),
            message=f"Update theme recommendation history ({run_id})",
        )
    return {
        "status": "success",
        "theme_commit": theme_result.get("commit", {}).get("html_url", ""),
        "history_commit": (history_result or {}).get("commit", {}).get("html_url", ""),
    }
