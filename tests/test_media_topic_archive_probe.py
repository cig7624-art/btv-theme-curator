"""Read-only archive diagnostics must keep real dates, topics, and budgets."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from scripts import probe_media_topic_archives as probe


SOURCE = {"id": "official", "name": "Official newsroom", "company": "Company",
          "url": "https://official.example/feed/", "allowed_hosts": ["official.example"],
          "source_type": "official_announcement"}
BODY = ["The streaming service announced a new advertising plan for its film and television subscribers, with details confirmed in this official article.",
        "The company outlined the rollout and the viewer experience in its public announcement, including which users can access the changed streaming service."]


def feed(topic, count=3):
    title = "OTT advertising plan" if topic == "ads" else "IPTV AI-powered search"
    entries = []
    for index in range(count):
        paragraphs = ''.join('<p>' + text + ' Document ' + str(index) + '.</p>' for text in BODY)
        entries.append(f'<item><title>{title} {index}</title><link>https://official.example/news/{topic}-{index}</link>'
                       '<pubDate>Wed, 21 Oct 2026 12:00:00 GMT</pubDate><content:encoded><![CDATA['
                       + paragraphs + ']]></content:encoded></item>')
    return '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>' + ''.join(entries) + '</channel></rss>'


class TopicArchiveProbeTests(unittest.TestCase):
    def test_only_new_dated_relevant_items_are_proposed_without_mutating_saved_archive(self):
        entries = [{"probe_id": topic, "source": {**SOURCE, "url": SOURCE["url"] + "?s=" + topic}, "query": topic}
                   for topic in ("ads", "ai")]
        saved = {"items": [{"url": "https://official.example/news/ads-0", "topic": "advertising_pricing",
                            "published_at": "2026-10-21", "title": "OTT advertising plan", "body_text": BODY[0],
                            "evidence": [{"text": BODY[0]}], "analysis": {"summary": "Keep saved analysis"}}]}
        before = deepcopy(saved)
        with patch.object(probe, "archive_sources", return_value=entries):
            report = probe.run_probe(previous=saved, fetcher=lambda url: feed("ads" if url.endswith("ads") else "ai"))
        self.assertEqual(saved, before)
        self.assertEqual(report["requests"], 2)
        self.assertEqual(report["confirmed_topic_counts"], {"advertising_pricing": 3, "ai_technology": 3})
        self.assertEqual(report["new_confirmed_topic_counts"], {"advertising_pricing": 2, "ai_technology": 3})
        self.assertTrue(all(report["minimum_three_reached"].values()))
        self.assertTrue(all(item["published_at"] == "2026-10-21" for item in report["new_confirmed_items"]))
        self.assertTrue(all(item["evidence"] for item in report["new_confirmed_items"]))

    def test_missing_dates_future_dates_and_festival_accessibility_are_not_technology_padding(self):
        item = {"title": "IPTV AI-powered search", "topic": "ai_technology", "body_text": BODY[0],
                "evidence": [{"text": BODY[0]}], "published_at": "2026-10-21"}
        observed = "2026-10-26T00:00:00+00:00"
        self.assertTrue(probe._eligible(item, observed))
        for changes in ({"published_at": ""}, {"published_at": "2027-01-01"},
                        {"title": "SKB, 서울국제여성영화제 후원…독립·예술영화 접근성 넓힌다"}):
            with self.subTest(changes=changes):
                self.assertFalse(probe._eligible({**item, **changes}, observed))

    def test_total_public_request_budget_is_enforced_across_workers(self):
        entries = [{"probe_id": str(i), "source": {**SOURCE, "url": SOURCE["url"] + str(i)}, "query": "ads"}
                   for i in range(10)]
        called = []
        def fetcher(url):
            called.append(url)
            return feed("ads")
        with patch.object(probe, "archive_sources", return_value=entries):
            report = probe.run_probe(max_requests=2, fetcher=fetcher)
        self.assertEqual(len(called), 2)
        self.assertEqual(report["requests"], 2)
        self.assertTrue(any(row["error_code"] == "probe_request_budget_exhausted" for row in report["sources"]))

    def test_unrecognized_direct_headline_is_a_diagnostic_not_a_verified_finding(self):
        entries = [{"probe_id": "upfront", "source": SOURCE, "query": ""}]
        document = '<meta property="article:published_time" content="2026-05-15"><article><h1>Netflix Upfront 2026: The Center of Attention</h1>' + ''.join('<p>' + text + '</p>' for text in BODY) + '</article>'
        with patch.object(probe, "archive_sources", return_value=entries):
            report = probe.run_probe(fetcher=lambda _: document)
        self.assertEqual(report["new_confirmed_items"], [])
        diagnostic = report["sources"][0]
        self.assertEqual(diagnostic["document_title"], "Netflix Upfront 2026: The Center of Attention")
        self.assertEqual(diagnostic["publication_dates_observed"], ["2026-05-15"])
        self.assertEqual(diagnostic["source_paragraphs_for_review"], BODY)


if __name__ == "__main__":
    unittest.main()
