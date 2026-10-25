"""Project evidence is reused independently of the public magazine's filters."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from media_research_runtime import answer_research, with_project_evidence


def project():
    source = {"id": "reference:p1", "document_id": "reference", "title": "Official plan",
              "url": "https://help.netflix.com/ko/node/24926", "content_hash": "original-version",
              "excerpt": "The original plan includes advertising and a monthly subscription.",
              "collected_at": "2026-10-25T00:00:00+00:00", "published_at": "", "company": "Netflix"}
    return {"brief": "가족 고객을 위한 구독 상품", "decisions": [], "turns": [
        {"question": "가격 검토", "answer": {"headline": "AI interpretation is not an original source",
                                               "sources": [source]}}]}


class ResearchRuntimeTests(unittest.TestCase):
    def test_reuses_only_original_excerpts_without_changing_public_archive_or_project(self):
        snapshot = {"items": [], "sources": [], "schema_version": 1}
        memory = project()
        before = deepcopy(memory)
        enriched = with_project_evidence(snapshot, memory)
        self.assertEqual(snapshot["items"], [])
        self.assertEqual(memory, before)
        self.assertEqual(enriched["items"][0]["published_at"], "")
        self.assertEqual(enriched["items"][0]["reference_checked_at"], "2026-10-25T00:00:00+00:00")
        self.assertEqual(enriched["items"][0]["evidence"][0]["text"], memory["turns"][0]["answer"]["sources"][0]["excerpt"])
        self.assertNotIn("AI interpretation", str(enriched))

    def test_current_archive_version_is_preserved_instead_of_old_conversation_source(self):
        latest = {"id": "reference", "content_hash": "new-version", "evidence": []}
        result = with_project_evidence({"items": [latest]}, project())
        self.assertEqual(result["items"], [latest])

    def test_no_primary_sources_does_not_turn_saved_claims_or_other_projects_into_evidence(self):
        memory = {"turns": [{"answer": {"facts": [{"text": "invented claim"}],
                                        "sources": [{"url": "javascript:bad", "id": "x"}]}}]}
        self.assertEqual(with_project_evidence({"items": []}, memory)["items"], [])
        self.assertEqual(with_project_evidence({"items": []}, {})["items"], [])

    def test_explicit_action_enriches_once_then_answers_once_with_full_project(self):
        snapshot, memory = {"items": [], "sources": []}, project()
        expanded = {"items": [{"id": "fresh-primary"}]}
        search_report = {"status": "enriched", "attempted": True, "documents_added": 1, "errors": []}
        response = {"status": "grounded", "headline": "실제 근거와 검토안"}
        with (patch("media_research_sources.enrich_research_sources", return_value={
                  "snapshot": expanded, "report": search_report}) as search,
              patch("media_research_engine.answer_business_question", return_value=response) as answer):
            result = answer_research("이전 안에서 광고형을 추가하면?", snapshot, memory, api_key="test-key", model="configured")
        search.assert_called_once()
        self.assertEqual(search.call_args.args[1]["items"][0]["id"], "reference")
        answer.assert_called_once_with("이전 안에서 광고형을 추가하면?", expanded, memory,
                                       api_key="test-key", model="configured")
        self.assertEqual(result["source_search"], search_report)
        self.assertEqual(snapshot["items"], [])


if __name__ == "__main__":
    unittest.main()
