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
from scripts.verify_deployment import APP_URL, VerificationIssue, _load_version, _wait_app_idle, _attach_rerun_tracker


def warning_code(text):
    """Classify known public UI warnings without exporting their raw text."""
    text = str(text or "")
    known = (
        ("일부 자료의 한글 정리를 확인하지 못했습니다", "translation_validation_held"),
        ("한글 정리 연결을 확인하지 못했습니다", "translation_provider_error"),
        ("한글 정리를 사용하려면 AI 연결 설정이 필요합니다", "translation_api_key_missing"),
        ("AI가 한글 정리 결과를 반환하지 않았습니다", "translation_response_unavailable"),
        ("원문이 갱신돼 한글 정리를 저장하지 않았습니다", "translation_source_changed"),
    )
    for phrase, code in known:
        if phrase in text:
            return code
    storage = (
        "한글 정리의 저장 형식을 확인하지 못했습니다",
        "한글 제목·요약과 원문 정보를 확인하지 못했습니다",
        "GitHub 저장소 연결 설정을 확인해 주세요",
        "GitHub 저장 결과를 확인하지 못했습니다",
        "GitHub 미디어 자료를 읽지 못했습니다",
        "GitHub 미디어 자료의 저장 형식을 확인하지 못했습니다",
        "GitHub 미디어 자료를 저장하지 못했습니다",
        "미디어 자료의 저장 경로를 확인해 주세요",
        "미디어 자료 저장을 확인하지 못했습니다",
        "미디어 자료를 저장하지 못했습니다",
        "다른 작업이 미디어 자료를 갱신했습니다",
    )
    if any(phrase in text for phrase in storage):
        return "translation_storage_error"
    return "translation_action_warning"


def _action_warning_codes(frame):
    codes = []
    if frame.locator('.st-key-media_insights_section [data-testid="stException"]').count():
        codes.append("translation_app_exception")
    # Only fixed codes leave this function. Provider/exception text, links and
    # credentials must never be copied into the public JSON report.
    alerts = frame.locator('.st-key-media_insights_section [data-testid="stAlert"]')
    codes.extend(warning_code(text) for text in alerts.all_text_contents())
    return list(dict.fromkeys(codes))


def run(args):
    from playwright.sync_api import sync_playwright
    report = {"status": "failed", "tests": {}, "expected_version": args.expected_version,
              "button_submitted": False, "stage": "loading", "checked_at": datetime.now(timezone.utc).isoformat()}
    check_only = bool(getattr(args, "check_only", False))
    report["check_only"] = check_only
    if check_only:
        report["provider_health_checked"] = False
    try:
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                page = browser.new_page(viewport={"width": 1366, "height": 1000})
                if check_only:
                    _attach_rerun_tracker(page)
                deadline = time.monotonic() + args.timeout
                # Only unpaid loading/navigation may retry while Cloud updates.
                while True:
                    try:
                        frame = _load_version(page, args, report, "localization")
                        frame.get_by_role("tab", name="미디어 인사이트", exact=True).click()
                        frame.locator(".media-insights-heading").wait_for(state="visible", timeout=30000)
                        report["stage"] = "media_open"
                        if check_only:
                            _wait_app_idle(frame, report, "localization",
                                           selector=".st-key-media_insights_section", stage="inspection_ready")
                        else:
                            frame.locator('.media-section-label').first.wait_for(state='visible', timeout=30000)
                        action = frame.get_by_role('button', name='한글 정리', exact=True)
                        report['translation_control_count'] = action.count()
                        report['pending_count'] = frame.locator('.media-pending-translation').count()
                        if check_only:
                            report["status"] = "pending_translation" if report['pending_count'] else "already_cached"
                            report["stage"] = "inspection_complete"
                            report["translation_control_enabled"] = action.is_enabled() if action.count() == 1 else False
                            return report
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
                    frame.locator('.st-key-media_insights_section [data-testid="stSpinner"]').first.wait_for(state="hidden", timeout=150000)
                    after = frame.locator(".media-pending-translation").count()
                    report["batches"].append({"pending_before": before, "pending_after": after})
                    codes = _action_warning_codes(frame)
                    if codes or after >= before:
                        report["status"] = "review_needed"
                        report["warning_codes"] = codes
                        report["error_code"] = codes[0] if codes else "translation_pending_unchanged"
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
    parser.add_argument("--check-only", action="store_true",
                        help="Inspect cached/pending state without clicking the paid translation action; does not test provider credentials.")
    parser.add_argument("--max-batches", type=int, choices=(1, 2), default=1,
                        help="One daily batch; optionally finish a two-batch initial backlog.")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/media_localization_validation.json")
    args = parser.parse_args()
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] in {"completed", "already_cached", "pending_translation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
