import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

import collect_issues as c


TODAY = date(2026, 10, 21)


def feed_row(**changes):
    row = dict(date='2026-10-21', source='온라인 화제·뉴스', issue_title="영화 '실낙원' 개봉", related_content='실낙원', keywords='영화', description='영화 실낙원 개봉', source_url='https://example.org/story', image_url='')
    row.update(changes)
    return row


def meta(**changes):
    row = dict(video_id='video1', title='[닥터X] 공식 예고편', channel='SBS Drama', url='https://www.youtube.com/watch?v=video1', upload_date='2026-10-19', description='SBS 드라마 닥터X', category_id='1')
    row.update(changes)
    return row


def stats(*entries):
    return pd.DataFrame([dict(date=day, video_id='video1', view_count=views, comment_count=comments, like_count=0, observed_at=timestamp) for day, views, comments, timestamp in entries])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.old_status = c.COLLECTION_STATUS
        self.old_errors = dict(c.SOURCE_ERRORS)
        c.COLLECTION_STATUS = {'schema_version': 1, 'sources': {}}
        c.SOURCE_ERRORS.clear()
        self.clock = patch.object(c, 'today_kst_date', return_value=TODAY)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        c.COLLECTION_STATUS = self.old_status
        c.SOURCE_ERRORS.clear()
        c.SOURCE_ERRORS.update(self.old_errors)

    def test_comma_counts_and_missing_measurements(self):
        self.assertEqual(c.parse_int('22,474'), 22474)
        self.assertEqual(c.parse_int('123.0'), 123)
        self.assertEqual(c.optional_count('0'), 0)
        for value in ['', None, 'bad', 'nan', '-1', '1.5']:
            self.assertIsNone(c.optional_count(value))

    def test_title_extraction_skips_episode_labels_and_promotional_format(self):
        self.assertEqual(c.guess_related_content('[11화 예고] 대사 [너 말고 다른 연애]'), '너 말고 다른 연애')
        self.assertEqual(c.guess_related_content('리뷰 ≪닥터X≫ 3~4화'), '닥터X')
        self.assertEqual(c.guess_related_content('[홍.건.데] 이 조합 | 100일의 거짓말 홍보하러 온 건 맞는데'), '100일의 거짓말')
        for title in ['메이킹', '1위', '[5회 예고]', '저 알바라 해당사항 없는데요']:
            self.assertFalse(c.is_reliable_related_content(title))

    def test_game_highlight_and_music_are_not_content_evidence(self):
        game = {'snippet': {'title': '메이플스토리 게임', 'description': '게임 하이라이트', 'categoryId': '20'}}
        music = {'snippet': {'title': '가수 Official MV', 'description': 'Music video', 'categoryId': '10'}}
        self.assertFalse(c.is_content_relevant_resource(game))
        self.assertFalse(c.is_content_relevant_resource(music))

    def test_unknown_rss_date_is_not_today(self):
        self.assertIsNone(c.parse_entry_date({}))

    def test_unresolved_news_is_partial_and_not_an_official_ott_announcement(self):
        entry = {'title': "영화 '실낙원' 개봉", 'summary': '새 영화 실낙원 개봉', 'link': 'https://news.google.com/rss/articles/id', 'source': {'title':'신문'}, 'published_parsed': (2026,10,21,0,0,0,0,0,0)}
        feed = Mock(bozo=False, entries=[entry])
        with patch.object(c, 'NEWS_QUERY_GROUPS', [('OTT 공식 신작','신작')]), patch.object(c.requests, 'get', return_value=Mock(content=b'rss')), patch.object(c.feedparser, 'parse', return_value=feed), patch.object(c, 'enrich_news_images', side_effect=lambda rows: rows):
            rows = c.collect_google_news()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_type'], 'news')
        self.assertEqual(rows[0]['published_at'], '2026-10-21')
        self.assertEqual(c.COLLECTION_STATUS['sources']['news']['state'], 'partial')
        self.assertEqual(c.COLLECTION_STATUS['sources']['news']['unresolved_original_count'], 1)

    def test_image_enrichment_never_rewrites_rank_or_video_urls(self):
        rows = [feed_row(source='Netflix Top 10', source_url='https://www.netflix.com/tudum/top10'), feed_row(source='KOBIS 박스오피스', source_url='https://www.kobis.or.kr/chart'), feed_row(source='YouTube 반응', source_url='https://www.youtube.com/watch?v=video1')]
        with patch.object(c, 'fetch_article_image_and_url') as fetch:
            result = c.enrich_news_images(rows)
        fetch.assert_not_called()
        self.assertEqual([r['source_url'] for r in result], [r['source_url'] for r in rows])

    def test_error_page_redirect_does_not_replace_article_reference(self):
        original = 'https://about.netflix.com/news/work-announcement'
        response = Mock(url='https://www.netflix.com/unsupportedbrowser', headers={'content-type':'text/html'}, text='<html>unsupported</html>')
        with patch.object(c, 'resolve_article_url', return_value=original), patch.object(c.requests, 'get', return_value=response):
            url, image = c.fetch_article_image_and_url(original)
        self.assertEqual(url, original)
        self.assertEqual(image, '')

    def test_observed_change_does_not_use_future_capture(self):
        history = stats(('2026-10-19', 100, 1, ''), ('2026-10-20', 30100, 5, ''), ('2026-10-21', 900000, 500, ''))
        result = c.build_youtube_metrics(meta(), history, date(2026, 10, 20))
        self.assertEqual(result['views_total'], 30100)
        self.assertEqual(result['views_change'], 30000)
        self.assertEqual(result['comments_change'], 4)
        self.assertEqual(result['observation_precision'], 'date')
        self.assertEqual(result['observation_days'], 1)

    def test_timestamp_observations_use_elapsed_time(self):
        history = stats(('2026-10-19', 100, 1, '2026-10-19T00:00:00Z'), ('2026-10-20', 30100, 5, '2026-10-20T12:00:00Z'))
        result = c.build_youtube_metrics(meta(), history, TODAY)
        self.assertEqual(result['observation_precision'], 'timestamp')
        self.assertEqual(result['observation_days'], 1.5)
        self.assertEqual(result['views_change_per_day'], 20000)
        initial = c.build_youtube_metrics(meta(), history.iloc[:1], TODAY)
        self.assertEqual(initial['reaction_basis'], 'initial')
        self.assertIsNone(initial['views_change'])

    def test_hidden_comments_are_unknown_and_missing_views_are_not_zero(self):
        resources = [{'id': 'video1', 'statistics': {'viewCount': '30100'}, 'status': {'privacyStatus': 'public'}}, {'id': 'missing', 'statistics': {}}]
        with patch.object(c, 'fetch_youtube_video_resources', return_value=resources):
            captured = c.fetch_video_stats_api(['video1', 'missing'])
        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0]['comment_count'])
        history = stats(('2026-10-19', 100, '', ''), ('2026-10-20', 30100, '', ''))
        result = c.build_youtube_metrics(meta(), history, TODAY)
        self.assertIsNone(result['comments_total'])
        self.assertIsNone(result['comments_change'])
        self.assertEqual(result['views_change'], 30000)
        row = c.youtube_issue_row(meta(), result)
        self.assertIsNotNone(row)
        self.assertIn('댓글 변화 미측정', row['description'])

    def test_migration_restores_real_audience_without_changing_original_rows(self):
        original = pd.DataFrame([feed_row(source='KOBIS 박스오피스', issue_title='닥터X 박스오피스 1위', related_content='닥터X', description='2026-10-20 KOBIS 일별 박스오피스 1위. 일일 관객 22,474명, 누적 관객 1,875,609명.', source_url='https://www.kobis.or.kr/chart')])
        result = c.annotate_existing_issues(original, pd.DataFrame(), pd.DataFrame())
        metrics = json.loads(result.iloc[0].metrics_json)
        self.assertEqual(metrics['daily_audience'], 22474)
        self.assertEqual(metrics['cumulative_audience'], 1875609)
        self.assertEqual(result.date.tolist(), original.date.tolist())
        self.assertEqual(result.issue_title.tolist(), original.issue_title.tolist())
        pd.testing.assert_frame_equal(result, c.annotate_existing_issues(result, pd.DataFrame(), pd.DataFrame()))

    def test_official_domains_require_real_host_boundary(self):
        self.assertTrue(c.is_official_url('https://about.netflix.com/announcement'))
        self.assertFalse(c.is_official_url('https://netflix.com.example.org/article'))
        self.assertFalse(c.is_official_url('https://www.wikitree.co.kr/article'))

    def test_netflix_stale_week_is_reported_and_latest_categories_preserved(self):
        def chart(week):
            return pd.DataFrame([dict(country_name='South Korea', week=week, weekly_rank=rank, show_title=f'{category} 작품{rank}', season_title='', category=category) for category in ['Films', 'TV'] for rank in range(1,11)])
        response = Mock(content=b'xlsx')
        with patch.object(c.requests, 'get', return_value=response), patch.object(c.pd, 'read_excel', return_value=chart('2026-10-04')):
            self.assertEqual(c.collect_netflix_top10(), [])
        self.assertEqual(c.COLLECTION_STATUS['sources']['netflix']['state'], 'stale')
        with patch.object(c.requests, 'get', return_value=response), patch.object(c.pd, 'read_excel', return_value=chart('2026-10-18')):
            rows = c.collect_netflix_top10()
        self.assertEqual(len(rows), 20)
        self.assertEqual({r['reference_date'] for r in rows}, {'2026-10-18'})
        self.assertEqual({r['date'] for r in rows}, {'2026-10-18'})
        self.assertEqual(c.COLLECTION_STATUS['sources']['netflix']['category_counts'], {'Films':10, 'TV':10})
        self.assertEqual(c.COLLECTION_STATUS['sources']['netflix']['state'], 'ok')
        with patch.object(c.requests, 'get', return_value=response), patch.object(c.pd, 'read_excel', return_value=chart('2026-10-18').iloc[:9]):
            c.collect_netflix_top10()
        self.assertEqual(c.COLLECTION_STATUS['sources']['netflix']['state'], 'partial')

    def test_missing_kobis_measurement_is_failure_not_zero(self):
        response = Mock()
        response.json.return_value = {'boxOfficeResult': {'showRange': '20261020~20261020', 'dailyBoxOfficeList': [{'movieNm': '작품', 'movieCd': '123', 'rank': '1', 'audiAcc': '100'}]}}
        with patch.dict(c.os.environ, {'KOBIS_API_KEY': 'test-value'}), patch.object(c.requests, 'get', return_value=response):
            self.assertEqual(c.collect_kobis_boxoffice(), [])
        self.assertEqual(c.COLLECTION_STATUS['sources']['kobis']['state'], 'failed')

    def test_ranking_reference_history_is_not_collapsed(self):
        a = feed_row(source='KOBIS 박스오피스', content_id='123', reference_date='2026-10-19')
        b = dict(a, reference_date='2026-10-20')
        self.assertNotEqual(c.exact_content_key(a), c.exact_content_key(b))
        self.assertEqual(len(c.collapse_exact_content_duplicates(pd.DataFrame([a,b]))), 2)

    def test_failed_source_preserves_same_day_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'issue_feed.csv'
            old = feed_row(source='YouTube 반응', source_url='https://www.youtube.com/watch?v=old', issue_title='기존 영상')
            c.save_csv(pd.DataFrame([old]), path, c.ISSUE_COLUMNS)
            c.source_status('youtube', 'failed')
            c.source_status('news', 'ok', 1)
            with patch.object(c, 'ISSUE_PATH', path):
                c.save_issue_feed([feed_row()])
            saved = pd.read_csv(path, sep='|')
            self.assertIn('기존 영상', saved.issue_title.tolist())
            self.assertEqual(len(saved), 2)

    def test_source_diagnostics_and_secrets_are_preserved_safely(self):
        c.source_status('youtube', 'ok', tracking_omitted_count=10)
        c.source_status('youtube', 'partial', 3)
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['tracking_omitted_count'], 10)
        with patch.dict(c.os.environ, {'YOUTUBE_API_KEY': 'test-secret'}):
            error = c.safe_error('https://example.org?key=test-secret&other=1 test-secret')
        self.assertNotIn('test-secret', error)

    def test_main_persists_all_provider_failures_and_retains_data(self):
        with tempfile.TemporaryDirectory() as folder:
            feed_path, status_path = Path(folder)/'feed.csv', Path(folder)/'status.json'
            c.save_csv(pd.DataFrame([feed_row()]), feed_path, c.ISSUE_COLUMNS)
            failure = requests.ConnectionError('provider unavailable')
            with patch.object(c, 'ISSUE_PATH', feed_path), patch.object(c, 'COLLECTION_STATUS_PATH', status_path), patch.object(c, 'collect_google_news', side_effect=failure), patch.object(c, 'collect_kobis_boxoffice', side_effect=failure), patch.object(c, 'collect_netflix_top10', side_effect=failure), patch.object(c, 'discover_youtube_videos', side_effect=failure):
                result = c.main()
            self.assertEqual(result['overall_state'], 'failed')
            self.assertEqual(len(pd.read_csv(feed_path, sep='|')), 1)
            persisted = json.loads(status_path.read_text())
            self.assertEqual(set(persisted['sources']), {'youtube','news','kobis','netflix'})
            self.assertTrue(all(r['state']=='failed' for r in persisted['sources'].values()))


if __name__ == '__main__':
    unittest.main()
