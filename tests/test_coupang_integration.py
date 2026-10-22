"""Channel news cannot become claimed homepage placement or refreshed old data."""
import copy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from bs4 import BeautifulSoup
from ott_collection import collect_public_homes, save_snapshot, load_snapshot
from ott_editorial import select_editorial_highlights
from ott_view import ott_platform_html, ott_comparison_html

NOW = '2026-10-22T12:00:00+00:00'
OBSERVED = '2026-10-22T01:19:06+00:00'
VIDEO = 'p7y7I-NAKKA'
CHANNEL = 'https://www.youtube.com/channel/UCjn-VbcIkAeXQKCmLJV8YwQ'


def source():
    return {'source_url': CHANNEL, 'source_method': 'verified_watchlist', 'observed_at': OBSERVED,
            'updates': [{'video_id': VIDEO, 'title': '[직장인들 시즌3] 9화 예고편', 'work_title': '직장인들 시즌3',
                         'url': 'https://www.youtube.com/watch?v=' + VIDEO,
                         'image_url': 'https://i.ytimg.com/vi/' + VIDEO + '/hqdefault.jpg',
                         'published_at': '2026-10-21', 'position': 1, 'source_position': 8, 'update_kind': 'trailer'}]}


def snapshot():
    return collect_public_homes(platform_ids=['coupang'], fetcher=lambda _: {'html': '', 'http_status': 403},
                                official_updates_fetcher=lambda **kwargs: source(), now=NOW)


class CoupangIntegrationTests(unittest.TestCase):
    def test_official_updates_leave_home_failure_and_its_counts_untouched(self):
        data = snapshot(); p = next(p for p in data['platforms'] if p['id'] == 'coupang')
        self.assertEqual(p['status'], 'unavailable'); self.assertEqual(p['http_status'], 403)
        self.assertIsNone(p['section_count']); self.assertEqual(p['sections'], [])
        self.assertEqual(p['official_updates_last_success_at'], OBSERVED)
        self.assertEqual(p['official_updates'][0]['work_title'], '직장인들 시즌3')

    def test_clean_roundtrip_preserves_video_links_and_accurate_channel_scope(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'; save_snapshot(snapshot(), path)
            p = next(p for p in load_snapshot(path)['platforms'] if p['id'] == 'coupang')
        self.assertEqual(p['official_updates_source_url'], CHANNEL)
        self.assertIn(VIDEO, p['official_updates'][0]['url'])
        self.assertEqual(p['official_updates_last_success_at'], OBSERVED)
        html = ott_platform_html(p)
        self.assertIn('공식 채널 소식', html); self.assertIn('9화 예고편', html)
        self.assertNotIn('홈 1번째', html); self.assertNotIn('메인 배너', html)
        self.assertIn('2026.10.21 공개', html)

    def test_failed_channel_refresh_keeps_old_original_date_and_excludes_digest(self):
        result = collect_public_homes(snapshot(), ['coupang'], fetcher=lambda _: {'http_status': 403},
                                      official_updates_fetcher=lambda **kwargs: None, now=NOW)
        p = next(p for p in result['platforms'] if p['id'] == 'coupang')
        self.assertEqual(p['official_updates_freshness'], 'stale')
        self.assertEqual(p['official_updates_last_success_at'], OBSERVED)
        self.assertEqual(select_editorial_highlights(result, now=datetime.fromisoformat(NOW)), [])
        self.assertIn('이전 공식 채널 소식', ott_platform_html(p))

    def test_news_can_enter_editorial_but_never_claim_home_prominence(self):
        stories = select_editorial_highlights(snapshot(), now=datetime.fromisoformat(NOW))
        self.assertEqual(len(stories), 1); self.assertEqual(stories[0]['scope'], 'official_channel')
        self.assertEqual(stories[0]['headline'], '직장인들 시즌3')
        self.assertNotIn('홈', stories[0]['selection_reason'])
        markup = ott_comparison_html(snapshot())
        self.assertIn('공식 채널', markup); self.assertNotIn('메인 홍보작', markup)

    def test_video_spoof_links_and_non_official_images_are_removed(self):
        data = snapshot(); p = next(p for p in data['platforms'] if p['id'] == 'coupang')
        for field, value in [('url', 'https://youtube.com.evil.example/watch?v=' + VIDEO),
                             ('image_url', 'https://example.com/fake.jpg')]:
            with self.subTest(field=field), TemporaryDirectory() as directory:
                bad = copy.deepcopy(data); next(p for p in bad['platforms'] if p['id'] == 'coupang')['official_updates'][0][field] = value
                path = Path(directory) / 's.json'; save_snapshot(bad, path)
                self.assertEqual(next(p for p in load_snapshot(path)['platforms'] if p['id'] == 'coupang')['official_updates'], [])
