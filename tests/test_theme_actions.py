"""Theme generation fills the new run with content and preserves retry safety."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from theme_content_manager import ensure_content_schema


ROOT = Path(__file__).resolve().parents[1]


def recommendation(theme_id="TEST-1"):
    return {
        "recommendation_source": "AI_GENERATED", "source_label": "AI 신규 생성",
        "theme": {"theme_id": theme_id, "theme_name": "공범들의 마지막 선택", "copy": "믿음이 갈라지는 순간",
                  "genre": "범죄", "mood": "긴장감", "trigger_keywords": "범죄,배신", "source_issue": "영화 '실낙원' 극장 순위"},
        "source_issue_summary": "영화 '실낙원' 극장 순위", "rationale": "함께 범죄를 계획한 인물들의 선택을 살펴봅니다.",
        "contents": [],
    }


@contextmanager
def app_fixture(*, with_history=True):
    """Use only temporary inputs; every remote call and writeback is mocked."""
    with TemporaryDirectory() as temporary, ExitStack() as stack:
        directory = Path(temporary)
        day = pd.Timestamp.now(tz="Asia/Seoul").strftime("%Y-%m-%d")
        yesterday = (pd.Timestamp.now(tz="Asia/Seoul") - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        issues = pd.DataFrame([dict(date=day, source="KOBIS 박스오피스", related_content="실낙원",
                                   issue_title="실낙원 일일 박스오피스 1위", keywords="영화,범죄", image_url="",
                                   source_url="https://www.kobis.or.kr/chart", reference_date=yesterday,
                                   description=f"{yesterday} KOBIS 일별 박스오피스 1위. 일일 관객 22,474명.")])
        issues.to_csv(directory / "issue_feed.csv", sep="|", index=False)
        themes = pd.DataFrame([recommendation()["theme"]])
        themes.to_csv(directory / "theme_db.csv", sep="|", index=False)
        run = dict(run_id="test-run", recommended_at=f"{day} 08:00:00", recommendations=[recommendation()],
                   meta=dict(run_id="test-run", selected_count=1, new_count=1, existing_count=0, recommended_at=f"{day} 08:00:00"))
        state = {"frame": ensure_content_schema(None), "runs": [run] if with_history else [], "events": []}
        stack.enter_context(patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-openai-key", "GITHUB_WRITE_TOKEN": "test-github-token", "GITHUB_REPO": "test/repo",
        }))
        def mock(name, **kwargs):
            result = stack.enter_context(patch(name, **kwargs))
            state[name.rsplit(".", 1)[-1]] = result
            return result
        mock("llm_theme_generator.github_writeback_configured", return_value=True)
        mock("llm_theme_generator.sync_files_from_github", return_value={"status": "skipped"})
        mock("llm_theme_generator.load_recommendation_runs", side_effect=lambda *args: deepcopy(state["runs"]))
        mock("llm_theme_generator.load_recommendation_history", return_value=pd.DataFrame())
        generated_meta = dict(run["meta"], run_id="new-run")
        generated_recs = [recommendation("NEW-1")]
        def generate(**kwargs):
            state["events"].append("generate_themes")
            return deepcopy(generated_recs), deepcopy(generated_meta)
        mock("llm_theme_generator.generate_weekly_themes", side_effect=generate)
        def persist_themes(**kwargs):
            state["events"].append("persist_themes")
            latest = dict(run_id=kwargs["run_meta"]["run_id"], recommended_at=kwargs["run_meta"]["recommended_at"],
                          recommendations=deepcopy(kwargs["recommendations"]), meta=deepcopy(kwargs["run_meta"]))
            state["runs"].insert(0, latest)
            added = pd.DataFrame([rec["theme"] for rec in kwargs["recommendations"]])
            return pd.concat([themes, added], ignore_index=True), pd.DataFrame(), added
        mock("llm_theme_generator.persist_recommendations_locally", side_effect=persist_themes)
        mock("llm_theme_generator.persist_files_to_github", return_value={"status": "success"})
        def generate_contents(**kwargs):
            state["events"].append("generate_contents")
            return {rec["theme"]["theme_id"]: [{"title": "타짜", "year": "2006"}]
                    for rec in kwargs["recommendations"]}, {}
        mock("llm_theme_generator.generate_content_candidates_for_themes", side_effect=generate_contents)
        def validate(suggestions, **kwargs):
            return {key: [dict(title="타짜", year="2006", content_type="영화", status="verified",
                               source_url="https://m.kinolights.com/title/test", reason="공범과 배신")]
                    for key in suggestions}, {"verified_count": len(suggestions)}
        mock("kinolights_candidates.validate_content_suggestions", side_effect=validate)
        mock("poster_assets.enrich_poster_candidates", side_effect=lambda items, **kwargs: [dict(item, poster_url="https://images.example.com/tazza.jpg") for item in items])
        mock("theme_content_manager.load_content_frame", side_effect=lambda *args: state["frame"].copy())
        def store(path, rows):
            state["frame"] = rows.copy()
            return rows.copy()
        mock("theme_content_manager.upsert_content_frame", side_effect=store)
        mock("llm_theme_generator.persist_content_candidates_to_github", return_value={"status": "success"})
        mock("requests.get", side_effect=AssertionError("App flow tests must never perform network requests"))
        previous = Path.cwd()
        os.chdir(directory)
        try:
            state["app"] = AppTest.from_file(ROOT / "app.py", default_timeout=15).run()
            yield state
        finally:
            os.chdir(previous)


class ThemeActionTests(unittest.TestCase):
    def assert_no_errors(self, app):
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def test_normal_reruns_do_not_generate_or_search_posters(self):
        with app_fixture() as state:
            app = state["app"]
            self.assert_no_errors(app)
            self.assertFalse(app.button(key="generate_weekly_themes").disabled)
            self.assertFalse(app.button(key="load_weekly_contents").disabled)
            app.run()
            self.assert_no_errors(app)
            state["generate_weekly_themes"].assert_not_called()
            state["generate_content_candidates_for_themes"].assert_not_called()
            state["enrich_poster_candidates"].assert_not_called()
            state["upsert_content_frame"].assert_not_called()

    def test_generation_fills_new_run_with_content_and_posters_before_rerender(self):
        with app_fixture() as state:
            app = state["app"]
            app.select_slider[0].set_value(15).run()
            app.button(key="generate_weekly_themes").click().run()
            self.assert_no_errors(app)
            generated = state["generate_weekly_themes"]
            generated.assert_called_once()
            self.assertEqual(generated.call_args.kwargs["top_n"], 15)
            self.assertTrue(generated.call_args.kwargs["issue_records"])
            state["persist_recommendations_locally"].assert_called_once()
            state["persist_files_to_github"].assert_called_once()
            self.assertEqual(state["persist_files_to_github"].call_args.kwargs["run_id"], "new-run")
            self.assertEqual(app.session_state["last_generated_run_id"], "new-run")
            self.assertEqual(app.session_state["github_writeback_result"]["status"], "success")
            content = state["generate_content_candidates_for_themes"]
            content.assert_called_once()
            self.assertEqual(content.call_args.kwargs["per_theme"], 16)
            self.assertEqual(content.call_args.kwargs["recommendations"][0]["theme"]["theme_id"], "NEW-1")
            self.assertLess(state["events"].index("persist_themes"), state["events"].index("generate_contents"))
            state["enrich_poster_candidates"].assert_called_once()
            state["upsert_content_frame"].assert_called_once()
            self.assertEqual(set(state["frame"]["run_id"]), {"new-run", "THEME_DB"})
            self.assertEqual(set(state["frame"]["theme_key"]), {"NEW-1"})
            self.assertNotIn("test-run", set(state["frame"]["run_id"]))
            self.assertEqual(state["persist_content_candidates_to_github"].call_args.kwargs["run_id"], "new-run")
            markup = " ".join(item.value for item in app.markdown)
            self.assertIn("https://images.example.com/tazza.jpg", markup)
            self.assertEqual(app.button(key="load_weekly_contents").label, "콘텐츠 새로고침")
            app.run()
            self.assert_no_errors(app)
            generated.assert_called_once()
            content.assert_called_once()
            state["enrich_poster_candidates"].assert_called_once()

    def test_bulk_load_targets_current_run_and_keeps_poster_after_rerun(self):
        with app_fixture() as state:
            app = state["app"]
            app.button(key="load_weekly_contents").click().run()
            self.assert_no_errors(app)
            candidates = state["generate_content_candidates_for_themes"]
            candidates.assert_called_once()
            self.assertEqual(candidates.call_args.kwargs["per_theme"], 16)
            self.assertEqual(candidates.call_args.kwargs["recommendations"][0]["theme"]["theme_id"], "TEST-1")
            state["enrich_poster_candidates"].assert_called_once()
            saved = state["frame"]
            self.assertEqual(set(saved["run_id"]), {"test-run", "THEME_DB"})
            self.assertTrue(saved["poster_url"].eq("https://images.example.com/tazza.jpg").all())
            state["persist_content_candidates_to_github"].assert_called_once()
            self.assertEqual(state["persist_content_candidates_to_github"].call_args.kwargs["run_id"], "test-run")
            markup = " ".join(item.value for item in app.markdown)
            self.assertIn("https://images.example.com/tazza.jpg", markup)
            self.assertEqual(app.button(key="load_weekly_contents").label, "콘텐츠 새로고침")
            app.run()
            self.assert_no_errors(app)
            state["enrich_poster_candidates"].assert_called_once()

    def test_content_failure_keeps_generated_themes_and_retry_only_fills_new_run(self):
        with app_fixture() as state:
            app = state["app"]
            content = state["generate_content_candidates_for_themes"]
            successful_lookup = content.side_effect
            content.side_effect = RuntimeError("content provider unavailable")
            app.button(key="generate_weekly_themes").click().run()
            self.assert_no_errors(app)
            state["generate_weekly_themes"].assert_called_once()
            state["persist_recommendations_locally"].assert_called_once()
            state["persist_files_to_github"].assert_called_once()
            content.assert_called_once()
            self.assertEqual(state["runs"][0]["run_id"], "new-run")
            self.assertEqual(app.session_state["last_generated_run_id"], "new-run")
            self.assertTrue(app.session_state["content_candidate_error"])
            self.assertTrue(any("콘텐츠" in item.value for item in app.warning))
            self.assertFalse(app.button(key="load_weekly_contents").disabled)
            self.assertTrue(state["frame"].empty)
            state["enrich_poster_candidates"].assert_not_called()
            state["upsert_content_frame"].assert_not_called()
            app.run()
            self.assert_no_errors(app)
            content.assert_called_once()
            content.side_effect = successful_lookup
            app.button(key="load_weekly_contents").click().run()
            self.assert_no_errors(app)
            self.assertEqual(content.call_count, 2)
            state["generate_weekly_themes"].assert_called_once()
            state["persist_recommendations_locally"].assert_called_once()
            self.assertEqual(set(state["frame"]["run_id"]), {"new-run", "THEME_DB"})
            self.assertEqual(set(state["frame"]["theme_key"]), {"NEW-1"})
            self.assertFalse(app.session_state.get("content_candidate_error", ""))
            self.assertEqual(app.button(key="load_weekly_contents").label, "콘텐츠 새로고침")
            self.assertFalse(any("content provider unavailable" in item.value for item in app.warning))

    def test_poster_loading_covers_five_visible_unique_works_after_duplicate_rows(self):
        with app_fixture() as state:
            def validate(suggestions, **kwargs):
                return {key: [dict(title=title, year="2020", content_type="영화", status="verified",
                                   source_url="https://m.kinolights.com/title/test")
                              for title in ["작품1", "작품1", "작품2", "작품3", "작품4", "작품5", "작품6"]]
                        for key in suggestions}, {}
            state["validate_content_suggestions"].side_effect = validate
            app = state["app"]
            app.button(key="load_weekly_contents").click().run()
            self.assert_no_errors(app)
            lookup = state["enrich_poster_candidates"]
            lookup.assert_called_once()
            self.assertEqual([item["title"] for item in lookup.call_args.args[0]],
                             ["작품1", "작품2", "작품3", "작품4", "작품5"])
            self.assertEqual(lookup.call_args.kwargs["limit"], 50)
            self.assertIn("작품6", set(state["frame"].title))

    def test_empty_content_response_is_reported_without_saving_empty_results(self):
        with app_fixture() as state:
            app = state["app"]
            state["validate_content_suggestions"].side_effect = lambda suggestions, **kwargs: ({key: [] for key in suggestions}, {})
            app.button(key="generate_weekly_themes").click().run()
            self.assert_no_errors(app)
            state["persist_recommendations_locally"].assert_called_once()
            self.assertEqual(state["runs"][0]["run_id"], "new-run")
            self.assertTrue(app.session_state["content_candidate_error"])
            self.assertTrue(app.warning)
            state["enrich_poster_candidates"].assert_not_called()
            state["upsert_content_frame"].assert_not_called()
            state["persist_content_candidates_to_github"].assert_not_called()

    def test_theme_generation_failure_does_not_start_or_save_content(self):
        with app_fixture() as state:
            app = state["app"]
            state["generate_weekly_themes"].side_effect = RuntimeError("theme generation failed")
            app.button(key="generate_weekly_themes").click().run()
            self.assertTrue(app.error or app.exception)
            state["persist_recommendations_locally"].assert_not_called()
            state["generate_content_candidates_for_themes"].assert_not_called()
            state["enrich_poster_candidates"].assert_not_called()
            state["upsert_content_frame"].assert_not_called()
            self.assertEqual(state["runs"][0]["run_id"], "test-run")

    def test_empty_history_is_renderable_and_content_load_is_disabled(self):
        with app_fixture(with_history=False) as state:
            app = state["app"]
            self.assert_no_errors(app)
            self.assertTrue(app.button(key="load_weekly_contents").disabled)
            self.assertFalse(app.button(key="generate_weekly_themes").disabled)
            self.assertTrue(any("테마 생성하기" in item.value for item in app.info))
            state["generate_content_candidates_for_themes"].assert_not_called()
            state["enrich_poster_candidates"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
