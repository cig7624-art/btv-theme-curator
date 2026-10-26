"""Verify one real weekly action using the deployed app's normal controls.

This is intentionally NOT a read-only smoke test: one click invokes the normal
LLM calls. Content-refresh-only mode updates the latest saved run's candidates
without creating themes. Never retry a click after an uncertain result. Keys
remain on the app server. Only counts, booleans, release markers and fixed error
codes enter the verification report.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_deployment import APP_URL, VerificationIssue, _load_version, has_app_exception
from theme_generation_rules import title_review

# Production retains the original tracked theme_pool name. theme_db can exist
# as an ignored local mirror, so only inspect the actual repository data here.
FILES = ("theme_pool.csv", "theme_recommendation_history.csv", "theme_content_candidates.csv")


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


def exact_content_coverage(rows, run, themes, per_theme=10):
    """Count saved title rows, rejecting blank or normalized duplicate titles."""
    selected = [row for row in rows if row.get("run_id") == run]
    keys = {row.get("theme_id", "").strip() for row in themes}
    groups = {key: [row for row in selected if row.get("theme_key") == key] for key in keys}
    counts, exact, invalid = [], 0, 0
    for items in groups.values():
        titles = [re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", str(row.get("title", ""))).lower())
                  for row in items]
        unique = {title for title in titles if title}
        counts.append(len(items))
        invalid += len(items) - len(unique)
        exact += len(items) == per_theme and len(unique) == per_theme
    return {
        **content_coverage(rows, run, themes),
        "themes_with_exact_unique_titles": exact,
        "duplicate_or_blank_titles": invalid,
        "unexpected_theme_rows": sum(row.get("theme_key") not in keys for row in selected),
        "minimum_titles_per_theme": min(counts, default=0),
        "maximum_titles_per_theme": max(counts, default=0),
    }


def rows_unchanged(before, after):
    """Compare all cells and duplicate multiplicities while tolerating sorting."""
    return Counter(tuple(sorted(row.items())) for row in before) == Counter(tuple(sorted(row.items())) for row in after)


def latest_weekly_run(rows):
    """Find the current weekly run using the app's saved timestamps and ranks."""
    groups = {}
    for row in rows:
        run = row.get("run_id", "").strip()
        if run:
            groups.setdefault(run, []).append(row)

    def timestamp(row):
        values = [row.get("recommended_at", ""), row.get("run_id", "")[:15], row.get("recommended_date", "")]
        for value in values:
            try:
                parsed = datetime.strptime(value, "%Y%m%d-%H%M%S") if re.fullmatch(r"\d{8}-\d{6}", value) else datetime.fromisoformat(value.replace("Z", "+00:00"))
                return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("Asia/Seoul"))).astimezone(timezone.utc)
            except (TypeError, ValueError):
                continue
        return datetime.min.replace(tzinfo=timezone.utc)

    ordinary = [(run, group) for run, group in groups.items()
                if not run.upper().startswith("AI-DIRECTOR-") and not any(
                    any(str(row.get(field, "")).strip().upper() == "AI_DIRECTOR"
                        for field in ("recommendation_source", "source_status", "creation_origin"))
                    or any(str(row.get(field, "")).strip() for field in ("director_draft_id", "director_adoption_key"))
                    for row in group)]
    if not ordinary:
        return None, []
    run, group = max(ordinary, key=lambda item: max(timestamp(row) for row in item[1]))
    try:
        return run, sorted(group, key=lambda row: int(row.get("rank") or 0))
    except (TypeError, ValueError):
        raise VerificationIssue("weekly_saved_run_invalid") from None


def _names(frame):
    return [name.strip() for name in frame.locator(".theme-editorial-card .theme-editorial-title").all_text_contents()]


