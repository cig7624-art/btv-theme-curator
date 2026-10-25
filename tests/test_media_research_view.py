"""Project continuity, explicit paid actions and safe grounded answer rendering."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from media_research_view import CODE_KEY, PROJECT_KEY, _safe_url
from media_research_storage import ResearchStorageError
import media_research_storage as store


SNAPSHOT = {"items": [{"id": "source-one", "content_hash": "source-hash"}]}
ANSWER = {
    "status": "grounded", "headline": "단품 상품의 가격 가설부터 검증합니다.",
    "facts": [{"id": "f1", "text": "공식 발표에서 광고형 상품을 확인했습니다.", "evidence_ids": ["S1"]}],
    "insights": [{"text": "서비스 조건과 고객별 가치를 비교할 수 있습니다.", "fact_ids": ["f1"]}],
    "options": [{"title": "단품 중심", "proposal": "조건을 고정해 수용도를 검증합니다.",
                 "rationale": "이용 목적을 분리해 볼 수 있습니다.", "tradeoffs": "실제 수익성 확인이 필요합니다.",
                 "validation": "유료 전환 실험을 진행합니다.", "fact_ids": ["f1"]}],
    "recommendation": {"text": "단품의 제공 범위부터 확정하고 가격안을 검증합니다.", "fact_ids": ["f1"]},
    "assumptions": ["원가는 아직 제공되지 않았습니다."],
    "missing_information": ["목표 고객과 최소 수익 조건이 필요합니다."],
    "next_steps": ["현재 상품 조건을 사업 조건에 저장합니다."],
    "sources": [{"id": "S1", "document_id": "source-one", "url": "https://official.example/plan",
                 "title": "공식 상품 발표", "published_at": "2026-10-15T00:00:00Z",
                 "locator": "본문 1문단", "excerpt": "The company introduced an ad-supported plan."}],
    "context_hashes": {"source-one": "source-hash"}, "generated_at": "2026-10-25T00:00:00Z", "model": "test-model",
}


def fixture(directory, answer=None, *, enabled=True, error=False):
    def render(directory, answer, enabled, error):
        from copy import deepcopy
        import streamlit as st
        from media_research_view import render_media_research
        st.session_state.setdefault("test_ask_count", 0)
        st.session_state.setdefault("test_contexts", [])
        def ask(question, project):
            st.session_state["test_ask_count"] += 1
            st.session_state["test_contexts"].append({"question": question, "project": deepcopy(project)})
            if error:
                raise ValueError("provider-secret-not-for-display")
            return deepcopy(answer)
        render_media_research({"items": [{"id": "source-one", "content_hash": "source-hash"}]},
                              on_ask=ask if enabled else None, storage_config={"storage_dir": directory})
    return AppTest.from_function(render, args=(directory, answer or deepcopy(ANSWER), enabled, error)).run()


def button(app, label):
    return next(element for element in app.button if element.label == label)


class MediaResearchViewTests(unittest.TestCase):
    def test_passive_render_and_project_controls_never_call_models_or_create_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory)
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["test_ask_count"], 0)
            self.assertEqual(list(Path(directory).glob("*.json")), [])
            self.assertFalse(app.chat_input(key="media_research_question").disabled)
            button(app, "새 대화").click().run()
            app.run()
            self.assertEqual(app.session_state["test_ask_count"], 0)
            self.assertFalse(app.exception)

    def test_followup_receives_saved_turn_business_conditions_and_adopted_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory)
            app.text_input(key="media_research_project_title").set_value("비플 가격 검토")
            app.text_area(key="media_research_brief_editor").set_value("목표 고객은 가족 단위입니다.")
            button(app, "사업 조건 저장").click().run()
            self.assertEqual(app.session_state["test_ask_count"], 0)
            app.chat_input(key="media_research_question").set_value("단품 가격을 어떻게 검토할까?").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["test_ask_count"], 1)
            self.assertEqual(len(app.session_state[PROJECT_KEY]["turns"]), 1)
            button(app, "이 결론 채택").click().run()
            self.assertEqual(len(app.session_state[PROJECT_KEY]["decisions"]), 1)
            self.assertEqual(app.session_state["test_ask_count"], 1)
            app.chat_input(key="media_research_question").set_value("그 방향에서 브랜드 메시지도 검토해줘.").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["test_ask_count"], 2)
            context = app.session_state["test_contexts"][-1]["project"]
            self.assertEqual(context["title"], "비플 가격 검토")
            self.assertEqual(context["brief"], "목표 고객은 가족 단위입니다.")
            self.assertEqual(len(context["turns"]), 1)
            self.assertEqual(len(context["decisions"]), 1)
            app.run()
            self.assertEqual(app.session_state["test_ask_count"], 2)

    def test_recovery_code_reopens_only_the_project_in_a_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            first = fixture(directory)
            first.chat_input(key="media_research_question").set_value("브랜딩을 어떻게 검토할까?").run()
            code = first.session_state[CODE_KEY]
            project_id = first.session_state[PROJECT_KEY]["id"]
            second = fixture(directory)
            self.assertNotIn(PROJECT_KEY, second.session_state)
            self.assertEqual(second.session_state["test_ask_count"], 0)
            second.text_input(key="media_research_open_code").set_value(code)
            button(second, "프로젝트 이어가기").click().run()
            self.assertFalse(second.exception)
            self.assertEqual(second.session_state[PROJECT_KEY]["id"], project_id)
            self.assertEqual(len(second.session_state[PROJECT_KEY]["turns"]), 1)
            self.assertEqual(second.session_state["test_ask_count"], 0)
            third = fixture(directory)
            self.assertNotIn(PROJECT_KEY, third.session_state)

    def test_next_question_refreshes_concurrent_brief_without_saving_editor_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory)
            app.chat_input(key="media_research_question").set_value("브랜딩 검토").run()
            code = app.session_state[CODE_KEY]
            store.update_brief(code, "동료가 확정한 목표는 가족 고객입니다.", storage_dir=directory)
            app.text_area(key="media_research_brief_editor").set_value("아직 저장하지 않은 다른 가정")
            app.chat_input(key="media_research_question").set_value("그 사업 조건으로 계속 검토해줘.").run()
            self.assertFalse(app.exception)
            context = app.session_state["test_contexts"][-1]["project"]
            self.assertEqual(context["brief"], "동료가 확정한 목표는 가족 고객입니다.")
            self.assertEqual(store.open_project(code, storage_dir=directory)["project"]["brief"],
                             "동료가 확정한 목표는 가족 고객입니다.")

    def test_adopted_decision_can_be_changed_or_removed_without_model_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory)
            app.chat_input(key="media_research_question").set_value("가격 후보 검토").run()
            button(app, "이 결론 채택").click().run()
            editor = next(element for element in app.text_area if str(element.key).startswith("media_research_decision_edit_"))
            editor.set_value("사업 조건을 검토한 뒤 고객 조사를 진행합니다.")
            button(app, "결정 수정").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state[PROJECT_KEY]["decisions"][0]["text"],
                             "사업 조건을 검토한 뒤 고객 조사를 진행합니다.")
            button(app, "결정 제외").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state[PROJECT_KEY]["decisions"], [])
            self.assertEqual(app.session_state["test_ask_count"], 1)

    def test_failed_answer_save_retries_storage_without_another_paid_question(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory)
            with patch("media_research_storage.append_turn", side_effect=ResearchStorageError("저장을 다시 시도해 주세요.")):
                app.chat_input(key="media_research_question").set_value("현재 사업 상황을 검토해줘.").run()
                self.assertFalse(app.exception)
                self.assertEqual(app.session_state["test_ask_count"], 1)
                self.assertTrue(app.chat_input(key="media_research_question").disabled)
                app.run()
                self.assertEqual(app.session_state["test_ask_count"], 1)
            button(app, "답변 저장 다시 시도").click().run()
            self.assertFalse(app.exception)
            self.assertEqual(len(app.session_state[PROJECT_KEY]["turns"]), 1)
            self.assertEqual(app.session_state["test_ask_count"], 1)
            self.assertFalse(app.chat_input(key="media_research_question").disabled)

    def test_model_error_is_safe_and_requires_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory, error=True)
            app.chat_input(key="media_research_question").set_value("브랜드 방향").run()
            self.assertFalse(app.exception)
            self.assertEqual(app.session_state["test_ask_count"], 1)
            self.assertFalse(any("provider-secret" in warning.value for warning in app.warning))
            app.run()
            self.assertEqual(app.session_state["test_ask_count"], 1)
            button(app, "질문 다시 보내기").click().run()
            self.assertEqual(app.session_state["test_ask_count"], 2)

    def test_no_api_keeps_saved_history_visible_and_disables_composer(self):
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory, enabled=False)
            self.assertFalse(app.exception)
            self.assertTrue(app.chat_input(key="media_research_question").disabled)
            self.assertEqual(app.session_state["test_ask_count"], 0)

    def test_model_text_and_source_links_cannot_insert_markup_or_private_urls(self):
        for source in ("javascript:alert(1)", "https://user:password@official.example/", "http://127.0.0.1/",
                       "https://localhost/", "https://local.example:bad/", "https://official.example/\nscript"):
            self.assertEqual(_safe_url(source), "")
        answer = deepcopy(ANSWER)
        answer["headline"] = '<script>alert("bad")</script> ![image](https://tracker.example/pixel)'
        answer["facts"][0]["text"] = "[secret](javascript:alert(1))"
        answer["sources"].append({"id": "S2", "url": "http://127.0.0.1/private", "title": "hidden"})
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory, answer=answer)
            app.chat_input(key="media_research_question").set_value("근거를 보여줘.").run()
            self.assertFalse(app.exception)
            markup = " ".join(element.value for element in app.markdown)
            self.assertNotIn("<script>", markup)
            self.assertIn("&lt;script&gt;", markup)
            self.assertNotIn("![image](", markup)
            self.assertEqual(len(app.session_state[PROJECT_KEY]["turns"]), 1)

    def test_transient_reference_uses_check_date_without_false_source_change_warning(self):
        answer = deepcopy(ANSWER)
        answer["context_hashes"] = {"transient-reference": "ref-hash"}
        answer["sources"][0].update(document_id="transient-reference", published_at="",
                                      reference_checked_at="2026-10-25T12:00:00Z")
        answer["source_search"] = {"status": "archive_only", "attempted": True, "documents_added": 0}
        with tempfile.TemporaryDirectory() as directory:
            app = fixture(directory, answer=answer)
            app.chat_input(key="media_research_question").set_value("공식 가격 안내를 확인해줘.").run()
            self.assertFalse(app.exception)
            captions = " ".join(element.value for element in app.caption)
            self.assertIn("발표일 미표기", captions)
            self.assertIn("확인일 2026", captions)
            self.assertNotIn("이 답변 이후 원문이 갱신", captions)
            self.assertIn("저장된 자료를 참고", captions)


if __name__ == "__main__":
    unittest.main()
