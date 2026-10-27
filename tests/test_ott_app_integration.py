"""The new navigation must preserve existing actions and avoid implicit crawls."""
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import unittest
import streamlit as st
from test_theme_actions import app_fixture

NOW = datetime.now(timezone.utc).isoformat()
TABS = ['지금 주목할 이야기','추천 테마','AI 디렉터','OTT는 지금','미디어 인사이트']

SNAPSHOT = {'schema_version':1,'updated_at':NOW,'platforms':[
    {'id':'tving','status':'ok','checked_at':NOW,'last_success_at':NOW,
     'promotions':[],'sections':[{'id':'integration-home','title':'수집 결과의 원래 목록', 'position':1,
        'kind':'theme','works':[{'title':'연결된 작품','url':'https://www.tving.com/contents/P1','image_url':'https://image.tving.com/P1.jpg'}]}]}]}

class OttAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        st.cache_data.clear()

    def test_order_and_reruns_do_not_request_public_sites(self):
        with patch('ott_collection.collect_public_homes') as collect, patch('ott_title_search_view._search_titles') as search, patch('ott_title_search_view._get_title_availability') as lookup, patch('ott_collection.load_snapshot',return_value=SNAPSHOT), app_fixture(home_tab='OTT는 지금') as state:
            app=state['app']
            self.assertEqual([tab.label for tab in app.tabs],TABS)
            self.assertFalse(app.exception)
            app.session_state['home_sections']='OTT는 지금'
            app.run()
            self.assertFalse(app.exception)
            self.assertIn('수집 결과의 원래 목록',' '.join(m.value for m in app.markdown))
            app.session_state['home_sections']='AI 디렉터'
            app.run()
            self.assertFalse(app.exception)
            collect.assert_not_called()
            search.assert_not_called()
            lookup.assert_not_called()

    def test_title_search_result_survives_filters_and_navigation_without_refetching(self):
        candidate = {'id':'104','title':'기생충','year':'2019','content_type':'movie',
                     'source_url':'https://m.kinolights.com/title/104','fetched_at':NOW}
        availability = {**candidate,'status':'available','providers':[{
            'name':'TVING','offer_type':'subscription','url':'https://www.tving.com/contents/P104'}]}
        with patch('ott_collection.collect_public_homes') as collect, patch('ott_title_search_view._search_titles',return_value=[candidate]) as search, patch('ott_title_search_view._get_title_availability',return_value=availability) as lookup, patch('ott_collection.load_snapshot',return_value=SNAPSHOT), app_fixture(home_tab='OTT는 지금') as state:
            app=state['app']
            app.text_input(key='ott_title_search_query').set_value('기생충')
            app.button(key='ott_title_search_submit').click().run()
            self.assertFalse(app.exception)
            self.assertIn('TVING · 구독',' '.join(m.value for m in app.markdown))
            app.session_state['ott_selected_platform']='tving'
            app.run()
            self.assertFalse(app.exception)
            app.session_state['home_sections']='AI 디렉터'
            app.run()
            app.session_state['home_sections']='OTT는 지금'
            app.run()
            self.assertFalse(app.exception)
            self.assertIn('TVING · 구독',' '.join(m.value for m in app.markdown))
            self.assertEqual(app.session_state['ott_selected_platform'],'tving')
            search.assert_called_once_with('기생충')
            lookup.assert_called_once_with(candidate)
            collect.assert_not_called()

    def test_explicit_refresh_uses_browser_once_and_keeps_result_on_shared_cache(self):
        context=MagicMock();context.__enter__.return_value='public-browser'
        with patch('ott_collection.collect_public_homes',return_value=SNAPSHOT) as collect, patch('ott_collection.save_snapshot') as save, patch('ott_collection.load_snapshot',return_value=SNAPSHOT), patch('ott_collection.PublicBrowser',return_value=context), patch('shutil.which',return_value='/usr/bin/chromium'), app_fixture(home_tab='OTT는 지금') as state:
            app=state['app']
            app.button(key='refresh_ott_public_homes').click().run()
            self.assertFalse(app.exception)
            collect.assert_called_once()
            self.assertEqual(collect.call_args.kwargs['fetcher'],'public-browser')
            save.assert_called_once()
            self.assertIn('수집 결과의 원래 목록',' '.join(m.value for m in app.markdown))
            app.button(key='refresh_ott_public_homes').click().run()
            collect.assert_called_once()

    def test_library_navigation_returns_to_recommendations_without_collection(self):
        with patch('ott_collection.collect_public_homes') as collect, app_fixture() as state:
            app=state['app']
            app.button(key='open_theme_library').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['page'],'theme_db')
            self.assertEqual(app.session_state['home_tab_default'],'추천 테마')
            back=next(b for b in app.button if b.label.startswith('← 추천 화면'))
            back.click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['page'],'home')
            self.assertEqual([t.label for t in app.tabs],TABS)
            collect.assert_not_called()
