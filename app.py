import html
import json
import os
import re
import shutil
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pandas as pd
import streamlit as st

from llm_theme_generator import (
    ThemeGenerationError,
    ensure_theme_schema,
    generate_content_candidates_for_themes,
    generate_weekly_themes,
    github_writeback_configured,
    load_recommendation_history,
    load_recommendation_runs,
    persist_content_candidates_to_github,
    persist_files_to_github,
    persist_recommendations_locally,
    sync_files_from_github,
)

from kinolights_candidates import content_rows_to_frame, validate_content_suggestions
from poster_assets import enrich_poster_candidates
from theme_editorial import theme_card_html
from issue_carousel import CAROUSEL_SCRIPT, issue_carousel_html
from issue_quality import assess_issues, build_ranked_issues
# Independent video/news signals and related articles share the same evidence.
from issue_presentation import present_issue
from issue_related_news import attach_related_news
from issue_collection_context import apply_collection_context
from issue_rules import ISSUE_LOOKBACK_DAYS
from ott_collection import collect_public_homes, load_snapshot, save_snapshot
from ott_view import render_ott_page
from ai_director_view import activate_director_tab, render_ai_director_page
from theme_provenance import director_theme_mask, is_director_theme, weekly_recommendation_runs
from api_usage_view import render_api_cost_control
from media_insights_runtime import render_media_insights_tab
from issue_selection_copy import SOURCE_CONFIG, SOURCE_DETAIL_LOGIC, SELECTION_STEPS

from theme_content_manager import (
    ai_suggestions_to_recommendations,
    content_coverage,
    content_map_for_scope,
    load_content_frame,
    missing_theme_rows,
    stale_theme_rows,
    theme_rows_to_recommendations,
    upsert_content_frame,
)

APP_VERSION = Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()


st.set_page_config(
    page_title="B tv+ AI Theme Curator",
    page_icon="🧠",
    layout="wide"
)

st.markdown(
    "<style>" + Path(__file__).with_name("styles.css").read_text(encoding="utf-8") + "</style>",
    unsafe_allow_html=True,
)


ISSUE_PATH = Path("issue_feed.csv")
THEME_DB_PATH = Path("theme_db.csv")
OLD_THEME_PATH = Path("theme_pool.csv")
CONTENT_DB_PATH = Path("content_db.csv")
THEME_HISTORY_PATH = Path("theme_recommendation_history.csv")
THEME_CONTENT_PATH = Path("theme_content_candidates.csv")
OTT_SNAPSHOT_PATH = Path(__file__).with_name("ott_home_snapshot.json")

SOURCE_DETAIL_KEY_BY_NAME = {
    "YouTube 반응": "youtube",
    "극장·박스오피스": "boxoffice",
    "OTT 랭킹·신작": "ott",
    "온라인 화제·뉴스": "online_news",
}

STOPWORDS = [
    "좋은", "보기", "보면", "볼", "때", "추천", "싶은", "생각나는",
    "같은", "하는", "있는", "없는", "영화", "드라마", "콘텐츠", "작품",
    "뭐", "무엇", "좀", "보고싶", "볼만한"
]

INTENT_MAP = {
    "공포": {
        "triggers": ["공포", "호러", "무서", "귀신", "오컬트", "괴담", "섬뜩", "소름"],
        "keywords": ["공포", "호러", "오컬트", "괴담", "귀신", "스릴러", "긴장감", "무서운"],
        "must": True,
        "negative": ["자녀", "키즈", "아이", "가족", "힐링", "따뜻한"]
    },
    "스릴러": {
        "triggers": ["스릴", "반전", "추리", "미스터리", "범죄", "수사"],
        "keywords": ["스릴러", "반전", "추리", "미스터리", "범죄", "수사", "긴장감"],
        "must": True,
        "negative": ["키즈", "아이"]
    },
    "로맨스": {
        "triggers": ["첫사랑", "사랑", "연애", "설렘", "로맨스", "로코"],
        "keywords": ["첫사랑", "사랑", "연애", "설렘", "로맨스", "로코", "멜로", "청춘"],
        "must": True,
        "negative": ["공포", "호러", "오컬트"]
    },
    "여행": {
        "triggers": ["여행", "떠나", "휴가", "바다", "해외", "풍경"],
        "keywords": ["여행", "떠나고싶은", "로드무비", "휴가", "바다", "해외", "풍경", "힐링"],
        "must": False,
        "negative": []
    },
    "비": {
        "triggers": ["비", "비올", "비오는", "장마", "빗소리"],
        "keywords": ["비", "비오는날", "비올때", "장마", "우중", "빗소리", "감성"],
        "must": False,
        "negative": []
    },
    "여름": {
        "triggers": ["여름", "무더위", "더위", "한여름", "바캉스"],
        "keywords": ["여름", "무더위", "더위", "휴가", "바캉스", "한여름", "해변", "바다"],
        "must": False,
        "negative": []
    },
    "힐링": {
        "triggers": ["힐링", "위로", "잔잔", "따뜻", "쉬고", "휴식"],
        "keywords": ["힐링", "위로", "잔잔한", "따뜻한", "감성", "휴식", "가족"],
        "must": True,
        "negative": ["공포", "호러", "오컬트"]
    },
    "코미디": {
        "triggers": ["웃", "코미디", "유쾌", "개그", "머리비우"],
        "keywords": ["코미디", "웃긴", "유쾌한", "예능", "개그"],
        "must": True,
        "negative": ["공포", "호러"]
    },
    "가족": {
        "triggers": ["가족", "자녀", "아이", "부모", "키즈"],
        "keywords": ["가족", "자녀", "아이", "부모", "키즈", "따뜻한", "감동"],
        "must": True,
        "negative": ["공포", "호러", "오컬트", "잔인"]
    },
}


def go_page(page):
    st.session_state["page"] = page
    if page == "theme_db":
        st.session_state["home_tab_default"] = "추천 테마"
    elif page == "issue_db":
        st.session_state["home_tab_default"] = "지금 주목할 이야기"


if "page" not in st.session_state:
    st.session_state["page"] = "home"


@st.cache_data(ttl=900, max_entries=1, show_spinner=False)
def refresh_ott_public_homes():
    """Share bounded public refreshes; ordinary reruns only read stored data."""
    from ott_collection import PublicBrowser

    previous = load_snapshot(OTT_SNAPSHOT_PATH)
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if chromium:
        with PublicBrowser(executable_path=chromium) as browser:
            snapshot = collect_public_homes(previous=previous, fetcher=browser)
    else:
        snapshot = collect_public_homes(previous=previous)
    save_snapshot(snapshot, OTT_SNAPSHOT_PATH)
    return snapshot


def clear_theme_ai_search_state():
    for key in [
        "theme_ai_search_intent", "theme_ai_search_usage", "theme_ai_search_query",
        "theme_ai_search_input", "theme_quick_search_input",
        "ai_suggestion_content_map", "ai_suggestion_content_meta",
    ]:
        st.session_state.pop(key, None)


def _detail_box(title, text):
    st.markdown(
        '<div class="logic-detail-box">'
        f'<div class="logic-detail-title">{html.escape(title)}</div>'
        f'<div class="logic-detail-text">{html.escape(str(text))}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _detail_list(title, items):
    if not items:
        return
    rendered = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
    st.markdown(
        '<div class="logic-detail-box">'
        f'<div class="logic-detail-title">{html.escape(title)}</div>'
        f'<div class="logic-detail-text"><ol style="margin:6px 0 0 20px;padding:0;">{rendered}</ol></div>'
        '</div>',
        unsafe_allow_html=True,
    )


def load_data():
    sync_persistent_storage_once()
    issues = pd.read_csv(ISSUE_PATH, sep="|").fillna("")

    if THEME_DB_PATH.exists():
        themes = pd.read_csv(THEME_DB_PATH, sep="|").fillna("")
    elif OLD_THEME_PATH.exists():
        themes = pd.read_csv(OLD_THEME_PATH, sep="|").fillna("")
    else:
        raise FileNotFoundError("theme_db.csv 또는 theme_pool.csv 파일이 없습니다.")
    themes = ensure_theme_schema(themes)

    # AI 디렉터는 이 실제 콘텐츠 목록을 사용해 작품의 존재를 대조합니다.
    # 작품 정보가 있다는 사실만으로 B tv 서비스 가능 여부를 확정하지 않습니다.
    if CONTENT_DB_PATH.exists():
        contents = pd.read_csv(CONTENT_DB_PATH, sep="|").fillna("")
    else:
        contents = pd.DataFrame(columns=[
            "content_id", "title", "type", "genre", "year", "actors", "director", "tags"
        ])

    for optional_column in ["source_url", "image_url"]:
        if optional_column not in issues.columns:
            issues[optional_column] = ""

    return issues, themes, contents


def filter_recent_issues(issues, days=ISSUE_LOOKBACK_DAYS):
    issues = issues.copy()
    issues["date"] = pd.to_datetime(issues["date"], errors="coerce")

    today = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).normalize()
    start_date = today - pd.Timedelta(days=days - 1)

    recent = issues[
        (issues["date"] >= start_date)
        & (issues["date"] <= today)
    ].copy()

    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")

    return recent, start_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def classify_source(source):
    value = str(source).lower()
    if "youtube" in value or "유튜브" in value:
        return "YouTube 반응"
    if any(token in value for token in ["kobis", "kofic", "박스오피스", "극장"]):
        return "극장·박스오피스"
    if "netflix top" in value or "넷플릭스 top" in value:
        return "OTT 랭킹·신작"
    return "온라인 화제·뉴스"


def split_keywords(text):
    if pd.isna(text):
        return []

    return [
        t.strip()
        for t in str(text).replace("/", ",").replace(" ", ",").split(",")
        if t.strip()
    ]


def keyword_score(a_keywords, b_keywords):
    return len(set(a_keywords).intersection(set(b_keywords)))


def safe_url(url):
    url = str(url).strip()
    try:
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return url
    except ValueError:
        pass
    return ""


def build_theme_search_text(row):
    return " ".join([
        str(row.get("theme_name", "")),
        str(row.get("copy", "")),
        str(row.get("trigger_keywords", "")),
        str(row.get("genre", "")),
        str(row.get("mood", "")),
    ]).lower()


