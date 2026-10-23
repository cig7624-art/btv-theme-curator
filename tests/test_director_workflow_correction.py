"""Weekly generation stays put; the library's sentence interface becomes chat."""
from pathlib import Path
import unittest
from unittest.mock import patch
import pandas as pd
from test_theme_actions import app_fixture


class DirectorWorkflowCorrectionTests(unittest.TestCase):
    def test_library_retains_quick_lookup_but_moves_sentence_ai_to_director(self):
        with patch('ai_director_view.run_director_turn') as run, app_fixture() as state:
            app=state['app']; before={p.name:p.read_bytes() for p in Path('.').glob('*.csv')}
            app.button(key='open_theme_library').click().run()
            self.assertFalse(app.exception)
            self.assertFalse(any(widget.key=='theme_ai_search_input' for widget in app.text_input))
            self.assertFalse(any('AI로 유사 테마 찾기' in button.label for button in app.button))
            app.session_state['theme_ai_search_intent']={'genres':['공포']}
            app.text_input(key='theme_quick_search_input').set_value('공범').run()
            self.assertIn('공범들의 마지막 선택',' '.join(item.value for item in app.markdown))
            app.button(key='library_open_ai_director').click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state['page'],'home')
            self.assertEqual(app.session_state['home_sections'],'AI 디렉터')
            self.assertFalse(app.button(key='generate_weekly_themes').disabled)
            run.assert_not_called(); state['generate_weekly_themes'].assert_not_called()
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path('.').glob('*.csv')})

    def test_no_collected_issues_disable_weekly_creation_but_not_creative_conversation(self):
        reply={'schema_version':1,'messages':[{'role':'user','content':'뭐 만들어볼까?'},{'role':'assistant','content':'잔잔한 관계극이나 강렬한 반전극은 어때요?'}],
               'drafts':[],'versions':[],'current_version_id':None,'version_sequence':0,'last_usage':{'api_call_count':1}}
        with patch('ai_director_view.run_director_turn',return_value=reply) as run, app_fixture() as state:
            path=Path('issue_feed.csv');pd.read_csv(path,sep='|').iloc[:0].to_csv(path,sep='|',index=False)
            app=state['app'].run()
            self.assertFalse(app.exception)
            self.assertTrue(app.button(key='generate_weekly_themes').disabled)
            self.assertFalse(app.button(key='director_quick_0').disabled)
            app.chat_input(key='director_chat_input').set_value('뭐 만들어볼까?').run()
            self.assertFalse(app.exception)
            self.assertEqual(run.call_args.kwargs['issue_records'],[])
            state['generate_weekly_themes'].assert_not_called()
            state['persist_recommendations_locally'].assert_not_called()


if __name__=='__main__':unittest.main()
