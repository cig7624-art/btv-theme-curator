import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import collect_issues as c


TODAY = date(2026, 10, 21)
SNAPSHOT = '2026-10-21T00:30:00+00:00'


def meta(**changes):
    result = dict(video_id='video1', title='[닥터X] 공식 예고편', channel='SBS Drama',
                  url='https://www.youtube.com/watch?v=video1', upload_date='2026-07-01',
                  description='SBS 드라마 닥터X', category_id='1',
                  popular_snapshot_at=SNAPSHOT, popular_region='KR', popular_list_position=4,
                  popular_category_id='1', popular_snapshot_complete=True)
    result.update(changes)
    return result


def stats(*values):
    return pd.DataFrame([dict(date=day, video_id='video1', view_count=views,
                              like_count=0, comment_count=0, observed_at='')
                         for day, views in values])


class YoutubePopularTests(unittest.TestCase):
    def setUp(self):
        self.old_status, self.old_errors = c.COLLECTION_STATUS, dict(c.SOURCE_ERRORS)
        c.COLLECTION_STATUS = {'schema_version': 1, 'sources': {}}
        c.SOURCE_ERRORS.clear()
        self.clock = patch.object(c, 'today_kst_date', return_value=TODAY)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        c.COLLECTION_STATUS = self.old_status
        c.SOURCE_ERRORS.clear()
        c.SOURCE_ERRORS.update(self.old_errors)

    def test_current_api_membership_selects_old_video_without_growth(self):
        metrics = c.build_youtube_metrics(meta(), stats(('2026-10-21', 120)), TODAY)
        row = c.youtube_issue_row(meta(), metrics)
        self.assertIsNotNone(row)
        self.assertEqual(row['event_type'], 'popular_video')
        self.assertEqual(row['reference_date'], TODAY.isoformat())
        self.assertEqual(json.loads(row['metrics_json'])['popular_category_id'], '1')
        self.assertIn('한국 인기 영상 목록(영화·애니메이션)', row['description'])

    def test_old_origin_string_or_stale_membership_cannot_select_popular_video(self):
        history = stats(('2026-10-21', 120))
        for changes in [dict(popular_snapshot_at=''), dict(popular_snapshot_at='2026-10-20T00:30:00Z'),
                        dict(popular_region='US'), dict(popular_snapshot_complete=False),
                        dict(popular_list_position=''), dict(popular_category_id='20')]:
            video = meta(query='한국 인기 영상', **changes)
            self.assertEqual(c.youtube_popular_evidence(video, TODAY), {})
            self.assertIsNone(c.youtube_issue_row(video, c.build_youtube_metrics(video, history, TODAY)))

    def test_popular_date_uses_korean_calendar(self):
        video = meta(popular_snapshot_at='2026-10-20T15:01:00Z')
        self.assertTrue(c.youtube_popular_evidence(video, TODAY))
        self.assertFalse(c.youtube_popular_evidence(meta(popular_snapshot_at='2026-10-21T15:00:00Z'), TODAY))

    def test_old_upload_can_be_selected_by_fresh_observed_reaction(self):
        video = meta(popular_snapshot_at='', upload_date='2026-10-01')
        metrics = c.build_youtube_metrics(video, stats(('2026-10-20', 10000), ('2026-10-21', 90000)), TODAY)
        row = c.youtube_issue_row(video, metrics)
        self.assertIsNotNone(row)
        self.assertEqual(row['event_type'], 'observed_reaction')
        initial = c.build_youtube_metrics(video, stats(('2026-10-21', 9000000)), TODAY)
        self.assertIsNone(c.youtube_issue_row(video, initial))

    def test_description_can_name_scene_work_without_using_recommendation_lists(self):
        video = meta(title='저 알바라 해당사항 없는데요', description='SBS 금토드라마 닥터X : 하얀 마피아의 시대의 한 장면입니다\n다른 정보')
        self.assertEqual(c.youtube_related_content(video), '닥터X : 하얀 마피아의 시대')
        self.assertEqual(c.youtube_related_content(meta(title='이런 이야기는 처음', description='추천 작품: 드라마 [다른 작품]')), '')
        self.assertEqual(c.youtube_related_content(meta(title='이런 이야기는 처음', description='채널 [ALTUBE]에서 추천하는 영화 목록')), '')
        self.assertFalse(c.youtube_meta_relevant(meta(title='메이플스토리 게임', description='게임 하이라이트', category_id='20')))

    def test_tracking_includes_current_popular_video_older_than_window(self):
        old = meta()
        recent = meta(video_id='video2', upload_date='2026-10-20', popular_snapshot_at='')
        selected = c.select_youtube_tracking(pd.DataFrame([recent, old]), stats(('2026-10-21', 120)), limit=1)
        self.assertEqual(selected, ['video1'])

    def test_watchlist_refresh_clears_membership_when_api_response_is_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watchlist.csv'
            c.save_csv(pd.DataFrame([meta()]), path, c.WATCHLIST_COLUMNS)
            with patch.object(c, 'YOUTUBE_WATCHLIST_PATH', path):
                refreshed = c.update_youtube_watchlist(pd.DataFrame(columns=c.WATCHLIST_COLUMNS))
            self.assertEqual(refreshed.iloc[0]['popular_snapshot_at'], '')
            self.assertEqual(pd.read_csv(path, sep='|').fillna('').iloc[0]['popular_snapshot_at'], '')

    def test_api_discovery_records_scoped_popular_snapshots_and_filters_games(self):
        def resource(video_id, title, category='1'):
            return dict(id=video_id, snippet=dict(title=title, description='SBS 드라마 닥터X',
                        categoryId=category, channelTitle='SBS', publishedAt='2026-07-01T00:00:00Z'),
                        status=dict(privacyStatus='public'), contentDetails=dict(duration='PT2M'))
        main = resource('video1', '[닥터X] 공식 예고편')
        film = resource('video2', '[닥터X] 공식 예고편')
        game = resource('game1', '메이플스토리 게임', '20')
        scopes = []
        def get(endpoint, params):
            scope = params.get('videoCategoryId', 'all')
            scopes.append(scope)
            return {'items': [main, game] if scope == 'all' else [film] if scope == '1' else []}
        with patch.object(c, 'api_key', return_value='unused-test-key'), patch.object(c, 'YOUTUBE_QUERIES', []), patch.object(c, 'OFFICIAL_YOUTUBE_CHANNELS', []), patch.object(c, 'youtube_get', side_effect=get), patch.object(c, 'fetch_youtube_video_resources', return_value=[main, film, game]), patch.object(c, 'utc_now_iso', return_value=SNAPSHOT):
            found = c.discover_youtube_videos_api()
        self.assertEqual(scopes, ['all', '1', '24'])
        self.assertEqual(set(found.video_id), {'video1', 'video2'})
        self.assertEqual(found.set_index('video_id').loc['video2', 'popular_category_id'], '1')
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_list_state'], 'ok')
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_snapshots'], {'all': SNAPSHOT, '1': SNAPSHOT, '24': SNAPSHOT})
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_failed_lists'], [])

    def test_selected_works_are_not_silently_capped_at_fifteen(self):
        videos = [meta(video_id=f'video{number}', title=f'[닥터X 작품{number}] 공식 예고편') for number in range(17)]
        history = pd.DataFrame([dict(date='2026-10-21', video_id=video['video_id'], view_count=120,
                                    like_count=0, comment_count=0, observed_at='') for video in videos])
        self.assertEqual(len(c.make_youtube_issues(pd.DataFrame(videos), history)), 17)

    def test_failed_popular_requests_do_not_turn_search_matches_into_popular(self):
        resource = dict(id='video1', snippet=dict(title='[닥터X] 공식 예고편', description='SBS 드라마 닥터X',
                        categoryId='1', channelTitle='SBS', publishedAt='2026-07-01T00:00:00Z'),
                        status=dict(privacyStatus='public'), contentDetails=dict(duration='PT2M'))
        def get(endpoint, params):
            if params.get('chart'):
                raise RuntimeError('popular endpoint unavailable')
            return {'items': [{'id': {'videoId': 'video1'}, 'snippet': resource['snippet']}]}
        with patch.object(c, 'api_key', return_value='unused-test-key'), patch.object(c, 'YOUTUBE_QUERIES', ['드라마']), patch.object(c, 'OFFICIAL_YOUTUBE_CHANNELS', []), patch.object(c, 'youtube_get', side_effect=get), patch.object(c, 'fetch_youtube_video_resources', return_value=[resource]):
            found = c.discover_youtube_videos_api()
        self.assertEqual(len(found), 1)
        self.assertEqual(found.iloc[0]['popular_snapshot_at'], '')
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_list_state'], 'failed')
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_snapshots'], {})
        self.assertEqual(c.COLLECTION_STATUS['sources']['youtube']['popular_failed_lists'], ['all', '1', '24'])
        metrics = c.build_youtube_metrics(found.iloc[0].to_dict(), stats(('2026-10-21', 120)), TODAY)
        self.assertIsNone(c.youtube_issue_row(found.iloc[0].to_dict(), metrics))


if __name__ == '__main__':
    unittest.main()
