"""Official-source collection tests; no network or model calls are made."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import media_insights_collection as collection


SOURCE = {"id": "official", "name": "Official Company News", "company": "Company",
          "url": "https://official.example/feed", "allowed_hosts": ["official.example"],
          "source_type": "official_announcement", "country": ""}
NOW = "2026-10-23T10:00:00+00:00"
LATER = "2026-10-24T10:00:00+00:00"
PARAGRAPHS = [
    "The company announced an advertising partnership that will make its streaming service available through a new distribution agreement.",
    "The official announcement describes the countries and services covered by the agreement, while leaving commercial contract terms undisclosed.",
]


def article(title="Company announces advertising partnership", *, date="2026-10-21", paragraphs=None):
    metadata = f'<meta property="article:published_time" content="{date}">' if date else ""
    return f'<html><head>{metadata}</head><body><article><h1>{title}</h1>' + "".join(
        f"<p>{text}</p>" for text in (PARAGRAPHS if paragraphs is None else paragraphs)
    ) + "</article></body></html>"


def feed(entries):
    return '<?xml version="1.0"?><rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>' + "".join(
        "<item><title>" + entry.get("title", "Company announces advertising partnership") + "</title><link>"
        + entry["url"] + "</link><pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate>"
        + ("<content:encoded><![CDATA[" + entry["body"] + "]]></content:encoded>" if entry.get("body") else "")
        + "</item>" for entry in entries
    ) + "</channel></rss>"


class DocumentExtractionTests(unittest.TestCase):
    def test_actual_paragraphs_have_globally_unique_stable_evidence_ids(self):
        first = collection.parse_html_document(article(), SOURCE, url="https://official.example/news/one", collected_at=NOW)
        second = collection.parse_html_document(article(), SOURCE, url="https://official.example/news/two", collected_at=NOW)
        self.assertEqual(first["published_at"], "2026-10-21")
        self.assertEqual(first["topic"], "advertising_pricing")
        self.assertEqual(first["country"], "")
        self.assertEqual(first["period"], "")
        self.assertEqual([e["text"] for e in first["evidence"]], PARAGRAPHS)
        self.assertTrue(all(e["id"].startswith(first["id"] + "-") for e in first["evidence"]))
        self.assertTrue(set(e["id"] for e in first["evidence"]).isdisjoint(e["id"] for e in second["evidence"]))
        repeat = collection.parse_html_document(article(), SOURCE, url=first["url"], collected_at=LATER)
        self.assertEqual(first["evidence"], repeat["evidence"])
        self.assertEqual(first["content_hash"], repeat["content_hash"])

    def test_missing_publication_is_not_collection_date_or_financial_period(self):
        paragraphs = ["Revenue for the quarter ended June 30, 2026 was reported in the attached financial statement and the company discussed changes in its advertising business.", PARAGRAPHS[1]]
        item = collection.parse_html_document(article(date="", paragraphs=paragraphs), SOURCE,
                                              url="https://official.example/news/quarter", collected_at=NOW)
        self.assertEqual(item["published_at"], "")
        self.assertEqual(item["collected_at"], NOW)

    def test_structured_article_metadata_is_used_but_other_document_metadata_is_not(self):
        data = {"@type": "NewsArticle", "url": "https://official.example/news/current",
                "headline": "Company expands streaming distribution partnership", "datePublished": "2026-10-20T10:30:00Z"}
        html = article(date="").replace("<head>", '<head><script type="application/ld+json">' + json.dumps(data) + "</script>")
        item = collection.parse_html_document(html, SOURCE, url=data["url"], collected_at=NOW)
        self.assertEqual(item["published_at"], "2026-10-20")
        self.assertEqual(item["title"], data["headline"])
        other = collection.parse_html_document(html, SOURCE, url="https://official.example/news/other", collected_at=NOW)
        self.assertEqual(other["published_at"], "")

    def test_navigation_movie_promotion_and_schedule_are_not_business_documents(self):
        for title in ("Newsroom", "A closer look at the new summer movie", "Company to Announce Third Quarter Financial Results"):
            self.assertIsNone(collection.parse_html_document(article(title), SOURCE, collected_at=NOW))
        nav = '<h1>Company advertising partnership</h1><nav><p>' + PARAGRAPHS[0] + '</p></nav>'
        self.assertIsNone(collection.parse_html_document(nav, SOURCE, collected_at=NOW))
        earnings = {**SOURCE, "source_type": "earnings"}
        self.assertIsNone(collection.parse_html_document(article("The Walt Disney Company - Investor Relations"), earnings, collected_at=NOW))

    def test_skt_requires_media_subject_not_general_ai_or_phone_promotion(self):
        source = {**SOURCE, "require_media_subject": True, "country": "KR"}
        self.assertIsNone(collection.parse_html_document(article("Generative AI powers new phone advertising"), source, collected_at=NOW))
        item = collection.parse_html_document(article("IPTV streaming advertising partnership announced"), source, collected_at=NOW)
        self.assertEqual(item["country"], "KR")

    def test_pdf_evidence_has_page_locators_and_no_invented_publication(self):
        class Page:
            def __init__(self, text):
                self.text = text
            def extract_text(self):
                return self.text
        class Reader:
            def __init__(self, stream):
                self.pages = [Page("Q3 2026 Shareholder Letter\n\n" + PARAGRAPHS[0]), Page(PARAGRAPHS[1])]
        source = {**SOURCE, "source_type": "earnings", "country": "global"}
        item = collection.parse_pdf_document(b"%PDF-fixture", source, url="https://official.example/q3.pdf",
                                             collected_at=NOW, title_hint="Q3 2026 Shareholder Letter", pdf_reader=Reader)
        self.assertEqual(item["period"], "2026 Q3")
        self.assertEqual(item["published_at"], "")
        self.assertEqual(item["topic"], "earnings_strategy")
        self.assertTrue(all("page" in e["locator"] and e["id"].startswith(item["id"]) for e in item["evidence"]))
        self.assertEqual(item["evidence"][0]["url"], "https://official.example/q3.pdf#page=1")
        self.assertEqual(item["evidence"][1]["url"], "https://official.example/q3.pdf#page=2")
        self.assertEqual(item["url"], "https://official.example/q3.pdf")


class TransportAndDiscoveryTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_and_rejects_external_credentials_and_secrets(self):
        self.assertEqual(collection._official_url("https://official.example/news/a?utm_source=feed&lang=en#top", SOURCE),
                         "https://official.example/news/a?lang=en")
        for value in ("https://official.example.evil.test/news/a", "https://user:secret@official.example/news/a",
                      "https://official.example/news/a?token=secret", "http://127.0.0.1/news/a", "file:///tmp/a"):
            self.assertEqual(collection._official_url(value, SOURCE), "")

    def test_fetch_rejects_redirect_to_non_official_host(self):
        with self.assertRaisesRegex(collection.CollectionError, "redirect_not_allowed"):
            collection._fetch(SOURCE["url"], SOURCE, lambda _: {"content": article(), "url": "https://evil.example/news/a"})

    def test_http_redirect_is_checked_before_following(self):
        class Response:
            status_code = 302
            headers = {"Location": "http://169.254.169.254/latest/meta-data"}
            def close(self):
                pass
        with patch.object(collection.requests, "get", return_value=Response()) as get:
            with self.assertRaisesRegex(collection.CollectionError, "redirect_not_allowed"):
                collection._http_fetch(SOURCE["url"], SOURCE)
            self.assertEqual(get.call_count, 1)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_rss_canonical_link_and_inline_article_are_used_without_extra_fetch(self):
        url = "https://official.example/news/partnership"
        inline = "".join(f"<p>{text}</p>" for text in PARAGRAPHS) + '<a href="https://official.example/news/other">advertising partnership</a>'
        rss = feed([{"url": url, "body": inline}, {"url": "https://evil.example/news/advertising"}])
        requests_seen = []
        def fetcher(request_url):
            requests_seen.append(request_url)
            return rss
        snapshot = collection.collect_media_insights(fetcher=fetcher, now=NOW, sources=[SOURCE])
        self.assertEqual(requests_seen, [SOURCE["url"]])
        self.assertEqual(len(snapshot["items"]), 1)
        item = snapshot["items"][0]
        self.assertEqual(item["url"], url)
        self.assertEqual(item["published_at"], "2026-10-21")
        self.assertEqual(item["fetched_from"], SOURCE["url"])
        self.assertEqual(item["evidence"][0]["url"], url)

    def test_atom_uses_alternate_document_link_not_self_feed_url(self):
        xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Streaming distribution partnership</title><link rel="self" href="https://official.example/api/entry"/><link rel="alternate" href="https://official.example/news/partnership"/><published>2026-10-20T12:00:00Z</published></entry></feed>'
        links = collection._discover_documents({"content": xml.encode(), "url": SOURCE["url"]}, SOURCE)
        self.assertEqual([row["url"] for row in links], ["https://official.example/news/partnership"])
        self.assertEqual(links[0]["published_at"], "2026-10-20")

    def test_document_budget_is_bounded_and_no_same_body_duplicates(self):
        rss = feed([{"url": f"https://official.example/news/{i}"} for i in range(12)])
        requests_seen = []
        def fetcher(url):
            requests_seen.append(url)
            return rss if url == SOURCE["url"] else article()
        snapshot = collection.collect_media_insights(fetcher=fetcher, now=NOW, sources=[SOURCE], max_documents=3)
        self.assertEqual(len(requests_seen), 4)
        self.assertEqual(len(snapshot["items"]), 1)


class SnapshotPreservationTests(unittest.TestCase):
    def collect(self, html=None, previous=None, now=NOW):
        source = {**SOURCE, "url": "https://official.example/news/article"}
        return collection.collect_media_insights(previous, fetcher=lambda _: html or article(), now=now, sources=[source])

    def test_unchanged_source_retains_analysis_and_original_version_collection_time(self):
        before = self.collect()
        before["items"][0]["analysis"] = {"summary": "Saved analysis", "input_hash": "same"}
        after = self.collect(previous=before, now=LATER)
        self.assertEqual(after["items"][0]["analysis"], before["items"][0]["analysis"])
        self.assertEqual(after["items"][0]["collected_at"], NOW)
        self.assertEqual(after["items"][0]["last_checked_at"], LATER)
        self.assertEqual(after["sources"][0]["last_success_at"], LATER)
        self.assertEqual(before["items"][0]["collected_at"], NOW)

    def test_changed_body_requires_fresh_analysis_and_preserves_previous_evidence(self):
        before = self.collect()
        before["items"][0]["analysis"] = {"summary": "Old analysis"}
        changed = article(paragraphs=[PARAGRAPHS[0] + " This source was updated.", PARAGRAPHS[1]])
        after = self.collect(changed, previous=before, now=LATER)
        self.assertNotIn("analysis", after["items"][0])
        self.assertNotEqual(after["items"][0]["content_hash"], before["items"][0]["content_hash"])
        self.assertEqual(after["items"][0]["collected_at"], LATER)
        self.assertEqual(after["items"][0]["versions"][0]["evidence"], before["items"][0]["evidence"])

    def test_missing_rechecked_metadata_preserves_previously_verified_publication(self):
        before = self.collect()
        after = self.collect(article(date=""), previous=before, now=LATER)
        self.assertEqual(after["items"][0]["published_at"], "2026-10-21")

    def test_failure_preserves_items_and_success_date_without_leaking_exception_text(self):
        before = self.collect()
        original = deepcopy(before["items"])
        def fail(_):
            raise requests.ConnectionError("private password=secret https://example.test/?token=secret")
        after = collection.collect_media_insights(before, fetcher=fail, now=LATER,
                                                   sources=[{**SOURCE, "url": "https://official.example/news/article"}])
        self.assertEqual(after["items"], original)
        self.assertEqual(after["sources"][0]["last_success_at"], NOW)
        self.assertEqual(after["sources"][0]["last_checked_at"], LATER)
        self.assertEqual(after["sources"][0]["error_code"], "network_unavailable")
        self.assertEqual(after["sources"][0]["status"], "unavailable")
        self.assertNotIn("secret", json.dumps(after))

    def test_one_failed_document_marks_partial_and_keeps_success(self):
        rss = feed([{"url": "https://official.example/news/one"}, {"url": "https://official.example/news/two"}])
        def fetcher(url):
            if url == SOURCE["url"]:
                return rss
            if url.endswith("two"):
                return {"status": 403, "content": "forbidden"}
            return article()
        snapshot = collection.collect_media_insights(fetcher=fetcher, now=NOW, sources=[SOURCE])
        self.assertEqual(snapshot["sources"][0]["status"], "partial")
        self.assertEqual(snapshot["sources"][0]["last_success_at"], NOW)
        self.assertEqual(len(snapshot["items"]), 1)

    def test_snapshot_save_is_separate_and_corrupt_load_has_empty_safe_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media_insights_snapshot.json"
            old_csv = Path(directory) / "themes.csv"
            old_csv.write_text("existing,user,data\n", encoding="utf-8")
            snapshot = self.collect()
            collection.save_snapshot(path, snapshot)
            self.assertEqual(collection.load_snapshot(path), snapshot)
            self.assertEqual(old_csv.read_text(encoding="utf-8"), "existing,user,data\n")
            self.assertEqual(list(Path(directory).glob(".media-insights-*")), [])
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(collection.load_snapshot(path)["items"], [])
            with self.assertRaises(ValueError):
                collection.save_snapshot(path, {"items": []})


if __name__ == "__main__":
    unittest.main()
