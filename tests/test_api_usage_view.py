"""The cost inspector reads only on demand and never invents an account balance."""
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from api_usage import SESSION_KEY
from api_usage_view import BILLING_URL, USAGE_URL, _cached_report


def view_source(*, configured=False, budget="20"):
    secrets = ({"OPENAI_ADMIN_API_KEY": "test-admin-secret", "OPENAI_BILLING_PROJECT_ID": "proj-test",
                "OPENAI_MONTHLY_BUDGET_USD": budget} if configured else {})
    return ("from api_usage_view import render_api_cost_control\n"
            f"render_api_cost_control(lambda name: {secrets!r}.get(name, ''))\n")


def visible_text(app):
    return "\n".join(item.value for item in [*app.markdown, *app.caption])


class ApiUsageViewTests(unittest.TestCase):
    def assert_no_errors(self, app):
        self.assertEqual(len(app.exception), 0, [item.value for item in app.exception])

    def test_missing_configuration_has_real_billing_links_without_fake_balance(self):
        with patch("api_usage_view.fetch_monthly_project_costs") as fetch, \
                patch("requests.get", side_effect=AssertionError("No network during UI tests")):
            app = AppTest.from_string(view_source()).run()
            app.session_state["api_cost_details"] = True
            app.run()
            self.assert_no_errors(app)
            fetch.assert_not_called()
            text = visible_text(app)
            self.assertIn("자동 조회는 아직 연결되지 않았습니다", text)
            self.assertIn("응답 0회", text)
            self.assertNotIn("$0.00", text)
            links = {item.proto.label: item.proto.url for item in app.get("link_button")}
            self.assertEqual(links["충전 잔액 확인 ↗"], BILLING_URL)
            self.assertEqual(links["OpenAI 사용 내역 ↗"], USAGE_URL)

    def test_configured_view_and_reruns_do_not_query_until_details_are_open(self):
        report = {"status": "ok", "cost_usd": Decimal("3.25")}
        with patch("api_usage_view.fetch_monthly_project_costs", return_value=report) as fetch, \
                patch("requests.get", side_effect=AssertionError("No network during UI tests")):
            app = AppTest.from_string(view_source(configured=True)).run()
            app.run()
            self.assert_no_errors(app)
            fetch.assert_not_called()
            app.session_state["api_cost_details"] = True
            app.run()
            self.assert_no_errors(app)
            fetch.assert_called_once_with("test-admin-secret", "proj-test")
            text = visible_text(app)
            self.assertIn("이번 달 사용액 **$3.25**", text)
            self.assertIn("남은 월 예산 **$16.75**", text)
            self.assertIn("충전 잔액과 다릅니다", text)
            self.assertNotIn("test-admin-secret", text)
            app.run()
            fetch.assert_called_once()

    def test_overspend_and_unavailable_expenses_are_not_a_zero_balance(self):
        for report, expected in [({"status": "ok", "cost_usd": Decimal("21.50")}, "월 예산 초과 **$1.50**"),
                                 ({"status": "unavailable", "cost_usd": None}, "월 사용액을 확인하지 못했어요")]:
            with self.subTest(status=report["status"]), \
                    patch("api_usage_view.fetch_monthly_project_costs", return_value=report):
                app = AppTest.from_string(view_source(configured=True)).run()
                app.session_state["api_cost_details"] = True
                app.run()
                self.assert_no_errors(app)
                text = visible_text(app)
                self.assertIn(expected, text)
                self.assertNotIn("$0.00", text)
                if report["status"] == "unavailable":
                    self.assertNotIn("남은 월 예산", text)
                    self.assertNotIn("이번 달 사용액 **", text)

    def test_session_counts_are_rendered_without_any_billable_operation(self):
        with patch("api_usage_view.fetch_monthly_project_costs") as fetch:
            app = AppTest.from_string(view_source()).run()
            app.session_state[SESSION_KEY] = {
                "response_count": 3, "input_tokens": 1100, "output_tokens": 200,
                "cached_input_tokens": 500, "features": {"director": 2, "content_candidates": 1},
            }
            app.run()
            self.assert_no_errors(app)
            text = visible_text(app)
            self.assertIn("응답 3회 · 1,300토큰", text)
            self.assertIn("입력 1,100", text)
            self.assertIn("캐시 500토큰", text)
            self.assertIn("AI 디렉터 2회", text)
            self.assertIn("콘텐츠 후보 생성 1회", text)
            fetch.assert_not_called()
            app.run()
            self.assertEqual(app.session_state[SESSION_KEY]["response_count"], 3)

    def test_cost_cache_is_ten_minutes_and_invalidates_for_credentials_project_or_month(self):
        state = {}
        report = {"status": "ok", "cost_usd": Decimal("3.25")}
        with patch("api_usage_view.st.session_state", state), \
                patch("api_usage_view.datetime") as clock, \
                patch("api_usage_view.time.monotonic", return_value=100) as monotonic, \
                patch("api_usage_view.fetch_monthly_project_costs", return_value=report) as fetch:
            clock.now.return_value = datetime(2026, 10, 23, tzinfo=timezone.utc)
            self.assertIsNone(_cached_report("test-admin-secret", "proj-test"))
            fetch.assert_not_called()
            self.assertEqual(_cached_report("test-admin-secret", "proj-test", fetch=True), report)
            monotonic.return_value = 699
            self.assertEqual(_cached_report("test-admin-secret", "proj-test", fetch=True), report)
            self.assertEqual(fetch.call_count, 1)
            monotonic.return_value = 700
            _cached_report("test-admin-secret", "proj-test", fetch=True)
            self.assertEqual(fetch.call_count, 2)
            for key, project in [("other-admin-secret", "proj-test"), ("test-admin-secret", "proj-other")]:
                self.assertIsNone(_cached_report(key, project))
            clock.now.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
            self.assertIsNone(_cached_report("test-admin-secret", "proj-test"))
            _cached_report("test-admin-secret", "proj-test", fetch=True)
            self.assertEqual(fetch.call_count, 3)
            self.assertNotIn("test-admin-secret", repr(state))
            self.assertNotIn("proj-test", repr(state))


if __name__ == "__main__":
    unittest.main()
