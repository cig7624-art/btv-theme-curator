import json
import unittest

import pandas as pd

from issue_quality import BOXOFFICE, NEWS, YOUTUBE
from issue_related_news import attach_related_news


NOW = "2026-10-21T12:00:00+09:00"


def article(**changes):
    row = dict(source_group=NEWS, issue_key="work:실낙원", eligible=True, related_content="실낙원",
               issue_title="'실낙원' 개봉 첫날 관객 1위", source_url="https://ent.example.com/articles/123",
               published_at="2026-10-20T10:00:00+09:00", publisher="연예일보", event_type="release",
               quality_status="candidate", metrics_json="")
    row.update(changes)
    return row


def work(**changes):
    row = dict(source_group=YOUTUBE, issue_key="work:실낙원", source_url="https://www.youtube.com/watch?v=abcdefghijk",
               issue_title="[실낙원] 공식 예고편", related_content="실낙원", sort_priority=4,
               selection_reason="영상 반응", confirmed_route_count=1, issue_count=1)
    row.update(changes)
    return row


class RelatedNewsTests(unittest.TestCase):
    def attach(self, articles, works=None, **kwargs):
        return attach_related_news(pd.DataFrame(works or [work()]), pd.DataFrame(articles), now=NOW, **kwargs)

    def test_same_work_attaches_without_changing_ranking_or_source_counts(self):
        ranked = pd.DataFrame([work(), work(source_group=BOXOFFICE, issue_key="work:오디세이", source_url="https://www.kobis.or.kr/list")], index=[7, 42])
        result = attach_related_news(ranked, pd.DataFrame([article(), article(issue_key="work:오디세이", issue_title="오디세이 개봉", source_url="https://news.example.com/second")]), now=NOW)
        pd.testing.assert_frame_equal(result.drop(columns="related_news"), ranked)
        self.assertNotIn("related_news", ranked)
        self.assertEqual(result.at[7, "related_news"][0]["publisher"], "연예일보")
        self.assertEqual(result.at[42, "related_news"][0]["title"], "오디세이 개봉")

    def test_unknown_season_and_different_season_do_not_match(self):
        result = self.attach([article(issue_key="work:실낙원:season:2"), article(issue_key="work:실낙원:part:1")])
        self.assertEqual(result.iloc[0].related_news, [])
        self.assertEqual(len(self.attach([article(issue_key="work:실낙원:season:2")], [work(issue_key="work:실낙원:season:2")]).iloc[0].related_news), 1)

    def test_article_publication_date_is_required_and_not_replaced_by_capture_date(self):
        for changes in [dict(published_at="", date="2026-10-21"), dict(published_at="2026-10-14"),
                        dict(published_at="2026-10-22"), dict(published_at="2026-10-21T15:00:00+09:00")]:
            with self.subTest(changes=changes):
                self.assertEqual(self.attach([article(**changes)]).iloc[0].related_news, [])
        self.assertEqual(len(self.attach([article(published_at="2026-10-15")]).iloc[0].related_news), 1)

    def test_missing_publisher_rejected_rows_and_bad_links_do_not_attach(self):
        cases = [dict(publisher=""), dict(eligible=False), dict(quality_status="rejected"),
                 dict(source_group=YOUTUBE), dict(source_url="javascript:alert(1)"),
                 dict(source_url="https://user:password@news.example.com/story"),
                 dict(source_url="https://news.example.com/login")]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.attach([article(**changes)]).iloc[0].related_news, [])

    def test_google_relay_requires_actual_rss_publication_provenance(self):
        url = "https://news.google.com/rss/articles/abcdef"
        metrics = dict(news_provenance="google_news_rss", publisher_url="https://ent.example.com/", rss_url=url, original_url_status="relay")
        row = article(source_url=url, metrics_json=json.dumps(metrics))
        related = self.attach([row]).iloc[0].related_news
        self.assertEqual(related[0]["via"], "Google 뉴스")
        for changes in [dict(metrics_json=""), dict(event_type="content_news"),
                        dict(metrics_json={**metrics, "publisher_url": ""}),
                        dict(metrics_json={**metrics, "rss_url": "https://news.google.com/rss/articles/other"})]:
            with self.subTest(changes=changes):
                self.assertEqual(self.attach([{**row, **changes}]).iloc[0].related_news, [])

    def test_duplicate_url_headline_and_primary_article_are_not_repeated(self):
        stories = [article(source_url="https://ent.example.com/articles/123?utm_source=feed"),
                   article(issue_title="다른 제목", source_url="https://ent.example.com/articles/123#top"),
                   article(issue_title="'실낙원' 개봉 첫날 관객 1위 - 연예일보", source_url="https://other.example.com/story")]
        self.assertEqual(len(self.attach(stories).iloc[0].related_news), 1)
        primary = work(source_group=NEWS, source_url=stories[0]["source_url"], issue_title=stories[0]["issue_title"])
        self.assertEqual(self.attach(stories, [primary]).iloc[0].related_news, [])

    def test_original_url_is_preferred_over_relay_for_identical_headline(self):
        url = "https://news.google.com/rss/articles/abcdef"
        relay = article(source_url=url, published_at="2026-10-21", metrics_json=dict(news_provenance="google_news_rss", publisher_url="https://ent.example.com/", rss_url=url))
        related = self.attach([relay, article()]).iloc[0].related_news
        self.assertEqual(len(related), 1)
        self.assertEqual(related[0]["url"], article()["source_url"])
        self.assertEqual(related[0]["via"], "")

    def test_related_articles_are_recent_first_and_limited_to_three(self):
        rows = [article(issue_title=f"실낙원 개봉 소식 {day}", source_url=f"https://ent.example.com/story/{day}", published_at=f"2026-10-{day}") for day in [17, 20, 18, 19]]
        related = self.attach(rows).iloc[0].related_news
        self.assertEqual([item["published_at"] for item in related], ["2026-10-20", "2026-10-19", "2026-10-18"])
        self.assertTrue(all(set(item) == {"title", "url", "publisher", "published_at", "via"} for item in related))

    def test_empty_inputs_keep_a_usable_object_column(self):
        result = attach_related_news(pd.DataFrame(), pd.DataFrame(), now=NOW)
        self.assertIn("related_news", result)
        self.assertEqual(self.attach([]).iloc[0].related_news, [])


if __name__ == "__main__":
    unittest.main()