def extract_query_intents(query):
    text = str(query).lower()
    intents = []

    for intent, info in INTENT_MAP.items():
        if any(trigger in text for trigger in info["triggers"]):
            intents.append(intent)

    return intents


def expand_query_keywords(query):
    text = str(query).lower()

    tokens = split_keywords(query)
    expanded = set(tokens)

    for sw in STOPWORDS:
        text = text.replace(sw, " ")

    for intent in extract_query_intents(query):
        expanded.update(INTENT_MAP[intent]["keywords"])

    for token in text.replace(" ", ",").split(","):
        token = token.strip()
        if len(token) >= 2 and token not in STOPWORDS:
            expanded.add(token)

    return list(expanded)


def natural_theme_search(themes, query):
    if not query:
        return themes.copy()

    query_keywords = expand_query_keywords(query)
    intents = extract_query_intents(query)

    df = themes.copy()
    scored_rows = []

    for _, row in df.iterrows():
        search_text = build_theme_search_text(row)

        # must intent 필터: 공포/로맨스/힐링 등 핵심 의도는 반드시 포함되어야 함
        blocked = False
        for intent in intents:
            info = INTENT_MAP[intent]
            if info.get("must"):
                if not any(kw in search_text for kw in info["keywords"]):
                    blocked = True
                    break

        if blocked:
            continue

        score = 0
        matched = []

        for kw in query_keywords:
            if not kw:
                continue

            if kw in search_text:
                score += 3
                matched.append(kw)

        # intent bonus / negative
        for intent in intents:
            info = INTENT_MAP[intent]

            if any(kw in search_text for kw in info["keywords"]):
                score += 8

            if any(neg in search_text for neg in info.get("negative", [])):
                score -= 15

        # 제목/카피에 직접 맞으면 가산
        title_copy = f"{row.get('theme_name','')} {row.get('copy','')}".lower()
        for kw in query_keywords:
            if kw in title_copy:
                score += 3

        if score > 0:
            item = row.copy()
            item["natural_score"] = score
            item["matched_keywords"] = ",".join(sorted(set(matched))[:12])
            scored_rows.append(item)

    if not scored_rows:
        return themes.iloc[0:0].copy()

    result = pd.DataFrame(scored_rows)
    result = result.sort_values("natural_score", ascending=False)
    return result


def _normalize_search_text(value):
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def _search_term_score(term, field_value, exact_weight):
    term_text = str(term or "").strip().lower()
    field_text = str(field_value or "").strip().lower()
    if len(term_text) < 2 or not field_text:
        return 0.0
    term_norm = _normalize_search_text(term_text)
    field_norm = _normalize_search_text(field_text)
    if not term_norm or not field_norm:
        return 0.0
    if term_norm in field_norm:
        return float(exact_weight)

    field_tokens = [token for token in re.findall(r"[0-9a-z가-힣]+", field_text) if len(token) >= 2]
    best = 0.0
    for token in field_tokens:
        ratio = SequenceMatcher(None, term_norm, _normalize_search_text(token)).ratio()
        best = max(best, ratio)
    if best >= 0.86:
        return exact_weight * 0.55
    if best >= 0.76:
        return exact_weight * 0.25
    return 0.0


def rank_themes_by_ai_intent(themes, intent):
    if not intent:
        return themes.copy()

    weighted_terms = []
    for key, weight in [
        ("genres", 12),
        ("subjects", 11),
        ("narrative_elements", 10),
        ("moods", 8),
        ("positive_keywords", 8),
        ("anchor_titles", 3),
    ]:
        for value in intent.get(key, []) or []:
            if str(value).strip():
                weighted_terms.append((str(value).strip(), weight))

    negative_terms = [str(value).strip() for value in (intent.get("negative_keywords", []) or []) if str(value).strip()]
    scored_rows = []

    for _, row in themes.iterrows():
        fields = {
            "theme_name": row.get("theme_name", ""),
            "copy": row.get("copy", ""),
            "trigger_keywords": row.get("trigger_keywords", ""),
            "genre": row.get("genre", ""),
            "mood": row.get("mood", ""),
            "source_issue": row.get("source_issue", ""),
        }
        score = 0.0
        matched = []
        for term, base_weight in weighted_terms:
            term_score = max(
                _search_term_score(term, fields["theme_name"], base_weight * 1.35),
                _search_term_score(term, fields["copy"], base_weight * 1.15),
                _search_term_score(term, fields["trigger_keywords"], base_weight * 1.25),
                _search_term_score(term, fields["genre"], base_weight),
                _search_term_score(term, fields["mood"], base_weight * 0.85),
                _search_term_score(term, fields["source_issue"], base_weight * 0.55),
            )
            if term_score > 0:
                score += term_score
                matched.append(term)

        combined_text = " ".join(str(value) for value in fields.values()).lower()
        for negative in negative_terms:
            if _normalize_search_text(negative) in _normalize_search_text(combined_text):
                score -= 25

        # 여러 서로 다른 의도 축이 동시에 맞는 테마를 우선합니다.
        unique_matches = len({_normalize_search_text(value) for value in matched if value})
        score += min(18, unique_matches * 3)

        if score > 0:
            item = row.copy()
            item["ai_search_score"] = round(score, 2)
            item["ai_matched_terms"] = ",".join(dict.fromkeys(matched))
            scored_rows.append(item)

    if not scored_rows:
        return themes.iloc[0:0].copy()

    result = pd.DataFrame(scored_rows)
    return result.sort_values(["ai_search_score", "theme_name"], ascending=[False, True])


def render_ai_search_intent(intent, usage=None):
    if not intent:
        return
    chips = []
    for key in ["anchor_titles", "genres", "subjects", "narrative_elements", "moods", "positive_keywords"]:
        for value in intent.get(key, []) or []:
            if value and value not in chips:
                chips.append(str(value))
    chip_html = "".join(f'<span class="tag">{html.escape(value)}</span>' for value in chips[:8])
    negative = intent.get("negative_keywords", []) or []
    negative_html = ""
    if negative:
        negative_html = '<div class="small" style="margin-top:8px">제외 조건 · ' + html.escape(" · ".join(map(str, negative[:6]))) + '</div>'
    usage_text = "API 1회"
    if usage and usage.get("total_tokens"):
        usage_text += f' · {int(usage.get("total_tokens", 0)):,}토큰'
    st.markdown(
        '<div class="logic-card" style="margin-top:12px">'
        '<div class="small">AI 검색 의도</div>'
        f'<div style="font-weight:800;margin:4px 0 10px">{html.escape(str(intent.get("interpreted_request", "")))}</div>'
        f'{chip_html}{negative_html}'
        f'<div class="small" style="margin-top:10px">{html.escape(usage_text)}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def prepare_issues(issues):
    assessed = assess_issues(issues)
    return assessed[assessed["eligible"]].copy()


def read_collection_status():
    try:
        status = json.loads(Path("collection_status.json").read_text(encoding="utf-8"))
        return status if isinstance(status, dict) else {}
    except (OSError, ValueError):
        return {}


def build_core_issues(issues, news_pool=None):
    return attach_related_news(
        build_ranked_issues(issues), issues if news_pool is None else news_pool
    )


def find_matched_issues(theme, issues):
    theme_keywords = split_keywords(theme.get("trigger_keywords", ""))
    matched = []

    for _, issue in issues.iterrows():
        issue_keywords = split_keywords(issue.get("keywords", ""))
        score = keyword_score(issue_keywords, theme_keywords)

        if score > 0:
            matched.append({
                "date": issue.get("date", ""),
                "source": issue.get("source", ""),
                "source_group": issue.get("source_group", classify_source(issue.get("source", ""))),
                "issue_title": issue.get("issue_title", ""),
                "related_content": issue.get("related_content", ""),
                "description": issue.get("description", ""),
                "source_url": issue.get("source_url", ""),
                "score": score,
                "sort_priority": issue.get("sort_priority", 0)
            })

    return sorted(
        matched,
        key=lambda x: (x["score"], x["sort_priority"]),
        reverse=True
    )


def find_matched_contents(theme, contents, limit=12):
    theme_keywords = split_keywords(theme.get("trigger_keywords", ""))
    matched = []

    for _, content in contents.iterrows():
        tag_keywords = split_keywords(content.get("tags", ""))
        genre_keywords = split_keywords(content.get("genre", ""))
        title_keywords = split_keywords(content.get("title", ""))

        score = 0
        score += keyword_score(tag_keywords, theme_keywords) * 3
        score += keyword_score(genre_keywords, theme_keywords) * 2
        score += keyword_score(title_keywords, theme_keywords)

        if score > 0:
            matched.append({
                "content_id": content.get("content_id", ""),
                "title": content.get("title", ""),
                "type": content.get("type", ""),
                "genre": content.get("genre", ""),
                "year": content.get("year", ""),
                "tags": content.get("tags", ""),
                "score": score
            })

    return sorted(matched, key=lambda x: x["score"], reverse=True)[:limit]


def build_reason_summary(theme, matched_issues):
    if not matched_issues:
        return "최근 이슈와 테마 키워드의 직접 매칭 근거가 부족합니다."

    top = matched_issues[0]
    related = str(top.get("related_content", "")).strip()
    source = top.get("source_group", top.get("source", "외부 이슈"))

    if related:
        return f"{source}에서 '{related}' 관련 이슈가 감지되어, '{theme['theme_name']}' 테마와 연결성이 높습니다."

    return f"{source} 이슈의 핵심 키워드가 '{theme['theme_name']}' 테마 키워드와 강하게 매칭됩니다."



def get_runtime_secret(name, default=""):
    value = os.getenv(name, "")
    try:
        secret_value = st.secrets.get(name, "")
        if secret_value:
            value = str(secret_value)
    except Exception:
        pass
    return value or default


def get_active_theme_path():
    return THEME_DB_PATH if THEME_DB_PATH.exists() else OLD_THEME_PATH


def build_llm_issue_records(issues, max_issues=12):
    core = build_core_issues(issues).head(max_issues)
    records = []
    for index, (_, issue) in enumerate(core.iterrows(), start=1):
        records.append({
            "issue_key": f"I{index:02d}",
            "issue_title": issue.get("issue_title", ""),
            "related_content": issue.get("related_content", ""),
            "source_group": issue.get("source_group", ""),
            "confirmed_routes": issue.get("confirmed_routes", ""),
            "strength_label": issue.get("strength_label", ""),
            "evidence_status": issue.get("evidence_status", ""),
            "reference_label": issue.get("reference_label", ""),
            "metric_summary": issue.get("metric_summary", ""),
            "selection_reason": issue.get("selection_reason", ""),
            "source_url": issue.get("source_url", ""),
            "keywords": issue.get("keywords", ""),
            "description": issue.get("description", ""),
        })
    return records


def build_search_issue_records(issues, query, max_issues=5):
    """AI 검색 문장과 직접 연결되는 최근 이슈만 LLM에 전달합니다.

    일반적인 분위기·장르 요청에는 이슈 전체를 보내지 않아 속도와 토큰을 줄이고,
    작품명이 포함된 요청에서만 해당 작품의 최근 이슈 맥락을 보강합니다.
    """
    query_text = str(query or "").strip()
    query_norm = _normalize_search_text(query_text)
    query_tokens = {
        token for token in re.findall(r"[0-9a-z가-힣]+", query_text.lower())
        if len(token) >= 2 and token not in STOPWORDS
    }
    if not query_norm and not query_tokens:
        return []

    scored = []
    for _, issue in issues.iterrows():
        related = str(issue.get("related_content", "")).strip()
        title = str(issue.get("issue_title", "")).strip()
        keywords = str(issue.get("keywords", "")).strip()
        description = str(issue.get("description", "")).strip()
        anchors = [related, title]
        score = 0
        for anchor in anchors:
            anchor_norm = _normalize_search_text(anchor)
            if not anchor_norm:
                continue
            if anchor_norm in query_norm or query_norm in anchor_norm:
                score += 100
        issue_tokens = {
            token for token in re.findall(
                r"[0-9a-z가-힣]+",
                f"{related} {title} {keywords}".lower(),
            )
            if len(token) >= 2
        }
        overlap = query_tokens & issue_tokens
        score += len(overlap) * 12
        if score <= 0:
            continue
        scored.append((score, {
            "issue_title": title,
            "related_content": related,
            "keywords": keywords,
            "description": description,
            **{field: issue.get(field, "") for field in [
                "source_group", "strength_label", "evidence_status", "reference_label",
                "metric_summary", "selection_reason", "source_url",
            ]},
        }))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [record for _, record in scored[:max_issues]]


def get_github_writeback_config():
    return {
        "token": get_runtime_secret("GITHUB_WRITE_TOKEN"),
        "repo": get_runtime_secret("GITHUB_REPO"),
        "branch": get_runtime_secret("GITHUB_BRANCH", "main"),
        "theme_path": get_runtime_secret("THEME_DB_GITHUB_PATH", get_active_theme_path().name),
        "history_path": get_runtime_secret("THEME_HISTORY_GITHUB_PATH", THEME_HISTORY_PATH.name),
        "content_path": get_runtime_secret("THEME_CONTENT_GITHUB_PATH", THEME_CONTENT_PATH.name),
    }


def sync_persistent_storage_once():
    if st.session_state.get("persistent_storage_synced"):
        return st.session_state.get("persistent_storage_sync_result", {"status": "skipped"})

    config = get_github_writeback_config()
    if not github_writeback_configured(config):
        result = {"status": "not_configured"}
    else:
        try:
            result = sync_files_from_github(
                theme_path=get_active_theme_path(),
                history_path=THEME_HISTORY_PATH,
                content_path=THEME_CONTENT_PATH,
                config=config,
            )
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}

    st.session_state["persistent_storage_synced"] = True
    st.session_state["persistent_storage_sync_result"] = result
    return result


