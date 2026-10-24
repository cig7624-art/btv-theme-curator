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
                "headline": "Company expands streaming distribution partnership", "datePublished": "2026-10-20T10:30:00Z",
                "dateModified": "2026-10-22T11:20:00Z"}
        html = article(date="").replace("<head>", '<head><script type="application/ld+json">' + json.dumps(data) + "</script>")
        item = collection.parse_html_document(html, SOURCE, url=data["url"], collected_at=NOW)
        self.assertEqual(item["published_at"], "2026-10-20")
        self.assertEqual(item["title"], data["headline"])
        self.assertEqual(item["source_updated_at"], "2026-10-22")
        other = collection.parse_html_document(html, SOURCE, url="https://official.example/news/other", collected_at=NOW)
        self.assertEqual(other["published_at"], "")
        self.assertNotIn("source_updated_at", other)

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

    def test_pdf_reporting_period_date_is_not_publication(self):
        class Page:
            def extract_text(self):
                return "Reconciliation of Non-GAAP Financial Measures\nJune 27, 2026\n\n" + "\n\n".join(PARAGRAPHS)
        class Reader:
            def __init__(self, stream):
                self.pages = [Page()]
        source = {**SOURCE, "source_type": "earnings"}
        item = collection.parse_pdf_document(b"%PDF-test", source, url="https://official.example/q3.pdf",
                                             collected_at=NOW, title_hint="Q3 2026 Financial Reconciliations", pdf_reader=Reader)
        self.assertEqual(item["published_at"], "")
        self.assertEqual(item["published_at_source"], "pdf_publication_unconfirmed")

    def test_verified_feed_title_replaces_generic_press_release_scaffolding(self):
        source = {**SOURCE, "source_type": "earnings"}
        item = collection.parse_html_document(article("Press Release Details"), source,
                                              url="https://official.example/news/warner", collected_at=NOW,
                                              title_hint="Netflix and Warner Bros. Discovery Amend Agreement to All-Cash Transaction")
        self.assertEqual(item["title"], "Netflix and Warner Bros. Discovery Amend Agreement to All-Cash Transaction")
        self.assertEqual(item["topic"], "partnership_distribution")

    def test_q4_aspx_form_does_not_remove_real_earnings_release(self):
        # Small structural fixture derived from the actual Amazon IR page.
        html = '<html><head><meta property="og:title" content="Amazon.com Announces Second Quarter Results"></head><body><div class="Sectionnews-release" id="pageClass"><form><h1>News release details</h1><div class="module-news-details"><div class="module_date-text">July 30, 2026</div><div class="module_body"><div class="q4default">' + "".join(f"<p>{p}</p>" for p in PARAGRAPHS) + '</div></div></div></form></div></body></html>'
        source = {**SOURCE, "source_type": "earnings"}
        item = collection.parse_html_document(html, source, collected_at=NOW)
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Amazon.com Announces Second Quarter Results")
        self.assertEqual(item["topic"], "earnings_strategy")
        self.assertEqual(item["published_at"], "2026-07-30")
        self.assertEqual([e["text"] for e in item["evidence"]], PARAGRAPHS)


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

    def test_verified_official_seed_is_refetched_when_promotions_push_it_off_listing(self):
        seed_url = "https://official.example/news/nba"
        source = {**SOURCE, "document_urls": [{"url": seed_url, "title": "NBA streaming partnership"},
                                             {"url": "https://evil.example/news/nba", "title": "NBA partnership"}]}
        calls = []
        def fetcher(url):
            calls.append(url)
            return '<h1>Entertainment</h1><a href="/news/movie">A movie premiere</a>' if url == SOURCE["url"] else article("NBA streaming partnership")
        snapshot = collection.collect_media_insights(fetcher=fetcher, now=NOW, sources=[source])
        self.assertEqual(calls, [SOURCE["url"], seed_url])
        self.assertEqual(snapshot["items"][0]["url"], seed_url)
        self.assertEqual(snapshot["items"][0]["topic"], "sports_content")
        self.assertEqual(snapshot["sources"][0]["status"], "ok")

    def test_verified_product_seed_cannot_be_starved_by_listing_candidates(self):
        seed = "https://official.example/news/important-product"
        source = {**SOURCE, "document_urls": [{"url": seed, "title": "NBA streaming partnership"}]}
        listing = ''.join(f'<a href="/news/{i}">Company advertising partnership {i}</a>' for i in range(10))
        calls = []
        def fetcher(url):
            calls.append(url)
            return listing if url == source["url"] else article("NBA streaming partnership")
        snapshot = collection.collect_media_insights(fetcher=fetcher, sources=[source], max_documents=1, now=NOW)
        self.assertEqual(calls, [source["url"], seed])
        self.assertEqual(snapshot["items"][0]["url"], seed)


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

    def test_pdf_unconfirmed_publication_clears_previously_misread_fiscal_end(self):
        before = self.collect()
        before["items"][0]["published_at"] = "2026-06-27"
        before["items"][0]["analysis"] = {"summary": "A summary using the wrong date"}
        parsed = deepcopy(before["items"][0])
        parsed.pop("analysis")
        parsed.update(published_at="", published_at_source="pdf_publication_unconfirmed", collected_at=LATER)
        with patch.object(collection, "parse_pdf_document", return_value=parsed):
            after = collection.collect_media_insights(before, fetcher=lambda _: b"%PDF-fixture", now=LATER,
                                                       sources=[{**SOURCE, "url": "https://official.example/news/article"}])
        self.assertEqual(after["items"][0]["published_at"], "")
        self.assertEqual(after["items"][0]["collected_at"], NOW)
        self.assertNotIn("analysis", after["items"][0])

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


