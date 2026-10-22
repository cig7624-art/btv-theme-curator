#!/usr/bin/env python3
"""Capture bounded public-home evidence without signing in or inventing rows.

The GitHub runner is outside Korea; a successful page load proves the public
source structure in that observation environment, not a Korea-home rollout.
HTML evidence omits executable scripts and tracking data. No browser storage,
request headers, authorization values, or cookies are saved.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests


SERVICES = (
    {"id": "netflix", "url": "https://www.netflix.com/kr/", "domains": ("netflix.com", "netflix.net")},
    {"id": "tving", "url": "https://www.tving.com/", "domains": ("tving.com",)},
    {"id": "coupang", "url": "https://www.coupangplay.com/", "domains": ("coupangplay.com", "coupang.com")},
    {"id": "disney", "url": "https://www.disneyplus.com/ko-kr", "domains": ("disneyplus.com", "bamgrid.com", "dssott.com")},
    {"id": "wavve", "url": "https://www.wavve.com/", "domains": ("wavve.com", "pooq.co.kr")},
)
MAX_HTML = 2_000_000
MAX_JSON = 900_000
MAX_JSON_FILES = 18
MAX_NETWORK = 180
REDACT_KEYS = {
    "accesstoken", "refreshtoken", "idtoken", "token", "authorization", "apikey",
    "cookie", "setcookie", "session", "sessionid", "trackingid", "anonymousid",
    "visitorid", "deviceid", "accountid", "userid", "profileid", "password",
    "credential", "credentials", "email", "ip", "ipaddress", "clientip", "remoteip",
    "userip", "ipv", "guid", "securetoken", "tracking",
}
TRACKING_QUERY = {"gclid", "fbclid", "dclid", "msclkid", "_ga", "_gl"}


def sensitive_key(key) -> bool:
    normalized = re.sub(r"[^a-z]", "", str(key).lower())
    return normalized in REDACT_KEYS or any(
        part in normalized for part in ("token", "session", "cookie", "tracking", "signature", "guid", "ipaddress")
    )


def safe_url(value: str) -> str:
    try:
        u = urlsplit(str(value))
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            return ""
        query = [(key, val) for key, val in parse_qsl(u.query, keep_blank_values=True)
                 if not sensitive_key(key) and not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ""))[:2500]
    except (TypeError, ValueError):
        return ""


# Scrub serialized state and textual excerpts as well as structured objects.
ASSIGNMENT_SECRET = re.compile(
    r"([\"']?(?:[a-z_-]*(?:token|session|cookie|tracking|guid|ipaddress)[a-z_-]*|ip)[\"']?\s*[:=]\s*)([\"'])(.*?)\2",
    re.I,
)
EMBEDDED_URL = re.compile(r"https?://[^\s<>\"')]+")


def redact_text(value: str, depth=0) -> str:
    stripped = value.strip()
    if depth < 32 and stripped.startswith(("{", "[")):
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, (dict, list)):
                return json.dumps(redact(decoded, depth + 1), ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            pass
    cleaned = ASSIGNMENT_SECRET.sub(lambda match: match.group(1) + match.group(2) + "[redacted]" + match.group(2), value)
    cleaned = EMBEDDED_URL.sub(lambda match: safe_url(match.group(0)), cleaned)
    return cleaned


def redact(value, depth=0):
    if depth > 32:
        return None
    if isinstance(value, dict):
        return {str(key): redact(item, depth + 1) for key, item in value.items() if not sensitive_key(key)}
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value[:1200]]
    if isinstance(value, str):
        return redact_text(value[:20_000], depth)
    return value


def clean_html(raw: str) -> tuple[str, str, str]:
    soup = BeautifulSoup(raw, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    for node in soup.select("script"):
        if node.get("type", "").lower() not in {"application/json", "application/ld+json"} and node.get("id") != "__NEXT_DATA__":
            node.decompose()
            continue
        try:
            node.string = json.dumps(redact(json.loads(node.string or node.get_text())), ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError):
            node.decompose()
    for node in soup.select("style, noscript, iframe, input, textarea"):
        node.decompose()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on") or sensitive_key(attr):
                del node.attrs[attr]
            elif isinstance(node.attrs[attr], str):
                node.attrs[attr] = redact_text(node.attrs[attr])
        for attr in ("href", "src", "poster", "data-src"):
            if isinstance(node.get(attr), str) and node[attr].startswith(("http://", "https://")):
                node[attr] = safe_url(node[attr])
    # Text nodes can contain escaped JSON state or URL query strings too.
    for node in list(soup.find_all(string=True)):
        cleaned_text = redact_text(str(node))
        if cleaned_text != str(node):
            node.replace_with(cleaned_text)
    text = soup.get_text(" ", strip=True)[:30_000]
    cleaned = str(soup)
    if len(cleaned.encode("utf-8")) > MAX_HTML:
        cleaned = cleaned[:MAX_HTML // 3]
    return cleaned, title, text


def observation(text: str, status=None, url="", title="") -> str:
    lowered = text.lower().strip()
    heading = title.lower().strip()
    if status in {401, 403, 429}:
        return "blocked_or_challenge"
    challenge_terms = ("access denied", "verify you are human", "checking your browser", "request blocked", "just a moment")
    # A reCAPTCHA legal notice in a normal marketing footer is not a challenge.
    if any(term in heading for term in challenge_terms) or (len(lowered) < 1600 and any(term in lowered for term in challenge_terms)):
        return "blocked_or_challenge"
    if len(lowered) < 1600 and (("captcha" in heading) or re.search(r"(?:enter|complete|solve|verify).{0,35}captcha|captcha.{0,35}(?:required|verification)|보안\s*문자를?\s*(?:입력|확인)", lowered)):
        return "blocked_or_challenge"
    if any(t in lowered for t in ("not available in your region", "not available in your country", "not available in this location", "해당 국가", "서비스 지역이 아닙니다", "not yet available")):
        return "region_restricted"
    if status and status >= 400:
        return "http_error"
    if not text.strip():
        return "empty_document"
    return "public_document_received"


def dump(path: Path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture_http(service, folder):
    result = {"url": service["url"], "status": None, "result": "network_error"}
    try:
        with requests.get(service["url"], timeout=(5, 8), allow_redirects=True, stream=True,
                          headers={"User-Agent": "Mozilla/5.0 PublicHomeVerification/1.0", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7"}) as response:
            chunks, length = [], 0
            for chunk in response.iter_content(32_768):
                chunks.append(chunk)
                length += len(chunk)
                if length >= MAX_HTML:
                    break
            raw = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            cleaned, title, text = clean_html(raw)
            with gzip.open(folder / "http.html.gz", "wt", encoding="utf-8") as stream:
                stream.write(cleaned)
            result.update(url=safe_url(response.url), status=response.status_code,
                          content_type=response.headers.get("Content-Type", "")[:150], title=title,
                          text_excerpt=text[:3000], result=observation(text, response.status_code, response.url, title),
                          truncated=length >= MAX_HTML)
    except requests.RequestException as exc:
        result["error_type"] = type(exc).__name__
    dump(folder / "http.json", result)
    return result


def official_url(url, domains):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def capture_browser(service, folder, browser, deadline):
    result = {"url": service["url"], "status": None, "result": "browser_error"}
    context = browser.new_context(locale="ko-KR", viewport={"width": 1366, "height": 900}, timezone_id="Asia/Seoul")
    context.set_default_timeout(5000)
    page = context.new_page()
    network, json_responses = [], []

    def seen(response):
        try:
            if not official_url(response.url, service["domains"]):
                return
            kind = response.request.resource_type
            if kind not in {"document", "xhr", "fetch", "script"}:
                return
            content_type = response.headers.get("content-type", "")
            if len(network) < MAX_NETWORK:
                network.append({"url": safe_url(response.url), "status": response.status, "resource_type": kind, "content_type": content_type[:100]})
            size = response.headers.get("content-length", "")
            if kind in {"xhr", "fetch"} and "json" in content_type and response.ok and len(json_responses) < 40 and (
                (service["id"] == "wavve" and urlsplit(response.url).path in {"/v1/home", "/v1/catalog"})
                or (service["id"] != "wavve" and re.search(r"home|catalog|collection|banner", urlsplit(response.url).path, re.I))
            ):
                if not size.isdigit() or int(size) <= MAX_JSON:
                    json_responses.append(response)
        except Exception:
            pass

    page.on("response", seen)
    try:
        remaining_ms = max(1000, int((deadline - time.monotonic()) * 1000))
        nav = page.goto(service["url"], wait_until="domcontentloaded", timeout=min(25_000, remaining_ms))
        result["status"] = nav.status if nav else None
        for _ in range(8):
            if time.monotonic() + 1 > deadline:
                break
            page.wait_for_timeout(550)
            page.evaluate("window.scrollBy(0, Math.min(innerHeight, 700))")
        page.evaluate("window.scrollTo(0,0)")
        raw = page.content()
        cleaned, title, text = clean_html(raw)
        with gzip.open(folder / "browser.html.gz", "wt", encoding="utf-8") as stream:
            stream.write(cleaned)
        result.update(url=safe_url(page.url), title=title, text_excerpt=text[:8000],
                      result=observation(text, result["status"], page.url, title))
        if time.monotonic() < deadline:
            page.screenshot(path=str(folder / "viewport.jpg"), full_page=False, type="jpeg", quality=60, timeout=4000)
        saved = []
        for response in json_responses:
            if len(saved) >= MAX_JSON_FILES or time.monotonic() >= deadline:
                break
            try:
                # All traffic comes from a fresh public browser. Never save headers/storage.
                body = response.body()
                if len(body) > MAX_JSON:
                    continue
                payload = redact(json.loads(body))
                name = f"response-{len(saved) + 1:02d}.json"
                dump(folder / name, {"source_url": safe_url(response.url), "data": payload})
                saved.append(name)
            except Exception:
                continue
        result["json_files"] = saved
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        try:
            result["url"] = safe_url(page.url)
            cleaned, title, text = clean_html(page.content())
            with gzip.open(folder / "browser.html.gz", "wt", encoding="utf-8") as stream:
                stream.write(cleaned)
            result.update(title=title, text_excerpt=text[:3000])
        except Exception:
            pass
    finally:
        dump(folder / "network.json", network)
        dump(folder / "browser.json", result)
        context.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="artifacts/ott_public_probe")
    parser.add_argument("--platform", action="append", choices=[s["id"] for s in SERVICES])
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    environment = "github_actions_global" if os.environ.get("GITHUB_ACTIONS") == "true" else "execution_environment"
    summary = {"observed_at": datetime.now(timezone.utc).isoformat(), "observed_environment": environment,
               "country_verified": False, "scope": "public_home", "authenticated": False, "platforms": []}
    browser = playwright = None
    if not args.skip_browser:
        try:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            executable = os.environ.get("OTT_CHROMIUM_EXECUTABLE")
            browser = playwright.chromium.launch(headless=True, **({"executable_path": executable} if executable else {}))
        except Exception as exc:
            summary["browser_error_type"] = type(exc).__name__
    try:
        for service in SERVICES:
            if args.platform and service["id"] not in args.platform:
                continue
            folder = root / service["id"]
            folder.mkdir(parents=True, exist_ok=True)
            deadline = time.monotonic() + 40
            record = {"id": service["id"], "http": capture_http(service, folder)}
            if browser and time.monotonic() < deadline:
                record["browser"] = capture_browser(service, folder, browser, deadline)
            summary["platforms"].append(record)
            print(json.dumps({"id": service["id"], "http": record["http"]["result"],
                              "browser": record.get("browser", {}).get("result", "not_run")}, ensure_ascii=False), flush=True)
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()
    dump(root / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
