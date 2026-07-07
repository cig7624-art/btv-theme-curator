import streamlit as st
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
except Exception:
    TfidfVectorizer = None
    cosine_similarity = None


st.set_page_config(
    page_title="B tv+ AI Theme Curator",
    page_icon="🧠",
    layout="wide"
)

st.markdown("""
<style>
.stApp { background:#090d1a; }
.block-container { max-width:1800px; padding-top:1.2rem; }
h1,h2,h3,p,label,div,span,li,b,a { color:#f8fafc !important; }

.card {
    background:#0f172a;
    border:1px solid #1e293b;
    border-radius:16px;
    padding:16px 18px;
    margin-bottom:14px;
}

.logic-card {
    background:#111827;
    border:1px solid #334155;
    border-radius:18px;
    padding:18px 20px;
    margin-bottom:18px;
}

.theme-card {
    background:#111827;
    border:1px solid #334155;
    border-radius:18px;
    padding:18px 20px;
    margin-bottom:16px;
}

.rank {
    color:#38bdf8 !important;
    font-size:26px;
    font-weight:900;
}

.theme-name {
    font-size:23px;
    font-weight:900;
    margin-bottom:6px;
}

.copy {
    color:#f97316 !important;
    font-weight:900;
    font-size:15px;
    margin-bottom:6px;
}

.small {
    color:#94a3b8 !important;
    font-size:13px;
    line-height:1.5;
}

.logic-desc {
    color:#cbd5e1 !important;
    font-size:13px;
    line-height:1.6;
}

.tag {
    display:inline-block;
    background:#1e293b;
    border:1px solid #475569;
    border-radius:999px;
    padding:5px 10px;
    margin-right:6px;
    margin-top:6px;
    font-size:12px;
}

.score {
    color:#22c55e !important;
    font-weight:900;
}

.issue-item {
    background:#0f172a;
    border:1px solid #1f2937;
    border-radius:12px;
    padding:10px 12px;
    margin-bottom:8px;
}

.section-label {
    margin-top:14px;
    margin-bottom:8px;
    font-weight:900;
    font-size:15px;
}

.one-line-reason {
    background:#0f172a;
    border-left:4px solid #38bdf8;
    padding:10px 12px;
    border-radius:8px;
    margin-top:10px;
    margin-bottom:10px;
    color:#cbd5e1 !important;
    font-size:13px;
    line-height:1.5;
}

.source-link {
    display:inline-block;
    margin-top:8px;
    color:#38bdf8 !important;
    font-size:13px;
    font-weight:800;
    text-decoration:none;
}

.source-link:hover { text-decoration:underline; }

.stButton button {
    background:#2563eb;
    color:white;
    border-radius:12px;
    border:0;
    font-weight:800;
}

/* select input */
[data-baseweb="select"] * { color:#111827 !important; }

/* dropdown menu */
[data-baseweb="popover"] * { color:#111827 !important; }
[data-baseweb="menu"] * { color:#111827 !important; }
[data-baseweb="option"] * { color:#111827 !important; }

input, textarea { color:#111827 !important; }
</style>
""", unsafe_allow_html=True)


ISSUE_PATH = Path("issue_feed.csv")
THEME_DB_PATH = Path("theme_db.csv")
OLD_THEME_PATH = Path("theme_pool.csv")
CONTENT_DB_PATH = Path("content_db.csv")


SOURCE_WEIGHTS = {
    "유튜브": 35,
    "SNS/숏폼": 28,
    "극장/박스오피스": 22,
    "OTT/랭킹": 18,
    "네이버 이슈": 15,
    "뉴스/공식자료": 10,
}


def go_page(page):
    st.session_state["page"] = page


if "page" not in st.session_state:
    st.session_state["page"] = "home"


