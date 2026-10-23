"""Director saves belong to the library, while weekly runs keep their origin."""
from copy import deepcopy
from pathlib import Path
import unittest

import pandas as pd

from test_theme_actions import app_fixture, recommendation


WEEKLY_NAME = '공범들의 마지막 선택'
LEGACY_NAME = '예전 디렉터가 만든 심야의 선택'
DIRECTOR_NAME = '새 디렉터가 만든 유리 도시'


def seed_library_rows():
    base = recommendation()['theme']
    rows = [
        dict(base, source_status='AI_GENERATED'),
        dict(base, theme_id='DIRECTOR-OLD', theme_name=LEGACY_NAME,
             source_status='AI_GENERATED', director_draft_id='legacy-draft'),
        dict(base, theme_id='DIRECTOR-NEW', theme_name=DIRECTOR_NAME,
             source_status='AI_DIRECTOR'),
    ]
    pd.DataFrame(rows).fillna('').to_csv('theme_db.csv', sep='|', index=False)
    return rows


def csv_bytes():
    return {path.name: path.read_bytes() for path in Path('.').glob('*.csv')}


class DirectorLibraryRoutingTests(unittest.TestCase):
    def assert_no_generation(self, state):
        for name in ('generate_weekly_themes', 'persist_recommendations_locally',
                     'generate_content_candidates_for_themes', 'persist_files_to_github'):
            state[name].assert_not_called()

    def test_existing_dropdown_filters_both_new_and_previously_saved_director_themes(self):
        with app_fixture() as state:
            seed_library_rows(); before = csv_bytes()
            app = state['app'].run()
            app.button(key='open_theme_library').click().run()
            self.assertFalse(app.exception)
            options = app.selectbox(key='theme_library_source_filter').options
            self.assertIn('AI 디렉터', options)
            self.assertIn('AI 신규 생성', options)
            app.selectbox(key='theme_library_source_filter').set_value('AI 디렉터').run()
            self.assertFalse(app.exception)
            markup = ' '.join(item.value for item in app.markdown)
            self.assertIn(LEGACY_NAME, markup)
            self.assertIn(DIRECTOR_NAME, markup)
            self.assertNotIn(WEEKLY_NAME, markup)
            app.selectbox(key='theme_library_source_filter').set_value('AI 신규 생성').run()
            self.assertFalse(app.exception)
            markup = ' '.join(item.value for item in app.markdown)
            self.assertIn(WEEKLY_NAME, markup)
            self.assertNotIn(LEGACY_NAME, markup)
            self.assertNotIn(DIRECTOR_NAME, markup)
            self.assertEqual(csv_bytes(), before)
            self.assert_no_generation(state)

    def test_quick_search_combines_with_director_source_instead_of_resetting_it(self):
        with app_fixture() as state:
            seed_library_rows(); before = csv_bytes()
            app = state['app'].run()
            app.button(key='open_theme_library').click().run()
            app.selectbox(key='theme_library_source_filter').set_value('AI 디렉터').run()
            app.text_input(key='theme_quick_search_input').set_value('유리').run()
            self.assertFalse(app.exception)
            markup = ' '.join(item.value for item in app.markdown)
            self.assertIn(DIRECTOR_NAME, markup)
            self.assertNotIn(LEGACY_NAME, markup)
            self.assertNotIn(WEEKLY_NAME, markup)
            self.assertEqual(app.selectbox(key='theme_library_source_filter').value, 'AI 디렉터')
            self.assertEqual(csv_bytes(), before)
            self.assert_no_generation(state)

    def test_weekly_view_ignores_director_runs_and_keeps_the_older_issue_recommendation(self):
        with app_fixture() as state:
            original = deepcopy(state['runs'][0])
            legacy_director = deepcopy(original)
            legacy_director['run_id'] = 'AI-DIRECTOR-legacy-adoption'
            legacy_director['recommendations'][0]['theme']['theme_name'] = LEGACY_NAME
            new_director = deepcopy(original)
            new_director['run_id'] = 'new-director-origin-run'
            new_director['recommendations'][0]['recommendation_source'] = 'AI_DIRECTOR'
            new_director['recommendations'][0]['theme']['theme_name'] = DIRECTOR_NAME
            state['runs'][:] = [new_director, legacy_director, original]
            before_runs, before_files = deepcopy(state['runs']), csv_bytes()
            app = state['app'].run()
            self.assertFalse(app.exception)
            markup = ' '.join(item.value for item in app.markdown)
            self.assertIn(WEEKLY_NAME, markup)
            self.assertNotIn(LEGACY_NAME, markup)
            self.assertNotIn(DIRECTOR_NAME, markup)
            self.assertEqual(state['runs'], before_runs)
            self.assertEqual(csv_bytes(), before_files)
            self.assert_no_generation(state)

    def test_weekly_run_may_reuse_a_director_origin_theme_without_becoming_a_director_run(self):
        with app_fixture() as state:
            rows = seed_library_rows()
            weekly = deepcopy(state['runs'][0])
            weekly['run_id'] = 'weekly-reuses-library-theme'
            weekly['recommendations'][0]['recommendation_source'] = 'EXISTING_DB'
            weekly['recommendations'][0]['theme'] = rows[2]
            state['runs'][:] = [weekly]
            app = state['app'].run()
            self.assertFalse(app.exception)
            self.assertIn(DIRECTOR_NAME, ' '.join(item.value for item in app.markdown))
            self.assertFalse(app.button(key='load_weekly_contents').disabled)
            self.assert_no_generation(state)


if __name__ == '__main__':
    unittest.main()
