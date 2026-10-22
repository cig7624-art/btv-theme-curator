"""Saved recommendations browse preserved data without starting AI generation."""
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

    def assert_no_generation_or_writes(self, state):
        for name in ("generate_weekly_themes", "generate_content_candidates_for_themes",
                     "enrich_poster_candidates", "persist_recommendations_locally",
                     "persist_files_to_github", "persist_content_candidates_to_github",
                     "upsert_content_frame"):
            state[name].assert_not_called()

    def csv_bytes(self):
        return {path.name: path.read_bytes() for path in Path.cwd().glob("*.csv")}

    def test_normal_reruns_browse_saved_theme_without_generation_or_file_changes(self):
        with app_fixture() as state:
            app = state["app"]
            before_files, before_runs = self.csv_bytes(), deepcopy(state["runs"])
            self.assert_no_errors(app)
            keys = {button.key for button in app.button}
            self.assertNotIn("generate_weekly_themes", keys)
            self.assertNotIn("load_weekly_contents", keys)
            self.assertIn("open_ai_director", keys)
            self.assertIn("open_theme_library", keys)
            app.run()
            self.assert_no_errors(app)
            self.assertIn("공범들의 마지막 선택", " ".join(item.value for item in app.markdown))
            self.assertEqual(self.csv_bytes(), before_files)
            self.assertEqual(state["runs"], before_runs)
            self.assert_no_generation_or_writes(state)

    def test_stored_content_and_poster_are_visible_without_refetch_or_mutation(self):
        with app_fixture() as state:
            state["frame"] = ensure_content_schema(pd.DataFrame([{
                "run_id": "test-run", "theme_key": "TEST-1", "theme_id": "TEST-1", "rank": "1",
                "title": "타짜", "year": "2006", "content_type": "영화", "kinolights_status": "verified",
                "source_url": "https://m.kinolights.com/title/test", "poster_url": "https://images.example.com/tazza.jpg",
                "reason": "공범과 배신을 다루는 작품",
            }]))
            app = state["app"]
            before_frame, before_files = state["frame"].copy(), self.csv_bytes()
            app.run()
            self.assert_no_errors(app)
            markup = " ".join(item.value for item in app.markdown)
            self.assertIn("https://images.example.com/tazza.jpg", markup)
            self.assertIn("타짜", markup)
            app.run()
            pd.testing.assert_frame_equal(state["frame"], before_frame)
            self.assertEqual(self.csv_bytes(), before_files)
            self.assert_no_generation_or_writes(state)

    def test_saved_run_selector_keeps_older_recommendations_accessible(self):
        with app_fixture() as state:
            earlier = deepcopy(state["runs"][0])
            earlier["run_id"] = "older-run"
            earlier["recommendations"][0]["theme"]["theme_name"] = "먼저 저장했던 기획"
            state["runs"].append(earlier)
            before_runs, before_files = deepcopy(state["runs"]), self.csv_bytes()
            app = state["app"].run()
            app.selectbox(key="saved_recommendation_run").set_value("older-run").run()
            self.assert_no_errors(app)
            self.assertIn("먼저 저장했던 기획", " ".join(item.value for item in app.markdown))
            self.assertEqual(app.selectbox(key="saved_recommendation_run").value, "older-run")
            self.assertEqual(state["runs"], before_runs)
            self.assertEqual(self.csv_bytes(), before_files)
            self.assert_no_generation_or_writes(state)

    def test_director_adoption_handoff_selects_the_new_saved_run(self):
        with app_fixture() as state:
            adopted = deepcopy(state["runs"][0])
            adopted["run_id"] = "adopted-run"
            adopted["recommendations"][0]["theme"]["theme_name"] = "디렉터에서 최종 채택한 기획"
            state["runs"].insert(0, adopted)
            app = state["app"]
            app.session_state["saved_recommendation_run"] = "test-run"
            app.session_state["director_new_saved_run_id"] = "adopted-run"
            app.run()
            self.assert_no_errors(app)
            self.assertEqual(app.selectbox(key="saved_recommendation_run").value, "adopted-run")
            self.assertIn("디렉터에서 최종 채택한 기획", " ".join(item.value for item in app.markdown))
            self.assert_no_generation_or_writes(state)

    def test_director_navigation_keeps_saved_theme_data_intact(self):
        with app_fixture() as state:
            app = state["app"]
            before_files, before_runs = self.csv_bytes(), deepcopy(state["runs"])
            app.button(key="open_ai_director").click().run()
            self.assert_no_errors(app)
            self.assertEqual(app.session_state["home_sections"], "AI 디렉터")
            self.assertEqual(app.session_state["home_tab_default"], "AI 디렉터")
            self.assertTrue(any(chat.key == "director_chat_input" for chat in app.chat_input))
            self.assertEqual(self.csv_bytes(), before_files)
            self.assertEqual(state["runs"], before_runs)
            self.assert_no_generation_or_writes(state)

    def test_saved_theme_db_is_still_visible_without_recommendation_history(self):
        with app_fixture(with_history=False) as state:
            app = state["app"]
            before_files = self.csv_bytes()
            self.assert_no_errors(app)
            self.assertIn("공범들의 마지막 선택", " ".join(item.value for item in app.markdown))
            self.assertFalse(any(button.key in {"generate_weekly_themes", "load_weekly_contents"} for button in app.button))
            app.run()
            self.assertEqual(self.csv_bytes(), before_files)
            self.assert_no_generation_or_writes(state)

    def test_empty_saved_themes_offer_director_without_automatic_generation(self):
        with app_fixture(with_history=False) as state:
            path = Path("theme_db.csv")
            pd.read_csv(path, sep="|").iloc[0:0].to_csv(path, sep="|", index=False)
            app = state["app"].run()
            self.assert_no_errors(app)
            self.assertTrue(any("아직 채택한 테마가 없어요" in item.value for item in app.info))
            self.assertFalse(app.button(key="open_ai_director").disabled)
            self.assert_no_generation_or_writes(state)


if __name__ == "__main__":
    unittest.main()
