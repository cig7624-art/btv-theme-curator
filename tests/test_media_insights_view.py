"""Source/version guards and explicit-only paid actions for media research."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import unittest

from streamlit.testing.v1 import AppTest

from media_insights_view import (analysis_markdown, comparison_csv, filter_insight_items,
                                 insight_items, validated_analysis, validated_answer, _url, _locator_label, display_copy, artwork_html)


def document(identifier="one", *, topic="advertising_pricing", country="US", published="2026-10-01"):
    return {"id": identifier, "company": "Test Media " + identifier, "topic": topic, "country": country,
            "title": "Netflix introduces new ad-supported subscription plan " + identifier, "url": "https://official.example/" + identifier,
            "source_name": "Official newsroom", "source_type": "official_newsroom", "period": "2026년 3분기",
            "published_at": published, "collected_at": "2026-10-23T09:00:00Z", "content_hash": "hash-" + identifier,
            "excerpt": "This is an official source excerpt.",
            "evidence": [{"id": identifier + ":e1", "text": "The company announced a new advertising partnership.",
                          "locator": "Paragraph 2", "url": "https://official.example/" + identifier}]}


def analysis(item=None):
    item = item or document()
    return {"headline": "광고 제휴를 발표한 미디어 기업", "summary": "공식 발표에서 광고 제휴를 확인했습니다.",
            "facts": [{"text": "광고 제휴를 발표했습니다.", "evidence_ids": [item["id"] + ":e1"]}],
            "interpretation": "협업 범위를 살펴볼 수 있습니다.", "btv_options": ["제휴 범위의 차이를 검토합니다."],
            "questions": ["한국에도 적용되나요?"], "comparison": [], "context_hashes": {},
            "generated_at": "2026-10-23T10:00:00Z", "model": "actual-api-model", "source_hash": item["content_hash"]}


def snapshot():
    return {"schema_version": 1, "collected_at": "2026-10-23T09:00:00Z",
            "items": [document(), document("two", topic="ai_technology", country="KR", published="2025-01-01")],
            "sources": [{"id": "official", "name": "Official newsroom", "status": "failed",
                         "last_success_at": "2026-10-20T09:00:00Z", "last_checked_at": "2026-10-23T09:00:00Z"}]}


def app_fixture(data=None, note=None, answer=None):
    def run_app(data, note, answer):
        import streamlit as st
        from media_insights_view import render_media_insights_page
        st.session_state.setdefault("test_callbacks", {"refresh": 0, "analyze": 0, "ask": 0})
        def refresh():
            st.session_state["test_callbacks"]["refresh"] += 1
            return data
        def analyze(identifier):
            st.session_state["test_callbacks"]["analyze"] += 1
            st.session_state["test_analyzed"] = identifier
            if isinstance(note, Exception):
                raise note
            return note
        def ask(question, identifier):
            st.session_state["test_callbacks"]["ask"] += 1
            st.session_state["test_asked"] = (question, identifier)
            return answer
        render_media_insights_page(data, on_refresh=refresh, on_analyze=analyze, on_ask=ask)
    return AppTest.from_function(run_app, args=(data or snapshot(), note or analysis(), answer)).run()


class SourceGuardsTests(unittest.TestCase):
    def test_answer_retains_only_a_safe_engine_revision(self):
        data = snapshot()["items"]
        value = {"status": "supported", "answer": "공식 발표를 확인했습니다.", "evidence_ids": ["one:e1"],
                 "source_engine_revision": "1.3.4"}
        self.assertEqual(validated_answer(value, data)["source_engine_revision"], "1.3.4")
        value["source_engine_revision"] = '<script>unsafe</script>'
        self.assertNotIn("source_engine_revision", validated_answer(value, data))

    def test_dates_filter_publication_not_collection_and_default_keeps_older_records(self):
        data = snapshot(); data["items"].append(document("unknown", published=None))
        before = deepcopy(data)
        items = insight_items(data)
        self.assertEqual(len(filter_insight_items(items)), 3)
        recent = filter_insight_items(items, lookback_days=30, now=datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertEqual([item["id"] for item in recent], ["one"])
        self.assertEqual([item["id"] for item in filter_insight_items(items, topic="AI·기술", country="한국")], ["two"])
        self.assertEqual(data, before)

    def test_links_reject_executable_credential_and_malformed_urls(self):
        for url in ["javascript:alert(1)", "https://user:password@example.com/", "https://example.com:bad", "https://example.com/\nnext"]:
            self.assertEqual(_url(url), "")
        self.assertEqual(_url("https://official.example/a"), "https://official.example/a")

    def test_locators_are_readable_without_altering_evidence_identifiers(self):
        self.assertEqual(_locator_label("page12-p003"), "PDF 12쪽 · 3문단")
        self.assertEqual(_locator_label("p002"), "본문 2문단")
        self.assertEqual(_locator_label("Paragraph 2"), "Paragraph 2")

    def test_analysis_requires_real_generation_hash_and_saved_evidence(self):
        item = document(); note = analysis(item)
        self.assertIsNotNone(validated_analysis(item, note))
        for field, value in [("generated_at", ""), ("model", ""), ("source_hash", "older-hash")]:
            invalid = deepcopy(note); invalid[field] = value
            self.assertIsNone(validated_analysis(item, invalid))
        invalid = deepcopy(note); invalid["facts"][0]["evidence_ids"] = ["invented"]
        invalid["evidence"] = [{"id": "invented", "text": "invented quote", "url": "https://official.example/"}]
        self.assertIsNone(validated_analysis(item, invalid))
        self.assertIsNone(validated_analysis({**item, "content_hash": ""}, note))

    def test_cross_document_citations_require_current_context_hashes(self):
        items = snapshot()["items"]; note = analysis(items[0])
        note["comparison"] = [{"company": "Other media", "aspect": "제휴", "detail": "다른 기업도 제휴를 발표했습니다.",
                               "evidence_ids": ["two:e1"]}]
        self.assertIsNone(validated_analysis(items[0], note, items))
        note["context_hashes"] = {"two": items[1]["content_hash"]}
        checked = validated_analysis(items[0], note, items)
        self.assertEqual(checked["comparison"][0]["evidence_ids"], ["two:e1"])
        changed = deepcopy(items); changed[1]["content_hash"] = "changed"
        self.assertIsNone(validated_analysis(changed[0], note, changed))
        duplicate = deepcopy(items); duplicate[1]["evidence"][0]["id"] = "one:e1"
        self.assertIsNone(validated_analysis(items[0], analysis(items[0]), duplicate))

    def test_analysis_uses_authoritative_quotes_and_tolerates_optional_null_lists(self):
        item = document(); note = analysis(item)
        note["evidence"] = [{"id": "one:e1", "text": "Invented quote", "url": "https://untrusted.example"}]
        note["btv_options"] = None; note["questions"] = None
        checked = validated_analysis(item, note)
        self.assertEqual(checked["evidence"][0]["text"], item["evidence"][0]["text"])
        self.assertEqual(checked["btv_options"], [])

    def test_answers_need_existing_evidence_and_rebuild_source_links(self):
        items = snapshot()["items"]
        checked = validated_answer({"status": "supported", "answer": "발표에 제휴가 포함됩니다.", "evidence_ids": ["one:e1"],
                                    "sources": [{"url": "https://untrusted.example/"}]}, items)
        self.assertEqual(checked["sources"][0]["url"], items[0]["url"])
        self.assertIsNone(validated_answer({"answer": "근거 없는 내용", "evidence_ids": ["missing"]}, items))
        self.assertIsNone(validated_answer("근거 없는 내용", items))
        absent = validated_answer({"status": "not_found", "answer": "Arbitrary model content"}, items)
        self.assertEqual(absent["sources"], [])
        self.assertIn("근거를 확인하지 못했습니다", absent["answer"])
        self.assertNotIn("Arbitrary", absent["answer"])

    def test_answers_reject_changed_sources_and_preserve_citation_versions(self):
        items = snapshot()["items"]
        original = {"status": "supported", "answer": "제휴 발표를 확인했습니다.", "evidence_ids": ["one:e1"]}
        checked = validated_answer(original, items)
        self.assertEqual(checked["context_hashes"], {"one": "hash-one"})
        wrong = {**original, "context_hashes": {"one": "older-hash"}}
        self.assertIsNone(validated_answer(wrong, items))
        self.assertIsNone(validated_answer({**original, "source_hash": "wrong"}, items, "one"))
        changed = deepcopy(items); changed[0]["content_hash"] = "updated-hash"
        self.assertIsNone(validated_answer(checked, changed))

    def test_exports_include_citations_and_do_not_execute_spreadsheet_formulas(self):
        item = document(); note = analysis(item)
        note["comparison"] = [{"company": "=formula", "aspect": "+formula", "detail": "@formula", "evidence_ids": ["one:e1"]}]
        checked = validated_analysis(item, note)
        markdown = analysis_markdown(item, checked).decode("utf-8")
        self.assertIn(item["evidence"][0]["text"], markdown)
        self.assertIn(item["url"], markdown)
        self.assertIn("one:e1", markdown)
        csv = comparison_csv(checked).decode("utf-8-sig")
        self.assertIn("'=formula", csv)
        self.assertIn("'+formula", csv)
        self.assertIn("'@formula", csv)


class MagazinePresentationTests(unittest.TestCase):
    def test_korean_analysis_is_reused_and_original_is_not_a_headline(self):
        item = document()
        item["analysis"] = analysis(item)
        copy = display_copy(item)
        self.assertEqual(copy["title"], item["analysis"]["headline"])
        self.assertNotEqual(copy["title"], item["title"])
        missing = display_copy(document())
        self.assertRegex(missing["title"], "[가-힣]")
        self.assertNotEqual(missing["summary"], document()["excerpt"])

    def test_artwork_escapes_metadata_and_rejects_unsafe_image_addresses(self):
        item = {**document(), "company": "Disney", "image_url": "https://images.disney.example/art.jpg",
                "image_attribution": '<script>alert("bad")</script>'}
        art = artwork_html(item, lead=True)
        self.assertIn('src="https://images.disney.example/art.jpg"', art)
        self.assertIn("디즈니 공식 발표 관련 이미지", art)
        self.assertIn("&lt;script&gt;", art)
        self.assertNotIn("<script>", art)
        for url in ("http://images.example/a.jpg", "https://127.0.0.1/a.jpg", "https://localhost/a.jpg",
                    "https://user:password@images.example/a.jpg", "https://images.example:8443/a.jpg"):
            art = artwork_html({**item, "image_url": url})
            self.assertNotIn("<img ", art)
            self.assertIn("media-art-fallback", art)

    def test_major_edition_keeps_old_material_only_in_the_archive(self):
        app = app_fixture()
        self.assertFalse(app.exception)
        major_keys = [button.key for button in app.button if str(button.key).startswith("media_select_")]
        self.assertEqual(major_keys, ["media_select_" + hashlib.sha256(b"one").hexdigest()[:16]])
        self.assertTrue(any("수집 자료 보관함 · 2건" in expander.label for expander in app.expander))
        self.assertEqual(len(app.tabs), 0)
        markup = " ".join(row.value for row in app.markdown)
        self.assertIn("media-story-art-lead", markup)
        self.assertIn("주목한 이유", markup)

    def test_domestic_option_shares_topic_row_and_includes_media_and_products(self):
        data = snapshot()
        domestic = {**document("domestic", country="KR"), "company": "SK브로드밴드",
                    "title": "B tv 신규 결합 상품 출시", "market_scope": "domestic_product"}
        domestic_content = {**document("content", country="KR"), "company": "티빙",
                            "title": "티빙 스포츠 독점 중계 발표", "topic": "sports_content",
                            "market_scope": "domestic_media"}
        data["items"].extend([domestic, domestic_content])
        app = app_fixture(data)
        self.assertEqual(app.radio(key="media_insight_topic").options[:2], ["전체", "국내 이슈"])
        self.assertEqual(len(app.get("button_group")), 0)
        app.radio(key="media_insight_topic").set_value("국내 이슈").run()
        self.assertFalse(app.exception)
        self.assertIn(app.session_state["media_insight_selected_id"], {"domestic", "content"})
        markup = " ".join(row.value for row in app.markdown)
        self.assertIn("국내에서 주목할 소식", markup)
        self.assertIn("티빙 스포츠 독점 중계 발표", " ".join(button.label for button in app.button))
        self.assertIn("B tv 신규 결합 상품 출시", " ".join(button.label for button in app.button))
        self.assertIn('data-item-id="one"', markup)
        self.assertNotIn("Test Media one", markup)
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})

    def test_localization_only_calls_explicit_button_and_uses_returned_saved_copy(self):
        def render():
            import streamlit as st
            from media_insights_view import render_media_insights_page
            from test_media_insights_view import snapshot
            data = snapshot()
            st.session_state.setdefault("translate_calls", 0)
            def translate():
                st.session_state["translate_calls"] += 1
                for item in data["items"]:
                    item["localization"] = {
                        "title": "새로운 광고 제휴를 발표한 미디어 기업", "summary": "공식 발표에서 광고 제휴를 소개했습니다.",
                        "source_hash": item["content_hash"], "generated_at": "2026-10-24T01:00:00Z",
                        "model": "test", "language": "ko"}
                st.session_state["translated_data"] = data
                return data
            render_media_insights_page(st.session_state.get("translated_data", data), on_localize=translate)
        app = AppTest.from_function(render).run()
        self.assertFalse(app.exception)
        app.run()
        self.assertEqual(app.session_state["translate_calls"], 0)
        app.button(key="media_insights_localize").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["translate_calls"], 1)
        self.assertIn("새로운 광고 제휴를 발표한 미디어 기업", " ".join(row.value for row in app.markdown))
        self.assertFalse(any("media-pending-translation" in row.value for row in app.markdown))
        app.run()
        self.assertFalse(any(button.key == "media_insights_localize" for button in app.button))
        self.assertEqual(app.session_state["translate_calls"], 1)


class MediaInsightsAppTests(unittest.TestCase):
    def test_render_filter_and_selection_never_trigger_callbacks(self):
        app = app_fixture()
        self.assertFalse(app.exception)
        app.run()
        app.radio(key="media_insight_topic").set_value("AI·기술").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["media_insight_selected_id"], "two")
        app.radio(key="media_insight_topic").set_value("전체").run()
        key = "media_select_" + hashlib.sha256(b"one").hexdigest()[:16]
        app.button(key=key).click().run()
        self.assertEqual(app.session_state["media_insight_selected_id"], "one")
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})
        self.assertTrue(any("1곳" in expander.label for expander in app.expander))
        markup = " ".join(widget.value for widget in app.markdown)
        self.assertIn("이전 자료 보관", markup)
        self.assertIn("2026.10.20", markup)
        self.assertNotIn("2026.10.23</span>", markup)

    def test_analyze_explicitly_once_keeps_selection_and_shows_korean_headline(self):
        app = app_fixture()
        app.button(key="media_insights_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["test_callbacks"]["analyze"], 1)
        self.assertEqual(app.session_state["test_analyzed"], "one")
        self.assertEqual(app.session_state["media_insight_selected_id"], "one")
        markup = " ".join(widget.value for widget in app.markdown)
        self.assertIn("확인한 사실", markup)
        self.assertIn("광고 제휴를 발표한 미디어 기업", markup)
        self.assertTrue(any("원문 제목" in widget.value for widget in app.caption))
        app.run()
        self.assertEqual(app.session_state["test_callbacks"]["analyze"], 1)

    def test_question_uses_explicit_form_and_only_verified_source_link(self):
        answer = {"status": "supported", "answer": "제휴 발표를 확인했습니다.", "evidence_ids": ["one:e1"],
                  "sources": [{"url": "https://untrusted.example/"}]}
        app = app_fixture(answer=answer)
        app.button(key="media_insights_research_toggle").click().run()
        self.assertEqual(app.session_state["test_callbacks"]["ask"], 0)
        app.text_input(key="media_research_question").set_value("어떤 제휴인가요?")
        next(button for button in app.button if button.label == "근거 찾아 답하기").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["test_asked"], ("어떤 제휴인가요?", "one"))
        self.assertEqual(app.session_state["test_callbacks"]["ask"], 1)
        markup = " ".join(widget.value for widget in app.markdown)
        self.assertIn("https://official.example/one", markup)
        self.assertNotIn("https://untrusted.example", markup)
        app.run()
        self.assertEqual(app.session_state["test_callbacks"]["ask"], 1)

    def test_model_and_source_markup_is_escaped(self):
        data = snapshot(); data["items"][0]["excerpt"] = '<script>alert("bad")</script>'
        app = app_fixture(data)
        self.assertFalse(app.exception)
        markup = " ".join(widget.value for widget in app.markdown)
        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_only_trusted_domain_errors_are_shown(self):
        from media_insights_analysis import MediaInsightError
        app = app_fixture(note=MediaInsightError("분석 API 설정을 확인해 주세요."))
        app.button(key="media_insights_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertIn("분석 API 설정을 확인해 주세요.", [widget.value for widget in app.warning])
        app = app_fixture(note=ValueError("provider-secret-do-not-display"))
        app.button(key="media_insights_analyze").click().run()
        self.assertFalse(app.exception)
        self.assertFalse(any("provider-secret" in widget.value for widget in app.warning))

    def test_changed_source_hides_old_question_answer_without_repeating_ai_call(self):
        app = app_fixture()
        app.button(key="media_insights_research_toggle").click().run()
        app.session_state["media_insight_questions"] = [{"question": "앞선 질문", "answer": {
            "status": "supported", "answer": "이전 버전에서만 확인한 답변", "evidence_ids": ["one:e1"],
            "context_hashes": {"one": "old-source-hash"}}}]
        app.run()
        self.assertFalse(app.exception)
        markup = " ".join(widget.value for widget in app.markdown)
        self.assertIn("앞선 질문", markup)
        self.assertIn("공식 자료가 갱신됐어요", markup)
        self.assertNotIn("이전 버전에서만 확인한 답변", markup)
        self.assertIsNone(app.session_state["media_insight_questions"][0]["answer"])
        self.assertEqual(app.session_state["test_callbacks"]["ask"], 0)


if __name__ == "__main__":
    unittest.main()
