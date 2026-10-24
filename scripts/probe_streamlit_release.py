"""Read-only diagnosis of the anonymous public Cloud wrapper; never calls AI."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts/streamlit_release_probe.json"
APP = "https://btv-theme-curator-moec8jpeyjkp9skzhefihq.streamlit.app/"


def public_text(value):
    value = re.sub(r"https?://\S+", "[URL]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[EMAIL]", value)
    value = re.sub(r"\b(?:sk-|ghp_|github_pat_)[\w-]+", "[REDACTED]", value)
    value = re.sub(r"[A-Za-z0-9_=-]{45,}", "[IDENTIFIER]", value)
    return value[:5000]


def main():
    report = {"frames": [], "http_errors": [], "network_errors": [], "loaded": False}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("DEPLOYMENT_CHROMIUM_PATH"),
                                   headless=True, args=["--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        def failed(request):
            report["network_errors"].append({"host": urlsplit(request.url).hostname,
                                             "error": request.failure})
        def response(result):
            if result.status >= 400:
                report["http_errors"].append({"host": urlsplit(result.url).hostname, "status": result.status})
        page.on("requestfailed", failed)
        page.on("response", response)
        try:
            page.goto(APP, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
            wake = page.get_by_role("button", name=re.compile(r"get this app back up|wake up", re.I))
            report["wake_button_found"] = wake.count() > 0
            report["buttons"] = page.get_by_role("button").all_text_contents()[:10]
            if wake.count() and wake.first.is_visible():
                wake.first.click()
                report["wake_clicked"] = True
            page.wait_for_timeout(30000)
            report["loaded"] = True
            for frame in page.frames:
                parsed = urlsplit(frame.url)
                try:
                    body = frame.locator("body").inner_text(timeout=5000)
                except Exception:
                    body = ""
                report["frames"].append({"host": parsed.hostname, "path": parsed.path,
                    "text": public_text(body), "iframes": frame.locator("iframe").count(),
                    "app_marker": frame.locator(".magazine-footer[data-version]").count()})
            # Only the public anonymous application page; no login or account UI.
            if urlsplit(page.url).hostname == urlsplit(APP).hostname:
                page.screenshot(path=str(ROOT / "artifacts/streamlit_release_probe.png"))
        except Exception as error:
            report["error_type"] = type(error).__name__
        finally:
            browser.close()
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"loaded": report["loaded"], "frames": len(report["frames"])}))


if __name__ == "__main__":
    main()
