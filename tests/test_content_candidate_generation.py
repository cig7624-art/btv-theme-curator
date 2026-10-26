"""Regular recommendations keep an exact count without padding unknown titles."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api_usage import read_session_usage
from llm_theme_generator import (
    ThemeContentCandidateBatch,
    ThemeGenerationError,
    generate_content_candidates_for_themes,
)


TITLES = ["기생충", "마더", "올드보이", "장화, 홍련", "유전", "그을린 사랑",
          "나이브스 아웃", "오펀: 천사의 비밀", "식스 센스", "시크릿", "곡성", "추격자"]


def theme(key):
    return {"theme": {"theme_id": key, "theme_name": "가족이라는 미스터리",
                      "content_selection_rule": "가족의 비밀이 사건의 중심인 작품",
                      "content_exclusion_rule": "가족이 사건과 무관한 작품 제외"}}


def candidate(title):
    return {"title": title, "content_type": "영화", "year": "", "reason": "가족 사이에 감춰진 비밀"}


def response(*theme_sets, inputs=100, outputs=80):
    return SimpleNamespace(output_parsed=ThemeContentCandidateBatch(themes=[
        {"theme_key": key, "candidates": [candidate(title) for title in titles]}
        for key, titles in theme_sets
    ]), usage=SimpleNamespace(input_tokens=inputs, output_tokens=outputs,
                              total_tokens=inputs + outputs))


class ContentCandidateGenerationTests(unittest.TestCase):
    def test_default_limits_each_theme_to_ten_unique_candidates_in_one_response(self):
        sdk = Mock()
        sdk.responses.parse.return_value = response(("T1", TITLES), ("unknown", TITLES))
        with patch("llm_theme_generator.OpenAI", return_value=sdk):
            result, usage = generate_content_candidates_for_themes([theme("T1")], "test-key")

        self.assertEqual(list(result), ["T1"])
        self.assertEqual([item["title"] for item in result["T1"]], TITLES[:10])
        self.assertEqual(usage, {"input_tokens": 100, "output_tokens": 80,
                                 "total_tokens": 180, "api_call_count": 1})
        sdk.responses.parse.assert_called_once()
        prompt = sdk.responses.parse.call_args.kwargs["input"]
        payload = json.loads(prompt[-1]["content"])["themes"][0]
        self.assertEqual(payload["candidate_count"], 10)
        self.assertIn("정확히", prompt[0]["content"])
        self.assertIn("서비스", prompt[0]["content"])
        self.assertEqual(set(result["T1"][0]), {"title", "content_type", "year", "reason"})

    def test_duplicate_blocks_and_spelling_variants_refill_only_the_incomplete_theme(self):
        sdk = Mock()
        sdk.responses.parse.side_effect = [
            response(("T1", TITLES), ("T2", ["Alien", "ＡＬＩＥＮ", " ", *TITLES[:5]]),
                     ("T2", ["기 생충", TITLES[5]])),
            response(("T1", ["다른 작품"]), ("T2", [TITLES[0], *TITLES[6:9]]), inputs=60, outputs=30),
        ]
        state = {}
        with patch("llm_theme_generator.OpenAI", return_value=sdk), \
                patch("api_usage._session_state", return_value=state):
            result, usage = generate_content_candidates_for_themes([theme("T1"), theme("T2")], "test-key")

        self.assertEqual([item["title"] for item in result["T1"]], TITLES[:10])
        self.assertEqual([item["title"] for item in result["T2"]], ["Alien", *TITLES[:9]])
        refill = json.loads(sdk.responses.parse.call_args_list[1].kwargs["input"][-1]["content"])["themes"]
        self.assertEqual(len(refill), 1)
        self.assertEqual(refill[0]["theme_key"], "T2")
        self.assertEqual(refill[0]["candidate_count"], 3)
        self.assertEqual(refill[0]["excluded_titles"], ["Alien", *TITLES[:6]])
        self.assertEqual(refill[0]["selection_rule"], "가족의 비밀이 사건의 중심인 작품")
        self.assertEqual(usage, {"input_tokens": 160, "output_tokens": 110,
                                 "total_tokens": 270, "api_call_count": 2})
        self.assertEqual(read_session_usage(state)["response_count"], 2)
        self.assertEqual(read_session_usage(state)["total_tokens"], 270)

    def test_missing_theme_and_repeated_shortage_fail_after_one_refill(self):
        sdk = Mock()
        sdk.responses.parse.side_effect = [response(("T1", TITLES[:8])), response(("T1", TITLES[:8]))]
        with patch("llm_theme_generator.OpenAI", return_value=sdk), \
                self.assertRaises(ThemeGenerationError) as error:
            generate_content_candidates_for_themes([theme("T1"), theme("T2")], "test-key")
        self.assertIn("T1 8/10", str(error.exception))
        self.assertIn("T2 0/10", str(error.exception))
        self.assertEqual(sdk.responses.parse.call_count, 2)

    def test_explicit_candidate_target_is_enforced_after_normalized_duplicates(self):
        sdk = Mock()
        sdk.responses.parse.side_effect = [response(("T1", [*TITLES[:3], "기 생충"])),
                                             response(("T1", TITLES[3:]))]
        with patch("llm_theme_generator.OpenAI", return_value=sdk):
            result, usage = generate_content_candidates_for_themes([theme("T1")], "test-key", per_theme=4)
        self.assertEqual([item["title"] for item in result["T1"]], TITLES[:4])
        self.assertEqual(usage["api_call_count"], 2)

    def test_empty_request_does_not_construct_a_client_or_require_a_key(self):
        with patch("llm_theme_generator.OpenAI") as sdk:
            result, usage = generate_content_candidates_for_themes([], "")
        self.assertEqual(result, {})
        self.assertEqual(usage, {"input_tokens": 0, "output_tokens": 0,
                                 "total_tokens": 0, "api_call_count": 0})
        sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