def refresh_existing_contents(page, frame, args, report, *, read_data=read_main):
    """Click only content refresh once, then observe persistence without reloading."""
    frame.locator(".theme-editorial-card").first.wait_for(state="visible", timeout=30000)
    before = read_data()
    run, themes = latest_weekly_run(before[FILES[1]])
    keys = {row.get("theme_id", "").strip() for row in themes}
    library_keys = {row.get("theme_id", "").strip() for row in before[FILES[0]]}
    if (not run or len(themes) != args.expected_count or len(keys) != args.expected_count
            or "" in keys or not keys.issubset(library_keys)
            or _names(frame) != [row.get("theme_name", "").strip() for row in themes]):
        raise VerificationIssue("weekly_existing_run_unconfirmed", blocked=True)
    button = frame.get_by_role("button", name="콘텐츠 새로고침", exact=True)
    button.wait_for(state="visible", timeout=30000)
    if not button.is_enabled():
        raise VerificationIssue("weekly_content_refresh_unavailable", blocked=True)
    original_selected = [row for row in before[FILES[2]] if row.get("run_id") == run]
    report["counts"] = {"existing_themes": len(themes), "prior_content_rows": len(original_selected)}
    # Record BEFORE click; a timeout may mean the server already accepted it.
    report["content_refresh_click_attempted"] = True
    button.click(timeout=30000)
    deadline = time.monotonic() + args.generation_timeout
    after = None
    coverage = {}
    while time.monotonic() < deadline:
        if has_app_exception(page):
            raise VerificationIssue("weekly_content_refresh_exception")
        if not frame.locator('[data-stale="true"], [data-testid="stSpinner"]').count():
            candidate = read_data()
            coverage = exact_content_coverage(candidate[FILES[2]], run, themes)
            selected = [row for row in candidate[FILES[2]] if row.get("run_id") == run]
            if (coverage["themes_with_exact_unique_titles"] == args.expected_count
                    and coverage["unexpected_theme_rows"] == 0
                    and not rows_unchanged(original_selected, selected)):
                after = candidate
                break
        page.wait_for_timeout(5000)
    if after is None:
        report["counts"].update(coverage)
        raise VerificationIssue("weekly_content_refresh_result_unconfirmed")
    report["counts"].update(coverage)
    report["counts"]["generated_themes"] = 0
    displayed = [value.strip() for value in frame.locator(".theme-editorial-card .theme-poster-head strong").all_text_contents()]
    report["counts"]["themes_displaying_ten_titles"] = sum(value == "10편" for value in displayed)
    report["tests"]["existing_run_refreshed"] = True
    report["tests"]["exactly_ten_unique_titles_per_theme"] = coverage["themes_with_exact_unique_titles"] == args.expected_count
    report["tests"]["theme_pool_unchanged"] = rows_unchanged(before[FILES[0]], after[FILES[0]])
    report["tests"]["recommendation_history_unchanged"] = rows_unchanged(before[FILES[1]], after[FILES[1]])
    other_contents = lambda data: [row for row in data[FILES[2]] if row.get("run_id") not in {run, "THEME_DB"}]
    report["tests"]["other_run_contents_unchanged"] = rows_unchanged(other_contents(before), other_contents(after))
    report["tests"]["displayed_themes_unchanged"] = _names(frame) == [row.get("theme_name", "").strip() for row in themes]
    report["tests"]["ten_titles_displayed_per_theme"] = len(displayed) == args.expected_count and all(value == "10편" for value in displayed)
    report["tests"]["no_generation_click"] = not report.get("generation_click_attempted", False)
    report["tests"]["no_exception"] = not has_app_exception(page)
    if not all(report["tests"].values()):
        raise VerificationIssue("weekly_content_refresh_validation_failed")


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
    if getattr(args, "content_refresh_only", False):
        refresh_existing_contents(page, frame, args, report, read_data=read_data)
        return
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
    parser.add_argument("--content-refresh-only", action="store_true",
                        help="Refresh the latest existing weekly run once; never create new themes.")
    parser.add_argument("--deployment-timeout", type=int, default=480)
    parser.add_argument("--generation-timeout", type=int, default=360)
    parser.add_argument("--executable", default=os.getenv("WEEKLY_CHROMIUM_PATH", ""))
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/weekly_live_validation.json")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "failed", "expected_version": args.expected_version,
              "observed_version": None, "started_at": datetime.now(timezone.utc).isoformat(),
              "generation_click_attempted": False, "content_refresh_click_attempted": False,
              "content_refresh_only": args.content_refresh_only, "tests": {}, "counts": {}, "errors": [],
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
    print(json.dumps({"status": report["status"], "generation_click_attempted": report["generation_click_attempted"],
                      "content_refresh_click_attempted": report["content_refresh_click_attempted"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
