"""Independent current-video and dated-news evidence cannot renew old claims."""
import unittest

import pandas as pd

from issue_quality import assess_issues, build_ranked_issues


NOW = "2026-10-21T12:00:00+09:00"


def video(**changes):
    row = dict(
        date="2026-10-21", collected_at="2026-10-21T02:00:00Z",
        source="YouTube 반응", source_type="youtube_reaction",
        issue_title="[실낙원] 공식 예고편", related_content="실낙원",
        published_at="2025-01-01", source_url="https://www.youtube.com/watch?v=film1234567",
        metrics_json=dict(views_total=5000),
    )
    row.update(changes)
    return row


def popular_metrics(**changes):
    metrics = dict(
        views_total=5000, popular_snapshot_at="2026-10-21T02:00:00Z",
        popular_region="KR", popular_list_position=12,
        popular_category_id="1", popular_snapshot_complete=True,
    )
    metrics.update(changes)
    return metrics


def news(**changes):
    row = dict(
        date="2026-10-21", collected_at="2026-10-21T02:00:00Z",
        source="온라인 화제·뉴스", source_type="news", issue_title="'실낙원', 21일 개봉",
        related_content="실낙원", description="신작 영화 실낙원의 개봉 소식",
        publisher="영화신문", published_at="2026-10-20", event_type="release",
        source_url="https://news.google.com/rss/articles/article-id",
        metrics_json=dict(news_provenance="google_news_rss", publisher_url="https://filmnews.example",
                          rss_url="https://news.google.com/rss/articles/article-id",
                          original_url_status="relay"),
    )
    row.update(changes)
    return row


