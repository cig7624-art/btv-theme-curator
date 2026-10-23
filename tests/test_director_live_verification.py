"""A real draft validation must reject hidden mutations and invented content."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.verify_director_live import (
    content_edit_is_scoped, title_edit_is_scoped, valid_drafts, valid_saved_matches, verified_titles,
)


def drafts():
    return [{'id': f'D{i}', 'name': f'테마 {i}', 'copy': '서브카피', 'rationale': '기획 의도',
             'style': '드라마 · 긴장', 'contents': [
                 {'title': f'실제작품 {j}', 'meta': '드라마 · 2020', 'reason': '관계의 반전을 비교하기 위해',
                  'poster': f'url("https://images.example.test/{j}.jpg")'} for j in range(5)]}
            for i in range(3)]


class DirectorLiveVerificationTests(unittest.TestCase):
    def test_numbered_search_cards_must_reference_actual_saved_themes(self):
        self.assertTrue(valid_saved_matches(['1. 여름밤의 공포', '2. 반전의 선택'], {'여름밤의공포', '반전의선택'}))
        self.assertFalse(valid_saved_matches(['1. 모델이 지어낸 검색 결과'], {'여름밤의공포'}))
        self.assertFalse(valid_saved_matches([], {'여름밤의공포'}))

    def test_three_drafts_require_verified_titles_reasons_and_real_poster_links(self):
        rows = drafts(); known = {f'실제작품{j}' for j in range(5)}
        self.assertTrue(valid_drafts(rows, known))
        for field, value in [('title', '존재를 확인하지 않은 작품'), ('reason', '')]:
            bad = deepcopy(rows); bad[1]['contents'][0][field] = value
            self.assertFalse(valid_drafts(bad, known))
        bad = deepcopy(rows)
        for row in bad[1]['contents']:
            row['poster'] = ''
        self.assertFalse(valid_drafts(bad, known))
        self.assertFalse(valid_drafts(rows[:2], known))

    def test_title_only_rule_rejects_a_changed_other_theme_or_any_changed_content(self):
        before = drafts(); after = deepcopy(before); after[1]['name'] = '새로운 제목'
        self.assertTrue(title_edit_is_scoped(before, after))
        bad = deepcopy(after); bad[0]['copy'] = '잘못 변경된 카피'
        self.assertFalse(title_edit_is_scoped(before, bad))
        bad = deepcopy(after); bad[1]['contents'][0]['reason'] = '바뀐 이유'
        self.assertFalse(title_edit_is_scoped(before, bad))
        self.assertFalse(title_edit_is_scoped(before, before))

    def test_content_rule_rejects_retained_exclusion_or_changed_metadata(self):
        before = drafts(); after = deepcopy(before)
        after[1]['contents'][0] = dict(after[1]['contents'][0], title='다른 실제작품')
        self.assertTrue(content_edit_is_scoped(before, after, '실제작품 0'))
        self.assertFalse(content_edit_is_scoped(before, before, '실제작품 0'))
        bad = deepcopy(after); bad[1]['name'] = '바뀌면 안 되는 테마명'
        self.assertFalse(content_edit_is_scoped(before, bad, '실제작품 0'))
        bad = deepcopy(after); bad[2]['contents'] = []
        self.assertFalse(content_edit_is_scoped(before, bad, '실제작품 0'))

    def test_known_title_cache_excludes_unverified_external_suggestions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'content_db.csv').write_text('content_id|title\nC1|로컬 작품\n', encoding='utf-8')
            (root / 'theme_content_candidates.csv').write_text(
                'title|kinolights_status\n확인한 외부 작품|verified\n미확인 제안|search_link_only\n', encoding='utf-8')
            self.assertEqual(verified_titles(root), {'로컬작품', '확인한외부작품'})


if __name__ == '__main__':
    unittest.main()
