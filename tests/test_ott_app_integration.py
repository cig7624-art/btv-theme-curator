"""The new navigation must preserve existing actions and avoid implicit crawls."""
from unittest.mock import patch, MagicMock
import unittest
import streamlit as st
from test_theme_actions import app_fixture

SNAPSHOT = {'schema_version':1,'updated_at':'2026-10-22T07:00:00Z','platforms':[
    {'id':'tving','status':'ok','checked_at':'2026-10-22T07:00:00Z','last_success_at':'2026-10-22T07:00:00Z',
     'promotions':[],'sections':[{'id':'integration-home','title':'수집 결과의 원래 목록', 'position':1,
        'kind':'theme','works':[{'title':'연결된 작품','url':'https://www.tving.com/contents/P1','image_url':''}]}]}]}

class OttAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        st.cache_data.clear()

    def test_order_and_reruns_do_not_request_public_sites(self):
        with patch('ott_collection.collect_public_homes') as collect, patch('ott_collection.load_snapshot',return_value=SNAPSHOT), app_fixture() as state:
            app=state['app']
            self.assertEqual([tab.label for tab in app.tabs],['지금 주목할 이야기','추천 테마','OTT는 지금'])
            self.assertFalse(app.exception)
            app.run()
            self.assertFalse(app.exception)
            collect.assert_not_called()
            self.assertIn('수집 결과의 원래 목록',' '.join(m.value for m in app.markdown))

    def test_explicit_refresh_uses_browser_once_and_keeps_result_on_shared_cache(self):
        context=MagicMock();context.__enter__.return_value='public-browser'
        with patch('ott_collection.collect_public_homes',return_value=SNAPSHOT) as collect, patch('ott_collection.save_snapshot') as save, patch('ott_collection.load_snapshot',return_value=SNAPSHOT), patch('ott_collection.PublicBrowser',return_value=context), patch('shutil.which',return_value='/usr/bin/chromium'), app_fixture() as state:
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
            self.assertEqual([t.label for t in app.tabs],['지금 주목할 이야기','추천 테마','OTT는 지금'])
            collect.assert_not_called()
