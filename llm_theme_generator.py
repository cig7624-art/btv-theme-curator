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
from zoneinfo import ZoneInfo

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
    "recommended_at",
    "rank",
    "theme_id",
    "theme_name",
    "theme_copy",
    "genre",
    "mood",
    "trigger_keywords",
    "recommendation_source",
    "source_issue",
    "rationale",
    "creation_angle",
    "content_search_terms",
    "generation_model",
]

VALID_ANGLES = {
    "소재형", "상황형", "감정형", "인물형", "관계형", "공간형", "해석형", "장르변주형"
}

GENERIC_THEME_WORDS = {
    "요즘 인기작", "인기 콘텐츠", "주말에 보기 좋은", "이번 주 추천", "화제의 작품",
    "지금 봐야 할", "놓치면 안 될", "재미있는 영화", "재미있는 드라마",
}

CONCRETE_THEME_TERMS = {
    "팬", "팬덤", "전문가", "평론가", "관객", "배우", "감독", "작가", "주인공", "악당",
    "영웅", "형사", "범인", "왕", "재벌", "가족", "부부", "친구", "동료", "아이", "부모",
    "원작", "웹툰", "웹소설", "애니메이션", "실사", "리메이크", "시리즈", "후속편", "시즌",
    "극장", "OTT", "드라마", "영화", "예능", "다큐", "공포", "로맨스", "사극", "SF",
    "반전", "결말", "해석", "복수", "생존", "추리", "법정", "수사", "괴물", "외계인",
    "재개봉", "역주행", "정주행", "공개", "캐스팅", "연기", "명장면", "빌런", "능력", "무기",
}

AMBIGUOUS_TITLE_PHRASES = {
    "한 장면으로 뒤집힌 기대", "스크린으로 찢고 나온 세계", "극장 밖에서 다시 뜬다",
    "악당의 기술, 주인공의 무기", "호불호를 넘어선 밤",
}

