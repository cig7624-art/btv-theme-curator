import json
import unittest
from datetime import date
from unittest.mock import Mock, patch

import collect_issues as c
import pandas as pd

from issue_presentation import present_issue
from issue_quality import assess_issues


TODAY = date(2026, 10, 21)


def entry(**changes):
    value = {
        "title": "영화 '실낙원' 개봉 - 기사신문",
        "summary": "영화 실낙원이 개봉한다.",
        "link": "https://news.google.com/rss/articles/example",
        "source": {"title": "기사신문", "href": "https://publisher.example"},
        "published_parsed": (2026, 10, 21, 0, 0, 0, 0, 0, 0),
    }
    value.update(changes)
    return value


def news_row(url):
    return {"source": "온라인 화제·뉴스", "source_type": "news", "source_url": url,
            "date": TODAY.isoformat(), "image_url": ""}


class NewsCollectionTests(unittest.TestCase):
    def setUp(self):
        self.old_status = c.COLLECTION_STATUS
        self.old_errors = dict(c.SOURCE_ERRORS)
        c.COLLECTION_STATUS = {"schema_version": 1, "sources": {}}
        c.SOURCE_ERRORS.clear()
        self.clock = patch.object(c, "today_kst_date", return_value=TODAY)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        c.COLLECTION_STATUS = self.old_status
        c.SOURCE_ERRORS.clear()
        c.SOURCE_ERRORS.update(self.old_errors)

    def collect(self, entries, queries=None):
        feed = Mock(bozo=False, entries=entries)
        with patch.object(c, "NEWS_QUERY_GROUPS", queries or [("온라인 화제·뉴스", "영화 개봉")]), \
             patch.object(c.requests, "get", return_value=Mock(content=b"rss")), \
             patch.object(c.feedparser, "parse", return_value=feed), \
             patch.object(c, "enrich_news_images", side_effect=lambda rows: rows):
            return c.collect_google_news()

    def test_relay_records_keep_real_feed_provenance_without_claiming_original(self):
        row, = self.collect([entry()])
        metrics = json.loads(row["metrics_json"])
        self.assertEqual(row["issue_title"], "영화 '실낙원' 개봉")
        self.assertEqual(row["related_content"], "실낙원")
        self.assertEqual(row["published_at"], "2026-10-21T00:00:00+00:00")
        self.assertEqual(row["publisher"], "기사신문")
        self.assertEqual(row["event_type"], "release")
        self.assertEqual(row["source_type"], "news")
        self.assertEqual(metrics["news_provenance"], "google_news_rss")
        self.assertEqual(metrics["publisher_url"], "https://publisher.example")
        self.assertEqual(metrics["rss_url"], entry()["link"])
        self.assertEqual(metrics["original_url_status"], "relay")
        self.assertEqual(metrics["article_url"], "")

    def test_unknown_dates_publishers_events_and_comparison_targets_are_not_candidates(self):
        invalid = [
            entry(published_parsed=None),
            entry(source={}),
            entry(title="영화 '실낙원' 이야기", summary="영화 실낙원 이야기"),
            entry(title="[극장가] 영화 '암살자' 넘었다…1위 한국영화", summary="영화 관객 기록"),
            entry(title="[영화] 신작 공개", summary="영화 신작 공개"),
        ]
        self.assertEqual(self.collect(invalid), [])
        status = c.COLLECTION_STATUS["sources"]["news"]
        self.assertEqual(status["missing_publication_date_count"], 1)
        self.assertEqual(status["rejected_count"], 4)

    def test_same_article_found_by_multiple_queries_is_one_candidate(self):
        rows = self.collect([entry()], [("온라인 화제·뉴스", "개봉"), ("OTT 공식 신작", "공개")])
        self.assertEqual(len(rows), 1)

    def test_malformed_publication_date_is_not_replaced_by_collection_day(self):
        self.assertIsNone(c.parse_entry_date({"published_parsed": (2026, 99, 21, 0, 0, 0)}))
        self.assertIsNone(c.parse_entry_date({"updated_parsed": (2026, 10, 21, 0, 0, 0)}))

    def test_latest_valid_feed_entry_is_found_after_old_or_irrelevant_first_twelve(self):
        old = [entry(title=f"영화 '옛작품' 개봉 {number}", published_parsed=(2026, 9, 1, 0, 0, 0)) for number in range(15)]
        rows = self.collect([*old, entry(title="영화 '달빛' 공개 - 기사신문",
                                        published_parsed=(2026, 10, 21, 5, 30, 0))])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["related_content"], "달빛")
        self.assertEqual(rows[0]["published_at"], "2026-10-21T05:30:00+00:00")

    def test_rss_request_has_recent_window_without_trusting_collection_date(self):
        feed = Mock(bozo=False, entries=[])
        with patch.object(c, "NEWS_QUERY_GROUPS", [("온라인 화제·뉴스", "영화 (개봉 OR 공개)")]), \
             patch.object(c.requests, "get", return_value=Mock(content=b"rss")) as fetch, \
             patch.object(c.feedparser, "parse", return_value=feed), \
             patch.object(c, "enrich_news_images", side_effect=lambda rows: rows):
            c.collect_google_news()
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(fetch.call_args.args[0]).query)["q"][0]
        self.assertIn("when:7d", query)
        self.assertIn("OR", query)

    def test_repeated_same_work_event_does_not_hide_other_works_after_the_query_cap(self):
        repeated = [entry(title=f"영화 '실낙원' 개봉 발표 {number} - 기사신문",
                          link=f"https://news.google.com/rss/articles/repeat-{number}",
                          published_parsed=(2026, 10, 21, 5, 30, 0)) for number in range(18)]
        other = entry(title="영화 '달빛' 공개 - 기사신문", link="https://news.google.com/rss/articles/other-work",
                      published_parsed=(2026, 10, 21, 5, 0, 0))
        rows = self.collect([*repeated, other])
        self.assertEqual({row["related_content"] for row in rows}, {"실낙원", "달빛"})
        self.assertEqual(sum(row["related_content"] == "실낙원" for row in rows), 2)

    def test_editorial_column_label_is_not_a_work_or_selected_news(self):
        headline = "무료 공개로 웃은 쿠팡플레이, 유료 전환 노린 디플…엇갈린 성적표 [TEN스타필드]"
        self.assertEqual(c.guess_news_related_content(headline), "")
        bad = entry(title=headline, summary="OTT 공개 서비스의 성적표")
        self.assertEqual(self.collect([bad]), [])
        old_row = dict(date="2026-10-21", source="온라인 화제·뉴스", source_type="news", related_content="TEN스타필드",
                       issue_title=headline, published_at="2026-10-21", publisher="기사신문",
                       source_url="https://publisher.example/story/1", event_type="release")
        self.assertFalse(assess_issues(pd.DataFrame([old_row]), now="2026-10-21T12:00:00+09:00").iloc[0].eligible)

    def test_news_title_extraction_handles_publication_punctuation_and_interview(self):
        cases = {
            "'실낙원' 배현성 인터뷰": "실낙원",
            "'실낙원' 배현성, '연상호 사단' 합류 [인터뷰①]": "실낙원",
            "영화 `실낙원` 개봉": "실낙원",
            "‘실낙원’ 한국영화 예매율 1위": "실낙원",
            "개봉 D-3 ‘실낙원’, ‘얼굴’ 흥행이을까": "실낙원",
            "‘비전퀘스트’, 공개…영화제 상영": "비전퀘스트",
            "[영화] 신작 ‘오디세이’ 개봉 확정": "오디세이",
            "영화 '암살자' 넘어선 '실낙원' 개봉 흥행": "실낙원",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(c.guess_news_related_content(title), expected)
        for generic in ["영화", "작품", "극장가", "드라마", "신작", "한국 영화"]:
            self.assertFalse(c.is_reliable_related_content(generic))
        self.assertEqual(c.infer_news_event("'실낙원' 배현성 인터뷰"), "interview")
        self.assertEqual(c.infer_news_event("영화 '실낙원' 예매율 1위"), "audience")
        self.assertEqual(c.infer_news_event("연상호 신작 '실낙원' 예매율 1위, 내일 개봉"), "audience")
        self.assertEqual(c.infer_news_event("신작 '실낙원' 배현성 인터뷰"), "interview")
        self.assertEqual(c.infer_news_event("신작 이야기"), "")

    def test_all_article_urls_resolve_even_when_image_collection_is_disabled(self):
        urls = [f"https://news.google.com/rss/articles/{number}" for number in range(3)]
        rows = [news_row(url) for url in urls]
        with patch.object(c, "ARTICLE_IMAGE_FETCH_LIMIT", 0), \
             patch.object(c, "resolve_article_url", side_effect=lambda url: url.replace("news.google.com/rss/articles", "publisher.example/story")) as resolve, \
             patch.object(c, "fetch_article_image_and_url") as images:
            result = c.enrich_news_images(rows)
        self.assertEqual(resolve.call_count, 3)
        images.assert_not_called()
        self.assertTrue(all(row["source_url"].startswith("https://publisher.example/story/") for row in result))

    def test_assessment_keeps_news_subject_after_a_quoted_comparison_target(self):
        row, = self.collect([entry(title="영화 '암살자' 넘어선 '실낙원' 개봉 흥행 - 기사신문")])
        row['collected_at'] = '2026-10-21T10:00:00+09:00'
        assessed = assess_issues(pd.DataFrame([row]), now='2026-10-21T12:00:00+09:00').iloc[0]
        self.assertTrue(assessed.eligible)
        self.assertEqual(assessed.related_content, '실낙원')
        self.assertEqual(assessed.issue_key, 'work:실낙원')

    def test_news_selection_reason_preserves_specific_collected_event(self):
        cases = [
            ("영화 '실낙원' 예매율 1위", 'audience', '예매율'),
            ("영화 '실낙원' 예매율 1위, 내일 개봉", 'audience', '예매율'),
            ("영화 '실낙원' 배우 인터뷰, 내일 개봉", 'interview', '인터뷰'),
            ("영화 '실낙원' 캐스팅 확정, 내년 공개", 'casting', '캐스팅'),
            ("영화 '실낙원' 개봉 확정", 'release', '개봉'),
            ("드라마 '실낙원' 공개 확정", 'release', '공개'),
        ]
        for title, event_type, expected in cases:
            with self.subTest(title=title):
                row, = self.collect([entry(title=title + ' - 기사신문')])
                row['collected_at'] = '2026-10-21T10:00:00+09:00'
                self.assertEqual(row['event_type'], event_type)
                assessed = assess_issues(pd.DataFrame([row]), now='2026-10-21T12:00:00+09:00').iloc[0]
                self.assertTrue(assessed.eligible)
                self.assertIn(expected, assessed.metric_summary)
                self.assertEqual(present_issue(assessed)['headline'], expected + ' 소식')

    def test_image_budget_does_not_limit_article_resolution_and_shared_urls_resolve_once(self):
        rows = [news_row("https://news.google.com/rss/articles/one"),
                news_row("https://news.google.com/rss/articles/two"),
                news_row("https://news.google.com/rss/articles/one")]
        with patch.object(c, "ARTICLE_IMAGE_FETCH_LIMIT", 1), \
             patch.object(c, "resolve_article_url", side_effect=lambda url: url.replace("news.google.com/rss/articles", "publisher.example/story")) as resolve, \
             patch.object(c, "fetch_article_image_and_url", side_effect=lambda url: (url, "https://images.example/poster.jpg")) as images:
            result = c.enrich_news_images(rows)
        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(images.call_count, 1)
        self.assertTrue(all("publisher.example/story" in row["source_url"] for row in result))

    def test_error_redirect_is_not_saved_as_an_article_and_relay_image_is_not_fetched(self):
        original = "https://news.google.com/rss/articles/one"
        with patch.object(c, "resolve_article_url", return_value="https://publisher.example/login"), \
             patch.object(c, "fetch_article_image_and_url") as images:
            rows = c.enrich_news_images([news_row(original)])
        self.assertEqual(rows[0]["source_url"], original)
        images.assert_not_called()


if __name__ == "__main__":
    unittest.main()
