"""Grounded research retrieval and explicit cached translation integration."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from media_insights_runtime import related_items, render_media_insights_tab


class RelatedResearchTests(unittest.TestCase):
    def test_requested_company_and_subject_outrank_current_story_topic(self):
        selected = {"id": "selected", "topic": "partnership_distribution"}
        rows = [dict(id="other-merger", company="Netflix", topic="partnership_distribution", evidence=[{}]),
                dict(id="amazon-earnings", company="Amazon", topic="earnings_strategy", evidence=[{}]),
                dict(id="disney-earnings", company="Disney", topic="earnings_strategy", evidence=[{}])]
        result = related_items({"items": rows}, selected, "아마존 실적은 무엇이 달라졌나요?", limit=1)
        self.assertEqual([item["id"] for item in result], ["amazon-earnings"])

    def test_plain_analysis_stays_on_topic_and_excludes_selected_or_empty_docs(self):
        selected = dict(id="selected", topic="sports_content", evidence=[{}])
        rows = [selected, dict(id="no-evidence", topic="sports_content"),
                dict(id="unrelated", topic="earnings_strategy", evidence=[{}]),
                dict(id="sports", topic="sports_content", evidence=[{}])]
        self.assertEqual([item["id"] for item in related_items({"items": rows}, selected)], ["sports"])


class MediaLocalizationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("/tmp/isolated-media-test/media_insights_snapshot.json")
        self.item = {"id": "official-source", "title": "Advertising plan launch", "company": "Netflix",
            "content_hash": "version-a", "published_at": "2026-10-23", "collected_at": "2026-10-24",
            "excerpt": "The company introduced an advertising plan.",
            "evidence": [{"id": "official-source-p001", "text": "The company introduced an advertising plan."}]}
        self.snapshot = {"schema_version": 1, "items": [self.item], "sources": []}
        self.translation = {"title": "광고형 상품 출시", "summary": "광고형 상품을 출시했습니다.",
            "source_hash": "version-a", "language": "ko", "generated_at": "2026-10-24T00:00:00+00:00", "model": "model"}

    def test_render_only_registers_callbacks_without_model_collection_or_save(self):
        with (patch("media_insights_runtime.load_snapshot", return_value=deepcopy(self.snapshot)),
              patch("media_insights_runtime.render_media_insights_page") as page,
              patch("media_insights_runtime.localize_media_items") as translate,
              patch("media_insights_runtime.persist_media_localizations") as save,
              patch("media_insights_runtime.collect_media_insights") as collect,
              patch("media_insights_runtime.analyze_media_item") as analyze,
              patch("media_insights_runtime.answer_media_question") as ask):
            render_media_insights_tab(api_key="test-key", model="model", github_config={}, snapshot_path=self.path)
        self.assertEqual(page.call_args.args[0], self.snapshot)
        self.assertTrue(callable(page.call_args.kwargs["on_localize"]))
        for method in (translate, save, collect, analyze, ask):
            method.assert_not_called()

    def test_explicit_action_uses_current_source_version_and_persists_only_media_json(self):
        current = deepcopy(self.snapshot)
        current["items"][0]["content_hash"] = "fresh-version"
        translation = {**self.translation, "source_hash": "fresh-version"}
        mapping = {self.item["id"]: translation}
        localized = deepcopy(current)
        localized["items"][0]["localization"] = translation
        with (patch("media_insights_runtime.load_snapshot", side_effect=[deepcopy(self.snapshot), current]),
              patch("media_insights_runtime.render_media_insights_page") as page,
              patch("media_insights_runtime.localize_media_items", return_value={
                  "localizations": mapping, "failed_ids": [], "api_called": True}) as translate,
              patch("media_insights_runtime.persist_media_localizations", return_value={
                  "snapshot": localized, "status": "success"}) as save,
              patch("media_insights_runtime.persist_media_snapshot") as collect_save,
              patch("media_insights_runtime.persist_media_analysis") as note_save):
            render_media_insights_tab(api_key="test-key", model="model", github_config={"repo": "test/repo"},
                                      snapshot_path=self.path)
            result = page.call_args.kwargs["on_localize"]()
        translate.assert_called_once_with(current["items"], api_key="test-key", model="model")
        save.assert_called_once_with(mapping, snapshot_path=self.path,
                                     github_config={"repo": "test/repo", "media_insights_path": self.path.name})
        self.assertEqual(result, localized)
        collect_save.assert_not_called()
        note_save.assert_not_called()

    def test_already_cached_versions_do_not_repeat_model_or_storage_on_action(self):
        snapshot = deepcopy(self.snapshot)
        snapshot["items"][0]["localization"] = self.translation
        with (patch("media_insights_runtime.load_snapshot", return_value=snapshot),
              patch("media_insights_runtime.render_media_insights_page") as page,
              patch("media_insights_runtime.localize_media_items") as translate,
              patch("media_insights_runtime.persist_media_localizations") as save):
            render_media_insights_tab(api_key="test-key", model="model", github_config={}, snapshot_path=self.path)
            result = page.call_args.kwargs["on_localize"]()
        self.assertEqual(result, snapshot)
        translate.assert_not_called()
        save.assert_not_called()

    def test_no_credentials_offer_no_paid_callback(self):
        with (patch("media_insights_runtime.load_snapshot", return_value=self.snapshot),
              patch("media_insights_runtime.render_media_insights_page") as page):
            render_media_insights_tab(api_key="", model="model", github_config={}, snapshot_path=self.path)
        self.assertIsNone(page.call_args.kwargs["on_localize"])
        self.assertIsNone(page.call_args.kwargs["on_analyze"])
        self.assertIsNone(page.call_args.kwargs["on_ask"])


if __name__ == "__main__":
    unittest.main()
