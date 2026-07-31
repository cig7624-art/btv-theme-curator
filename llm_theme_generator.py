from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import date, datetime
from difflib import SequenceMatcher
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

VALID_ANGLES = {
    "소재형", "상황형", "감정형", "인물형", "관계형", "공간형", "해석형", "장르변주형"
}

GENERIC_THEME_WORDS = {
    "요즘 인기작", "인기 콘텐츠", "주말에 보기 좋은", "이번 주 추천", "화제의 작품",
    "지금 봐야 할", "놓치면 안 될", "재미있는 영화", "재미있는 드라마",
}


class ThemeGenerationError(RuntimeError):
    """Raised when an LLM theme generation run cannot be completed."""


class GeneratedTheme(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    theme_name: str = Field(description="B tv+ 화면에 노출할 한국어 큐레이션 테마명")
    display_copy: str = Field(alias="copy", description="테마를 보조하는 짧은 한국어 카피")
    genre: str = Field(description="대표 장르. 복수 장르는 /로 구분")
    mood: str = Field(description="대표 감정 또는 분위기")
    keywords: list[str] = Field(description="테마를 설명하는 핵심 키워드 5~8개")
    creation_angle: str = Field(description="소재형/상황형/감정형/인물형/관계형/공간형/해석형/장르변주형 중 하나")
    source_issue_keys: list[str] = Field(description="생성 근거가 된 이슈 ID 목록")
    source_issue_summary: str = Field(description="이 테마가 어떤 최근 이슈에서 나왔는지 한 문장 설명")
    content_search_terms: list[str] = Field(description="추후 콘텐츠 검색에 사용할 검색어 4~6개")
    rationale: str = Field(description="여러 작품을 묶는 테마로서 유효한 이유")


class GeneratedThemeBatch(BaseModel):
    themes: list[GeneratedTheme]


class SelectedThemeDecision(BaseModel):
    source: Literal["NEW", "EXISTING"]
    candidate_index: int | None = None
    existing_theme_id: str | None = None
    relevance_score: int = Field(ge=0, le=100)
    novelty_score: int = Field(ge=0, le=100)
    quality_score: int = Field(ge=0, le=100)
    reason: str


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


def _tokenize(value: Any) -> set[str]:
    return {
        token for token in re.findall(r"[0-9a-z가-힣]+", str(value or "").lower())
        if len(token) >= 2
    }


def _similarity(left: Any, right: Any) -> float:
    left_text = normalize_theme_text(left)
    right_text = normalize_theme_text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    seq = SequenceMatcher(None, left_text, right_text).ratio()
    left_tokens = _tokenize(left)
    right_tokens = _tokenize(right)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    containment = 0.0
    if min(len(left_text), len(right_text)) >= 6 and (left_text in right_text or right_text in left_text):
        containment = 0.88
    return min(1.0, max(containment, seq * 0.62 + jaccard * 0.38))


def ensure_theme_schema(themes: pd.DataFrame) -> pd.DataFrame:
    df = themes.copy()
    required = ["theme_id", "theme_name", "trigger_keywords", "genre", "mood", "copy"]
    for column in required:
        if column not in df.columns:
            df[column] = ""
    for column, default in THEME_OPTIONAL_DEFAULTS.items():
        if column not in df.columns:
            df[column] = default
        elif column in {"source_status", "approved_status"}:
            df[column] = df[column].replace("", default)
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
            "keywords": _dedupe_strings(str(issue.get("keywords", "")).replace("/", ",").split(","), 14),
            "description": _clean_short_text(issue.get("description", ""), 360),
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
        keywords = _dedupe_strings(theme.keywords, 8)
        search_terms = _dedupe_strings(theme.content_search_terms, 6)
        cleaned.append({
            "theme_name": name,
            "copy": _clean_short_text(theme.display_copy, 58),
            "genre": _clean_short_text(theme.genre, 40),
            "mood": _clean_short_text(theme.mood, 30),
            "trigger_keywords": ",".join(keywords),
            "keywords": keywords,
            "creation_angle": _clean_short_text(theme.creation_angle, 20),
            "source_issue_keys": _dedupe_strings(theme.source_issue_keys, 4),
            "source_issue_summary": _clean_short_text(theme.source_issue_summary, 160),
            "content_search_terms": search_terms,
            "rationale": _clean_short_text(theme.rationale, 180),
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
    recent_names: list[str] = []
    if not recent_history.empty:
        recent_names = _dedupe_strings(recent_history["theme_name"].astype(str).tolist(), 40)

    system_prompt = """
당신은 한국 IPTV의 콘텐츠 편성·큐레이션을 담당하는 시니어 에디터다.
최근 콘텐츠 이슈를 해석해, 여러 작품을 묶을 수 있는 신선한 큐레이션 테마를 새로 만든다.
기존 테마 DB는 제공되지 않는다. 최근 이슈 자체에서만 아이디어를 발산한다.
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
            "콘텐츠 검색어는 추후 작품 후보를 찾기 쉬운 구체적인 표현일 것",
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


def _candidate_text(candidate: dict[str, Any]) -> str:
    return " ".join([
        str(candidate.get("theme_name", "")),
        str(candidate.get("copy", "")),
        str(candidate.get("genre", "")),
        str(candidate.get("mood", "")),
        str(candidate.get("trigger_keywords", "")),
    ])


def _row_text(row: pd.Series) -> str:
    return " ".join([
        str(row.get("theme_name", "")),
        str(row.get("copy", "")),
        str(row.get("genre", "")),
        str(row.get("mood", "")),
        str(row.get("trigger_keywords", "")),
    ])


def _quality_score(candidate: dict[str, Any]) -> int:
    name = str(candidate.get("theme_name", "")).strip()
    copy = str(candidate.get("copy", "")).strip()
    keywords = candidate.get("keywords", []) or []
    search_terms = candidate.get("content_search_terms", []) or []
    rationale = str(candidate.get("rationale", "")).strip()
    score = 48
    if 7 <= len(name) <= 28:
        score += 15
    elif 5 <= len(name) <= 34:
        score += 8
    else:
        score -= 8
    if 10 <= len(copy) <= 58:
        score += 8
    if len(keywords) >= 5:
        score += 9
    if len(search_terms) >= 4:
        score += 8
    if str(candidate.get("creation_angle", "")) in VALID_ANGLES:
        score += 6
    if len(rationale) >= 20:
        score += 6
    normalized_name = normalize_theme_text(name)
    if any(normalize_theme_text(word) in normalized_name for word in GENERIC_THEME_WORDS):
        score -= 20
    if len(_tokenize(name)) <= 1:
        score -= 8
    return max(0, min(100, score))


def _relevance_score(candidate: dict[str, Any], issues_payload: list[dict[str, Any]]) -> int:
    issue_map = {str(item.get("issue_key", "")): item for item in issues_payload}
    keys = [key for key in candidate.get("source_issue_keys", []) if key in issue_map]
    selected_issues = [issue_map[key] for key in keys]
    if not selected_issues:
        selected_issues = issues_payload[:2]
    issue_text = " ".join(
        " ".join([
            str(issue.get("title", "")),
            str(issue.get("related_content", "")),
            " ".join(issue.get("keywords", []) or []),
            str(issue.get("description", "")),
        ])
        for issue in selected_issues
    )
    candidate_tokens = _tokenize(_candidate_text(candidate))
    issue_tokens = _tokenize(issue_text)
    overlap = len(candidate_tokens & issue_tokens)
    coverage = overlap / max(1, min(len(candidate_tokens), 12))
    score = 58 + min(26, round(coverage * 40))
    if keys:
        score += min(12, len(keys) * 5)
    return max(0, min(100, score))


def _existing_match(candidate: dict[str, Any], themes: pd.DataFrame) -> tuple[pd.Series | None, float]:
    if themes.empty:
        return None, 0.0
    candidate_name = str(candidate.get("theme_name", ""))
    candidate_text = _candidate_text(candidate)
    best_row: pd.Series | None = None
    best_score = 0.0
    for _, row in themes.iterrows():
        name_score = _similarity(candidate_name, row.get("theme_name", ""))
        text_score = _similarity(candidate_text, _row_text(row))
        score = max(name_score, name_score * 0.78 + text_score * 0.22)
        if score > best_score:
            best_score = score
            best_row = row
    return best_row, best_score


def _novelty_score(
    candidate: dict[str, Any],
    themes: pd.DataFrame,
    recent_history: pd.DataFrame,
) -> tuple[int, pd.Series | None, float]:
    best_row, existing_similarity = _existing_match(candidate, themes)
    recent_similarity = 0.0
    if not recent_history.empty:
        for value in recent_history.get("theme_name", pd.Series(dtype=str)).astype(str):
            recent_similarity = max(recent_similarity, _similarity(candidate.get("theme_name", ""), value))
    strongest = max(existing_similarity, recent_similarity)
    novelty = round(100 - strongest * 88)
    if strongest < 0.45:
        novelty = max(novelty, 82)
    return max(0, min(100, novelty)), best_row, existing_similarity


def _candidate_to_recommendation(
    candidate: dict[str, Any],
    decision: SelectedThemeDecision,
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
        "relevance_score": decision.relevance_score,
        "novelty_score": decision.novelty_score,
        "quality_score": decision.quality_score,
        "selection_reason": decision.reason,
        "contents": [],
    }


def _existing_to_recommendation(
    row: pd.Series,
    decision: SelectedThemeDecision,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    theme = {key: str(row.get(key, "")) for key in ensure_theme_schema(pd.DataFrame([row])).columns}
    return {
        "recommendation_source": "EXISTING_DB",
        "source_label": "기존 DB 활용",
        "theme": theme,
        "source_issue_keys": candidate.get("source_issue_keys", []),
        "source_issue_summary": candidate.get("source_issue_summary", ""),
        "creation_angle": candidate.get("creation_angle", "기존 테마") or "기존 테마",
        "rationale": candidate.get("rationale", ""),
        "content_search_terms": candidate.get("content_search_terms", []),
        "relevance_score": decision.relevance_score,
        "novelty_score": decision.novelty_score,
        "quality_score": decision.quality_score,
        "selection_reason": decision.reason,
        "contents": [],
    }


def _local_review_and_select(
    candidates: list[dict[str, Any]],
    themes: pd.DataFrame,
    recent_history: pd.DataFrame,
    issues_payload: list[dict[str, Any]],
    top_n: int,
    model: str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    theme_df = ensure_theme_schema(themes)
    evaluated: list[dict[str, Any]] = []

    for index, candidate in enumerate(candidates, start=1):
        relevance = _relevance_score(candidate, issues_payload)
        quality = _quality_score(candidate)
        novelty, best_row, existing_similarity = _novelty_score(candidate, theme_df, recent_history)
        total = relevance * 0.43 + quality * 0.32 + novelty * 0.25
        evaluated.append({
            "index": index,
            "candidate": candidate,
            "relevance": relevance,
            "quality": quality,
            "novelty": novelty,
            "total": total,
            "best_row": best_row,
            "existing_similarity": existing_similarity,
        })

    selected: list[dict[str, Any]] = []
    angle_counts: dict[str, int] = {}
    issue_counts: dict[str, int] = {}
    remaining = evaluated.copy()

    while remaining and len(selected) < top_n:
        best_item: dict[str, Any] | None = None
        best_adjusted = -10_000.0
        for item in remaining:
            candidate = item["candidate"]
            angle = str(candidate.get("creation_angle", ""))
            issue_key = str((candidate.get("source_issue_keys", []) or [""])[0])
            similarity_to_selected = max(
                (_similarity(candidate.get("theme_name", ""), other["candidate"].get("theme_name", "")) for other in selected),
                default=0.0,
            )
            diversity_penalty = angle_counts.get(angle, 0) * 5 + issue_counts.get(issue_key, 0) * 4
            similarity_penalty = similarity_to_selected * 18
            if similarity_to_selected >= 0.88:
                similarity_penalty += 24
            adjusted = item["total"] - diversity_penalty - similarity_penalty
            if adjusted > best_adjusted:
                best_adjusted = adjusted
                best_item = item
        if best_item is None:
            break
        selected.append(best_item)
        remaining.remove(best_item)
        candidate = best_item["candidate"]
        angle = str(candidate.get("creation_angle", ""))
        issue_key = str((candidate.get("source_issue_keys", []) or [""])[0])
        angle_counts[angle] = angle_counts.get(angle, 0) + 1
        issue_counts[issue_key] = issue_counts.get(issue_key, 0) + 1

    recommendations: list[dict[str, Any]] = []
    existing_count = 0
    for item in selected:
        candidate = item["candidate"]
        best_row = item["best_row"]
        similarity = float(item["existing_similarity"])
        source_status = str(best_row.get("source_status", "")) if best_row is not None else ""
        approved_status = str(best_row.get("approved_status", "")) if best_row is not None else ""
        exact_name = bool(
            best_row is not None
            and normalize_theme_text(candidate.get("theme_name", "")) == normalize_theme_text(best_row.get("theme_name", ""))
        )
        trusted_existing = source_status in {"HUMAN_APPROVED", "USED"} or approved_status in {"HUMAN_APPROVED", "APPROVED", "USED"}
        use_existing = best_row is not None and (exact_name or (trusted_existing and similarity >= 0.93))

        if use_existing:
            decision = SelectedThemeDecision(
                source="EXISTING",
                existing_theme_id=str(best_row.get("theme_id", "")),
                relevance_score=item["relevance"],
                novelty_score=item["novelty"],
                quality_score=item["quality"],
                reason=f"LLM이 독립적으로 만든 후보가 기존 테마와 {round(similarity * 100)}% 일치해 기존 DB 항목을 재사용했습니다. 추가 LLM 검수 호출은 하지 않았습니다.",
            )
            recommendations.append(_existing_to_recommendation(best_row, decision, candidate))
            existing_count += 1
        else:
            decision = SelectedThemeDecision(
                source="NEW",
                candidate_index=int(item["index"]),
                relevance_score=item["relevance"],
                novelty_score=item["novelty"],
                quality_score=item["quality"],
                reason="최근 이슈 연관성·표현 완성도·기존/최근 테마와의 거리·테마 간 다양성을 코드로 계산해 선정했습니다.",
            )
            recommendations.append(_candidate_to_recommendation(candidate, decision, model))

    return recommendations, {
        "existing_count": existing_count,
        "new_count": len(recommendations) - existing_count,
    }


def generate_weekly_themes(
    issue_records: list[dict[str, Any]],
    themes: pd.DataFrame,
    top_n: int,
    api_key: str,
    model: str = "gpt-5.6-luna",
    recent_history: pd.DataFrame | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not api_key:
        raise ThemeGenerationError("OPENAI_API_KEY가 설정되지 않았습니다.")
    if not issue_records:
        raise ThemeGenerationError("신규 테마를 만들 최근 핵심 이슈가 없습니다.")

    top_n = max(1, min(int(top_n), 30))
    candidate_count = min(max(top_n + 12, 24), 45)
    history = recent_history if recent_history is not None else pd.DataFrame(columns=HISTORY_COLUMNS)
    issues_payload = _issue_payload(issue_records)
    client = OpenAI(api_key=api_key)

    # 비용이 드는 LLM API 호출은 여기 한 번뿐입니다.
    candidates = _generate_candidates(
        client=client,
        model=model,
        issues_payload=issues_payload,
        recent_history=history,
        candidate_count=candidate_count,
    )
    if len(candidates) < top_n:
        raise ThemeGenerationError(f"신규 테마 후보가 {len(candidates)}개만 생성되어 최종 {top_n}개를 만들 수 없습니다.")

    recommendations, local_meta = _local_review_and_select(
        candidates=candidates,
        themes=themes,
        recent_history=history,
        issues_payload=issues_payload,
        top_n=top_n,
        model=model,
    )
    if len(recommendations) < top_n:
        raise ThemeGenerationError(
            f"중복·다양성 필터 후 추천 가능 테마가 {len(recommendations)}개만 남았습니다. 다시 생성해 주세요."
        )

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(
        "|".join(rec["theme"]["theme_name"] for rec in recommendations).encode("utf-8")
    ).hexdigest()[:8]

    meta = {
        "run_id": run_id,
        "model": model,
        "api_call_count": 1,
        "candidate_count_requested": candidate_count,
        "candidate_count_received": len(candidates),
        "selected_count": len(recommendations),
        "new_count": local_meta["new_count"],
        "existing_count": local_meta["existing_count"],
        "review_warning": "기존 테마 DB 비교와 최종 선별은 추가 API 호출 없이 로컬 유사도·품질·다양성 계산으로 처리했습니다.",
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
