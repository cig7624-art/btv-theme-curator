"""Public source history never becomes a fabricated product-price change."""
from copy import deepcopy
import unittest

from public_media_monitoring import compare_public_document, public_documents


def document(identifier="current", *, company="Netflix", published="2026-10-15", amount="10"):
    return {"id": identifier, "company": company, "title": "Official plan announcement",
            "url": "https://official.example/" + identifier, "topic": "advertising_pricing",
            "published_at": published, "collected_at": "2026-10-26T10:00:00+00:00", "content_hash": "current-hash",
            "evidence": [{"id": identifier + "-p1", "text": "This plan costs USD " + amount + " per month in the announced market."}]}


class PublicMonitoringTests(unittest.TestCase):
    def test_two_prices_from_different_products_never_become_a_price_change(self):
        first, second = document("first", amount="10"), document("second", amount="20")
        rows = public_documents({"items": [first, second]})
        self.assertEqual(len(rows), 2)
        for row in rows:
            result = compare_public_document(row)
            self.assertEqual(result["status"], "no_previous")
            self.assertEqual(result["added"], [])

    def test_actual_same_document_versions_show_literal_before_and_after(self):
        current = document(amount="20")
        current["versions"] = [{"content_hash": "old-hash", "collected_at": "2026-10-20T10:00:00+00:00",
                                "evidence": document(amount="10")["evidence"]}]
        result = compare_public_document(current)
        self.assertEqual(result["status"], "changed")
        self.assertIn("USD 10", result["removed"][0])
        self.assertIn("USD 20", result["added"][0])
        self.assertNotIn("percent", str(result))
        self.assertEqual(result["source_url"], current["url"])

    def test_different_country_product_document_cannot_be_used_as_previous_version(self):
        current = document()
        previous = {**document("other-product"), "content_hash": "old", "collected_at": "2026-10-20T10:00:00+00:00"}
        current["versions"] = [previous]
        self.assertEqual(compare_public_document(current)["status"], "no_previous")

    def test_missing_source_excerpts_or_dates_are_not_a_comparison(self):
        current = document()
        for version in ({"content_hash": "old", "collected_at": "2026-10-20"},
                        {"content_hash": "old", "evidence": current["evidence"]},
                        {"content_hash": "old", "collected_at": "2026-10-27", "evidence": current["evidence"]}):
            current["versions"] = [version]
            self.assertEqual(compare_public_document(current)["status"], "no_previous")

    def test_reordered_or_whitespace_only_paragraphs_do_not_claim_a_change(self):
        current = document()
        text = current["evidence"][0]["text"]
        current["versions"] = [{"content_hash": "old", "collected_at": "2026-10-20",
                                "evidence": [{"text": "   ".join(text.split())}]}]
        self.assertEqual(compare_public_document(current)["status"], "unchanged")

    def test_most_recent_actual_earlier_observation_is_used(self):
        current = document(amount="20")
        current["versions"] = [{"content_hash": "first", "collected_at": "2026-10-10", "evidence": document(amount="5")["evidence"]},
                               {"content_hash": "second", "collected_at": "2026-10-20", "evidence": document(amount="10")["evidence"]}]
        self.assertIn("USD 10", compare_public_document(current)["removed"][0])

    def test_public_timeline_sorts_publication_dates_and_does_not_read_private_state(self):
        data = {"items": [document("old", published="2025-01-01"), document("recent"), document("another", company="Disney")],
                "notes": [{"brief": "private-business-brief"}], "research_projects": [{"answer": "private-answer"}]}
        before = deepcopy(data)
        rows = public_documents(data, "Netflix")
        self.assertEqual([row["id"] for row in rows], ["recent", "old"])
        self.assertNotIn("private", str(rows))
        self.assertEqual(data, before)

    def test_unsafe_links_and_missing_evidence_are_not_public_documents(self):
        for url in ("javascript:alert(1)", "https://user:password@example.com/private", "https://127.0.0.1/private"):
            self.assertEqual(public_documents({"items": [{**document(), "url": url}]}), [])
        self.assertEqual(public_documents({"items": [{**document(), "evidence": []}]}), [])


if __name__ == "__main__":
    unittest.main()
