"""Billing failures must never become a plausible expense or credit balance."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from api_billing import MAX_COST_PAGES, fetch_monthly_project_costs, monthly_budget_summary


NOW = datetime(2026, 10, 23, 5, 4, 3, tzinfo=timezone.utc)


def page(*rows, has_more=False, next_page=None):
    return {"data": [{"results": list(rows)}], "has_more": has_more, "next_page": next_page}


def cost(value, project="proj-app", currency="usd"):
    return {"project_id": project, "amount": {"value": value, "currency": currency}}


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


class ApiBillingTests(unittest.TestCase):
    def fetch(self, get, **kwargs):
        return fetch_monthly_project_costs("admin-secret", "proj-app", now=NOW, request_get=get, **kwargs)

    def test_requires_explicit_admin_key_and_single_project(self):
        get = Mock()
        for key, project in [("", "proj-app"), ("admin-secret", ""), (None, "proj-app")]:
            with self.subTest(key=bool(key), project=project):
                report = fetch_monthly_project_costs(key, project, now=NOW, request_get=get)
                self.assertEqual(report["status"], "not_configured")
                self.assertIsNone(report["cost_usd"])
        for project in ["*", "proj-a,proj-b", "proj-a proj-b"]:
            report = fetch_monthly_project_costs("admin-secret", project, now=NOW, request_get=get)
            self.assertEqual(report["status"], "unavailable")
        get.assert_not_called()

    def test_month_bounds_and_scope_are_explicit_and_usd_sum_is_exact(self):
        get = Mock(return_value=response(page(cost("0.1"), cost("0.2"), cost("500", project="proj-other"))))
        report = self.fetch(get)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["cost_usd"], Decimal("0.3"))
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["project_ids"], ["proj-app"])
        self.assertEqual(params["group_by"], ["project_id"])
        self.assertEqual(params["start_time"], int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(params["end_time"], int(NOW.timestamp()))
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 15))
        self.assertNotIn("admin-secret", str(report))
        self.assertNotIn("proj-app", str(report))

    def test_timezone_conversion_uses_utc_calendar_month(self):
        local = datetime(2026, 11, 1, 1, tzinfo=timezone(timedelta(hours=9)))
        get = Mock(return_value=response({"data": [], "has_more": False}))
        report = fetch_monthly_project_costs("admin-secret", "proj-app", now=local, request_get=get)
        self.assertEqual(report["month"], "2026-10")
        self.assertEqual(report["cost_usd"], Decimal("0"))

    def test_all_pages_accumulate_and_incomplete_fetch_never_returns_partial_total(self):
        get = Mock(side_effect=[response(page(cost("1.23"), has_more=True, next_page="two")),
                                response(page(cost("2.34")))])
        self.assertEqual(self.fetch(get)["cost_usd"], Decimal("3.57"))
        self.assertEqual(get.call_args.kwargs["params"]["page"], "two")
        get = Mock(side_effect=[response(page(cost("1.23"), has_more=True, next_page="two")),
                                RuntimeError("SECRET RESPONSE BODY")])
        report = self.fetch(get)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["cost_usd"])
        self.assertNotIn("SECRET", str(report))

    def test_ungrouped_or_malformed_amounts_are_unknown_not_zero(self):
        bad_rows = [cost("5", project=None), cost("5", project=""), cost("NaN"), cost("Infinity"),
                    cost(True), cost(None), cost("3", currency="eur"), {"project_id": "proj-app"}, "bad"]
        for row in bad_rows:
            with self.subTest(row=row):
                report = self.fetch(Mock(return_value=response(page(row))))
                self.assertEqual(report["status"], "unavailable")
                self.assertIsNone(report["cost_usd"])

    def test_malformed_buckets_and_http_failures_are_sanitized(self):
        for payload in [{}, {"data": "bad", "has_more": False}, {"data": [{}], "has_more": False},
                        {"data": [], "has_more": "false"}, None]:
            report = self.fetch(Mock(return_value=response(payload)))
            self.assertEqual(report["status"], "unavailable")
            self.assertIsNone(report["cost_usd"])
        denied = response(page())
        denied.raise_for_status.side_effect = RuntimeError("admin-secret should never appear")
        report = self.fetch(Mock(return_value=denied))
        self.assertNotIn("admin-secret", str(report))
        self.assertIsNone(report["cost_usd"])

    def test_cyclic_missing_and_unbounded_pagination_never_return_partial_costs(self):
        for next_page in [None, "", " "]:
            report = self.fetch(Mock(return_value=response(page(cost(1), has_more=True, next_page=next_page))))
            self.assertEqual(report["reason"], "invalid_pagination")
            self.assertIsNone(report["cost_usd"])
        get = Mock(return_value=response(page(cost(1), has_more=True, next_page="repeat")))
        self.assertEqual(self.fetch(get)["reason"], "invalid_pagination")
        self.assertEqual(get.call_count, 2)
        get = Mock(side_effect=[response(page(cost(1), has_more=True, next_page=str(i))) for i in range(MAX_COST_PAGES)])
        report = self.fetch(get)
        self.assertEqual(report["reason"], "pagination_limit")
        self.assertIsNone(report["cost_usd"])
        self.assertEqual(get.call_count, MAX_COST_PAGES)

    def test_slow_pagination_expires_without_exposing_partial_amount(self):
        get = Mock(return_value=response(page(cost(1), has_more=True, next_page="two")))
        with patch("api_billing.monotonic", side_effect=[0, 0, 31]):
            report = self.fetch(get)
        self.assertEqual(report["reason"], "fetch_timeout")
        self.assertIsNone(report["cost_usd"])
        self.assertEqual(get.call_count, 1)

    def test_budget_remainder_is_not_credit_and_preserves_overspend(self):
        report = {"status": "ok", "cost_usd": Decimal("12.50")}
        self.assertEqual(monthly_budget_summary(report, "10")["remaining_budget_usd"], Decimal("-2.50"))
        self.assertEqual(monthly_budget_summary(report, "0")["remaining_budget_usd"], Decimal("-12.50"))
        for budget in ["", None, True, "NaN", "Infinity", "-1"]:
            self.assertIsNone(monthly_budget_summary(report, budget)["budget_usd"])
        failed = monthly_budget_summary({"status": "unavailable", "cost_usd": None}, "10")
        self.assertEqual(failed["budget_usd"], Decimal("10"))
        self.assertIsNone(failed["remaining_budget_usd"])


if __name__ == "__main__":
    unittest.main()
