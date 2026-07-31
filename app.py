import html
import math
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import streamlit as st

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

.card,.theme-card,.logic-card {
    background:#111827;
    border:1px solid #334155;
    border-radius:18px;
    padding:18px 20px;
    margin-bottom:16px;
}

.issue-card { display:flex; gap:18px; align-items:stretch; min-height:210px; }
.issue-card-body { flex:1 1 auto; min-width:0; }
.issue-media {
    flex:0 0 320px; width:320px; min-height:176px; border-radius:14px;
    overflow:hidden; background:#0b1220; border:1px solid #263449; align-self:stretch;
}
.issue-media img { width:100%; height:100%; min-height:176px; object-fit:cover; display:block; }
.issue-placeholder {
    width:100%; height:100%; min-height:176px; padding:20px; box-sizing:border-box;
    display:flex; flex-direction:column; justify-content:flex-end;
    background:linear-gradient(145deg,#172033,#0b1220 60%,#1e293b);
}
.issue-placeholder strong { font-size:22px; line-height:1.25; }
.issue-placeholder span { color:#94a3b8 !important; font-size:12px; margin-bottom:8px; }
.issue-meta-row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; }

.rank { color:#38bdf8 !important; font-size:26px; font-weight:900; }
.theme-name { font-size:23px; font-weight:900; margin-bottom:6px; }
.copy { color:#f97316 !important; font-weight:900; font-size:15px; margin-bottom:6px; }
.small,.logic-desc { color:#cbd5e1 !important; font-size:13px; line-height:1.5; }
.tag,.weight-chip {
    display:inline-block; background:#1e293b; border:1px solid #475569; border-radius:999px;
    padding:5px 10px; margin-right:6px; margin-top:6px; font-size:12px;
}
.weight-chip-wrap {
    position:relative; display:inline-block; margin-right:6px; margin-top:6px;
}
.weight-chip { position:relative; cursor:help; font-weight:800; margin:0; }
.weight-tooltip {
    display:none; position:absolute; z-index:9999; left:0; top:calc(100% + 8px);
    width:350px; white-space:normal; padding:12px 13px 10px; border-radius:11px;
    background:#020617; border:1px solid #475569; box-shadow:0 12px 34px rgba(0,0,0,.45);
    color:#f8fafc !important; font-size:12px; font-weight:500; line-height:1.55;
}
.weight-chip-wrap:hover .weight-tooltip,
.weight-tooltip:hover { display:block; }
.weight-tooltip::before {
    content:""; position:absolute; left:20px; top:-7px; width:12px; height:12px;
    background:#020617; border-left:1px solid #475569; border-top:1px solid #475569;
    transform:rotate(45deg);
}
.weight-detail-link {
    display:block; margin-top:9px; padding-top:8px; border-top:1px solid #334155;
    color:#38bdf8 !important; font-weight:900; text-align:right; text-decoration:none;
}
.weight-detail-link:hover { text-decoration:underline; }
.logic-detail-box {
    background:#0f172a; border:1px solid #334155; border-radius:12px;
    padding:12px 14px; margin:8px 0 14px;
}
.logic-detail-title { font-size:14px; font-weight:900; margin-bottom:5px; }
.logic-detail-text { color:#cbd5e1 !important; font-size:13px; line-height:1.6; }
.logic-code {
    background:#020617; border:1px solid #334155; border-radius:10px;
    padding:10px 12px; color:#e2e8f0 !important; font-size:12px; line-height:1.6;
    white-space:pre-wrap; overflow-wrap:anywhere;
}
.score { color:#22c55e !important; font-weight:900; }
.section-label { margin-top:14px; margin-bottom:8px; font-weight:900; font-size:15px; }
.one-line-reason {
    background:#0f172a; border-left:4px solid #38bdf8; padding:10px 12px; border-radius:8px;
    margin-top:10px; margin-bottom:10px; color:#cbd5e1 !important; font-size:13px; line-height:1.5;
}
.source-link {
    display:inline-block; margin-top:8px; color:#38bdf8 !important; font-size:13px;
    font-weight:800; text-decoration:none;
}
.source-link:hover { text-decoration:underline; }
.stButton button { background:#2563eb; color:white; border-radius:12px; border:0; font-weight:800; }

/* 닫힌 select와 펼친 드롭다운 옵션 모두 어두운 글자로 고정 */
div[data-baseweb="select"] > div { background:#f8fafc !important; border-color:#cbd5e1 !important; }
div[data-baseweb="select"] *, div[data-baseweb="select"] input,
[role="listbox"], [role="listbox"] *, [role="option"], [role="option"] * {
    color:#0f172a !important; -webkit-text-fill-color:#0f172a !important;
}
[role="listbox"], [role="option"] { background:#ffffff !important; }
[role="option"]:hover, [aria-selected="true"][role="option"] { background:#e2e8f0 !important; }
input, textarea { color:#0f172a !important; -webkit-text-fill-color:#0f172a !important; }

/* Streamlit dialog: 기본 흰색 배경 때문에 흰 글자가 사라지는 문제 방지 */
div[data-testid="stDialog"] div[role="dialog"],
div[role="dialog"][aria-modal="true"] {
    background:#090d1a !important;
    color:#f8fafc !important;
    border:1px solid #334155 !important;
}
div[data-testid="stDialog"] div[role="dialog"] > div,
div[role="dialog"][aria-modal="true"] > div {
    background:#090d1a !important;
}
div[data-testid="stDialog"] h1, div[data-testid="stDialog"] h2,
div[data-testid="stDialog"] h3, div[data-testid="stDialog"] p,
div[data-testid="stDialog"] span, div[data-testid="stDialog"] li,
div[role="dialog"][aria-modal="true"] h1, div[role="dialog"][aria-modal="true"] h2,
div[role="dialog"][aria-modal="true"] h3, div[role="dialog"][aria-modal="true"] p,
div[role="dialog"][aria-modal="true"] span, div[role="dialog"][aria-modal="true"] li {
    color:#f8fafc !important;
    -webkit-text-fill-color:#f8fafc !important;
}
div[data-testid="stDialog"] [data-testid="stCaptionContainer"] p,
div[role="dialog"][aria-modal="true"] [data-testid="stCaptionContainer"] p {
    color:#cbd5e1 !important;
    -webkit-text-fill-color:#cbd5e1 !important;
}
div[data-testid="stDialog"] button, div[role="dialog"][aria-modal="true"] button {
    color:#f8fafc !important;
}

@media (max-width:900px) {
    .issue-card { flex-direction:column; }
    .issue-media { width:100%; flex-basis:190px; }
}
</style>
""", unsafe_allow_html=True)


ISSUE_PATH = Path("issue_feed.csv")
THEME_DB_PATH = Path("theme_db.csv")
OLD_THEME_PATH = Path("theme_pool.csv")
CONTENT_DB_PATH = Path("content_db.csv")

SOURCE_CONFIG = {
    "YouTube 반응": {
        "weight_pct": 30,
        "path_points": 18,
        "tooltip": "한국 인기 영상, 주요 OTT·방송사 공식 채널의 신규 업로드, 리뷰·해석·명장면 검색 결과를 수집하며 조회수·댓글·공개 후 반응 속도를 반영합니다.",
    },
    "극장·박스오피스": {
        "weight_pct": 25,
        "path_points": 15,
        "tooltip": "KOBIS 일별 박스오피스의 순위, 신규 진입, 순위 변화, 일일·누적 관객 수를 반영합니다. 관련 기사는 흥행 배경을 설명하는 보조자료로 사용합니다.",
    },
    "OTT 랭킹·신작": {
        "weight_pct": 25,
        "path_points": 15,
        "tooltip": "Netflix 공식 국가별 Top 10 데이터에서 한국 순위를 자동 수집하고, 티빙·웨이브·디즈니+·쿠팡플레이는 공식 신작·공개 예정 기사와 YouTube 공식 채널 신호를 반영합니다.",
    },
    "온라인 화제성": {
        "weight_pct": 15,
        "path_points": 9,
        "tooltip": "네이버 데이터랩 검색 관심도 변화와 관련 뉴스 언급량, YouTube 반응을 함께 확인해 실제 온라인 관심 상승 여부를 검증합니다.",
    },
    "뉴스·공식자료": {
        "weight_pct": 5,
        "path_points": 3,
        "tooltip": "콘텐츠 관련 뉴스와 플랫폼·방송사·제작사의 공식 발표를 수집해 공개, 캐스팅, 수상, 편성 변경 등 이슈의 원인과 배경을 설명합니다.",
    },
}

SOURCE_WEIGHTS = {name: info["weight_pct"] for name, info in SOURCE_CONFIG.items()}


SOURCE_DETAIL_LOGIC = {
    "youtube": {
        "source_name": "YouTube 반응",
        "weight": "30% · 경로 점수 최대 18점",
        "purpose": "공개 직후 반응이 큰 콘텐츠 영상과 공식 채널 신규 업로드를 발견합니다.",
        "integration": "YouTube Data API v3를 사용합니다. 검색어 검색, 한국 인기 영상, 지정 공식 채널 업로드를 API로 조회합니다.",
        "collection": [
            "최근 7일 기준 12개 검색어별 최대 8개 영상 수집",
            "검색어 없이 YouTube 한국 인기 영상(mostPopular) 최대 50개 수집 후 콘텐츠 관련 영상만 유지",
            "넷플릭스·티빙·웨이브·디즈니+·쿠팡플레이와 주요 방송사 11개 공식 채널의 최근 업로드 수집",
        ],
        "filters": [
            "일반 검색·공식 채널 영상은 최근 7일 이내만 후보로 사용",
            "한국 인기 영상은 인기 목록 신호가 있으므로 업로드일 제한을 별도로 적용하지 않음",
            "조회수 10만 이상, 일평균 조회수 2만 이상, 댓글 200개 이상 중 하나를 충족하면 후보",
            "공식 채널 영상은 조회수 3만 이상 또는 댓글 50개 이상이면 후보",
            "영상 제목에서 신뢰할 수 있는 작품명이 추출된 경우만 이슈로 등록",
            "같은 작품은 YouTube 피드에서 1건만 남기고 신호가 강한 순으로 최대 15건 저장",
        ],
        "ranking": "YouTube 내부 후보 순서는 일평균 조회수 + 댓글×250 + 한국 인기 영상 보너스 30,000 + 공식 채널 보너스 15,000으로 계산합니다.",
        "score": "핵심 이슈 점수에서는 조회수와 댓글을 로그 스케일로 환산해 반응 강도에 반영합니다. 같은 작품이 다른 경로에서도 발견되면 경로 점수와 교차 확인 점수가 추가됩니다.",
        "limitations": "YouTube 전체 급상승 순위를 직접 제공받는 구조는 아닙니다. 12개 검색어, 한국 인기 영상, 지정 공식 채널 범위에서 발견한 후보만 대상으로 합니다.",
        "queries": [
            "한국 드라마 리뷰", "한국 영화 리뷰", "한국 예능 리뷰", "드라마 결말 해석",
            "영화 결말 해석", "드라마 몰아보기", "영화 요약", "드라마 명장면 쇼츠",
            "예능 명장면 쇼츠", "영화 명장면 쇼츠", "OTT 신작 반응", "배우 신작 인터뷰",
        ],
        "channels": [
            "Netflix Korea", "TVING", "wavve", "Disney Plus Korea", "Coupang Play",
            "SBS Drama", "MBC Drama", "KBS Drama", "tvN D ENT", "JTBC Drama", "ENA",
        ],
    },
    "boxoffice": {
        "source_name": "극장·박스오피스",
        "weight": "25% · 경로 점수 최대 15점",
        "purpose": "기사 제목이 아니라 KOBIS의 실제 극장 관객 데이터를 수집합니다.",
        "integration": "KOBIS OpenAPI를 사용합니다. GitHub Secret의 KOBIS_API_KEY로 전일 일별 박스오피스 데이터를 요청합니다.",
        "collection": [
            "매일 전일 기준 KOBIS 일별 박스오피스 API 호출",
            "KOBIS가 제공하는 일별 Top 10 전 작품 수집",
            "순위, 전일 대비 순위 변화, 신규 진입 여부, 일일 관객, 누적 관객, 매출 점유율 저장",
        ],
        "filters": [
            "영화명이 비어 있는 행만 제외하고 Top 10을 모두 등록",
            "신규 진입이면 '신규 진입', 1위면 '일일 1위', 2계단 이상 상승이면 '순위 상승' 문구로 강조",
            "관련 뉴스는 박스오피스 수치가 아니라 흥행 원인을 설명하는 별도 보조 피드로 취급",
        ],
        "ranking": "KOBIS 순위를 그대로 수집하며 별도의 내부 후보 컷은 적용하지 않습니다.",
        "score": "반응 강도는 일일 관객 수를 로그 스케일로 환산하며, 1위·신규 진입 등 강한 신호가 있으면 추가 점수를 부여합니다.",
        "limitations": "현재 대표 이미지는 KOBIS API에서 제공되지 않습니다. 같은 작품으로 묶인 YouTube·뉴스 피드에 이미지가 있으면 그 이미지를 카드에 사용합니다.",
    },
    "ott": {
        "source_name": "OTT 랭킹·신작",
        "weight": "25% · 경로 점수 최대 15점",
        "purpose": "Netflix 실제 순위와 주요 플랫폼의 신규 공개·공개 예정 신호를 함께 확인합니다.",
        "integration": "Netflix 순위는 API가 아니라 Netflix가 공개하는 국가별 주간 Top 10 엑셀 파일을 자동 다운로드해 읽습니다. 다른 OTT의 신작 정보는 Google News RSS와 YouTube 공식 채널 데이터로 수집합니다.",
        "collection": [
            "Netflix 공식 국가별 주간 Top 10 엑셀 파일을 매 실행 시 자동 다운로드",
            "파일에서 한국 최신 집계 주간을 찾고 순위 1~10위 작품을 모두 등록",
            "Google News RSS에서 넷플릭스·티빙·웨이브·디즈니+·쿠팡플레이 신작·공개 예정 관련 기사 수집",
        ],
        "filters": [
            "Netflix는 최신 주간의 한국 Top 10만 사용",
            "OTT 신작 기사는 최근 7일 이내, 검색어별 최대 12개 기사 후보 수집",
            "동일 작품은 이후 핵심 이슈 묶음 단계에서 하나의 이슈로 통합",
        ],
        "ranking": "Netflix의 공식 주간 순위를 그대로 사용합니다. 신작 관련 기사는 자체 순위가 아니라 보조 피드입니다.",
        "score": "Top 10 순위가 확인되면 반응 강도 기본점수가 반영되고, 신작·공개 예정·신규 진입 신호가 있으면 추가됩니다.",
        "limitations": "현재 자동 수집되는 실제 순위는 Netflix 한국 Top 10뿐입니다. 티빙·웨이브·디즈니+·쿠팡플레이의 실제 순위 API는 연동되어 있지 않으며, 공식 신작 관련 기사와 YouTube 공식 채널 신규 업로드만 사용합니다.",
    },
    "buzz": {
        "source_name": "온라인 화제성",
        "weight": "15% · 경로 점수 최대 9점",
        "purpose": "다른 경로에서 먼저 발견한 작품이 실제 검색 관심도 상승을 보이는지 검증합니다.",
        "integration": "네이버 데이터랩 검색어 트렌드 API를 사용합니다. NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET이 없으면 이 경로는 건너뜁니다.",
        "collection": [
            "YouTube·KOBIS·Netflix·뉴스에서 발견된 작품 중 작품명이 신뢰 가능한 후보를 선정",
            "서로 다른 출처 수와 피드 수가 많은 순으로 최대 25개 후보 선정",
            "네이버 데이터랩에서 최근 14일 일별 검색 관심도 조회",
        ],
        "filters": [
            "직전 7일 평균과 최근 7일 평균을 비교",
            "최근 평균이 직전 평균보다 20% 이상 상승해야 등록",
            "최근 7일 관심도 평균이 5 이상이어야 등록",
            "상승률이 높은 순으로 최대 10개 저장",
        ],
        "ranking": "상승률이 큰 작품 순으로 온라인 화제성 피드를 정렬합니다.",
        "score": "현재 온라인 화제성 경로 자체 점수와 교차 확인 점수에 반영됩니다. 이 경로는 새로운 작품을 단독 발견하기보다 기존 이슈를 검증하는 역할입니다.",
        "limitations": "네이버 데이터랩은 절대 검색량이 아니라 조회 기간 내 상대 지수입니다. 작품명이 모호하거나 동명이작이면 값이 왜곡될 수 있습니다.",
    },
    "news": {
        "source_name": "뉴스·공식자료",
        "weight": "5% · 경로 점수 최대 3점",
        "purpose": "공개·캐스팅·수상·시청률·제작 발표 등 이슈가 발생한 이유와 맥락을 보완합니다.",
        "integration": "Google News RSS를 검색어별로 조회합니다. 별도 뉴스 API 키는 사용하지 않습니다.",
        "collection": [
            "Google News RSS에서 일반 콘텐츠 뉴스 9개 검색어 사용",
            "OTT 신작 관련 5개 검색어는 OTT 랭킹·신작 경로의 보조자료로 분류",
            "검색어별 최대 12개, 최근 7일 기사만 수집",
            "가능하면 Google News 중계 URL을 원문 URL로 변환하고 og:image·twitter:image를 추출",
        ],
        "filters": [
            "제목과 링크가 없는 기사는 제외",
            "기사 제목·요약에서 작품명과 키워드를 추출",
            "같은 작품의 여러 기사와 타 경로 피드는 핵심 이슈 묶음 단계에서 통합",
        ],
        "ranking": "뉴스 자체는 단독 화제성을 과대평가하지 않도록 경로 가중치를 가장 낮게 설정합니다.",
        "score": "공식자료 여부와 설명의 구체성을 반응 강도 보조점수로 반영합니다. 동일 이슈가 여러 기사에서 반복되면 관련 피드 수 보너스가 붙습니다.",
        "limitations": "현재 검색어 기반 Google News RSS이므로 모든 언론 보도를 전수 수집하지 않습니다. '공식자료' 표시는 원문 출처를 완전히 검증한 인증 마크가 아니라 수집 분류입니다.",
        "queries": [
            "한국 드라마 신작 공개", "한국 예능 신작 공개", "한국 영화 신작 공개",
            "드라마 캐스팅 확정", "예능 새 멤버 합류", "콘텐츠 수상 화제",
            "드라마 시청률 상승", "웹툰 원작 드라마 제작", "배우 감독 인터뷰 신작",
        ],
    },
}

SOURCE_DETAIL_KEY_BY_NAME = {
    "YouTube 반응": "youtube",
    "극장·박스오피스": "boxoffice",
    "OTT 랭킹·신작": "ott",
    "온라인 화제성": "buzz",
    "뉴스·공식자료": "news",
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


if "page" not in st.session_state:
    st.session_state["page"] = "home"


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


def _render_source_detail(detail_key):
    detail = SOURCE_DETAIL_LOGIC.get(detail_key)
    if not detail:
        st.warning("세부 로직 정보를 찾지 못했습니다.")
        return

    st.markdown(f"### {detail['source_name']}")
    st.caption(detail["weight"])
    _detail_box("역할", detail["purpose"])
    if detail.get("integration"):
        _detail_box("연동 방식", detail["integration"])
    _detail_list("수집 단계", detail.get("collection", []))
    _detail_list("선정·제외 기준", detail.get("filters", []))
    _detail_box("경로 내 정렬 기준", detail.get("ranking", ""))
    _detail_box("핵심 이슈 점수 반영", detail.get("score", ""))

    if detail.get("queries"):
        st.markdown("**실제 검색어**")
        st.markdown(
            '<div class="logic-code">' + html.escape("\n".join(detail["queries"])) + '</div>',
            unsafe_allow_html=True,
        )
    if detail.get("channels"):
        st.markdown("**수집 대상 공식 채널**")
        st.markdown(
            '<div class="logic-code">' + html.escape("\n".join(detail["channels"])) + '</div>',
            unsafe_allow_html=True,
        )

    _detail_box("현재 한계", detail.get("limitations", ""))
    st.caption("이 내용은 현재 app.py와 collect_issues.py에 구현된 실제 기준을 설명합니다.")


if hasattr(st, "dialog"):
    @st.dialog("수집 경로 세부 로직", width="large")
    def show_source_detail_dialog(detail_key):
        _render_source_detail(detail_key)
else:
    def show_source_detail_dialog(detail_key):
        st.session_state["logic_detail_fallback"] = detail_key


def handle_logic_detail_request():
    try:
        detail_key = st.query_params.get("logic_detail", "")
        view = st.query_params.get("view", "")
    except Exception:
        detail_key = ""
        view = ""

    if isinstance(detail_key, list):
        detail_key = detail_key[0] if detail_key else ""
    if isinstance(view, list):
        view = view[0] if view else ""

    if view in {"home", "issue_db", "theme_db"}:
        st.session_state["page"] = view

    if detail_key in SOURCE_DETAIL_LOGIC:
        try:
            st.query_params.clear()
        except Exception:
            pass
        show_source_detail_dialog(detail_key)


handle_logic_detail_request()

if st.session_state.get("logic_detail_fallback"):
    with st.expander("수집 경로 세부 로직", expanded=True):
        _render_source_detail(st.session_state.pop("logic_detail_fallback"))


def load_data():
    issues = pd.read_csv(ISSUE_PATH, sep="|").fillna("")

    if THEME_DB_PATH.exists():
        themes = pd.read_csv(THEME_DB_PATH, sep="|").fillna("")
    elif OLD_THEME_PATH.exists():
        themes = pd.read_csv(OLD_THEME_PATH, sep="|").fillna("")
    else:
        raise FileNotFoundError("theme_db.csv 또는 theme_pool.csv 파일이 없습니다.")

    contents = pd.read_csv(CONTENT_DB_PATH, sep="|").fillna("")

    for optional_column in ["source_url", "image_url"]:
        if optional_column not in issues.columns:
            issues[optional_column] = ""

    return issues, themes, contents


def filter_recent_issues(issues, days=7):
    issues = issues.copy()
    issues["date"] = pd.to_datetime(issues["date"], errors="coerce")

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
        return "YouTube 반응"
    if any(token in s for token in ["극장", "KOFIC", "KOBIS", "박스오피스", "CGV", "롯데시네마", "메가박스"]):
        return "극장·박스오피스"
    if any(token in s for token in ["OTT", "넷플릭스", "티빙", "웨이브", "디즈니", "쿠팡플레이"]):
        return "OTT 랭킹·신작"
    if any(token in s for token in ["온라인 화제성", "데이터랩", "검색 관심도"]):
        return "온라인 화제성"

    # 기존 온라인 화제 기사·네이버 이슈는 기사 데이터이므로 뉴스로 분류합니다.
    return "뉴스·공식자료"


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
    if url.startswith("http://") or url.startswith("https://"):
        return url
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


def normalize_issue_key(text):
    value = str(text or "").lower()
    value = re.sub(r"관련\s*(유튜브|youtube)?\s*(반응 상승|화제 영상|반응 확인)", " ", value)
    value = re.sub(r"[^0-9a-z가-힣]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def issue_group_key(issue):
    related = normalize_issue_key(issue.get("related_content", ""))
    generic = {"", "드라마", "영화", "예능", "콘텐츠", "ott", "신작"}
    if related not in generic and len(related) >= 2:
        return related
    return normalize_issue_key(issue.get("issue_title", ""))


def parse_metric(description, labels):
    text = str(description or "")
    for label in labels:
        match = re.search(rf"{label}\s*(?:약\s*)?([0-9][0-9,]*)", text)
        if match:
            try:
                return int(match.group(1).replace(",", ""))
            except ValueError:
                pass
    return 0


def row_reaction_strength(issue):
    group = issue.get("source_group", classify_source(issue.get("source", "")))
    desc = str(issue.get("description", ""))
    score = 0.0

    if group == "YouTube 반응":
        views = parse_metric(desc, ["조회수", "일평균 조회수"])
        comments = parse_metric(desc, ["댓글"])
        if views > 0:
            score += min(9.0, math.log10(max(views, 10)) * 1.8)
        if comments > 0:
            score += min(4.0, math.log10(max(comments, 10)) * 1.4)
        if any(word in desc for word in ["빠르게 확산", "증가", "높은 반응"]):
            score += 2.0
    elif group == "극장·박스오피스":
        audience = parse_metric(desc, ["일일 관객", "관객 수", "누적 관객"])
        if audience > 0:
            score += min(10.0, math.log10(max(audience, 10)) * 2.0)
        if any(word in desc for word in ["1위", "신규 진입", "순위 상승"]):
            score += 4.0
    elif group == "OTT 랭킹·신작":
        if re.search(r"(?:top\s*10|톱\s*10|[1-9]위)", desc, re.I):
            score += 8.0
        if any(word in desc for word in ["신규 진입", "순위 상승", "공개 예정", "신작"]):
            score += 4.0
    elif group == "온라인 화제성":
        percent = parse_metric(desc, ["상승", "증가"])
        score += min(12.0, percent / 10.0) if percent else 5.0
    else:
        if any(word in str(issue.get("source", "")) for word in ["공식", "보도자료", "KOBIS", "Netflix"]):
            score += 5.0
        score += min(4.0, len(desc) / 100.0)

    return min(score, 15.0)


def recency_points(date_value):
    date_dt = pd.to_datetime(date_value, errors="coerce")
    if pd.isna(date_dt):
        return 0
    age = max((pd.Timestamp.today().normalize() - date_dt.normalize()).days, 0)
    if age <= 1:
        return 5
    if age <= 3:
        return 4
    if age <= 7:
        return 2
    return 0


def cross_source_points(route_count):
    if route_count >= 4:
        return 15
    if route_count == 3:
        return 10
    if route_count == 2:
        return 5
    return 0


def prepare_issues(issues):
    issues = issues.copy()
    issues["source_group"] = issues["source"].apply(classify_source)
    issues["issue_group_key"] = issues.apply(issue_group_key, axis=1)
    issues["row_reaction"] = issues.apply(row_reaction_strength, axis=1)

    group_stats = {}
    for key, group in issues.groupby("issue_group_key", dropna=False):
        routes = sorted(set(group["source_group"].astype(str)))
        feed_count = len(group)
        path_score = min(sum(SOURCE_CONFIG.get(route, {}).get("path_points", 0) for route in routes), 60)
        repetition = min(8.0, max(feed_count - 1, 0) * 2.0)
        reaction_score = min(20, round(float(group["row_reaction"].max()) + repetition))
        cross_score = cross_source_points(len(routes))
        latest_date = pd.to_datetime(group["date"], errors="coerce").max()
        recent_score = recency_points(latest_date)
        total = min(100, int(round(path_score + reaction_score + cross_score + recent_score)))
        group_stats[key] = {
            "issue_score": total,
            "path_score": path_score,
            "reaction_score": reaction_score,
            "cross_score": cross_score,
            "recent_score": recent_score,
            "route_count": len(routes),
            "feed_count": feed_count,
            "confirmed_routes": " · ".join(routes),
        }

    for column in ["issue_score", "path_score", "reaction_score", "cross_score", "recent_score", "route_count", "feed_count", "confirmed_routes"]:
        issues[column] = issues["issue_group_key"].map(lambda key: group_stats.get(key, {}).get(column, 0))

    return issues.sort_values(["issue_score", "date"], ascending=[False, False])


def representative_priority(row):
    source = str(row.get("source", ""))
    official_bonus = 3 if any(token in source for token in ["공식", "KOBIS", "보도자료", "Netflix"]) else 0
    route = row.get("source_group", "뉴스·공식자료")
    path_points = SOURCE_CONFIG.get(route, {}).get("path_points", 0)
    image_bonus = 2 if get_issue_image_url(row) else 0
    return official_bonus + path_points + image_bonus


def build_core_issues(issues):
    if issues.empty:
        return issues.copy()

    representatives = []
    for _, group in issues.groupby("issue_group_key", sort=False):
        ranked = group.copy()
        ranked["representative_priority"] = ranked.apply(representative_priority, axis=1)
        ranked = ranked.sort_values(["representative_priority", "date"], ascending=[False, False])
        representative = ranked.iloc[0].copy()

        # 카드의 제목·설명은 가장 신뢰도 높은 대표 피드를 사용하되,
        # 썸네일은 같은 이슈로 묶인 모든 피드 중 실제 이미지가 있는 것을 사용합니다.
        # KOBIS가 대표 피드여도 관련 YouTube/기사 이미지가 있으면 오른쪽에 표시됩니다.
        media_candidates = group.copy()
        media_candidates["resolved_image_url"] = media_candidates.apply(get_issue_image_url, axis=1)
        media_candidates = media_candidates[media_candidates["resolved_image_url"].astype(str).str.len() > 0].copy()
        if not media_candidates.empty:
            media_candidates["media_priority"] = media_candidates.apply(
                lambda row: (
                    3 if str(row.get("source_group", "")) == "YouTube 반응" else
                    2 if str(row.get("source_group", "")) == "뉴스·공식자료" else
                    1
                ),
                axis=1,
            )
            media_candidates = media_candidates.sort_values(
                ["media_priority", "date"], ascending=[False, False]
            )
            media_row = media_candidates.iloc[0]
            representative["image_url"] = media_row.get("resolved_image_url", "")
            representative["image_source_url"] = media_row.get("source_url", "")

        representatives.append(representative)

    return pd.DataFrame(representatives).sort_values(["issue_score", "date"], ascending=[False, False])

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
                "issue_score": issue.get("issue_score", 0)
            })

    return sorted(
        matched,
        key=lambda x: (x["score"], x["issue_score"]),
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


def score_tooltip(issue):
    return (
        f"수집 경로 {int(issue.get('path_score', 0))}/60 · "
        f"반응 강도 {int(issue.get('reaction_score', 0))}/20 · "
        f"교차 확인 {int(issue.get('cross_score', 0))}/15 · "
        f"최근성 {int(issue.get('recent_score', 0))}/5"
    )


def render_source_link(url):
    url = safe_url(url)
    if not url:
        return ""
    return f'<a class="source-link" href="{url}" target="_blank">근거 링크 보기 ↗</a>'


def render_issue_card(issue):
    url_html = render_source_link(issue.get("source_url", ""))
    source_group = html.escape(str(issue.get("source_group", classify_source(issue.get("source", "")))))
    issue_score = int(issue.get("issue_score", 0))
    tooltip = html.escape(score_tooltip(issue), quote=True)
    media_html = render_issue_media(issue)
    routes = html.escape(str(issue.get("confirmed_routes", source_group)))
    feed_count = int(issue.get("feed_count", 1))
    route_count = int(issue.get("route_count", 1))

    body = (
        '<div class="issue-card-body">'
        f'<span class="tag">{source_group}</span>'
        f'<div class="theme-name">{html.escape(str(issue.get("issue_title", "")))}</div>'
        f'<div class="small">{html.escape(str(issue.get("date", "")))} · {html.escape(str(issue.get("source", "")))}</div>'
        '<div class="issue-meta-row">'
        f'<span class="small" title="{tooltip}">핵심 이슈 점수: <span class="score">{issue_score}</span></span>'
        f'<span class="small">확인 경로 {route_count}개 · 관련 피드 {feed_count}개</span>'
        '</div>'
        f'<div class="small">확인 경로: {routes}</div>'
        f'<div class="small">{html.escape(str(issue.get("description", "")))}</div>'
        f'{url_html}'
        '</div>'
    )
    st.markdown(f'<div class="card issue-card">{body}{media_html}</div>', unsafe_allow_html=True)

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
    chip_parts = []
    for name, info in SOURCE_CONFIG.items():
        detail_key = SOURCE_DETAIL_KEY_BY_NAME.get(name, "")
        detail_href = f"?logic_detail={detail_key}&view=issue_db"
        chip_parts.append(
            '<span class="weight-chip-wrap">'
            f'<span class="weight-chip">{html.escape(name)} {info["weight_pct"]}%</span>'
            '<span class="weight-tooltip">'
            f'{html.escape(info["tooltip"])}'
            f'<a class="weight-detail-link" href="{html.escape(detail_href, quote=True)}" target="_self">세부내용 보기 →</a>'
            '</span>'
            '</span>'
        )
    chips = "".join(chip_parts)
    html_block = (
        '<div class="logic-card">'
        '<div class="theme-name">이슈 수집·선정 로직</div>'
        '<div class="logic-desc">최근 7일간 YouTube 반응, KOBIS 박스오피스, OTT 공식 랭킹·신작, 온라인 검색 관심도, 뉴스·공식자료를 수집합니다. 동일 이슈가 여러 경로와 여러 피드에서 반복 확인되고 실제 반응이 강할수록 핵심 이슈 점수가 높아집니다.</div>'
        '<div class="section-label">경로별 가중치</div>'
        f'{chips}'
        '<div class="section-label">핵심 이슈 점수</div>'
        '<div class="logic-desc">수집 경로 60점 + 반응 강도 20점 + 교차 확인 15점 + 최근성 5점으로 계산합니다. 같은 작품의 피드가 반복되면 반응 강도가 높아지고, 서로 다른 경로에서 동시에 확인되면 교차 확인 점수가 추가됩니다. 각 가중치에 마우스를 올리면 수집 기준을 볼 수 있습니다.</div>'
        '</div>'
    )
    st.markdown(html_block, unsafe_allow_html=True)


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
        render_issue_card(issue)
        keyword_tags = "".join(
            f'<span class="tag">{html.escape(kw)}</span>'
            for kw in split_keywords(issue.get("keywords", ""))[:16]
        )
        if keyword_tags:
            st.markdown(f'<div style="margin:-10px 20px 18px 20px">{keyword_tags}</div>', unsafe_allow_html=True)


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
        "사용자가 자연어로 입력한 상황/무드/장르를 키워드로 해석해, "
        "기존 테마 DB에서 가장 가까운 테마와 콘텐츠 후보를 탐색합니다."
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
        filtered = natural_theme_search(themes, search)

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
        if "natural_score" in row:
            try:
                score_html = f'<div class="small">자연어 매칭 점수: <span class="score">{int(row["natural_score"])}</span></div>'
            except Exception:
                score_html = ""

        matched_html = ""
        if "matched_keywords" in row and str(row["matched_keywords"]).strip():
            matched_html = f'<div class="small">해석된 키워드: {row["matched_keywords"]}</div>'

        html = (
            '<div class="theme-card">'
            f'<div class="small">{row["theme_id"]}</div>'
            f'<div class="theme-name">{row["theme_name"]}</div>'
            f'<div class="copy">노출명/카피: {row["copy"]}</div>'
            f'<div class="small">장르: {row["genre"]} · 무드: {row["mood"]}</div>'
            f'{score_html}'
            f'{matched_html}'
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
            main_issues = build_core_issues(issues).head(8)

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
