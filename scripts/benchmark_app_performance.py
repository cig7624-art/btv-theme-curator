"""Measure ordinary public-app viewing with completed Streamlit reruns.

Only navigation, local quick search, and the library source filter are used.
No generation, refresh, editing, cost popovers, or data-writing controls are used.
Websocket payloads, DOM text, screenshots, URLs, and prompts are never persisted.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import time
from urllib.parse import urlsplit

from verify_deployment import (
    APP_URL, ROOT, VerificationIssue, _load_version, has_app_exception,
    normalized_version, resolve_app_frame,
)


PROTOCOL = "streamlit-full-rerun-v1"
VIEWPORTS = (("desktop", 1366, 1000), ("mobile", 390, 844))
TABS = (
    ("recommendation", "추천 테마", ".st-key-curation_section"),
    ("director", "AI 디렉터", ".director-heading"),
    ("ott", "OTT는 지금", ".ott-magazine"),
    ("media", "미디어 인사이트", ".media-insights-heading"),
)
SEARCHES = (("horror", "공포"), ("summer_night", "여름밤"), ("healing", "힐링"))
FLOW_ORDER = [f"tab_{key}" for key, _, _ in TABS] + [
    "library_open", *[f"library_search_{key}" for key, _ in SEARCHES],
    "library_reset", "library_source_director",
]


class BenchmarkIssue(RuntimeError):
    """A fixed, safe error code; never serialize browser exception messages."""

    def __init__(self, code, diagnostics=None):
        super().__init__(code)
        self.diagnostics = diagnostics


class RerunTracker:
    def __init__(self, page):
        from streamlit.proto.ForwardMsg_pb2 import ForwardMsg

        self.message_type = ForwardMsg
        self.runs = self.successes = self.messages = self.bytes = self.decode_errors = 0
        self.finished_run = -1
        self.last_success = self.last_control = 0.0
        self.running = False
        self.compile_error = False
        page.on("websocket", self._socket)

    def _socket(self, socket):
        if urlsplit(socket.url).path.rstrip("/").endswith("/_stcore/stream"):
            socket.on("framereceived", self._received)

    def _received(self, payload):
        if not isinstance(payload, bytes):
            return
        self.messages += 1
        self.bytes += len(payload)
        try:
            message = self.message_type()
            message.ParseFromString(payload)
            kind = message.WhichOneof("type")
            now = time.monotonic()
            if kind == "new_session":
                self.runs += 1
                self.running = True
                self.last_control = now
            elif kind == "session_status_changed":
                self.running = message.session_status_changed.script_is_running
                self.last_control = now
            elif kind == "script_finished":
                self.last_control = now
                # Check the oneof: FINISHED_SUCCESSFULLY is enum value zero.
                if message.script_finished == 0:
                    self.successes += 1
                    self.finished_run = self.runs
                    self.last_success = now
                elif message.script_finished == 1:
                    self.compile_error = True
                # 2 is FINISHED_EARLY_FOR_RERUN; 3 is a fragment completion.
                # Neither is a completed full-app measurement.
        except Exception:
            self.decode_errors += 1

    def checkpoint(self):
        return (self.runs, self.successes, self.messages, self.bytes)


# Inspect only the active target and retain a numeric/widget-state signature
# in memory. Hidden tabs may intentionally retain stale DOM after a lazy rerun.
DOM_STATE = r"""({selector, tab, query, source}) => {
  const visible = el => !!el && el.getClientRects().length > 0
    && getComputedStyle(el).visibility !== 'hidden';
  const markers = Array.from(document.querySelectorAll(selector));
  const counts = {markers: markers.length, visible_markers: markers.filter(visible).length};
  const fail = reason => ({ready: false, reason, counts});
  const marker = markers.find(visible);
  if (!marker) return fail('marker_missing');
  if (marker.closest('[data-stale="true"]')) return fail('marker_stale');
  if (tab && !Array.from(document.querySelectorAll('[role="tab"]')).some(
      el => el.textContent.trim() === tab && el.getAttribute('aria-selected') === 'true')) return fail('tab_not_selected');
  const scope = marker.closest('[role="tabpanel"]') ||
    (query !== null ? document.querySelector('[data-testid="stMain"]') : marker);
  if (!scope) return fail('scope_missing');
  counts.stale_nodes = Array.from(scope.querySelectorAll('[data-stale="true"]')).filter(visible).length;
  if (counts.stale_nodes) return fail('target_stale');
  const search = document.querySelector('.st-key-theme_quick_search_input input');
  const filter = document.querySelector('.st-key-theme_library_source_filter');
  if (query !== null && (!search || search.value !== query)) return fail('query_not_applied');
  // Streamlit's selected label can be an input value rather than a text node.
  const sourceValue = filter ? [filter.textContent, ...Array.from(
    filter.querySelectorAll('[role="combobox"],input,select'))
      .map(el => [el.value || '', el.textContent || ''].join(' '))].join(' ') : '';
  if (source && !sourceValue.includes(source)) return fail('source_not_applied');
  const cards = Array.from(scope.querySelectorAll('.theme-db-main'));
  counts.cards = cards.length;
  if (query !== null) {
    const result = Array.from(scope.querySelectorAll('h3')).map(el => el.textContent)
      .find(text => /빠른 검색 결과 \d+개|전체 \d+개 중 \d+개 표시/.test(text));
    if (!result) return fail('result_count_missing');
    const numbers = result.match(/\d+/g).map(Number);
    const pageMeta = scope.querySelector('.library-page-meta[data-visible-count]');
    const expectedCards = pageMeta ? Number(pageMeta.dataset.visibleCount) : numbers[numbers.length - 1];
    counts.expected_cards = Number.isInteger(expectedCards) ? expectedCards : -1;
    if (!Number.isInteger(expectedCards) || expectedCards < 0 || cards.length !== expectedCards) return fail('card_count_mismatch');
    if (source === 'AI 디렉터' && cards.some(card =>
      !card.querySelector('.small')?.textContent.includes('[AI 디렉터]'))) return fail('source_badge_mismatch');
  }
  return {ready: true, reason: 'ready', counts, signature: JSON.stringify({nodes: scope.querySelectorAll('*').length,
    textLength: scope.textContent.length, cards: cards.length,
    widgets: Array.from(scope.querySelectorAll('input,button,[role="combobox"],[role="tab"]'))
      .filter(visible).map(el => [el.tagName, !!el.disabled, el.value || '',
        el.getAttribute('aria-selected'), el.getAttribute('aria-expanded')])})};
}"""


class Session:
    def __init__(self, page, tracker, deadline):
        self.page, self.tracker, self.deadline = page, tracker, deadline

    def action(self, callback, *, selector, tab=None, query=None, source=None):
        tracker = self.tracker
        before = tracker.checkpoint()
        started = time.monotonic()
        callback()
        return self.wait(before, started, selector=selector, tab=tab, query=query, source=source)

    def wait(self, before, started, **target):
        stable_signature = None
        stable_since = 0.0
        diagnostics = {"stage": "script_completion", "reason": "full_rerun_pending"}
        end = min(self.deadline, started + 40)
        while time.monotonic() < end:
            self.page.wait_for_timeout(50)
            frame = resolve_app_frame(self.page)
            if not frame:
                diagnostics = {"stage": "frame_resolution", "reason": "app_frame_missing"}
                continue
            if has_app_exception(self.page) or self.tracker.compile_error:
                raise BenchmarkIssue("app_exception")
            tracker = self.tracker
            finished = (tracker.runs > before[0] and tracker.successes > before[1]
                        and tracker.finished_run == tracker.runs and not tracker.running)
            if not finished:
                diagnostics = {"stage": "script_completion", "reason": "full_rerun_pending"}
                stable_signature = None
                continue
            try:
                state = frame.evaluate(DOM_STATE, target)
                signature = state.get("signature") if state.get("ready") else None
                diagnostics = {"stage": "target_readiness", "reason": state["reason"], **state["counts"]}
            except Exception:
                signature = None
                diagnostics = {"stage": "target_readiness", "reason": "dom_evaluation_unavailable"}
            now = time.monotonic()
            if signature and signature == stable_signature:
                if now - stable_since >= .25 and now - tracker.last_control >= .25:
                    return {
                        "completion_ms": round((tracker.last_success - started) * 1000, 1),
                        "ready_ms": round((now - started) * 1000, 1),
                        "reruns": tracker.runs - before[0],
                        "successful_reruns": tracker.successes - before[1],
                        "forward_messages": tracker.messages - before[2],
                        "forward_bytes": tracker.bytes - before[3],
                        "rendered_cards": json.loads(signature)["cards"],
                    }
            else:
                stable_signature, stable_since = signature, now
        diagnostics.update({"reruns": self.tracker.runs - before[0],
                            "successful_reruns": self.tracker.successes - before[1],
                            "forward_messages": self.tracker.messages - before[2],
                            "running": int(self.tracker.running),
                            "decode_errors": self.tracker.decode_errors})
        raise BenchmarkIssue("completed_rerun_or_target_timeout", diagnostics)


def tab_action(session, name, selector):
    return session.action(
        lambda: resolve_app_frame(session.page).get_by_role("tab", name=name, exact=True).click(),
        selector=selector, tab=name,
    )


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["viewport"], row["flow"])].append(row)
    return [{
        "viewport": viewport, "flow": flow, "samples": len(values),
        "median_completion_ms": round(statistics.median(x["completion_ms"] for x in values), 1),
        "median_ready_ms": round(statistics.median(x["ready_ms"] for x in values), 1),
        "min_completion_ms": min(x["completion_ms"] for x in values),
        "max_completion_ms": max(x["completion_ms"] for x in values),
    } for (viewport, flow), values in groups.items()]


def benchmark(args):
    report = {
        "schema_version": 1, "protocol": PROTOCOL, "label": args.label,
        "status": "failed", "expected_version": args.expected_version,
        "observed_version": None, "started_at": datetime.now(timezone.utc).isoformat(),
        "samples_per_flow": args.samples, "flow_order": FLOW_ORDER,
        "viewports": [{"name": name, "width": width, "height": height}
                      for name, width, height in VIEWPORTS],
        "measurements": [], "summary": [], "errors": [],
    }
    deadline = time.monotonic() + args.timeout
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                for viewport, width, height in VIEWPORTS:
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  is_mobile=viewport == "mobile", has_touch=viewport == "mobile")
                    try:
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        tracker = RerunTracker(page)
                        session = Session(page, tracker, deadline)
                        initial = tracker.checkpoint()
                        started = time.monotonic()
                        version_report = {"tests": {}}
                        _load_version(page, args, version_report, viewport)
                        report["observed_version"] = version_report["observed_version"]
                        session.wait(initial, started, selector=".magazine-hero", tab="지금 주목할 이야기",
                                     query=None, source=None)

                        def record(flow, values, sample):
                            report["measurements"].append({"viewport": viewport, "sample": sample,
                                                           "flow": flow, **values})

                        for sample in range(1, args.samples + 1):
                            for key, name, selector in TABS:
                                record(f"tab_{key}", tab_action(session, name, selector), sample)
                            tab_action(session, "추천 테마", ".st-key-curation_section")
                            record("library_open", session.action(
                                lambda: resolve_app_frame(page).get_by_role("button", name="테마 라이브러리", exact=True).click(),
                                selector=".st-key-theme_quick_search_input", query="", source="전체"), sample)
                            for key, query in SEARCHES:
                                def search(value=query):
                                    box = resolve_app_frame(page).locator(".st-key-theme_quick_search_input input")
                                    box.fill(value)
                                    box.press("Enter")
                                record(f"library_search_{key}", session.action(search,
                                       selector=".st-key-theme_quick_search_input", query=query, source="전체"), sample)
                            record("library_reset", session.action(
                                lambda: resolve_app_frame(page).get_by_role("button", name="검색 초기화", exact=True).click(),
                                selector=".st-key-theme_quick_search_input", query="", source="전체"), sample)

                            def source_filter(value):
                                frame = resolve_app_frame(page)
                                frame.locator(".st-key-theme_library_source_filter").get_by_role("combobox").click()
                                frame.get_by_role("option", name=value, exact=True).click()

                            record("library_source_director", session.action(lambda: source_filter("AI 디렉터"),
                                   selector=".st-key-theme_quick_search_input", query="", source="AI 디렉터"), sample)
                            # Restore the same starting state for the next sample.
                            session.action(lambda: source_filter("전체"),
                                           selector=".st-key-theme_quick_search_input", query="", source="전체")
                            session.action(
                                lambda: resolve_app_frame(page).get_by_role("button", name="← 추천 화면으로 돌아가기", exact=True).click(),
                                selector=".st-key-curation_section", tab="추천 테마")
                            tab_action(session, "지금 주목할 이야기", ".magazine-hero")
                        if tracker.decode_errors:
                            raise BenchmarkIssue("websocket_decode_error")
                    finally:
                        context.close()
                report["status"] = "passed"
            finally:
                browser.close()
    except VerificationIssue as error:
        report["errors"] = [error.code]
        if error.blocked:
            report["status"] = "blocked"
    except BenchmarkIssue as error:
        report["errors"] = [str(error)]
        if error.diagnostics:
            report["timeout_diagnostics"] = error.diagnostics
    except Exception:
        report["errors"] = ["browser_benchmark_unavailable"]
    report["summary"] = summarize(report["measurements"])
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    if args.label == "optimized" and report["status"] == "passed":
        try:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            compatible = all(baseline.get(key) == report[key]
                             for key in ("protocol", "samples_per_flow", "flow_order", "viewports"))
            if not compatible or baseline.get("status") != "passed":
                raise ValueError("incompatible_baseline")
            previous = {(row["viewport"], row["flow"]): row for row in baseline["summary"]}
            report["baseline_version"] = baseline["observed_version"]
            report["comparison"] = [{
                "viewport": row["viewport"], "flow": row["flow"],
                "baseline_median_ms": previous[(row["viewport"], row["flow"])]["median_completion_ms"],
                "optimized_median_ms": row["median_completion_ms"],
                "change_percent": round(100 * (row["median_completion_ms"] /
                    previous[(row["viewport"], row["flow"])]["median_completion_ms"] - 1), 1),
            } for row in report["summary"]]
        except (OSError, KeyError, ValueError, TypeError, ZeroDivisionError):
            report["errors"] = ["baseline_comparison_unavailable"]
            report["status"] = "failed"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--label", choices=("baseline", "optimized"), default="baseline")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=480)
    parser.add_argument("--executable", default=os.environ.get("PERFORMANCE_CHROMIUM_PATH"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, default=ROOT / "artifacts/performance_baseline.json")
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text(encoding="utf-8"))
    parsed = urlsplit(args.url)
    if (not args.expected_version or args.samples != 3 or not 0 < args.timeout <= 480
            or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password):
        parser.error("use an exact version, public HTTPS URL, three samples, and a timeout up to 480 seconds")
    args.output = args.output or ROOT / f"artifacts/performance_{args.label}.json"
    report = benchmark(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    # The report contains only fixed identifiers, versions, counts and timings.
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
