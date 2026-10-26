"""Extra news refreshes never spend YouTube quota or renew other snapshots."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd
import collect_issues as c


TODAY = date(2026, 10, 26)
NOW = "2026-10-26T05:00:00+00:00"


def row(source, suffix, day="2026-10-26"):
    return c.metadata_row(dict(date=day, source=source, issue_title="기존 " + suffix, related_content="기존작품",
                              source_url="https://example.com/" + suffix),
                          published_at=day, collected_at="2026-10-25T01:00:00+00:00",
                          reference_date=day, publisher="기존 발행처")


class NewsOnlyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.feed, self.status = self.root / "issues.csv", self.root / "status.json"
        self.watch, self.stats = self.root / "watch.csv", self.root / "stats.csv"
        self.watch.write_bytes(b"unchanged watchlist\n")
        self.stats.write_bytes(b"unchanged observations\n")
        self.old_rows = [row("YouTube 반응", "today-video"), row("YouTube 반응", "old-video", "2026-07-01"),
                         row("KOBIS 박스오피스", "audience", "2026-10-25"),
                         row("Netflix Top 10", "weekly", "2026-10-18"), row("온라인 화제·뉴스", "old-news", "2026-10-25")]
        c.save_csv(pd.DataFrame(self.old_rows), self.feed, c.ISSUE_COLUMNS)
        self.before_feed = c.load_csv(self.feed, c.ISSUE_COLUMNS)
        self.previous = {"schema_version": 1, "sources": {
            "youtube": {"state": "partial", "collected_at": "2026-10-25T01:00:00+00:00", "count": 42,
                        "popular_snapshots": {"1": "2026-10-25T01:00:00+00:00"}, "retained_count": 2},
            "kobis": {"state": "ok", "collected_at": "2026-10-25T01:00:00+00:00", "reference_date": "2026-10-24"},
            "netflix": {"state": "ok", "collected_at": "2026-10-25T01:00:00+00:00", "reference_date": "2026-10-18"},
            "news": {"state": "ok", "last_success_at": "2026-10-25T01:00:00+00:00"},
        }}
        self.status.write_text(json.dumps(self.previous))
        self.before_status, self.before_errors = c.COLLECTION_STATUS, deepcopy(c.SOURCE_ERRORS)
        self.addCleanup(self.restore_status)

    def restore_status(self):
        c.COLLECTION_STATUS = self.before_status
        c.SOURCE_ERRORS.clear()
        c.SOURCE_ERRORS.update(self.before_errors)

    def run_refresh(self, *, fail=False):
        def collect():
            if fail:
                raise RuntimeError("news request unavailable")
            c.source_status("news", "ok", 1, reference_date=TODAY.isoformat())
            return [row("온라인 화제·뉴스", "new-news")]
        with (patch.object(c, "ISSUE_PATH", self.feed), patch.object(c, "COLLECTION_STATUS_PATH", self.status),
              patch.object(c, "YOUTUBE_WATCHLIST_PATH", self.watch), patch.object(c, "YOUTUBE_STATS_PATH", self.stats),
              patch.object(c, "today_kst_date", return_value=TODAY), patch.object(c, "utc_now_iso", return_value=NOW),
              patch.object(c, "collect_google_news", side_effect=collect) as news,
              patch.object(c, "collect_kobis_boxoffice") as kobis, patch.object(c, "collect_netflix_top10") as netflix,
              patch.object(c, "discover_youtube_videos") as youtube, patch.object(c, "update_youtube_stats") as observations):
            result = c.main(news_only=True)
        news.assert_called_once()
        for method in (kobis, netflix, youtube, observations):
            method.assert_not_called()
        self.assertEqual(self.watch.read_bytes(), b"unchanged watchlist\n")
        self.assertEqual(self.stats.read_bytes(), b"unchanged observations\n")
        for route in ("youtube", "kobis", "netflix"):
            self.assertEqual(result["sources"][route], self.previous["sources"][route])
        saved = c.load_csv(self.feed, c.ISSUE_COLUMNS)
        actual = saved[~saved.source.eq("온라인 화제·뉴스")].sort_values("source_url").reset_index(drop=True)
        expected = self.before_feed[~self.before_feed.source.eq("온라인 화제·뉴스")].sort_values("source_url").reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected)
        return result, saved

    def test_refreshes_news_only_and_preserves_old_non_news_snapshots_and_observation_files(self):
        result, saved = self.run_refresh()
        self.assertEqual(result["sources"]["news"]["collected_at"], NOW)
        self.assertEqual(sum(saved.source.eq("온라인 화제·뉴스")), 2)
        self.assertEqual(result["overall_state"], "partial")

    def test_news_failure_keeps_previous_articles_and_does_not_relabel_other_sources(self):
        result, saved = self.run_refresh(fail=True)
        self.assertEqual(result["sources"]["news"]["state"], "failed")
        self.assertEqual(result["sources"]["news"]["last_success_at"], self.previous["sources"]["news"]["last_success_at"])
        self.assertEqual(sum(saved.source.eq("온라인 화제·뉴스")), 1)


if __name__ == "__main__":
    unittest.main()
