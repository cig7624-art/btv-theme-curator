"""The production verifier may make one mutation and must preserve prior runs."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.verify_deployment import VerificationIssue
from scripts.verify_weekly_live import (
    FILES, content_coverage, exercise, find_new_run, prior_rows_preserved, valid_contract,
)


def theme(rank=1):
    return {"run_id": "new", "theme_id": "T" + str(rank), "rank": str(rank),
            "theme_name": "가족이라는 미스터리" if rank == 1 else "비 오는 날 추억의 영화",
            "content_selection_rule": "가족의 비밀을 밝히는 작품", "content_exclusion_rule": "가족이 주변 소재인 경우 제외",
            "content_reference_titles": json.dumps(["나이브스 아웃", "기생충", "그을린 사랑"])}


class WeeklyLiveVerificationTests(unittest.TestCase):
    def test_new_run_must_match_actual_display_order_and_not_a_director_adoption(self):
        before = [{"run_id": "old", "rank": "1", "theme_name": "기존 테마"}]
        rows = [theme(2), theme(1)]
        names = [theme(1)["theme_name"], theme(2)["theme_name"]]
        run, ordered = find_new_run(before, before + rows, names)
        self.assertEqual(run, "new")
        self.assertEqual([row["rank"] for row in ordered], ["1", "2"])
        self.assertEqual(find_new_run(before, before + rows, list(reversed(names))), (None, []))
        self.assertEqual(find_new_run(before, [dict(row, run_id="AI-DIRECTOR-x") for row in rows], names), (None, []))

    def test_contract_rejects_metrics_and_missing_criteria_even_with_correct_count(self):
        rows = [theme(1), theme(2)]
        self.assertTrue(valid_contract(rows, 2))
        for field, value in [("theme_name", "OTT 공개 후 조회수가 늘어난 콘텐츠"),
                             ("content_selection_rule", ""), ("content_exclusion_rule", ""),
                             ("content_reference_titles", '["기생충", "기생충"]')]:
            changed = deepcopy(rows)
            changed[0][field] = value
            self.assertFalse(valid_contract(changed, 2))

    def test_preservation_compares_old_cells_and_allows_only_named_mutable_fields(self):
        old = [{"theme_id": "T1", "copy": "원래 카피", "last_recommended_date": "2026-01-01"}]
        new = [dict(old[0], last_recommended_date="2026-10-23", added_column="새 기준")]
        self.assertTrue(prior_rows_preserved(old, new, keys=("theme_id",), mutable=("last_recommended_date",)))
        self.assertFalse(prior_rows_preserved(old, new, keys=("theme_id",)))
        self.assertFalse(prior_rows_preserved(old, [dict(new[0], copy="실수로 바꾼 카피")], keys=("theme_id",), mutable=("last_recommended_date",)))

    def test_coverage_requires_every_selected_theme_and_never_uses_other_run_posters(self):
        rows = [{"run_id": "new", "theme_key": "T1", "title": "기생충", "poster_url": "https://example.test/a.jpg"},
                {"run_id": "other", "theme_key": "T2", "title": "코코", "poster_url": "https://example.test/b.jpg"}]
        result = content_coverage(rows, "new", [theme(1), theme(2)])
        self.assertEqual(result, {"rows": 1, "themes_with_contents": 1, "themes_with_posters": 1})

    def test_uncertain_click_result_is_never_retried_or_loaded_again(self):
        page, frame, button = Mock(), Mock(), Mock()
        frame.get_by_role.side_effect = lambda role, **kwargs: button if role == "button" else Mock()
        button.is_enabled.return_value = True
        button.click.side_effect = RuntimeError("Browser timeout after server acceptance")
        args = SimpleNamespace(deployment_timeout=1, generation_timeout=1, expected_count=10)
        report = {"tests": {}, "generation_click_attempted": False}
        with patch("scripts.verify_weekly_live._load_version", return_value=frame) as load, \
                patch("scripts.verify_weekly_live._names", return_value=["원래 테마"]):
            with self.assertRaises(RuntimeError):
                exercise(page, args, report, read_data=lambda: dict.fromkeys(FILES, []))
        button.click.assert_called_once()
        load.assert_called_once()
        self.assertTrue(report["generation_click_attempted"])


if __name__ == "__main__":
    unittest.main()
