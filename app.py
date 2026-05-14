import streamlit as st
import pandas as pd
from pathlib import Path

st.set_page_config(
    page_title="B tv+ AI Theme Curator",
    page_icon="🧠",
    layout="wide"
)

st.markdown("""
<style>
.stApp {
    background:#090d1a;
}
.block-container {
    max-width:1700px;
    padding-top:1.2rem;
}
h1,h2,h3,p,label,div,span,li,b {
    color:#f8fafc !important;
}
.card {
    background:#0f172a;
    border:1px solid #1e293b;
    border-radius:16px;
    padding:16px 18px;
    margin-bottom:14px;
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
    font-size:24px;
    font-weight:900;
    margin-bottom:6px;
}
.copy {
    color:#f97316 !important;
    font-weight:900;
    font-size:16px;
    margin-bottom:6px;
}
.small {
    color:#94a3b8 !important;
    font-size:13px;
    line-height:1.5;
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
.stButton button {
    background:#2563eb;
    color:white;
    border-radius:12px;
    border:0;
    font-weight:800;
}
[data-baseweb="select"] * {
    color:#111827 !important;
}
input, textarea {
    color:#111827 !important;
}
</style>
""", unsafe_allow_html=True)

ISSUE_PATH = Path("issue_feed.csv")
THEME_DB_PATH = Path("theme_db.csv")
OLD_THEME_PATH = Path("theme_pool.csv")
CONTENT_DB_PATH = Path("content_db.csv")

def go_page(page):
    st.session_state["page"] = page

if "page" not in st.session_state:
    st.session_state["page"] = "home"

@st.cache_data
def load_data():
    issues = pd.read_csv(ISSUE_PATH, sep="|").fillna("")

    if THEME_DB_PATH.exists():
        themes = pd.read_csv(THEME_DB_PATH, sep="|").fillna("")
    elif OLD_THEME_PATH.exists():
        themes = pd.read_csv(OLD_THEME_PATH, sep="|").fillna("")
    else:
        raise FileNotFoundError("theme_db.csv 또는 theme_pool.csv 파일이 없습니다.")

    contents = pd.read_csv(CONTENT_DB_PATH, sep="|").fillna("")

    return issues, themes, contents

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

def find_matched_issues(theme, issues):
    theme_keywords = split_keywords(theme["trigger_keywords"])
    matched = []

    for _, issue in issues.iterrows():
        issue_keywords = split_keywords(issue["keywords"])
        score = keyword_score(issue_keywords, theme_keywords)

        if score > 0:
            matched.append({
                "source": issue["source"],
                "issue_title": issue["issue_title"],
                "related_content": issue["related_content"],
                "description": issue["description"],
                "score": score
            })

    return sorted(matched, key=lambda x: x["score"], reverse=True)

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

