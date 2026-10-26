"""Session counters observe returned responses without inventing billing data."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from api_usage import SESSION_KEY, read_session_usage, record_llm_response
from llm_theme_generator import (
    ThemeGenerationError, _generate_candidates, generate_content_candidates_for_themes,
    generate_director_plan, interpret_theme_search_query,
)


def response(inputs=120, outputs=30, cached=40):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=inputs, output_tokens=outputs,
                           input_tokens_details=SimpleNamespace(cached_tokens=cached)),
                           output_parsed=None, output="PRIVATE ANSWER", api_key="PRIVATE KEY")


class ApiUsageTests(unittest.TestCase):
    def record(self, value, state, feature="director"):
        record_llm_response(value, feature=feature, model="configured-model", state=state)

    def test_browsers_are_isolated_and_reads_are_detached_without_increment(self):
        first, second = {}, {}
        self.record(response(), first)
        self.record(response(80, 20, 10), first, "content_candidates")
        self.assertEqual(read_session_usage(second)["response_count"], 0)
        before = deepcopy(first)
        summary = read_session_usage(first)
        self.assertEqual(summary["response_count"], 2)
        self.assertEqual(summary["total_tokens"], 250)
        self.assertEqual(summary["cached_input_tokens"], 50)
        self.assertEqual(summary["features"]["director"], 1)
        self.assertEqual(summary["features"]["content_candidates"], 1)
        summary["features"]["director"] = 999
        self.assertEqual(first, before)
        self.assertEqual(read_session_usage(first)["response_count"], 2)

    def test_metadata_whitelist_never_retains_response_or_credentials(self):
        state = {}
        self.record(response(), state)
        stored = repr(state)
        self.assertNotIn("PRIVATE", stored)
        self.assertNotIn("configured-model", stored)
        self.assertEqual(set(state), {SESSION_KEY})

    def test_missing_or_invalid_usage_marks_unmetered_without_breaking_result(self):
        state = {}
        self.record(SimpleNamespace(usage=None), state)
        self.record({"usage": {"input_tokens": -5, "output_tokens": float("nan")}}, state)
        result = read_session_usage(state)
        self.assertEqual(result["response_count"], 2)
        self.assertEqual(result["unmetered_response_count"], 2)
        self.assertEqual(result["total_tokens"], 0)
        self.record(response(10, 5, 900), state)
        self.assertEqual(read_session_usage(state)["cached_input_tokens"], 10)

    def test_no_response_and_unknown_feature_are_not_counted(self):
        state = {}
        self.record(None, state)
        self.record(response(), state, "PRIVATE USER PROMPT")
        self.assertEqual(state, {})

    def test_no_streamlit_context_uses_no_global_state(self):
        with patch("api_usage._session_state", return_value=None):
            record_llm_response(response(), feature="director", model="model")
            self.assertEqual(read_session_usage()["response_count"], 0)

    def test_corrupt_previous_counter_is_safe_and_replaced_with_numeric_whitelist(self):
        state = {SESSION_KEY: {"response_count": "invalid", "input_tokens": -1,
                 "output_tokens": True, "cached_input_tokens": 99,
                 "features": {"director": -3, "SECRET": 100}, "prompt": "SECRET"}}
        self.record(response(), state)
        result = read_session_usage(state)
        self.assertEqual(result["response_count"], 1)
        self.assertEqual(result["total_tokens"], 150)
        self.assertNotIn("SECRET", repr(state))

    def test_all_four_sdk_entry_points_count_before_validation_and_skip_failed_requests(self):
        state = {}
        sdk = Mock()
        sdk.responses.parse.return_value = response()
        themes = pd.DataFrame(columns=["theme_id", "theme_name"])
        calls = [
            ("director", lambda: generate_director_plan(
                request="스릴러를 만들어줘", messages=[], drafts=[], themes=themes,
                contents=pd.DataFrame(), api_key="test", model="model", client=sdk)),
            ("weekly_themes", lambda: _generate_candidates(sdk, "model", [], pd.DataFrame(), 1)),
            ("theme_search", lambda: interpret_theme_search_query("공포", "test", "model")),
            ("content_candidates", lambda: generate_content_candidates_for_themes(
                [{"theme": {"theme_id": "T1", "theme_name": "가족의 비밀"}}], "test", "model")),
        ]
        with patch("api_usage._session_state", return_value=state), patch("llm_theme_generator.OpenAI", return_value=sdk):
            for feature, call in calls:
                with self.subTest(feature=feature), self.assertRaises(ThemeGenerationError):
                    call()
            self.assertEqual(read_session_usage(state)["response_count"], 4)
            self.assertTrue(all(read_session_usage(state)["features"][feature] == 1 for feature, _ in calls))
            sdk.responses.parse.side_effect = RuntimeError("connection failed")
            for feature, call in calls:
                with self.subTest(failed_feature=feature), self.assertRaises(ThemeGenerationError):
                    call()
            self.assertEqual(read_session_usage(state)["response_count"], 4)


if __name__ == "__main__":
    unittest.main()