class CurrentSignalTests(unittest.TestCase):
    def assessed(self, row):
        return assess_issues(pd.DataFrame([row]), now=NOW).iloc[0]

    def test_current_popular_is_independent_of_upload_age_or_reaction_history(self):
        result = self.assessed(video(metrics_json=popular_metrics()))
        self.assertTrue(result.eligible)
        self.assertTrue(result.popular_valid)
        self.assertFalse(result.comparison_valid)
        self.assertEqual(result.sort_priority, 4)
        self.assertIn("영화·애니메이션", result.metric_summary)
        self.assertNotIn("12위", result.metric_summary)
        self.assertTrue(result._quality_reference.startswith("2026-10-21"))

    def test_yesterdays_popular_snapshot_cannot_claim_current_popularity(self):
        result = self.assessed(video(metrics_json=popular_metrics(popular_snapshot_at="2026-10-20T14:00:00Z")))
        self.assertFalse(result.popular_valid)
        self.assertFalse(result.eligible)

    def test_popular_origin_string_and_discovery_timestamp_are_insufficient(self):
        result = self.assessed(video(metrics_json=dict(views_total=5000, query="한국 인기 영상", last_discovered_at=NOW)))
        self.assertFalse(result.popular_valid)
        self.assertFalse(result.eligible)

    def test_initial_reaction_requires_an_actual_or_inferable_upload_date(self):
        result = self.assessed(video(published_at="", metrics_json=dict(views_total=500000)))
        self.assertFalse(result.eligible)
        self.assertIn("공개일", result.exclusion_reason)

    def test_invalid_or_future_popular_metadata_does_not_qualify(self):
        for changes in (
            {"popular_region": "US"}, {"popular_snapshot_complete": False},
            {"popular_list_position": 0}, {"popular_list_position": 2.5},
            {"popular_category_id": "999"}, {"popular_snapshot_at": "2026-10-21"},
            {"popular_snapshot_at": "2026-10-21T04:00:00Z"},
        ):
            with self.subTest(changes=changes):
                result = self.assessed(video(metrics_json=popular_metrics(**changes)))
                self.assertFalse(result.popular_valid)
                self.assertFalse(result.eligible)

    def test_fresh_measured_reaction_can_select_an_old_upload(self):
        metrics = dict(views_total=150000, views_change=50000,
                       observation_start="2026-10-20", observation_end="2026-10-21")
        result = self.assessed(video(metrics_json=metrics))
        self.assertTrue(result.eligible)
        self.assertTrue(result.comparison_valid)
        self.assertEqual(result.sort_priority, 4)
        self.assertTrue(result._quality_reference.startswith("2026-10-21"))

    def test_stale_reaction_and_small_change_do_not_renew_old_upload(self):
        for metrics in (
            dict(views_total=150000, views_change=50000, observation_start="2026-10-10", observation_end="2026-10-11"),
            dict(views_total=150000, views_change=1000, observation_start="2026-10-20", observation_end="2026-10-21"),
        ):
            with self.subTest(metrics=metrics):
                self.assertFalse(self.assessed(video(metrics_json=metrics)).eligible)

    def test_comments_are_a_measured_independent_reaction(self):
        metrics = dict(views_total=150000, views_change=0, comments_change=120,
                       observation_start="2026-10-20", observation_end="2026-10-21")
        result = self.assessed(video(metrics_json=metrics))
        self.assertTrue(result.eligible)
        self.assertEqual(result.sort_priority, 4)
        self.assertIn("120", result.metric_summary)

    def test_newer_complete_list_supersedes_an_older_membership_in_same_category(self):
        old = video(metrics_json=popular_metrics(popular_snapshot_at="2026-10-21T01:00:00Z"))
        new = video(related_content="새로운작품", issue_title="[새로운작품] 예고편",
                    source_url="https://www.youtube.com/watch?v=other123456",
                    metrics_json=popular_metrics())
        result = assess_issues(pd.DataFrame([old, new]), now=NOW)
        self.assertFalse(result.iloc[0].popular_valid)
        self.assertFalse(result.iloc[0].eligible)
        self.assertTrue(result.iloc[1].popular_valid)

    def test_another_category_or_failed_response_does_not_invalidate_a_good_list(self):
        old = video(metrics_json=popular_metrics(popular_snapshot_at="2026-10-21T01:00:00Z"))
        for changes in ({"popular_category_id": "24"}, {"popular_snapshot_complete": False}):
            other = video(related_content="새로운작품", issue_title="[새로운작품] 예고편",
                          metrics_json=popular_metrics(**changes))
            with self.subTest(changes=changes):
                result = assess_issues(pd.DataFrame([old, other]), now=NOW)
                self.assertTrue(result.iloc[0].popular_valid)

    def test_old_membership_can_still_qualify_through_a_fresh_measured_reaction(self):
        metrics = popular_metrics(popular_snapshot_at="2026-10-21T01:00:00Z", views_total=100000,
                                  views_change=50000, observation_start="2026-10-20", observation_end="2026-10-21")
        old = video(metrics_json=metrics)
        new = video(related_content="새로운작품", issue_title="[새로운작품] 예고편", metrics_json=popular_metrics())
        result = assess_issues(pd.DataFrame([old, new]), now=NOW)
        self.assertFalse(result.iloc[0].popular_valid)
        self.assertTrue(result.iloc[0].comparison_valid)
        self.assertTrue(result.iloc[0].eligible)

    def test_valid_rss_news_can_be_selected_without_a_resolved_original_url(self):
        result = self.assessed(news())
        self.assertTrue(result.eligible)
        self.assertEqual(result.sort_priority, 2)
        self.assertIn("Google 뉴스 연결", result.evidence_status)
        self.assertNotIn("공식", result.evidence_status)

    def test_legacy_missing_publication_and_publisher_are_not_invented(self):
        for changes in ({"published_at": ""}, {"publisher": ""}, {"metrics_json": {}}):
            with self.subTest(changes=changes):
                self.assertFalse(self.assessed(news(**changes)).eligible)

    def test_news_requires_attributed_publisher_and_genuine_rss_link(self):
        for changes in ({"publisher_url": ""}, {"publisher_url": "https://news.google.com"},
                        {"rss_url": "https://news.google.com/search?q=film"}):
            row = news()
            row["metrics_json"].update(changes)
            with self.subTest(changes=changes):
                self.assertFalse(self.assessed(row).eligible)

    def test_generic_or_old_news_is_not_selected_to_fill_a_slot(self):
        for row in (
            news(related_content="영화", issue_title="영화 이야기", description="영화 이야기", event_type="content_news"),
            news(published_at="2026-10-01"),
            news(issue_title="실낙원 시즌 2 이야기", description="실낙원 시즌 2 이야기", event_type="content_news"),
        ):
            with self.subTest(row=row):
                self.assertFalse(self.assessed(row).eligible)

    def test_repeated_news_is_one_work_and_does_not_boost_priority(self):
        row = news()
        ranked = build_ranked_issues(assess_issues(pd.DataFrame([row, row, row]), now=NOW))
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked.iloc[0].issue_count, 1)
        self.assertEqual(ranked.iloc[0].sort_priority, 2)


if __name__ == "__main__":
    unittest.main()