def build_theme_recommendations(issues, themes, contents, top_n=20, content_limit=12):
    recs = []

    for _, theme in themes.iterrows():
        matched_issues = find_matched_issues(theme, issues)
        matched_contents = find_matched_contents(theme, contents, limit=content_limit)

        if not matched_issues:
            continue

        issue_score = sum(i["score"] for i in matched_issues[:5])
        source_diversity = len(set(i["source"] for i in matched_issues))
        related_content_bonus = sum(
            1 for i in matched_issues
            if str(i["related_content"]).strip()
        )
        content_score = sum(c["score"] for c in matched_contents[:content_limit])

        total_score = (
            issue_score * 10
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
            "source_diversity": source_diversity
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

def render_issue_card(issue):
    st.markdown(
        f'''
        <div class="card">
            <b>{issue["issue_title"]}</b><br>
            <span class="small">
                {issue["source"]} · {issue["related_content"]}
            </span><br>
            <span class="small">
                {issue["description"]}
            </span>
        </div>
        ''',
        unsafe_allow_html=True
    )

def render_content_tags(matched_contents):
    if not matched_contents:
        return '<span class="small">매칭 콘텐츠 없음</span>'

    content_tags = ""

    for c in matched_contents:
        content_tags += (
            f'<span class="tag">'
            f'{c["title"]} · {c["type"]} · {c["year"]}'
            f'</span>'
        )

    return content_tags

def render_theme_card(idx, rec):
    theme = rec["theme"]
    matched_issues = rec["issues"]
    matched_contents = rec["contents"]

    keyword_tags = ""
    for kw in split_keywords(theme["trigger_keywords"])[:12]:
        keyword_tags += f'<span class="tag">{kw}</span>'

    issue_blocks = ""
    for issue in matched_issues:
        issue_blocks += (
            '<div class="issue-item">'
            f'<b>{issue["source"]}</b> · {issue["issue_title"]}<br>'
            f'<span class="small">{issue["description"]}</span>'
            '</div>'
        )

    content_tags = render_content_tags(matched_contents)

    html = (
        '<div class="theme-card">'
        f'<div class="rank">#{idx}</div>'
        f'<div class="theme-name">{theme["theme_name"]}</div>'
        f'<div class="copy">노출명/카피: {theme["copy"]}</div>'
        f'<div class="small">추천 점수: <span class="score">{rec["score"]}</span> · '
        f'매칭 이슈 {rec["matched_count"]}개 · 출처 {rec["source_diversity"]}종 · '
        f'콘텐츠 후보 {len(matched_contents)}개</div>'
        '<div class="section-label">매칭 키워드</div>'
        f'{keyword_tags}'
        '<div class="section-label">선정 근거</div>'
        f'{issue_blocks}'
        '<div class="section-label">추천 콘텐츠 후보</div>'
        f'{content_tags}'
        '</div>'
    )

    st.markdown(html, unsafe_allow_html=True)

try:
    issues, themes, contents = load_data()
except Exception as e:
    st.error(f"CSV 로드 실패: {e}")
    st.stop()

if st.session_state["page"] == "theme_db":
    st.markdown("<h1>📚 테마 DB 전체 보기</h1>", unsafe_allow_html=True)
    st.caption("전체 테마 풀을 확인하고, 각 테마에 자동 매칭되는 콘텐츠 후보를 미리 볼 수 있습니다.")

    if st.button("← 추천 화면으로 돌아가기"):
        go_page("home")
        st.rerun()

    st.markdown("---")

    search = st.text_input(
        "테마명/키워드 검색",
        placeholder="예: 스릴러, 비오는날, 로맨스, 쇼츠"
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
        s = search.strip()
        filtered = filtered[
            filtered["theme_name"].astype(str).str.contains(s, case=False, na=False)
            | filtered["trigger_keywords"].astype(str).str.contains(s, case=False, na=False)
            | filtered["genre"].astype(str).str.contains(s, case=False, na=False)
            | filtered["mood"].astype(str).str.contains(s, case=False, na=False)
        ]

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

        st.markdown(
            f'''
            <div class="theme-card">
                <div class="small">{row["theme_id"]}</div>
                <div class="theme-name">{row["theme_name"]}</div>
                <div class="copy">노출명/카피: {row["copy"]}</div>
                <div class="small">장르: {row["genre"]} · 무드: {row["mood"]}</div>

                <div class="section-label">테마 키워드</div>
                <div>{keyword_tags}</div>

                <div class="section-label">추천 콘텐츠 후보</div>
                <div>{content_tags}</div>
            </div>
            ''',
            unsafe_allow_html=True
        )

else:
    st.markdown("<h1>🧠 B tv+ AI Theme Curator</h1>", unsafe_allow_html=True)
    st.caption("지난주 외부 콘텐츠 이슈를 기반으로 이번주 노출할 테마와 콘텐츠 후보를 추천합니다.")

    col1, col2 = st.columns(2)

    with col1:
        render_metric(
            "지난주 외부 이슈",
            len(issues),
            "이번주 테마 추천의 근거 데이터"
        )

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

    left, right = st.columns([1.05, 2])

    with left:
        st.subheader("지난주 수집 이슈")

        for _, issue in issues.iterrows():
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
            st.info("버튼을 누르면 지난주 이슈와 가장 밀접한 테마와 콘텐츠 후보가 생성됩니다.")
        else:
            recs = st.session_state["recs"]

            if not recs:
                st.warning("추천 결과가 없습니다. issue/theme/content 키워드를 확인하세요.")
            else:
                for idx, rec in enumerate(recs, start=1):
                    render_theme_card(idx, rec)
