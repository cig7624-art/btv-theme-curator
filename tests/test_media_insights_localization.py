"""Korean presentation is cached by source version and grounded per document."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai.lib._pydantic import to_strict_json_schema

from media_insights_localization import (
    MAX_BATCH_ITEMS, MAX_DOCUMENT_CHARS, MAX_INPUT_CHARS,
    MediaLocalizationError, localize_media_items, localization_candidates,
    valid_localization,
)


NOW = "2026-10-24T08:00:00+00:00"


def source(identifier="source-a", text=None):
    text = text or "The advertising plan is available in the United States. Revenue was $100 million."
    return {"id": identifier, "company": "Netflix", "title": "Advertising plan update",
            "content_hash": "hash-" + identifier, "published_at": "2026-10-23",
            "collected_at": NOW, "excerpt": text, "body_text": text,
            "evidence": [{"id": identifier + "-p001", "text": text, "locator": "p1"}]}


def row(identifier="source-a", **changes):
    value = {"id": identifier, "title": "미국 광고형 상품 관련 실적 발표",
             "summary": "광고형 상품을 미국에서 제공하며 매출은 $100 million으로 발표했습니다."}
    value.update(changes)
    return value


def sdk_with(rows):
    sdk = Mock()
    sdk.responses.parse.return_value = SimpleNamespace(
        output_parsed={"items": rows}, usage=SimpleNamespace(input_tokens=100, output_tokens=40))
    return sdk


def saved(item, **changes):
    result = {"title": row()["title"], "summary": row()["summary"],
              "source_hash": item["content_hash"], "language": "ko", "generated_at": NOW, "model": "model"}
    result.update(changes)
    return result


class MediaInsightsLocalizationTests(unittest.TestCase):
    def test_actual_structured_batch_uses_only_permitted_ids_and_records_returned_response(self):
        item, sdk = source(), sdk_with([row()])
        before = deepcopy(item)
        with patch("media_insights_localization.record_llm_response") as record:
            result = localize_media_items([item], api_key="test", model="model", client=sdk, now=NOW)
        sdk.responses.parse.assert_called_once()
        record.assert_called_once_with(sdk.responses.parse.return_value, feature="media_localization", model="model")
        self.assertTrue(result["api_called"])
        self.assertEqual(result["requested_ids"], [item["id"]])
        self.assertEqual(result["failed_ids"], [])
        localized = result["localizations"][item["id"]]
        self.assertEqual(localized["source_hash"], item["content_hash"])
        self.assertEqual(localized["language"], "ko")
        self.assertEqual(localized["generated_at"], NOW)
        self.assertEqual(item, before)
        arguments = sdk.responses.parse.call_args.kwargs
        schema = to_strict_json_schema(arguments["text_format"])
        self.assertIn('"const": "source-a"', json.dumps(schema))
        self.assertNotIn('"default":', json.dumps(schema))
        self.assertIn("명령", arguments["input"][0]["content"])
        self.assertEqual(json.loads(arguments["input"][1]["content"])["documents"][0]["id"], item["id"])

    def test_bad_source_hash_language_timestamp_or_model_invalidates_saved_translation(self):
        item = source()
        item["localization"] = saved(item)
        self.assertIsNotNone(valid_localization(item))
        for change in ({"source_hash": "stale"}, {"language": "en"}, {"generated_at": "invalid"}, {"model": ""},
                       {"title": "English title"}, {"summary": "An English summary."}):
            with self.subTest(change=change):
                item["localization"] = saved(item, **change)
                self.assertIsNone(valid_localization(item))
        item["localization"] = saved(item)
        item["content_hash"] = "changed-source"
        self.assertEqual(localization_candidates({"items": [item]})[0]["id"], item["id"])

    def test_unrelated_source_cannot_supply_a_rows_number_or_currency(self):
        a, b = source(), source("source-b", "Revenue was $900 million.")
        invalid = ["매출은 $101 million입니다.", "매출은 100 million원입니다.",
                   "매출은 $100 billion입니다.", "매출은 $900 million입니다."]
        for summary in invalid:
            with self.subTest(summary=summary):
                result = localize_media_items([a, b], client=sdk_with([
                    row(summary=summary), row("source-b", summary="매출은 $900 million입니다.")]), now=NOW)
                self.assertEqual(set(result["localizations"]), {"source-b"})
                self.assertEqual(result["failed_ids"], ["source-a"])

    def test_whole_company_revenue_and_active_users_keep_their_scope(self):
        cases = [
            ("Amazon reported revenue of $100 million.", "프라임 비디오 매출은 $100 million입니다."),
            ("The service had 100 million monthly active viewers.", "유료 가입자는 100 million명입니다."),
        ]
        for text, summary in cases:
            with self.subTest(summary=summary):
                item = source(text=text)
                result = localize_media_items([item], client=sdk_with([row(summary=summary)]), now=NOW)
                self.assertEqual(result["localizations"], {})
                self.assertEqual(result["failed_ids"], [item["id"]])

    def test_the_supplied_opening_excerpt_is_valid_evidence_for_its_own_document(self):
        item = source(text="The company announced an advertising partnership for its streaming service.")
        item["excerpt"] = "Revenue was $123.4 million."
        good = row(summary="공식 발표에서 매출은 $123.4 million으로 제시했습니다.")
        result = localize_media_items([item], client=sdk_with([good]), now=NOW)
        self.assertEqual(result["failed_ids"], [])
        item["localization"] = result["localizations"][item["id"]]
        self.assertIsNotNone(valid_localization(item))
        bad = row(summary="공식 발표에서 매출은 $999 million으로 제시했습니다.")
        item.pop("localization")
        self.assertEqual(localize_media_items([item], client=sdk_with([bad]), now=NOW)["failed_ids"], [item["id"]])

    def test_unknown_duplicate_missing_or_malformed_rows_do_not_discard_good_siblings(self):
        a, b, c = source(), source("source-b"), source("source-c")
        result = localize_media_items([a, b, c], client=sdk_with([
            row(), row("source-b"), row("source-b"), row("not-a-document"),
            {"id": "source-c", "title": "제목만 존재"},
        ]), now=NOW)
        self.assertEqual(set(result["localizations"]), {"source-a"})
        self.assertEqual(result["failed_ids"], ["source-b", "source-c"])

    def test_saved_translation_korean_analysis_and_original_korean_are_free_to_reuse(self):
        translated, analyzed, korean = source(), source("source-b"), source("source-c", "광고형 상품을 새롭게 출시했습니다.")
        translated["localization"] = saved(translated)
        analyzed["analysis"] = {"headline": row()["title"], "summary": row()["summary"],
            "source_hash": analyzed["content_hash"], "generated_at": NOW, "model": "model",
            "facts": [{"text": "매출은 $100 million입니다.", "evidence_ids": ["source-b-p001"]}]}
        korean["title"] = "광고형 상품 새롭게 출시"
        sdk = sdk_with([])
        result = localize_media_items([translated, analyzed, korean], client=sdk, now=NOW)
        sdk.responses.parse.assert_not_called()
        self.assertFalse(result["api_called"])
        self.assertEqual(len(result["localizations"]), 3)
        self.assertEqual(result["localizations"][analyzed["id"]]["method"], "analysis_reuse")
        self.assertEqual(result["localizations"][korean["id"]]["method"], "source_korean")
        self.assertEqual(localization_candidates({"items": [translated, analyzed, korean]}), [])

    def test_analysis_with_foreign_evidence_is_not_reused(self):
        item = source()
        item["analysis"] = {"headline": row()["title"], "summary": row()["summary"],
            "source_hash": item["content_hash"], "generated_at": NOW, "model": "model",
            "facts": [{"text": "관련 발표가 있습니다.", "evidence_ids": ["foreign-p001"]}]}
        self.assertIsNone(valid_localization(item))
        item["analysis"]["facts"][0]["evidence_ids"] = ["source-metadata"]
        self.assertIsNone(valid_localization(item))

    def test_batch_size_and_prompt_character_limits_with_no_automatic_retry(self):
        items = [source(f"source-{index}", "Advertising plan update. " * 500) for index in range(25)]
        for item in items:
            item["evidence"] = [{"id": f"{item['id']}-p{n}", "text": "Streaming advertising plan. " * 100} for n in range(12)]
        sdk = sdk_with([])
        result = localize_media_items(items, client=sdk, now=NOW)
        sdk.responses.parse.assert_called_once()
        self.assertLessEqual(len(result["requested_ids"]), MAX_BATCH_ITEMS)
        self.assertEqual(len(result["requested_ids"]) + len(result["deferred_ids"]), len(items))
        payload = sdk.responses.parse.call_args.kwargs["input"][1]["content"]
        self.assertLessEqual(len(payload), MAX_INPUT_CHARS)
        self.assertTrue(all(len(json.dumps(doc, ensure_ascii=False)) <= MAX_DOCUMENT_CHARS
                            for doc in json.loads(payload)["documents"]))

    def test_candidates_deduplicate_limit_and_never_call_model(self):
        item = source()
        snapshot = {"items": [item, deepcopy(item), source("source-b"), source("source-c")]}
        before = deepcopy(snapshot)
        self.assertEqual(localization_candidates(snapshot, limit=0), [])
        self.assertEqual([x["id"] for x in localization_candidates(snapshot, limit=2)], ["source-a", "source-b"])
        self.assertEqual(snapshot, before)

    def test_missing_key_provider_error_and_bad_output_are_safe_without_retries(self):
        with self.assertRaises(MediaLocalizationError) as caught:
            localize_media_items([source()])
        self.assertEqual(caught.exception.code, "api_key_missing")
        sdk = sdk_with([])
        sdk.responses.parse.side_effect = RuntimeError("SECRET_PROVIDER_BODY")
        with self.assertRaises(MediaLocalizationError) as caught:
            localize_media_items([source()], client=sdk)
        sdk.responses.parse.assert_called_once()
        self.assertEqual(caught.exception.code, "provider_error")
        self.assertNotIn("SECRET_PROVIDER_BODY", str(caught.exception))
        sdk = sdk_with([])
        sdk.responses.parse.return_value.output_parsed = None
        with patch("media_insights_localization.record_llm_response") as record:
            with self.assertRaises(MediaLocalizationError):
                localize_media_items([source()], client=sdk)
        record.assert_called_once()


if __name__ == "__main__":
    unittest.main()
