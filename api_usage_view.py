"""A small, optional cost inspector; no generation or billing mutations."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import time

import streamlit as st

from api_billing import fetch_monthly_project_costs, monthly_budget_summary
from api_usage import read_session_usage


BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"
USAGE_URL = "https://platform.openai.com/usage"
_CACHE_KEY = "api_project_cost_cache"
_FEATURE_NAMES = {
    "director": "AI 디렉터", "weekly_themes": "추천 테마 기획",
    "content_candidates": "콘텐츠 후보 생성", "theme_search": "AI 검색",
    "media_analysis": "미디어 분석", "media_research": "미디어 리서치 질문",
    "media_localization": "미디어 한글 정리",
}


def _config_signature(admin_key, project_id):
    return sha256((admin_key + "\0" + project_id).encode()).hexdigest()


def _cached_report(admin_key, project_id, *, fetch=False):
    """Cache only reports in this browser session, never the admin credential."""
    if not admin_key or not project_id:
        return None
    signature = _config_signature(admin_key, project_id)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    cached = st.session_state.get(_CACHE_KEY, {})
    valid = cached.get("signature") == signature and cached.get("month") == month
    if fetch and (not valid or time.monotonic() - cached.get("fetched_at", 0) >= 600):
        report = fetch_monthly_project_costs(admin_key, project_id)
        cached = {"signature": signature, "month": month, "report": report,
                  "fetched_at": time.monotonic()}
        st.session_state[_CACHE_KEY] = cached
        valid = True
    return cached.get("report") if valid else None


def _money(value):
    return f"${value:,.2f}"


def render_api_cost_control(get_secret):
    """Opening details may read project costs; ordinary rendering never does."""
    usage = read_session_usage()
    admin_key = get_secret("OPENAI_ADMIN_API_KEY")
    project_id = get_secret("OPENAI_BILLING_PROJECT_ID")
    budget = get_secret("OPENAI_MONTHLY_BUDGET_USD")
    with st.container(key="api_cost_indicator"):
        amount_note = st.empty()
        details = st.popover("AI 비용·잔액", key="api_cost_details", type="tertiary",
                             width="content", on_change="rerun")
        with details:
            st.markdown("**AI 사용과 비용**")
            st.caption("OpenAI API 요금은 ChatGPT 구독과 별도로 청구됩니다.")
            report = _cached_report(admin_key, project_id, fetch=bool(details.open))
            if report and report.get("status") == "ok":
                summary = monthly_budget_summary(report, budget)
                note = f"이번 달 {_money(report['cost_usd'])}"
                if summary.get("remaining_budget_usd") is not None:
                    remainder = summary["remaining_budget_usd"]
                    note += (f" · 월 예산 {_money(remainder)} 남음" if remainder >= 0 else
                             f" · 월 예산 {_money(-remainder)} 초과")
                amount_note.markdown('<span class="api-cost-summary">' + note + '</span>',
                                     unsafe_allow_html=True)
                st.markdown(f"이번 달 사용액 **{_money(report['cost_usd'])}**")
                if summary.get("remaining_budget_usd") is not None:
                    remaining = summary["remaining_budget_usd"]
                    text = (f"남은 월 예산 **{_money(remaining)}**" if remaining >= 0 else
                            f"월 예산 초과 **{_money(-remaining)}**")
                    st.markdown(text)
                    st.caption(f"설정 예산 {_money(summary['budget_usd'])} 기준이며 충전 잔액과 다릅니다. 예산은 자동 차단 한도가 아닙니다.")
                st.caption("지정한 프로젝트의 이번 달 비용 · UTC 기준 · 최대 10분 간격 조회 · 제공사 집계 지연 가능")
            elif admin_key and project_id:
                st.caption("월 사용액을 확인하지 못했어요. OpenAI 사용 내역에서 확인해 주세요.")
            else:
                st.caption("충전 잔액은 아래 OpenAI 결제 화면에서 확인할 수 있어요. 앱의 월 사용액 자동 조회는 아직 연결되지 않았습니다.")
            st.link_button("충전 잔액 확인 ↗", BILLING_URL, width="stretch")
            st.link_button("OpenAI 사용 내역 ↗", USAGE_URL, width="stretch")
            st.markdown("**비용이 발생하는 작업**")
            st.markdown("- AI 디렉터에 질문·생성·수정 요청\n"
                        "- 추천 테마 생성: 테마 기획 + 작품 후보 생성\n"
                        "- 콘텐츠 새로고침·라이브러리 작품 연결 및 업데이트\n"
                        "- 미디어 인사이트 분석·리서치 질문·새 자료 한글 정리")
            st.caption("화면 열람·빠른 검색·필터·채택·저장·버전 복구 버튼은 AI를 호출하지 않습니다. 이슈·OTT 정기 수집은 OpenAI를 사용하지 않으며 YouTube 등 각 서비스의 API 할당량을 사용합니다.")
            st.markdown(f"**이번 접속: 응답 {usage['response_count']:,}회 · {usage['total_tokens']:,}토큰**")
            if usage['response_count']:
                st.caption(f"입력 {usage['input_tokens']:,} · 출력 {usage['output_tokens']:,} · 입력 중 캐시 {usage['cached_input_tokens']:,}토큰")
                counts = [f"{_FEATURE_NAMES.get(name, '기타')} {count}회"
                          for name, count in usage['features'].items() if count]
                st.caption(" · ".join(counts))
                if usage.get('unmetered_response_count'):
                    st.caption(f"토큰 정보가 없는 응답 {usage['unmetered_response_count']}회는 토큰 합계에서 제외했습니다.")
            st.caption("이 브라우저 접속에서 반환받은 AI 응답만 집계합니다. 재접속 전·다른 사용자의 작업·응답을 받지 못한 요청은 포함되지 않아 청구 내역과 다를 수 있습니다.")
