"""Popular-list and news copy must describe the actual selection evidence."""
import json
import unittest

import pandas as pd

from issue_presentation import present_issue
from issue_selection_copy import SELECTION_STEPS, SOURCE_DETAIL_LOGIC


class SignalCopyTests(unittest.TestCase):
    def popular_row(self, **metrics):
        evidence = {
            'views_total': 12000,
            'reaction_basis': 'initial',
            'popular_snapshot_at': '2026-10-21T01:30:00+00:00',
            'popular_region': 'KR',
            'popular_category_id': '1',
            'popular_list_position': 8,
            'popular_snapshot_complete': True,
        }
        evidence.update(metrics)
        return dict(
            source='YouTube 반응', source_group='YouTube 반응',
            source_url='https://www.youtube.com/watch?v=example1234',
            related_content='실낙원', published_at='2025-01-01',
            popular_valid=True, metrics_json=json.dumps(evidence),
        )

    def test_popular_only_card_explains_membership_without_inventing_growth_or_platform_rank(self):
        view = present_issue(self.popular_row())
        self.assertIn('인기', view['headline'])
        self.assertIn('영화·애니메이션', view['reason'])
        self.assertNotIn('증가', view['headline'])
        self.assertNotIn('8위', str(view))
        self.assertIn('10.21', view['period_label'])
        self.assertIn('12,000', str(view['details']))
        self.assertIn('2026-10-21 10:30', str(view['details']))
        self.assertNotIn('검증 완료', str(view))

    def test_popular_card_keeps_measured_growth_as_additional_evidence(self):
        row = self.popular_row(
            views_total=60000, views_change=48000,
            observation_start='2026-10-20', observation_end='2026-10-21',
        )
        row['comparison_valid'] = True
        view = present_issue(row)
        self.assertIn('인기', view['headline'])
        self.assertIn('48,000', str(view['details']))
        self.assertNotIn('늘어난', view['reason'])

    def test_rejected_popular_snapshot_cannot_be_shown_as_current(self):
        row = self.popular_row()
        row['popular_valid'] = False
        view = present_issue(row)
        self.assertNotIn('인기', view['headline'])
        self.assertNotIn('인기', view['reason'])
        self.assertIn('누적', view['headline'])

    def test_no_assessment_fallback_requires_actual_current_list_evidence(self):
        row = self.popular_row()
        del row['popular_valid']
        # A sticky discovery query or an old API list is not evidence of today's list.
        row['query'] = '한국 인기 영상'
        evidence = json.loads(row['metrics_json'])
        evidence['popular_snapshot_at'] = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=2)).isoformat()
        row['metrics_json'] = json.dumps(evidence)
        view = present_issue(row)
        self.assertNotIn('인기', view['headline'])
        row['metrics_json'] = json.dumps({'views_total': 12000, 'popular_snapshot_at': pd.Timestamp.now(tz='UTC').isoformat()})
        self.assertNotIn('인기', present_issue(row)['headline'])

    def test_news_card_describes_reported_event_and_source_without_claiming_body_verification(self):
        row = dict(
            source='온라인 화제·뉴스', source_url='https://news.google.com/rss/articles/item',
            related_content='실낙원', issue_title="영화 '실낙원' 21일 개봉",
            description='신작 영화 실낙원이 개봉한다.',
            publisher='영화신문', published_at='2026-10-21',
            metrics_json=json.dumps({'news_provenance': 'google_news_rss'}),
        )
        view = present_issue(row)
        self.assertIn('개봉', view['reason'])
        self.assertIn('영화신문', str(view['details']))
        self.assertIn('10.21', view['period_label'])
        self.assertIn('제목·요약', view['verification_note'])
        self.assertNotIn('인기', view['reason'])
        self.assertNotIn('본문 확인', view['verification_note'])

    def test_methodology_includes_independent_popular_and_news_lanes(self):
        youtube = SOURCE_DETAIL_LOGIC['youtube']
        self.assertIn('이전 조회수 기록이 없어도', ' '.join(youtube['filters']))
        self.assertIn('영화·애니메이션', youtube['integration'])
        self.assertIn('50개', youtube['limitations'])
        news = SOURCE_DETAIL_LOGIC['online_news']
        self.assertIn('거쳐 연결', ' '.join(news['filters']))
        self.assertIn('새로운 소식', news['ranking'])
        self.assertIn('새로운 소식', str(SELECTION_STEPS))
        for jargon in ['종합 100점', '원문 미대조', '관측기간 증가 확인']:
            self.assertNotIn(jargon, str(SOURCE_DETAIL_LOGIC) + str(SELECTION_STEPS))


if __name__ == '__main__':
    unittest.main()