class OfficialImageryAndDomesticTests(unittest.TestCase):
    def test_publisher_revision_metadata_does_not_replace_publication(self):
        html = article().replace('<head>', '<head><meta property="article:modified_time" content="2026-10-22T11:20:00Z">')
        item = collection.parse_html_document(html, SOURCE, collected_at=NOW)
        self.assertEqual(item["published_at"], "2026-10-21")
        self.assertEqual(item["source_updated_at"], "2026-10-22")

    def test_official_article_og_image_is_recorded_with_attribution(self):
        # Structural excerpt from the real Disney publisher page: metadata is
        # outside the article, and its hero asset shares the publisher host.
        source = next(row for row in collection.SOURCES if row["id"] == "disney_news")
        hero = "https://thewaltdisneycompany.com/app/uploads/2026/10/Disney_Dude-Perfect-scaled.jpeg"
        html = article().replace("<head>", '<head><meta property="og:image" content="' + hero + '">')
        item = collection.parse_html_document(html, source,
            url="https://thewaltdisneycompany.com/news/dude-perfect-creative-partnership/", collected_at=NOW)
        self.assertEqual(item["image_url"], hero)
        self.assertEqual(item["image_source_url"], item["url"])
        self.assertEqual(item["image_attribution"], "디즈니 공식 자료")
        self.assertEqual(item["image_kind"], "article_image")

    def test_only_registered_https_image_assets_and_safe_nested_urls_are_allowed(self):
        source = {**SOURCE, "image_hosts": ["assets.official.example"]}
        good = "https://assets.official.example/resize?url=https%3A%2F%2Fassets.official.example%2Fphoto.jpg"
        self.assertEqual(collection._image_url(good, source), good)
        for bad in ("http://assets.official.example/photo.jpg", "https://unrelated.example/photo.jpg",
                    "https://assets.official.example:8443/photo.jpg", "https://user:pw@assets.official.example/photo.jpg",
                    "https://assets.official.example/photo.jpg?credential=private",
                    "https://assets.official.example/resize?url=https%3A%2F%2Fevil.example%2Fphoto.jpg",
                    "https://assets.official.example/resize?fname=https%3A%2F%2Fassets.official.example%2Fphoto.jpg%3Ftoken%3Dprivate"):
            with self.subTest(bad=bad):
                self.assertEqual(collection._image_url(bad, source), "")

    def test_wordpress_asset_proxy_must_embed_this_publishers_host(self):
        source = next(row for row in collection.SOURCES if row["id"] == "skb_products")
        real = "https://i0.wp.com/blog.bworld.co.kr/wp-content/uploads/2026/07/260701-ai-Btv.jpg"
        self.assertEqual(collection._image_url(real, source), real)
        self.assertEqual(collection._image_url("https://i0.wp.com/unrelated.example/photo.jpg", source), "")

    def test_rss_inline_image_needs_no_extra_article_fetch(self):
        source = {**SOURCE, "image_hosts": ["assets.official.example"]}
        hero = "https://assets.official.example/product.jpg"
        inline = '<img src="' + hero + '" alt="상품 화면">' + ''.join(f'<p>{text}</p>' for text in PARAGRAPHS)
        rss = feed([{"url": "https://official.example/news/product", "body": inline}])
        calls = []
        snapshot = collection.collect_media_insights(fetcher=lambda url: calls.append(url) or rss,
                                                      sources=[source], now=NOW)
        self.assertEqual(calls, [source["url"]])
        item = snapshot["items"][0]
        self.assertEqual(item["image_url"], hero)
        self.assertEqual(item["image_alt"], "상품 화면")
        self.assertEqual(item["image_fetched_from"], source["url"])

    def test_optional_article_art_fetch_preserves_feed_hash_and_saved_notes(self):
        source = {**SOURCE, "fetch_article_images": True}
        url = "https://official.example/news/product"
        inline = ''.join(f'<p>{text}</p>' for text in PARAGRAPHS)
        rss = feed([{"url": url, "body": inline}])
        before = collection.collect_media_insights(fetcher=lambda _: rss, sources=[{**SOURCE}], now=NOW)
        before["items"][0]["analysis"] = {"source_hash": before["items"][0]["content_hash"], "summary": "기존 분석"}
        before["items"][0]["localization"] = {"title": "기존 한글 제목"}
        # Article text differs from feed text. The artwork-only fetch must not
        # invalidate feed evidence or trigger paid retranslation/reanalysis.
        art = article(paragraphs=[text + " Additional article text." for text in PARAGRAPHS])
        art = art.replace('<head>', '<head><meta property="og:image" content="https://official.example/hero.jpg">')
        after = collection.collect_media_insights(before, sources=[source], now=LATER,
            fetcher=lambda request: rss if request == SOURCE["url"] else art)
        self.assertEqual(after["items"][0]["content_hash"], before["items"][0]["content_hash"])
        self.assertEqual(after["items"][0]["analysis"], before["items"][0]["analysis"])
        self.assertEqual(after["items"][0]["localization"], before["items"][0]["localization"])
        self.assertEqual(after["items"][0]["image_url"], "https://official.example/hero.jpg")

    def test_media_thumbnail_is_used_when_feed_body_has_no_image(self):
        rss = feed([{"url": "https://official.example/news/product", "body": ''.join(f'<p>{text}</p>' for text in PARAGRAPHS)}])
        rss = rss.replace('<rss ', '<rss xmlns:media="http://search.yahoo.com/mrss/" ')
        rss = rss.replace('</item>', '<media:thumbnail url="https://official.example/media.jpg"/></item>')
        result = collection.collect_media_insights(fetcher=lambda _: rss, sources=[SOURCE], now=NOW)
        self.assertEqual(result["items"][0]["image_url"], "https://official.example/media.jpg")

    def test_domestic_numeric_permalink_discovery_and_verified_product_marker(self):
        source = {**SOURCE, "company": "LG유플러스", "require_domestic_product": True, "country": "KR"}
        listing = '<a href="/22596">U+tv, 넷플릭스 결합 요금제 개편</a><a href="/22597">OTT 요금제 활용하는 방법</a>'
        links = collection._discover_documents({"url": source["url"], "content": listing.encode()}, source)
        self.assertEqual([row["url"] for row in links], ["https://official.example/22596"])
        item = collection.parse_html_document(article("U+tv, 넷플릭스 결합 요금제 개편"), source,
                                              url=links[0]["url"], collected_at=NOW)
        self.assertEqual(item["market_scope"], "domestic_product")
        self.assertEqual(item["country"], "KR")

if __name__ == "__main__":
    unittest.main()
