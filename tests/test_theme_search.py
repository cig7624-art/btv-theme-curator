"""Intent ranking stays identical to the frozen pre-optimization app functions."""
import ast
from difflib import SequenceMatcher
from pathlib import Path
import random
import re
import unittest

import pandas as pd

from theme_search import _search_term_score, rank_themes_by_ai_intent


# Frozen from app.py before optimization. AST loading avoids running the app or
# importing Streamlit and keeps the oracle independent of the optimized helper.
_BASELINE_SOURCE = r'''
def _normalize_search_text(value):
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())

def _search_term_score(term, field_value, exact_weight):
    term_text = str(term or "").strip().lower()
    field_text = str(field_value or "").strip().lower()
    if len(term_text) < 2 or not field_text:
        return 0.0
    term_norm = _normalize_search_text(term_text)
    field_norm = _normalize_search_text(field_text)
    if not term_norm or not field_norm:
        return 0.0
    if term_norm in field_norm:
        return float(exact_weight)
    field_tokens = [token for token in re.findall(r"[0-9a-z가-힣]+", field_text) if len(token) >= 2]
    best = 0.0
    for token in field_tokens:
        ratio = SequenceMatcher(None, term_norm, _normalize_search_text(token)).ratio()
        best = max(best, ratio)
    if best >= 0.86:
        return exact_weight * 0.55
    if best >= 0.76:
        return exact_weight * 0.25
    return 0.0

def rank_themes_by_ai_intent(themes, intent):
    if not intent:
        return themes.copy()
    weighted_terms = []
    for key, weight in [
        ("genres", 12), ("subjects", 11), ("narrative_elements", 10),
        ("moods", 8), ("positive_keywords", 8), ("anchor_titles", 3),
    ]:
        for value in intent.get(key, []) or []:
            if str(value).strip():
                weighted_terms.append((str(value).strip(), weight))
    negative_terms = [str(value).strip() for value in (intent.get("negative_keywords", []) or []) if str(value).strip()]
    scored_rows = []
    for _, row in themes.iterrows():
        fields = {
            "theme_name": row.get("theme_name", ""), "copy": row.get("copy", ""),
            "trigger_keywords": row.get("trigger_keywords", ""), "genre": row.get("genre", ""),
            "mood": row.get("mood", ""), "source_issue": row.get("source_issue", ""),
        }
        score = 0.0
        matched = []
        for term, base_weight in weighted_terms:
            term_score = max(
                _search_term_score(term, fields["theme_name"], base_weight * 1.35),
                _search_term_score(term, fields["copy"], base_weight * 1.15),
                _search_term_score(term, fields["trigger_keywords"], base_weight * 1.25),
                _search_term_score(term, fields["genre"], base_weight),
                _search_term_score(term, fields["mood"], base_weight * 0.85),
                _search_term_score(term, fields["source_issue"], base_weight * 0.55),
            )
            if term_score > 0:
                score += term_score
                matched.append(term)
        combined_text = " ".join(str(value) for value in fields.values()).lower()
        for negative in negative_terms:
            if _normalize_search_text(negative) in _normalize_search_text(combined_text):
                score -= 25
        unique_matches = len({_normalize_search_text(value) for value in matched if value})
        score += min(18, unique_matches * 3)
        if score > 0:
            item = row.copy()
            item["ai_search_score"] = round(score, 2)
            item["ai_matched_terms"] = ",".join(dict.fromkeys(matched))
            scored_rows.append(item)
    if not scored_rows:
        return themes.iloc[0:0].copy()
    result = pd.DataFrame(scored_rows)
    return result.sort_values(["ai_search_score", "theme_name"], ascending=[False, True])
'''
_BASELINE = {"pd": pd, "re": re, "SequenceMatcher": SequenceMatcher}
exec(compile(ast.parse(_BASELINE_SOURCE), "frozen_app_search", "exec"), _BASELINE)


def ten_term_intent():
    return {
        "genres": ["스릴러", "드라마"], "subjects": ["가족", "복수"],
        "narrative_elements": ["반전", "성장"], "moods": ["긴장감"],
        "positive_keywords": ["추리", "미스터리"], "anchor_titles": ["괴물"],
        "negative_keywords": ["키즈", "호러"],
    }


