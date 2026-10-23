"""Generate one real weekly run using the deployed app's normal action.

This is intentionally NOT a read-only smoke test: one click invokes the normal
theme and content LLM calls and appends a production weekly run. Never retry a
click after an uncertain result. Keys remain on the app server. Only counts,
booleans, release markers and fixed error codes enter the verification report.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import APP_URL, VerificationIssue, _load_version, has_app_exception
from theme_generation_rules import title_review

FILES = ("theme_db.csv", "theme_recommendation_history.csv", "theme_content_candidates.csv")


def csv_rows(text):
    return list(csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="|"))


def read_main():
    """Fetch only normal public Git history; do not write app data or print git errors."""
    def git(*args):
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=45)
        if result.returncode:
            raise VerificationIssue("saved_data_read_failed")
        return result.stdout.decode("utf-8-sig")
    git("fetch", "origin", "main")
    return {name: csv_rows(git("show", "FETCH_HEAD:" + name)) for name in FILES}


def prior_rows_preserved(before, after, *, keys, mutable=()):
    """Compare original cells, tolerating added schema columns and allowed dates."""
    indexed = {tuple(row.get(key, "") for key in keys): row for row in after}
    for original in before:
        current = indexed.get(tuple(original.get(key, "") for key in keys))
        if current is None or any(current.get(key, "") != value for key, value in original.items() if key not in mutable):
            return False
    return True


def find_new_run(before, after, names):
    prior = {row.get("run_id") for row in before}
    groups = {}
    for row in after:
        run = row.get("run_id", "")
        if run and run not in prior and not run.startswith("AI-DIRECTOR-"):
            groups.setdefault(run, []).append(row)
    for run, rows in groups.items():
        ordered = sorted(rows, key=lambda row: int(row.get("rank") or 0))
        if [row.get("theme_name", "") for row in ordered] == names:
            return run, ordered
    return None, []


def valid_contract(rows, expected_count=10):
    if len(rows) != expected_count:
        return False
    for row in rows:
        if not title_review(row)["eligible"] or not row.get("content_selection_rule") or not row.get("content_exclusion_rule"):
            return False
        try:
            references = json.loads(row.get("content_reference_titles") or "[]")
        except (TypeError, ValueError):
            return False
        if not isinstance(references, list) or len({name for name in references if isinstance(name, str) and name.strip()}) < 3:
            return False
    return True


def content_coverage(rows, run, themes):
    selected = [row for row in rows if row.get("run_id") == run]
    keys = {row.get("theme_id") for row in themes}
    return {
        "rows": len(selected),
        "themes_with_contents": sum(any(row.get("theme_key") == key and row.get("title") for row in selected) for key in keys),
        "themes_with_posters": sum(any(row.get("theme_key") == key and str(row.get("poster_url", "")).startswith("https://") for row in selected) for key in keys),
    }


def _names(frame):
    return [name.strip() for name in frame.locator(".theme-editorial-card .theme-editorial-title").all_text_contents()]


def exercise(page, args, report, *, read_data=read_main):
    # Reloading is permitted only before the single potentially billable click.
    release_deadline = time.monotonic() + args.deployment_timeout
    while True:
        try:
            frame = _load_version(page, args, report, "desktop")
            break
        except VerificationIssue as issue:
            if issue.blocked or not issue.retry or time.monotonic() >= release_deadline:
                raise
            page.wait_for_timeout(15000)
    frame.get_by_role("tab", name="추천 테마", exact=True).click()
    frame.locator(".st-key-curation_section").wait_for(state="visible", timeout=30000)
    button = frame.get_by_role("button", name="테마 생성하기", exact=True)
    button.wait_for(state="visible", timeout=30000)
    if not button.is_enabled():
        raise VerificationIssue("weekly_generation_unavailable", blocked=True)
    before = read_data()
    report["generation_click_attempted"] = True
    # Mark BEFORE click: a browser timeout can occur after the server received it.
    # No catch path calls click again, opens a replacement session, or retries.
    button.click(timeout=30000)
    deadline = time.monotonic() + args.generation_timeout
    after, run, generated = None, None, []
    while time.monotonic() < deadline:
        if has_app_exception(page):
            raise VerificationIssue("weekly_generation_exception")
        names = _names(frame)
        ready = (len(names) == args.expected_count
                 and not frame.locator('[data-stale="true"], [data-testid="stSpinner"]').count())
        if ready:
            after = read_data()
            run, generated = find_new_run(before[FILES[1]], after[FILES[1]], names)
            if run:
                break
        page.wait_for_timeout(5000)
    if not run or after is None:
        raise VerificationIssue("weekly_generation_result_unconfirmed")
    report["tests"]["normal_run_saved"] = True
    report["tests"]["titles_and_content_rules"] = valid_contract(generated, args.expected_count)
    report["tests"]["prior_themes_preserved"] = prior_rows_preserved(
        before[FILES[0]], after[FILES[0]], keys=("theme_id",), mutable=("last_recommended_date",))
    report["tests"]["prior_history_preserved"] = prior_rows_preserved(
        before[FILES[1]], after[FILES[1]], keys=("run_id", "theme_id", "rank"))
    historical_content = lambda data: [row for row in data[FILES[2]] if row.get("run_id") != "THEME_DB"]
    report["tests"]["prior_run_contents_preserved"] = prior_rows_preserved(
        historical_content(before), historical_content(after), keys=("run_id", "theme_key", "rank"))
    coverage = content_coverage(after[FILES[2]], run, generated)
    report["counts"] = {"generated_themes": len(generated), **coverage}
    report["tests"]["content_connection"] = coverage["themes_with_contents"] == args.expected_count
    report["tests"]["poster_connection"] = coverage["themes_with_posters"] == args.expected_count
    loaded = frame.locator('.theme-editorial-card').evaluate_all(r'''async cards => {
      return await Promise.all(cards.map(async card => {
        const sources = Array.from(card.querySelectorAll('.theme-poster-image')).map(element => {
          const match = getComputedStyle(element).backgroundImage.match(/^url\((["']?)(.*?)\1\)$/);
          return match ? match[2] : '';
        }).filter(Boolean);
        const results = await Promise.all(sources.map(source => new Promise(resolve => {
          const image = new Image(); let complete = false;
          const finish = loaded => {if (complete) return; complete = true; clearTimeout(timer); resolve(loaded);};
          const timer = setTimeout(() => finish(false), 8000);
          image.onload = () => finish(image.naturalWidth > 0);
          image.onerror = () => finish(false);
          image.src = source;
          if (image.complete && image.naturalWidth > 0) finish(true);
        })));
        return results.some(Boolean);
      }));
    }''')
    report["counts"]["themes_with_loaded_poster"] = sum(loaded)
    report["tests"]["actual_poster_loading"] = len(loaded) == args.expected_count and all(loaded)
    report["tests"]["no_exception"] = not has_app_exception(page)
    if not all(report["tests"].values()):
        raise VerificationIssue("weekly_saved_run_validation_failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version", default=(ROOT / "VERSION").read_text().strip())
    parser.add_argument("--expected-count", type=int, default=10)
    parser.add_argument("--deployment-timeout", type=int, default=480)
    parser.add_argument("--generation-timeout", type=int, default=360)
    parser.add_argument("--executable", default=os.getenv("WEEKLY_CHROMIUM_PATH", ""))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/weekly_live_validation.json")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "failed", "expected_version": args.expected_version,
              "observed_version": None, "started_at": datetime.now(timezone.utc).isoformat(),
              "generation_click_attempted": False, "tests": {}, "counts": {}, "errors": [],
              "source_commit": os.getenv("GITHUB_SHA", "")}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                page = browser.new_page(viewport={"width": 1366, "height": 900})
                exercise(page, args, report)
                report["status"] = "passed"
            finally:
                browser.close()
    except VerificationIssue as issue:
        report["status"] = "blocked" if issue.blocked else "failed"
        report["errors"].append(issue.code)
    except Exception:
        report["errors"].append("weekly_verification_failed")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "generation_click_attempted": report["generation_click_attempted"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
