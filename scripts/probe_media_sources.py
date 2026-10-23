"""Inspect public official business-source responses without keys or LLM calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import os
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ("amazon_entertainment_debug", "https://www.aboutamazon.com/news/entertainment"),
    ("amazon_report_debug", "https://ir.aboutamazon.com/news-release/news-release-details/2026/Amazon-com-Announces-Second-Quarter-Results/default.aspx"),
    ("amazon_nba_debug", "https://www.aboutamazon.com/news/entertainment/stream-nba-prime-video"),
    ("netflix_release_debug", "https://ir.netflix.net/investor-news-and-events/financial-releases/press-release-details/2026/Netflix-Declines-to-Raise-Offer-for-Warner-Bros-/default.aspx"),
    ("netflix_ir", "https://ir.netflix.net/financials/quarterly-earnings/default.aspx"),
    ("netflix_ir_rss", "https://ir.netflix.net/rss/pressrelease.aspx"),
    ("netflix_news", "https://about.netflix.com/"),
    ("disney_ir", "https://thewaltdisneycompany.com/investor-relations/"),
    ("disney_news", "https://thewaltdisneycompany.com/feed/"),
    ("amazon_ir", "https://ir.aboutamazon.com/quarterly-results/default.aspx"),
    ("amazon_ir_rss", "https://ir.aboutamazon.com/rss/pressrelease.aspx"),
    ("amazon_news", "https://www.aboutamazon.com/news/entertainment"),
    ("comcast_ir", "https://www.cmcsa.com/financial-information/quarterly-results"),
    ("comcast_rss", "https://www.cmcsa.com/rss/news-releases.xml"),
    ("skt_news", "https://news.sktelecom.com/feed"),
    ("cjenm_ir", "https://www.cjenm.com/ko/ir/"),
    ("kisdi", "https://www.kisdi.re.kr/"),
    ("kocca", "https://www.kocca.kr/"),
]


def probe(entry):
    source, url = entry
    result = {"id": source, "url": url, "status": "unavailable", "links": []}
    try:
        response = requests.get(url, timeout=(6, 20), headers={"User-Agent": "BtvMediaResearch/1.0 (public official sources)"})
        result.update(http_status=response.status_code, final_url=response.url,
                      content_type=response.headers.get("Content-Type", ""))
        if response.status_code != 200:
            return result
        if len(response.content) > 8 * 1024 * 1024:
            result["error_code"] = "response_too_large"
            return result
        soup = BeautifulSoup(response.content, "html.parser")
        result["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
        if source.endswith('_debug'):
            result['document_html'] = response.text[:700000]
        results = []
        if "xml" in result["content_type"] or "rss" in result["content_type"]:
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                results.append({"text": item.findtext("title", ""), "url": item.findtext("link", ""),
                                "date": item.findtext("pubDate", "")})
        for item in ([] if results else soup.find_all(["item", "entry"])):
            title, link, date = item.find("title"), item.find("link"), item.find(["pubdate", "published", "updated"])
            target = link.get("href") or link.get_text(strip=True) if link else ""
            results.append({"text": title.get_text(" ", strip=True) if title else "", "url": target,
                            "date": date.get_text(" ", strip=True) if date else ""})
        if results:
            result["feed_items"] = results[:16]
        seen = set()
        for anchor in soup.select("a[href]"):
            href = urljoin(response.url, anchor.get("href", ""))
            title = anchor.get_text(" ", strip=True)
            if not title or href in seen:
                continue
            if "#" not in href and re.search(r"\.pdf|2026|2025|earnings|quarter|results|광고|제휴|미디어|요금|OTT|stream|advertis", href + " " + title, re.I):
                seen.add(href)
                result["links"].append({"text": title[:180], "url": href})
            if len(result["links"]) >= 40:
                break
        for node in soup.select("script,style,nav,header,footer"):
            node.decompose()
        main = soup.find("main") or soup.find("article") or soup
        result["excerpt"] = re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:4000]
        result["status"] = "ok"
    except Exception:
        result["error_code"] = "request_failed"
    return result


def browser_documents():
    from playwright.sync_api import sync_playwright
    output = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ["MEDIA_PROBE_CHROMIUM"], headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        for source, url in SOURCES:
            if source not in {"netflix_ir", "amazon_ir"}:
                continue
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(6000)
                links = page.locator('a[href]').evaluate_all("nodes => nodes.map(a=>({text:a.innerText,url:a.href})).filter(a=> /\\.pdf|\\.xlsx|2026|2025/i.test(a.url) && a.text.trim())")
                output.append({"id": source, "status": "ok", "links": links[:50],
                               "excerpt": page.locator('body').inner_text()[-14000:]})
            except Exception:
                output.append({"id": source, "status": "unavailable"})
            finally:
                page.close()
        browser.close()
    return output


def main():
    with ThreadPoolExecutor(max_workers=5) as executor:
        sources = list(executor.map(probe, SOURCES))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "sources": sources}
    if os.getenv("MEDIA_PROBE_CHROMIUM"):
        report["browser_documents"] = browser_documents()
    path = ROOT / "artifacts/media_source_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sources": len(sources), "http_ok": sum(s["status"] == "ok" for s in sources)}))


if __name__ == "__main__":
    main()
