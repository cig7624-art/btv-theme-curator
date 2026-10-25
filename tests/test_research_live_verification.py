"""The real browser probe cannot repeat paid questions or leak prose/codes."""
from copy import deepcopy
from unittest.mock import Mock, patch
import unittest

from scripts.verify_deployment import VerificationIssue
from scripts.verify_research_live import (memory_answer_supports_context, public_source_hosts,
                                          safe_result_metadata, submit_question_once, _recovery_code)


def report():
    return {"checks": {}, "counts": {"questions_attempted": 0}}


def metadata():
    return {"fact_count": "2", "source_count": "3", "option_count": "2", "added_documents": "1",
            "status": "grounded", "search_status": "enriched", "engine_version": "business-research-v1", "model": "gpt-test"}


class ResearchLiveVerificationTests(unittest.TestCase):
    def test_recovery_waits_for_lazily_rendered_code_after_project_state_is_ready(self):
        frame = Mock()
        code = "rp_" + "A" * 43
        frame.locator.return_value.all_text_contents.side_effect = [[], ["Loading code"], ["\n" + code + "\n"]]
        with (patch("scripts.verify_research_live._management"),
              patch("scripts.verify_research_live.time.monotonic", side_effect=[100, 100, 100.2, 100.4])):
            self.assertEqual(_recovery_code(frame), code)
        self.assertEqual(frame.page.wait_for_timeout.call_count, 2)

    def test_missing_recovery_code_expires_without_question_submission_or_prose_in_error(self):
        frame = Mock()
        frame.locator.return_value.all_text_contents.return_value = ["non-code private prose"]
        with (patch("scripts.verify_research_live._management"),
              patch("scripts.verify_research_live.time.monotonic", side_effect=[100, 100, 131])):
            with self.assertRaisesRegex(VerificationIssue, "^project_recovery_code_missing$"):
                _recovery_code(frame)
        frame.get_by_role.assert_not_called()

    def test_two_questions_are_attempted_once_and_later_repeats_are_rejected(self):
        composer, result = Mock(), report()
        for position in (1, 2):
            submit_question_once(composer, result, position, "test question")
        self.assertEqual(composer.press.call_count, 2)
        self.assertEqual(result["counts"]["questions_attempted"], 2)
        for position in (1, 2, 3, True):
            with self.assertRaises(VerificationIssue):
                submit_question_once(composer, result, position, "do not retry")
        self.assertEqual(composer.press.call_count, 2)

    def test_uncertain_browser_failure_is_recorded_before_enter_and_never_retried(self):
        composer, result = Mock(), report()
        composer.press.side_effect = RuntimeError("uncertain browser result")
        with self.assertRaises(RuntimeError):
            submit_question_once(composer, result, 1, "question")
        self.assertTrue(result["checks"]["question_1_attempted"])
        self.assertEqual(result["counts"]["questions_attempted"], 1)
        with self.assertRaises(VerificationIssue):
            submit_question_once(composer, result, 1, "question")
        composer.press.assert_called_once()

    def test_disabled_composer_does_not_use_budget_or_press_enter(self):
        composer, result = Mock(), report()
        composer.is_enabled.return_value = False
        with self.assertRaisesRegex(VerificationIssue, "research_composer_disabled"):
            submit_question_once(composer, result, 1, "question")
        self.assertEqual(result["counts"]["questions_attempted"], 0)
        composer.press.assert_not_called()

    def test_metadata_contains_only_fixed_labels_counts_and_safe_model_version(self):
        raw = {**metadata(), "answer": "private prose", "recovery_code": "private key"}
        parsed = safe_result_metadata(raw)
        self.assertEqual(parsed["fact_count"], 2)
        self.assertNotIn("answer", parsed)
        self.assertNotIn("recovery_code", parsed)

    def test_malformed_metadata_cannot_escape_or_claim_completed_results(self):
        for key, value in (("fact_count", -1), ("source_count", True), ("option_count", "NaN"),
                           ("added_documents", "100001"), ("model", "secret\nprovider-response"),
                           ("engine_version", "raw response"), ("status", "unknown"),
                           ("search_status", "error with private details")):
            with self.subTest(key=key):
                raw = deepcopy(metadata()); raw[key] = value
                self.assertIsNone(safe_result_metadata(raw))

    def test_memory_check_requires_both_business_target_and_earlier_decision(self):
        self.assertTrue(memory_answer_supports_context("가족 나침반 기준에 따라 가족 고객을 대상으로 가격 확정은 보류하고 수용도를 검증합니다."))
        self.assertFalse(memory_answer_supports_context("가족 고객에게 어울리는 메시지입니다."))
        self.assertFalse(memory_answer_supports_context("가격 수용도 검증을 제안합니다."))
        self.assertFalse(memory_answer_supports_context("가족 고객을 대상으로 가격 수용도 검증을 제안합니다."))

    def test_source_report_records_public_hosts_never_urls_credentials_or_paths(self):
        urls = ["https://help.netflix.com/ko/node/plan?secret=not-exported",
                "https://help.netflix.com/another", "https://user:password@example.com/",
                "http://public.example/", "https://127.0.0.1/a", "https://localhost/a",
                "javascript:alert(1)", "https://private.internal/a"]
        self.assertEqual(public_source_hosts(urls), ["help.netflix.com"])


if __name__ == "__main__":
    unittest.main()
