"""The fifth tab preserves existing actions and calls paid engines only on intent."""
from copy import deepcopy
from unittest.mock import patch
import unittest

from test_theme_actions import app_fixture


ITEM = {
    "id": "official-report", "company": "Disney", "source_id": "disney_news",
    "source_name": "Disney News", "source_type": "official_announcement",
    "topic": "partnership_distribution", "title": "Official creative partnership",
    "url": "https://thewaltdisneycompany.com/news/partnership/",
    "published_at": "2026-10-15T20:00:00+00:00", "country": "", "period": "",
    "content_hash": "source-version", "excerpt": "A company-wide creative partnership.",
    "evidence": [{"id": "official-report-p001", "text": "A company-wide creative partnership.",
                  "locator": "본문 1문단", "url": "https://thewaltdisneycompany.com/news/partnership/"}],
}
SNAPSHOT = {"schema_version": 1, "collected_at": "2026-10-23T00:00:00+00:00", "sources": [], "items": [ITEM]}
ANALYSIS = {
    "headline": "디즈니의 크리에이터 제휴", "summary": "공식 크리에이터 제휴 발표입니다.",
    "facts": [{"text": "전사적인 크리에이티브 제휴를 발표했습니다.", "evidence_ids": ["official-report-p001"]}],
    "interpretation": "제휴의 활용 범위를 검토할 수 있습니다.",
    "btv_options": ["기존 파트너와 공동 기획 가능성을 검토합니다."],
    "questions": ["한국 적용 범위는 별도 확인이 필요합니다."], "comparison": [],
    "generated_at": "2026-10-23T00:01:00+00:00", "model": "test-model",
    "source_hash": "source-version", "context_hashes": {},
}


class MediaInsightsAppTests(unittest.TestCase):
    def test_navigation_and_filters_never_collect_or_call_models(self):
        with (patch("media_insights_runtime.load_snapshot", return_value=deepcopy(SNAPSHOT)),
              patch("media_insights_runtime.collect_media_insights") as collect,
              patch("media_insights_runtime.localize_media_items") as localize,
              patch("media_insights_runtime.persist_media_localizations") as localization_save,
              patch("media_insights_runtime.analyze_media_item") as analyze,
              patch("media_insights_runtime.answer_media_question") as ask,
              app_fixture() as state):
            app = state["app"]
            self.assertFalse(app.exception)
            self.assertEqual([tab.label for tab in app.tabs],
                             ["지금 주목할 이야기", "추천 테마", "OTT는 지금", "AI 디렉터", "미디어 인사이트"])
            app.radio(key="media_insight_topic").set_value("제휴·유통").run()
            app.selectbox(key="media_insight_period").set_value("최근 30일").run()
            next(widget for widget in app.get("button_group") if widget.key == "media_insight_scope").set_value("국내 상품 이슈").run()
            next(widget for widget in app.get("button_group") if widget.key == "media_insight_scope").set_value("주요 이슈").run()
            app.run()
            self.assertFalse(app.exception)
            for callback in (collect, analyze, ask, localize, localization_save):
                callback.assert_not_called()
            state["generate_weekly_themes"].assert_not_called()
            state["persist_files_to_github"].assert_not_called()

    def test_explicit_korean_action_saves_once_then_rerenders_without_paid_calls(self):
        current = deepcopy(SNAPSHOT)
        localized = {"title": "디즈니의 크리에이터 제휴", "summary": "전사적인 크리에이티브 제휴를 발표했습니다.",
            "source_hash": ITEM["content_hash"], "language": "ko", "generated_at": "2026-10-24T00:00:00+00:00",
            "model": "test-model"}
        translations = {ITEM["id"]: localized}
        def persist(mapping, **kwargs):
            current["items"][0]["localization"] = deepcopy(mapping[ITEM["id"]])
            return {"status": "success", "snapshot": deepcopy(current)}
        with (patch("media_insights_runtime.load_snapshot", side_effect=lambda *args: deepcopy(current)),
              patch("media_insights_runtime.localize_media_items", return_value={
                  "localizations": translations, "failed_ids": [], "requested_ids": [ITEM["id"]],
                  "api_called": True}) as localize,
              patch("media_insights_runtime.persist_media_localizations", side_effect=persist) as save,
              patch("media_insights_runtime.collect_media_insights") as collect,
              patch("media_insights_runtime.analyze_media_item") as analyze,
              patch("media_insights_runtime.answer_media_question") as ask,
              app_fixture() as state):
            app = state["app"]
            self.assertFalse(app.exception)
            localize.assert_not_called()
            app.button(key="media_insights_localize").click().run()
            self.assertFalse(app.exception)
            localize.assert_called_once()
            self.assertEqual([item["id"] for item in localize.call_args.args[0]], [ITEM["id"]])
            save.assert_called_once()
            self.assertEqual(save.call_args.args, (translations,))
            self.assertEqual(save.call_args.kwargs["snapshot_path"].name, "media_insights_snapshot.json")
            self.assertIn(localized["title"], " ".join(element.value for element in app.markdown))
            app.run()
            next(widget for widget in app.get("button_group") if widget.key == "media_insight_scope").set_value("국내 상품 이슈").run()
            next(widget for widget in app.get("button_group") if widget.key == "media_insight_scope").set_value("주요 이슈").run()
            self.assertFalse(app.exception)
            self.assertFalse(any(button.key == "media_insights_localize" for button in app.button))
            localize.assert_called_once()
            save.assert_called_once()
            for callback in (collect, analyze, ask):
                callback.assert_not_called()
            state["generate_weekly_themes"].assert_not_called()
            state["persist_files_to_github"].assert_not_called()
            state["persist_content_candidates_to_github"].assert_not_called()

    def test_explicit_analysis_saves_only_insight_note_not_theme_data(self):
        with (patch("media_insights_runtime.load_snapshot", return_value=deepcopy(SNAPSHOT)),
              patch("media_insights_runtime.analyze_media_item", return_value=deepcopy(ANALYSIS)) as analyze,
              patch("media_insights_runtime.persist_media_analysis", return_value={"status": "success"}) as save,
              app_fixture() as state):
            app = state["app"]
            app.button(key="media_insights_analyze").click().run()
            self.assertFalse(app.exception)
            analyze.assert_called_once()
            self.assertEqual(analyze.call_args.args[0]["id"], ITEM["id"])
            save.assert_called_once()
            self.assertEqual(save.call_args.args, (ITEM["id"], ANALYSIS))
            self.assertIn("확인한 사실", " ".join(row.value for row in app.markdown))
            state["generate_weekly_themes"].assert_not_called()
            state["persist_files_to_github"].assert_not_called()
            state["persist_content_candidates_to_github"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
