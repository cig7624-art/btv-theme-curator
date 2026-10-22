"""Verify the public deployed release through its real Streamlit browser UI."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
APP_URL = "https://btv-theme-curator-moec8jpeyjkp9skzhefihq.streamlit.app/"
CHECKS = ("version", "ott_magazine", "ott_feature", "no_exception", "no_horizontal_overflow", "lead_artwork")


def normalized_version(value):
    value = str(value or "").strip()
    match = re.fullmatch(r"v?((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
                         r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
                         r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)", value)
    if not match or any(part.isdigit() and len(part) > 1 and part.startswith("0")
                        for part in (match.group(2) or "").split(".")):
        return None
    return match.group(1)


def exact_version(observed, expected):
    version = normalized_version(observed)
    return version is not None and version == normalized_version(expected)


def access_problem(url, status=None, private_message=False):
    """An anonymous auth-cookie redirect alone is not a required login."""
    parsed = urlsplit(url)
    if status in {401, 403}:
        return "http_access_denied"
    if private_message or parsed.hostname == "accounts.google.com":
        return "authentication_required"
    if parsed.hostname in {"share.streamlit.io", "streamlit.io"} and re.search(
            r"/(?:account/)?(?:login|signin|sign-in)(?:/|$)", parsed.path):
        return "authentication_required"
    return None


def no_horizontal_overflow(dimensions):
    return (dimensions["document_scroll"] <= dimensions["viewport"] + 1
            and dimensions["main_scroll"] <= dimensions["main_width"] + 1)


class VerificationIssue(RuntimeError):
    def __init__(self, code, *, blocked=False, retry=False):
        self.code, self.blocked, self.retry = code, blocked, retry
        super().__init__(code)


def _access_check(page, status=None):
    # Inspect only known access messages. Never persist HTML, auth URLs, cookies,
    # account information, exception text, or request/response headers.
    body = page.locator("body").inner_text(timeout=5000).lower()
    private = any(message in body for message in (
        "this app is private", "you do not have access to this app",
        "sign in to view this app", "log in to view this app"))
    problem = access_problem(page.url, status, private)
    if problem:
        raise VerificationIssue(problem, blocked=True)


def _load_version(page, args, report, label):
    response = page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
    _access_check(page, response.status if response else None)
    wake = page.get_by_role("button", name=re.compile(r"^(?:yes, get this app back up!?|wake up)$", re.I))
    if wake.count():
        wake.first.click()
    try:
        page.locator(".magazine-footer[data-version]").wait_for(state="attached", timeout=20000)
    except Exception:
        _access_check(page)
        if page.locator('[data-testid="stException"]').count():
            raise VerificationIssue("startup_exception", retry=True)
        raise VerificationIssue("release_marker_not_ready", retry=True)
    observed = normalized_version(page.locator(".magazine-footer[data-version]").last.get_attribute("data-version"))
    report["observed_version"] = observed
    report["tests"][f"{label}_version"] = exact_version(observed, args.expected_version)
    if not report["tests"][f"{label}_version"]:
        raise VerificationIssue("expected_release_not_ready", retry=True)


def _check_editorial(page, report, label, screenshots):
    page.get_by_role("tab", name="OTT는 지금", exact=True).click(timeout=15000)
    try:
        page.locator(".ott-magazine").first.wait_for(state="visible", timeout=20000)
        report["tests"][f"{label}_ott_magazine"] = True
    except Exception:
        raise VerificationIssue("ott_magazine_missing")
    try:
        page.locator(".ott-magazine .ott-feature").first.wait_for(state="visible", timeout=15000)
        report["tests"][f"{label}_ott_feature"] = True
    except Exception:
        raise VerificationIssue("ott_feature_missing")
    report["tests"][f"{label}_no_exception"] = not page.locator('[data-testid="stException"]').count()
    if not report["tests"][f"{label}_no_exception"]:
        raise VerificationIssue("app_exception")
    dimensions = page.evaluate("""() => {
      const main = document.querySelector('[data-testid="stMain"]');
      return {viewport: innerWidth, document_scroll: document.documentElement.scrollWidth,
              main_width: main ? main.clientWidth : innerWidth,
              main_scroll: main ? main.scrollWidth : document.documentElement.scrollWidth};
    }""")
    report["tests"][f"{label}_no_horizontal_overflow"] = no_horizontal_overflow(dimensions)
    if not report["tests"][f"{label}_no_horizontal_overflow"]:
        raise VerificationIssue("horizontal_overflow")
    loaded = page.evaluate(r"""async () => {
      const artworks = Array.from(document.querySelectorAll('.ott-magazine .ott-feature-art')).slice(0, 6);
      return await Promise.all(artworks.map(artwork => new Promise(resolve => {
        const element = artwork.querySelector('.ott-feature-image') || artwork.querySelector('img');
        const background = element ? getComputedStyle(element).backgroundImage : '';
        const match = background.match(/^url\((["']?)(.*?)\1\)$/);
        const source = element && element.tagName === 'IMG' ? (element.currentSrc || element.src)
          : (match ? match[2] : '');
        if (!source) { resolve(false); return; }
        const image = new Image();
        let complete = false;
        const finish = success => {
          if (complete) return;
          complete = true; clearTimeout(timer); resolve(success);
        };
        const timer = setTimeout(() => finish(false), 8000);
        image.onload = () => finish(image.naturalWidth > 0);
        image.onerror = () => finish(false);
        image.src = source;
        if (image.complete && image.naturalWidth > 0) finish(true);
      })));
    }""")
    report["artwork"][label] = {"checked": len(loaded), "loaded": sum(loaded),
                                 "unavailable": len(loaded) - sum(loaded),
                                 "lead_loaded": bool(loaded and loaded[0])}
    report["tests"][f"{label}_lead_artwork"] = report["artwork"][label]["lead_loaded"]
    if not report["tests"][f"{label}_lead_artwork"]:
        raise VerificationIssue("lead_artwork_unavailable")
    if not all(loaded):
        report["warnings"].append(f"{label}_some_artwork_unavailable")
    screenshots.mkdir(parents=True, exist_ok=True)
    page.locator(".ott-magazine").first.scroll_into_view_if_needed()
    page.screenshot(path=str(screenshots / f"{label}.png"), full_page=False)


def verify(args):
    report = {"schema_version": 1, "status": "failed", "expected_version": args.expected_version,
              "observed_version": None, "started_at": datetime.now(timezone.utc).isoformat(),
              "finished_at": None, "attempts": 0, "tests": {}, "errors": [], "warnings": [], "artwork": {}}
    source_commit = os.environ.get("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", source_commit):
        report["source_commit"] = source_commit
    deadline = time.monotonic() + args.timeout
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                while True:
                    if report["attempts"] and time.monotonic() >= deadline:
                        break
                    report["attempts"] += 1
                    report["tests"] = {f"{label}_{check}": False for label in ("desktop", "mobile") for check in CHECKS}
                    report["errors"] = []
                    report["warnings"] = []
                    report["artwork"] = {}
                    issue = None
                    try:
                        for label, width, height in (("desktop", 1366, 1000), ("mobile", 390, 844)):
                            context = browser.new_context(viewport={"width": width, "height": height},
                                                          is_mobile=label == "mobile", has_touch=label == "mobile")
                            try:
                                page = context.new_page()
                                _load_version(page, args, report, label)
                                _check_editorial(page, report, label, args.screenshots_dir)
                            finally:
                                context.close()
                        report["status"] = "passed"
                        break
                    except VerificationIssue as error:
                        issue = error
                    except Exception as error:
                        # Match known network failures internally; never serialize
                        # Playwright exception messages, which may contain URLs.
                        blocked = any(code in str(error) for code in (
                            "ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED",
                            "ERR_CERT_AUTHORITY_INVALID", "ERR_ACCESS_DENIED"))
                        issue = VerificationIssue("network_access_blocked" if blocked else "browser_navigation_unavailable",
                                                  blocked=blocked, retry=not blocked)
                    report["errors"] = [issue.code]
                    if issue.blocked:
                        report["status"] = "blocked"
                        break
                    if not issue.retry or time.monotonic() >= deadline:
                        break
                    time.sleep(min(args.poll, max(0, deadline - time.monotonic())))
            finally:
                browser.close()
    except Exception:
        report["errors"] = ["browser_start_failed"]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version")
    parser.add_argument("--timeout", type=float, default=480)
    parser.add_argument("--poll", type=float, default=30)
    parser.add_argument("--executable", help="Optional installed Chromium executable")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/deployment_validation.json")
    parser.add_argument("--screenshots-dir", type=Path, default=ROOT / "artifacts/deployment-screenshots")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not 0 < args.poll <= 60:
        parser.error("timeout must be positive; poll must be between 0 and 60 seconds")
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text(encoding="utf-8"))
    parsed = urlsplit(args.url)
    if not args.expected_version or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        parser.error("use an exact release version and a public HTTP(S) URL without credentials")
    report = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
