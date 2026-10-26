"""A successful collection does not prove the browser has the new stories."""
from datetime import datetime, timezone
import unittest
from scripts.verify_news_digest_live import observed_matches


class NewsLiveVerificationTests(unittest.TestCase):
    def test_same_count_but_old_articles_cannot_pass_the_refresh_check(self):
        clock = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
        dates = ['2026-10-26T08:00:00+09:00'] * 4
        self.assertTrue(observed_matches('new', 4, 'new', 4, dates, now=clock))
        self.assertFalse(observed_matches('new', 4, 'old', 4, dates, now=clock))
        self.assertFalse(observed_matches('new', 4, 'new', 2, dates[:2], now=clock))

    def test_matching_fingerprint_cannot_hide_stale_or_future_publication(self):
        clock = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
        for value in ('2026-10-18', '2026-10-27', ''):
            self.assertFalse(observed_matches('same', 1, 'same', 1, [value], now=clock))


if __name__ == '__main__':
    unittest.main()
