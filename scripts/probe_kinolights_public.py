#!/usr/bin/env python3
"""Inspect logged-out Kinolights title/search sources; never uses API keys.

Only bounded public content responses are saved. Browser cookies, request
headers, browser storage and authentication/analytics traffic are not recorded.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import quote, urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_ott_public import clean_html, redact, safe_url

ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_probe(folder):
    records, links = [], []
    for index, query in enumerate(("기생충", "괴물")):
        url = "https://m.kinolights.com/search?keyword=" + quote(query)
        record = {"url": url, "query": query}
        try:
            response = requests.get(url, headers=HEADERS, timeout=(5, 20))
            record.update(status=response.status_code, final_url=safe_url(response.url))
            cleaned, title, text = clean_html(response.text[:3_000_000])
            (folder / f"search-{index}.html").write_text(cleaned, encoding="utf-8")
            record.update(title=title, text=text[:5000])
            soup = BeautifulSoup(response.text, "html.parser")
            title_links = [urljoin(response.url, a["href"]) for a in soup.select('a[href*="/title/"]')]
            links.extend(title_links[:6])
            record["title_links"] = title_links[:12]
            # Read published application code to discover the public content
            # operation names/fields, retaining context rather than all bundles.
            snippets = []
            for script in soup.select("script[src]")[:30]:
                script_url = urljoin(response.url, script["src"])
                if not (urlsplit(script_url).hostname or "").endswith("kinolights.com"):
                    continue
                try:
                    js = requests.get(script_url, headers=HEADERS, timeout=(5, 15)).text[:3_000_000]
                    hits = []
                    for pattern in ("graphql", "watchProvider", "watchProvider", "streaming", "ottProvider", "platforms", "contents(", "content("):
                        for match in list(re.finditer(re.escape(pattern), js, re.I))[:8]:
                            hits.append(js[max(0, match.start()-350):match.end()+1200])
                    if hits:
                        # Keep GraphQL documents and field identifiers only;
                        # application bundles may contain unrelated static IDs.
                        documents = []
                        for hit in hits:
                            documents.extend(re.findall(r'(?:query|fragment)\s+[A-Za-z_][A-Za-z_0-9]*[^`";]{0,4500}', hit))
                        identifiers = sorted(set(re.findall(r'\b[A-Za-z_][A-Za-z_0-9]*(?:Provider|Platform|Ott|OTT|Stream|Watch)[A-Za-z_0-9]*\b', " ".join(hits))))
                        snippets.append({"url": safe_url(script_url), "documents": documents[:15], "identifiers": identifiers[:100]})
                except requests.RequestException:
                    pass
            dump(folder / f"search-{index}-script-snippets.json", snippets)
        except requests.RequestException as exc:
            record["error"] = type(exc).__name__
        records.append(record)
    payload = {"operationName": "SearchContents", "variables": {"keyword": "괴물"},
               "query": "query SearchContents($keyword:String!){contents(keyword:$keyword,limit:8){id titleKr titleEn openYear}}"}
    try:
        response = requests.post("https://gateway.kinolights.com/graphql", json=payload, headers=HEADERS, timeout=(5, 20))
        data = response.json()
        dump(folder / "legacy-graphql.json", {"status": response.status_code, "response": redact(data)})
        for item in (data.get("data") or {}).get("contents", []) or []:
            if str(item.get("id", "")).isdigit():
                links.append("https://m.kinolights.com/title/" + str(item["id"]))
        if data.get("errors"):
            schema_query = {"query": "{__schema{queryType{fields{name args{name type{kind name ofType{kind name}}} type{kind name ofType{kind name}}}}}}"}
            schema_response = requests.post("https://gateway.kinolights.com/graphql", json=schema_query, headers=HEADERS, timeout=(5, 20))
            schema = schema_response.json()
            fields = ((schema.get("data") or {}).get("__schema") or {}).get("queryType", {}).get("fields", [])
            fields = [item for item in fields if re.search(r"content|title|provider|platform|stream|watch", item.get("name", ""), re.I)]
            dump(folder / "public-query-fields.json", {"status": schema_response.status_code, "fields": fields, "errors": schema.get("errors", [])})
    except (requests.RequestException, ValueError) as exc:
        dump(folder / "legacy-graphql.json", {"error": type(exc).__name__})
    dump(folder / "http-summary.json", records)
    return list(dict.fromkeys(links))[:5]


def browser_probe(folder, title_links, chromium_path=""):
    from playwright.sync_api import sync_playwright
    urls = ["https://m.kinolights.com/search?keyword=" + quote(query) for query in ("기생충", "괴물")]
    records = []
    with sync_playwright() as pw:
        options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        executable = chromium_path or shutil.which("google-chrome") or shutil.which("chromium")
        if executable:
            options["executable_path"] = executable
        browser = pw.chromium.launch(**options)
        context = browser.new_context(locale="ko-KR")
        page = context.new_page()
        captured = []
        def response_seen(response):
            url = response.url
            host = urlsplit(url).hostname or ""
            if not (host == "kinolights.com" or host.endswith(".kinolights.com")):
                return
            if re.search(r"auth|login|user|session|token|tracking|analytics", url, re.I):
                return
            try:
                if "json" not in response.headers.get("content-type", "") or len(captured) >= 35:
                    return
                data = response.json()
                # Content responses only: no generic/account/bootstrap JSON.
                encoded = json.dumps(data, ensure_ascii=False)
                if len(encoded) > 1_000_000 or not re.search(r'titleKr|openYear|watchProvider|streaming|ottProvider|"contents"|"providers"', encoded):
                    return
                entry = {"url": safe_url(url), "status": response.status, "response": redact(data)}
                request = response.request
                if request.method == "POST":
                    try:
                        posted = request.post_data_json
                        if isinstance(posted, dict):
                            entry["operation"] = redact({key: posted[key] for key in ("operationName", "query", "variables") if key in posted})
                    except Exception:
                        pass
                captured.append(entry)
            except Exception:
                pass
        page.on("response", response_seen)
        queue = list(dict.fromkeys(urls + title_links))[:8]
        index = 0
        while index < len(queue) and index < 8:
            url = queue[index]
            record = {"url": safe_url(url)}
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=35000)
                page.wait_for_timeout(3500)
                if "/search?" in url:
                    links = page.locator('a[href*="/title/"]').evaluate_all("els=>els.map(a=>a.href)")
                    for link in links[:3]:
                        if link not in queue and len(queue) < 8:
                            queue.append(link)
                cleaned, title, text = clean_html(page.content())
                (folder / f"browser-{index}.html").write_text(cleaned, encoding="utf-8")
                record.update(status=response.status if response else None, title=title, text=text[:10000])
            except Exception as exc:
                record["error"] = type(exc).__name__
            records.append(record)
            index += 1
        dump(folder / "browser-content-responses.json", captured)
        dump(folder / "browser-summary.json", records)
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--chromium-path", default="")
    args = parser.parse_args()
    folder = ROOT / "artifacts" / "kinolights_public_probe"
    folder.mkdir(parents=True, exist_ok=True)
    links = http_probe(folder)
    if args.browser:
        browser_probe(folder, links, args.chromium_path)
    print(json.dumps({"artifact_path": str(folder.relative_to(ROOT)), "title_links": links}, ensure_ascii=False))


if __name__ == "__main__":
    main()
