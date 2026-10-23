"""Weekly generation must keep prior director adoptions and their provenance."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd

from llm_theme_generator import (
    HISTORY_COLUMNS, ThemeGenerationError, load_recommendation_runs,
    persist_recommendations_locally,
)


def recommendation(name):
    return {
        "recommendation_source": "AI_GENERATED",
        "theme": {"theme_name": name, "copy": "새로운 선택을 따라갑니다.",
                  "genre": "드라마", "mood": "긴장", "trigger_keywords": "선택,관계"},
        "rationale": "이번 주 이슈에서 출발한 기획입니다.",
    }


class WeeklyHistoryPreservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.theme_path, self.history_path = root / "themes.csv", root / "history.csv"
        self.themes = pd.DataFrame([{
            "theme_id": "T041", "theme_name": "채택된 디렉터 테마", "copy": "기존 카피",
            "approved_status": "ADOPTED", "director_adoption_key": "adoption-original",
            "operator_note": "보존할 운영 메모",
        }])
        self.prior = pd.DataFrame([{
            **dict.fromkeys(HISTORY_COLUMNS, ""),
            "run_id": "AI-DIRECTOR-original", "theme_id": "T041", "rank": "1",
            "theme_name": "채택된 디렉터 테마", "recommended_at": "2026-10-22T09:00:00+09:00",
            "recommended_date": "2026-10-22", "recommendation_source": "AI_GENERATED",
            "director_draft_id": "draft-original", "director_adoption_key": "adoption-original",
            "custom_history": "이력에 남겨야 할 운영 정보",
        }])
        self.themes.to_csv(self.theme_path, sep="|", index=False)
        self.prior.to_csv(self.history_path, sep="|", index=False)

    def persist(self, name, run_id, themes=None):
        return persist_recommendations_locally(
            [recommendation(name)], self.themes if themes is None else themes,
            self.theme_path, self.history_path, {"run_id": run_id, "model": "test-model"},
        )

    def test_multiple_weekly_runs_append_and_reload_prior_adoption_with_all_extra_fields(self):
        updated, _, _ = self.persist("첫 주간 추천 테마", "weekly-one")
        updated, history, _ = self.persist("다음 주간 추천 테마", "weekly-two", updated)
        saved = pd.read_csv(self.history_path, sep="|", dtype=str).fillna("")
        self.assertEqual(saved["run_id"].tolist(), ["AI-DIRECTOR-original", "weekly-one", "weekly-two"])
        self.assertEqual(saved.iloc[0][self.prior.columns].to_dict(), self.prior.iloc[0].to_dict())
        self.assertEqual(len(history), 3)
        self.assertEqual({run["run_id"] for run in load_recommendation_runs(self.history_path, updated)},
                         {"AI-DIRECTOR-original", "weekly-one", "weekly-two"})

    def test_same_run_retry_does_not_duplicate_rows_or_erase_adoption_provenance(self):
        updated, _, _ = self.persist("같은 요청의 주간 테마", "weekly-one")
        _, history, _ = self.persist("같은 요청의 주간 테마", "weekly-one", updated)
        self.assertEqual(history["run_id"].tolist(), ["AI-DIRECTOR-original", "weekly-one"])
        self.assertEqual(history.iloc[0][self.prior.columns].to_dict(), self.prior.iloc[0].to_dict())

    def test_stale_input_cannot_remove_new_adoptions_or_revert_existing_metadata(self):
        latest = self.themes.copy()
        latest.loc[0, "copy"] = "다른 화면에서 수정한 최신 카피"
        latest.loc[0, "operator_note"] = "최신 운영 메모"
        latest = pd.concat([latest, pd.DataFrame([{
            "theme_id": "T099", "theme_name": "생성 중 별도로 채택한 테마",
            "approved_status": "ADOPTED", "director_adoption_key": "adoption-latest",
            "new_custom_column": "새 채택의 추가 정보",
        }])], ignore_index=True).fillna("")
        latest.to_csv(self.theme_path, sep="|", index=False)
        stale_input = pd.concat([self.themes, pd.DataFrame([{
            "theme_id": "T075", "theme_name": "입력에만 포함된 기존 테마",
        }])], ignore_index=True).fillna("")
        updated, _, added = self.persist("현재 이슈의 새로운 주간 테마", "weekly-one", stale_input)
        self.assertEqual(set(updated["theme_id"]), {"T041", "T075", "T099", "T100"})
        for old in latest.to_dict("records"):
            current = updated[updated["theme_id"].eq(old["theme_id"])].iloc[0]
            self.assertEqual(current[latest.columns].to_dict(), old)
        self.assertEqual(added[0]["theme_id"], "T100")

    def test_corrupt_history_is_rejected_before_either_saved_file_is_overwritten(self):
        self.history_path.write_bytes(b'run_id|theme_id\n"unterminated')
        before = {path: path.read_bytes() for path in (self.theme_path, self.history_path)}
        with self.assertRaises(ThemeGenerationError):
            self.persist("저장되면 안 되는 새 테마", "weekly-one")
        self.assertEqual({path: path.read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
