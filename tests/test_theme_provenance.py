"""Origin filters preserve old adoptions and independent weekly recommendations."""
from copy import deepcopy
import unittest

import pandas as pd

from theme_provenance import (
    director_theme_mask, is_director_theme, weekly_recommendation_runs,
)


class ThemeProvenanceTests(unittest.TestCase):
    def test_current_and_legacy_origins_keep_the_original_dataframe_index(self):
        themes = pd.DataFrame([
            {"theme_id": "T1", "source_status": "AI_DIRECTOR"},
            {"theme_id": "T2", "source_status": "AI_GENERATED", "director_draft_id": "draft-old"},
            {"theme_id": "T3", "source_status": "AI_GENERATED", "director_adoption_key": "saved-key"},
            {"theme_id": "T4", "creation_origin": "AI_DIRECTOR"},
            {"theme_id": "T5", "recommendation_source": "AI_DIRECTOR"},
            {"theme_id": "T6", "source_status": "AI_GENERATED"},
        ], index=[8, 4, 4, 1, 12, 15])
        before = themes.copy(deep=True)
        mask = director_theme_mask(themes)
        self.assertEqual(mask.tolist(), [True, True, True, True, True, False])
        self.assertEqual(mask.index.tolist(), themes.index.tolist())
        self.assertEqual(mask.dtype, bool)
        pd.testing.assert_frame_equal(themes, before)

    def test_missing_values_and_words_in_a_theme_name_are_not_provenance(self):
        for missing in (None, float("nan"), pd.NA, pd.NaT, "", "  ", "nan", "NaN", "None", "null", "<NA>"):
            with self.subTest(missing=str(missing)):
                self.assertFalse(is_director_theme({"source_status": missing,
                    "director_draft_id": missing, "director_adoption_key": missing}))
        self.assertFalse(is_director_theme({"theme_name": "AI 디렉터가 등장하는 영화", "source_status": "AI_GENERATED"}))
        self.assertFalse(is_director_theme({"source_status": "DIRECTOR_LIKE"}))
        self.assertFalse(is_director_theme(None))

    def test_history_identifies_saved_legacy_themes_without_changing_csv_rows(self):
        themes = pd.DataFrame([{"theme_id": "T1"}, {"theme_id": "T2"}, {"theme_id": "T3"}, {"theme_id": ""}])
        history = pd.DataFrame([
            {"run_id": "AI-DIRECTOR-old-key", "theme_id": "T1"},
            {"run_id": "old-custom-run", "theme_id": "T2", "director_adoption_key": "old-key"},
            {"run_id": "weekly-run", "theme_id": "T3", "recommendation_source": "AI_GENERATED"},
            {"run_id": "AI-DIRECTOR-no-theme", "theme_id": float("nan")},
        ])
        before = history.copy(deep=True)
        self.assertEqual(director_theme_mask(themes, history).tolist(), [True, True, False, False])
        pd.testing.assert_frame_equal(history, before)

    def test_weekly_run_reusing_a_director_theme_remains_weekly(self):
        themes = pd.DataFrame([{"theme_id": "T1", "source_status": "AI_DIRECTOR", "director_draft_id": "D1"}])
        director = {"run_id": "AI-DIRECTOR-key", "recommendations": [{"theme": themes.iloc[0].to_dict()}]}
        weekly = {"run_id": "weekly-later", "recommendations": [{
            "recommendation_source": "EXISTING_DB", "theme": themes.iloc[0].to_dict()}]}
        history = pd.DataFrame([
            {"run_id": "AI-DIRECTOR-key", "theme_id": "T1", "director_adoption_key": "saved"},
            {"run_id": "weekly-later", "theme_id": "T1", "recommendation_source": "EXISTING_DB"},
        ])
        before = deepcopy([director, weekly])
        result = weekly_recommendation_runs([director, weekly], themes, history)
        self.assertEqual(result, [weekly])
        self.assertIs(result[0], weekly)
        self.assertEqual([director, weekly], before)

    def test_run_level_and_history_markers_filter_without_reordering_weekly_runs(self):
        runs = [
            {"run_id": "weekly-new"},
            {"run_id": "legacy-meta", "meta": {"director_draft_id": "D1"}},
            {"run_id": "legacy-source", "creation_origin": "AI_DIRECTOR"},
            {"run_id": "legacy-history"},
            {"run_id": "weekly-old", "meta": {"source_status": "AI_GENERATED"}},
            {"run_id": "legacy-meta-id", "meta": {"run_id": "AI-DIRECTOR-reserved"}},
        ]
        history = [{"run_id": "legacy-history", "theme_id": "T2", "recommendation_source": "AI_DIRECTOR"}]
        before = deepcopy(runs)
        kept = weekly_recommendation_runs(runs, history=history)
        self.assertEqual([run["run_id"] for run in kept], ["weekly-new", "weekly-old"])
        self.assertIs(kept[0], runs[0]); self.assertIs(kept[1], runs[4])
        self.assertEqual(runs, before)

    def test_empty_inputs_are_safe_and_do_not_mark_missing_theme_ids(self):
        empty = pd.DataFrame(columns=["theme_id", "source_status"])
        mask = director_theme_mask(empty)
        self.assertTrue(mask.empty); self.assertEqual(mask.dtype, bool)
        self.assertEqual(weekly_recommendation_runs(None), [])
        self.assertEqual(weekly_recommendation_runs([]), [])
        self.assertFalse(director_theme_mask(pd.DataFrame([{"theme_id": None}]),
            [{"theme_id": None, "run_id": "AI-DIRECTOR-missing"}]).iloc[0])


if __name__ == "__main__":
    unittest.main()
