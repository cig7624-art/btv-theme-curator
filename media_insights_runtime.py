"""Explicit actions connecting official collection, grounded analysis and storage."""
from __future__ import annotations

from pathlib import Path
import re

import streamlit as st

from media_insights_analysis import analyze_media_item, answer_media_question
from media_insights_collection import collect_media_insights, load_snapshot
from media_insights_storage import persist_media_analysis, persist_media_snapshot
from media_insights_view import render_media_insights_page


SNAPSHOT_PATH = Path(__file__).with_name("media_insights_snapshot.json")


def related_items(snapshot, selected, question="", limit=5):
    tokens = set(re.findall(r"[a-z0-9가-힣]{2,}", question.lower()))
    companies = {name for pattern, name in (
        (r"넷플릭스|netflix", "netflix"),
        (r"디즈니|disney|espn", "disney"),
        (r"아마존|프라임\s*비디오|amazon|prime\s*video", "amazon"),
        (r"컴캐스트|피콕|comcast|peacock", "comcast"),
        (r"sk텔레콤|skt|에스케이텔레콤", "skt"),
    ) if re.search(pattern, question, re.I)}
    topics = {name for pattern, name in (
        (r"광고|요금|구독료|advertis|pricing", "advertising_pricing"),
        (r"실적|매출|분기|earnings|revenue", "earnings_strategy"),
        (r"스포츠|중계권|sports|nba|nfl", "sports_content"),
        (r"제휴|인수|유통|번들|partner|merger|bundle", "partnership_distribution"),
        (r"인공지능|기술|검색|\bai\b|technology", "ai_technology"),
    ) if re.search(pattern, question, re.I)}
    ranked = []
    for item in snapshot.get("items", []):
        if item.get("id") == selected.get("id") or not item.get("evidence"):
            continue
        text = " ".join(str(item.get(key, "")) for key in ("title", "excerpt", "company", "topic")).lower()
        relevance = (2 * (item.get("topic") == selected.get("topic"))
                     + 20 * (str(item.get("company", "")).lower() in companies)
                     + 12 * (item.get("topic") in topics)
                     + sum(token in text for token in tokens))
        if relevance:
            ranked.append((relevance, str(item.get("published_at", "")), item))
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in ranked[:limit]]


def render_media_insights_tab(*, api_key, model, github_config, snapshot_path=SNAPSHOT_PATH):
    path = Path(snapshot_path)
    snapshot = load_snapshot(path)
    config = dict(github_config or {})
    config.setdefault("media_insights_path", path.name)

    def current_item(item_id):
        current = load_snapshot(path)
        item = next((row for row in current.get("items", []) if row.get("id") == item_id), None)
        if item is None:
            raise ValueError("자료가 갱신됐어요. 목록에서 다시 선택해 주세요.")
        return current, item

    def refresh():
        updated = collect_media_insights(previous=load_snapshot(path))
        result = persist_media_snapshot(updated, snapshot_path=path, github_config=config)
        return result["snapshot"]

    def analyze(item_id):
        current, item = current_item(item_id)
        analysis = analyze_media_item(item, related_items(current, item), api_key=api_key, model=model)
        result = persist_media_analysis(item_id, analysis, snapshot_path=path, github_config=config)
        if result.get("status") == "local_only":
            st.caption("분석은 현재 앱에 저장됐습니다. 영구 보관은 저장소 연결 후 사용할 수 있어요.")
        if result.get("refresh_required"):
            st.caption("새 자료도 함께 갱신됐어요. 다시 열면 최신 자료와 저장한 노트를 확인할 수 있습니다.")
        return analysis

    def ask(question, item_id):
        current, item = current_item(item_id)
        return answer_media_question(question, item, related_items(current, item, question),
                                     api_key=api_key, model=model)

    render_media_insights_page(snapshot, on_refresh=refresh,
                               on_analyze=analyze if api_key else None,
                               on_ask=ask if api_key else None)