def _dedupe_text_values(values, limit=20):
    result = []
    seen = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = _normalize_search_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def enrich_ai_intent_from_issues(intent, query, issues):
    enriched = dict(intent or {})
    query_norm = _normalize_search_text(query)
    matched_rows = []
    for _, issue in issues.iterrows():
        anchors = [issue.get("related_content", ""), issue.get("issue_title", "")]
        if any(
            _normalize_search_text(anchor)
            and (
                _normalize_search_text(anchor) in query_norm
                or query_norm in _normalize_search_text(anchor)
            )
            for anchor in anchors
        ):
            matched_rows.append(issue)
    if not matched_rows:
        # 작품명이 일부만 입력된 경우를 대비해 2글자 이상 핵심 토큰으로 한 번 더 확인합니다.
        query_tokens = {token for token in re.findall(r"[0-9a-z가-힣]+", str(query).lower()) if len(token) >= 2}
        for _, issue in issues.iterrows():
            related_tokens = set(split_keywords(issue.get("related_content", "")))
            title_tokens = set(split_keywords(issue.get("issue_title", "")))
            if query_tokens & (related_tokens | title_tokens):
                matched_rows.append(issue)

    positive = list(enriched.get("positive_keywords", []) or [])
    subjects = list(enriched.get("subjects", []) or [])
    moods = list(enriched.get("moods", []) or [])
    genres = list(enriched.get("genres", []) or [])
    anchors = list(enriched.get("anchor_titles", []) or [])
    for issue in matched_rows[:4]:
        anchors.append(issue.get("related_content", ""))
        positive.extend(split_keywords(issue.get("keywords", "")))
        positive.extend(split_keywords(issue.get("description", ""))[:12])
        subjects.extend(split_keywords(issue.get("keywords", ""))[:8])
        issue_text = " ".join([str(issue.get("issue_title", "")), str(issue.get("description", "")), str(issue.get("keywords", ""))])
        for genre in ["공포", "스릴러", "미스터리", "SF", "사극", "로맨스", "코미디", "액션", "범죄", "애니메이션", "다큐"]:
            if genre.lower() in issue_text.lower():
                genres.append(genre)
        for mood in ["긴장감", "서늘한", "기괴한", "잔잔한", "따뜻한", "유쾌한", "묵직한", "감동", "설렘"]:
            if mood in issue_text:
                moods.append(mood)

    for suggestion in enriched.get("suggested_themes", []) or []:
        positive.extend(suggestion.get("keywords", []) or [])
        genres.append(suggestion.get("genre", ""))
        moods.append(suggestion.get("mood", ""))

    enriched["anchor_titles"] = _dedupe_text_values(anchors, 8)
    enriched["genres"] = _dedupe_text_values(genres, 8)
    enriched["moods"] = _dedupe_text_values(moods, 10)
    enriched["subjects"] = _dedupe_text_values(subjects, 14)
    enriched["positive_keywords"] = _dedupe_text_values(positive, 24)
    return enriched


def render_ai_theme_suggestions(intent, query="", content_map=None):
    suggestions = (intent or {}).get("suggested_themes", []) or []
    if not suggestions:
        return
    content_map = content_map or {}
    recs = ai_suggestions_to_recommendations(intent, query)
    rec_by_name = {
        str(rec.get("theme", {}).get("theme_name", "")): rec
        for rec in recs
    }
    st.markdown("### AI 신규 테마 제안")
    st.caption("현재 DB에 정확한 테마가 부족할 때 참고하는 임시 제안입니다. 자동으로 DB에 저장되지는 않습니다.")
    for item in suggestions[:5]:
        name = str(item.get("theme_name", ""))
        rec = rec_by_name.get(name, {})
        theme_key = str(rec.get("theme", {}).get("theme_id", ""))
        contents = content_map.get(theme_key, [])
        tags = "".join(
            f'<span class="tag">{html.escape(str(keyword))}</span>'
            for keyword in (item.get("keywords", []) or [])[:6]
        )
        last_date = _latest_content_date(contents)
        content_meta = f'{len(contents)}편' + (f' · {last_date} 갱신' if last_date else "")
        st.markdown(
            '<div class="ai-suggestion-card ai-suggestion-grid">'
            '<div>'
            '<div class="small">[AI 제안 · 미등록]</div>'
            f'<div class="theme-name">{html.escape(name)}</div>'
            f'<div class="copy">{html.escape(str(item.get("copy", "")))}</div>'
            f'<div class="small">{html.escape(str(item.get("genre", "")))} · {html.escape(str(item.get("mood", "")))}</div>'
            f'{tags}'
            '</div>'
            '<div class="ai-suggestion-content">'
            '<div class="theme-db-content-head">'
            '<div class="theme-db-content-title">추천 콘텐츠 후보</div>'
            f'<div class="theme-db-content-meta">{html.escape(content_meta)}</div>'
            '</div>'
            f'{_content_links_html(contents, visible=10)}'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )


def load_content_candidate_frame():
    return load_content_frame(THEME_CONTENT_PATH)


def attach_content_candidates(recommendations, run_id):
    frame = load_content_candidate_frame()
    if frame.empty or "run_id" not in frame.columns:
        return recommendations
    run_rows = frame[frame["run_id"].astype(str) == str(run_id)].copy()
    if run_rows.empty:
        return recommendations
    for index, rec in enumerate(recommendations, start=1):
        theme = rec.get("theme", {}) or {}
        theme_key = str(theme.get("theme_id", "")).strip() or f"R{index:02d}"
        rows = run_rows[run_rows.get("theme_key", pd.Series(dtype=str)).astype(str) == theme_key]
        if "rank" in rows.columns:
            rows = rows.assign(_rank=pd.to_numeric(rows["rank"], errors="coerce")).sort_values("_rank")
        contents = []
        for _, row in rows.iterrows():
            status = str(row.get("kinolights_status", ""))
            contents.append({
                "title": row.get("title", ""),
                "type": row.get("content_type", ""),
                "year": row.get("year", ""),
                "source_url": row.get("source_url", ""),
                "poster_url": row.get("poster_url", ""),
                "fetched_at": row.get("fetched_at", ""),
                "source": "키노라이츠 확인" if status == "verified" else "키노라이츠 제목 검색 링크",
                "kinolights_status": status,
                "reason": row.get("reason", ""),
            })
        rec["contents"] = contents
    return recommendations


def save_content_candidates(frame):
    return upsert_content_frame(THEME_CONTENT_PATH, frame)


