"""The production verifier may make one mutation and must preserve prior runs."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts.verify_deployment import VerificationIssue
from scripts.verify_weekly_live import (
    FILES, content_coverage, exact_content_coverage, exercise, find_new_run,
    latest_weekly_run, prior_rows_preserved, rows_unchanged, valid_contract,
)


def theme(rank=1):
    return {"run_id": "new", "theme_id": "T" + str(rank), "rank": str(rank),
            "theme_name": "가족이라는 미스터리" if rank == 1 else "비 오는 날 추억의 영화",
            "content_selection_rule": "가족의 비밀을 밝히는 작품", "content_exclusion_rule": "가족이 주변 소재인 경우 제외",
            "content_reference_titles": json.dumps(["나이브스 아웃", "기생충", "그을린 사랑"])}


TITLES = ["기생충", "마더", "올드보이", "장화, 홍련", "유전", "그을린 사랑",
          "나이브스 아웃", "오펀: 천사의 비밀", "식스 센스", "시크릿"]


def saved_contents(count=10, *, fetched_at="before"):
    return [{"run_id": "new", "theme_key": "T" + str(rank), "rank": str(index),
             "title": title, "poster_url": "https://example.test/poster.jpg", "fetched_at": fetched_at}
            for rank in range(1, 11) for index, title in enumerate(TITLES[:count], 1)]


def refresh_data():
    themes = [dict(theme(rank), recommended_at="2026-10-26T12:00:00+09:00") for rank in range(1, 11)]
    old_content = {"run_id": "old", "theme_key": "old-theme", "rank": "1", "title": "보존할 작품"}
    before = {
        FILES[0]: [{"theme_id": row["theme_id"], "theme_name": row["theme_name"], "operator_note": "보존할 메모"} for row in themes],
        FILES[1]: [*themes, {"run_id": "old", "theme_id": "old-theme", "rank": "1", "theme_name": "옛 테마",
                            "recommended_at": "2026-10-01T12:00:00+09:00"}],
        FILES[2]: [*saved_contents(8), old_content,
                   {"run_id": "THEME_DB", "theme_key": "T1", "rank": "1", "title": "기존 후보"}],
    }
    after = deepcopy(before)
    after[FILES[2]] = [*saved_contents(fetched_at="after"), old_content,
                       {"run_id": "THEME_DB", "theme_key": "T1", "rank": "1", "title": "갱신한 후보"}]
    return before, after, [row["theme_name"] for row in themes]


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

    def test_exact_coverage_rejects_eight_titles_blank_titles_duplicates_and_extra_keys(self):
        themes = [theme(rank) for rank in range(1, 11)]
        good = exact_content_coverage(saved_contents(), "new", themes)
        self.assertEqual(good["themes_with_exact_unique_titles"], 10)
        self.assertEqual(good["rows"], 100)
        self.assertEqual(good["minimum_titles_per_theme"], 10)
        self.assertEqual(good["maximum_titles_per_theme"], 10)
        self.assertEqual(exact_content_coverage(saved_contents(8), "new", themes)["themes_with_exact_unique_titles"], 0)
        for replacement in ("기 생충", "   "):
            changed = saved_contents()
            changed[1]["title"] = replacement
            result = exact_content_coverage(changed, "new", themes)
            self.assertEqual(result["themes_with_exact_unique_titles"], 9)
            self.assertEqual(result["duplicate_or_blank_titles"], 1)
        changed = saved_contents()
        changed[0]["title"], changed[1]["title"] = "Alien", "ＡＬＩＥＮ"
        self.assertEqual(exact_content_coverage(changed, "new", themes)["themes_with_exact_unique_titles"], 9)
        extra = [*saved_contents(), {"run_id": "new", "theme_key": "unknown", "title": "기생충"}]
        self.assertEqual(exact_content_coverage(extra, "new", themes)["unexpected_theme_rows"], 1)

    def test_latest_weekly_run_ignores_director_adoptions_and_uses_saved_timestamps(self):
        before, _, _ = refresh_data()
        rows = [dict(theme(1), run_id="AI-DIRECTOR-20261027", recommended_at="2026-10-27T12:00:00+09:00"),
                dict(theme(1), run_id="legacy-director", director_adoption_key="saved-key",
                     recommended_at="2026-10-28T12:00:00+09:00"), *reversed(before[FILES[1]])]
        run, themes = latest_weekly_run(rows)
        self.assertEqual(run, "new")
        self.assertEqual([int(row["rank"]) for row in themes], list(range(1, 11)))

    def test_full_preservation_detects_lost_duplicates_added_rows_and_changed_cells(self):
        rows = [{"run_id": "old", "title": "기생충"}, {"run_id": "old", "title": "기생충"}]
        self.assertTrue(rows_unchanged(rows, list(reversed(rows))))
        self.assertFalse(rows_unchanged(rows, rows[:1]))
        self.assertFalse(rows_unchanged(rows, [*rows, {"run_id": "new", "title": "코코"}]))
        self.assertFalse(rows_unchanged(rows, [rows[0], {"run_id": "old", "title": "마더"}]))

    def run_refresh(self, before, after, *, click_effect=None):
        page, frame, button = Mock(), Mock(), Mock()
        frame.get_by_role.side_effect = lambda role, **kwargs: button if role == "button" else Mock()
        frame.locator.return_value.count.return_value = 0
        frame.locator.return_value.all_text_contents.return_value = ["10편"] * 10
        button.is_enabled.return_value = True
        button.click.side_effect = click_effect
        args = SimpleNamespace(deployment_timeout=1, generation_timeout=1,
                               expected_count=10, content_refresh_only=True)
        report = {"tests": {}, "counts": {}, "generation_click_attempted": False,
                  "content_refresh_click_attempted": False}
        names = [row["theme_name"] for row in before[FILES[1]] if row.get("run_id") == "new"]
        reader = Mock(side_effect=[before, after])
        return page, frame, button, args, report, names, reader

    def test_content_refresh_updates_only_existing_run_and_never_selects_generation(self):
        before, after, _ = refresh_data()
        page, frame, button, args, report, names, reader = self.run_refresh(before, after)
        with patch("scripts.verify_weekly_live._load_version", return_value=frame) as load, \
                patch("scripts.verify_weekly_live._names", return_value=names), \
                patch("scripts.verify_weekly_live.has_app_exception", return_value=False):
            exercise(page, args, report, read_data=reader)
        button.click.assert_called_once()
        load.assert_called_once()
        self.assertEqual(reader.call_count, 2)
        self.assertTrue(report["content_refresh_click_attempted"])
        self.assertFalse(report["generation_click_attempted"])
        self.assertEqual(report["counts"]["generated_themes"], 0)
        self.assertEqual(report["counts"]["rows"], 100)
        self.assertEqual(report["counts"]["themes_displaying_ten_titles"], 10)
        self.assertTrue(all(report["tests"].values()))
        calls = [call.kwargs.get("name") for call in frame.get_by_role.call_args_list]
        self.assertEqual(calls, ["추천 테마", "콘텐츠 새로고침"])

    def test_refresh_marks_before_uncertain_click_and_never_retries_or_reloads(self):
        before, after, _ = refresh_data()
        page, frame, button, args, report, names, reader = self.run_refresh(before, after)

        def uncertain(**kwargs):
            self.assertTrue(report["content_refresh_click_attempted"])
            self.assertFalse(report["generation_click_attempted"])
            raise RuntimeError("Timeout after server acceptance")

        button.click.side_effect = uncertain
        with patch("scripts.verify_weekly_live._load_version", return_value=frame) as load, \
                patch("scripts.verify_weekly_live._names", return_value=names), \
                self.assertRaises(RuntimeError):
            exercise(page, args, report, read_data=reader)
        button.click.assert_called_once()
        load.assert_called_once()
        reader.assert_called_once()

    def test_persisted_ten_titles_do_not_pass_when_display_still_shows_eight(self):
        before, after, _ = refresh_data()
        page, frame, button, args, report, names, reader = self.run_refresh(before, after)
        frame.locator.return_value.all_text_contents.return_value = ["8편"] * 10
        with patch("scripts.verify_weekly_live._load_version", return_value=frame), \
                patch("scripts.verify_weekly_live._names", return_value=names), \
                patch("scripts.verify_weekly_live.has_app_exception", return_value=False), \
                self.assertRaises(VerificationIssue) as error:
            exercise(page, args, report, read_data=reader)
        self.assertEqual(error.exception.code, "weekly_content_refresh_validation_failed")
        self.assertTrue(report["tests"]["exactly_ten_unique_titles_per_theme"])
        self.assertFalse(report["tests"]["ten_titles_displayed_per_theme"])
        button.click.assert_called_once()

    def test_refresh_requires_ten_existing_themes_before_any_paid_click(self):
        before, after, _ = refresh_data()
        before[FILES[1]] = [row for row in before[FILES[1]] if row.get("theme_id") != "T10"]
        page, frame, button, args, report, names, reader = self.run_refresh(before, after)
        with patch("scripts.verify_weekly_live._load_version", return_value=frame), \
                patch("scripts.verify_weekly_live._names", return_value=names), \
                self.assertRaises(VerificationIssue) as error:
            exercise(page, args, report, read_data=reader)
        self.assertEqual(error.exception.code, "weekly_existing_run_unconfirmed")
        self.assertTrue(error.exception.blocked)
        button.click.assert_not_called()
        self.assertFalse(report["content_refresh_click_attempted"])

    def test_refresh_timeout_with_eight_titles_never_recovers_by_clicking_again(self):
        before, after, _ = refresh_data()
        after[FILES[2]] = saved_contents(8, fetched_at="after")
        page, frame, button, args, report, names, reader = self.run_refresh(before, after)
        with patch("scripts.verify_weekly_live._load_version", return_value=frame) as load, \
                patch("scripts.verify_weekly_live._names", return_value=names), \
                patch("scripts.verify_weekly_live.has_app_exception", return_value=False), \
                patch("scripts.verify_weekly_live.time.monotonic", side_effect=[0, 0, 0, 2]), \
                self.assertRaises(VerificationIssue) as error:
            exercise(page, args, report, read_data=reader)
        self.assertEqual(error.exception.code, "weekly_content_refresh_result_unconfirmed")
        self.assertEqual(report["counts"]["themes_with_exact_unique_titles"], 0)
        button.click.assert_called_once()
        load.assert_called_once()

    def test_refresh_rejects_changes_to_themes_history_or_any_other_run(self):
        before, after, _ = refresh_data()
        changes = [(FILES[0], 0, "operator_note", "changed", "theme_pool_unchanged"),
                   (FILES[1], 0, "theme_name", "changed", "recommendation_history_unchanged"),
                   (FILES[2], 100, "title", "changed", "other_run_contents_unchanged")]
        for file, index, field, value, check in changes:
            with self.subTest(check=check):
                changed = deepcopy(after)
                changed[file][index][field] = value
                page, frame, button, args, report, names, reader = self.run_refresh(before, changed)
                with patch("scripts.verify_weekly_live._load_version", return_value=frame), \
                        patch("scripts.verify_weekly_live._names", return_value=names), \
                        patch("scripts.verify_weekly_live.has_app_exception", return_value=False), \
                        self.assertRaises(VerificationIssue) as error:
                    exercise(page, args, report, read_data=reader)
                self.assertEqual(error.exception.code, "weekly_content_refresh_validation_failed")
                self.assertFalse(report["tests"][check])
                button.click.assert_called_once()

    def test_workflow_refreshes_only_the_named_development_branch(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/verify_weekly_live.yml").read_text()
        self.assertIn("branches: [codex/theme-titles-v161]", workflow)
        self.assertIn("python scripts/verify_weekly_live.py --content-refresh-only", workflow)
        self.assertNotIn("branches: [main]", workflow)


if __name__ == "__main__":
    unittest.main()
