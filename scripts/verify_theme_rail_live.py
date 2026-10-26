"""Check public theme rails with real mouse/touch input and browser image loads.

Only navigation and existing poster links are used. External link navigation is
intercepted before loading. Reports contain counts and booleans, never titles,
URLs, page text, screenshots, prompts, credentials, or response payloads.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urldefrag, urlsplit

if __package__:
    from .verify_deployment import (
        APP_URL, ROOT, VerificationIssue, _load_version, has_app_exception,
        no_horizontal_overflow, normalized_version,
    )
else:
    from verify_deployment import (
        APP_URL, ROOT, VerificationIssue, _load_version, has_app_exception,
        no_horizontal_overflow, normalized_version,
    )


VIEWPORTS = (("desktop", 1366, 1000), ("mobile", 390, 844))
RAILS = ".st-key-curation_section .theme-carousel"

STRUCTURE = r"""roots => roots.map((root, index) => {
  const rail = root.querySelector('.theme-poster-rail');
  const list = root.querySelector('.theme-candidate-list');
  const declared = Number(root.querySelector('.theme-poster-head strong')?.textContent.match(/\d+/)?.[0]);
  return {theme: index + 1, declared_titles: declared,
    rendered_titles: rail?.children.length || 0,
    expanded_list_titles: list ? list.children.length : null,
    linked_titles: rail?.querySelectorAll('a.theme-poster-tile[href]').length || 0,
    overflowing: !!rail && rail.scrollWidth > rail.clientWidth + 2,
    controls_match: !!rail && !!rail.id
      && root.querySelector('.carousel-prev')?.getAttribute('aria-controls') === rail.id
      && root.querySelector('.carousel-next')?.getAttribute('aria-controls') === rail.id};
})"""

ARTWORK = r"""async roots => {
  const probes = new Map();
  const visibleFallback = tile => {
    const placeholder = tile.querySelector('.theme-poster-placeholder');
    if (!placeholder || !placeholder.textContent.trim()) return false;
    const style = getComputedStyle(placeholder), box = placeholder.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Number(style.opacity) > 0 && box.width > 0 && box.height > 0;
  };
  const probe = url => {
    if (!probes.has(url)) probes.set(url, new Promise(resolve => {
      const image = new Image(); let settled = false;
      const finish = result => {if (settled) return; settled = true; clearTimeout(timer); resolve(result);};
      const timer = setTimeout(() => finish('timed_out'), 8000);
      image.onload = async () => {
        try {await image.decode();} catch (_) {finish('failed'); return;}
        finish(image.naturalWidth > 0 && image.naturalHeight > 0 ? 'loaded' : 'failed');
      };
      image.onerror = () => finish('failed'); image.src = url;
    }));
    return probes.get(url);
  };
  // Create all probes together so the entire set, including offscreen cards,
  // shares the same eight-second load deadline rather than one per title.
  return await Promise.all(roots.map(async (root, index) => {
    const tiles = Array.from(root.querySelector('.theme-poster-rail').children);
    const results = await Promise.all(tiles.map(async tile => {
      const image = tile.querySelector('.theme-poster-image');
      const css = image ? getComputedStyle(image).backgroundImage : '';
      const match = css.match(/^url\((.*)\)$/);
      const url = match ? match[1].replace(/^["']|["']$/g, '') : '';
      return {result: url ? await probe(url) : 'missing_url', fallback: visibleFallback(tile)};
    }));
    const count = state => results.filter(item => item.result === state).length;
    return {theme: index + 1, total: results.length, poster_urls: results.length - count('missing_url'),
      loaded: count('loaded'), missing_url: count('missing_url'), failed: count('failed'),
      timed_out: count('timed_out'), missing: results.filter(item => item.result !== 'loaded').length,
      fallbacks_rendered: results.filter(item => item.fallback).length,
      missing_fallbacks_rendered: results.filter(item => item.result !== 'loaded' && item.fallback).length};
  }));
}"""

SETTLE = r"""rail => new Promise(resolve => {
  let previous = rail.scrollLeft, stable = 0, frames = 0;
  function next() {
    const current = rail.scrollLeft;
    stable = Math.abs(current - previous) < .5 ? stable + 1 : 0;
    previous = current;
    if (stable >= 5 || ++frames >= 120) resolve(current);
    else requestAnimationFrame(next);
  }
  requestAnimationFrame(next);
})"""


def require(condition, code):
    if not condition:
        raise VerificationIssue(code)


def load_release(page, args, report, label):
    """Allow an unpaid Cloud wake/restart before beginning any interaction."""
    deadline = time.monotonic() + 180
    class LoadingPage:
        # _load_version owns the Cloud wake/access/version checks. Cap its
        # navigation and polling at this check's overall startup deadline.
        def __getattr__(self, name):
            return getattr(page, name)

        def remaining_ms(self):
            remaining = (deadline - time.monotonic()) * 1000
            if remaining <= 0:
                raise VerificationIssue("release_startup_timeout")
            return remaining

        def goto(self, *positional, **keywords):
            keywords["timeout"] = min(keywords.get("timeout", 45000), self.remaining_ms())
            return page.goto(*positional, **keywords)

        def wait_for_timeout(self, milliseconds):
            page.wait_for_timeout(min(milliseconds, self.remaining_ms()))
            self.remaining_ms()

    loading_page = LoadingPage()
    attempts = 0
    while True:
        attempts += 1
        try:
            frame = _load_version(loading_page, args, report, label)
            report.setdefault("startup_attempts", {})[label] = attempts
            return frame
        except VerificationIssue as error:
            if error.blocked or not error.retry or time.monotonic() >= deadline:
                raise
        except Exception:
            if time.monotonic() >= deadline:
                raise VerificationIssue("release_navigation_not_ready")
        page.wait_for_timeout(min(2000, max(0, deadline - time.monotonic()) * 1000))


def reset_rail(rail):
    rail.evaluate("rail => {rail.scrollLeft = 0}")
    rail.evaluate(SETTLE)


def input_origin(rail):
    """Find a real visible poster anchor with room for a leftward drag."""
    rail.scroll_into_view_if_needed()
    bounds = rail.bounding_box()
    require(bool(bounds), "rail_not_visible")
    distance = min(160, bounds["width"] * .4)
    links = rail.locator("a.theme-poster-tile[href]")
    choices = []
    for index in range(links.count()):
        art = links.nth(index).locator(".theme-poster-art").bounding_box()
        if not art:
            continue
        x = art["x"] + art["width"] / 2
        if (x - distance >= bounds["x"] + 2
                and art["x"] + art["width"] <= bounds["x"] + bounds["width"] + 1):
            choices.append((x, art["y"] + art["height"] / 2))
    require(bool(choices) and distance >= 80, "visible_link_drag_origin_missing")
    return (*choices[-1], distance)


def mouse_drag(page, rail):
    x, y, distance = input_origin(rail)
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - distance, y, steps=12)
    page.mouse.up()
    return distance, rail.evaluate(SETTLE)


def touch_swipe(page, context, rail):
    x, y, distance = input_origin(rail)
    cdp = context.new_cdp_session(page)
    try:
        point = lambda current: [{"x": current, "y": y, "id": 1, "radiusX": 1, "radiusY": 1, "force": 1}]
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": point(x)})
        for step in range(1, 13):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": point(x - distance * step / 12)})
            page.wait_for_timeout(16)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        cdp.detach()
    return distance, rail.evaluate(SETTLE)


def check_viewport(page, context, args, report, label):
    frame = load_release(page, args, report, label)
    frame.get_by_role("tab", name="추천 테마", exact=True).click()
    frame.locator(RAILS).first.wait_for(state="visible", timeout=40000)
    frame.wait_for_function("""() => {
      const panel = document.querySelector('.st-key-curation_section');
      return panel && !panel.closest('[data-stale="true"]')
        && !panel.querySelector('[data-stale="true"]')
        && typeof window.__btvThemeCarouselCleanup === 'function'
        && Array.from(document.querySelectorAll('.theme-carousel .carousel-position'))
          .every(el => el.textContent.trim());
    }""", timeout=30000)
    roots = frame.locator(RAILS)
    structure = roots.evaluate_all(STRUCTURE)
    report.setdefault("themes", {})[label] = structure
    require(len(structure) >= 2, "insufficient_rails_for_independence")
    for theme in structure:
        require(theme["rendered_titles"] > 0 and theme["declared_titles"] == theme["rendered_titles"]
                and (theme["expanded_list_titles"] is None
                     or theme["expanded_list_titles"] == theme["rendered_titles"]), "stored_title_count_mismatch")
        require(not args.expected_titles or theme["rendered_titles"] == args.expected_titles,
                "expected_title_count_mismatch")
        require(theme["controls_match"] and theme["overflowing"], "rail_controls_or_overflow_missing")

    artwork = roots.evaluate_all(ARTWORK)
    report.setdefault("artwork", {})[label] = artwork
    missing = sum(item["missing"] for item in artwork)
    if missing:
        report["warnings"].append({"code": "poster_artwork_unavailable", "viewport": label, "missing": missing})
    require(all(item["fallbacks_rendered"] == item["total"]
                and item["missing_fallbacks_rendered"] == item["missing"] for item in artwork),
            "poster_fallback_missing")

    navigation_attempts = []
    # Install only after loading the app. Every following action is on a rail;
    # abort navigation attempts while retaining in-memory destination checks.
    def guard_navigation(route):
        if route.request.is_navigation_request():
            navigation_attempts.append(route.request.url)
            route.abort()
        else:
            route.continue_()
    context.route("**/*", guard_navigation)
    results = []
    report.setdefault("interactions", {})[label] = results
    for index in range(roots.count()):
        root = roots.nth(index)
        rail = root.locator(".theme-poster-rail")
        other = roots.nth((index + 1) % roots.count()).locator(".theme-poster-rail")
        reset_rail(rail)
        other_before = other.evaluate("rail => rail.scrollLeft")
        pages_before, url_before = len(context.pages), page.url
        navigation_attempts.clear()
        distance, moved = mouse_drag(page, rail)
        row = {"theme": index + 1, "mouse_drag_px": round(distance, 1),
               "mouse_scroll_px": round(moved, 1),
               "drag_did_not_navigate": not navigation_attempts and len(context.pages) == pages_before and page.url == url_before,
               "other_rail_drift_px": round(abs(other.evaluate("rail => rail.scrollLeft") - other_before), 2)}
        results.append(row)
        require(moved >= 20 and row["drag_did_not_navigate"] and row["other_rail_drift_px"] <= 1,
                "linked_poster_drag_failed")
        require(not rail.evaluate("rail => rail.classList.contains('is-dragging')"), "drag_capture_not_released")

        if label == "mobile":
            reset_rail(rail)
            navigation_attempts.clear()
            distance, moved = touch_swipe(page, context, rail)
            row.update(touch_swipe_px=round(distance, 1), touch_scroll_px=round(moved, 1),
                       touch_did_not_navigate=not navigation_attempts and len(context.pages) == pages_before)
            require(moved >= 20 and row["touch_did_not_navigate"], "native_touch_swipe_failed")

        reset_rail(rail)
        root.locator(".carousel-next").click()
        row["button_scroll_px"] = round(rail.evaluate(SETTLE), 1)
        require(row["button_scroll_px"] >= 20, "next_button_failed")
        reset_rail(rail)
        rail.focus()
        rail.press("ArrowRight")
        row["keyboard_scroll_px"] = round(rail.evaluate(SETTLE), 1)
        require(row["keyboard_scroll_px"] >= 20, "keyboard_navigation_failed")

        reset_rail(rail)
        link = rail.locator("a.theme-poster-tile[href]").first
        expected = link.evaluate("link => link.href")
        parsed = urlsplit(expected)
        require(parsed.scheme in {"http", "https"} and bool(parsed.hostname), "poster_link_invalid")
        navigation_attempts.clear()
        with context.expect_page(timeout=5000) as popup_info, context.expect_event(
                "request", predicate=lambda request: request.is_navigation_request(), timeout=5000) as request_info:
            link.click()
        popup = popup_info.value
        row["normal_click_opened_same_href"] = urldefrag(request_info.value.url)[0] == urldefrag(expected)[0]
        row["normal_click_opened_new_tab"] = popup != page and page.url == url_before
        popup.close()
        require(row["normal_click_opened_same_href"] and row["normal_click_opened_new_tab"],
                "ordinary_poster_link_failed")
        row["other_rail_final_drift_px"] = round(abs(other.evaluate("rail => rail.scrollLeft") - other_before), 2)
        require(row["other_rail_final_drift_px"] <= 1, "rail_interactions_not_independent")

    dimensions = frame.evaluate("""() => {
      const main = document.querySelector('[data-testid="stMain"]');
      return {viewport: window.innerWidth, document_scroll: document.documentElement.scrollWidth,
        main_scroll: main.scrollWidth, main_width: main.clientWidth};
    }""")
    report["tests"][f"{label}_no_horizontal_overflow"] = no_horizontal_overflow(dimensions)
    report["tests"][f"{label}_no_exception"] = not has_app_exception(page)
    require(report["tests"][f"{label}_no_horizontal_overflow"], "page_horizontal_overflow")
    require(report["tests"][f"{label}_no_exception"], "app_exception")


def verify(args):
    report = {"schema_version": 1, "protocol": "theme-rail-browser-v1", "status": "failed",
              "expected_version": args.expected_version, "expected_titles": args.expected_titles,
              "started_at": datetime.now(timezone.utc).isoformat(), "tests": {}, "errors": [], "warnings": [],
              "viewports": [{"name": label, "width": width, "height": height} for label, width, height in VIEWPORTS]}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            launch = {"headless": True, "args": ["--disable-dev-shm-usage"]}
            if args.executable:
                launch["executable_path"] = args.executable
            browser = playwright.chromium.launch(**launch)
            try:
                for label, width, height in VIEWPORTS:
                    context = browser.new_context(viewport={"width": width, "height": height},
                                                  is_mobile=label == "mobile", has_touch=label == "mobile",
                                                  reduced_motion="reduce")
                    try:
                        check_viewport(context.new_page(), context, args, report, label)
                    finally:
                        context.close()
                report["status"] = "passed"
            finally:
                browser.close()
    except VerificationIssue as error:
        report["errors"] = [error.code]
        if error.blocked:
            report["status"] = "blocked"
    except Exception:
        report["errors"] = ["browser_rail_verification_unavailable"]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=APP_URL)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-titles", type=int, default=0,
                        help="Require this many titles per theme; zero accepts stored counts")
    parser.add_argument("--executable")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/theme_rail_validation.json")
    args = parser.parse_args(argv)
    args.expected_version = normalized_version(args.expected_version or (ROOT / "VERSION").read_text(encoding="utf-8"))
    parsed = urlsplit(args.url)
    if (not args.expected_version or args.expected_titles < 0 or parsed.scheme not in {"http", "https"}
            or not parsed.hostname or parsed.username or parsed.password):
        parser.error("use an exact version, nonnegative title count, and HTTP(S) URL without credentials")
    report = verify(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
