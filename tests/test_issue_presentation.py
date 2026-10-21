"""User-facing issue copy must explain evidence without changing its meaning."""
import json
import re
import unittest

import pandas as pd

from issue_presentation import present_issue
from issue_quality import assess_issues


NOW = "2026-10-21T12:00:00+09:00"
MAIN_FIELDS = ("source_label", "headline", "reason", "period_label")


def source_row(**changes):
    row = {
        "date": "2026-10-21", "source": "온라인 화제·뉴스",
        "issue_title": "영화 '실낙원' 21일 개봉", "related_content": "실낙원",
        "description": "신작 영화 실낙원이 개봉한다.", "keywords": "영화,개봉",
        "source_url": "https://news.example.org/story/123", "image_url": "",
        "publisher": "영화신문", "published_at": "2026-10-20",
    }
    row.update(changes)
    return row


def youtube_row(metrics, **changes):
    row = source_row(
        source="YouTube 반응", source_type="youtube_reaction", related_content="꿀알바",
        issue_title="[꿀알바] 공개 영상", publisher="Netflix Korea",
        source_url="https://www.youtube.com/watch?v=sample12345",
        published_at="2026-10-16", description="꿀알바 관련 영상의 조회수 기록",
        metrics_json=json.dumps(metrics),
    )
    row.update(changes)
    return row


