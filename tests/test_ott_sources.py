"""Regression checks against reduced, actually fetched official home sources."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from bs4 import BeautifulSoup
from ott_collection import PLATFORMS,parse_public_home,save_snapshot,load_snapshot,collect_public_homes
from ott_view import ott_platform_html

FIXTURES=Path(__file__).with_name('fixtures')/'ott'
NOW='2026-10-22T07:00:00+00:00'

def parsed(service):
    name={'tving':'tving-home.html','netflix':'netflix-public.html','disney':'disney-public.html','wavve':'wavve-home.html'}[service]
    spec=next(p for p in PLATFORMS if p['id']==service)
    responses=json.loads((FIXTURES/'wavve-public-responses.json').read_text()) if service=='wavve' else None
    return parse_public_home((FIXTURES/name).read_text(),spec,checked_at=NOW,public_responses=responses)

class SourceRegressionTests(unittest.TestCase):
    def test_tving_actual_home_keeps_live_short_and_single_work_rows_in_order(self):
        result=parsed('tving')
        self.assertEqual(result['status'],'ok')
        self.assertEqual([s['title'] for s in result['sections'][:4]],['첫 화는 티빙이 쏩니다! 1화 무료 보기','실시간 인기 LIVE','고민 끝! 티빙 추천 LIVE 채널','오늘의 티빙 TOP 20'])
        self.assertEqual([s['position'] for s in result['sections']],list(range(1,11)))
        self.assertEqual(result['sections'][1]['kind'],'live')
        self.assertEqual(result['sections'][4]['kind'],'promotion')
        self.assertEqual(result['sections'][5]['kind'],'shorts')

    def test_tving_banner_uses_matched_official_title_and_art_not_undefined_logo(self):
        first=parsed('tving')['promotions'][0]
        self.assertEqual(first['title'],'신인감독 김연경2')
        self.assertIn('/operation/banner/',first['image_url'])
        self.assertIn('.jpg',first['image_url'])
        self.assertNotIn('undefined',str(first))

    def test_netflix_landing_popularity_has_real_names_and_art_without_invented_links(self):
        result=parsed('netflix')
        self.assertEqual(result['status'],'promotion_only')
        self.assertEqual(result['sections'],[])
        listing=result['public_lists'][0]
        self.assertEqual(listing['title'],'지금 뜨는 콘텐츠')
        self.assertEqual([w['title'] for w in listing['works'][:2]],['사이버펑크: 엣지러너','원피스'])
        self.assertTrue(all(w['image_url'].startswith('https://') and not w['url'] for w in listing['works']))
        self.assertFalse(any(w['image_url'].endswith('%29') for w in listing['works']))

    def test_disney_actual_promotions_and_collection_links_have_separate_scope(self):
        result=parsed('disney')
        self.assertEqual(result['status'],'promotion_only')
        self.assertEqual(result['promotions'][0]['title'],'메이드 인 코리아 시즌2')
        self.assertEqual([r['title'] for r in result['public_lists']],['오늘 한국의 TOP 10','컬렉션'])
        self.assertEqual(result['public_lists'][1]['items_kind'],'collections')
        with TemporaryDirectory() as td:
            p=Path(td)/'snapshot.json';save_snapshot({'schema_version':1,'updated_at':NOW,'platforms':[result]},p)
            saved=load_snapshot(p)['platforms'][0]
        self.assertIn('/browse/page-',saved['public_lists'][1]['works'][0]['url'])
        markup=ott_platform_html(saved)
        self.assertIn('공개 홍보 화면의 목록',markup)
        self.assertIn('확인한 컬렉션',markup)

    def test_wavve_only_matching_home_catalogs_fill_actual_shelves(self):
        result=parsed('wavve')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(result['sections'][2]['title'],'오직 웨이브에서')
        self.assertTrue(result['sections'][2]['works'])
        # No observed catalog data for the first two shelves: preserve the real
        # names and positions without borrowing titles from another collection.
        self.assertEqual(result['sections'][0]['works'],[])
        self.assertEqual(result['sections'][1]['works'],[])
        self.assertEqual(result['sections'][3]['kind'],'promotion')
        self.assertEqual(len(result['sections'][3]['works']),1)
        self.assertEqual(result['sections'][3]['works'][0]['title'],'포식자들: 사이버 레커, 전쟁의 기록')
        self.assertTrue(result['promotions'][0]['image_url'].startswith('https://image.wavve.com/'))

    def test_roundtrip_preserves_physical_positions_and_last_good_public_lists(self):
        result=parsed('disney');result['public_lists'][1]['position']=7
        snapshot={'schema_version':1,'updated_at':NOW,'platforms':[result]}
        with TemporaryDirectory() as td:
            p=Path(td)/'s.json';save_snapshot(snapshot,p);old=load_snapshot(p)
        self.assertEqual(old['platforms'][0]['public_lists'][1]['position'],7)
        def failed(_):raise TimeoutError()
        later=collect_public_homes(old,platform_ids=['disney'],fetcher=failed,now='2026-10-23T00:00:00Z')
        disney=next(p for p in later['platforms'] if p['id']=='disney')
        self.assertEqual(disney['freshness'],'stale')
        self.assertEqual(disney['public_lists'][1]['title'],'컬렉션')
        self.assertEqual(disney['last_success_at'],NOW)
