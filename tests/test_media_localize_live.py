"""Read-only inspection must never submit the billed localization action."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, call, patch

from scripts.localize_media_live import run, warning_code
from scripts.verify_deployment import VerificationIssue


def fixture(*, check_only=True, pending=1, enabled=True, alerts=(), max_batches=2):
    args = SimpleNamespace(expected_version="1.6.4", executable=None, timeout=1,
                           max_batches=max_batches, check_only=check_only)
    manager, driver, browser, page, frame, action = [MagicMock() for _ in range(6)]
    manager.__enter__.return_value = driver
    driver.chromium.launch.return_value = browser
    browser.new_page.return_value = page
    frame.page = page
    action.count.return_value = int(pending > 0)
    action.is_enabled.return_value = enabled
    action.is_visible.return_value = True
    frame.get_by_role.side_effect = lambda role, **kwargs: action if role == "button" else MagicMock()
    pending_locator, alerts_locator, exception_locator = MagicMock(), MagicMock(), MagicMock()
    pending_locator.count.return_value = pending
    alerts_locator.all_text_contents.return_value = list(alerts)
    exception_locator.count.return_value = 0
    def locator(selector):
        if selector == ".media-pending-translation":
            return pending_locator
        if selector.endswith('[data-testid="stAlert"]'):
            return alerts_locator
        if selector.endswith('[data-testid="stException"]'):
            return exception_locator
        return MagicMock()
    frame.locator.side_effect = locator
    return args, manager, frame, action, browser


class ReadOnlyLocalizationTests(unittest.TestCase):
    def inspect(self, *, pending, enabled=True):
        args, manager, frame, action, browser = fixture(pending=pending, enabled=enabled)
        with (patch("playwright.sync_api.sync_playwright", return_value=manager),
              patch("scripts.localize_media_live._load_version", return_value=frame),
              patch("scripts.localize_media_live._wait_app_idle")):
            report = run(args)
        action.click.assert_not_called()  # Even trial clicks are unnecessary.
        action.scroll_into_view_if_needed.assert_not_called()
        browser.close.assert_called_once()
        self.assertFalse(report["button_submitted"])
        self.assertFalse(report["provider_health_checked"])
        self.assertTrue(report["check_only"])
        self.assertEqual(report["stage"], "inspection_complete")
        self.assertNotIn("batches", report)
        return report

    def test_cached_inspection_never_clicks_or_claims_api_credentials_were_tested(self):
        report = self.inspect(pending=0)
        self.assertEqual(report["status"], "already_cached")
        self.assertEqual(report["pending_count"], 0)

    def test_queued_work_is_reported_without_submitting_it(self):
        report = self.inspect(pending=2)
        self.assertEqual(report["status"], "pending_translation")
        self.assertEqual(report["pending_count"], 2)
        self.assertTrue(report["translation_control_enabled"])

    def test_disabled_control_does_not_turn_queued_work_into_an_api_health_claim(self):
        report = self.inspect(pending=1, enabled=False)
        self.assertEqual(report["status"], "pending_translation")
        self.assertFalse(report["translation_control_enabled"])

    def test_navigation_failure_remains_a_failure_without_clicks(self):
        args, manager, frame, action, _ = fixture()
        with (patch("playwright.sync_api.sync_playwright", return_value=manager),
              patch("scripts.localize_media_live._load_version", side_effect=VerificationIssue("http_access_denied"))):
            report = run(args)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_code"], "http_access_denied")
        action.click.assert_not_called()


class LocalizationDiagnosticTests(unittest.TestCase):
    def test_warning_codes_distinguish_validation_provider_storage_and_unknown_text(self):
        for text, expected in (
            ("일부 자료의 한글 정리를 확인하지 못했습니다. 확인된 결과만 저장했습니다.", "translation_validation_held"),
            ("한글 정리 연결을 확인하지 못했습니다. 원문은 유지됩니다.", "translation_provider_error"),
            ("GitHub 미디어 자료를 저장하지 못했습니다. 분석 내용은 유지했습니다.", "translation_storage_error"),
            ("원문이 갱신돼 한글 정리를 저장하지 않았습니다.", "translation_source_changed"),
            ("한글 정리를 완료하지 못했습니다. secret-data", "translation_action_warning"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(warning_code(text), expected)

    def test_normal_action_stops_after_a_warning_and_exports_only_a_safe_code(self):
        private = "secret-do-not-export https://private.example/?token=private-token"
        args, manager, frame, action, _ = fixture(
            check_only=False, alerts=("한글 정리 연결을 확인하지 못했습니다. " + private,))
        with (patch("playwright.sync_api.sync_playwright", return_value=manager),
              patch("scripts.localize_media_live._load_version", return_value=frame)):
            report = run(args)
        self.assertEqual(report["status"], "review_needed")
        self.assertEqual(report["error_code"], "translation_provider_error")
        self.assertEqual(report["warning_codes"], ["translation_provider_error"])
        self.assertEqual(action.click.call_args_list, [call(trial=True, timeout=15000), call()])
        self.assertEqual(len(report["batches"]), 1)
        self.assertTrue(report["button_submitted"])
        self.assertNotIn("secret-do-not-export", json.dumps(report))
        self.assertNotIn("private-token", json.dumps(report))

    def test_unchanged_pending_without_warning_is_not_called_a_provider_failure(self):
        args, manager, frame, action, _ = fixture(check_only=False)
        with (patch("playwright.sync_api.sync_playwright", return_value=manager),
              patch("scripts.localize_media_live._load_version", return_value=frame)):
            report = run(args)
        self.assertEqual(report["error_code"], "translation_pending_unchanged")
        self.assertEqual(report["warning_codes"], [])
        self.assertEqual(len(report["batches"]), 1)
        self.assertEqual(action.click.call_count, 2)  # One trial and one submission.


if __name__ == "__main__":
    unittest.main()
