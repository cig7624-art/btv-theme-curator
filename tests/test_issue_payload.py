import unittest

from llm_theme_generator import _issue_payload


class IssuePayloadTests(unittest.TestCase):
    def test_provenance_replaces_unvalidated_popularity_score(self):
        payload = _issue_payload([{
            'issue_title': '닥터X 예고 공개', 'related_content': '닥터X',
            'issue_score': 99, 'strength_label': '초기 반응 (증가 미확인)',
            'evidence_status': '일부 확인', 'reference_label': '관측 2026-10-21',
            'metric_summary': '누적 조회 100,000회 · 증가 미측정',
            'selection_reason': '작품명과 원문이 확인된 예고 영상',
            'source_url': 'https://www.youtube.com/watch?v=sample',
        }])[0]
        self.assertNotIn('issue_score', payload)
        self.assertEqual(payload['metrics'], '누적 조회 100,000회 · 증가 미측정')
        self.assertEqual(payload['reference_period'], '관측 2026-10-21')
        self.assertEqual(payload['strength_label'], '초기 반응 (증가 미확인)')
        self.assertEqual(payload['source_url'], 'https://www.youtube.com/watch?v=sample')


if __name__ == '__main__':
    unittest.main()
