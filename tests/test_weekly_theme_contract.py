"""A clear editorial premise must survive selection, saving and title matching."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from api_usage import read_session_usage
from llm_theme_generator import (
    GeneratedTheme, GeneratedThemeBatch, _local_review_and_select, _sanitize_generated,
    generate_content_candidates_for_themes, generate_weekly_themes,
    load_recommendation_runs, persist_recommendations_locally,
)
from theme_content_manager import theme_rows_to_recommendations


ISSUES = [{
    "issue_key": "I01", "issue_title": "영화와 드라마의 공식 예고 영상 반응",
    "related_content": "존 윅", "source_group": "YouTube 반응",
    "keywords": "액션,스릴러,가족,추억,영화", "description": "공식 영상의 관측 기간 조회수 증가를 확인했습니다.",
}]


def proposal(name, *, genre="드라마", angle="관계형", rule=None, excluded=None, references=None):
    return {
        "theme_name": name, "copy": "서로 다른 작품에서 함께 발견하는 이야기",
        "genre": genre, "mood": "몰입", "keywords": ["가족", "추억", "관계", "영화", "긴장감"],
        "creation_angle": angle, "source_issue_keys": ["I01"],
        "source_issue_summary": "존 윅 공식 영상의 관측 기간 반응을 확인했습니다.",
        "content_search_terms": ["가족의 비밀", "관계 미스터리", "과거의 기억", "인물의 선택"],
        "rationale": "공식 영상에서 시작해 작품을 함께 감상할 공통 주제로 확장합니다.",
        "selection_rule": rule or "가족 관계 속에 감춰진 비밀을 밝혀가는 영화와 드라마를 고릅니다.",
        "exclusion_rule": excluded or "가족이 단역으로만 등장하고 비밀의 중심이 아닌 작품은 제외합니다.",
        "reference_titles": references or ["나이브스 아웃", "그을린 사랑", "기생충"],
    }


def intuitive_proposals():
    return [
        proposal("존 윅과 비슷한 액션 스릴러", genre="액션/스릴러", angle="장르변주형",
                 rule="존 윅처럼 추격과 근접 액션이 전개를 이끄는 액션 스릴러를 고릅니다.",
                 excluded="복수 소재만 같고 액션의 비중이 낮은 작품은 제외합니다.",
                 references=["아토믹 블론드", "노바디", "아저씨"]),
        proposal("비 오는 날 생각나는 추억의 영화", angle="상황형",
                 rule="비 오는 날 다시 보고 싶은 기억과 그리움을 담은 기성 영화를 고릅니다.",
                 excluded="오직 재난이나 폭우의 위험만 다루는 영화는 제외합니다.",
                 references=["러브레터", "클래식", "시네마 천국"]),
        proposal("가을밤 온 가족이 모여 앉아", angle="상황형",
                 rule="아이와 어른이 함께 감상할 수 있는 따뜻한 가족 모험 영화를 고릅니다.",
                 excluded="가족이라는 단어만 같고 성인용 폭력과 공포가 중심인 작품은 제외합니다.",
                 references=["패딩턴", "코코", "업"]),
        proposal("가족이라는 미스터리", genre="미스터리/드라마"),
    ]


def sdk_response(parsed):
    return SimpleNamespace(output_parsed=parsed, usage=SimpleNamespace(
        input_tokens=100, output_tokens=80, input_tokens_details=SimpleNamespace(cached_tokens=0)))


def content_response(theme_key, count=10):
    titles = ["나이브스 아웃", "그을린 사랑", "기생충", "마더", "올드보이",
              "장화, 홍련", "유전", "오펀: 천사의 비밀", "식스 센스", "시크릿"]
    return sdk_response(SimpleNamespace(themes=[SimpleNamespace(theme_key=theme_key, candidates=[
        SimpleNamespace(title=title, content_type="영화", year="", reason="가족의 비밀이 이야기의 중심")
        for title in titles[:count]
    ])]))


class WeeklyThemeContractTests(unittest.TestCase):
    def candidates(self, rows):
        return _sanitize_generated([GeneratedTheme(**row) for row in rows], len(rows))

    def test_selection_keeps_intuitive_topics_and_removes_reporting_or_plot_checklists(self):
        good = intuitive_proposals()
        rejected = [proposal("OTT 공개 후 클립 조회가 늘어난 콘텐츠"),
                    proposal("이혼 뒤 새 사랑과 복수를 시작한 주인공")]
        candidates = self.candidates([*rejected, *good])
        original = deepcopy(candidates)
        selected, meta = _local_review_and_select(
            candidates, pd.DataFrame(), pd.DataFrame(), ISSUES, 4, "test-model")
        self.assertEqual({rec["theme"]["theme_name"] for rec in selected},
                         {row["theme_name"] for row in good})
        self.assertEqual(meta["new_count"], 4)
        self.assertEqual(candidates, original)
        self.assertTrue(all(rec["selection_rule"] for rec in selected))

    def test_real_generation_entry_uses_one_response_and_keeps_the_editorial_contract(self):
        rows = intuitive_proposals()
        sdk = Mock()
        sdk.responses.parse.return_value = sdk_response(
            GeneratedThemeBatch(themes=[GeneratedTheme(**row) for row in rows]))
        session = {}
        with patch("llm_theme_generator.OpenAI", return_value=sdk), \
                patch("api_usage._session_state", return_value=session), \
                patch("requests.get", side_effect=AssertionError("No network in contract tests")):
            selected, meta = generate_weekly_themes(
                ISSUES, pd.DataFrame(), 4, "test-key", "test-model")
        sdk.responses.parse.assert_called_once()
        self.assertEqual(meta["selected_count"], 4)
        self.assertEqual(read_session_usage(session)["response_count"], 1)
        self.assertEqual(read_session_usage(session)["features"]["weekly_themes"], 1)
        by_name = {rec["theme"]["theme_name"]: rec for rec in selected}
        for row in rows:
            rec = by_name[row["theme_name"]]
            self.assertEqual(rec["selection_rule"], row["selection_rule"])
            self.assertEqual(rec["exclusion_rule"], row["exclusion_rule"])
            self.assertEqual(rec["reference_titles"], row["reference_titles"])

    def test_saved_run_restores_rules_for_content_matching_without_changing_old_rows(self):
        old_theme = {"theme_id": "T010", "theme_name": "기존 운영 테마", "copy": "원래 카피",
                     "genre": "드라마", "mood": "감동", "trigger_keywords": "가족,관계",
                     "source_status": "HUMAN_APPROVED", "operator_note": "기존 운영 메모"}
        old_history = {"run_id": "old-run", "theme_id": "T010", "rank": "1",
                       "theme_name": "기존 운영 테마", "recommended_date": "2026-01-01",
                       "recommendation_source": "EXISTING_DB", "custom_history": "보존할 이력 메모"}
        row = intuitive_proposals()[-1]
        selected, _ = _local_review_and_select(
            self.candidates([row]), pd.DataFrame([old_theme]), pd.DataFrame(), ISSUES, 1, "test-model")
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            theme_path, history_path = root / "themes.csv", root / "history.csv"
            pd.DataFrame([old_theme]).to_csv(theme_path, sep="|", index=False)
            pd.DataFrame([old_history]).to_csv(history_path, sep="|", index=False)
            saved, history, _ = persist_recommendations_locally(
                selected, pd.DataFrame([old_theme]), theme_path, history_path,
                {"run_id": "new-contract-run", "model": "test-model"})
            self.assertEqual(saved.iloc[0][list(old_theme)].to_dict(), old_theme)
            self.assertEqual(history.iloc[0][list(old_history)].to_dict(), old_history)
            snapshot = history.loc[history["run_id"].eq("new-contract-run")].iloc[0]
            self.assertEqual(snapshot["content_selection_rule"], row["selection_rule"])
            self.assertEqual(snapshot["content_exclusion_rule"], row["exclusion_rule"])
            self.assertEqual(json.loads(snapshot["content_reference_titles"]), row["reference_titles"])
            current_library = saved.copy()
            new_mask = current_library["theme_name"].eq(row["theme_name"])
            current_library.loc[new_mask, "content_selection_rule"] = "라이브러리의 후속 수정 기준"
            current_library.loc[new_mask, "content_exclusion_rule"] = "라이브러리의 후속 제외 기준"
            current_library.loc[new_mask, "content_reference_titles"] = '["다른 참고 작품"]'
            restored = next(run for run in load_recommendation_runs(history_path, current_library)
                            if run["run_id"] == "new-contract-run")["recommendations"]
            theme = restored[0]["theme"]
            self.assertEqual(theme["content_selection_rule"], row["selection_rule"])
            self.assertEqual(theme["content_exclusion_rule"], row["exclusion_rule"])
            self.assertEqual(json.loads(theme["content_reference_titles"]), row["reference_titles"])
            library = pd.read_csv(theme_path, sep="|", dtype=str).fillna("")
            library_recommendations = theme_rows_to_recommendations(
                library.loc[library["theme_name"].eq(row["theme_name"])])
            for scope, recommendations in [("saved-run", restored),
                                           ("library-refresh", library_recommendations)]:
                with self.subTest(scope=scope):
                    sdk = Mock()
                    sdk.responses.parse.return_value = content_response(recommendations[0]["theme"]["theme_id"])
                    with patch("llm_theme_generator.OpenAI", return_value=sdk):
                        rows, _ = generate_content_candidates_for_themes(recommendations, "test-key", "test-model")
                    sdk.responses.parse.assert_called_once()
                    self.assertEqual(len(next(iter(rows.values()))), 10)
                    request = sdk.responses.parse.call_args.kwargs
                    payload = json.loads(request["input"][-1]["content"])["themes"][0]
                    self.assertEqual(payload["selection_rule"], row["selection_rule"])
                    self.assertEqual(payload["exclusion_rule"], row["exclusion_rule"])
                    self.assertEqual(payload["reference_titles"], row["reference_titles"])

    def test_missing_issue_evidence_or_incomplete_grouping_contract_is_excluded(self):
        baseline = self.candidates([intuitive_proposals()[-1]])[0]
        mutations = [
            {"source_issue_keys": []},
            {"source_issue_keys": ["I99"]},
            {"selection_rule": ""},
            {"reference_titles": []},
            {"reference_titles": ["기생충", "그을린 사랑"]},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                incomplete = {**deepcopy(baseline), **mutation}
                selected, meta = _local_review_and_select(
                    [incomplete], pd.DataFrame(), pd.DataFrame(), ISSUES, 1, "test-model")
                self.assertEqual(selected, [])
                self.assertEqual(meta["new_count"], 0)

    def test_legacy_theme_without_contract_still_generates_candidates(self):
        sdk = Mock()
        sdk.responses.parse.return_value = content_response("T001")
        legacy = [{"theme": {"theme_id": "T001", "theme_name": "가족의 비밀",
                               "genre": "미스터리", "copy": "가까운 사람들의 낯선 얼굴"}}]
        with patch("llm_theme_generator.OpenAI", return_value=sdk):
            rows, _ = generate_content_candidates_for_themes(legacy, "test-key", "test-model")
        self.assertEqual(len(rows["T001"]), 10)
        sdk.responses.parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