def load_data():
    issues = pd.read_csv(ISSUE_PATH, sep="|").fillna("")

    if THEME_DB_PATH.exists():
        themes = pd.read_csv(THEME_DB_PATH, sep="|").fillna("")
    elif OLD_THEME_PATH.exists():
        themes = pd.read_csv(OLD_THEME_PATH, sep="|").fillna("")
    else:
        raise FileNotFoundError("theme_db.csv 또는 theme_pool.csv 파일이 없습니다.")

    contents = pd.read_csv(CONTENT_DB_PATH, sep="|").fillna("")

    if "source_url" not in issues.columns:
        issues["source_url"] = ""

    return issues, themes, contents


def filter_recent_issues(issues, days=7):
    issues = issues.copy()

    issues["date"] = pd.to_datetime(
        issues["date"],
        errors="coerce"
    )

    today = pd.Timestamp.today().normalize()
    start_date = today - pd.Timedelta(days=days - 1)

    recent = issues[
        (issues["date"] >= start_date)
        & (issues["date"] <= today)
    ].copy()

    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")

    return recent, start_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def classify_source(source):
    s = str(source)

    if "유튜브" in s or "YouTube" in s:
        return "유튜브"
    if "SNS" in s or "인스타" in s or "Instagram" in s or "쇼츠" in s or "숏폼" in s:
        return "SNS/숏폼"
    if "극장" in s or "KOFIC" in s or "박스오피스" in s or "CGV" in s or "롯데시네마" in s or "메가박스" in s:
        return "극장/박스오피스"
    if "OTT" in s or "넷플릭스" in s or "티빙" in s or "웨이브" in s or "디즈니" in s or "쿠팡플레이" in s:
        return "OTT/랭킹"
    if "네이버" in s:
        return "네이버 이슈"

    return "뉴스/공식자료"


def split_keywords(text):
    if pd.isna(text):
        return []

    return [
        t.strip()
        for t in str(text).replace("/", ",").replace(" ", ",").split(",")
        if t.strip()
    ]


def build_theme_search_text(row):
    return " ".join([
        str(row.get("theme_name", "")),
        str(row.get("theme_name", "")),
        str(row.get("copy", "")),
        str(row.get("trigger_keywords", "")),
        str(row.get("trigger_keywords", "")),
        str(row.get("genre", "")),
        str(row.get("mood", "")),
    ])


def semantic_theme_search(themes, query):
    if not query or TfidfVectorizer is None or cosine_similarity is None:
        return themes.iloc[0:0].copy()

    query_text = str(query).lower()

    weak_words = [
        "좋은", "보기", "보면", "볼", "때", "영화", "드라마", "추천",
        "싶은", "생각나는", "같은", "하는", "있는", "없는"
    ]

    must_groups = []

    if any(w in query_text for w in ["공포", "호러", "무서", "귀신", "오컬트", "괴담"]):
        must_groups.append(["공포", "호러", "무서", "귀신", "오컬트", "괴담", "스릴러"])

    if any(w in query_text for w in ["로맨스", "사랑", "연애", "첫사랑", "설렘"]):
        must_groups.append(["로맨스", "사랑", "연애", "첫사랑", "설렘", "멜로", "청춘"])

    if any(w in query_text for w in ["여행", "떠나", "휴가", "바다"]):
        must_groups.append(["여행", "로드무비", "휴가", "바다", "해외", "풍경"])

    if any(w in query_text for w in ["힐링", "위로", "잔잔", "따뜻"]):
        must_groups.append(["힐링", "위로", "잔잔", "따뜻", "감성"])

    
    df = themes.copy()
    df["search_text"] = df.apply(build_theme_search_text, axis=1).fillna("").str.lower()

    # 핵심 장르/무드가 있으면 반드시 포함된 테마만 남김
    for group in must_groups:
        df = df[df["search_text"].apply(lambda x: any(g in x for g in group))].copy()

    if df.empty:
        return themes.iloc[0:0].copy()

    cleaned_query = query_text
    for w in weak_words:
        cleaned_query = cleaned_query.replace(w, " ")

    corpus = df["search_text"].tolist()

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        min_df=1
    )

    theme_matrix = vectorizer.fit_transform(corpus)
    query_vector = vectorizer.transform([cleaned_query])

    scores = cosine_similarity(query_vector, theme_matrix).flatten()

    df["semantic_score"] = scores

    # 핵심어 보너스
    def intent_bonus(text):
        bonus = 0
        if "공포" in query_text or "호러" in query_text or "무서" in query_text:
            if any(x in text for x in ["공포", "호러", "오컬트", "괴담", "귀신", "스릴러"]):
                bonus += 0.25
            if any(x in text for x in ["자녀", "가족", "키즈", "아이"]):
                bonus -= 0.5

        if "여름" in query_text:
            if any(x in text for x in ["여름", "무더위", "한여름", "휴가", "바캉스"]):
                bonus += 0.12

        if "첫사랑" in query_text:
            if any(x in text for x in ["첫사랑", "청춘", "설렘", "로맨스"]):
                bonus += 0.25

        return bonus

    df["semantic_score"] = df["semantic_score"] + df["search_text"].apply(intent_bonus)

    df = df[df["semantic_score"] > 0].copy()
    df = df.sort_values("semantic_score", ascending=False)

    return df.drop(columns=["search_text"])

