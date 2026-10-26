"""A fresh content-news digest must not freeze at the popularity work ranking."""
from copy import deepcopy
import unittest

import pandas as pd

from issue_quality import assess_issues
from news_digest import select_news_digest, news_digest_revision


NOW = "2026-10-26T18:00:00+09:00"


def news(work="새벽", *, published="2026-10-26T15:00:00+09:00", event="release", suffix="1", **changes):
    row = dict(date="2026-10-26", source="온라인 화제·뉴스", source_type="news", related_content=work,
               issue_title=f"영화 '{work}' 공개", description=f"영화 {work} 공개 소식", publisher="연예일보",
               source_url=f"https://publisher.example/article/{suffix}", published_at=published,
               collected_at="2026-10-26T17:00:00+09:00", reference_date=published[:10], event_type=event)
    row.update(changes)
    return row


def assessed(rows):
    return assess_issues(pd.DataFrame(rows), now=NOW)


class NewsDigestTests(unittest.TestCase):
    def test_revision_changes_with_new_public_story_not_repeated_collection(self):
        original = assessed([news()])
        captured = original.copy()
        captured["collected_at"] = "2026-10-26T17:30:00+09:00"
        self.assertEqual(news_digest_revision(original), news_digest_revision(captured))
        captured["issue_title"] = "영화 '새벽' 추가 캐스팅 확정"
        self.assertNotEqual(news_digest_revision(original), news_digest_revision(captured))
        self.assertEqual(len(news_digest_revision(original.iloc[0:0])), 64)

    def test_four_different_works_are_sorted_by_publication_not_capture_or_work_key(self):
        data = assessed([news(work, published=f"2026-10-26T{hour}:00:00+09:00", suffix=work)
                         for work, hour in [("하늘", "10"), ("바다", "15"), ("새벽", "17"), ("달빛", "13"), ("별빛", "11")]])
        selected = select_news_digest(data, now=NOW)
        self.assertEqual(selected.related_content.tolist(), ["새벽", "바다", "달빛", "별빛"])
        self.assertEqual(len(selected), 4)

    def test_same_work_same_event_repeated_by_publishers_is_one_story(self):
        data = assessed([news(suffix="older", published="2026-10-26T10:00:00+09:00"),
                         news(suffix="newer", published="2026-10-26T16:00:00+09:00", publisher="문화일보",
                              issue_title="신작 영화 '새벽' 공개 발표"),
                         news("별빛", suffix="other")])
        selected = select_news_digest(data, now=NOW)
        self.assertEqual(len(selected), 2)
        self.assertIn("https://publisher.example/article/newer", selected.source_url.tolist())
        self.assertNotIn("https://publisher.example/article/older", selected.source_url.tolist())

    def test_distinct_works_precede_second_event_and_same_work_never_fills_all_four(self):
        rows = [news(event="release", suffix="release"),
                news(event="interview", suffix="interview", issue_title="영화 '새벽' 배우 인터뷰"),
                news(event="casting", suffix="casting", issue_title="영화 '새벽' 캐스팅 확정"),
                news("달빛", suffix="other", published="2026-10-25T10:00:00+09:00")]
        selected = select_news_digest(assessed(rows), now=NOW)
        self.assertEqual(len(selected), 3)
        self.assertEqual(selected.related_content.value_counts().to_dict(), {"새벽": 2, "달빛": 1})

    def test_different_audience_events_are_not_collapsed_as_one_popularity_work(self):
        data = assessed([news(event="audience", suffix="advance", issue_title="영화 '새벽' 예매율 1위"),
                         news(event="audience", suffix="opening", issue_title="영화 '새벽' 박스오피스 1위")])
        self.assertEqual(len(select_news_digest(data, now=NOW)), 2)

    def test_publication_guards_ignore_stale_unknown_future_and_recent_recapture(self):
        data = assessed([news(suffix="valid"), news(suffix="old", published="2026-10-18T10:00:00+09:00"),
                         news(suffix="unknown", published=""), news(suffix="future", published="2026-10-26T20:00:00+09:00")])
        # Even an old/mistaken eligibility cache cannot renew publication dates.
        data["eligible"] = True
        self.assertEqual(select_news_digest(data, now=NOW).source_url.tolist(), ["https://publisher.example/article/valid"])

    def test_newly_saved_article_replaces_the_old_top_story_without_mutating_source(self):
        before = assessed([news("달빛", published="2026-10-25T10:00:00+09:00", suffix="old")])
        baseline = deepcopy(before)
        self.assertEqual(select_news_digest(before, now=NOW).related_content.tolist(), ["달빛"])
        after = pd.concat([before, assessed([news("새벽", suffix="fresh")])], ignore_index=True)
        self.assertEqual(select_news_digest(after, now=NOW).related_content.tolist(), ["새벽", "달빛"])
        pd.testing.assert_frame_equal(before, baseline)

    def test_non_news_and_unverified_relay_cannot_fill_empty_slots(self):
        data = assessed([news(suffix="valid"), news("별빛", suffix="relay", source_url="https://news.google.com/rss/articles/unverified"),
                         news("하늘", suffix="video", source="YouTube 반응", source_type="youtube_reaction",
                              source_url="https://youtube.com/watch?v=abcdefghijk")])
        data["eligible"] = True
        self.assertEqual(len(select_news_digest(data, now=NOW)), 1)
        self.assertTrue(select_news_digest(data, limit=0, now=NOW).empty)


if __name__ == "__main__":
    unittest.main()
