"""One once-only public research question; persist sanitized source diagnostics."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_research_live import (
    APP_URL, QUESTIONS, VerificationIssue, _load_research, _metadata, _wait_state,
    normalized_version, public_source_hosts, submit_question_once,
)

ERRORS = frozenset({"empty_question", "search_not_configured", "search_unavailable", "search_incomplete",
    "no_primary_citations", "primary_fetch_unavailable", "primary_http_unavailable", "primary_timeout",
    "primary_redirect_refused", "primary_too_large", "primary_document_unusable"})


def sanitized_diagnostics(raw):
    result = {}
    for name, maximum in (("citations_found", 100), ("fetch_attempted", 4)):
        value = str(raw.get(name, ""))
        result[name] = min(int(value), maximum) if re.fullmatch(r"\d{1,5}", value) else 0
    for name in ("search_completed", "fallback_used"):
        result[name] = raw.get(name) == "true"
    result["search_errors"] = sorted({code for code in str(raw.get("search_errors", "")).split(",") if code in ERRORS})
    provider = raw.get("provider_code", "")
    result["provider_code"] = provider if isinstance(provider, str) and re.fullmatch(
        r"(?:not_called|none|unknown|request_timeout|api_connection|invalid_api_key|insufficient_quota|model_not_found|unsupported_parameter|invalid_value|rate_limit_exceeded|http_(?:400|401|403|404|408|409|422|429|500|502|503|504))", provider) else "unknown"
    result["fetch_http_statuses"] = sorted({int(value) for value in str(raw.get("fetch_http_statuses", "")).split(",")
                                           if re.fullmatch(r"[1-5]\d{2}", value)})
    return result


def probe(args):
    report = {"schema_version": 1, "status": "failed", "expected_version": args.expected_version,
              "started_at": datetime.now(timezone.utc).isoformat(), "checks": {},
              "counts": {"questions_attempted": 0}, "responses": [], "errors": []}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                page = browser.new_page(viewport={"width": 1366, "height": 1000})
                frame = _load_research(page, args, report, "desktop")
                started = time.monotonic()
                submit_question_once(frame.locator(".st-key-media_research_question textarea"), report, 1, QUESTIONS[0])
                _wait_state(frame, 1, timeout=180000, deadline=time.monotonic() + 180)
                values = _metadata(frame, 1)
                raw = frame.locator(".media-research-result").last.evaluate("""el => ({
                  search_completed:el.dataset.searchCompleted,citations_found:el.dataset.citationsFound,
                  fetch_attempted:el.dataset.fetchAttempted,search_errors:el.dataset.searchErrors,
                  provider_code:el.dataset.searchProvider,fallback_used:el.dataset.fallbackUsed,
                  fetch_http_statuses:el.dataset.fetchStatuses})""")
                values.update(sanitized_diagnostics(raw))
                values["completion_seconds"] = round(time.monotonic() - started, 2)
                report["responses"] = [values]
                report["source_hosts"] = public_source_hosts(frame.locator(
                    ".st-key-media_research_panel [data-testid='stLinkButton'] a[href]").evaluate_all(
                        "elements=>elements.map(el=>el.href)"))
                report["checks"]["new_primary_document_fetched"] = values["added_documents"] > 0
                report["checks"]["one_once_only_question"] = report["counts"]["questions_attempted"] == 1
                if not report["checks"]["new_primary_document_fetched"]:
                    raise VerificationIssue("new_primary_source_unavailable")
                report["status"] = "passed"
            finally:
                browser.close()
    except VerificationIssue as error:
        report["errors"] = [error.code]
        report["status"] = "blocked" if error.blocked else "failed"
    except Exception:
        report["errors"] = ["source_probe_unavailable"]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version")
    parser.add_argument("--executable", default=os.environ.get("RESEARCH_CHROMIUM_PATH"))
    args = parser.parse_args()
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text().strip())
    if not args.expected_version or not args.url.startswith("https://"):
        parser.error("Exact release and public HTTPS URL required")
    report = probe(args)
    (ROOT / "artifacts/research_sources_live_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
