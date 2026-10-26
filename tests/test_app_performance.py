"""Bounded rendering preserves complete search and work in progress."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup
import pandas as pd

from ai_director_view import SESSION_KEY
from test_ai_director_app import draft, session_with
from test_media_insights_app import SNAPSHOT as MEDIA_SNAPSHOT
from test_ott_app_integration import SNAPSHOT as OTT_SNAPSHOT
from test_theme_actions import app_fixture
from theme_search import rank_themes_by_ai_intent


def markup(app):
    return " ".join(item.value for item in app.markdown)


class AppPerformanceTests(unittest.TestCase):
    def assert_no_errors(self, app):
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def switch(self, app, label):
        app.session_state["home_sections"] = label
        app.run()
        self.assert_no_errors(app)

    def assert_no_generation_or_writes(self, state):
        for name in ("generate_weekly_themes", "generate_content_candidates_for_themes",
                     "persist_recommendations_locally", "persist_files_to_github",
                     "persist_content_candidates_to_github", "upsert_content_frame",
                     "enrich_poster_candidates"):
            state[name].assert_not_called()

    def page_meta(self, app, name):
        return BeautifulSoup(markup(app), "html.parser").select_one(f".{name}-page-meta")

    def test_initial_and_unvisited_tabs_skip_inactive_renderers_and_recommendation_reads(self):
        with (patch("ai_director_view.render_ai_director_page") as director,
              patch("ott_view.render_ott_page") as ott,
              patch("ott_collection.load_snapshot", return_value=deepcopy(OTT_SNAPSHOT)),
              patch("media_insights_runtime.render_media_insights_tab") as media,
              app_fixture(home_tab="지금 주목할 이야기") as state):
            app = state["app"]
            self.assert_no_errors(app)
            for renderer in (director, ott, media):
                renderer.assert_not_called()
            state["load_recommendation_runs"].assert_not_called()
            state["load_content_frame"].assert_not_called()
            self.switch(app, "OTT는 지금")
            ott.assert_called_once()
            director.assert_not_called()
            media.assert_not_called()
            state["load_recommendation_runs"].assert_not_called()
            state["load_content_frame"].assert_not_called()
            self.switch(app, "미디어 인사이트")
            media.assert_called_once()
            ott.assert_called_once()
            director.assert_not_called()
            self.switch(app, "추천 테마")
            state["load_recommendation_runs"].assert_called_once()
            self.assertTrue(any(button.key == "generate_weekly_themes" for button in app.button))
            self.assert_no_generation_or_writes(state)

    def test_director_conversation_and_unsubmitted_card_edit_survive_tab_roundtrip(self):
        initial = session_with(draft())
        with (patch("ai_director_view.run_director_turn") as turn,
              patch("ai_director_view.adopt_director_draft") as adopt,
              patch("ott_collection.load_snapshot", return_value=deepcopy(OTT_SNAPSHOT)),
              app_fixture(home_tab="AI 디렉터") as state):
            app = state["app"]
            app.session_state[SESSION_KEY] = deepcopy(initial)
            app.run()
            request_id = app.text_area(key="director_card_request_draft-1").id
            # AppTest omits pending form values even on an unchanged-tab run.
            # Seed stored values, then verify the form remains mounted without
            # a submit event while both inactive and active.
            app.session_state["director_card_scope_draft-1"] = "title"
            app.session_state["director_card_request_draft-1"] = "아직 제출하지 않은 제목 수정"
            self.switch(app, "OTT는 지금")
            self.assertEqual(app.session_state[SESSION_KEY], initial)
            self.assertEqual(app.text_area(key="director_card_request_draft-1").id, request_id)
            self.assertEqual(app.text_area(key="director_card_request_draft-1").value, "아직 제출하지 않은 제목 수정")
            self.switch(app, "AI 디렉터")
            self.assertEqual(app.text_area(key="director_card_request_draft-1").id, request_id)
            self.assertEqual(app.text_area(key="director_card_request_draft-1").value, "아직 제출하지 않은 제목 수정")
            self.assertEqual(app.selectbox(key="director_card_scope_draft-1").value, "title")
            self.assertEqual(app.session_state[SESSION_KEY], initial)
            turn.assert_not_called()
            adopt.assert_not_called()
            self.assert_no_generation_or_writes(state)

    def test_director_saved_matches_reuse_session_lookup_and_refresh_after_library_change(self):
        initial = session_with()
        initial["library_search_intent"] = {"genres": ["범죄"], "positive_keywords": ["배신"]}
        with (patch("theme_search.rank_themes_by_ai_intent", wraps=rank_themes_by_ai_intent) as rank,
              patch("ai_director_view.run_director_turn") as turn,
              patch("ai_director_view.adopt_director_draft") as adopt,
              patch("ott_collection.load_snapshot", return_value=deepcopy(OTT_SNAPSHOT)),
              app_fixture(home_tab="AI 디렉터") as state):
            app = state["app"]
            rank.assert_not_called()
            app.session_state[SESSION_KEY] = deepcopy(initial)
            app.run()
            self.assert_no_errors(app)
            rank.assert_called_once()
            self.assertEqual([row["theme_id"] for row in app.session_state[SESSION_KEY]["library_search_results"]], ["TEST-1"])
            app.run()
            self.switch(app, "OTT는 지금")
            self.switch(app, "AI 디렉터")
            rank.assert_called_once()

            path = Path("theme_db.csv")
            replacement = pd.read_csv(path, sep="|").fillna("")
            replacement.loc[0, "theme_id"] = "TEST-NEW"
            replacement.loc[0, "theme_name"] = "배신 이후의 새 선택"
            replacement.to_csv(path, sep="|", index=False)
            saved_bytes = path.read_bytes()
            app.run()
            self.assert_no_errors(app)
            self.assertEqual(rank.call_count, 2)
            self.assertEqual([row["theme_id"] for row in app.session_state[SESSION_KEY]["library_search_results"]], ["TEST-NEW"])
            self.assertIn("배신 이후의 새 선택", markup(app))
            self.assertEqual(app.session_state[SESSION_KEY]["library_search_intent"], initial["library_search_intent"])
            app.run()
            self.assertEqual(rank.call_count, 2)
            self.assertEqual(path.read_bytes(), saved_bytes)
            turn.assert_not_called()
            adopt.assert_not_called()
            self.assert_no_generation_or_writes(state)

    def test_saved_match_cache_follows_loaded_frame_when_file_revision_is_unchanged(self):
        # Reproduce a save between loading a frame and looking up its matches:
        # the first run has OLD in memory while the file stat is already NEW.
        # The next run loads NEW with that same stat and must discard OLD.
        app_path = Path(__file__).resolve().parents[1] / "app.py"
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
        helper = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "find_saved_theme_matches")
        rank = Mock(side_effect=lambda frame, intent: frame.copy())
        namespace = dict(
            pd=pd, hashlib=hashlib, json=json, deepcopy=deepcopy,
            st=SimpleNamespace(session_state={}), APP_VERSION="test-release",
            themes=pd.DataFrame([dict(theme_id="OLD", theme_name="이전에 읽은 테마")]),
            rank_themes_by_ai_intent=rank,
            file_revision=lambda path: ("unchanged-file", 1, 100, 1, 1),
            get_active_theme_path=lambda: "unused.csv",
        )
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(app_path), "exec"), namespace)
        lookup = namespace["find_saved_theme_matches"]
        intent = {"genres": ["범죄"]}
        self.assertEqual(lookup(intent)[0]["theme_id"], "OLD")
        self.assertEqual(lookup(intent)[0]["theme_id"], "OLD")
        rank.assert_called_once()
        namespace["themes"] = pd.DataFrame([dict(theme_id="NEW", theme_name="새로 읽은 테마")])
        self.assertEqual(lookup(intent)[0]["theme_id"], "NEW")
        self.assertEqual(rank.call_count, 2)

    def test_media_ott_and_weekly_filter_values_survive_switching_tabs(self):
        snapshot = deepcopy(MEDIA_SNAPSHOT)
        snapshot["items"][0]["country"] = "US"
        with (patch("media_insights_runtime.load_snapshot", return_value=snapshot),
              patch("ott_collection.load_snapshot", return_value=deepcopy(OTT_SNAPSHOT)),
              patch("media_insights_runtime.collect_media_insights") as collect,
              patch("media_insights_runtime.analyze_media_item") as analyze,
              patch("media_insights_analysis.answer_media_question") as ask,
              app_fixture(home_tab="미디어 인사이트") as state):
            app = state["app"]
            app.radio(key="media_insight_topic").set_value("제휴·유통").run()
            app.selectbox(key="media_insight_country").set_value("미국").run()
            app.selectbox(key="media_insight_period").set_value("최근 90일").run()
            self.switch(app, "OTT는 지금")
            app.segmented_control(key="ott_selected_platform").set_value("tving").run()
            self.switch(app, "추천 테마")
            app.select_slider(key="weekly_theme_count").set_value(20).run()
            self.switch(app, "미디어 인사이트")
            self.assertEqual(app.radio(key="media_insight_topic").value, "제휴·유통")
            self.assertEqual(app.selectbox(key="media_insight_country").value, "미국")
            self.assertEqual(app.selectbox(key="media_insight_period").value, "최근 90일")
            self.switch(app, "OTT는 지금")
            self.assertEqual(app.segmented_control(key="ott_selected_platform").value, "tving")
            self.switch(app, "추천 테마")
            self.assertEqual(app.select_slider(key="weekly_theme_count").value, 20)
            for callback in (collect, analyze, ask):
                callback.assert_not_called()
            self.assert_no_generation_or_writes(state)

    def test_public_comparison_preserves_selection_without_private_inputs_or_calls(self):
        with (patch("media_insights_runtime.load_snapshot", return_value=deepcopy(MEDIA_SNAPSHOT)),
              patch("media_research_runtime.answer_research") as ask,
              patch("media_research_storage.create_project") as create,
              app_fixture(home_tab="미디어 인사이트") as state):
            app = state["app"]
            app.session_state["media_research_project"] = {"brief": "retired private input"}
            app.session_state["media_insight_questions"] = [{"question": "retired question"}]
            app.session_state["media_insight_research_open"] = True
            app.run()
            for key in ("media_research_project", "media_insight_questions", "media_insight_research_open"):
                self.assertNotIn(key, app.session_state)
            app.button(key="media_insights_compare_toggle").click().run()
            selected = app.selectbox(key="public_media_document").value
            self.assertEqual(len(app.chat_input), 0)
            self.assertEqual(len(app.text_area), 0)
            self.switch(app, "추천 테마")
            self.switch(app, "미디어 인사이트")
            self.assertEqual(app.selectbox(key="public_media_document").value, selected)
            self.assertTrue(app.session_state["media_insight_comparison_open"])
            ask.assert_not_called()
            create.assert_not_called()
            self.assert_no_generation_or_writes(state)

    def test_library_pages_all_621_rows_and_searches_before_paging_then_resets_filters(self):
        with app_fixture() as state:
            rows = [dict(theme_id=f"SYN-{index:03d}", theme_name=f"합성 테마 {index:03d}",
                         copy="인물의 성장을 살펴봅니다", genre="드라마", mood="잔잔한",
                         trigger_keywords="성장", source_status="HUMAN_APPROVED" if index % 2 == 0 else "LEGACY_UNVERIFIED")
                    for index in range(621)]
            rows[-1]["theme_name"] = "숨은유리성"
            path = Path("theme_db.csv")
            pd.DataFrame(rows).to_csv(path, sep="|", index=False)
            saved_bytes = path.read_bytes()
            app = state["app"]
            app.button(key="open_theme_library").click().run()
            self.assert_no_errors(app)
            self.assertEqual(self.page_meta(app, "library")["data-total-results"], "621")
            self.assertEqual(self.page_meta(app, "library")["data-visible-count"], "24")
            self.assertTrue(app.button(key="theme_library_previous").disabled)
            self.assertTrue(any(button.key == "refresh_content_SYN-000" for button in app.button))
            self.assertFalse(any(button.key == "refresh_content_SYN-024" for button in app.button))
            app.button(key="theme_library_next").click().run()
            self.assertEqual(app.selectbox(key="theme_library_page").value, 2)
            self.assertTrue(any(button.key == "refresh_content_SYN-024" for button in app.button))
            app.button(key="theme_library_previous").click().run()
            self.assertEqual(app.selectbox(key="theme_library_page").value, 1)
            app.selectbox(key="theme_library_page").set_value(26).run()
            self.assertEqual(self.page_meta(app, "library")["data-visible-count"], "21")
            self.assertTrue(app.button(key="theme_library_next").disabled)
            self.assertIn("숨은유리성", markup(app))
            app.text_input(key="theme_quick_search_input").set_value("숨은유리성").run()
            self.assertEqual(app.session_state["theme_library_page"], 1)
            self.assertEqual(self.page_meta(app, "library")["data-total-results"], "1")
            self.assertTrue(any(button.key == "refresh_content_SYN-620" for button in app.button))
            app.text_input(key="theme_quick_search_input").set_value("").run()
            app.selectbox(key="theme_library_page").set_value(3).run()
            app.selectbox(key="theme_library_source_filter").set_value("검증 테마").run()
            self.assertEqual(app.selectbox(key="theme_library_page").value, 1)
            self.assertEqual(self.page_meta(app, "library")["data-total-results"], "311")
            app.selectbox(key="theme_library_page").set_value(2).run()
            app.selectbox(key="theme_library_source_filter").set_value("전체").run()
            self.assertEqual(app.selectbox(key="theme_library_page").value, 1)
            self.assertEqual(self.page_meta(app, "library")["data-total-results"], "621")
            app.text_input(key="theme_quick_search_input").set_value("완전히없는소재").run()
            self.assertEqual(self.page_meta(app, "library")["data-visible-count"], "0")
            reset = next(button for button in app.button if button.label == "검색 초기화")
            reset.click().run()
            self.assertEqual(self.page_meta(app, "library")["data-total-results"], "621")
            self.assertEqual(app.session_state["theme_library_page"], 1)
            self.assert_no_errors(app)
            self.assertEqual(path.read_bytes(), saved_bytes)
            self.assert_no_generation_or_writes(state)

    def test_issue_browser_pages_full_results_and_search_finds_an_offscreen_work(self):
        with app_fixture(home_tab="지금 주목할 이야기") as state:
            day = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
            rows = [dict(date=day, source="온라인 화제·뉴스", source_type="news",
                         related_content=f"검증작품{index:03d}", issue_title=f"검증작품{index:03d} 공개",
                         keywords="영화,공개", image_url="", published_at=day,
                         publisher="영화신문", event_type="release", description="새 영화 공개 소식",
                         source_url=f"https://news.example.com/article/{index}")
                    for index in range(100)]
            path = Path("issue_feed.csv")
            pd.DataFrame(rows).to_csv(path, sep="|", index=False)
            saved_bytes = path.read_bytes()
            app = state["app"]
            app.button(key="spotlight_all_issues").click().run()
            self.assert_no_errors(app)
            self.assertEqual(self.page_meta(app, "issue")["data-total-results"], "100")
            self.assertEqual(self.page_meta(app, "issue")["data-visible-count"], "24")
            app.button(key="issue_browser_next").click().run()
            self.assertEqual(app.selectbox(key="issue_browser_page").value, 2)
            app.selectbox(key="issue_browser_page").set_value(5).run()
            self.assertEqual(self.page_meta(app, "issue")["data-visible-count"], "4")
            search = next(widget for widget in app.text_input if widget.label == "이슈명/콘텐츠/키워드/출처 검색")
            search.set_value("검증작품000").run()
            self.assertEqual(app.session_state["issue_browser_page"], 1)
            self.assertEqual(self.page_meta(app, "issue")["data-total-results"], "1")
            self.assertIn("검증작품000", markup(app))
            self.assertEqual(path.read_bytes(), saved_bytes)
            self.assert_no_generation_or_writes(state)


if __name__ == "__main__":
    unittest.main()
