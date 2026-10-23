"""Korean research questions must retrieve relevant official English documents."""
import unittest

from media_insights_runtime import related_items


class RelatedResearchTests(unittest.TestCase):
    def test_requested_company_and_subject_outrank_current_story_topic(self):
        selected = {"id": "selected", "topic": "partnership_distribution"}
        rows = [dict(id="other-merger", company="Netflix", topic="partnership_distribution", evidence=[{}]),
                dict(id="amazon-earnings", company="Amazon", topic="earnings_strategy", evidence=[{}]),
                dict(id="disney-earnings", company="Disney", topic="earnings_strategy", evidence=[{}])]
        result = related_items({"items": rows}, selected, "아마존 실적은 무엇이 달라졌나요?", limit=1)
        self.assertEqual([item["id"] for item in result], ["amazon-earnings"])

    def test_plain_analysis_stays_on_topic_and_excludes_selected_or_empty_docs(self):
        selected = dict(id="selected", topic="sports_content", evidence=[{}])
        rows = [selected, dict(id="no-evidence", topic="sports_content"),
                dict(id="unrelated", topic="earnings_strategy", evidence=[{}]),
                dict(id="sports", topic="sports_content", evidence=[{}])]
        self.assertEqual([item["id"] for item in related_items({"items": rows}, selected)], ["sports"])


if __name__ == "__main__":
    unittest.main()
