"""Read project-scoped API expenses; never infer an account's prepaid balance.

``fetch_monthly_project_costs`` returns a small, safe report. Only ``status ==
"ok"`` has a Decimal ``cost_usd``. An absent configuration, failed request, or
incomplete response has no amount, so an outage cannot appear as zero expense.
The caller may cache reports; this module neither reads secrets nor persists data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from time import monotonic

import requests


COSTS_URL = "https://api.openai.com/v1/organization/costs"
MAX_COST_PAGES = 8
MAX_FETCH_SECONDS = 30


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def monthly_budget_summary(report, budget_usd):
    """Return a configured monthly budget and its remainder, not account credit.

    An invalid/negative budget is unconfigured. A valid budget remains visible
    during billing failures, but its remainder is unknown. Overspend is negative.
    """
    budget = _decimal(budget_usd)
    if budget is not None and budget < 0:
        budget = None
    cost = _decimal(report.get("cost_usd")) if report.get("status") == "ok" else None
    return {"budget_usd": budget,
            "remaining_budget_usd": budget - cost if budget is not None and cost is not None else None}


def fetch_monthly_project_costs(admin_api_key, project_id, *, now=None, request_get=None):
    """Get this UTC month's complete reported USD costs for one project.

    Requires a separately configured Admin API key and an explicit project ID.
    Ordinary generation keys are never used as a fallback. The HTTP response,
    credentials, identifiers, and exception text never appear in the report.
    ``request_get`` is an optional requests-compatible injectable transport.

    Result keys: status (ok/not_configured/unavailable), month (YYYY-MM),
    currency (USD), cost_usd (Decimal or None), checked_at (UTC ISO8601), reason
    (a safe enum or empty). Costs may be delayed in the provider's reporting.
    """
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    start = observed.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    report = {"status": "not_configured", "month": start.strftime("%Y-%m"),
              "currency": "USD", "cost_usd": None, "checked_at": observed.isoformat(),
              "reason": "configuration_missing"}
    if not isinstance(admin_api_key, str) or not admin_api_key.strip() or not isinstance(project_id, str) or not project_id.strip():
        return report
    # Treat project scope as an exact identifier, not a list or wildcard.
    project = project_id.strip()
    if any(char.isspace() for char in project) or "," in project or project == "*":
        report.update(status="unavailable", reason="invalid_configuration")
        return report
    report.update(status="unavailable", reason="fetch_failed")
    headers = {"Authorization": "Bearer " + admin_api_key.strip()}
    params = {"start_time": int(start.timestamp()), "end_time": math.ceil(observed.timestamp()),
              "bucket_width": "1d", "project_ids": [project], "group_by": ["project_id"], "limit": 31}
    if params["end_time"] <= params["start_time"]:
        report.update(status="ok", cost_usd=Decimal("0"), reason="")
        return report
    get = request_get or requests.get
    total = Decimal("0")
    seen_pages = set()
    deadline = monotonic() + MAX_FETCH_SECONDS
    try:
        for _ in range(MAX_COST_PAGES):
            remaining = deadline - monotonic()
            if remaining <= 0:
                report["reason"] = "fetch_timeout"
                return report
            timeout = (min(5, remaining / 3), min(15, remaining * 2 / 3))
            response = get(COSTS_URL, headers=headers, params=dict(params), timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not isinstance(payload.get("has_more"), bool):
                report["reason"] = "invalid_response"
                return report
            for bucket in payload["data"]:
                if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
                    report["reason"] = "invalid_response"
                    return report
                for row in bucket["results"]:
                    if not isinstance(row, dict):
                        report["reason"] = "invalid_response"
                        return report
                    # An ungrouped result might be the entire organization.
                    # Never attribute it to this app or present it as zero.
                    if not isinstance(row.get("project_id"), str) or not row["project_id"].strip():
                        report["reason"] = "unscoped_response"
                        return report
                    if row["project_id"] != project:
                        continue
                    amount = row.get("amount")
                    if not isinstance(amount, dict) or str(amount.get("currency", "")).lower() != "usd":
                        report["reason"] = "invalid_response"
                        return report
                    value = _decimal(amount.get("value"))
                    if value is None:
                        report["reason"] = "invalid_response"
                        return report
                    total += value
            if not payload["has_more"]:
                report.update(status="ok", cost_usd=total, reason="")
                return report
            page = payload.get("next_page")
            if not isinstance(page, str) or not page.strip() or page in seen_pages:
                report["reason"] = "invalid_pagination"
                return report
            seen_pages.add(page)
            params["page"] = page
        report["reason"] = "pagination_limit"
    except Exception:
        # Do not expose response bodies, provider errors, or credential values.
        report["reason"] = "fetch_failed"
    return report
