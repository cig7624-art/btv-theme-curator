"""Measure lazy-tab layout across completed Streamlit reruns.

Only selects existing tabs. Reports numeric geometry and rerun timings; no
content, network payloads, screenshots, prompts, or API credentials are saved.
Run against a local offline fixture to keep the check independent of APIs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

if __package__:
    from .benchmark_app_performance import BenchmarkIssue, RerunTracker, Session
    from .verify_deployment import ROOT, VerificationIssue, normalized_version, resolve_app_frame
    from .verify_theme_rail_live import load_release
else:
    from benchmark_app_performance import BenchmarkIssue, RerunTracker, Session
    from verify_deployment import ROOT, VerificationIssue, normalized_version, resolve_app_frame
    from verify_theme_rail_live import load_release


VIEWPORTS = (("desktop", 1366, 1000), ("mobile", 390, 844))
TABS = (
    ("recommendation", "추천 테마", ".st-key-curation_section"),
    ("director", "AI 디렉터", ".director-heading"),
    ("ott", "OTT는 지금", ".ott-magazine"),
    ("media", "미디어 인사이트", ".media-insights-heading"),
    ("story", "지금 주목할 이야기", ".magazine-hero"),
)

START_SAMPLING = r"""() => {
  cancelAnimationFrame(window.__tabStabilityFrame);
  const samples = [];
  window.__tabStabilitySamples = samples;
  function sample() {
    const main = document.querySelector('[data-testid="stMain"]');
    const shell = document.querySelector('.st-key-home_sections');
    const list = shell?.querySelector('[role="tablist"]');
    const footer = document.querySelector('.magazine-footer');
    if (main && shell && list) {
      const header = list.getBoundingClientRect();
      samples.push({scroll: main.scrollTop, x: header.x, y: header.y,
        shell_height: shell.getBoundingClientRect().height,
        footer_y: footer?.getBoundingClientRect().y ?? null,
        viewport_height: main.clientHeight});
    }
    window.__tabStabilityFrame = requestAnimationFrame(sample);
  }
  sample();
}"""
STOP_SAMPLING = r"""() => {
  cancelAnimationFrame(window.__tabStabilityFrame);
  return window.__tabStabilitySamples;
}"""


def summarize_geometry(samples):
    """Measure visible-shell movement while allowing content to grow normally."""
    if not samples:
        raise BenchmarkIssue("no_layout_samples")

    def spread(key):
        values = [sample[key] for sample in samples]
        return round(max(values) - min(values), 2)

    return {
        "layout_frames": len(samples),
        "scroll_drift_px": spread("scroll"),
        "header_x_drift_px": spread("x"),
        "header_y_drift_px": spread("y"),
        "minimum_shell_height_px": round(min(sample["shell_height"] for sample in samples), 2),
        "collapsed_shell_frames": sum(sample["shell_height"] + 1 < sample["viewport_height"]
                                       for sample in samples),
        "footer_intrusion_frames": sum(sample["footer_y"] is not None
                                       and sample["footer_y"] < sample["viewport_height"]
                                       for sample in samples),
    }


def verify(args):
    report = {"protocol": "streamlit-lazy-tab-layout-v1", "status": "failed",
              "expected_version": args.expected_version, "observed_versions": [],
              "started_at": datetime.now(timezone.utc).isoformat(), "measurements": [], "tests": {}, "errors": []}
    deadline = time.monotonic() + args.timeout
    stage = "browser_launch"
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                for viewport, width, height in VIEWPORTS:
                    stage = "browser_context"
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  is_mobile=viewport == "mobile", has_touch=viewport == "mobile")
                    try:
                        page = context.new_page()
                        stage = "tracker_initialization"
                        # Subscribe before navigation: attaching after version
                        # readiness misses the already-open Streamlit socket.
                        tracker = RerunTracker(page)
                        session = Session(page, tracker, deadline)
                        before = tracker.checkpoint()
                        stage = "release_loading"
                        load_release(page, args, report, viewport)
                        stage = "initial_rerun"
                        # Cloud wake/restart may take longer than Session's
                        # 40-second action window. Initial timing is discarded;
                        # start its readiness window after release loading.
                        session.wait(before, time.monotonic(), selector=".magazine-hero", tab="지금 주목할 이야기",
                                     query=None, source=None)
                        observed = resolve_app_frame(page).locator(".magazine-footer").get_attribute("data-version")
                        report["observed_versions"].append({"viewport": viewport, "version": observed})
                        if args.expected_version and normalized_version(observed or "") != normalized_version(args.expected_version):
                            raise BenchmarkIssue("unexpected_app_version")
                        # A visible tab strip can be clicked at either scroll
                        # position; 50px also catches browser scroll clamping
                        # when an empty lazy panel used to collapse the page.
                        for scroll in (0, 50):
                            stage = "layout_sampling"
                            frame = resolve_app_frame(page)
                            frame.locator('[data-testid="stMain"]').evaluate(
                                "(main, y) => {main.scrollTop = y}", scroll)
                            for key, label, selector in TABS:
                                frame = resolve_app_frame(page)
                                frame.evaluate(START_SAMPLING)
                                stage = "tab_switch"
                                timing = session.action(
                                    lambda: resolve_app_frame(page).get_by_role("tab", name=label, exact=True).click(),
                                    selector=selector, tab=label)
                                stage = "geometry_summary"
                                geometry = summarize_geometry(resolve_app_frame(page).evaluate(STOP_SAMPLING))
                                report["measurements"].append({"viewport": viewport, "requested_scroll_px": scroll,
                                                               "tab": key, **geometry, **timing})
                    finally:
                        context.close()
            finally:
                browser.close()
        failures = [row for row in report["measurements"]
                    if max(row["scroll_drift_px"], row["header_x_drift_px"], row["header_y_drift_px"]) > 1
                    or row["collapsed_shell_frames"] or row["footer_intrusion_frames"]]
        report["status"] = "failed" if failures else "passed"
        report["unstable_switches"] = len(failures)
    except VerificationIssue as error:
        report["errors"].append(error.code)
        report["failure_stage"] = stage
        if error.blocked:
            report["status"] = "blocked"
    except BenchmarkIssue as error:
        report["errors"].append(str(error))
        report["failure_stage"] = stage
        if error.diagnostics:
            report["timeout_diagnostics"] = error.diagnostics
    except ImportError:
        report["errors"].append("tracker_dependency_unavailable" if stage == "tracker_initialization"
                                else "browser_dependency_unavailable")
        report["failure_stage"] = stage
    except Exception:
        report["errors"].append({
            "browser_launch": "browser_launch_unavailable",
            "browser_context": "browser_context_unavailable",
            "tracker_initialization": "tracker_initialization_failed",
            "release_loading": "release_loading_failed",
            "initial_rerun": "initial_rerun_unavailable",
            "layout_sampling": "layout_sampling_failed",
            "tab_switch": "tab_switch_unavailable",
            "geometry_summary": "geometry_summary_failed",
        }[stage])
        report["failure_stage"] = stage
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--executable")
    parser.add_argument("--expected-version")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text(encoding="utf-8"))
    if not args.expected_version or args.timeout <= 0:
        parser.error("use an exact release version and a positive timeout")
    report = verify(args)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