class IssuePresentationTests(unittest.TestCase):
    def assessed(self, row):
        return assess_issues(pd.DataFrame([row]), now=NOW).iloc[0]

    def main_text(self, view):
        return " ".join(str(view[field]) for field in MAIN_FIELDS)

    def detail_text(self, view):
        return " ".join(str(value) for pair in view["details"] for value in pair)

    def assert_mentions_date(self, text, iso_date):
        year, month, day = iso_date.split("-")
        alternatives = [iso_date, f"{year}.{month}.{day}", f"{month}.{day}", f"{month}/{day}", f"{int(month)}월 {int(day)}일"]
        self.assertTrue(any(value in text for value in alternatives), (text, iso_date))

    def test_growth_card_names_video_metric_and_keeps_exact_evidence_in_details(self):
        row = self.assessed(youtube_row({
            "views_total": 500000, "views_change": 379455, "comments_total": 100,
            "observation_start": "2026-10-16", "observation_end": "2026-10-17",
            "observation_precision": "date",
        }))
        view = present_issue(row)
        self.assertIn("영상", view["headline"])
        self.assertIn("조회", view["headline"])
        self.assertIn("증가", view["headline"])
        self.assertTrue(any(count in view["headline"] for count in ["37.9만", "37만", "379,455", "379455"]))
        self.assertRegex(view["reason"], r"5만|50,?000")
        self.assert_mentions_date(view["period_label"], "2026-10-16")
        self.assert_mentions_date(view["period_label"], "2026-10-17")
        details = self.detail_text(view)
        self.assertRegex(details, r"379,?455")
        self.assert_mentions_date(details, "2026-10-16")
        self.assert_mentions_date(details, "2026-10-17")
        self.assertNotIn("24시간 동안", self.main_text(view))
        self.assertTrue(view["verification_note"])
        self.assertNotIn("검증 완료", view["verification_note"])

    def test_main_copy_has_no_collection_or_scoring_jargon(self):
        row = self.assessed(youtube_row({
            "views_total": 500000, "views_change": 379455,
            "observation_start": "2026-10-16", "observation_end": "2026-10-17",
            "observation_precision": "date",
        }))
        view = present_issue(row)
        for word in ["관측", "미대조", "100점", "sort_priority", "source_type"]:
            self.assertNotIn(word, self.main_text(view))
        self.assertTrue(all(isinstance(pair, (list, tuple)) and len(pair) == 2 for pair in view["details"]))

    def test_initial_observation_is_cumulative_and_never_an_increase(self):
        row = self.assessed(youtube_row({"views_total": 150000, "comments_total": 100, "reaction_basis": "initial"}))
        view = present_issue(row)
        self.assertIn("누적", self.main_text(view))
        for word in ["증가", "급등", "상승"]:
            self.assertNotIn(word, view["headline"])
        self.assertTrue(any(value in self.main_text(view) for value in ["15만", "150,000", "150000"]))

    def test_small_observed_increase_does_not_claim_priority_threshold_was_met(self):
        row = self.assessed(youtube_row({
            "views_total": 150000, "views_change": 1000,
            "observation_start": "2026-10-20", "observation_end": "2026-10-21",
            "observation_precision": "date",
        }))
        self.assertEqual(row.sort_priority, 1)
        view = present_issue(row)
        self.assertNotRegex(view["reason"], r"2만\s*회\s*이상\s*(?:증가한|늘어난)")
        self.assertNotIn("우선 후보", view["reason"])
        self.assertNotIn("급등", self.main_text(view))

    def test_comment_increase_explains_why_low_view_candidates_qualified(self):
        for increase in [0, 1000, 25000]:
            with self.subTest(view_change=increase):
                row = self.assessed(youtube_row({"views_total": 50000, "views_change": increase, "comments_change": 150,
                    "observation_start": "2026-10-20", "observation_end": "2026-10-21"}))
                view = present_issue(row)
                self.assertIn("댓글", view['reason'])
                self.assertIn("100", view['reason'])
                self.assertIn("150", self.detail_text(view))
                if increase < 20000:
                    self.assertIn("댓글", view['headline'])

    def test_comparison_rejected_by_assessment_is_not_presented_as_growth(self):
        row = self.assessed(youtube_row({"views_total": 500000, "views_change": 100000,
            "observation_start": "2026-10-21", "observation_end": "2026-10-22"}))
        self.assertFalse(row.comparison_valid)
        view = present_issue(row)
        self.assertNotIn("증가", view['headline'])
        self.assertNotIn("늘어난", view['reason'])

    def test_percentage_growth_rule_is_explained_when_view_count_is_below_other_threshold(self):
        row = self.assessed(youtube_row({"views_total": 35000, "views_change": 25000, "growth_rate": 2.5,
            "observation_start": "2026-10-20", "observation_end": "2026-10-21"}))
        self.assertIn("200%", present_issue(row)['reason'])

    def test_boxoffice_card_keeps_actual_rank_audience_and_target_day(self):
        row = self.assessed(source_row(
            source="KOBIS 박스오피스", source_type="boxoffice", related_content="오디세이",
            issue_title="오디세이 일일 박스오피스 4위", published_at="",
            reference_date="2026-10-20", reference_period="2026-10-20",
            source_url="https://www.kobis.or.kr/kobis/business/stat/boxs/findDailyBoxOfficeList.do",
            description="2026-10-20 KOBIS 일별 박스오피스 4위. 일일 관객 22,474명.",
            metrics_json=json.dumps({"rank": 4, "daily_audience": 22474}),
        ))
        view = present_issue(row)
        self.assertRegex(view["headline"], r"4\s*위")
        self.assertRegex(self.main_text(view) + " " + self.detail_text(view), r"22,?474")
        self.assert_mentions_date(view["period_label"], "2026-10-20")
        self.assertNotIn("2026-10-21", view["period_label"])
        self.assertRegex(view["reason"], r"10|열")

    def test_netflix_cards_separate_film_and_series_and_use_weekly_dates(self):
        for category, korean in [("Films", "영화"), ("TV", "TV 프로그램")]:
            with self.subTest(category=category):
                row = self.assessed(source_row(
                    source="Netflix Top 10", source_type="ott_ranking", related_content="The Eyes",
                    issue_title="The Eyes 넷플릭스 한국 2위", published_at="",
                    reference_date="2026-10-18", reference_period="2026-10-12/2026-10-18",
                    source_url="https://www.netflix.com/tudum/top10/south-korea",
                    description=f"한국 2위. 카테고리: {category}. 집계 주간: 2026-10-18.",
                    metrics_json=json.dumps({"rank": 2, "category": category, "country": "KR"}),
                ))
                view = present_issue(row)
                self.assertIn("한국", self.main_text(view))
                self.assertIn(korean, self.main_text(view))
                self.assertRegex(view["headline"], r"2\s*위")
                self.assertIn("주간", self.main_text(view))
                self.assert_mentions_date(view["period_label"], "2026-10-12")
                self.assert_mentions_date(view["period_label"], "2026-10-18")
                self.assertNotIn("오늘", view["headline"])

    def test_news_explains_a_dated_event_without_claiming_popularity(self):
        row = self.assessed(source_row(event_type="release"))
        view = present_issue(row)
        self.assertIn("개봉", self.main_text(view))
        self.assert_mentions_date(view["period_label"], "2026-10-20")
        for word in ["급등", "인기", "조회수 증가"]:
            self.assertNotIn(word, self.main_text(view))

    def test_generic_news_metadata_does_not_turn_into_a_popularity_claim(self):
        row = self.assessed(source_row(issue_title="실낙원 이야기", description="실낙원에 관한 생각", event_type="content_news"))
        self.assertFalse(row.eligible)
        view = present_issue(row)
        for word in ["인기", "급등", "개봉 확정", "공개 확정"]:
            self.assertNotIn(word, self.main_text(view))

    def test_mapping_and_series_inputs_produce_same_view(self):
        row = self.assessed(youtube_row({"views_total": 150000, "reaction_basis": "initial"}))
        self.assertEqual(present_issue(row), present_issue(row.to_dict()))


if __name__ == "__main__":
    unittest.main()
