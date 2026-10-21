"""An in-place Streamlit update must work with the previous cached module API."""
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

import issue_quality


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def presentation_with_previous_quality_api():
    # Streamlit may keep imported modules while re-running an updated app.py.
    # This is the API available before read_issue_metrics was introduced.
    previous = ModuleType('issue_quality')
    for name in ('BOXOFFICE', 'NEWS', 'OTT', 'YOUTUBE', 'classify_source', '_metrics'):
        setattr(previous, name, getattr(issue_quality, name))
    if hasattr(previous, 'read_issue_metrics'):
        raise AssertionError('The previous cached module must not contain the new helper')
    spec = importlib.util.spec_from_file_location(
        '_live_updated_issue_presentation', ROOT / 'issue_presentation.py'
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'issue_quality': previous}):
        spec.loader.exec_module(module)
        yield module


class LiveUpdateTests(unittest.TestCase):
    def test_updated_video_card_works_with_previous_quality_module_cached(self):
        row = dict(
            source='YouTube 반응', source_group='YouTube 반응',
            source_url='https://www.youtube.com/watch?v=example',
            related_content='꿀알바', published_at='2020-10-16',
            metrics_json=json.dumps({
                'views_total': 500000, 'views_change': 379455,
                'observation_start': '2020-10-16', 'observation_end': '2020-10-17',
                'observation_precision': 'date',
            }),
        )
        with presentation_with_previous_quality_api() as presentation:
            result = presentation.present_issue(row)
        self.assertIn('37.9만', result['headline'])
        self.assertIn('증가', result['headline'])
        self.assertTrue(any('379,455' in str(value) for _, value in result['details']))

    def test_updated_boxoffice_card_keeps_measured_audience_with_previous_api(self):
        row = dict(
            source='KOBIS 박스오피스', source_group='극장·박스오피스',
            source_url='https://www.kobis.or.kr/chart', reference_date='2020-10-20',
            related_content='오디세이',
            metrics_json=json.dumps({'rank': 1, 'daily_audience': 22474}),
        )
        with presentation_with_previous_quality_api() as presentation:
            result = presentation.present_issue(row)
        self.assertIn('1위', result['headline'])
        self.assertIn('22,474', result['period_label'])
        self.assertIn('10.20', result['period_label'])

    def test_updated_weekly_card_keeps_category_and_period_with_previous_api(self):
        row = dict(
            source='Netflix Top 10', source_group='OTT 랭킹·신작',
            source_type='ott_ranking', source_url='https://www.netflix.com/tudum/top10',
            reference_date='2020-10-18', related_content='The Eyes',
            metrics_json=json.dumps({'rank': 2, 'category': 'Films', 'country': 'KR'}),
        )
        with presentation_with_previous_quality_api() as presentation:
            result = presentation.present_issue(row)
        self.assertIn('영화', result['headline'])
        self.assertIn('2위', result['headline'])
        self.assertIn('10.12~10.18', result['period_label'])


if __name__ == '__main__':
    unittest.main()
