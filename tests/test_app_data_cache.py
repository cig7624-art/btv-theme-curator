"""Saved-data caching must preserve freshness, evidence and caller isolation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import app_data_cache as cache
from issue_quality import assess_issues, build_ranked_issues, NEWS, YOUTUBE
from issue_related_news import attach_related_news


NOW = "2026-10-21T12:00:05+09:00"


def news(**changes):
    row = dict(
        date="2026-10-21", source=NEWS, source_type="news", related_content="실낙원",
        issue_title="실낙원 21일 개봉", source_url="https://news.example.com/article/1",
        published_at="2026-10-20", publisher="영화신문", description="실낙원 개봉 소식",
        event_type="release", keywords="영화,개봉",
    )
    row.update(changes)
    return row


def popular():
    return news(
        source=YOUTUBE, source_type="youtube_reaction", publisher="영화 채널",
        issue_title="실낙원 공식 영상", source_url="https://www.youtube.com/watch?v=abcdefghijk",
        published_at="2026-09-01",
        metrics_json=json.dumps(dict(
            views_total=300000, popular_snapshot_at="2026-10-21T11:00:00+09:00",
            popular_region="KR", popular_list_position=3, popular_category_id="1",
            popular_snapshot_complete=True,
        )),
    )


class AppDataCacheTests(unittest.TestCase):
    def setUp(self):
        cache.invalidate_saved_data()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(cache.invalidate_saved_data)
        self.root = Path(self.directory.name)
        self.issue_path = self.root / "issues.csv"
        self.status_path = self.root / "status.json"

    def write_issues(self, rows):
        pd.DataFrame(rows).to_csv(self.issue_path, sep="|", index=False)

    def test_csv_preserves_inference_dtype_missing_values_and_independent_results(self):
        self.issue_path.write_text("id|name\n001|\n002|saved\n", encoding="utf-8")
        inferred = cache.cached_csv(self.issue_path)
        expected = pd.read_csv(self.issue_path, sep="|").fillna("")
        pd.testing.assert_frame_equal(inferred, expected)
        inferred.loc[0, "name"] = "mutated"
        self.assertEqual(cache.cached_csv(self.issue_path).loc[0, "name"], "")
        self.assertEqual(cache.cached_csv(self.issue_path, dtype=str).loc[0, "id"], "001")

    def test_same_size_write_with_restored_mtime_invalidates(self):
        self.issue_path.write_text("id|name\n1|first\n", encoding="utf-8")
        before = self.issue_path.stat()
        revision = cache.file_revision(self.issue_path)
        self.assertEqual(cache.cached_csv(self.issue_path).loc[0, "name"], "first")
        self.issue_path.write_text("id|name\n1|other\n", encoding="utf-8")
        os.utime(self.issue_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.issue_path.stat().st_size, before.st_size)
        self.assertNotEqual(cache.file_revision(self.issue_path), revision)
        self.assertEqual(cache.cached_csv(self.issue_path).loc[0, "name"], "other")

    def test_atomic_replacement_with_same_size_and_mtime_invalidates(self):
        self.issue_path.write_text("id|name\n1|first\n", encoding="utf-8")
        before = self.issue_path.stat()
        revision = cache.file_revision(self.issue_path)
        cache.cached_csv(self.issue_path)
        replacement = self.root / "replacement.csv"
        replacement.write_text("id|name\n1|other\n", encoding="utf-8")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.issue_path)
        self.assertNotEqual(cache.file_revision(self.issue_path), revision)
        self.assertEqual(cache.cached_csv(self.issue_path).loc[0, "name"], "other")

    def test_revision_resolves_paths_and_changes_when_missing_file_appears(self):
        missing = cache.file_revision(self.issue_path)
        self.assertEqual(missing[0], str(self.issue_path.resolve()))
        self.assertIsNone(missing[1])
        self.issue_path.write_text("id\n1\n", encoding="utf-8")
        self.assertNotEqual(cache.file_revision(self.issue_path), missing)

    def test_read_retries_instead_of_caching_a_changed_file(self):
        self.issue_path.write_text("id|name\n1|first\n", encoding="utf-8")
        original_read = pd.read_csv
        reads = []

        def changing_read(*args, **kwargs):
            frame = original_read(*args, **kwargs)
            reads.append(1)
            if len(reads) == 1:
                self.issue_path.write_text("id|name\n1|other\n", encoding="utf-8")
            return frame

        with patch.object(cache.pd, "read_csv", side_effect=changing_read):
            result = cache.cached_csv(self.issue_path)
        self.assertEqual(len(reads), 2)
        self.assertEqual(result.loc[0, "name"], "other")
        self.assertEqual(cache.cached_csv(self.issue_path).loc[0, "name"], "other")

    def test_prepared_status_change_invalidates_current_popular_membership(self):
        self.write_issues([popular()])
        before = cache.cached_prepared_issues(self.issue_path, self.status_path, now=NOW)
        self.assertTrue(before.loc[0, "popular_valid"])
        self.status_path.write_text(json.dumps({"sources": {"youtube": {
            "popular_failed_lists": ["1"], "popular_checked_at": "2026-10-21T11:30:00+09:00",
        }}}), encoding="utf-8")
        after = cache.cached_prepared_issues(self.issue_path, self.status_path, now=NOW)
        self.assertFalse(after.loc[0, "popular_valid"])
        self.assertFalse(after.loc[0, "eligible"])

    def test_prepared_cache_reuses_minute_but_rechecks_next_minute_and_release(self):
        self.write_issues([news()])
        with patch.object(cache, "assess_issues", wraps=assess_issues) as assess:
            cache.cached_prepared_issues(self.issue_path, self.status_path, release_version="v1", now=NOW)
            cache.cached_prepared_issues(self.issue_path, self.status_path, release_version="v1", now="2026-10-21T12:00:59+09:00")
            self.assertEqual(assess.call_count, 1)
            cache.cached_prepared_issues(self.issue_path, self.status_path, release_version="v1", now="2026-10-21T12:01:00+09:00")
            cache.cached_prepared_issues(self.issue_path, self.status_path, release_version="v2", now="2026-10-21T12:01:00+09:00")
            self.assertEqual(assess.call_count, 3)
            self.assertEqual(assess.call_args_list[0].kwargs["now"], pd.Timestamp(NOW))

    def test_kst_midnight_expires_old_issue_without_file_changes(self):
        self.write_issues([news(date="2026-10-15", published_at="2026-10-15")])
        before = cache.cached_prepared_issues(self.issue_path, self.status_path, now="2026-10-21T14:59:59Z")
        after = cache.cached_prepared_issues(self.issue_path, self.status_path, now="2026-10-21T15:00:00Z")
        self.assertTrue(before.loc[0, "eligible"])
        self.assertFalse(after.loc[0, "eligible"])

    def test_core_preserves_news_pool_semantics_and_nested_copy_isolation(self):
        assessed = assess_issues(pd.DataFrame([popular(), news()]), now=NOW)
        only_video = assessed[assessed.source_group == YOUTUBE]
        expected = attach_related_news(build_ranked_issues(only_video), assessed, now=NOW)
        first = cache.cached_core_issues(only_video, news_pool=assessed, now=NOW)
        pd.testing.assert_frame_equal(first, expected)
        self.assertEqual(len(first.loc[0, "related_news"]), 1)
        first.loc[0, "related_news"][0]["title"] = "mutated"
        first.loc[0, "evidence_records"][0]["title"] = "mutated"
        second = cache.cached_core_issues(only_video, news_pool=assessed, now=NOW)
        pd.testing.assert_frame_equal(second, expected)
        default = cache.cached_core_issues(only_video, now=NOW)
        self.assertEqual(default.loc[0, "related_news"], [])

    def test_core_rechecks_time_sensitive_news_at_minute_and_kst_day_boundaries(self):
        assessed = assess_issues(pd.DataFrame([popular(), news(published_at="2026-10-15")]), now=NOW)
        only_video = assessed[assessed.source_group == YOUTUBE]
        with patch.object(cache, "attach_related_news", wraps=attach_related_news) as attach:
            cache.cached_core_issues(only_video, news_pool=assessed, now=NOW)
            cache.cached_core_issues(only_video, news_pool=assessed, now="2026-10-21T12:00:59+09:00")
            self.assertEqual(attach.call_count, 1)
            cache.cached_core_issues(only_video, news_pool=assessed, now="2026-10-21T12:01:00+09:00")
            self.assertEqual(attach.call_count, 2)
        before = cache.cached_core_issues(only_video, news_pool=assessed, now="2026-10-21T23:59:59+09:00")
        after = cache.cached_core_issues(only_video, news_pool=assessed, now="2026-10-22T00:00:00+09:00")
        self.assertEqual(len(before.loc[0, "related_news"]), 1)
        self.assertEqual(after.loc[0, "related_news"], [])

    def test_explicit_invalidation_clears_calculated_results(self):
        self.write_issues([news()])
        with patch.object(cache, "assess_issues", wraps=assess_issues) as assess:
            cache.cached_prepared_issues(self.issue_path, self.status_path, now=NOW)
            cache.invalidate_saved_data()
            cache.cached_prepared_issues(self.issue_path, self.status_path, now=NOW)
            self.assertEqual(assess.call_count, 2)


if __name__ == "__main__":
    unittest.main()
