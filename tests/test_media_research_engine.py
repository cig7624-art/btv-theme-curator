"""Business reasoning keeps source evidence separate from ideas and memory."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openai.lib._pydantic import to_strict_json_schema

from media_insights_analysis import MediaInsightError
from media_research_engine import (
    BusinessResearchPlan, MAX_PROJECT_CHARS, _plan_type, answer_business_question,
    project_context, recalled_project_turns, retrieve_business_documents,
)


QUOTE = "Netflix offers an ad-supported plan in the United States for $7.99 per month."
NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def document(identifier="netflix", *, company="Netflix", topic="advertising_pricing",
             title="Netflix advertising subscription price", quote=QUOTE, published="2026-10-01"):
    return {"id": identifier, "company": company, "topic": topic, "title": title,
            "url": "https://official.example.com/" + identifier,
            "published_at": published, "collected_at": "2026-10-25T00:00:00Z",
            "country": "US", "content_hash": "hash-" + identifier,
            "source_name": company + " official", "source_type": "official_announcement",
            "evidence": [{"id": identifier + "-e1", "locator": "paragraph 1", "text": quote,
                          "url": "https://official.example.com/" + identifier}]}


def plan(**changes):
    result = {
        "headline": "상품의 제공 가치를 먼저 나누고 가격을 검증하세요",
        "facts": [{"text": "넷플릭스는 미국에서 월 $7.99 광고형 상품을 안내했습니다.",
                   "company": "Netflix", "evidence_ids": ["netflix-e1"], "source_quote": QUOTE}],
        "insights": [{"text": "광고 허용 여부와 제공 가치를 나눠 비교할 수 있습니다.", "fact_ids": ["F1"]}],
        "options": [{"title": "입문 상품의 가치를 명확하게", "proposal": "광고 허용 고객과 무광고 선호 고객을 나눠 검토하세요.",
                     "rationale": "같은 콘텐츠라도 고객이 허용하는 이용 조건이 다를 수 있다는 가정입니다.",
                     "tradeoffs": ["광고 부담이 고객 경험을 해칠 수 있어 확인이 필요합니다."],
                     "validation": "구성과 지불의사를 함께 묻는 고객 조사를 진행하세요.", "fact_ids": ["F1"]}],
        "recommendation": {"text": "원가와 핵심 고객의 지불의사를 확인한 뒤 가격 후보를 비교하세요.", "fact_ids": ["F1"]},
        "assumptions": ["목표 고객과 허용할 광고 수준은 아직 확정하지 않았습니다."],
        "missing_information": ["콘텐츠 원가와 기존 고객의 지불의사가 필요합니다."],
        "next_steps": ["상품 구성과 경쟁 범위를 정리한 뒤 고객 조사를 설계하세요."],
    }
    result.update(changes)
    return result


def client(value):
    result = Mock()
    result.responses.parse.return_value = SimpleNamespace(output_parsed=BusinessResearchPlan(**value),
        usage=SimpleNamespace(input_tokens=100, output_tokens=80))
    return result


class BusinessRetrievalTests(unittest.TestCase):
    def test_price_alias_and_entire_archive_search_do_not_follow_selected_or_recent_display(self):
        unrelated = document("sports", company="Disney", topic="sports_content",
                             title="Disney sports announcement", quote="Disney announced a sports broadcast.")
        older = document("netflix", published="2025-01-01")
        data = {"items": [unrelated, older]}
        before = deepcopy(data)
        found = retrieve_business_documents("B tv+ 단품 가격을 얼마로 정할까?", data, now=NOW)
        self.assertEqual(found[0]["id"], "netflix")
        self.assertEqual(data, before)

    def test_branding_routes_match_campaign_documents_even_without_korean_title_tokens(self):
        branding = document("brand", topic="branding_marketing", title="A new brand identity and campaign",
                            quote="The campaign focuses on audience choice and discovery.")
        sports = document("sports", topic="sports_content", title="Sports rights",
                          quote="Sports rights were announced.")
        found = retrieve_business_documents("리브랜딩 키메시지와 포지셔닝을 바꾸고 싶어", {"items": [sports, branding]}, now=NOW)
        self.assertEqual(found[0]["id"], "brand")

    def test_followup_uses_prior_user_question_but_not_prior_assistant_claims(self):
        data = {"items": [document()]}
        project = {"turns": [{"question": "가격을 어떻게 정할까?", "answer": {"headline": "Invented secret strategy"}}]}
        found = retrieve_business_documents("그럼 다른 안은?", data, project, now=NOW)
        self.assertEqual([item["id"] for item in found], ["netflix"])
        self.assertEqual(retrieve_business_documents("휴양지에서 쓸 물건 알려줘", data, now=NOW), [])

    def test_future_sources_and_unsafe_or_evidenceless_documents_are_not_evidence(self):
        invalid = [document("future", published="2027-01-01"),
                   {**document("local"), "url": "http://127.0.0.1/private"},
                   {**document("empty"), "evidence": []}]
        self.assertEqual(retrieve_business_documents("요금", {"items": invalid}, now=NOW), [])

    def test_fresh_query_reference_with_unknown_publication_date_is_prioritized_without_fabrication(self):
        reference = {**document("current", published=""), "research_reference": True,
                     "source_type": "research_reference", "reference_checked_at": "2026-10-25T00:00:00Z"}
        old = document("old", published="2025-01-01")
        found = retrieve_business_documents("가격", {"items": [old, reference]}, now=NOW)
        self.assertEqual(found[0]["id"], "current")
        self.assertEqual(found[0]["published_at"], "")

    def test_fresh_remembered_reference_needs_question_relevance_before_recency_bonus(self):
        remembered = {**document("recent-price", published=""), "research_reference": True,
                      "from_project_history": True, "source_type": "research_reference",
                      "reference_checked_at": "2026-10-25T00:00:00Z"}
        archive = {"items": [remembered]}
        self.assertEqual(retrieve_business_documents("휴양지에서 쓸 물건 알려줘", archive, now=NOW), [])
        self.assertEqual([item["id"] for item in retrieve_business_documents("요금 후보 검토", archive, now=NOW)],
                         ["recent-price"])


class BusinessResearchTests(unittest.TestCase):
    def test_real_structured_request_has_project_memory_source_constraints_and_usage_count(self):
        data = {"items": [document()]}
        project = {"name": "단품 검토", "brief": {"target": "가격 부담을 줄이고 싶은 고객"},
                   "decisions": ["상품 구성을 먼저 정한다"],
                   "turns": [{"question": "요금 검토를 시작하자", "answer": {"headline": "이전의 조건부 제안"}}],
                   "token": "do-not-send-this-metadata"}
        before = deepcopy([data, project])
        sdk = client(plan())
        with patch("media_research_engine.record_llm_response") as record:
            result = answer_business_question("단품 가격을 어떻게 정할까?", data, project,
                                              api_key="test", model="configured-model", client=sdk)
        call = sdk.responses.parse.call_args.kwargs
        payload = json.loads(call["input"][1]["content"])
        self.assertEqual(call["model"], "configured-model")
        self.assertNotIn("selected_document_id", payload)
        self.assertEqual(payload["project"]["approved_decisions"], ["상품 구성을 먼저 정한다"])
        self.assertEqual(payload["project"]["conversation"][0]["question"], "요금 검토를 시작하자")
        self.assertNotIn("do-not-send-this-metadata", call["input"][1]["content"])
        self.assertIn("사업 전략", call["input"][0]["content"])
        record.assert_called_once_with(sdk.responses.parse.return_value, feature="media_research", model="configured-model")
        self.assertEqual(result["status"], "grounded")
        self.assertEqual(result["facts"][0]["id"], "F1")
        self.assertEqual(result["facts"][0]["source_quote"], QUOTE)
        self.assertEqual(result["context_hashes"], {"netflix": "hash-netflix"})
        self.assertEqual(result["sources"][0]["url"], document()["url"])
        self.assertEqual([data, project], before)

    def test_unknown_citation_quote_number_and_company_cannot_publish_as_a_fact(self):
        variants = [{"evidence_ids": ["invented"]}, {"source_quote": "Invented source quotation."},
                    {"text": "미국 요금은 $99.99입니다."}, {"company": "Disney"},
                    {"text": "디즈니는 미국에서 광고형 상품을 안내했습니다."},
                    {"text": "넷플릭스는 한국에서 월 $7.99 상품을 안내했습니다."},
                    {"text": "넷플릭스는 전 세계에서 월 $7.99 상품을 제공합니다."},
                    {"text": "넷플릭스는 광고형 요금을 $7.99로 인상했습니다."},
                    {"text": "넷플릭스는 광고형 상품을 종료했습니다."}]
        for update in variants:
            with self.subTest(update=update):
                value = plan()
                value["facts"][0].update(update)
                result = answer_business_question("가격", {"items": [document()]}, client=client(value))
                self.assertEqual(result["facts"], [])
                self.assertEqual(result["sources"], [])
                self.assertEqual(result["insights"], [])
                self.assertEqual(result["options"], [])
                self.assertEqual(result["recommendation"]["fact_ids"], [])
                self.assertEqual(result["verification"]["facts_omitted"], 1)

    def test_a_bad_fact_does_not_shift_other_fact_ids_or_detach_their_insights(self):
        value = plan()
        good = deepcopy(value["facts"][0])
        value["facts"] = [{**good, "source_quote": "made up quote that cannot match"}, good]
        value["insights"] = [{"text": "유효한 사실과 연결한 검토입니다.", "fact_ids": ["F2"]},
                             {"text": "제외한 사실로 만든 해석입니다.", "fact_ids": ["F1"]}]
        result = answer_business_question("가격", {"items": [document()]}, client=client(value))
        self.assertEqual([row["id"] for row in result["facts"]], ["F2"])
        self.assertEqual([row["fact_ids"] for row in result["insights"]], [["F2"]])

    def test_no_relevant_sources_still_calls_real_engine_and_returns_conditional_help(self):
        value = plan(facts=[], insights=[{"text": "먼저 목표 고객이 원하는 가치를 정리하는 가정으로 접근할 수 있습니다.", "fact_ids": []}],
                     options=[{"title": "취향을 발견하는 시간", "proposal": "발견의 가치를 강조하는 메시지 후보입니다.",
                               "rationale": "가격 중심 메시지에서 경험으로 옮기는 가정입니다.", "tradeoffs": [],
                               "validation": "기존 메시지와 함께 고객 인터뷰에서 비교하세요.", "fact_ids": []}],
                     recommendation={"text": "목표 고객과 브랜드가 제공할 경험을 먼저 연결하세요.", "fact_ids": []})
        sdk = client(value)
        result = answer_business_question("새로운 브랜드 키메시지를 정하고 싶어", {"items": []}, client=sdk)
        sdk.responses.parse.assert_called_once()
        self.assertEqual(result["status"], "conditional")
        self.assertEqual(result["options"][0]["title"], "취향을 발견하는 시간")
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["sources"], [])
        self.assertTrue(result["missing_information"])

    def test_invented_price_is_omitted_but_explicit_user_price_is_preserved_as_a_proposal(self):
        value = plan()
        value["options"][0]["proposal"] = "월 5900원 후보를 검증하세요. 확정 가격은 아닙니다."
        result = answer_business_question("가격", {"items": [document()]}, client=client(value))
        self.assertEqual(result["options"], [])
        self.assertGreater(result["verification"]["proposal_fields_omitted"], 0)
        for project in [{"brief": "검토할 시험 가격은 월 5900원입니다."},
                        {"decisions": ["시험 후보 5900원을 검토하기로 했다"]}]:
            with self.subTest(project=project):
                accepted = answer_business_question("가격", {"items": [document()]}, project, client=client(value))
                self.assertIn("5900원", accepted["options"][0]["proposal"])

    def test_previous_ai_price_does_not_become_user_supplied_business_input(self):
        value = plan()
        value["options"][0]["proposal"] = "월 5900원 후보를 검증하세요."
        project = {"turns": [{"question": "가격을 검토하자", "answer": {"headline": "월 5900원은 임의의 가정"}}]}
        result = answer_business_question("다른 조건은?", {"items": [document()]}, project, client=client(value))
        self.assertEqual(result["options"], [])

    def test_recalled_user_cost_is_preserved_but_prior_ai_price_stays_unverified(self):
        value = plan()
        provided_cost = {**value["options"][0], "proposal": "사용자가 알려준 원가 3000원을 기준으로 상품 구성을 검토하세요."}
        prior_ai_price = {**value["options"][0], "title": "별도 가격안", "proposal": "월 3900원 가격을 검토하세요."}
        value["options"] = [provided_cost, prior_ai_price]
        project = {"turns": [{"question": "단품 원가는 3000원이야. 가격 구조를 검토해줘.",
                              "answer": {"headline": "월 3900원은 검증하지 않은 제안"}}]}
        result = answer_business_question("그럼 광고를 넣으면 어떻게 달라져?", {"items": [document()]}, project,
                                          client=client(value))
        self.assertEqual(len(result["options"]), 1)
        self.assertIn("3000원", result["options"][0]["proposal"])
        self.assertNotIn("3900", json.dumps(result["options"], ensure_ascii=False))

    def test_long_question_reaches_model_and_final_topic_drives_archive_retrieval(self):
        question = ("검토 배경을 충분히 설명합니다. " * 180) + "마지막 요청: 단품 가격과 요금 구조를 검토해줘."
        self.assertGreater(len(question), 2000)
        self.assertLess(len(question), 10000)
        sdk = client(plan())
        result = answer_business_question(question, {"items": [document()]}, client=sdk)
        payload = json.loads(sdk.responses.parse.call_args.kwargs["input"][1]["content"])
        self.assertEqual(payload["question"], question)
        self.assertTrue(payload["question"].endswith("단품 가격과 요금 구조를 검토해줘."))
        self.assertEqual(result["retrieval"]["source_ids"], ["netflix"])

    def test_korean_btv_company_alias_is_valid_only_with_the_actual_btv_source(self):
        quote = "SK브로드밴드는 B tv 신규 결합 상품을 출시하고 해당 상품의 제공 조건을 안내했습니다."
        item = {**document(company="SK브로드밴드", title="B tv 신규 상품", quote=quote), "country": "KR"}
        value = plan()
        value["facts"][0].update(text="B tv는 국내에서 신규 결합 상품을 안내했습니다.", company="SK Broadband", source_quote=quote)
        result = answer_business_question("B tv 단품 가격을 검토하자", {"items": [item]}, client=client(value))
        self.assertEqual(len(result["facts"]), 1)

    def test_historical_and_dateless_reference_dates_remain_honest_in_prompt_and_saved_sources(self):
        item = {**document(published=""), "research_reference": True,
                "reference_checked_at": "2026-10-25T00:00:00Z"}
        sdk = client(plan())
        result = answer_business_question("가격", {"items": [item]}, client=sdk)
        payload = json.loads(sdk.responses.parse.call_args.kwargs["input"][1]["content"])
        self.assertEqual(payload["documents"][0]["published_at"], "")
        self.assertEqual(payload["documents"][0]["reference_checked_at"], item["reference_checked_at"])
        self.assertEqual(result["sources"][0]["published_at"], "")
        self.assertEqual(result["sources"][0]["content_hash"], item["content_hash"])
        self.assertEqual(result["sources"][0]["collected_at"], item["collected_at"])
        self.assertEqual(result["retrieval"]["scope"], "archive_and_retrieved_sources")

    def test_source_links_ignore_model_prose_links_and_wrong_host_citation_urls(self):
        item = document()
        item["evidence"][0]["url"] = "https://untrusted.example.com/redirect"
        value = plan()
        value["options"][0]["proposal"] = "https://untrusted.example.com/ 확인하세요"
        result = answer_business_question("가격", {"items": [item]}, client=client(value))
        self.assertEqual(result["sources"][0]["url"], item["url"])
        self.assertEqual(result["options"], [])

    def test_dynamic_schema_whitelists_only_actual_excerpt_ids(self):
        schema = to_strict_json_schema(_plan_type({"real-e1": {}, "real-e2": {}}))
        fact = next(value for name, value in schema["$defs"].items() if "CitedBusinessFact" in name)
        self.assertEqual(fact["properties"]["evidence_ids"]["items"]["enum"], ["real-e1", "real-e2"])
        self.assertFalse(fact["additionalProperties"])

    def test_project_context_is_bounded_and_does_not_import_credentials(self):
        context = project_context({"name": "검토", "brief": "조건" * 10000, "decisions": ["결정" * 1000] * 100,
                                   "turns": [{"question": "질문" * 1000, "answer": "답" * 10000}] * 100,
                                   "api_key": "secret-key"})
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertLessEqual(len(serialized), MAX_PROJECT_CHARS)
        self.assertNotIn("secret-key", serialized)
        self.assertLessEqual(len(context["conversation"]), 6)

    def test_older_similar_questions_are_recalled_with_recent_conversation_and_approved_decisions(self):
        turns = [{"question": "단품 가격을 얼마로 할까?", "answer": {"headline": "기존 요금 검토안"}}]
        turns.extend({"question": "프로그램 편성을 검토하자 " + str(position), "answer": {"headline": "편성 검토안"}}
                     for position in range(12))
        project = {"turns": turns, "decisions": [{"text": "입문 상품을 먼저 검증한다", "id": "do-not-send-id"}]}
        context = project_context(project, "요금 후보를 다시 검토하자")
        self.assertIn("단품 가격을 얼마로 할까?", [turn["question"] for turn in context["conversation"]])
        self.assertIn("프로그램 편성을 검토하자 11", [turn["question"] for turn in context["conversation"]])
        self.assertEqual(context["approved_decisions"], ["입문 상품을 먼저 검증한다"])
        self.assertNotIn("do-not-send-id", json.dumps(context, ensure_ascii=False))

    def test_recalled_turns_retain_original_source_from_far_older_related_question(self):
        source = {"id": "price-original", "document_id": "price-doc", "excerpt": QUOTE,
                  "content_hash": "original-source-hash", "url": "https://official.example.com/price"}
        turns = [{"id": "old-price", "question": "단품 가격을 얼마로 정할까?",
                  "answer": {"headline": "기존 검토안", "sources": [source]}}]
        turns.extend({"id": "sports-" + str(position), "question": "프로그램 편성 전략을 검토하자",
                      "answer": {"headline": "편성 검토안", "sources": []}} for position in range(50))
        project = {"turns": turns}
        selected = recalled_project_turns(project, "요금 후보를 다시 살펴보자")
        self.assertLessEqual(len(selected), 6)
        self.assertIn("sports-49", [turn["id"] for turn in selected])
        old = next(turn for turn in selected if turn["id"] == "old-price")
        self.assertEqual(old["answer"]["sources"], [source])
        old["answer"]["sources"][0]["excerpt"] = "caller mutation"
        self.assertEqual(project["turns"][0]["answer"]["sources"][0]["excerpt"], QUOTE)

    def test_empty_question_and_provider_failures_do_not_leak_messages(self):
        sdk = client(plan())
        with self.assertRaises(MediaInsightError):
            answer_business_question(" ", {"items": []}, client=sdk)
        sdk.responses.parse.assert_not_called()
        sdk.responses.parse.side_effect = RuntimeError("secret-token provider body")
        with self.assertRaises(MediaInsightError) as error:
            answer_business_question("가격을 알려줘", {"items": []}, client=sdk)
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(error.exception.code, "provider_error")


if __name__ == "__main__":
    unittest.main()
