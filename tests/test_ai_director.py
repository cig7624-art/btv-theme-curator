"""Real-model plans may only change the requested draft and requested fields."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from ai_director import new_director_session, run_director_turn, restore_director_version
from content_pipeline import resolve_director_contents
from llm_theme_generator import DirectorPlan, ThemeGenerationError, generate_director_plan


def catalog():
    return pd.DataFrame([{"content_id": f"C{i}", "title": f"실제 작품 {i}", "type": "드라마", "year": "2020", "genre": "스릴러", "tags": "복수,반전"} for i in range(1, 12)])


def suggestions(start=1):
    return [{"content_id": f"C{i}", "title": f"실제 작품 {i}", "content_type": "드라마", "year": "2020", "reason": "복수의 선택이 관계를 바꾸는 작품"} for i in range(start, start + 5)]


def proposal(name="복수의 대가를 치르는 주인공", start=1, **changes):
    return {"theme_name": name, "copy": "되돌아갈 수 없는 선택", "rationale": "복수의 선택과 결과를 비교하는 기획",
            "genre": "드라마/스릴러", "mood": "자극적/긴장", "keywords": ["복수", "관계"],
            "content_search_terms": ["복수 드라마"], "creation_angle": "인물형", "source_issue_keys": [],
            "contents": suggestions(start), **changes}


def create_plan(count=3):
    return DirectorPlan.model_validate({"action": "create", "message": f"{count}개 초안을 만들었어요.",
                        "themes": [proposal(f"복수를 선택한 주인공 {index}") for index in range(1, count + 1)], "edits": []})


def edit_plan(draft_id, scope="title", **changes):
    return DirectorPlan.model_validate({"action": "revise", "message": "선택한 항목을 수정했어요.",
                        "themes": [], "edits": [{"target_draft_id": draft_id, "scope": scope, **changes}]})


def client(plan):
    value = Mock()
    value.responses.parse.return_value = SimpleNamespace(output_parsed=plan,
                        usage=SimpleNamespace(input_tokens=100, output_tokens=50))
    return value


def resolver(items, local_contents, **kwargs):
    return resolve_director_contents(items, local_contents, enrich_posters=False, **kwargs)


class AiDirectorTests(unittest.TestCase):
    def setUp(self):
        self.themes = pd.DataFrame(columns=["theme_id", "theme_name", "copy", "trigger_keywords", "genre", "mood"])
        self.catalog = catalog()

    def turn(self, state, request, plan, **changes):
        return run_director_turn(state, request, themes=self.themes, contents=self.catalog, issue_records=[],
                                 api_key="", client=client(plan), content_resolver=resolver, **changes)

    def initial(self):
        return self.turn(new_director_session(), "복수극 테마 3개 만들어줘.", create_plan())

    def test_free_prompt_creates_real_catalog_drafts_without_issues_or_any_storage(self):
        state = new_director_session()
        sdk = client(create_plan())
        with patch('llm_theme_generator.persist_recommendations_locally') as persist, patch('llm_theme_generator.persist_files_to_github') as remote:
            result = run_director_turn(state, "한국 영화와 드라마를 섞은 복수극 테마 3개 만들어줘.",
                        themes=self.themes, contents=self.catalog, issue_records=[], api_key="", client=sdk, content_resolver=resolver)
        self.assertEqual(state['drafts'], [])
        self.assertEqual(len(result['drafts']), 3)
        self.assertEqual(len(result['versions']), 1)
        self.assertTrue(all(len(row['contents']) == 5 for row in result['drafts']))
        self.assertTrue(all(item['availability_status'] == 'not_checked' for row in result['drafts'] for item in row['contents']))
        payload = json.loads(sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertEqual(payload['actual_content_catalog'][0]['content_id'], 'C1')
        self.assertIn('한국 영화', payload['request'])
        self.assertNotIn('recent_issues', payload)
        persist.assert_not_called(); remote.assert_not_called()

    def test_open_ideation_is_a_real_conversation_without_creating_cards_or_versions(self):
        state = new_director_session(); before = deepcopy(state)
        answer = ('1. 가족의 비밀이 뒤집히는 순간 — 관계의 반전으로 묶어볼 수 있어요.\n'
                  '2. 도시를 떠난 사람들 — 낯선 공간이 바꾸는 삶을 비교해요.\n'
                  '3. 능력보다 팀워크 — 엇갈린 인물이 협력하는 과정을 모아요.\n\n'
                  '긴장감 있는 이야기와 편안한 이야기 중 어느 쪽이 끌리세요?')
        sdk = client(DirectorPlan(action='reply', message=answer))
        with patch('content_pipeline.resolve_director_contents', side_effect=AssertionError('ideation must not resolve content')):
            result = run_director_turn(state, '뭐 만들어볼까?', themes=self.themes, contents=self.catalog, client=sdk)
        self.assertEqual(state, before)
        self.assertEqual(result['drafts'], [])
        self.assertEqual(result['versions'], [])
        self.assertEqual(result['messages'][-1]['content'], answer)
        self.assertEqual(result['last_usage']['api_call_count'], 1)
        sdk.responses.parse.assert_called_once()
        prompt = sdk.responses.parse.call_args.kwargs['input'][0]['content']
        self.assertIn("'뭐 만들어볼까?'", prompt)
        self.assertIn('기획 방향 2~3개', prompt)
        self.assertIn('action=reply, themes=[], edits=[]', prompt)

    def test_selecting_a_conversation_direction_creates_without_requiring_an_existing_draft(self):
        state = self.turn(new_director_session(), '뭐 만들어볼까?',
                          DirectorPlan(action='reply', message='1. 관계의 반전\n2. 도시를 떠난 사람들\n어느 방향이 좋으세요?'))
        sdk = client(create_plan())
        result = run_director_turn(state, '2번 방향으로 테마 3개 만들어줘.', themes=self.themes,
                                  contents=self.catalog, client=sdk, content_resolver=resolver)
        self.assertEqual(len(result['drafts']), 3)
        inputs = sdk.responses.parse.call_args.kwargs['input']
        self.assertEqual(inputs[2]['content'], state['messages'][1]['content'])
        self.assertIsNone(json.loads(inputs[-1]['content'])['selected_draft_id'])

    def test_conversation_direction_is_not_mistaken_for_an_existing_draft_target(self):
        state = self.initial()
        state = self.turn(state, '새 방향을 제안해줘.', DirectorPlan(action='reply', message='1. 시간의 반복\n2. 도시의 밤'))
        with patch('ai_director.generate_director_plan', return_value=(create_plan(), {'api_call_count': 1})) as planner:
            result = run_director_turn(state, '2번 방향으로 테마 3개 만들어줘.', themes=self.themes,
                                      contents=self.catalog, content_resolver=resolver)
        self.assertIsNone(planner.call_args.kwargs['target_draft_id'])
        self.assertEqual(len(result['drafts']), 3)

    def test_director_ignores_legacy_issue_arguments_and_does_not_attach_issue_provenance(self):
        issue = {'issue_key': 'I01', 'issue_title': 'DO_NOT_INJECT_WEEKLY_ISSUE', 'title': 'DO_NOT_INJECT_WEEKLY_ISSUE'}
        raw = proposal(source_issue_keys=['I01'])
        sdk = client(DirectorPlan(action='create', message='요청하신 기획 초안입니다.', themes=[raw]))
        result = run_director_turn(new_director_session(), '복수극 테마 1개 만들어줘.',
                                  themes=self.themes, contents=self.catalog, issue_records=[issue],
                                  client=sdk, content_resolver=resolver)
        payload = json.loads(sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertNotIn('recent_issues', payload)
        self.assertNotIn('DO_NOT_INJECT_WEEKLY_ISSUE', json.dumps(payload))
        self.assertEqual(result['drafts'][0]['source_issue_keys'], [])
        self.assertEqual(result['drafts'][0]['source_issue'], '')
        direct_sdk = client(DirectorPlan(action='reply', message='다른 관점도 함께 생각해 볼까요?'))
        generate_director_plan(request='다른 방향을 알려줘.', messages=[], drafts=result['drafts'],
                               themes=self.themes, contents=self.catalog, issue_records=[issue],
                               api_key='', model='configured-model', client=direct_sdk)
        direct_payload = json.loads(direct_sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertNotIn('recent_issues', direct_payload)
        self.assertNotIn('source_issue', direct_payload['current_drafts'][0])

    def test_library_search_reuses_semantic_intent_in_one_call_without_creating_drafts(self):
        state = self.initial(); before = deepcopy(state)
        intent = {'interpreted_request': '잔인하지 않은 복수 테마 찾기', 'anchor_titles': ['실제 작품 1'],
                  'genres': ['드라마'], 'moods': ['긴장'], 'subjects': ['복수'], 'narrative_elements': ['관계의 반전'],
                  'positive_keywords': ['복수', '복수', '관계'], 'negative_keywords': ['잔인함'], 'result_count': 5,
                  'suggested_themes': [{'theme_name': 'DB 결과로 가장하면 안 되는 제안', 'theme_copy': '가상 결과', 'genre': '드라마', 'mood': '긴장'}]}
        sdk = client(DirectorPlan(action='reply', message='저장된 테마에서 조건에 맞는 항목을 찾아볼게요.', library_search=intent))
        result = run_director_turn(state, '라이브러리에서 잔인하지 않은 복수 테마를 찾아줘.',
                                  themes=self.themes, contents=self.catalog, client=sdk)
        self.assertEqual(result['library_search_intent']['positive_keywords'], ['복수', '관계'])
        self.assertEqual(result['library_search_intent']['negative_keywords'], ['잔인함'])
        self.assertEqual(result['library_search_intent']['suggested_themes'], [])
        self.assertEqual(result['drafts'], before['drafts'])
        self.assertEqual(result['versions'], before['versions'])
        self.assertEqual(state, before)
        sdk.responses.parse.assert_called_once()
        followed = self.turn(result, '그 조건으로 테마 3개 만들어줘.', create_plan())
        self.assertIsNone(followed['library_search_intent'])
        self.assertEqual(followed['messages'][2:4], result['messages'][2:4])

    def test_blank_model_reply_does_not_claim_a_draft_was_created(self):
        state = self.initial(); before = deepcopy(state)
        with self.assertRaisesRegex(ThemeGenerationError, '답변이 비어'):
            self.turn(state, '뭐 만들어볼까?', DirectorPlan(action='reply', message='  '))
        self.assertEqual(state, before)

    def test_retrieved_library_matches_are_numbered_context_for_subsequent_creation(self):
        state = self.initial()
        state['library_search_intent'] = {'positive_keywords': ['도시']}
        state['library_search_results'] = [
            {'theme_id': 'T11', 'theme_name': '도시를 벗어난 관계', 'copy': '낯선 곳에서 다시 시작하다', 'genre': '드라마', 'mood': '편안함'},
            {'theme_id': 'T12', 'theme_name': '한밤의 공범들', 'copy': '도시의 비밀을 함께 감추다', 'genre': '범죄', 'mood': '긴장'}]
        before = deepcopy(state)
        sdk = client(create_plan())
        result = run_director_turn(state, '그중 2번으로 테마 3개 기획해줘.', themes=self.themes,
                                  contents=self.catalog, client=sdk, content_resolver=resolver)
        payload = json.loads(sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertIsNone(payload['selected_draft_id'])
        self.assertEqual(payload['saved_library_matches'][1]['number'], 2)
        self.assertEqual(payload['saved_library_matches'][1]['theme_id'], 'T12')
        self.assertEqual(payload['saved_library_matches'][1]['theme_name'], '한밤의 공범들')
        self.assertEqual(len(result['drafts']), 3)
        self.assertEqual(state, before)

    def test_library_new_planning_operation_ignores_quoted_edit_words_and_appends(self):
        state = self.initial(); before = deepcopy(state)
        reference = ('라이브러리의 다음 테마에서 출발해 새로운 큐레이션 테마 1개를 기획해줘. '
                     '기존 초안은 유지해줘.\n'
                     '참고 테마명: 한 편만 보려다 밤새는 스릴러\n'
                     '참고 카피: 결말까지 멈출 수 없는 이야기. 제목만 바꿔줘라는 말이 시작한 반전\n'
                     '참고 장르: 스릴러')
        plan = DirectorPlan(action='create', creation_mode='replace', message='새 초안을 추가했어요.',
                            themes=[proposal('잠들 수 없는 도시의 밤')])
        sdk = client(plan)
        result = run_director_turn(state, reference, themes=self.themes, contents=self.catalog, client=sdk,
                                  content_resolver=resolver, operation='create', edit_scope='title',
                                  target_draft_id='quoted-metadata-is-not-a-draft-target')
        self.assertEqual(result['drafts'][:3], before['drafts'])
        self.assertEqual(len(result['drafts']), 4)
        self.assertEqual(result['drafts'][3]['theme_name'], '잠들 수 없는 도시의 밤')
        self.assertEqual(state, before)
        payload = json.loads(sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertEqual(payload['operation'], 'create')
        self.assertEqual(payload['edit_scope'], 'auto')
        self.assertIsNone(payload['selected_draft_id'])

    def test_explicit_new_planning_rejects_model_revisions_or_replies_without_mutating(self):
        state = self.initial(); before = deepcopy(state)
        for plan in (edit_plan(state['drafts'][0]['draft_id'], theme_name='원본을 덮어쓰면 안 됨'),
                     DirectorPlan(action='reply', message='어떤 방향이 좋을까요?')):
            with self.subTest(action=plan.action), self.assertRaisesRegex(ThemeGenerationError, '새 기획 요청'):
                self.turn(state, '참고 테마에서 새롭게 기획해줘.', plan, operation='create')
            self.assertEqual(state, before)

    def test_library_ordinal_survives_general_reply_before_next_creation(self):
        state = self.initial()
        search = DirectorPlan(action='reply', message='저장한 테마에서 찾아볼게요.',
                              library_search={'interpreted_request': '도시의 밤을 다룬 테마'})
        state = self.turn(state, '라이브러리에서 도시의 밤을 다룬 테마 찾아줘.', search)
        actual_matches = [
            {'number': 1, 'theme_id': 'T11', 'theme_name': '도시를 벗어난 관계', 'copy': '다시 시작하다', 'genre': '드라마'},
            {'number': 2, 'theme_id': 'T12', 'theme_name': '한밤의 공범들', 'copy': '비밀을 함께 감추다', 'genre': '범죄'}]
        state['library_search_results'] = deepcopy(actual_matches)
        state = self.turn(state, '어떤 관점으로 더 확장할 수 있을까?',
                          DirectorPlan(action='reply', message='관계의 비밀이나 도시의 이면을 살려볼 수 있어요.'))
        self.assertIsNone(state['library_search_intent'])
        self.assertEqual(state['library_search_results'], actual_matches)
        sdk = client(create_plan())
        run_director_turn(state, '그중 2번으로 테마 3개 기획해줘.', themes=self.themes,
                          contents=self.catalog, client=sdk, content_resolver=resolver)
        payload = json.loads(sdk.responses.parse.call_args.kwargs['input'][-1]['content'])
        self.assertIsNone(payload['selected_draft_id'])
        self.assertEqual(payload['saved_library_matches'][1]['number'], 2)
        self.assertEqual(payload['saved_library_matches'][1]['theme_id'], 'T12')

    def test_second_theme_title_only_preserves_every_other_user_field_and_other_drafts(self):
        state = self.initial()
        before = deepcopy(state)
        target = state['drafts'][1]['draft_id']
        plan = edit_plan(target, scope='all', theme_name='칼끝에서 다시 태어난 관계', copy='잘못 바꾸려는 카피',
                         contents=suggestions(6), genre='코미디')
        with patch('content_pipeline.resolve_director_contents', side_effect=AssertionError('title edit must not rematch')):
            result = self.turn(state, '2번 테마가 좋은데 더 감각적인 제목으로 바꿔줘.', plan)
        expected = deepcopy(before['drafts'][1]); expected['theme_name'] = '칼끝에서 다시 태어난 관계'; expected['duplicate_matches'] = []
        self.assertEqual(result['drafts'][1], expected)
        self.assertEqual(result['drafts'][0], before['drafts'][0])
        self.assertEqual(result['drafts'][2], before['drafts'][2])
        self.assertEqual(state, before)

    def test_content_only_replacement_preserves_name_copy_rationale_and_other_drafts(self):
        state = self.initial(); target = state['drafts'][1]['draft_id']
        plan = edit_plan(target, 'all', theme_name='변경되면 안 되는 제목', copy='바꾸면 안 됨',
                         contents=suggestions(6), excluded_titles=['실제 작품 6'])
        result = self.turn(state, '이 작품은 제외하고 다른 작품을 추천해줘.', plan,
                           target_draft_id=target, edit_scope='contents')
        self.assertEqual(result['drafts'][1]['theme_name'], state['drafts'][1]['theme_name'])
        self.assertEqual(result['drafts'][1]['copy'], state['drafts'][1]['copy'])
        self.assertEqual(result['drafts'][1]['rationale'], state['drafts'][1]['rationale'])
        self.assertNotIn('실제 작품 6', [row['title'] for row in result['drafts'][1]['contents']])
        self.assertEqual(result['drafts'][0], state['drafts'][0])

    def test_selected_target_cannot_be_replaced_by_another_model_target(self):
        state = self.initial(); before = deepcopy(state)
        with self.assertRaisesRegex(ThemeGenerationError, '다른 초안'):
            self.turn(state, '2번 테마명만 수정해줘.', edit_plan(state['drafts'][0]['draft_id'], theme_name='잘못된 대상'))
        self.assertEqual(state, before)

    def test_narrow_edit_without_selected_target_cannot_replace_all_existing_drafts(self):
        state = self.initial(); before = deepcopy(state)
        with self.assertRaisesRegex(ThemeGenerationError, '수정 범위'):
            self.turn(state, '테마명만 더 감각적으로 바꿔줘.', create_plan(1))
        self.assertEqual(state, before)

    def test_empty_model_exclusions_do_not_lift_previous_bans_on_later_turn(self):
        state = self.initial(); target = state['drafts'][1]['draft_id']
        first = self.turn(state, '2번 테마에서 실제 작품 6은 제외하고 작품만 교체해줘.',
                          edit_plan(target, 'contents', contents=suggestions(6), excluded_titles=['실제 작품 6']))
        result = self.turn(first, '2번 테마 추천 작품만 다시 바꿔줘.',
                           edit_plan(target, 'contents', contents=suggestions(6), excluded_titles=[]))
        self.assertEqual(result['drafts'][1]['excluded_titles'], ['실제 작품 6'])
        self.assertNotIn('실제 작품 6', [row['title'] for row in result['drafts'][1]['contents']])
        self.assertEqual(result['drafts'][0], state['drafts'][0])

    def test_explicit_reinclude_lifts_only_the_named_ban(self):
        state = self.initial(); target = state['drafts'][0]['draft_id']
        state['drafts'][0]['excluded_titles'] = ['실제 작품 6', '실제 작품 7']
        result = self.turn(state, '1번 테마에 실제 작품 6은 다시 포함해줘.',
                           edit_plan(target, 'contents', contents=suggestions(6), excluded_titles=[],
                                     reincluded_titles=['실제 작품 6', '실제 작품 7']))
        self.assertEqual(result['drafts'][0]['excluded_titles'], ['실제 작품 7'])
        self.assertIn('실제 작품 6', [row['title'] for row in result['drafts'][0]['contents']])
        self.assertNotIn('실제 작품 7', [row['title'] for row in result['drafts'][0]['contents']])

    def test_explicit_extra_theme_appends_even_when_model_says_replace(self):
        state = self.initial(); before = deepcopy(state)
        plan = DirectorPlan.model_validate({'action':'create','message':'한 개 추가했어요.', 'creation_mode':'replace',
                                           'themes':[proposal('일상을 되찾는 복수의 선택')]})
        result = self.turn(state, '기존 테마는 유지하고 하나 더 만들어줘.', plan)
        self.assertEqual(len(result['drafts']), 4)
        self.assertEqual(result['drafts'][:3], before['drafts'])
        self.assertEqual(state, before)

    def test_style_scope_changes_genre_mood_and_keeps_contents_and_text(self):
        state = self.initial(); target = state['drafts'][0]['draft_id']
        result = self.turn(state, '장르와 분위기를 변경해줘.', edit_plan(target, 'all', genre='미스터리', mood='서늘함',
                           theme_name='바꾸면 안 됨', contents=suggestions(6)), target_draft_id=target, edit_scope='style')
        self.assertEqual(result['drafts'][0]['genre'], '미스터리')
        self.assertEqual(result['drafts'][0]['mood'], '서늘함')
        self.assertEqual(result['drafts'][0]['theme_name'], state['drafts'][0]['theme_name'])
        self.assertEqual(result['drafts'][0]['contents'], state['drafts'][0]['contents'])

    def test_undo_and_selected_theme_restore_are_deterministic_and_do_not_call_model(self):
        first = self.initial(); target = first['drafts'][1]['draft_id']
        second = self.turn(first, '2번 테마명만 바꿔줘.', edit_plan(target, theme_name='복수의 새로운 이름'))
        with patch('ai_director.generate_director_plan', side_effect=AssertionError('undo must be local')):
            undone = run_director_turn(second, '이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
        self.assertEqual(undone['drafts'], first['drafts'])
        self.assertEqual(undone['current_version_id'], first['current_version_id'])
        self.assertEqual(undone['last_usage']['api_call_count'], 0)
        restored = restore_director_version(second, first['current_version_id'], target_draft_id=target)
        self.assertEqual(restored['drafts'][1], first['drafts'][1])
        self.assertEqual(restored['drafts'][0], second['drafts'][0])
        self.assertEqual(len(restored['versions']), 3)
        with patch('ai_director.generate_director_plan', side_effect=AssertionError('undo must be local')):
            scoped = run_director_turn(second, '2번 테마 앞선 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
        self.assertEqual(scoped['drafts'][1], first['drafts'][1])
        self.assertEqual(scoped['drafts'][0], second['drafts'][0])

    def test_repeated_theme_undo_walks_back_without_redoing_and_preserves_other_themes(self):
        first = self.initial(); target = first['drafts'][1]['draft_id']
        second = self.turn(first, '2번 제목만 바꿔줘.', edit_plan(target, theme_name='복수의 두 번째 이름'))
        third = self.turn(second, '2번 제목만 바꿔줘.', edit_plan(target, theme_name='복수의 세 번째 이름'))
        other = first['drafts'][0]['draft_id']
        latest = self.turn(third, '1번 카피만 바꿔줘.', edit_plan(other, 'copy', copy='다른 테마의 새 카피'))
        before = deepcopy(latest)
        with patch('ai_director.generate_director_plan', side_effect=AssertionError('undo must be local')):
            once = run_director_turn(latest, '2번 이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
            twice = run_director_turn(once, '2번 이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
            with self.assertRaisesRegex(ThemeGenerationError, '더 이전'):
                run_director_turn(twice, '2번 이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
        self.assertEqual(once['drafts'][1], second['drafts'][1])
        self.assertEqual(twice['drafts'][1], first['drafts'][1])
        for restored in (once, twice):
            self.assertEqual(restored['drafts'][0], latest['drafts'][0])
            self.assertEqual(restored['drafts'][2], latest['drafts'][2])
            self.assertEqual(restored['last_usage']['api_call_count'], 0)
        self.assertEqual(latest, before)

    def test_theme_undo_after_restored_branch_does_not_revisit_abandoned_edits(self):
        first = self.initial(); target = first['drafts'][1]['draft_id']
        second = self.turn(first, '2번 제목만 바꿔줘.', edit_plan(target, theme_name='복수의 두 번째 이름'))
        abandoned = self.turn(second, '2번 제목만 바꿔줘.', edit_plan(target, theme_name='버린 분기의 이름'))
        restored = restore_director_version(abandoned, second['current_version_id'], target_draft_id=target)
        branch = self.turn(restored, '2번 제목만 바꿔줘.', edit_plan(target, theme_name='새 분기의 이름'))
        with patch('ai_director.generate_director_plan', side_effect=AssertionError('undo must be local')):
            once = run_director_turn(branch, '2번 이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
            twice = run_director_turn(once, '2번 이전 버전으로 되돌려줘', themes=self.themes, contents=self.catalog)
        self.assertEqual(once['drafts'][1], second['drafts'][1])
        self.assertEqual(twice['drafts'][1], first['drafts'][1])
        self.assertEqual(twice['drafts'][0], branch['drafts'][0])
        self.assertEqual(twice['drafts'][2], branch['drafts'][2])

    def test_next_turn_contains_prior_conversation_and_current_draft_state(self):
        state = self.initial(); target = state['drafts'][1]['draft_id']; sdk = client(edit_plan(target, theme_name='관계를 끝내는 복수'))
        result = run_director_turn(state, '2번 제목만 바꿔줘.', themes=self.themes, contents=self.catalog,
                                  api_key='', client=sdk, content_resolver=resolver)
        inputs = sdk.responses.parse.call_args.kwargs['input']
        self.assertEqual(inputs[1]['content'], state['messages'][0]['content'])
        payload = json.loads(inputs[-1]['content'])
        self.assertEqual(payload['current_drafts'][1]['draft_id'], target)
        self.assertEqual(len(result['messages']), 4)

    def test_existing_database_duplicate_is_reported_without_replacing_or_writing_it(self):
        self.themes = pd.DataFrame([{'theme_id':'T1', 'theme_name':'복수를 선택한 주인공 1'}])
        result = self.turn(new_director_session(), '복수 테마 1개 만들어줘.', create_plan(1))
        duplicate = result['drafts'][0]['duplicate_matches'][0]
        self.assertEqual(duplicate['theme_id'], 'T1')
        self.assertTrue(duplicate['exact'])
        self.assertEqual(self.themes.iloc[0]['theme_id'], 'T1')

    def test_unknown_content_is_not_filled_with_invented_titles_and_unknown_issue_keys_drop(self):
        raw = proposal(source_issue_keys=['invented'], contents=[{'title':'DB에 없는 가상 작품','content_type':'드라마','year':'2020','reason':'상상'}])
        p = DirectorPlan.model_validate({'action':'create','message':'초안','themes':[raw]})
        result = self.turn(new_director_session(), '테마 1개 만들어줘.', p)
        draft = result['drafts'][0]
        self.assertEqual(draft['contents'], [])
        self.assertIn('0편', draft['content_warning'])
        self.assertEqual(draft['source_issue_keys'], [])
        self.assertEqual(draft['source_issue'], '')

    def test_api_failure_is_sanitized_and_leaves_session_unchanged(self):
        state = self.initial(); before = deepcopy(state); sdk = Mock()
        sdk.responses.parse.side_effect = RuntimeError('https://secret.example/?key=DO_NOT_LOG')
        with self.assertRaises(ThemeGenerationError) as caught:
            run_director_turn(state, '다른 테마를 만들어줘.', themes=self.themes, contents=self.catalog, client=sdk)
        self.assertNotIn('DO_NOT_LOG', str(caught.exception))
        self.assertEqual(state, before)

    def test_missing_api_key_has_no_demo_or_fixed_response(self):
        with patch('llm_theme_generator.OpenAI') as sdk, self.assertRaises(ThemeGenerationError):
            generate_director_plan(
                request='테마를 만들어줘.',messages=[],drafts=[],themes=self.themes,contents=self.catalog,issue_records=[],api_key='',model='configured-model')
        sdk.assert_not_called()


if __name__ == '__main__':
    unittest.main()
