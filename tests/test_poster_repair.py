"""Saved poster repairs preserve recommendations and concurrent new generations."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import repair_theme_posters as repair


def candidate(title="더 글로리", **changes):
    row = dict(run_id="new-run", theme_key="T1", theme_id="T1", theme_name="반전 드라마", rank="1",
               title=title, content_type="드라마", year="2022", reason="추천 이유", source_url="https://m.kinolights.com/search?q=work",
               poster_url="", kinolights_status="search_link_only", matched_score="0", fetched_at="2026-10-22T03:00:00Z",
               future_column="must stay unchanged")
    row.update(changes)
    return row


def history(run="new-run", stamp="2026-10-22T11:42:13+09:00"):
    return dict(run_id=run, recommended_at=stamp, recommended_date="2026-10-22")


def save(root, rows, generations=None):
    pd.DataFrame(rows).to_csv(root / "theme_content_candidates.csv", sep="|", index=False)
    pd.DataFrame(generations if generations is not None else [history()]).to_csv(root / "theme_recommendation_history.csv", sep="|", index=False)


class PosterRepairTests(unittest.TestCase):
    def test_latest_run_uses_timestamps_and_deterministic_ties_not_append_order(self):
        content = pd.DataFrame([candidate()])
        generations = pd.DataFrame([history("a", "2026-10-22T10:00:00+09:00"),
                                    history("newest", "2026-10-22T02:00:00Z"),
                                    history("old-last", "2026-10-21T23:00:00+09:00")])
        self.assertEqual(repair.latest_run_id(generations, content), "newest")
        tied = pd.DataFrame([history("a"), history("z"), history("m")])
        self.assertEqual(repair.latest_run_id(tied, content), "z")
        self.assertEqual(repair.latest_run_id(tied.iloc[::-1], content), "z")

    def test_collect_checks_only_latest_visible_unique_missing_titles_with_budget(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [candidate("이미지 있음", poster_url="https://images.example.com/existing.jpg"),
                    candidate("더 글로리", rank="2"), candidate("더 글로리", rank="3"),
                    candidate("부부의 세계", rank="4"), candidate("스카이 캐슬", rank="5"),
                    candidate("화면 밖 작품", rank="6"), candidate("옛 작품", run_id="old-run"),
                    candidate("더 글로리", theme_key="T2"), candidate("마지막 후보", theme_key="T2", rank="2")]
            save(root, rows, [history(), history("old-run", "2026-10-21T10:00:00+09:00")])
            before = (root / "theme_content_candidates.csv").read_bytes()
            def enrich(items, **kwargs):
                self.assertEqual(kwargs["limit"], 2)
                self.assertEqual([item["title"] for item in items], ["더 글로리", "부부의 세계"])
                return [dict(item, poster_url="https://upload.wikimedia.org/real.jpg", poster_source="위키백과") for item in items]
            with patch.object(repair, "enrich_poster_candidates", side_effect=enrich) as lookup:
                manifest = repair.collect_updates(root, limit=2)
            lookup.assert_called_once()
            self.assertEqual(manifest["source_run_id"], "new-run")
            self.assertEqual(manifest["checked_unique"], 2)
            self.assertEqual(manifest["found_unique"], 2)
            self.assertEqual((root / "theme_content_candidates.csv").read_bytes(), before)
            self.assertNotIn("화면 밖 작품", str(manifest["updates"]))

    def test_apply_changes_only_blank_poster_identity_preserving_rows_order_and_all_fields(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [candidate(), candidate(run_id="THEME_DB"), candidate(year="2023"),
                    candidate(content_type="영화"), candidate(title="다른 작품"),
                    candidate(poster_url="https://images.example.com/existing.jpg"), candidate(run_id="old-run")]
            save(root, rows)
            path = root / "theme_content_candidates.csv"
            before = pd.read_csv(path, sep="|", dtype=str).fillna("")
            manifest = dict(schema_version=1, source_run_id="new-run", checked_unique=1,
                            updates=[dict(title="더 글로리", year="2022", content_type="드라마", poster_url="https://upload.wikimedia.org/real.jpg")])
            result = repair.apply_updates(manifest, root, write=True)
            after = pd.read_csv(path, sep="|", dtype=str).fillna("")
            self.assertEqual(result["updated_rows"], 3)
            self.assertEqual(after.poster_url.tolist(), ["https://upload.wikimedia.org/real.jpg", "https://upload.wikimedia.org/real.jpg", "", "", "", "https://images.example.com/existing.jpg", "https://upload.wikimedia.org/real.jpg"])
            pd.testing.assert_frame_equal(before.drop(columns="poster_url"), after.drop(columns="poster_url"))
            self.assertTrue((root / "artifacts" / "poster_repair_status.json").exists())

    def test_apply_rereads_latest_generation_and_preserves_concurrent_recommendations(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            save(root, [candidate()])
            manifest = dict(schema_version=1, source_run_id="new-run", checked_unique=1,
                            updates=[dict(title="더 글로리", year="2022", content_type="드라마", poster_url="https://upload.wikimedia.org/real.jpg")])
            # New recommendations arrive after network lookups have completed.
            newer = candidate(run_id="latest-run", theme_key="T99", theme_name="새 추천 테마", reason="newly generated")
            save(root, [candidate(), newer], [history(), history("latest-run", "2026-10-22T14:00:00+09:00")])
            result = repair.apply_updates(manifest, root, write=True)
            after = pd.read_csv(root / "theme_content_candidates.csv", sep="|", dtype=str).fillna("")
            self.assertEqual(result["latest_run_id"], "latest-run")
            self.assertEqual(result["before_visible_with_poster"], 0)
            self.assertEqual(result["visible_with_poster"], 1)
            self.assertEqual(after.iloc[1].reason, "newly generated")
            self.assertEqual(after.iloc[1].theme_name, "새 추천 테마")

    def test_dry_run_does_not_write_csv_or_report_and_rejects_invalid_image_urls(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            save(root, [candidate()])
            path = root / "theme_content_candidates.csv"
            before = path.read_bytes()
            for bad in ["javascript:alert(1)", "https://", "https://user:password@example.com/image.jpg"]:
                manifest = dict(schema_version=1, updates=[dict(title="더 글로리", year="2022", content_type="드라마", poster_url=bad)])
                report = repair.apply_updates(manifest, root)
                self.assertEqual(report["updated_rows"], 0)
            manifest = dict(schema_version=1, updates=[dict(title="더 글로리", year="2022", content_type="드라마", poster_url="https://upload.wikimedia.org/real.jpg")])
            self.assertEqual(repair.apply_updates(manifest, root)["updated_rows"], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((root / "artifacts" / "poster_repair_status.json").exists())

    def test_new_generation_without_content_does_not_repair_an_old_run_instead(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            save(root, [candidate(run_id="old-run")], [history()])
            with patch.object(repair, "enrich_poster_candidates") as lookup:
                manifest = repair.collect_updates(root)
            lookup.assert_not_called()
            self.assertEqual(manifest["source_run_id"], "new-run")
            self.assertEqual(manifest["checked_unique"], 0)
            self.assertEqual(manifest["updates"], [])


if __name__ == "__main__":
    unittest.main()