def _latest_content_date(contents):
    values = [str(item.get("fetched_at", "")).strip() for item in contents if item.get("fetched_at")]
    if not values:
        return ""
    parsed = pd.to_datetime(pd.Series(values), errors="coerce", utc=True).dropna()
    if parsed.empty:
        return values[-1][:10]
    latest = parsed.max().tz_convert("Asia/Seoul")
    return latest.strftime("%Y.%m.%d")


def _content_links_html(contents, visible=16):
    if not contents:
        return '<div class="theme-db-content-empty">아직 콘텐츠 후보가 없습니다. 옆의 새로고침 버튼으로 연결할 수 있습니다.</div>'
    links = []
    for item in contents[:visible]:
        title = html.escape(str(item.get("title", "")))
        year = str(item.get("year", "")).strip()
        label = f"{title} · {html.escape(year)}" if year else title
        url = html.escape(str(item.get("source_url", "")), quote=True)
        if url:
            links.append(f'<a class="content-tag-link" href="{url}" target="_blank" rel="noopener">{label}</a>')
        else:
            links.append(f'<span class="content-tag-link">{label}</span>')
    more = ""
    if len(contents) > visible:
        more = f'<div class="content-more">외 {len(contents) - visible}편 · 총 {len(contents)}편</div>'
    return '<div class="content-chip-wrap">' + "".join(links) + '</div>' + more


_fragment = getattr(st, "fragment", lambda func: func)


@_fragment
def render_theme_db_card(
    row,
    contents,
    active_intent=False,
    *,
    api_key="",
    model="",
    github_config=None,
):
    """테마 정보와 콘텐츠 후보를 좌우로 표시하고, 카드 안에서만 콘텐츠를 새로고침합니다."""
    row_data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
    current_contents = list(contents or [])
    keyword_tags = "".join(
        f'<span class="tag">{html.escape(str(kw))}</span>'
        for kw in split_keywords(row_data.get("trigger_keywords", ""))[:12]
    )
    label = source_status_label("AI_DIRECTOR" if is_director_theme(row_data) else row_data.get("source_status", "LEGACY_UNVERIFIED"))
    source_issue = str(row_data.get("source_issue", "")).strip()
    source_issue_html = f'<div class="small">생성 근거: {html.escape(source_issue)}</div>' if source_issue else ""
    created = str(row_data.get("created_date", "")).strip()
    created_html = f' · 생성일 {html.escape(created)}' if created else ""
    matched_terms = split_keywords(row_data.get("ai_matched_terms", ""))[:8]
    matched_html = ""
    if active_intent and matched_terms:
        matched_html = '<div class="small" style="margin-top:8px">AI 매칭 · ' + html.escape(" · ".join(matched_terms)) + '</div>'

    theme_id = str(row_data.get("theme_id", "")).strip()
    safe_theme_id = re.sub(r"[^0-9A-Za-z_-]+", "_", theme_id or "theme")
    refresh_meta = {}

    with st.container(border=True, key=f"theme_card_{safe_theme_id}"):
        main_col, content_col = st.columns([0.44, 0.56], gap="large")

        with main_col:
            st.markdown(
                '<div class="theme-db-main">'
                f'<div class="small">{html.escape(theme_id)} · [{html.escape(label)}]{created_html}</div>'
                f'<div class="theme-name">{html.escape(str(row_data.get("theme_name", "")))}</div>'
                f'<div class="copy">노출명/카피: {html.escape(str(row_data.get("copy", "")))}</div>'
                f'<div class="small">장르: {html.escape(str(row_data.get("genre", "")))} · 무드: {html.escape(str(row_data.get("mood", "")))}</div>'
                f'{source_issue_html}{matched_html}'
                '<div class="section-label">테마 키워드</div>'
                f'{keyword_tags}'
                '</div>',
                unsafe_allow_html=True,
            )

        with content_col:
            head_left, head_right = st.columns([5, 1.15], gap="small")
            with head_right:
                refresh_clicked = st.button(
                    "↻ 새로고침",
                    key=f"refresh_content_{safe_theme_id}",
                    use_container_width=True,
                )

            if refresh_clicked:
                if not api_key:
                    st.error("OPENAI_API_KEY가 연결되지 않았습니다.")
                else:
                    try:
                        with st.spinner("이 테마의 콘텐츠 후보만 다시 찾는 중입니다..."):
                            target_rows = pd.DataFrame([row_data])
                            recommendations = theme_rows_to_recommendations(target_rows)
                            stored_frame, _, refresh_meta = build_and_store_content_candidates(
                                recommendations,
                                scope_run_id="THEME_DB",
                                api_key=api_key,
                                model=model,
                                github_config=github_config or {},
                                per_theme=16,
                                persist=True,
                                mirror_theme_db=False,
                            )
                            current_contents = content_map_for_scope(stored_frame, "THEME_DB").get(theme_id, [])
                    except ThemeGenerationError as exc:
                        st.error(str(exc))
                    except Exception as exc:
                        st.error(f"콘텐츠 새로고침 실패: {exc}")

            last_date = _latest_content_date(current_contents)
            content_meta = f'{len(current_contents)}편' + (f' · {last_date} 갱신' if last_date else "")
            with head_left:
                st.markdown(
                    '<div class="theme-db-content-title">추천 콘텐츠 후보</div>'
                    f'<div class="theme-db-content-meta">{html.escape(content_meta)}</div>',
                    unsafe_allow_html=True,
                )

            st.markdown(
                '<div class="theme-db-contents" style="border-left:0;padding-left:0;justify-content:flex-start">'
                f'{_content_links_html(current_contents, visible=16)}'
                '</div>',
                unsafe_allow_html=True,
            )

            if refresh_meta:
                if refresh_meta.get("github_status") == "error":
                    st.warning(f"화면은 갱신됐지만 영구 저장에 실패했습니다: {refresh_meta.get('github_error', '')}")
                else:
                    st.caption(
                        f"새로고침 완료 · 키노라이츠 직접 확인 {refresh_meta.get('verified_count', 0)}편 · "
                        f"제목 검색 링크 {refresh_meta.get('search_link_only_count', 0)}편"
                    )


def build_and_store_content_candidates(
    recommendations,
    *,
    scope_run_id,
    api_key,
    model,
    github_config,
    per_theme=16,
    persist=True,
    mirror_theme_db=False,
):
    suggestions, usage = generate_content_candidates_for_themes(
        recommendations=recommendations,
        api_key=api_key,
        model=model,
        per_theme=per_theme,
    )
    validated, kino_meta = validate_content_suggestions(
        suggestions,
        max_workers=10,
        allow_browser_fallback=False,
    )
    if not any(validated.values()):
        raise ThemeGenerationError("연결할 추천 작품을 받지 못했습니다. 콘텐츠를 다시 불러와 주세요.")
    # Fetch art only during an explicit content load, for the five visible
    # candidates per theme. Ordinary page loads never perform poster searches.
    visible_candidates = []
    for key, items in validated.items():
        seen = set()
        for index, item in enumerate(items):
            title = re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", str(item.get("title", ""))).lower())
            identity = (title, str(item.get("year", "")).strip())
            if not title or identity in seen:
                continue
            seen.add(identity)
            visible_candidates.append({**item, "_theme_key": key, "_candidate_index": index})
            if len(seen) == 5:
                break
    for item in enrich_poster_candidates(visible_candidates, limit=50):
        validated[item["_theme_key"]][item["_candidate_index"]]["poster_url"] = item.get("poster_url", "")
    candidate_frame = content_rows_to_frame(
        run_id=str(scope_run_id),
        recommendations=recommendations,
        validated=validated,
    )
    frames = [candidate_frame]
    if mirror_theme_db and not candidate_frame.empty:
        db_copy = candidate_frame.copy()
        db_copy = db_copy[~db_copy["theme_id"].astype(str).str.startswith("AIS-")]
        db_copy["run_id"] = "THEME_DB"
        frames.append(db_copy)
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    github_result = {"status": "not_saved"}
    stored_frame = combined
    if persist:
        stored_frame = save_content_candidates(combined)
        try:
            github_result = persist_content_candidates_to_github(
                content_path=THEME_CONTENT_PATH,
                config=github_config,
                run_id=str(scope_run_id),
            )
        except Exception as github_exc:
            github_result = {"status": "error", "error": str(github_exc)}
    return stored_frame, validated, {
        **kino_meta,
        "total_tokens": usage.get("total_tokens", 0),
        "github_status": github_result.get("status", ""),
        "github_error": github_result.get("error", ""),
    }



def fill_weekly_content_candidates(recommendations, *, run_id, api_key, model, github_config):
    """Fill one saved run, retaining its themes when content providers fail."""
    st.session_state["content_candidate_run_id"] = str(run_id)
    st.session_state.pop("content_candidate_error", None)
    st.session_state.pop("content_candidate_meta", None)
    try:
        _, _, content_meta = build_and_store_content_candidates(
            recommendations,
            scope_run_id=str(run_id),
            api_key=api_key,
            model=model,
            github_config=github_config,
            per_theme=16,
            persist=True,
            mirror_theme_db=True,
        )
        st.session_state["content_candidate_meta"] = content_meta
        return True
    except Exception as exc:
        st.session_state["content_candidate_error"] = str(exc)
        return False


def source_status_label(value):
    mapping = {
        "AI_GENERATED": "AI 신규 생성",
        "AI_DIRECTOR": "AI 디렉터",
        "HUMAN_APPROVED": "검증 테마",
        "USED": "실제 활용",
        "LEGACY_UNVERIFIED": "기존 임시 DB",
        "EXISTING_DB": "기존 DB 활용",
    }
    return mapping.get(str(value), str(value) or "기존 임시 DB")