class ThemeSearchTests(unittest.TestCase):
    def assert_baseline_parity(self, themes, intent):
        before = themes.copy(deep=True)
        expected = _BASELINE["rank_themes_by_ai_intent"](themes, intent)
        actual = rank_themes_by_ai_intent(themes, intent)
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
        pd.testing.assert_frame_equal(themes, before, check_exact=True)
        return actual

    def test_current_theme_catalogue_has_exact_parity_for_several_intents(self):
        themes = pd.read_csv(Path(__file__).resolve().parents[1] / "theme_pool.csv", sep="|").fillna("")
        intents = [
            ten_term_intent(),
            {"genres": ["로맨스", "코미디"], "subjects": ["첫사랑", "여행"], "negative_keywords": ["호러", "가족"]},
            {"genres": ["공포", "공포"], "positive_keywords": ["공 포", "스릴러", "공포"], "negative_keywords": ["가족", "가족"]},
            {"positive_keywords": [None, "", " ", 0, False, "Été", "東京", "😱😱", "!A"], "negative_keywords": ["👻"]},
        ]
        for intent in intents:
            with self.subTest(intent=intent):
                self.assert_baseline_parity(themes, intent)

    def test_ties_duplicate_indices_extra_columns_and_existing_score_columns(self):
        themes = pd.DataFrame([
            dict(theme_id="T1", theme_name="같은 이름", copy="복수 가족", genre="스릴러", extra=2, type="영화", year="2006"),
            dict(theme_id="T2", theme_name="같은 이름", copy="복수 가족", genre="스릴러", extra=1, type="드라마", year="2021"),
            dict(theme_id="T3", theme_name="가족", copy="반전과 성장", genre="드라마", extra=3),
        ], index=pd.Index([7, 7, 2], name="saved_index"))
        themes["ai_search_score"] = -1
        themes["ai_matched_terms"] = "old"
        result = self.assert_baseline_parity(themes, ten_term_intent())
        self.assertEqual(result.loc[result["theme_id"].isin(["T1", "T2"]), "theme_id"].tolist(), ["T1", "T2"])

    def test_unicode_empty_values_and_normalized_term_collisions(self):
        themes = pd.DataFrame([
            dict(theme_name="A", copy="ABC-def 소름", trigger_keywords="공포", genre=None, mood=0, source_issue=float("nan")),
            dict(theme_name="CAFÉ", copy="cafe 東京 😱", trigger_keywords="", genre=False, mood=None, source_issue="Été"),
            dict(theme_name="", copy=None, trigger_keywords="", genre="", mood="", source_issue=""),
        ])
        intent = {"genres": ["A", "!A", "abc def", "ABC-def", "공 포", "공포"],
                  "positive_keywords": ["CAFÉ", "cafe", "東京", "😱😱", None, False, 0, ""],
                  "negative_keywords": ["없는 값", "   "]}
        self.assert_baseline_parity(themes, intent)
        # A punctuation-only negative keeps its original empty-normalized match.
        intent["negative_keywords"] = ["!", "!", "!"]
        self.assert_baseline_parity(themes, intent)

    def test_empty_and_no_match_results_preserve_columns_dtypes_and_index(self):
        themes = pd.DataFrame({"theme_name": ["가족"], "ordinal": pd.Series([1], dtype="int64")},
                              index=pd.Index([0], name="saved_index"))
        for intent in ({"genres": ["zzzzzz"]}, {"negative_keywords": ["가족"]}, {"interpreted_request": "empty"}):
            with self.subTest(intent=intent):
                result = self.assert_baseline_parity(themes, intent)
                self.assertTrue(result.empty)
                self.assertEqual(result.columns.tolist(), themes.columns.tolist())
        self.assert_baseline_parity(themes.iloc[0:0], ten_term_intent())

    def test_absent_intent_returns_an_independent_copy(self):
        themes = pd.DataFrame({"theme_name": ["복수"]})
        for intent in (None, {}):
            result = self.assert_baseline_parity(themes, intent)
            result.loc[0, "theme_name"] = "changed"
            self.assertEqual(themes.loc[0, "theme_name"], "복수")

    def test_mutating_themes_and_intent_is_visible_on_the_next_call(self):
        themes = pd.DataFrame({"theme_name": ["복수"], "genre": ["스릴러"]})
        intent = {"genres": ["스릴러"], "positive_keywords": ["복수"]}
        first = self.assert_baseline_parity(themes, intent)
        themes.loc[0, ["theme_name", "genre"]] = ["여행", "힐링"]
        intent["genres"][:] = ["힐링"]
        intent["positive_keywords"][:] = ["여행", "여행"]
        second = self.assert_baseline_parity(themes, intent)
        self.assertEqual(first.iloc[0]["ai_matched_terms"], "스릴러,복수")
        self.assertEqual(second.iloc[0]["ai_matched_terms"], "힐링,여행")

    def test_single_field_scores_match_exact_fuzzy_and_short_term_boundaries(self):
        cases = [
            ("공 포", "공포 영화", 12 * 1.35),
            ("abcdefghx", "abcdefghy", 11 * 1.15),
            ("abcdex", "abcdey", 8 * 1.25),
            ("abcx", "abcy", 3 * 0.55),
            ("A", "a", 12), ("!A", "a", 12), ("😱😱", "😱😱", 12),
            (None, "None", 8), ("nan", float("nan"), 8),
            ("東京", "東京", 8), ("Été", "CAFÉ été", 8), ("None", None, 8),
        ]
        for term, field, weight in cases:
            with self.subTest(term=term, field=field):
                self.assertEqual(_search_term_score(term, field, weight),
                                 _BASELINE["_search_term_score"](term, field, weight))

    def test_single_field_random_tokens_and_long_autojunk_inputs_match(self):
        generator = random.Random(91)
        alphabet = "abcdef가나다라마 123-!É"
        cases = [("a" * 210 + "bc", "a" * 210 + "bd"),
                 ("a" * 210 + "bc", "bd" + "a" * 210)]
        cases.extend(("".join(generator.choices(alphabet, k=generator.randrange(1, 15))),
                      "".join(generator.choices(alphabet, k=generator.randrange(1, 45))))
                     for _ in range(100))
        for term, field in cases:
            self.assertEqual(_search_term_score(term, field, 12 * 1.35),
                             _BASELINE["_search_term_score"](term, field, 12 * 1.35))


if __name__ == "__main__":
    unittest.main()
