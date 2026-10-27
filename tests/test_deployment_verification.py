"""A pushed revision or anonymous auth redirect is not a verified deployment."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.verify_deployment import (
    VerificationIssue, _load_version, access_problem, exact_version, has_app_exception,
    no_horizontal_overflow, normalized_version, resolve_app_frame, valid_news_digest,
    _reset_attempt, _wait_app_idle,
)


def fake_frame(*, version=None, app=False, exception=False):
    frame = Mock()
    frame.url = "https://public-app.streamlit.app/~/+/"
    def locator(selector):
        found = Mock()
        found.count.return_value = int((selector == ".magazine-footer[data-version]" and version is not None)
                                       or (selector == '.stApp, [data-testid="stApp"]' and app)
                                       or (selector == '[data-testid="stException"]' and exception))
        found.inner_text.return_value = "Public app"
        found.last.get_attribute.return_value = version
        return found
    frame.locator.side_effect = locator
    return frame


class DeploymentVerificationTests(unittest.TestCase):
    def test_retry_discards_previous_viewport_metadata_and_preserves_safe_failure_history(self):
        report = {'schema_version': 1, 'status': 'failed', 'expected_version': '1.6.2',
                  'attempts': 2, 'observed_version': 'old', 'frame_state': 'old',
                  'media_reader': {'mobile': {'scroll_drift_px': 0}},
                  'ott_title_search': {'mobile': {'providers': 9}},
                  'diagnostics': {'roots': 1}, 'active_check': 'old',
                  'attempt_failures': [{'code': 'app_target_idle_timeout'}]}
        _reset_attempt(report)
        for key in ('media_reader', 'ott_title_search', 'diagnostics', 'active_check', 'frame_state'):
            self.assertNotIn(key, report)
        self.assertEqual(report['attempts'], 2)
        self.assertEqual(len(report['tests']), 50)
        self.assertFalse(any(report['tests'].values()))
        self.assertIsNone(report['observed_version'])
        self.assertEqual(report['attempt_failures'], [{'code': 'app_target_idle_timeout'}])

    def test_idle_wait_requires_completed_server_run_before_stable_dom_is_accepted(self):
        page = Mock()
        tracker = SimpleNamespace(runs=1, successes=1, finished_run=1,
                                  running=True, compile_error=False, last_control=0,
                                  decode_errors=0)
        page._release_rerun_tracker = tracker
        frame = Mock(page=page)
        ticks = iter((0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0))
        def settle(_):
            if page.wait_for_timeout.call_count >= 3:
                tracker.running = False
        page.wait_for_timeout.side_effect = settle
        with (patch('scripts.verify_deployment.time.monotonic', side_effect=lambda: next(ticks)),
              patch('scripts.verify_deployment._numeric_state', return_value={'roots': 1, 'stale': 0, 'nodes': 5})):
            _wait_app_idle(frame, {}, 'desktop', selector='.test', stage='ready')
        self.assertGreaterEqual(page.wait_for_timeout.call_count, 4)

    def test_idle_timeout_reports_only_numeric_state_without_source_or_dom_text(self):
        page = Mock()
        frame = Mock(page=page)
        report = {}
        with (patch('scripts.verify_deployment.time.monotonic', side_effect=[0, .1, .2, 2]),
              patch('scripts.verify_deployment._numeric_state', return_value={'roots': 1, 'stale': 1})):
            with self.assertRaisesRegex(VerificationIssue, '^app_target_idle_timeout$'):
                _wait_app_idle(frame, report, 'mobile', selector='.test', stage='ready', timeout=1000)
        self.assertEqual(report['active_check'], 'mobile_ready')
        self.assertEqual(report['diagnostics'], {'roots': 1, 'stale': 1})

    def test_news_digest_requires_at_most_four_recent_publication_dates_in_order(self):
        now = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
        dates = ["2026-10-26T08:00:00+09:00", "2026-10-25T12:00:00+09:00",
                 "2026-10-24T12:00:00+09:00", "2026-10-23T12:00:00+09:00"]
        self.assertTrue(valid_news_digest(dates, 4, 4, now=now))
        self.assertTrue(valid_news_digest(dates[:2], 4, 2, now=now))
        self.assertFalse(valid_news_digest(dates + dates[:1], 4, 5, now=now))
        self.assertFalse(valid_news_digest(list(reversed(dates)), 4, 4, now=now))
        self.assertFalse(valid_news_digest(dates, 2, 4, now=now))

    def test_news_digest_rejects_old_future_missing_and_collection_only_dates(self):
        now = datetime(2026, 10, 26, 1, tzinfo=timezone.utc)
        for value in ("2026-10-19", "2026-10-27", "", "unknown"):
            with self.subTest(value=value):
                self.assertFalse(valid_news_digest([value], 4, 1, now=now))

    def test_sleep_button_can_appear_after_initial_page_navigation(self):
        app = fake_frame(version="1.3.0", app=True)
        page = Mock(frames=[app], url="https://public-app.streamlit.app/")
        page.goto.return_value.status = 200
        wake = page.get_by_role.return_value
        wake.count.side_effect = [0, 1]
        wake.first.is_visible.return_value = True
        report = {"tests": {}}
        args = SimpleNamespace(url=page.url, expected_version="1.3.0")
        with patch("scripts.verify_deployment.resolve_app_frame", side_effect=[None, app]):
            self.assertIs(_load_version(page, args, report, "desktop"), app)
        wake.first.click.assert_called_once()
        self.assertTrue(report["tests"]["desktop_version"])

    def test_actual_release_is_found_in_nested_public_iframe_not_wrapper(self):
        wrapper, intermediate, app = fake_frame(), fake_frame(), fake_frame(version="1.1.0", app=True)
        page = Mock(frames=[wrapper, intermediate, app])
        self.assertIs(resolve_app_frame(page), app)
        self.assertIs(resolve_app_frame(Mock(frames=[app])), app)

    def test_rendered_legacy_frame_and_nested_exception_are_not_wrapper_only(self):
        wrapper, app = fake_frame(), fake_frame(app=True, exception=True)
        page = Mock(frames=[wrapper, app])
        self.assertIs(resolve_app_frame(page), app)
        self.assertTrue(has_app_exception(page))

    def test_iframe_version_is_returned_and_old_version_remains_pending(self):
        for version in ("1.1.0", "1.0.0"):
            with self.subTest(version=version):
                wrapper, app = fake_frame(), fake_frame(version=version, app=True)
                page = Mock(frames=[wrapper, app])
                page.url = "https://public-app.streamlit.app/"
                page.goto.return_value.status = 200
                page.get_by_role.return_value.count.return_value = 0
                report = {"tests": {}}
                args = SimpleNamespace(url=page.url, expected_version="1.1.0")
                if version == "1.1.0":
                    self.assertIs(_load_version(page, args, report, "desktop"), app)
                    self.assertEqual(report["frame_state"], "matched_release")
                else:
                    with self.assertRaisesRegex(VerificationIssue, "expected_release_not_ready"):
                        _load_version(page, args, report, "desktop")
                    self.assertEqual(report["frame_state"], "outdated_release")
                self.assertEqual(report["observed_version"], version)

    def test_release_marker_must_match_the_complete_expected_version(self):
        self.assertTrue(exact_version("1.1.0", "1.1.0"))
        self.assertTrue(exact_version(" v1.1.0\n", "1.1.0"))
        for observed in (None, "", "1.1.0-rc.1", "1.1.01", "1.1.0+different-build", "1.1.0 old code", "11.1.0"):
            with self.subTest(observed=observed):
                self.assertFalse(exact_version(observed, "1.1.0"))

    def test_invalid_or_sensitive_marker_text_is_not_stored_as_a_version(self):
        for marker in ("https://example.test/?token=do-not-log", "<script>secret</script>", "v01.1.0", "1.1.0-01"):
            with self.subTest(marker=marker):
                self.assertIsNone(normalized_version(marker))

    def test_anonymous_auth_cookie_redirect_is_not_assumed_to_require_login(self):
        self.assertIsNone(access_problem("https://share.streamlit.io/-/auth/app?redirect_uri=public-app", 200))
        self.assertIsNone(access_problem("https://example.streamlit.app/", 200))

    def test_actual_access_denial_or_login_requirement_is_blocked(self):
        self.assertEqual(access_problem("https://example.streamlit.app/", 403), "http_access_denied")
        self.assertEqual(access_problem("https://share.streamlit.io/account/login", 200), "authentication_required")
        self.assertEqual(access_problem("https://accounts.google.com/signin", 200), "authentication_required")
        self.assertEqual(access_problem("https://example.streamlit.app/", 200, True), "authentication_required")

    def test_overflow_checks_include_the_streamlit_internal_scroll_container(self):
        dimensions = dict(viewport=390, document_scroll=390, main_width=390, main_scroll=390)
        self.assertTrue(no_horizontal_overflow(dimensions))
        self.assertTrue(no_horizontal_overflow(dict(dimensions, document_scroll=391)))
        self.assertFalse(no_horizontal_overflow(dict(dimensions, document_scroll=400)))
        self.assertFalse(no_horizontal_overflow(dict(dimensions, main_scroll=520)))


if __name__ == "__main__":
    unittest.main()
