"""Secondary topics require direct media evidence in one saved paragraph."""
from copy import deepcopy
import unittest

from media_insights_topics import media_primary_topic, media_topic_ids, secondary_topic_matches, topic_evidence_matches


def document(*paragraphs, topic="earnings_strategy", **changes):
    result = {"title": "Company quarterly earnings", "topic": topic, "url": "https://official.example/news/one",
              "evidence": [{"id": "e" + str(index), "text": text, "url": "https://official.example/news/one"}
                           for index, text in enumerate(paragraphs, 1)]}
    result.update(changes)
    return result


class EvidenceTopicTests(unittest.TestCase):
    def test_streaming_advertising_in_an_earnings_document_keeps_primary_category(self):
        # The actual Amazon Q1 2026 announcement paragraph, without its URL/ID.
        row = document("Added Amazon Audiences for advertisers using Amazon Ads to buy advertising on Netflix, enabling brands to use Amazon’s proprietary shopping, streaming, and browsing signals, to reach relevant Netflix audiences and drive even stronger performance.")
        before = deepcopy(row)
        self.assertEqual(secondary_topic_matches(row), {"advertising_pricing": ["e1"]})
        self.assertEqual(media_topic_ids(row), ("earnings_strategy", "advertising_pricing"))
        self.assertEqual(row, before)

    def test_ai_for_storytelling_and_postproduction_is_media_technology(self):
        row = document("That’s why we’re leveraging AI to bring the most innovative tools to our storytellers. AI isn’t simply about efficiency – it’s about enhancing a creative process that will always be human-centered, artist-driven, and creator-led.",
                       "AI lets us work faster and smarter, particularly in areas of pre- and post-production. Our teams can personalize content and experiences for fans around the world at scale.")
        self.assertEqual(secondary_topic_matches(row), {"ai_technology": ["e1", "e2"]})

    def test_connected_tv_ad_creation_can_support_both_topics(self):
        row = document("Disney Ad Creative Studio brings together multiple AI models within a single workflow to transform existing brand materials into video ads for connected TV.", topic="advertising_pricing")
        self.assertEqual(topic_evidence_matches(row), {"advertising_pricing": ["e1"], "ai_technology": ["e1"]})
        self.assertEqual(secondary_topic_matches(row), {"ai_technology": ["e1"]})

    def test_aws_retail_ai_and_unrelated_media_paragraphs_are_not_combined(self):
        row = document("AWS artificial intelligence infrastructure and machine learning services grew rapidly this quarter.",
                       "AI shopping recommendations personalize product offers in our retail store.",
                       "Prime Video delivered strong viewership for its films and sports broadcasts.",
                       "Advertising grew 26 percent across the business.")
        self.assertEqual(secondary_topic_matches(row), {})
        # A Prime Video endpoint mention alone does not make the assistant's
        # general shopping/smart-home AI a film or streaming technology claim.
        assistant = document("Alexa has endpoints across devices, cars, offices, Fire TV, and Prime Video. Generative AI made the assistant more capable and customers completed more purchases and used smart home functionality more.")
        self.assertEqual(secondary_topic_matches(assistant), {})

    def test_headline_body_footer_and_other_documents_cannot_supply_missing_evidence(self):
        row = document("Quarterly earnings increased this year.", title="Netflix AI-powered advertising search",
                       body_text="Netflix uses AI for film production and advertising.")
        self.assertEqual(secondary_topic_matches(row), {})
        row["evidence"].append({"id": "other", "text": "Netflix uses AI search and advertising.", "url": "https://other.example/article"})
        row["evidence"].append({"id": "missing", "text": "Netflix uses AI search and advertising."})
        self.assertEqual(secondary_topic_matches(row), {})

    def test_pricing_requires_a_subscription_subject_and_media_context(self):
        self.assertEqual(secondary_topic_matches(document("Netflix introduced a new monthly subscription plan.")), {"advertising_pricing": ["e1"]})
        self.assertEqual(secondary_topic_matches(document("Netflix stock price increased after the quarterly results.")), {})
        self.assertEqual(secondary_topic_matches(document("The retail store introduced a new monthly subscription plan for grocery deliveries.")), {})

    def test_legacy_film_festival_accessibility_uses_current_content_classification_without_mutation(self):
        row = document("B tv가 독립·예술영화를 시청할 수 있는 특별 상영관을 운영합니다.", topic="ai_technology",
                       source_id="skb_products", country="KR",
                       title="SKB, 서울국제여성영화제 4년 연속 후원…독립·예술영화 접근성 넓힌다",
                       body_text="B tv가 독립·예술영화를 시청할 수 있는 특별 상영관을 운영합니다.")
        before = deepcopy(row)
        self.assertEqual(media_primary_topic(row), "sports_content")
        self.assertEqual(media_topic_ids(row), ("sports_content",))
        self.assertEqual(row, before)


if __name__ == "__main__":
    unittest.main()
