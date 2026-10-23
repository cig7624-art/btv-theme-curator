"""Editorial rules must preserve evocative themes and reject collection reports."""
from copy import deepcopy
import unittest

from theme_generation_rules import title_review


class ThemeGenerationRuleTests(unittest.TestCase):
    def test_intuitive_topics_and_viewing_occasions_remain_eligible(self):
        for name in [
            "가족이라는 미스터리", "가을밤 온 가족이 모여 앉아",
            "비 오는 날 생각나는 추억의 영화", "베테랑과 비슷한 액션 스릴러",
            "이혼 후 다시 시작하는 로맨스", "복수에 인생을 건 여자들",
            "복수와 사랑 사이, 위험한 선택", "부모도 몰랐던 아이의 비밀",
            "잠들기 아까운 밤의 공포영화", "여름방학, 아이와 떠나는 모험",
        ]:
            with self.subTest(name=name):
                review = title_review({"theme_name": name})
                self.assertTrue(review["eligible"])
                self.assertGreaterEqual(review["clarity"], 70)

    def test_metric_reports_cannot_pass_with_rich_metadata(self):
        for name in [
            "OTT 공개 후 클립 조회가 늘어난 콘텐츠", "조회수 급증한 예고편",
            "검색량이 상승한 드라마", "시청률 상위 영화 모음", "조회수 100만 회 영화",
            "댓글 수가 많아진 영상", "선공개 반응을 모은 드라마", "인기 순위 추이 분석",
            "OTT 공개 후 역주행한 영화", "OTT 공개 후 클립 반응을 얻은 콘텐츠",
            "넷플릭스 공개 뒤 다시 주목받은 작품", "재개봉 후 입소문을 얻은 영화",
        ]:
            with self.subTest(name=name):
                review = title_review({"theme_name": name, "genre": "스릴러",
                                       "copy": "특별한 만남", "selection_rule": "복수하는 여성이 등장하는 작품"})
                self.assertFalse(review["eligible"])
                self.assertEqual(review["reason"], "observation_report")

    def test_publicity_without_a_grouping_topic_is_not_a_theme(self):
        for name in ["화제를 모은 작품", "요즘 인기 콘텐츠", "이번 주 화제의 드라마",
                     "주목받는 작품들", "지금 핫한 영화", "영화 추천", "이번 주 추천작"]:
            with self.subTest(name=name):
                self.assertFalse(title_review({"theme_name": name})["eligible"])

    def test_single_axis_must_not_be_rejected_as_a_plot_checklist(self):
        for name in ["이혼 후 복수하는 여자들", "도망친 뒤 사랑을 찾은 사람들",
                     "이혼과 복수로 시작한 로맨스", "복수에 인생을 건 여자들"]:
            with self.subTest(name=name):
                self.assertTrue(title_review({"theme_name": name})["eligible"])
        for name in ["이혼 뒤 새 사랑과 복수를 한 주인공",
                     "납치 후 도망하고 복수를 하는 여자들"]:
            with self.subTest(name=name):
                review = title_review({"theme_name": name})
                self.assertFalse(review["eligible"])
                self.assertEqual(review["reason"], "plot_checklist")

    def test_metrics_can_be_a_story_subject_without_being_a_traffic_report(self):
        for name in ["조회수에 인생을 건 인플루언서들", "검색량 뒤에 숨은 범죄 스릴러",
                     "시청률 전쟁, 방송국 사람들"]:
            with self.subTest(name=name):
                self.assertTrue(title_review({"theme_name": name})["eligible"])

    def test_distribution_signal_does_not_erase_an_actual_story_topic_or_period(self):
        for name in ["역주행을 꿈꾸는 아이돌", "OTT 공개 후 역주행을 꿈꾸는 아이돌",
                     "OTT 공개 후 다시 뜬 90년대 영화", "재개봉 후 주목받은 세기말 영화",
                     "넷플릭스 공개 뒤 다시 주목받은 가족 영화"]:
            with self.subTest(name=name):
                self.assertTrue(title_review({"theme_name": name})["eligible"])

    def test_evocative_title_can_use_context_without_becoming_a_summary(self):
        candidate = {"theme_name": "어른이 된다는 것", "genre": "드라마",
                     "copy": "늦게 배운 마음의 모양", "selection_rule": "성인이 된 후 성장통을 겪는 인물의 이야기",
                     "reference_titles": ["벌새", "소공녀"]}
        before = deepcopy(candidate)
        result = title_review(candidate)
        self.assertTrue(result["eligible"])
        self.assertEqual(candidate, before)
        self.assertGreater(result["clarity"], title_review({"theme_name": candidate["theme_name"]})["clarity"])

    def test_short_clear_topic_does_not_lose_to_action_word_count(self):
        short = title_review({"theme_name": "가족이라는 미스터리"})
        wordy = title_review({"theme_name": "복수하고 싸우고 되찾고 돌아오는 범죄 영화"})
        self.assertGreaterEqual(short["clarity"], wordy["clarity"])

    def test_missing_name_and_non_content_punctuation_are_rejected(self):
        for candidate in [None, {}, {"theme_name": None}, {"theme_name": "nan"},
                          {"theme_name": "..."}, {"theme_name": ""}]:
            with self.subTest(candidate=candidate):
                self.assertFalse(title_review(candidate)["eligible"])


if __name__ == "__main__":
    unittest.main()
