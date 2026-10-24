"""Cache Korean titles through one explicit action in the deployed app.

Uses the app's existing AI/storage configuration without exporting credentials.
Loading/filtering is free. Daily runs submit at most one batch;
an ambiguous timeout never resubmits. Initial backlog can use two distinct batches.
The source-hash cache avoids repeat work.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import APP_URL, VerificationIssue, _load_version


def run(args):
    from playwright.sync_api import sync_playwright
    report = {"status": "failed", "tests": {}, "expected_version": args.expected_version,
              "button_submitted": False, "stage": "loading", "checked_at": datetime.now(timezone.utc).isoformat()}
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                page = browser.new_page(viewport={"width": 1366, "height": 1000})
                deadline = time.monotonic() + args.timeout
                # Only unpaid loading/navigation may retry while Cloud updates.
                while True:
                    try:
                        frame = _load_version(page, args, report, "localization")
                        frame.get_by_role("tab", name="미디어 인사이트", exact=True).click()
                        frame.locator(".media-insights-heading").wait_for(state="visible", timeout=30000)
                        report["stage"] = "media_open"
                        frame.locator('.media-section-label').first.wait_for(state='visible', timeout=30000)
                        action = frame.get_by_role('button', name='한글 정리', exact=True)
                        report['translation_control_count'] = action.count()
                        if action.count() == 0:
                            report["status"] = "already_cached"
                            return report
                        if not action.is_enabled():
                            report["error_code"] = "translation_connection_unavailable"
                            return report
                        report["stage"] = "control_ready"
                        report["control_visible"] = action.is_visible()
                        action.click(trial=True, timeout=15000)  # no event dispatched, no API call
                        break
                    except VerificationIssue as error:
                        if not error.retry or time.monotonic() >= deadline:
                            raise
                        page.wait_for_timeout(15000)
                    except Exception as error:
                        report["prepare_failure_type"] = type(error).__name__
                        message = str(error).lower()
                        report['prepare_diagnostic'] = next((code for phrase, code in (
                            ('strict mode violation', 'multiple_controls'),
                            ('execution context was destroyed', 'context_reloaded'),
                            ('frame was detached', 'frame_detached'),
                            ('has been closed', 'browser_closed'),
                            ('is not a valid selector', 'invalid_selector'),
                        ) if phrase in message), 'unavailable')
                        if time.monotonic() >= deadline:
                            raise
                        page.wait_for_timeout(15000)
                report["batches"] = []
                for _ in range(args.max_batches):
                    before = frame.locator(".media-pending-translation").count()
                    action.scroll_into_view_if_needed(timeout=10000)
                    action.click()
                    report["button_submitted"] = True
                    report["stage"] = "submitted"
                    # An uncertain or partial submission is never repeated.
                    page.wait_for_timeout(1000)
                    frame.locator('[data-testid="stSpinner"]').wait_for(state="hidden", timeout=150000)
                    frame.wait_for_function("() => !document.querySelector('.st-key-media_insights_section [data-stale=true]')", timeout=30000)
                    after = frame.locator(".media-pending-translation").count()
                    report["batches"].append({"pending_before": before, "pending_after": after})
                    if (frame.locator('.st-key-media_insights_section [data-testid="stException"], .st-key-media_insights_section [data-testid="stAlert"]').count()
                            or after >= before):
                        report["status"] = "review_needed"
                        break
                    report["status"] = "completed"
                    action = frame.get_by_role('button', name='한글 정리', exact=True)
                    if not after or action.count() == 0 or not action.is_enabled():
                        break
            finally:
                browser.close()
    except VerificationIssue as error:
        report["error_code"] = error.code
    except Exception as error:
        report["failure_type"] = type(error).__name__
        report["error_code"] = ("submission_outcome_unconfirmed" if report["button_submitted"]
                                else "public_app_unavailable")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version", default=(ROOT / "VERSION").read_text().strip())
    parser.add_argument("--timeout", type=float, default=360)
    parser.add_argument("--executable")
    parser.add_argument("--max-batches", type=int, choices=(1, 2), default=1,
                        help="One daily batch; optionally finish a two-batch initial backlog.")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/media_localization_validation.json")
    args = parser.parse_args()
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"completed", "already_cached"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
