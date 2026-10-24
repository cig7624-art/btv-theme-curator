"""Content summaries preserve candidate ordering and reflect the latest writes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from theme_content_manager import (
    CONTENT_COLUMNS,
    content_coverage,
    content_map_for_scope,
    load_content_frame,
    missing_theme_rows,
    upsert_content_frame,
)


class ThemeContentManagerTests(unittest.TestCase):
    def test_map_keeps_stable_rank_order_cleaned_keys_and_extra_columns(self):
        frame = pd.DataFrame([
            dict(run_id="THEME_DB", theme_key=" T1 ", rank="2", title="  padded\n title  ", extra=" first\nvalue "),
            dict(run_id="THEME_DB", theme_key=" T1 ", rank="1", title="first tie", extra=None),
            dict(run_id="THEME_DB", theme_key=" T1 ", rank=1, title="second tie", extra=7),
            dict(run_id="THEME_DB", theme_key="T1", rank="0", title="same cleaned key"),
            dict(run_id="THEME_DB", theme_key="T2", rank="bad", title="unknown rank"),
            dict(run_id="THEME_DB", theme_key="T2", rank="3", title="numeric rank"),
            dict(run_id="THEME_DB", theme_key="\t ", rank="1", title="blank key"),
            dict(run_id="another run", theme_key="T1", rank="1", title="other scope"),
        ])
        before = frame.copy(deep=True)

        result = content_map_for_scope(frame, "THEME_DB")

        self.assertEqual(list(result), ["T1", "T2"])
        self.assertEqual([row["title"] for row in result["T1"]],
                         ["first tie", "second tie", "padded title", "same cleaned key"])
        self.assertEqual([row["title"] for row in result["T2"]], ["numeric rank", "unknown rank"])
        self.assertEqual([row["extra"] for row in result["T1"]], ["", "7", "first value", ""])
        self.assertEqual(result["T1"][0]["theme_key"], "T1")
        self.assertEqual(list(result["T1"][0]), [*CONTENT_COLUMNS, "extra"])
        pd.testing.assert_frame_equal(frame, before)

    def test_coverage_counts_unknown_candidates_and_duplicate_theme_rows(self):
        themes = pd.DataFrame({"theme_id": ["T1", "T1", "T2", " T3 ", "T4"]}, index=[8, 9, 4, 6, 1])
        frame = pd.DataFrame([
            dict(run_id="THEME_DB", theme_key=" T1 "),
            dict(run_id="THEME_DB", theme_key="T1"),
            dict(run_id="THEME_DB", theme_key="T3"),
            dict(run_id="THEME_DB", theme_key="unknown"),
            dict(run_id="THEME_DB", theme_key="\n"),
            dict(run_id="THEME_DB", theme_key=None),
            dict(run_id="weekly", theme_key="T2"),
        ])

        # Counts need only keys; neither operation should render candidate rows.
        with patch("theme_content_manager.content_map_for_scope", side_effect=AssertionError("unnecessary metadata")):
            self.assertEqual(content_coverage(themes, frame), {
                "total_themes": 5, "connected_themes": 2, "missing_themes": 3, "candidate_rows": 4,
            })
            missing = missing_theme_rows(themes, frame, limit=2)
            self.assertEqual(missing["theme_id"].tolist(), ["T2", " T3 "])
            self.assertEqual(missing.index.tolist(), [4, 6])
            self.assertEqual(missing_theme_rows(themes, frame, limit=0)["theme_id"].tolist(), ["T2"])

    def test_empty_frames_and_absent_schema_columns(self):
        themes = pd.DataFrame({"theme_id": ["T1"]})
        for frame in (pd.DataFrame(), pd.DataFrame({"title": ["unscoped"]})):
            self.assertEqual(content_map_for_scope(frame, "THEME_DB"), {})
            self.assertEqual(content_coverage(themes, frame), {
                "total_themes": 1, "connected_themes": 0, "missing_themes": 1, "candidate_rows": 0,
            })
            pd.testing.assert_frame_equal(missing_theme_rows(themes, frame), themes)

    def test_saved_replacements_and_external_edits_are_visible_immediately(self):
        themes = pd.DataFrame({"theme_id": ["T1", "T2"]})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            first = pd.DataFrame([
                dict(run_id="THEME_DB", theme_key="T1", title="old", rank="1"),
                dict(run_id="weekly", theme_key="T1", title="keep weekly", rank="1"),
            ])
            upsert_content_frame(path, first)
            self.assertEqual(content_coverage(themes, load_content_frame(path))["connected_themes"], 1)

            replacement = pd.DataFrame([
                dict(run_id="THEME_DB", theme_key="T1", title="new", rank="1"),
                dict(run_id="THEME_DB", theme_key="T2", title="added", rank="1"),
            ])
            upsert_content_frame(path, replacement)
            loaded = load_content_frame(path)
            self.assertEqual(content_map_for_scope(loaded, "THEME_DB")["T1"][0]["title"], "new")
            self.assertEqual(content_map_for_scope(loaded, "weekly")["T1"][0]["title"], "keep weekly")
            self.assertEqual(content_coverage(themes, loaded)["connected_themes"], 2)
            self.assertTrue(missing_theme_rows(themes, loaded).empty)

            # A collector or another process can replace the file, too.
            first.to_csv(path, sep="|", index=False, encoding="utf-8-sig")
            loaded = load_content_frame(path)
            self.assertEqual(content_map_for_scope(loaded, "THEME_DB")["T1"][0]["title"], "old")
            self.assertEqual(missing_theme_rows(themes, loaded)["theme_id"].tolist(), ["T2"])


if __name__ == "__main__":
    unittest.main()
