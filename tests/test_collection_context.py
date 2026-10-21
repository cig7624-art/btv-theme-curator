"""Latest API outcomes must not leave preserved rows claiming current popularity."""
import json
import unittest

import pandas as pd

from issue_collection_context import apply_collection_context


SNAPSHOT = '2026-10-21T01:00:00+00:00'
LATER = '2026-10-21T02:00:00+00:00'
OLDER = '2026-10-21T00:00:00+00:00'


def row(category='1', **changes):
    result = dict(
        source='YouTube 반응', source_type='youtube_reaction',
        source_url='https://www.youtube.com/watch?v=example1234',
        issue_title='실낙원 공개 영상', related_content='실낙원',
        metrics_json=json.dumps(dict(
            popular_snapshot_at=SNAPSHOT, popular_region='KR',
            popular_list_position=3, popular_category_id=category,
            popular_snapshot_complete=True, views_total=123456,
            observation_end='2026-10-21T01:00:00+00:00',
        )),
    )
    result.update(changes)
    return result


def status(**diagnostics):
    return {'sources': {'youtube': diagnostics}}


class CollectionContextTests(unittest.TestCase):
    def metrics(self, frame, position=0):
        value = frame.iloc[position]['metrics_json']
        return value if isinstance(value, dict) else json.loads(value)

    def test_failed_current_list_invalidates_preserved_membership_only(self):
        original = pd.DataFrame([row()], index=[42])
        before = original.copy(deep=True)
        result = apply_collection_context(original, status(
            state='partial', popular_failed_lists=['1'], popular_checked_at=LATER,
        ))
        self.assertFalse(self.metrics(result)['popular_snapshot_complete'])
        self.assertEqual(self.metrics(result)['views_total'], 123456)
        self.assertEqual(self.metrics(result)['observation_end'], SNAPSHOT)
        self.assertEqual(result.index.tolist(), [42])
        pd.testing.assert_frame_equal(original, before)
        pd.testing.assert_series_equal(result.drop(columns='metrics_json').iloc[0], original.drop(columns='metrics_json').iloc[0])

    def test_newer_empty_or_nonempty_success_invalidates_older_snapshot(self):
        for state in ['empty', 'ok']:
            with self.subTest(state=state):
                result = apply_collection_context(pd.DataFrame([row()]), status(
                    state=state, popular_snapshots={'1': LATER},
                    popular_failed_lists=[], popular_checked_at=LATER,
                ))
                self.assertFalse(self.metrics(result)['popular_snapshot_complete'])

    def test_matching_successful_snapshot_preserves_genuine_membership(self):
        original = pd.DataFrame([row()])
        result = apply_collection_context(original, status(
            popular_snapshots={'1': SNAPSHOT}, popular_checked_at=LATER,
        ))
        pd.testing.assert_frame_equal(result, original)

    def test_failure_and_success_are_scoped_to_the_actual_list(self):
        original = pd.DataFrame([row('1'), row('24'), row('all')])
        result = apply_collection_context(original, status(
            popular_snapshots={'24': SNAPSHOT}, popular_failed_lists=['1'],
            popular_checked_at=LATER,
        ))
        self.assertFalse(self.metrics(result, 0)['popular_snapshot_complete'])
        self.assertTrue(self.metrics(result, 1)['popular_snapshot_complete'])
        self.assertTrue(self.metrics(result, 2)['popular_snapshot_complete'])

    def test_older_status_cannot_revoke_newer_feed_evidence(self):
        original = pd.DataFrame([row()])
        for diagnostics in [
            dict(popular_snapshots={'1': OLDER}),
            dict(popular_failed_lists=['1'], popular_checked_at=OLDER),
        ]:
            with self.subTest(diagnostics=diagnostics):
                pd.testing.assert_frame_equal(apply_collection_context(original, status(**diagnostics)), original)

    def test_unknown_or_legacy_status_cannot_fabricate_or_invalidate_membership(self):
        original = pd.DataFrame([row()])
        for current in [None, {}, {'sources': {}}, status(state='failed'), status(popular_failed_lists=['1'])]:
            with self.subTest(status=current):
                result = apply_collection_context(original, current)
                pd.testing.assert_frame_equal(result, original)
                self.assertIsNot(result, original)

    def test_mapping_metrics_are_copied_and_duplicate_indices_preserved(self):
        first = row()
        first['metrics_json'] = json.loads(first['metrics_json'])
        original = pd.DataFrame([first, row('24')], index=[0, 0])
        result = apply_collection_context(original, status(popular_failed_lists=['1'], popular_checked_at=LATER))
        self.assertFalse(self.metrics(result)['popular_snapshot_complete'])
        self.assertTrue(self.metrics(original)['popular_snapshot_complete'])
        self.assertTrue(self.metrics(result, 1)['popular_snapshot_complete'])
        self.assertEqual(result.index.tolist(), [0, 0])

    def test_malformed_metrics_status_and_non_youtube_rows_are_unchanged(self):
        original = pd.DataFrame([
            row(metrics_json='{bad json'), row(metrics_json='[]'),
            row(source='온라인 화제·뉴스', source_type='news', source_url='https://news.example.org/article'),
        ])
        result = apply_collection_context(original, status(popular_snapshots={'1': LATER}, popular_failed_lists=['1'], popular_checked_at=LATER))
        pd.testing.assert_frame_equal(result, original)
        invalid = status(popular_snapshots=['1'], popular_failed_lists='1', popular_checked_at='not-a-date')
        pd.testing.assert_frame_equal(apply_collection_context(original, invalid), original)


if __name__ == '__main__':
    unittest.main()
