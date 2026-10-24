"""Director chat is draft-only until a specific version is explicitly adopted."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from ai_director_view import SESSION_KEY, _previous_draft_version, director_draft_card_html
from test_theme_actions import app_fixture


def draft(number=1, count=5):
    return {'draft_id': f'draft-{number}', 'theme_name': f'복수의 설계자 {number}',
            'copy': '돌아갈 곳 없는 선택', 'rationale': '인물들의 복수와 관계 변화를 묶는 기획입니다.',
            'genre': '범죄/드라마', 'mood': '강렬한', 'keywords': ['복수', '관계'],
            'content_search_terms': ['복수극'], 'source_issue': '', 'duplicate_matches': [],
            'contents': [dict(title=f'실제 작품 {i}', content_type='영화', year='2020',
                              poster_url=f'https://images.example.com/{i}.jpg', reason='복수의 대가를 다룹니다.',
                              catalog_status='confirmed_local', source_url='') for i in range(count)]}


def session_with(*drafts):
    values = deepcopy(list(drafts))
    return {'messages': [{'role': 'user', 'content': '복수극 테마를 만들어줘'},
                         {'role': 'assistant', 'content': '기획 초안을 준비했어요.'}],
            'drafts': values, 'versions': [{'version_id': 'v0001', 'label': '새 기획', 'created_at': '', 'drafts': deepcopy(values)}],
            'current_version_id': 'v0001', 'last_usage': {}}


class DirectorCardTests(unittest.TestCase):
    def test_card_undo_follows_restored_parent_chain_not_future_or_abandoned_versions(self):
        first = draft()
        second = deepcopy(first); second['theme_name'] = '미래에 만든 이름'
        branch = deepcopy(first); branch['theme_name'] = '다른 갈래의 이름'
        state = {'drafts': [first], 'current_version_id': 'v1', 'versions': [
            {'version_id': 'v1', 'parent_version_id': None, 'drafts': [first]},
            {'version_id': 'v2', 'parent_version_id': 'v1', 'drafts': [second]},
        ]}
        self.assertIsNone(_previous_draft_version(state, first))
        state['versions'].append({'version_id': 'v3', 'parent_version_id': 'v1', 'drafts': [branch]})
        state['current_version_id'] = 'v3'; state['drafts'] = [branch]
        self.assertEqual(_previous_draft_version(state, branch), 'v1')

    def test_card_shows_copy_rationale_posters_and_each_reason_without_availability_claim(self):
        soup = BeautifulSoup(director_draft_card_html(1, draft()), 'html.parser')
        self.assertEqual(len(soup.select('.theme-poster-image')), 5)
        self.assertEqual(len(soup.select('.director-content-reason')), 5)
        self.assertIn('기획 의도', soup.get_text())
        self.assertIn('돌아갈 곳 없는 선택', soup.get_text())
        self.assertNotIn('서비스 가능', soup.get_text())
        self.assertNotIn('연결된 이슈가', soup.get_text())

    def test_draft_markup_cannot_execute_model_text_or_css_urls(self):
        value = draft()
        value['theme_name'] = '<script>bad()</script>'
        value['copy'] = '<img src=x onerror=bad()>'
        value['contents'][0]['poster_url'] = 'https://images.example.com/a");color:red;background:url(https://evil.test/a)'
        markup = director_draft_card_html(1, value)
        self.assertNotIn('<script>', markup)
        self.assertNotIn('<img src=x', markup)
        self.assertIn('&lt;script&gt;', markup)
        self.assertNotIn('a");color:red', markup)


class DirectorAppTests(unittest.TestCase):
    def test_five_tabs_render_both_workflows_without_starting_either_generator(self):
        with patch('ai_director_view.run_director_turn') as run, patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']
            self.assertFalse(app.exception)
            self.assertEqual([tab.label for tab in app.tabs], ['지금 주목할 이야기', '추천 테마', 'AI 디렉터', 'OTT는 지금', '미디어 인사이트'])
            self.assertTrue(any(button.key == 'generate_weekly_themes' for button in app.button))
            self.assertTrue(any(button.key == 'load_weekly_contents' for button in app.button))
            self.assertTrue(any(button.key == 'open_theme_library' for button in app.button))
            app.run()
            run.assert_not_called(); adopt.assert_not_called()
            state['generate_weekly_themes'].assert_not_called()
            state['persist_recommendations_locally'].assert_not_called()

    def test_direct_brief_creates_without_intro_choices_or_collected_issues(self):
        request = '도시의 밤을 배경으로 어두운 스릴러 큐레이션을 만들어줘.'
        expected = session_with(draft())
        expected['messages'][0]['content'] = request
        with patch('ai_director_view.run_director_turn', return_value=expected) as run, patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']
            self.assertFalse(any(str(button.key or '').startswith('director_quick_') for button in app.button))
            self.assertFalse(any(widget.key in {'director_target', 'director_scope'} for widget in app.selectbox))
            intro = ' '.join(item.value for item in app.markdown)
            self.assertNotIn('director-conversation-empty', intro)
            self.assertNotIn('분위기부터 골라볼까?', intro)
            chat = app.chat_input(key='director_chat_input')
            self.assertFalse(chat.disabled)
            chat.set_value(request).run()
            self.assertFalse(app.exception)
            self.assertEqual(run.call_args.args[0]['messages'], [])
            self.assertEqual(run.call_args.args[1], request)
            self.assertIsNone(run.call_args.kwargs['target_draft_id'])
            self.assertEqual(run.call_args.kwargs['edit_scope'], 'auto')
            self.assertEqual(run.call_args.kwargs['operation'], 'auto')
            self.assertEqual(run.call_args.kwargs['issue_records'], [])
            self.assertEqual(len(app.session_state[SESSION_KEY]['drafts']), 1)
            adopt.assert_not_called()
            state['generate_weekly_themes'].assert_not_called()

    def test_natural_input_produces_only_drafts_and_persists_context_across_reruns(self):
        expected = session_with(draft())
        with patch('ai_director_view.run_director_turn', return_value=expected) as run, patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']
            app.chat_input(key='director_chat_input').set_value('한국 영화와 드라마를 섞어 테마 3개 만들어줘.').run()
            self.assertFalse(app.exception)
            self.assertEqual(run.call_args.args[1], '한국 영화와 드라마를 섞어 테마 3개 만들어줘.')
            self.assertEqual(run.call_args.kwargs['operation'], 'auto')
            self.assertEqual(app.session_state[SESSION_KEY]['drafts'][0]['theme_name'], draft()['theme_name'])
            self.assertIn('https://images.example.com/0.jpg', ' '.join(item.value for item in app.markdown))
            adopt.assert_not_called()
            app.run()
            self.assertFalse(app.exception)
            run.assert_called_once(); adopt.assert_not_called()

    def test_conversation_search_reuses_saved_themes_and_can_start_a_new_brief(self):
        initial = session_with()
        initial['library_search_intent'] = {'genres': ['범죄'], 'positive_keywords': ['공범', '배신']}
        with patch('ai_director_view.run_director_turn', return_value=session_with(draft())) as run, patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']; app.session_state[SESSION_KEY] = initial; app.run()
            self.assertFalse(app.exception)
            markup = ' '.join(item.value for item in app.markdown)
            self.assertIn('라이브러리에서 찾은 테마', markup)
            self.assertIn('class="director-library-card"', markup)
            self.assertTrue(any(button.key == 'director_library_rework_0' for button in app.button))
            references = app.session_state[SESSION_KEY]['library_search_results']
            self.assertEqual(len(references), 1)
            self.assertEqual(references[0]['number'], 1)
            self.assertEqual(references[0]['theme_name'], '공범들의 마지막 선택')
            self.assertEqual(references[0]['theme_id'], 'TEST-1')
            run.assert_not_called(); adopt.assert_not_called()
            app.button(key='director_library_rework_0').click().run()
            self.assertFalse(app.exception)
            self.assertIn('공범들의 마지막 선택', run.call_args.args[1])
            self.assertIn('새로운 큐레이션 테마 1개', run.call_args.args[1])
            self.assertIn('현재 초안들은 그대로 보존', run.call_args.args[1])
            self.assertEqual(run.call_args.kwargs['operation'], 'create')
            self.assertEqual(run.call_args.kwargs['issue_records'], [])
            state['generate_weekly_themes'].assert_not_called()
            adopt.assert_not_called()

    def test_saved_theme_search_without_matches_keeps_the_conversation_open(self):
        initial = session_with()
        initial['library_search_intent'] = {'positive_keywords': ['완전히없는소재']}
        expected = deepcopy(initial); expected['library_search_results'] = []
        with patch('ai_director_view.run_director_turn') as run, app_fixture() as state:
            app = state['app']; app.session_state[SESSION_KEY] = initial; app.run()
            self.assertFalse(app.exception)
            self.assertIn('가까운 저장 테마가 없어요.', ' '.join(item.value for item in app.caption))
            self.assertFalse(app.chat_input(key='director_chat_input').disabled)
            self.assertEqual(app.session_state[SESSION_KEY], expected)
            run.assert_not_called()

    def test_card_edit_targets_one_theme_without_requiring_global_selection(self):
        initial = session_with(draft(1), draft(2), draft(3))
        revised = deepcopy(initial); revised['drafts'][1]['theme_name'] = '더 감각적인 이름'
        with patch('ai_director_view.run_director_turn', return_value=revised) as run, patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']
            app.session_state[SESSION_KEY] = initial
            app.run()
            self.assertFalse(any(widget.key in {'director_target', 'director_scope'} for widget in app.selectbox))
            app.selectbox(key='director_card_scope_draft-2').set_value('title')
            app.text_area(key='director_card_request_draft-2').set_value('이 제목을 더 감각적으로 바꿔줘.')
            app.button(key='FormSubmitter:director_edit_form_draft-2-수정 요청').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(run.call_args.kwargs['target_draft_id'], 'draft-2')
            self.assertEqual(run.call_args.kwargs['edit_scope'], 'title')
            self.assertEqual(run.call_args.kwargs['operation'], 'auto')
            adopt.assert_not_called()
            self.assertEqual(app.session_state[SESSION_KEY]['drafts'][0], initial['drafts'][0])
            self.assertEqual(app.session_state[SESSION_KEY]['drafts'][2], initial['drafts'][2])

    def test_final_adoption_is_explicit_and_same_revision_is_disabled_after_success(self):
        result = {'status': 'success', 'theme_id': 'T999', 'run_id': 'director-run'}
        with patch('ai_director_view.adopt_director_draft', return_value=result) as adopt, app_fixture() as state:
            app = state['app']; app.session_state[SESSION_KEY] = session_with(draft())
            app.session_state['theme_quick_search_input'] = '이전에 검색한 조건'
            app.run(); adopt.assert_not_called()
            app.button(key='director_adopt_draft-1').click().run()
            self.assertFalse(app.exception)
            adopt.assert_called_once()
            self.assertEqual(adopt.call_args.args[0]['draft_id'], 'draft-1')
            self.assertEqual(app.session_state['page'], 'theme_db')
            self.assertEqual(app.selectbox(key='theme_library_source_filter').value, 'AI 디렉터')
            self.assertEqual(app.text_input(key='theme_quick_search_input').value, '')
            self.assertFalse(any(button.key == 'director_open_saved' for button in app.button))
            app.button(key='library_open_ai_director').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['home_sections'], 'AI 디렉터')
            self.assertTrue(app.button(key='director_adopt_draft-1').disabled)
            self.assertEqual(app.button(key='director_adopt_draft-1').label, '채택 완료')
            app.run(); adopt.assert_called_once()

    def test_insufficient_confirmed_contents_cannot_be_adopted(self):
        with patch('ai_director_view.adopt_director_draft') as adopt, app_fixture() as state:
            app = state['app']; app.session_state[SESSION_KEY] = session_with(draft(count=3)); app.run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='director_adopt_draft-1').disabled)
            self.assertIn('3/5편', ' '.join(item.value for item in app.caption))
            adopt.assert_not_called()

    def test_request_failure_preserves_previous_drafts_and_offers_retry(self):
        initial = session_with(draft())
        with patch('ai_director_view.run_director_turn', side_effect=RuntimeError('secret-provider-internals')) as run, app_fixture() as state:
            app = state['app']; app.session_state[SESSION_KEY] = deepcopy(initial); app.run()
            app.chat_input(key='director_chat_input').set_value('제목만 수정해줘').run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state[SESSION_KEY], initial)
            self.assertTrue(any(button.key == 'director_retry_request' for button in app.button))
            self.assertNotIn('secret-provider-internals', ' '.join(item.value for item in app.error))
            run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