ACTION_MARKERS = {
    "하는", "되는", "된", "맞선", "훔친", "빼앗은", "되찾은", "돌아온", "숨긴", "쫓는",
    "구한", "먼저", "다시", "넘어선", "바꾼", "뒤집은", "지킨", "살린", "무너뜨린",
    "공개", "정주행", "역주행", "재회", "복수", "생존", "해석", "싸우는", "찾는",
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
    source_issue_summary: str = Field(description="실제 작품명·사건명을 포함한 최근 이슈 요약. 내부 이슈 ID는 쓰지 말고 70자 이내 한 문장")
    content_search_terms: list[str] = Field(description="추후 콘텐츠 검색에 사용할 검색어 4~6개")
    rationale: str = Field(description="최근 이슈가 이 테마로 확장되는 이유. 80자 이내 한 문장")


class GeneratedThemeBatch(BaseModel):
    themes: list[GeneratedTheme]


class ThemeSearchIntent(BaseModel):
    interpreted_request: str = Field(description="사용자의 큐레이션 의도를 한 문장으로 요약")
    anchor_titles: list[str] = Field(default_factory=list, description="기준이 되는 작품명 또는 인물명")
    genres: list[str] = Field(default_factory=list, description="원하는 장르")
    moods: list[str] = Field(default_factory=list, description="원하는 분위기와 감정")
    subjects: list[str] = Field(default_factory=list, description="핵심 소재·인물·관계·공간")
    narrative_elements: list[str] = Field(default_factory=list, description="서사 구조·상황·행동")
    positive_keywords: list[str] = Field(default_factory=list, description="테마 DB 검색에 사용할 확장 키워드")
    negative_keywords: list[str] = Field(default_factory=list, description="사용자가 제외한 조건")
    result_count: int = Field(default=20, ge=5, le=30, description="추천 결과 수")


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


def _read_recommendation_history(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    try:
        history = pd.read_csv(path, sep="|").fillna("")
    except Exception:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    for column in HISTORY_COLUMNS:
        if column not in history.columns:
            history[column] = ""
    return history[HISTORY_COLUMNS].copy()


def load_recommendation_history(path: Path, days: int = 56) -> pd.DataFrame:
    history = _read_recommendation_history(path)
    if history.empty:
        return history
    dates = pd.to_datetime(history["recommended_date"], errors="coerce")
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    return history[(dates >= cutoff) | dates.isna()].copy()


def _history_run_timestamp(row: pd.Series) -> pd.Timestamp:
    value = pd.to_datetime(str(row.get("recommended_at", "")), errors="coerce")
    if not pd.isna(value):
        if getattr(value, "tzinfo", None) is not None:
            value = value.tz_convert("Asia/Seoul").tz_localize(None)
        return value
    run_id = str(row.get("run_id", ""))
    match = re.match(r"^(\d{8}-\d{6})", run_id)
    if match:
        parsed = pd.to_datetime(match.group(1), format="%Y%m%d-%H%M%S", errors="coerce")
        if not pd.isna(parsed):
            return parsed
    return pd.to_datetime(str(row.get("recommended_date", "")), errors="coerce")


def load_recommendation_runs(path: Path, themes: pd.DataFrame) -> list[dict[str, Any]]:
    """추천 이력 CSV를 실행 단위로 복원합니다.

    각 버튼 실행은 run_id 하나로 묶이며, 새로고침 후에도 최신 실행을 기본으로
    다시 표시할 수 있도록 카드 렌더링에 필요한 스냅샷을 함께 복원합니다.
    """
    history = _read_recommendation_history(path)
    if history.empty:
        return []

    theme_df = ensure_theme_schema(themes)
    by_id = {
        str(row.get("theme_id", "")): row.to_dict()
        for _, row in theme_df.iterrows()
        if str(row.get("theme_id", ""))
    }
    by_name = {
        normalize_theme_text(row.get("theme_name", "")): row.to_dict()
        for _, row in theme_df.iterrows()
        if normalize_theme_text(row.get("theme_name", ""))
    }

    history = history.copy()
    history["_run_ts"] = history.apply(_history_run_timestamp, axis=1)
    history["_row_order"] = range(len(history))
    runs: list[dict[str, Any]] = []

    for run_id, group in history.groupby("run_id", sort=False):
        if not str(run_id).strip():
            continue
        group = group.copy()
        group["_rank_num"] = pd.to_numeric(group["rank"], errors="coerce")
        group = group.sort_values(["_rank_num", "_row_order"], na_position="last")
        recs: list[dict[str, Any]] = []

        for _, row in group.iterrows():
            theme_id = str(row.get("theme_id", ""))
            theme_name = str(row.get("theme_name", ""))
            base = by_id.get(theme_id) or by_name.get(normalize_theme_text(theme_name)) or {}
            theme = {column: str(base.get(column, "")) for column in theme_df.columns}
            snapshots = {
                "theme_id": theme_id,
                "theme_name": theme_name,
                "copy": str(row.get("theme_copy", "")),
                "genre": str(row.get("genre", "")),
                "mood": str(row.get("mood", "")),
                "trigger_keywords": str(row.get("trigger_keywords", "")),
                "source_issue": str(row.get("source_issue", "")),
                "creation_angle": str(row.get("creation_angle", "")),
                "content_search_terms": str(row.get("content_search_terms", "")),
                "generation_model": str(row.get("generation_model", "")),
            }
            for key, value in snapshots.items():
                if value:
                    theme[key] = value

            source = str(row.get("recommendation_source", "")) or "AI_GENERATED"
            recs.append({
                "recommendation_source": source,
                "source_label": "AI 신규 생성" if source == "AI_GENERATED" else "기존 DB 활용",
                "theme": theme,
                "source_issue_summary": str(row.get("source_issue", "")),
                "creation_angle": str(row.get("creation_angle", "")),
                "rationale": str(row.get("rationale", "")),
                "content_search_terms": _dedupe_strings(
                    str(row.get("content_search_terms", "")).replace("/", ",").split(","), 8
                ),
                "contents": [],
            })

        run_ts = group["_run_ts"].dropna().max() if group["_run_ts"].notna().any() else pd.NaT
        recommended_at = ""
        if not pd.isna(run_ts):
            recommended_at = run_ts.strftime("%Y-%m-%d %H:%M:%S")
        model_values = [str(v) for v in group["generation_model"].tolist() if str(v)]
        model = model_values[0] if model_values else ""
        new_count = sum(1 for rec in recs if rec["recommendation_source"] == "AI_GENERATED")
        existing_count = len(recs) - new_count
        runs.append({
            "run_id": str(run_id),
            "recommended_at": recommended_at,
            "sort_timestamp": run_ts,
            "recommendations": recs,
            "meta": {
                "run_id": str(run_id),
                "model": model,
                "selected_count": len(recs),
                "new_count": new_count,
                "existing_count": existing_count,
                "recommended_at": recommended_at,
                "restored": True,
            },
        })

    runs.sort(
        key=lambda item: item.get("sort_timestamp") if not pd.isna(item.get("sort_timestamp")) else pd.Timestamp.min,
        reverse=True,
    )
    return runs


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
            "source_issue_summary": _clean_short_text(theme.source_issue_summary, 100),
            "content_search_terms": search_terms,
            "rationale": _clean_short_text(theme.rationale, 100),
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
최근 콘텐츠 이슈를 해석해 여러 작품을 묶을 수 있는 신규 큐레이션 테마를 만든다.
기존 테마 DB는 제공되지 않으며 최근 이슈에서만 아이디어를 발산한다.

가장 중요한 기준은 테마명만 읽어도 어떤 콘텐츠가 들어갈지 바로 예측되는 것이다.
테마명에는 인물, 관계, 소재, 장르, 사건, 서사 행동 중 하나 이상을 구체적으로 드러낸다.
주어·대상·콘텐츠 범위가 없는 시적 문구나 은유형 문구는 만들지 않는다.
최소 8편 이상의 영화·드라마·예능을 묶을 수 있어야 하며 특정 작품 하나의 홍보 문구가 되어서는 안 된다.

나쁜 예: '한 장면으로 뒤집힌 기대', '스크린으로 찢고 나온 세계', '극장 밖에서 다시 뜬다', '악당의 기술, 주인공의 무기', '호불호를 넘어선 밤'
좋은 방향: '한 장면으로 평가가 뒤집힌 영화', '애니메이션 세계를 실사로 옮긴 영화', 'OTT 공개 후 역주행한 영화', '악당의 능력을 빼앗아 싸우는 주인공', '혹평 뒤 입소문으로 살아난 공포영화'

소재형·상황형·감정형·인물형·관계형·공간형·해석형·장르변주형을 고르게 활용한다.
테마명은 한국어 8~28자 안팎으로 간결하게 작성한다.
""".strip()

    user_payload = {
        "requested_candidate_count": candidate_count,
        "recent_issues": issues_payload,
        "avoid_recently_recommended_theme_names": recent_names,
        "requirements": [
            "테마명만 보고 포함될 콘텐츠의 공통점을 바로 이해할 수 있을 것",
            "테마명에 구체적인 인물·관계·소재·장르·사건·행동 중 하나 이상을 포함할 것",
            "주어와 대상이 없는 시적 문구, 추상적 은유, 명사 두 개만 병렬한 문구는 금지",
            "각 테마는 최근 이슈와 연결되지만 특정 작품 하나에 종속되지 않을 것",
            "테마끼리 관점과 어휘가 충분히 다를 것",
            "테마명, 짧은 카피, 장르, 무드, 키워드, 생성 관점, 연계 이슈 요약, 콘텐츠 검색어를 모두 작성할 것",
            "연계 이슈 요약에는 I01 같은 내부 ID 대신 실제 작품명·사건명을 쓰고 70자 이내로 작성할 것",
            "선정 이유는 최근 이슈와 테마의 연결만 80자 이내 한 문장으로 작성할 것",
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


def interpret_theme_search_query(
    query: str,
    api_key: str,
    model: str = "gpt-5.6-luna",
    issue_records: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Interpret a natural-language theme search request with one LLM call.

    The theme database itself is deliberately not sent to the model. The model only
    converts the user's sentence into a compact search intent; ranking is performed
    locally by app.py.
    """
    cleaned_query = re.sub(r"\s+", " ", str(query or "")).strip()
    if not cleaned_query:
        raise ThemeGenerationError("검색 문장을 입력해 주세요.")
    if not api_key:
        raise ThemeGenerationError("OPENAI_API_KEY가 설정되지 않았습니다.")

    compact_issues: list[dict[str, Any]] = []
    for issue in (issue_records or [])[:20]:
        compact_issues.append({
            "issue_title": _clean_short_text(issue.get("issue_title", ""), 90),
            "related_content": _clean_short_text(issue.get("related_content", ""), 60),
            "keywords": _clean_short_text(issue.get("keywords", ""), 120),
            "description": _clean_short_text(issue.get("description", ""), 160),
        })

    system_prompt = """
당신은 한국 IPTV 콘텐츠 큐레이션 검색 도우미다.
사용자의 자유로운 문장을 기존 테마 DB를 검색하기 위한 구조화된 의도로 변환한다.
테마 DB 자체는 보지 않으며 새로운 테마를 생성하지 않는다.
작품명이 최근 이슈 맥락에 있으면 그 작품의 소재·장르·분위기를 검색어로 확장한다.
맥락에 없는 작품의 세부 내용을 확신할 수 없다면 작품명은 기준 작품으로만 유지하고 사실을 만들어내지 않는다.
사용자가 '잔인하지 않은', '로맨스 제외'처럼 제외 조건을 말하면 negative_keywords에 넣는다.
positive_keywords는 동의어와 유사 소재를 포함하되 너무 일반적인 단어는 피하고 6~14개로 작성한다.
interpreted_request는 사용자가 원하는 큐레이션 범위를 한국어 한 문장으로 명확히 요약한다.
""".strip()

    payload = {
        "user_query": cleaned_query,
        "recent_issue_context": compact_issues,
        "default_result_count": 20,
    }

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.parse(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            text_format=ThemeSearchIntent,
        )
    except Exception as exc:
        raise ThemeGenerationError(f"AI 테마 검색 해석 실패: {exc}") from exc

    parsed = response.output_parsed
    if parsed is None:
        raise ThemeGenerationError("AI가 검색 의도를 해석하지 못했습니다.")

    intent = parsed.model_dump()
    for key in [
        "anchor_titles", "genres", "moods", "subjects", "narrative_elements",
        "positive_keywords", "negative_keywords",
    ]:
        intent[key] = _dedupe_strings(intent.get(key, []), 14)
    intent["interpreted_request"] = _clean_short_text(intent.get("interpreted_request", cleaned_query), 140)
    intent["result_count"] = max(5, min(int(intent.get("result_count", 20) or 20), 30))

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
    total_tokens = int(getattr(usage, "total_tokens", input_tokens + output_tokens) or (input_tokens + output_tokens)) if usage is not None else 0
    return intent, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "api_call_count": 1,
    }


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


def _title_clarity_score(candidate: dict[str, Any]) -> int:
    name = str(candidate.get("theme_name", "")).strip()
    normalized = normalize_theme_text(name)
    score = 34

    if 8 <= len(name) <= 28:
        score += 14
    elif 5 <= len(name) <= 34:
        score += 7
    else:
        score -= 10

    concrete_hits = sum(1 for term in CONCRETE_THEME_TERMS if normalize_theme_text(term) in normalized)
    score += min(26, concrete_hits * 8)

    if any(marker in name for marker in ACTION_MARKERS):
        score += 10
    elif any(separator in name for separator in [",", "·", "/", ":"]):
        score -= 18

    if any(normalize_theme_text(phrase) in normalized for phrase in AMBIGUOUS_TITLE_PHRASES):
        score -= 34
    if concrete_hits == 0:
        score -= 24
    if len(_tokenize(name)) <= 2 and concrete_hits <= 1:
        score -= 12

    keyword_tokens = set()
    for keyword in candidate.get("keywords", []) or []:
        keyword_tokens |= _tokenize(keyword)
    title_overlap = len(_tokenize(name) & keyword_tokens)
    score += min(8, title_overlap * 2)

    return max(0, min(100, score))


def _quality_score(candidate: dict[str, Any]) -> int:
    name = str(candidate.get("theme_name", "")).strip()
    copy = str(candidate.get("copy", "")).strip()
    genre = str(candidate.get("genre", "")).strip()
    mood = str(candidate.get("mood", "")).strip()
    keywords = candidate.get("keywords", []) or []
    search_terms = candidate.get("content_search_terms", []) or []
    rationale = str(candidate.get("rationale", "")).strip()

    clarity = _title_clarity_score(candidate)
    score = round(clarity * 0.48)
    score += min(12, max(0, len(copy) - 6) // 4)
    score += min(10, len(keywords) * 2)
    score += min(8, len(search_terms) * 2)
    score += 6 if str(candidate.get("creation_angle", "")) in VALID_ANGLES else 0
    score += 5 if genre else 0
    score += 4 if mood else 0
    score += min(7, max(0, len(rationale) - 12) // 8)

    normalized_name = normalize_theme_text(name)
    if any(normalize_theme_text(word) in normalized_name for word in GENERIC_THEME_WORDS):
        score -= 18
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
        clarity = _title_clarity_score(candidate)
        if clarity < 52:
            continue
        relevance = _relevance_score(candidate, issues_payload)
        quality = _quality_score(candidate)
        novelty, best_row, existing_similarity = _novelty_score(candidate, theme_df, recent_history)
        total = relevance * 0.38 + quality * 0.27 + novelty * 0.20 + clarity * 0.15
        evaluated.append({
            "index": index,
            "candidate": candidate,
            "clarity": clarity,
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
                reason=f"기존 검증 테마와 {round(similarity * 100)}% 유사해 해당 테마를 활용했습니다.",
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
                reason="최근 이슈와의 연결, 테마명의 명확성, 여러 작품으로의 확장성을 기준으로 선정했습니다.",
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
    candidate_count = min(max(top_n + 15, 25), 45)
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

    run_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(
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

    # 화면과 영구 저장에는 가장 최근 추천 1회만 유지합니다.
    # 새로 생성할 때마다 이전 추천 결과를 교체하므로 새로고침 후에도 최신 결과만 복원됩니다.
    history_rows = []
    recommended_at = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
    for rank, rec in enumerate(recommendations, start=1):
        theme = rec["theme"]
        history_rows.append({
            "run_id": str(run_meta.get("run_id", "")),
            "recommended_date": date.today().isoformat(),
            "recommended_at": recommended_at,
            "rank": rank,
            "theme_id": str(theme.get("theme_id", "")),
            "theme_name": str(theme.get("theme_name", "")),
            "theme_copy": str(theme.get("copy", "")),
            "genre": str(theme.get("genre", "")),
            "mood": str(theme.get("mood", "")),
            "trigger_keywords": str(theme.get("trigger_keywords", "")),
            "recommendation_source": rec["recommendation_source"],
            "source_issue": str(rec.get("source_issue_summary", "")),
            "rationale": str(rec.get("rationale", "")),
            "creation_angle": str(rec.get("creation_angle", "")),
            "content_search_terms": ",".join(rec.get("content_search_terms", []) or []),
            "generation_model": str(run_meta.get("model", "")),
        })
    history = pd.DataFrame(history_rows, columns=HISTORY_COLUMNS)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(history_path, sep="|", index=False, encoding="utf-8-sig")
    return df, history, added_rows


def _github_get_file(
    *,
    token: str,
    repo: str,
    branch: str,
    path: str,
) -> bytes | None:
    api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=20)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ThemeGenerationError(f"GitHub 파일 조회 실패({response.status_code}): {response.text[:300]}")
    encoded = str(response.json().get("content", "")).replace("\n", "")
    if not encoded:
        return None
    return base64.b64decode(encoded)


def sync_files_from_github(
    theme_path: Path,
    history_path: Path,
    config: dict[str, str],
) -> dict[str, Any]:
    if not github_writeback_configured(config):
        return {"status": "not_configured"}

    branch = config.get("branch") or "main"
    theme_content = _github_get_file(
        token=config["token"],
        repo=config["repo"],
        branch=branch,
        path=config["theme_path"],
    )
    history_remote_path = config.get("history_path", "theme_recommendation_history.csv")
    history_content = None
    if history_remote_path:
        history_content = _github_get_file(
            token=config["token"],
            repo=config["repo"],
            branch=branch,
            path=history_remote_path,
        )

    synced = []
    if theme_content is not None:
        theme_path.parent.mkdir(parents=True, exist_ok=True)
        theme_path.write_bytes(theme_content)
        synced.append(str(theme_path))
    if history_content is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_bytes(history_content)
        synced.append(str(history_path))

    return {"status": "success", "synced": synced}


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
