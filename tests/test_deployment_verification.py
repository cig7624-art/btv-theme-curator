"""A pushed revision or anonymous auth redirect is not a verified deployment."""
import unittest

from scripts.verify_deployment import access_problem, exact_version, no_horizontal_overflow, normalized_version


class DeploymentVerificationTests(unittest.TestCase):
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
