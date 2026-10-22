"""The compact theme controls preserve generation, storage and explicit loading."""
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


def recommendation():
    return {
        "recommendation_source": "AI_GENERATED", "source_label": "AI 신규 생성",
        "theme": {"theme_id": "TEST-1", "theme_name": "공범들의 마지막 선택", "copy": "믿음이 갈라지는 순간",
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
        state = {"frame": ensure_content_schema(None)}
        stack.enter_context(patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-openai-key", "GITHUB_WRITE_TOKEN": "test-github-token", "GITHUB_REPO": "test/repo",
        }))
        def mock(name, **kwargs):
            result = stack.enter_context(patch(name, **kwargs))
            state[name.rsplit(".", 1)[-1]] = result
            return result
        mock("llm_theme_generator.github_writeback_configured", return_value=True)
        mock("llm_theme_generator.sync_files_from_github", return_value={"status": "skipped"})
        mock("llm_theme_generator.load_recommendation_runs", side_effect=lambda *args: deepcopy([run] if with_history else []))
        mock("llm_theme_generator.load_recommendation_history", return_value=pd.DataFrame())
        generated_meta = dict(run["meta"], run_id="new-run")
        mock("llm_theme_generator.generate_weekly_themes", return_value=([recommendation()], generated_meta))
        mock("llm_theme_generator.persist_recommendations_locally", return_value=(themes, pd.DataFrame(), themes))
        mock("llm_theme_generator.persist_files_to_github", return_value={"status": "success"})
        mock("llm_theme_generator.generate_content_candidates_for_themes", return_value=({"TEST-1": [{"title": "타짜", "year": "2006"}]}, {}))
        mock("kinolights_candidates.validate_content_suggestions", return_value=({"TEST-1": [dict(title="타짜", year="2006", content_type="영화", status="verified", source_url="https://m.kinolights.com/title/test", reason="공범과 배신")]}, {"verified_count": 1}))
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

    def test_generation_uses_selected_count_and_preserves_storage(self):
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
            state["enrich_poster_candidates"].assert_not_called()

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
