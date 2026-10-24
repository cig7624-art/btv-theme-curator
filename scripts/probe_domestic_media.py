"""Verify public domestic product sources and their actual editorial images."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    ("skb_new_feed", "https://blog.bworld.co.kr/feed/"),
    ("skb_new", "https://blog.bworld.co.kr/"),
    ("skb_news", "https://news.skbroadband.com/"),
    ("kt_newsroom", "https://newsroom.kt.com/"),
    ("kt_news_list", "https://corp.kt.com/html/promote/news/report_list.html"),
    ("kt_news_site", "https://corp.kt.com/html/promote/news/report.html"),
    ("lgu_rss", "https://news.lguplus.com/?feed=rss2"),
    ("lgu_sitemap", "https://news.lguplus.com/post-sitemap.xml"),
    ("lgu_products", "https://news.lguplus.com/category/newsroom/service/"),
    ("coupang_home", "https://news.coupang.com/"),
    ("coupang_play", "https://news.coupang.com/archives/category/coupang-play"),
]

BUSINESS = re.compile(r"B\s*tv|IPTV|OTT|티빙|TVING|웨이브|wavve|쿠팡플레이|디즈니|넷플릭스|지니\s*TV|U\+\s*tv|요금|구독|결합|번들|출시|제휴", re.I)


def probe(entry):
    identifier, url = entry
    result = {"id": identifier, "url": url, "status": "unavailable", "articles": [], "images": []}
    try:
        response = requests.get(url, timeout=(6, 22), headers={"User-Agent": "BtvMediaResearch/1.0 (official public product sources)"})
        result.update(http_status=response.status_code, final_url=response.url,
                      content_type=response.headers.get("Content-Type", ""))
        if response.status_code != 200 or len(response.content) > 8_000_000:
            return result
        soup = BeautifulSoup(response.content, "html.parser")
        result["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
        feed_items = []
        if re.search(br"<(rss|feed)[\s>]", response.content[:1500], re.I):
            tree = ET.fromstring(response.content)
            for item in list(tree.findall(".//item")) + list(tree.findall(".//{*}entry")):
                title = item.findtext("title") or item.findtext("{*}title") or ""
                node = item.find("link")
                if node is None:
                    node = item.find("{*}link")
                link = ((node.get("href") or node.text) if node is not None else "") or ""
                feed_items.append({"title": title, "url": urljoin(response.url, link),
                                   "date": item.findtext("pubDate") or item.findtext("{*}published")})
            result["feed_items"] = feed_items[:30]
            result["articles"] = [item for item in feed_items if BUSINESS.search(item["title"])][:10]
        else:
            seen = set()
            for anchor in soup.select("a[href]"):
                title = anchor.get_text(" ", strip=True)
                link = urljoin(response.url, anchor.get("href", ""))
                if title and link not in seen and BUSINESS.search(title):
                    seen.add(link)
                    result["articles"].append({"title": title[:240], "url": link})
            result["articles"] = result["articles"][:30]
        for meta in soup.select('meta[property="og:image"],meta[name="twitter:image"]'):
            if meta.get("content"):
                result["images"].append(urljoin(response.url, meta["content"]))
        for image in soup.select("article img,main img")[:10]:
            if image.get("src"):
                result["images"].append(urljoin(response.url, image["src"]))
        # Keep real, script-free DOM for source-adapter development.
        for node in soup.select("script,style,noscript"):
            node.decompose()
        result["document_html"] = str(soup)[:450000]
        result["excerpt"] = soup.get_text(" ", strip=True)[:2500]
        result["status"] = "ok"
    except Exception as error:
        result["error_type"] = type(error).__name__
    return result


def main():
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(probe, SOURCES))
    followups = []
    seen = set()
    for row in results:
        if row["id"].startswith(("disney_", "amazon_")):
            continue
        for index, article in enumerate(row["articles"][:3]):
            url = article["url"]
            if urlsplit(url).scheme in {"https", "http"} and url not in seen:
                seen.add(url)
                followups.append((row["id"] + "_article_" + str(index), url))
    with ThreadPoolExecutor(max_workers=6) as pool:
        details = list(pool.map(probe, followups))
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "sources": results, "article_details": details}
    path = ROOT / "artifacts/domestic_media_probe_current.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"sources": len(results), "ok": sum(r["status"] == "ok" for r in results), "details": len(details)}))


if __name__ == "__main__":
    main()