def format_recommended_at(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return text
    if getattr(parsed, "tzinfo", None) is not None:
        try:
            parsed = parsed.tz_convert("Asia/Seoul").tz_localize(None)
        except Exception:
            pass
    return parsed.strftime("%Y.%m.%d %H:%M")


def render_section_heading(number, title, description):
    st.markdown(
        '<header class="section-heading"><div class="section-title-row">'
        f'<span class="section-number">{number:02d}</span><h2>{html.escape(title)}</h2></div>'
        f'<p>{html.escape(description)}</p></header>',
        unsafe_allow_html=True,
    )


def render_generation_status(meta, added_count, github_result):
    new_count = int(meta.get("new_count", 0))
    existing_count = int(meta.get("existing_count", 0))
    status = str(github_result.get("status", "not_configured"))
    if status == "success":
        persist_text = "영구 저장 완료"
    elif status == "restored":
        persist_text = "영구 저장된 최신 추천"
    elif status == "error":
        persist_text = "영구 저장 실패"
    else:
        persist_text = "영구 저장 미설정"
    recommended_at = format_recommended_at(meta.get("recommended_at", ""))
    generated_text = f"마지막 생성: {recommended_at}" if recommended_at else "마지막 생성 시각 없음"
    st.markdown(
        '<div class="logic-card">'
        '<div class="theme-name">최근 생성 요약</div>'
        f'<div class="logic-desc">신규 {new_count}개 · 기존 활용 {existing_count}개 · DB 추가 {added_count}개</div>'
        f'<div class="small">{html.escape(generated_text)} · {html.escape(persist_text)}</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def youtube_video_id(url):
    value = safe_url(url)
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.netloc in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/")
    if "youtube.com" in parsed.netloc:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [""])[0]
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/"):
            return parsed.path.rstrip("/").split("/")[-1]
    return ""


def get_issue_image_url(issue):
    explicit = safe_url(issue.get("image_url", ""))
    if explicit:
        return explicit
    video_id = youtube_video_id(issue.get("source_url", ""))
    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


def render_issue_media(issue):
    image_url = get_issue_image_url(issue)
    issue_title = str(issue.get("issue_title", "")).strip()
    related = str(issue.get("related_content", "")).strip()
    # "1위", "신규"처럼 작품명이 아닌 추출값은 이미지 대체 문구로 쓰지 않습니다.
    invalid_related = (
        not related
        or bool(re.fullmatch(r"(?:제?\d+위|\d+위|신규|상승|공개)", related))
        or len(related) < 3
    )
    media_title = issue_title if invalid_related else related
    title = html.escape(media_title)
    source_group = html.escape(str(issue.get("source_group", "콘텐츠 이슈")))
    if image_url:
        safe_image = html.escape(image_url, quote=True)
        return (
            '<div class="issue-media">'
            f'<img src="{safe_image}" alt="{title}" loading="lazy" '
            'onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\';">'
            '<div class="issue-placeholder image-error-placeholder" style="display:none">'
            f'<span>{source_group}</span><strong>대표 이미지 없음</strong>'
            '</div></div>'
        )
    return (
        '<div class="issue-media"><div class="issue-placeholder">'
        f'<span>{source_group}</span><strong>대표 이미지 없음</strong>'
        '</div></div>'
    )


def issue_evidence_html(issue, presentation=None):
    public = presentation or present_issue(issue)
    rows = "".join(
        f'<div><dt>{html.escape(str(label))}</dt><dd>{html.escape(str(value))}</dd></div>'
        for label, value in public["details"]
    )
    source_url = safe_url(issue.get("source_url", ""))
    source_host = (urlparse(source_url).hostname or "").lower()
    source_label = "Google 뉴스에서 기사 보기" if source_host == "news.google.com" or source_host.endswith(".news.google.com") else "원문에서 확인"
    source_link = (
        f'<a class="evidence-source-link" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">{source_label} ↗</a>'
        if source_url else ''
    )
    return (
        '<div class="evidence-panel"><dl class="evidence-facts">' + rows + '</dl>'
        f'<p class="evidence-note">{html.escape(public["verification_note"])}</p>'
        f'{source_link}{related_news_html(issue)}</div>'
    )


def related_news_html(issue):
    articles = issue.get("related_news", [])
    if not isinstance(articles, list):
        return ""
    links = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        url = safe_url(article.get("url", ""))
        if not url:
            continue
        label = html.escape(str(article.get("title", "")))
        byline = html.escape(" · ".join(str(article.get(key, "")) for key in ("publisher", "published_at", "via") if article.get(key)))
        links.append(
            '<li><a href="' + html.escape(url, quote=True) + '" target="_blank" rel="noopener noreferrer">'
            + label + ' ↗</a><span>' + byline + '</span></li>'
        )
    return ('<div class="related-news"><p>함께 읽을 관련 기사</p><ul>' + ''.join(links) + '</ul></div>') if links else ''


def news_digest_html(issue):
    public = present_issue(issue)
    return (
        '<article class="news-digest-card"><span class="tag">' + html.escape(public["headline"]) + '</span>'
        '<h3>' + html.escape(str(issue.get("related_content", ""))) + '</h3>'
        '<p>' + html.escape(str(issue.get("issue_title", ""))) + '</p>'
        '<span class="small">' + html.escape(str(issue.get("publisher", ""))) + ' · ' + html.escape(public["period_label"]) + '</span>'
        '<details><summary>선정 근거</summary>' + issue_evidence_html(issue, public) + '</details></article>'
    )


def editorial_issue_card_html(issue, index):
    related = str(issue.get("related_content", "")).strip() or str(issue.get("issue_title", ""))
    public = present_issue(issue)
    image_url = get_issue_image_url(issue)
    image_style = ""
    if image_url:
        image = html.escape(quote(image_url, safe=":/?=&%+#@,;"), quote=True)
        image_style = f' style="background-image:url(\'{image}\')"'
    source_url = safe_url(issue.get("source_url", ""))
    opening = (
        f'<a class="poster-link" href="{html.escape(source_url, quote=True)}" '
        'target="_blank" rel="noopener noreferrer">'
        if source_url else '<div class="poster-link">'
    )
    closing = '</a>' if source_url else '</div>'
    return (
        '<article class="editorial-card">'
        f'{opening}<div class="editorial-poster tone-{(index - 1) % 4}">'
        f'<div class="poster-image"{image_style}></div><div class="poster-shade"></div>'
        f'<span class="poster-index">{index:02d} / THIS WEEK</span>'
        f'<h3 class="poster-title">{html.escape(_compact_card_text(related, 50))}</h3>'
        f'</div>{closing}<div class="editorial-meta">'
        f'<span class="editorial-source">{html.escape(public["source_label"])}</span></div>'
        f'<p class="editorial-title">{html.escape(public["headline"])}</p>'
        f'<p class="editorial-description">{html.escape(public["reason"])}</p>'
        f'<p class="editorial-reference">{html.escape(public["period_label"])}</p>'
        '<details class="editorial-evidence"><summary>선정 근거</summary>'
        f'{issue_evidence_html(issue, public)}</details></article>'
    )


def render_issue_card(issue):
    selected = bool(issue.get("eligible", False))
    public = present_issue(issue)
    source_url = safe_url(issue.get("source_url", ""))
    title = issue.get("related_content", "") if selected else issue.get("issue_title", "")
    title_text = html.escape(str(title or issue.get("issue_title", "")))
    title_html = (
        f'<a class="theme-name issue-title-link" href="{html.escape(source_url, quote=True)}" target="_blank" rel="noopener noreferrer">{title_text}</a>'
        if source_url else f'<div class="theme-name">{title_text}</div>'
    )
    reason = public["reason"] if selected else str(issue.get("exclusion_reason", "선정 기준 확인이 필요해요"))
    if not selected and issue.get("quality_notes", ""):
        reason += " · " + str(issue["quality_notes"])
    body = (
        '<div class="issue-card-body">'
        f'<span class="tag">{html.escape(public["source_label"])}</span>{title_html}'
        f'<div class="issue-byline small">{html.escape(public["period_label"])}</div>'
        f'<div class="issue-metrics">{html.escape(public["headline"] if selected else "선정 보류")}</div>'
        f'<div class="issue-description small">{html.escape(reason)}</div></div>'
    )
    st.markdown(f'<div class="card issue-card">{body}{render_issue_media(issue)}</div>', unsafe_allow_html=True)
    with st.expander("선정 근거 · 숫자와 출처 보기"):
        st.markdown(issue_evidence_html(issue, public), unsafe_allow_html=True)
        records = issue.get("evidence_records", [])
        if isinstance(records, list) and records:
            st.caption(f"같은 작품으로 묶인 자료 {len(records)}건")
            for record in records:
                url = safe_url(record.get("url", record.get("source_url", "")))
                label = html.escape(str(record.get("title", "원문")))
                link = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>' if url else label
                st.markdown(link, unsafe_allow_html=True)

def render_content_tags(matched_contents):
    if not matched_contents:
        return '<span class="small">키노라이츠에서 확인된 후보가 없습니다.</span>'

    content_tags = ""
    for c in matched_contents:
        title = html.escape(str(c.get("title", "")))
        content_type = html.escape(str(c.get("type", "")))
        year = html.escape(str(c.get("year", "")))
        source_url = safe_url(c.get("source_url", ""))
        source = str(c.get("source", ""))
        reason = str(c.get("reason", ""))
        status = str(c.get("kinolights_status", ""))

        meta_parts = [part for part in [content_type, year] if part]
        label = title + (" · " + " · ".join(meta_parts) if meta_parts else "")
        status_class = "content-verified" if status == "verified" else "content-link-only"
        css_class = f"content-tag-link {status_class}" if source_url else "tag"
        tooltip = " · ".join(part for part in [source, reason] if part)
        if source_url:
            content_tags += (
                f'<a class="{css_class}" href="{html.escape(source_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer" title="{html.escape(tooltip)}">{label}</a>'
            )
        else:
            content_tags += f'<span class="{css_class}" title="{html.escape(source)}">{label}</span>'
    return content_tags


def _render_content_meta(meta):
    if not meta:
        return ""
    status = str(meta.get("status", ""))
    query_count = int(meta.get("query_count", 0) or 0)
    raw_count = int(meta.get("raw_count", 0) or 0)
    unique_count = int(meta.get("unique_count", 0) or 0)
    cache_hits = int(meta.get("cache_hits", 0) or 0)
    fallback_count = int(meta.get("legacy_fallback_count", 0) or 0)
    errors = meta.get("errors", []) or []

    if status == "ok":
        text = f"키노라이츠 검색어 {query_count}개 · 원본 {raw_count}건 · 중복 제거 {unique_count}편"
        if cache_hits:
            text += f" · 캐시 {cache_hits}개"
        if fallback_count:
            text += f" · 기존풀 보완 {fallback_count}편"
    elif status == "disabled":
        text = "키노라이츠 실시간 탐색 꺼짐"
    elif status == "empty":
        text = f"키노라이츠 검색어 {query_count}개를 조회했지만 후보를 찾지 못했습니다."
    else:
        text = f"키노라이츠 후보 조회 실패 · 검색어 {query_count}개"

    if errors:
        text += f" · 오류 {len(errors)}건"
    return f'<div class="content-source-note">{html.escape(text)}</div>'


def _compact_card_text(value, max_chars=95):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def render_theme_card(idx, rec, issue_pool=None):
    st.markdown(theme_card_html(idx, rec, issue_pool), unsafe_allow_html=True)



def _logic_detail_box_html(title, text):
    if not text:
        return ""
    return (
        '<div class="logic-detail-box">'
        f'<div class="logic-detail-title">{html.escape(str(title))}</div>'
        f'<div class="logic-detail-text">{html.escape(str(text))}</div>'
        '</div>'
    )


def _logic_detail_list_html(title, items):
    if not items:
        return ""
    lis = "".join(f'<li>{html.escape(str(item))}</li>' for item in items)
    return (
        '<div class="logic-detail-box">'
        f'<div class="logic-detail-title">{html.escape(str(title))}</div>'
        f'<ol class="logic-detail-list">{lis}</ol>'
        '</div>'
    )


def build_logic_modal_html(detail_key):
    detail = SOURCE_DETAIL_LOGIC.get(detail_key, {})
    if not detail:
        return ""
    parts = [
        f'<div class="logic-modal-heading">{html.escape(detail.get("source_name", "수집 경로 세부 로직"))}</div>',
        f'<div class="logic-modal-weight">{html.escape(detail.get("weight", ""))}</div>',
        _logic_detail_box_html("어떤 작품을 찾나요?", detail.get("purpose", "")),
        _logic_detail_box_html("어디에서 가져오나요?", detail.get("integration", "")),
        _logic_detail_list_html("자료를 모으는 방법", detail.get("collection", [])),
        _logic_detail_list_html("새 후보로 넣는 조건", detail.get("filters", [])),
        _logic_detail_box_html("화면에 보이는 순서", detail.get("ranking", "")),
        _logic_detail_box_html("카드를 읽는 방법", detail.get("score", "")),
    ]
    if detail.get("queries"):
        query_text = "\n".join(detail["queries"])
        parts.append('<div class="logic-detail-title">실제 검색어</div>')
        parts.append(f'<div class="logic-code">{html.escape(query_text)}</div>')
    if detail.get("channels"):
        channel_text = "\n".join(detail["channels"])
        parts.append('<div class="logic-detail-title" style="margin-top:14px">수집 대상 공식 채널</div>')
        parts.append(f'<div class="logic-code">{html.escape(channel_text)}</div>')
    parts.append(_logic_detail_box_html("자료를 볼 때 참고해 주세요", detail.get("limitations", "")))
    return "".join(parts)

def render_collection_logic():
    chip_parts = []
    for name, info in SOURCE_CONFIG.items():
        detail_key = SOURCE_DETAIL_KEY_BY_NAME.get(name, "")
        modal_id = f"logic-modal-{detail_key}"
        detail_html = build_logic_modal_html(detail_key)
        chip_parts.append(
            f'<div class="weight-chip-wrap">'
            f'<input class="logic-modal-toggle" type="checkbox" id="{modal_id}">'
            f'<label class="weight-chip" for="{modal_id}" title="수집 기준 자세히 보기">'
            f'{html.escape(name)}</label>'
            '<div class="weight-tooltip">'
            f'{html.escape(info["tooltip"])}'
            f'<label class="weight-detail-link" for="{modal_id}">세부내용 보기 →</label>'
            '</div>'
            f'<div class="logic-modal-overlay">'
            f'<div class="logic-modal-panel">'
            f'<label class="logic-modal-close" for="{modal_id}" aria-label="닫기">×</label>'
            f'{detail_html}'
            '</div></div>'
            '</div>'
        )
    chips = "".join(chip_parts)
    steps = "".join(
        '<div class="selection-step">'
        f'<strong>{number}. {html.escape(title)}</strong><p>{html.escape(body)}</p></div>'
        for number, (title, body) in enumerate(SELECTION_STEPS, 1)
    )
    html_block = (
        '<div class="logic-card">'
        '<div class="theme-name">이슈를 이렇게 골랐어요</div>'
        '<div class="logic-desc">영화·드라마·예능의 관련 영상, 관객 순위, 넷플릭스 순위와 새로운 소식에서 작품을 고릅니다.</div>'
        '<div class="section-label">출처별 집계 기준</div>'
        f'{chips}<div class="section-label">선정 원칙</div>{steps}</div>'
    )
    st.markdown(html_block, unsafe_allow_html=True)


def render_collection_health(assessed, expanded=False):
    status_path = ISSUE_PATH.with_name("collection_status.json")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {}
    except (OSError, ValueError):
        status = {}
    sources = status.get("sources", {})
    state_names = {
        "ok": "수집 완료", "empty": "조건 충족 0건", "failed": "수집 실패",
        "partial": "일부 수집", "skipped": "미실행", "stale": "갱신 지연",
    }
    flagged = sum(item.get("state") in {"failed", "partial", "stale", "skipped"} for item in sources.values())
    summary = f"확인 필요 {flagged}개 경로" if flagged else "저장 데이터와 실행 기록 확인"
    if not sources:
        summary = "실행 기록 없음 · 저장 자료 재평가"
    with st.expander(f"출처별 수집 상태 · {summary}", expanded=expanded):
        recent, _, _ = filter_recent_issues(assessed)
        cards = []
        for key, name in zip(["youtube", "kobis", "netflix", "news"], SOURCE_CONFIG):
            record = sources.get(key, {})
            state = record.get("state", "")
            last_run = str(record.get("collected_at", ""))
            run_at = pd.to_datetime(last_run, errors="coerce", utc=True)
            if state in {"ok", "empty"} and pd.notna(run_at) and pd.Timestamp.now(tz="UTC") - run_at > pd.Timedelta(hours=36):
                state = "stale"
            candidates = recent[recent["source_group"] == name]
            usable = assessed[(assessed["source_group"] == name) & assessed["eligible"]]
            label = state_names.get(state, "실행 기록 없음")
            reference = str(record.get("reference_period") or record.get("reference_date") or "기준 기간은 개별 근거에 표시")
            error = str(record.get("error", ""))
            cards.append(
                '<div class="collection-source">'
                f'<strong>{html.escape(name)}</strong><span class="collection-state">{html.escape(label)}</span>'
                f'<div>저장 후보 {len(candidates)}건 · 사용 가능 {len(usable)}건</div>'
                f'<div>{html.escape(reference)}</div>'
                f'<div>{html.escape(last_run or "다음 수집부터 실행 시각 기록")}</div>'
                + (f'<div class="collection-error">{html.escape(error)}</div>' if error else '')
                + '</div>'
            )
        st.markdown('<div class="collection-grid">' + ''.join(cards) + '</div>', unsafe_allow_html=True)
        st.caption("저장 후보는 최근 7일 포착 기록입니다. 사용 가능 여부는 각 출처의 실제 기준일로 판단합니다. 수집 실패 시 마지막 정상 자료를 보존하며, 오래된 근거는 선정에서 제외합니다.")


try:
    all_issues, themes, contents = load_data()
    recent_issues, issue_start_date, issue_end_date = filter_recent_issues(
        all_issues,
        days=ISSUE_LOOKBACK_DAYS
    )
    assessed_issues = assess_issues(apply_collection_context(all_issues, read_collection_status()))
    issues = assessed_issues[assessed_issues["eligible"]].copy()
    review_issues = assessed_issues[assessed_issues.index.isin(recent_issues.index) & ~assessed_issues["eligible"]].copy()
except Exception as e:
    st.error(f"CSV 로드 실패: {e}")
    st.stop()


if st.session_state["page"] == "issue_db":
    st.markdown("<h1>🗂 최근 이슈 전체 보기</h1>", unsafe_allow_html=True)
    st.caption(f"{issue_start_date} ~ {issue_end_date} 기준 외부 콘텐츠 이슈와 출처 링크를 확인합니다.")

    if st.button("← 추천 화면으로 돌아가기"):
        go_page("home")
        st.rerun()

    st.markdown("---")

    render_collection_health(assessed_issues, expanded=True)
    render_collection_logic()

    source_options = ["전체"] + list(SOURCE_CONFIG.keys())

    c1, c2 = st.columns([1, 2])

    with c1:
        selected_source = st.selectbox(
            "경로별 보기",
            source_options,
            key="issue_source_filter",
        )

    with c2:
        search = st.text_input(
            "이슈명/콘텐츠/키워드/출처 검색",
            placeholder="예: 미키17, 살목지, 쇼츠, 요리"
        )

    view = st.radio("선정 상태", ["선정된 작품", "검토·제외 후보"], horizontal=True)
    filtered = issues.copy() if view == "선정된 작품" else review_issues.copy()
    total_count = len(build_core_issues(issues)) if view == "선정된 작품" else len(filtered)

    if selected_source != "전체":
        filtered = filtered[filtered["source_group"] == selected_source]

    if view == "선정된 작품":
        filtered = build_core_issues(filtered, news_pool=issues)

    if search:
        s = search.strip()
        filtered = filtered[
            filtered["issue_title"].astype(str).str.contains(s, case=False, na=False, regex=False)
            | filtered["related_content"].astype(str).str.contains(s, case=False, na=False, regex=False)
            | filtered["keywords"].astype(str).str.contains(s, case=False, na=False, regex=False)
            | filtered["source"].astype(str).str.contains(s, case=False, na=False, regex=False)
            | filtered["description"].astype(str).str.contains(s, case=False, na=False, regex=False)
        ]

    st.markdown(f"### 전체 {total_count}개 중 {len(filtered)}개 표시")

    for _, issue in filtered.iterrows():
        render_issue_card(issue)
        keyword_tags = "".join(
            f'<span class="tag">{html.escape(kw)}</span>'
            for kw in split_keywords(issue.get("keywords", ""))[:16]
        )
        if keyword_tags:
            st.markdown(f'<div style="margin:-10px 20px 18px 20px">{keyword_tags}</div>', unsafe_allow_html=True)


elif st.session_state["page"] == "theme_db":
    st.markdown("<h1>테마 라이브러리</h1>", unsafe_allow_html=True)
    st.caption("저장된 테마를 검색하고 관리하세요. 문장으로 찾거나 새 기획을 시작하려면 AI 디렉터와 이야기할 수 있어요.")
    if st.session_state.get("director_adoption_notice"):
        st.success(st.session_state.pop("director_adoption_notice"))
        if st.session_state.pop("director_refresh_required", False):
            st.warning("저장은 완료됐어요. 다른 작업의 변경을 보호해 화면 자료는 유지했습니다. 앱을 새로 열면 최신 저장 자료를 확인할 수 있어요.")

    if st.button("← 추천 화면으로 돌아가기"):
        go_page("home")
        st.rerun()

    st.markdown("---")

    openai_api_key = get_runtime_secret("OPENAI_API_KEY")
    openai_model = get_runtime_secret("OPENAI_MODEL", "gpt-5.6-luna")

    with st.container(border=True):
        st.markdown("### 빠른 검색")
        query_column, status_column = st.columns([2, 1], gap="small")
        with query_column:
            quick_query = st.text_input(
                "테마명·키워드·장르·무드 검색",
                key="theme_quick_search_input",
                placeholder="예: 공포, 왕, 정주행, 팬 반응, 여름밤",
            )
        with status_column:
            status_options = ["전체", "AI 신규 생성", "AI 디렉터", "기존 임시 DB", "검증 테마", "실제 활용"]
            selected_status = st.selectbox("테마 출처·상태", status_options, key="theme_library_source_filter")
        st.button("AI 디렉터와 새 기획 이야기하기 ↗", key="library_open_ai_director",
                  on_click=activate_director_tab, width="stretch")

    reset_col, download_col = st.columns([1, 2])
    with reset_col:
        st.button("검색 초기화", use_container_width=True, on_click=clear_theme_ai_search_state)
    with download_col:
        export_df = ensure_theme_schema(themes)
        st.download_button(
            "⬇ 현재 테마 DB 다운로드",
            data=export_df.to_csv(sep="|", index=False, encoding="utf-8-sig").encode("utf-8-sig"),
            file_name="theme_db_current.csv",
            mime="text/csv",
            use_container_width=True,
        )

    github_config = get_github_writeback_config()
    content_frame = load_content_candidate_frame()
    db_content_map = content_map_for_scope(content_frame, "THEME_DB")
    coverage = content_coverage(themes, content_frame)

    st.markdown("### 콘텐츠 후보 구축 현황")
    st.caption(
        f"테마 DB {coverage['total_themes']}개 각각에 추천 콘텐츠 후보 16편을 한 번씩 저장하는 초기 구축 작업입니다. "
        f"현재 연결된 테마 수가 {coverage['total_themes']}개가 되면 최초 구축이 끝납니다."
    )
    cov1, cov2, cov3 = st.columns(3)
    cov1.metric("연결 완료", f"{coverage['connected_themes']} / {coverage['total_themes']}")
    cov2.metric("남은 테마", coverage["missing_themes"])
    cov3.metric("저장된 콘텐츠 후보", f"{coverage['candidate_rows']:,}편")
    if coverage["total_themes"]:
        st.progress(min(1.0, coverage["connected_themes"] / coverage["total_themes"]))

    db_batch_size = min(20, max(1, int(coverage["missing_themes"] or 20)))
    build_missing_clicked = False
    refresh_stale_clicked = False
    if coverage["missing_themes"] > 0:
        build_missing_clicked = st.button(
            f"🎬 다음 미연결 테마 {db_batch_size}개 콘텐츠 연결",
            use_container_width=True,
        )
        st.caption("최초 구축 중에는 이 버튼만 사용합니다. 모든 테마 연결이 끝나면 정기 업데이트 버튼으로 바뀝니다.")
    else:
        refresh_stale_clicked = st.button(
            "↻ 30일 이상 지난 테마 20개 콘텐츠 업데이트",
            use_container_width=True,
        )
        st.caption("전체 테마 연결이 완료된 뒤, 갱신일이 30일 이상 지난 테마 20개의 콘텐츠 후보를 다시 찾습니다.")

    def run_theme_content_update(target_rows, action_label):
        if not openai_api_key:
            st.error("OPENAI_API_KEY가 연결되지 않았습니다.")
            return
        if target_rows.empty:
            st.info("처리할 테마가 없습니다.")
            return
        try:
            with st.spinner(f"{action_label}의 콘텐츠 후보를 생성하고 키노라이츠에서 확인하는 중입니다..."):
                recommendations = theme_rows_to_recommendations(target_rows)
                _, _, build_meta = build_and_store_content_candidates(
                    recommendations,
                    scope_run_id="THEME_DB",
                    api_key=openai_api_key,
                    model=openai_model,
                    github_config=github_config,
                    per_theme=16,
                    persist=True,
                    mirror_theme_db=False,
                )
                st.session_state["theme_db_content_meta"] = build_meta
            st.rerun()
        except ThemeGenerationError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(f"테마 DB 콘텐츠 연결 실패: {exc}")

    if build_missing_clicked:
        target_rows = missing_theme_rows(themes, content_frame, limit=db_batch_size)
        run_theme_content_update(target_rows, f"미연결 테마 {len(target_rows)}개")
    elif refresh_stale_clicked:
        target_rows = stale_theme_rows(themes, content_frame, limit=20, stale_days=30)
        run_theme_content_update(target_rows, f"30일 이상 지난 테마 {len(target_rows)}개")

    db_build_meta = st.session_state.get("theme_db_content_meta", {})
    if db_build_meta:
        st.caption(
            f"최근 처리 · 키노라이츠 직접 확인 {db_build_meta.get('verified_count', 0)}편 · "
            f"제목 검색 링크 {db_build_meta.get('search_link_only_count', 0)}편 · "
            f"API {db_build_meta.get('total_tokens', 0):,}토큰"
        )
        if db_build_meta.get("github_status") == "error":
            st.warning(f"콘텐츠 후보 영구 저장 실패: {db_build_meta.get('github_error', '')}")

    filtered = themes.copy()
    director_mask = director_theme_mask(themes, load_recommendation_history(THEME_HISTORY_PATH, days=36500))
    if selected_status == "AI 디렉터":
        filtered = themes.loc[director_mask].iloc[::-1].copy()
    elif selected_status != "전체":
        reverse_map = {
            "AI 신규 생성": "AI_GENERATED",
            "기존 임시 DB": "LEGACY_UNVERIFIED",
            "검증 테마": "HUMAN_APPROVED",
            "실제 활용": "USED",
        }
        filtered = filtered[filtered["source_status"].astype(str) == reverse_map[selected_status]]
        if selected_status == "AI 신규 생성":
            filtered = filtered.loc[~director_mask.loc[filtered.index]]

    if str(quick_query or "").strip():
        shown = natural_theme_search(filtered, quick_query).copy()
        st.markdown(f"### 빠른 검색 결과 {len(shown)}개")
        if shown.empty:
            st.info("빠른 검색 결과가 없습니다. AI 디렉터에서 원하는 분위기와 소재를 이야기해 보세요.")
    else:
        shown = filtered.copy()
        st.markdown(f"### 전체 {len(themes)}개 중 {len(shown)}개 표시")

    for _, row in shown.iterrows():
        if director_mask.loc[row.name]:
            row = row.copy()
            row["source_status"] = "AI_DIRECTOR"
        theme_id = str(row.get("theme_id", ""))
        render_theme_db_card(
            row,
            db_content_map.get(theme_id, []),
            active_intent=False,
            api_key=openai_api_key,
            model=openai_model,
            github_config=github_config,
        )

else:
    with st.container(key="magazine_nav"):
        st.markdown(
            '<div class="magazine-brand">B tv<span class="brand-plus">+</span>'
            '<span class="brand-caption">THEME CURATOR</span></div>',
            unsafe_allow_html=True,
        )

    story_tab, recommendation_tab, director_tab, ott_tab, media_tab = st.tabs(
        ["지금 주목할 이야기", "추천 테마", "AI 디렉터", "OTT는 지금", "미디어 인사이트"],
        key="home_sections", on_change="rerun",
        default=st.session_state.get("home_tab_default", "지금 주목할 이야기"),
    )
    with story_tab:
        st.markdown(
            '<section class="magazine-hero">'
            '<div class="hero-copy"><div class="eyebrow">THE WEEKLY EDIT</div>'
            '<h1>이슈를 읽고, 취향을 큐레이션하다.</h1>'
            '<p class="hero-description">이번 주의 이슈에서 새로운 테마를 발견하고, 어울리는 콘텐츠를 연결하세요.</p>'
            '<div class="hero-meta">'
            f'<span>선정 작품 <strong>{len(build_core_issues(issues)):,}</strong></span>'
            f'<span>테마 컬렉션 <strong>{len(themes):,}</strong></span>'
            f'<span>{html.escape(str(issue_start_date))} — {html.escape(str(issue_end_date))}</span>'
            '</div></div>'
            '<div class="hero-art" aria-hidden="true"><div class="orbit"></div>'
            '<div class="orbit-inner"></div><span class="art-label">STORIES, CONNECTED.</span></div>'
            '</section>',
            unsafe_allow_html=True,
        )
        with st.container(key="spotlight_section"):
            with st.container(key="spotlight_heading"):
                heading, all_issues_link = st.columns([5, 1.5], gap="small", vertical_alignment="center")
                with heading:
                    render_section_heading(1, '지금 주목할 이야기', '')
                with all_issues_link:
                    if st.button("이슈 전체 보기 ↗", key="spotlight_all_issues", use_container_width=True):
                        st.session_state["issue_source_filter"] = "전체"
                        go_page("issue_db")
                        st.rerun()
            st.markdown('<p class="section-intro">YouTube 인기 영상·반응, 관객 순위와 작품의 새 소식에서 골랐어요.</p>', unsafe_allow_html=True)
            if issues.empty:
                st.info("최근 수집된 이슈가 없습니다. 이슈가 수집되면 이곳에서 확인할 수 있어요.")
            else:
                main_issues = build_core_issues(issues).head(10)
                cards = "".join(
                    editorial_issue_card_html(issue, index)
                    for index, (_, issue) in enumerate(main_issues.iterrows(), start=1)
                )
                st.markdown(issue_carousel_html(cards), unsafe_allow_html=True)
                # The script is fixed application code; article text never enters it.
                with st.container(key="issue_carousel_script"):
                    st.html(CAROUSEL_SCRIPT, unsafe_allow_javascript=True)

                news_issues = build_core_issues(issues[issues["source_group"] == "온라인 화제·뉴스"], news_pool=issues)
                if not news_issues.empty:
                    news_cards = ''.join(news_digest_html(issue) for _, issue in news_issues.head(2).iterrows())
                    st.markdown(
                        '<div class="news-digest"><h3 class="news-digest-heading">새로운 소식</h3>'
                        '<p class="small">개봉·공개·캐스팅·제작 등 작품을 새롭게 이야기할 이유를 모았어요.</p>'
                        '<div class="news-digest-grid">' + news_cards + '</div></div>',
                        unsafe_allow_html=True,
                    )

    with recommendation_tab:
        with st.container(key="curation_section"):
            openai_api_key = get_runtime_secret("OPENAI_API_KEY")
            openai_model = get_runtime_secret("OPENAI_MODEL", "gpt-5.6-luna")
            github_config = get_github_writeback_config()
            persistent_ready = github_writeback_configured(github_config)
            recommendation_runs = weekly_recommendation_runs(
                load_recommendation_runs(THEME_HISTORY_PATH, themes),
                history=load_recommendation_history(THEME_HISTORY_PATH, days=36500),
            )
            selected_run = recommendation_runs[0] if recommendation_runs else {}
            recs = attach_content_candidates(selected_run.get("recommendations", []), selected_run.get("run_id", ""))
            meta = selected_run.get("meta", {})
            content_frame = load_content_candidate_frame()
            current_rows = content_frame[content_frame["run_id"].astype(str).eq(str(selected_run.get("run_id", "")))]
            has_current_contents = not current_rows.empty
            ready = bool(openai_api_key) and persistent_ready and not issues.empty
            description = f"최근 이슈에서 발견한 {len(recs)}개의 테마" if recs else "최근 이슈에서 새로운 테마를 발견하세요."

            with st.container(key="curation_heading"):
                title_column, action_column = st.columns([1, 2], gap="small", vertical_alignment="center")
                with title_column:
                    render_section_heading(2, '이번 주 추천 테마', description)
                with action_column:
                    with st.container(key="curation_actions"):
                        create_action, content_action, library_action, settings_action = st.columns([1, 1, 1, .23], gap="small")
                        with create_action:
                            generate_clicked = st.button(
                                "테마 생성하기", key="generate_weekly_themes", type="primary",
                                use_container_width=True, disabled=not ready,
                                help="최근 이슈로 테마를 만들고 추천 작품과 포스터까지 함께 채웁니다.",
                            )
                        with content_action:
                            content_clicked = st.button(
                                "콘텐츠 새로고침" if has_current_contents else "콘텐츠 채우기",
                                key="load_weekly_contents", use_container_width=True,
                                disabled=not bool(recs and openai_api_key and persistent_ready),
                                help="현재 추천된 모든 테마의 작품 후보와 포스터를 불러옵니다.",
                            )
                        with library_action:
                            if st.button("테마 라이브러리", key="open_theme_library", use_container_width=True):
                                go_page("theme_db")
                                st.rerun()
                        with settings_action:
                            with st.popover("⚙", help="생성 개수와 연결 설정", use_container_width=True):
                                top_n = st.select_slider("생성할 테마 수", options=[5, 10, 15, 20, 25, 30], value=10)
                                if not openai_api_key:
                                    st.warning("테마를 생성하려면 OpenAI 연결 설정이 필요합니다.")
                                if not persistent_ready:
                                    st.warning("생성한 테마를 저장하려면 GitHub 연결 설정이 필요합니다.")
                                if issues.empty:
                                    st.info("선정 가능한 최근 이슈가 없습니다. 이슈 수집 상태를 확인해 주세요.")
                                st.caption("테마를 생성하면 추천 작품과 포스터도 함께 준비합니다. 콘텐츠 버튼으로 작품 후보만 다시 불러올 수 있습니다.")

            if generate_clicked:
                try:
                    with st.spinner("최근 이슈로 테마를 만들고 저장하는 중입니다..."):
                        issue_records = build_llm_issue_records(issues, max_issues=12)
                        recent_history = load_recommendation_history(THEME_HISTORY_PATH, days=56)
                        recs, run_meta = generate_weekly_themes(
                            issue_records=issue_records,
                            themes=themes,
                            top_n=top_n,
                            api_key=openai_api_key,
                            model=openai_model,
                            recent_history=recent_history,
                        )
                        updated_themes, updated_history, added_rows = persist_recommendations_locally(
                            recommendations=recs,
                            themes=themes,
                            theme_path=get_active_theme_path(),
                            history_path=THEME_HISTORY_PATH,
                            run_meta=run_meta,
                        )
                        github_result = {"status": "not_configured"}
                        if github_writeback_configured(github_config):
                            try:
                                github_result = persist_files_to_github(
                                    theme_path=get_active_theme_path(),
                                    history_path=THEME_HISTORY_PATH,
                                    config=github_config,
                                    run_id=str(run_meta.get("run_id", "")),
                                )
                            except Exception as github_exc:
                                github_result = {"status": "error", "error": str(github_exc)}

                        st.session_state["recs"] = recs
                        st.session_state["generation_meta"] = run_meta
                        st.session_state["last_generated_run_id"] = str(run_meta.get("run_id", ""))
                        st.session_state["generation_added_count"] = len(added_rows)
                        st.session_state["github_writeback_result"] = github_result
                        st.session_state["updated_theme_csv"] = updated_themes.to_csv(
                            sep="|", index=False, encoding="utf-8-sig"
                        ).encode("utf-8-sig")
                    with st.spinner("테마에 맞는 추천 작품과 포스터를 채우는 중입니다..."):
                        fill_weekly_content_candidates(
                            recs,
                            run_id=str(run_meta.get("run_id", "")),
                            api_key=openai_api_key,
                            model=openai_model,
                            github_config=github_config,
                        )
                    st.rerun()
                except ThemeGenerationError as exc:
                    st.error(str(exc))
                except Exception as exc:
                    st.exception(exc)
            if content_clicked:
                with st.spinner("추천 작품과 포스터를 다시 채우는 중입니다..."):
                    fill_weekly_content_candidates(
                        recs,
                        run_id=str(selected_run.get("run_id", "")),
                        api_key=openai_api_key,
                        model=openai_model,
                        github_config=github_config,
                    )
                st.rerun()

            if not recs:
                st.info("테마 생성하기를 누르면 최근 이슈에 맞는 추천 테마가 이곳에 표시됩니다.")
            else:
                if selected_run.get("run_id") == st.session_state.get("last_generated_run_id"):
                    github_result = st.session_state.get("github_writeback_result", {"status": "restored"})
                    added_count = int(st.session_state.get("generation_added_count", meta.get("new_count", 0)))
                else:
                    github_result = {"status": "restored"}
                    added_count = int(meta.get("new_count", 0))
                if github_result.get("status") == "error":
                    st.warning(f"테마 저장 실패: {github_result.get('error', '')}")
                current_content_run = st.session_state.get("content_candidate_run_id") == str(selected_run.get("run_id", ""))
                content_meta = st.session_state.get("content_candidate_meta", {}) if current_content_run else {}
                content_error = st.session_state.get("content_candidate_error", "") if current_content_run else ""
                if content_error:
                    st.warning("추천 작품을 채우지 못했어요. 테마는 저장되어 있습니다. 콘텐츠 버튼으로 다시 시도해 주세요.")
                if content_meta.get("github_status") == "error":
                    st.warning(f"콘텐츠 후보 저장 실패: {content_meta.get('github_error', '')}")
                with st.expander("생성 기록", expanded=False):
                    render_generation_status(meta, added_count, github_result)
                    if content_error:
                        st.caption(f"콘텐츠 연결 오류: {content_error}")
                    if has_current_contents:
                        verified_count = int(current_rows["kinolights_status"].astype(str).eq("verified").sum())
                        poster_count = int(current_rows["poster_url"].astype(str).str.startswith(("https://", "http://")).sum())
                        st.caption(f"콘텐츠 후보 {len(current_rows)}편 · 작품 정보 확인 {verified_count}편 · 포스터 연결 {poster_count}편")
                    elif content_meta:
                        st.caption(f"작품 정보 확인 {content_meta.get('verified_count', 0)}편 · 제목 검색 링크 {content_meta.get('search_link_only_count', 0)}편")
                for index, rec in enumerate(recs, start=1):
                    rec["recommended_at"] = selected_run.get("recommended_at", "")
                    render_theme_card(index, rec, assessed_issues)

    with ott_tab:
        render_ott_page(load_snapshot(OTT_SNAPSHOT_PATH), on_refresh=refresh_ott_public_homes)

    with director_tab:
        render_ai_director_page(
            themes=themes, contents=contents,
            issue_records=[],
            api_key=openai_api_key, model=openai_model, github_config=github_config,
            theme_path=get_active_theme_path(), history_path=THEME_HISTORY_PATH,
            content_path=THEME_CONTENT_PATH, existing_candidates=load_content_candidate_frame(),
            find_saved_themes=lambda intent: rank_themes_by_ai_intent(themes, intent).head(5).fillna("").to_dict("records"),
        )

    with media_tab:
        media_github_config = dict(github_config)
        media_github_config["media_insights_path"] = get_runtime_secret("MEDIA_INSIGHTS_GITHUB_PATH", "media_insights_snapshot.json")
        render_media_insights_tab(api_key=openai_api_key, model=openai_model,
                                  github_config=media_github_config)

with st.container(key="app_footer"):
    footer_brand, footer_cost = st.columns([3, 2], vertical_alignment="center", gap="small")
    with footer_brand:
        st.markdown(
            f'<footer class="magazine-footer" data-release="api-cost-v1" data-version="{html.escape(APP_VERSION, quote=True)}">'
            f'<span>B tv+ · THEME CURATOR · v{html.escape(APP_VERSION)}</span></footer>',
            unsafe_allow_html=True,
        )
    with footer_cost:
        render_api_cost_control(get_runtime_secret)