def safe_url(url):
    url = str(url).strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return ""

def score_issue(issue):
    source_group = classify_source(issue.get("source", ""))
    base = SOURCE_WEIGHTS.get(source_group, 5)

    keywords = split_keywords(issue.get("keywords", ""))
    keyword_bonus = min(len(keywords), 12)

    related_bonus = 8 if str(issue.get("related_content", "")).strip() else 0
    link_bonus = 8 if safe_url(issue.get("source_url", "")) else 0

    desc = str(issue.get("description", ""))
    detail_bonus = min(len(desc) // 35, 8)

    return base + keyword_bonus + related_bonus + link_bonus + detail_bonus


def prepare_issues(issues):
    issues = issues.copy()
    issues["source_group"] = issues["source"].apply(classify_source)
    issues["issue_score"] = issues.apply(score_issue, axis=1)
    return issues.sort_values("issue_score", ascending=False)


def find_matched_issues(theme, issues):
    theme_keywords = split_keywords(theme["trigger_keywords"])
    matched = []

    for _, issue in issues.iterrows():
        issue_keywords = split_keywords(issue["keywords"])
        score = keyword_score(issue_keywords, theme_keywords)

        if score > 0:
            matched.append({
                "date": issue.get("date", ""),
                "source": issue["source"],
                "source_group": issue.get("source_group", classify_source(issue["source"])),
                "issue_title": issue["issue_title"],
                "related_content": issue["related_content"],
                "description": issue["description"],
                "source_url": issue.get("source_url", ""),
                "score": score,
                "issue_score": issue.get("issue_score", 0)
            })

    return sorted(
        matched,
        key=lambda x: (x["score"], x["issue_score"]),
        reverse=True
    )


def find_matched_contents(theme, contents, limit=12):
    theme_keywords = split_keywords(theme["trigger_keywords"])
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
                "title": content["title"],
                "type": content["type"],
                "genre": content["genre"],
                "year": content["year"],
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


def build_theme_recommendations(issues, themes, contents, top_n=20, content_limit=12):
    recs = []

    for _, theme in themes.iterrows():
        matched_issues = find_matched_issues(theme, issues)
        matched_contents = find_matched_contents(theme, contents, limit=content_limit)

        if not matched_issues:
            continue

        issue_score = sum(i["score"] for i in matched_issues[:5])
        issue_quality_score = sum(i.get("issue_score", 0) for i in matched_issues[:3])
        source_diversity = len(set(i["source_group"] for i in matched_issues))
        related_content_bonus = sum(
            1 for i in matched_issues
            if str(i["related_content"]).strip()
        )
        content_score = sum(c["score"] for c in matched_contents[:content_limit])

        total_score = (
            issue_score * 10
            + issue_quality_score
            + source_diversity * 3
            + related_content_bonus * 2
            + content_score
        )

        recs.append({
            "theme": theme,
            "issues": matched_issues[:5],
            "contents": matched_contents,
            "score": total_score,
            "matched_count": len(matched_issues),
            "source_diversity": source_diversity,
            "reason_summary": build_reason_summary(theme, matched_issues)
        })

    return sorted(recs, key=lambda x: x["score"], reverse=True)[:top_n]


def render_metric(title, number, subtitle=None):
    sub_html = f'<div class="small">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f'''
        <div class="card">
            <div class="small">{title}</div>
            <div class="rank">{number}</div>
            {sub_html}
        </div>
        ''',
        unsafe_allow_html=True
    )


def render_source_link(url):
    url = safe_url(url)
    if not url:
        return ""
    return f'<a class="source-link" href="{url}" target="_blank">근거 링크 보기 ↗</a>'


def render_issue_card(issue):
    url_html = render_source_link(issue.get("source_url", ""))
    related = str(issue.get("related_content", "")).strip()
    related_html = f" · {related}" if related else ""
    source_group = issue.get("source_group", classify_source(issue.get("source", "")))
    issue_score = issue.get("issue_score", "")

    html = (
        '<div class="card">'
        f'<span class="tag">{source_group}</span><br>'
        f'<b>{issue["issue_title"]}</b><br>'
        f'<span class="small">{issue["source"]}{related_html}</span><br>'
        f'<span class="small">{issue["description"]}</span><br>'
        f'<span class="small">이슈 점수: <span class="score">{issue_score}</span></span><br>'
        f'{url_html}'
        '</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


def render_content_tags(matched_contents):
    if not matched_contents:
        return '<span class="small">매칭 콘텐츠 없음</span>'

    content_tags = ""
    for c in matched_contents:
        content_tags += (
            f'<span class="tag">{c["title"]} · {c["type"]} · {c["year"]}</span>'
        )
    return content_tags


def render_theme_card(idx, rec):
    theme = rec["theme"]
    matched_contents = rec["contents"]

    keyword_tags = ""
    for kw in split_keywords(theme["trigger_keywords"])[:12]:
        keyword_tags += f'<span class="tag">{kw}</span>'

    content_tags = render_content_tags(matched_contents)

    html = (
        '<div class="theme-card">'
        f'<div class="rank">#{idx}</div>'
        f'<div class="theme-name">{theme["theme_name"]}</div>'
        f'<div class="copy">노출명/카피: {theme["copy"]}</div>'
        f'<div class="small">추천 점수: <span class="score">{rec["score"]}</span> · '
        f'매칭 이슈 {rec["matched_count"]}개 · 출처 {rec["source_diversity"]}종 · '
        f'콘텐츠 후보 {len(matched_contents)}개</div>'
        '<div class="section-label">선정 근거 요약</div>'
        f'<div class="one-line-reason">{rec["reason_summary"]}</div>'
        '<div class="section-label">매칭 키워드</div>'
        f'{keyword_tags}'
        '<div class="section-label">추천 콘텐츠 후보</div>'
        f'{content_tags}'
        '</div>'
    )

    st.markdown(html, unsafe_allow_html=True)


def render_collection_logic():
    html = (
        '<div class="logic-card">'
        '<div class="theme-name">이슈 수집·선정 로직</div>'
        '<div class="logic-desc">'
        '최근 이슈는 오늘 기준 최근 7일 이내의 외부 콘텐츠 신호 중, '
        '실제 시청 전환 가능성이 높은 이슈를 우선 노출합니다. '
        '단순 뉴스량보다 <b>유튜브/숏폼 확산, SNS 반응, 극장 흥행, '
        'OTT 화제성, 뉴스/공식자료 신뢰도</b>를 함께 반영합니다.'
        '</div>'

        '<div class="section-label">경로별 가중치</div>'
        '<span class="tag">유튜브 35</span>'
        '<span class="tag">SNS/숏폼 28</span>'
        '<span class="tag">극장/박스오피스 22</span>'
        '<span class="tag">OTT/랭킹 18</span>'
        '<span class="tag">네이버 이슈 15</span>'
        '<span class="tag">뉴스/공식자료 10</span>'

        '<div class="section-label">이슈 점수 산정 기준</div>'
        '<div class="logic-desc">'
        '이슈 점수 = 경로별 가중치 + 키워드 구체성 + 관련 콘텐츠명 존재 여부 '
        '+ 근거 링크 존재 여부 + 설명 상세도. '
        '메인 화면에는 이 중 점수가 높은 핵심 이슈만 우선 노출합니다.'
        '</div>'
        '</div>'
    )

    st.markdown(html, unsafe_allow_html=True)


try:
    all_issues, themes, contents = load_data()
    recent_issues, issue_start_date, issue_end_date = filter_recent_issues(
        all_issues,
        days=7
    )
    issues = prepare_issues(recent_issues)
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

    render_collection_logic()

    source_options = ["전체"] + list(SOURCE_WEIGHTS.keys())

    c1, c2 = st.columns([1, 2])

    with c1:
        selected_source = st.selectbox(
            "경로별 보기",
            source_options
        )

    with c2:
        search = st.text_input(
            "이슈명/콘텐츠/키워드/출처 검색",
            placeholder="예: 미키17, 살목지, 쇼츠, 요리"
        )

    filtered = issues.copy()

    if selected_source != "전체":
        filtered = filtered[filtered["source_group"] == selected_source]

    if search:
        s = search.strip()
        filtered = filtered[
            filtered["issue_title"].astype(str).str.contains(s, case=False, na=False)
            | filtered["related_content"].astype(str).str.contains(s, case=False, na=False)
            | filtered["keywords"].astype(str).str.contains(s, case=False, na=False)
            | filtered["source"].astype(str).str.contains(s, case=False, na=False)
            | filtered["description"].astype(str).str.contains(s, case=False, na=False)
        ]

    st.markdown(f"### 전체 {len(issues)}개 중 {len(filtered)}개 표시")

    for _, issue in filtered.iterrows():
        keyword_tags = ""
        for kw in split_keywords(issue["keywords"])[:16]:
            keyword_tags += f'<span class="tag">{kw}</span>'

        url_html = render_source_link(issue.get("source_url", ""))

        html = (
            '<div class="theme-card">'
            f'<span class="tag">{issue["source_group"]}</span>'
            f'<div class="small">{issue["date"]} · {issue["source"]}</div>'
            f'<div class="theme-name">{issue["issue_title"]}</div>'
            f'<div class="copy">관련 콘텐츠: {issue["related_content"]}</div>'
            f'<div class="small">{issue["description"]}</div>'
            f'<div class="small">이슈 점수: <span class="score">{issue["issue_score"]}</span></div>'
            '<div class="section-label">이슈 키워드</div>'
            f'{keyword_tags}<br>'
            f'{url_html}'
            '</div>'
        )

        st.markdown(html, unsafe_allow_html=True)


elif st.session_state["page"] == "theme_db":
    st.markdown("<h1>📚 테마 DB 전체 보기</h1>", unsafe_allow_html=True)
    st.caption("전체 테마 풀을 확인하고, 각 테마에 자동 매칭되는 콘텐츠 후보를 미리 볼 수 있습니다.")

    if st.button("← 추천 화면으로 돌아가기"):
        go_page("home")
        st.rerun()

    st.markdown("---")

    search = st.text_input(
        "AI 자연어 테마 검색",
        placeholder="예: 여름에 보면 좋은 공포영화, 첫사랑이 생각나는 영화, 여행 떠나고 싶을 때"
    )

    st.caption(
        "사용자가 자연어로 입력한 상황/무드/장르를 벡터화해, "
        "기존 테마 DB에서 의미가 가까운 테마와 콘텐츠 후보를 탐색합니다."
    )

    content_limit_preview = st.slider(
        "테마별 콘텐츠 미리보기 수",
        min_value=5,
        max_value=20,
        value=12,
        step=1
    )

    filtered = themes.copy()

    if search:
        filtered = semantic_theme_search(themes, search)

        if filtered.empty:
            s = search.strip()
            filtered = themes[
                themes["theme_name"].astype(str).str.contains(s, case=False, na=False)
                | themes["trigger_keywords"].astype(str).str.contains(s, case=False, na=False)
                | themes["genre"].astype(str).str.contains(s, case=False, na=False)
                | themes["mood"].astype(str).str.contains(s, case=False, na=False)
                | themes["copy"].astype(str).str.contains(s, case=False, na=False)
            ].copy()

    st.markdown(f"### 전체 {len(themes)}개 중 {len(filtered)}개 표시")

    for _, row in filtered.iterrows():
        keyword_tags = ""
        for kw in split_keywords(row["trigger_keywords"])[:12]:
            keyword_tags += f'<span class="tag">{kw}</span>'

        matched_contents = find_matched_contents(
            row,
            contents,
            limit=content_limit_preview
        )

        content_tags = render_content_tags(matched_contents)

        score_html = ""
        if "semantic_score" in row:
            try:
                score_html = f'<div class="small">자연어 유사도: <span class="score">{float(row["semantic_score"]):.3f}</span></div>'
            except Exception:
                score_html = ""

        html = (
            '<div class="theme-card">'
            f'<div class="small">{row["theme_id"]}</div>'
            f'<div class="theme-name">{row["theme_name"]}</div>'
            f'<div class="copy">노출명/카피: {row["copy"]}</div>'
            f'<div class="small">장르: {row["genre"]} · 무드: {row["mood"]}</div>'
            f'{score_html}'
            '<div class="section-label">테마 키워드</div>'
            f'{keyword_tags}'
            '<div class="section-label">추천 콘텐츠 후보</div>'
            f'{content_tags}'
            '</div>'
        )

        st.markdown(html, unsafe_allow_html=True)


else:
    st.markdown("<h1>🧠 B tv+ AI Theme Curator</h1>", unsafe_allow_html=True)
    st.caption("최근 외부 콘텐츠 이슈를 기반으로 이번주 노출할 테마와 콘텐츠 후보를 추천합니다.")

    col1, col2 = st.columns(2)

    with col1:
        render_metric(
            "최근 이슈",
            len(issues),
            f"{issue_start_date} ~ {issue_end_date} 기준"
        )

        if st.button("🗂 이슈 전체 보기", use_container_width=True):
            go_page("issue_db")
            st.rerun()

    with col2:
        render_metric(
            "테마 DB",
            len(themes),
            "전체 테마 풀"
        )

        if st.button("📚 테마 DB 전체 보기", use_container_width=True):
            go_page("theme_db")
            st.rerun()

    st.markdown("---")

    left, right = st.columns([1, 1])

    with left:
        st.subheader("최근 핵심 이슈")

        if issues.empty:
            st.warning("최근 기준 수집된 이슈가 없습니다. issue_feed.csv의 date 값을 확인하세요.")
        else:
            main_issues = issues.head(8)

            for _, issue in main_issues.iterrows():
                render_issue_card(issue)

    with right:
        st.subheader("이번주 추천 테마")

        c1, c2 = st.columns(2)

        with c1:
            top_n = st.slider(
                "AI가 뽑을 테마 수",
                min_value=5,
                max_value=30,
                value=20,
                step=5
            )

        with c2:
            content_limit = st.slider(
                "테마당 추천 콘텐츠 수",
                min_value=5,
                max_value=20,
                value=12,
                step=1
            )

        if st.button("🔄 이번주 테마 추천 생성", use_container_width=True):
            st.session_state["recs"] = build_theme_recommendations(
                issues,
                themes,
                contents,
                top_n=top_n,
                content_limit=content_limit
            )

        if "recs" not in st.session_state:
            st.info("버튼을 누르면 최근 이슈와 가장 밀접한 테마와 콘텐츠 후보가 생성됩니다.")
        else:
            recs = st.session_state["recs"]

            if not recs:
                st.warning("추천 결과가 없습니다. issue/theme/content 키워드를 확인하세요.")
            else:
                for idx, rec in enumerate(recs, start=1):
                    render_theme_card(idx, rec)
