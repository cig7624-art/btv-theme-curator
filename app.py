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
h1,h2,h3,p,label,div,span,li {
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
    margin-bottom:18px;
}
.rank {
    color:#38bdf8 !important;
    font-size:26px;
    font-weight:900;
}
.theme-name {
    font-size:25px;
    font-weight:900;
}
.copy {
    color:#f97316 !important;
    font-weight:900;
    font-size:16px;
}
.small {
    color:#94a3b8 !important;
    font-size:13px;
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
[data-baseweb="select"] * {
    color:#111827 !important;
}
input, textarea {
    color:#111827 !important;
}
</style>
""", unsafe_allow_html=True)

st.markdown("<h1>🧠 B tv+ AI Theme Curator</h1>", unsafe_allow_html=True)
st.caption("지난주 외부 콘텐츠 이슈를 기반으로 이번주 노출할 B tv+ 테마를 추천합니다.")

ISSUE_PATH = Path("issue_feed.csv")
THEME_PATH = Path("theme_pool.csv")
CONTENT_PATH = Path("content_db.csv")

@st.cache_data
def load_data():
    issues = pd.read_csv(ISSUE_PATH, sep="|").fillna("")
    themes = pd.read_csv(THEME_PATH, sep="|").fillna("")
    contents = pd.read_csv(CONTENT_PATH, sep="|").fillna("")
    return issues, themes, contents

def split_keywords(text):
    if pd.isna(text):
        return []

    return [
        t.strip()
        for t in str(text).replace("/", ",").split(",")
        if t.strip()
    ]

def keyword_score(a_keywords, b_keywords):
    a_set = set(a_keywords)
    b_set = set(b_keywords)
    return len(a_set.intersection(b_set))

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

    return sorted(matched, key=lambda x: x["score"], reverse=True)[:3]

def find_contents(theme, contents, limit=12):
    theme_keywords = split_keywords(theme["trigger_keywords"])
    results = []

    for _, content in contents.iterrows():
        tag_keywords = split_keywords(content.get("tags", ""))
        genre_keywords = split_keywords(content.get("genre", ""))

        score = 0
        score += keyword_score(tag_keywords, theme_keywords) * 2
        score += keyword_score(genre_keywords, theme_keywords)

        if score > 0:
            results.append({
                "title": content["title"],
                "type": content["type"],
                "genre": content["genre"],
                "year": content["year"],
                "score": score
            })

    return sorted(results, key=lambda x: x["score"], reverse=True)[:limit]

def build_theme_recommendations(issues, themes, contents, top_n=5):
    recs = []

    for _, theme in themes.iterrows():
        matched_issues = find_matched_issues(theme, issues)
        matched_contents = find_contents(theme, contents)

        if not matched_issues or not matched_contents:
            continue

        issue_score = sum(i["score"] for i in matched_issues)
        content_score = sum(c["score"] for c in matched_contents[:8])

        total_score = issue_score * 3 + content_score

        recs.append({
            "theme": theme,
            "issues": matched_issues,
            "contents": matched_contents,
            "score": total_score
        })

    return sorted(recs, key=lambda x: x["score"], reverse=True)[:top_n]

try:
    issues, themes, contents = load_data()
except Exception as e:
    st.error(f"CSV 로드 실패: {e}")
    st.stop()

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown(f"""
    <div class="card">
        <div class="small">지난주 외부 이슈</div>
        <div class="rank">{len(issues)}</div>
    </div>
    """, unsafe_allow_html=True)

with col2:
    st.markdown(f"""
    <div class="card">
        <div class="small">테마 후보 풀</div>
        <div class="rank">{len(themes)}</div>
    </div>
    """, unsafe_allow_html=True)

with col3:
    st.markdown(f"""
    <div class="card">
        <div class="small">콘텐츠 DB</div>
        <div class="rank">{len(contents)}</div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

left, right = st.columns([1.05, 2])

with left:
    st.subheader("지난주 수집 이슈")

    for _, issue in issues.iterrows():
        st.markdown(f"""
        <div class="card">
            <b>{issue['issue_title']}</b><br>
            <span class="small">{issue['source']} · {issue['related_content']}</span><br>
            <span class="small">{issue['description']}</span>
        </div>
        """, unsafe_allow_html=True)

with right:
    st.subheader("이번주 추천 테마 TOP5")

    if st.button("🔄 이번주 테마 추천 생성", use_container_width=True):
        st.session_state["recs"] = build_theme_recommendations(
            issues,
            themes,
            contents,
            top_n=5
        )

    if "recs" not in st.session_state:
        st.info("버튼을 누르면 지난주 이슈 기반 추천 테마가 생성됩니다.")
    else:
        recs = st.session_state["recs"]

        if not recs:
            st.warning("추천 결과가 없습니다. issue/theme/content 키워드를 확인하세요.")
        else:
            for idx, rec in enumerate(recs, start=1):
                theme = rec["theme"]
                matched_issues = rec["issues"]
                matched_contents = rec["contents"]

                issue_html = ""
                for issue in matched_issues:
                    issue_html += f"""
                    <li>
                        <b>{issue['source']}</b> · {issue['issue_title']}<br>
                        <span class="small">{issue['description']}</span>
                    </li>
                    """

                content_html = ""
                for c in matched_contents:
                    content_html += f"""
                    <span class="tag">
                        {c['title']} · {c['type']} · {c['year']}
                    </span>
                    """

                st.markdown(f"""
                <div class="theme-card">
                    <div class="rank">#{idx}</div>
                    <div class="theme-name">{theme['theme_name']}</div>
                    <div class="copy">노출 카피: {theme['copy']}</div>
                    <div class="small">
                        추천 점수: <span class="score">{rec['score']}</span>
                    </div>

                    <br>
                    <b>선정 근거</b>
                    <ul>
                        {issue_html}
                    </ul>

                    <b>추천 콘텐츠 후보</b><br>
                    {content_html}
                </div>
                """, unsafe_allow_html=True)
