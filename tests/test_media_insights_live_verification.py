"""The paid live check must not resubmit an uncertain request or leak content."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.verify_deployment import VerificationIssue
from scripts.verify_media_insights_live import (
    _load_media, _public_url, answer_distinguishes_unknown, saved_note_valid, submit_once, media_alert_code, prepare_note,
    main, verify,
)


def snapshot():
    note = {'source_hash': 'hash-one', 'generated_at': '2026-10-23T12:00:00+00:00',
            'model': 'runtime-model', 'context_hashes': {'one': 'hash-one'},
            'facts': [{'text': '확인한 사실', 'evidence_ids': ['one-p001']}], 'comparison': []}
    return {'items': [{'id': 'one', 'url': 'https://official.example.com/update',
                       'content_hash': 'hash-one', 'evidence': [{'id': 'one-p001', 'text': '원문 근거'}],
                       'analysis': note}]}


class MediaInsightsLiveVerificationTests(unittest.TestCase):
    def test_retired_cli_cannot_be_reenabled_by_an_old_expected_version(self):
        with (patch('scripts.verify_media_insights_live._legacy_research_enabled', return_value=False),
              patch('scripts.verify_media_insights_live.verify') as run,
              patch('builtins.print')):
            self.assertEqual(main(['--expected-version', '1.5.1']), 2)
        run.assert_not_called()

    def test_retired_programmatic_check_blocks_before_any_browser_or_question(self):
        args = SimpleNamespace(expected_version='1.6.0', reuse_analyses=True, analysis_limit=2)
        with (patch('scripts.verify_media_insights_live._legacy_research_enabled', return_value=False),
              patch('scripts.verify_media_insights_live.exercise') as exercise):
            report = verify(args)
        exercise.assert_not_called()
        self.assertTrue(report['blocked'])
        self.assertEqual(report['counts']['question_clicks'], 0)
        self.assertEqual(report['failure_code'], 'retired_research_verification')

    def test_saved_note_check_never_submits_another_analysis(self):
        frame = Mock()
        frame.locator.return_value.count.return_value = 4
        report = {'checks': {}, 'counts': {'analysis_clicks': 0}}
        prepare_note(frame, report, 1, reuse=True)
        self.assertEqual(report['counts']['analysis_clicks'], 0)
        frame.locator.return_value.click.assert_not_called()
        self.assertTrue(report['checks']['analysis_1_reused_without_model_call'])

    def test_alert_diagnostics_export_only_fixed_categories(self):
        self.assertEqual(media_alert_code("분석의 수치를 원문에서 확인하지 못했습니다."), "media_numbers_unverified")
        self.assertEqual(media_alert_code("PRIVATE provider body sk-do-not-log"), "media_action_failed")

    def test_uncertain_click_is_counted_before_error_and_never_repeated(self):
        button = Mock()
        button.is_enabled.return_value = True
        button.click.side_effect = RuntimeError('unknown result after submission')
        report = {'checks': {}, 'counts': {'analysis_clicks': 0}}
        with self.assertRaises(RuntimeError):
            submit_once(button, report, 'analysis')
        with self.assertRaises(VerificationIssue):
            submit_once(button, report, 'analysis')
        self.assertEqual(button.click.call_count, 1)
        self.assertEqual(report['counts']['analysis_clicks'], 1)
        self.assertTrue(report['checks']['analysis_click_attempted'])

    def test_three_distinct_analysis_actions_have_separate_guards_and_a_total_limit(self):
        button = Mock()
        button.is_enabled.return_value = True
        report = {"checks": {}, "counts": {"analysis_clicks": 0}}
        for index in range(1, 4):
            submit_once(button, report, "analysis", action_index=index)
            self.assertTrue(report["checks"][f"analysis_{index}_click_attempted"])
            self.assertEqual(report["counts"][f"analysis_{index}_clicks"], 1)
        for index in [1, 2, 3, 4]:
            with self.assertRaises(VerificationIssue):
                submit_once(button, report, "analysis", action_index=index)
        self.assertEqual(button.click.call_count, 3)
        self.assertEqual(report["counts"]["analysis_clicks"], 3)

    def test_disabled_action_is_not_counted_as_paid_request(self):
        button = Mock()
        button.is_enabled.return_value = False
        report = {'checks': {}, 'counts': {'question_clicks': 0}}
        with self.assertRaises(VerificationIssue):
            submit_once(button, report, 'question')
        button.click.assert_not_called()
        self.assertEqual(report['counts']['question_clicks'], 0)

    def test_version_gate_precedes_tab_selection_and_any_paid_action(self):
        page = Mock()
        with patch('scripts.verify_media_insights_live._load_version',
                   side_effect=VerificationIssue('expected_release_not_ready', retry=True)):
            with self.assertRaises(VerificationIssue):
                _load_media(page, SimpleNamespace(), {'checks': {}}, 'desktop')
        page.get_by_role.assert_not_called()
        page.locator.assert_not_called()

    def test_saved_note_requires_current_hashes_and_unambiguous_official_citations(self):
        saved = snapshot()
        url = saved['items'][0]['url']
        self.assertTrue(saved_note_valid(saved, url))
        for mutate in [
            lambda data: data['items'][0]['analysis'].update(source_hash='stale'),
            lambda data: data['items'][0]['analysis']['facts'][0].update(evidence_ids=['invented']),
            lambda data: data['items'][0]['analysis'].update(context_hashes={'missing': 'stale'}),
            lambda data: data['items'].append({'id': 'two', 'evidence': [{'id': 'one-p001'}]}),
        ]:
            bad = deepcopy(saved)
            mutate(bad)
            self.assertFalse(saved_note_valid(bad, url))

    def test_unknown_internal_revenue_must_be_distinguished_from_source_facts(self):
        self.assertTrue(answer_distinguishes_unknown('공식 발표는 상품 출시를 알렸습니다. B tv 예상 매출은 이 자료에서 확인할 수 없습니다.'))
        self.assertTrue(answer_distinguishes_unknown('B tv 매출은 발표에 제시되지 않았습니다.'))
        self.assertFalse(answer_distinguishes_unknown('B tv 예상 매출은 $100 million입니다.'))
        self.assertFalse(answer_distinguishes_unknown('이 발표에 한국이 포함되는지는 알 수 없습니다.'))

    def test_public_endpoints_cannot_embed_credentials_or_use_plain_http(self):
        self.assertTrue(_public_url('https://official.example.com/update'))
        for url in ['http://official.example.com', 'https://user:secret@example.com', 'https://localhost', 'file:///tmp/data']:
            self.assertFalse(_public_url(url))


if __name__ == '__main__':
    unittest.main()
