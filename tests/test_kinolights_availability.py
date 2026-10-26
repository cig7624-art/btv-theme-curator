"""Regression checks against reduced, actually fetched public Kinolights data."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import requests
import kinolights_availability as kino

FIXTURE = json.loads((Path(__file__).with_name('fixtures') / 'kinolights' / 'availability-public.json').read_text())
SELECTED = dict(id='66957', title='기생충', year='2019', content_type='영화',
                source_url='https://m.kinolights.com/title/66957')
NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def detail():
    return deepcopy(FIXTURE['details']['66957'])


def one_offer(**changes):
    data = detail()
    data['content']['vodOfferItems'] = [{**data['content']['vodOfferItems'][0], **changes}]
    return data


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        kino.clear_cache()

    def test_actual_same_title_remakes_keep_distinct_ids_and_years(self):
        rows = kino._parse_search_data(FIXTURE['searches']['괴물'])
        self.assertEqual(len(rows), 8)
        for identity in [('8374', '2006'), ('122296', '2023'), ('88720', '2021')]:
            self.assertIn(identity, [(row['id'], row['year']) for row in rows])
        same_year = deepcopy(FIXTURE['searches']['괴물'])
        same_year['searchContentsByKeyword']['items'][1]['content']['openYear'] = '1951'
        self.assertEqual(len(kino._parse_search_data(same_year)), 8)

    def test_single_character_and_english_only_titles_are_valid(self):
        data = {'searchContentsByKeyword': {'items': [
            {'contentId': 1, 'content': {'id': '1', 'titleKr': '봄', 'openYear': '2000'}},
            {'contentId': 2, 'content': {'id': '2', 'titleKr': None, 'titleEn': 'Film', 'openYear': '2001'}},
        ], 'totalCount': 2}}
        self.assertEqual([row['title'] for row in kino._parse_search_data(data)], ['봄', 'Film'])

    def test_actual_offers_separate_subscription_rental_purchase_and_collapse_tving_variants(self):
        result = kino._parse_detail_data(detail(), SELECTED, now=NOW)
        self.assertEqual(result['status'], 'available')
        pairs = [(row['provider_id'], row['offer_type']) for row in result['providers']]
        self.assertEqual(len(pairs), 9)
        self.assertEqual(pairs.count(('tving', 'subscription')), 1)
        for pair in [('netflix', 'subscription'), ('watcha-play', 'subscription'),
                     ('wavve', 'rent'), ('wavve', 'subscription'), ('apple', 'rent'), ('apple', 'buy')]:
            self.assertIn(pair, pairs)
        apple_buy = next(row for row in result['providers'] if row['provider_id'] == 'apple' and row['offer_type'] == 'buy')
        self.assertEqual(apple_buy['name'], 'Apple TV')
        self.assertEqual(apple_buy['price'], 7500)
        self.assertTrue(apple_buy['url'].startswith('https://tv.apple.com/kr/movie/'))

    def test_actual_coupang_rental_purchase_do_not_become_subscription(self):
        selected = dict(title='기생춘', year='2020', source_url='https://m.kinolights.com/title/95593')
        result = kino._parse_detail_data(FIXTURE['details']['95593'], selected, now=NOW)
        self.assertEqual({row['offer_type'] for row in result['providers'] if row['provider_id'] == 'coupang-play'}, {'rent', 'buy'})

    def test_inactive_removed_future_and_expired_offers_are_not_current_services(self):
        for changes in [dict(isActive=False), dict(isRemove=True),
                        dict(openDate='2026-10-27T00:00:00Z'), dict(expireDate='2026-10-25T23:00:00Z')]:
            with self.subTest(changes=changes):
                result = kino._parse_detail_data(one_offer(**changes), SELECTED, now=NOW)
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['providers'], [])
        self.assertEqual(kino._parse_detail_data(one_offer(openDate='2026-10-26T09:00:00+09:00'), SELECTED, now=NOW)['status'], 'available')

    def test_missing_or_unreadable_offer_state_is_unknown_not_no_service(self):
        for changes in [dict(isActive=None), dict(isRemove=None), dict(isDone=False),
                        dict(expireDate='not-a-date'), dict(openDate='2026-10-25T00:00:00'), dict(providerTechnicalName=None)]:
            with self.subTest(changes=changes):
                self.assertEqual(kino._parse_detail_data(one_offer(**changes), SELECTED, now=NOW)['status'], 'unknown')
        data = detail()
        del data['content']['vodOfferItems']
        data['content']['synopsis'] = '넷플릭스 인기작, 티빙 추천작'
        self.assertEqual(kino._parse_detail_data(data, SELECTED)['status'], 'unknown')
        data['content']['vodOfferItems'] = []
        self.assertEqual(kino._parse_detail_data(data, SELECTED)['status'], 'unavailable')

    def test_unknown_monetization_does_not_guess_subscription_from_zero_price(self):
        result = kino._parse_detail_data(one_offer(monetizationType='new-value', retailPrice=0), SELECTED, now=NOW)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['providers'][0]['offer_type'], 'unknown')
        self.assertEqual(kino._parse_detail_data(one_offer(url='javascript:alert(1)'), SELECTED)['providers'][0]['url'], '')

    def test_wrong_title_year_or_id_cannot_display_another_works_offers(self):
        for field, value in [('id', '111963'), ('titleKr', '기생충 흑백판'), ('openYear', '2020')]:
            data = detail()
            data['content'][field] = value
            with self.subTest(field=field):
                result = kino._parse_detail_data(data, SELECTED)
                self.assertEqual(result['status'], 'identity_mismatch')
                self.assertEqual(result['providers'], [])
        data = detail()
        data['content']['openYear'] = None
        self.assertEqual(kino._parse_detail_data(data, SELECTED)['status'], 'unknown')

    def test_unreadable_search_is_different_from_explicit_zero_hits(self):
        with patch.object(kino, '_graphql', return_value={'searchContentsByKeyword': {'items': [], 'totalCount': 0}}):
            self.assertEqual(kino.search_titles('없는 제목'), [])
        for data in [{}, {'searchContentsByKeyword': {'items': [], 'totalCount': 1}}, {'searchContentsByKeyword': {'items': [None]}}]:
            with patch.object(kino, '_graphql', return_value=data), self.assertRaises(kino.AvailabilityLookupError):
                kino.search_titles('괴물')

    def test_fetch_failure_does_not_claim_no_service_or_get_cached(self):
        with patch.object(kino, '_graphql', side_effect=kino.AvailabilityLookupError('연결 실패')) as fetch:
            for _ in range(2):
                self.assertEqual(kino.get_title_availability(SELECTED)['status'], 'fetch_error')
            self.assertEqual(fetch.call_count, 2)
        with patch.object(kino.requests, 'post', side_effect=requests.Timeout), self.assertRaises(kino.AvailabilityLookupError):
            kino.search_titles('괴물')

    def test_only_valid_public_title_addresses_can_be_looked_up(self):
        urls = ['https://other.example/title/1', 'http://m.kinolights.com/title/1',
                'https://user:password@m.kinolights.com/title/1', 'https://m.kinolights.com:bad/title/1',
                'https://m.kinolights.com/search?keyword=test']
        with patch.object(kino, '_graphql') as fetch:
            for url in urls:
                self.assertEqual(kino.get_title_availability({**SELECTED, 'source_url': url})['status'], 'identity_mismatch')
            fetch.assert_not_called()

    def test_normal_queries_only_use_public_content_metadata_and_offers(self):
        self.assertFalse(re.search(r'isMy|watched|watchList|user|auth|review|introspection|__schema', kino.SEARCH_QUERY + kino.DETAIL_QUERY, re.I))
        self.assertIn('searchContentsByKeyword', kino.SEARCH_QUERY)
        self.assertIn('vodOfferItems', kino.DETAIL_QUERY)

    def test_cache_is_short_lived_copied_and_bounded(self):
        with patch.object(kino.time, 'monotonic', return_value=0), patch.object(kino, '_graphql', return_value=detail()) as fetch:
            result = kino.get_title_availability(SELECTED)
            result['providers'].clear()
            self.assertTrue(kino.get_title_availability(SELECTED)['providers'])
            self.assertEqual(fetch.call_count, 1)
        with patch.object(kino.time, 'monotonic', return_value=kino.CACHE_TTL_SECONDS + 1), patch.object(kino, '_graphql', return_value=detail()) as fetch:
            kino.get_title_availability(SELECTED)
            self.assertEqual(fetch.call_count, 1)
        for index in range(kino.CACHE_MAX_ENTRIES + 10):
            kino._remember(('test', index), index)
        self.assertEqual(len(kino._CACHE), kino.CACHE_MAX_ENTRIES)
        with patch.object(kino, '_graphql', return_value=FIXTURE['searches']['기생충']) as fetch:
            self.assertTrue(kino.search_titles('기생충')[0]['fetched_at'])
            kino.search_titles('기생충')
            self.assertEqual(fetch.call_count, 1)

    def test_known_expiry_and_future_opening_end_cache_before_ten_minutes(self):
        for changes, before, after in [
            (dict(expireDate='2026-10-26T00:01:00Z'), 'available', 'unavailable'),
            (dict(openDate='2026-10-26T00:01:00Z'), 'unavailable', 'available'),
        ]:
            kino.clear_cache()
            with patch.object(kino, 'datetime', wraps=datetime) as clock, patch.object(kino, '_graphql', return_value=one_offer(**changes)) as fetch:
                clock.now.return_value = NOW
                with patch.object(kino.time, 'monotonic', return_value=0):
                    self.assertEqual(kino.get_title_availability(SELECTED)['status'], before)
                clock.now.return_value = datetime(2026, 10, 26, 0, 2, tzinfo=timezone.utc)
                with patch.object(kino.time, 'monotonic', return_value=120):
                    self.assertEqual(kino.get_title_availability(SELECTED)['status'], after)
                self.assertEqual(fetch.call_count, 2)


if __name__ == '__main__':
    unittest.main()
