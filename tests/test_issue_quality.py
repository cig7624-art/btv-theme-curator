import json
import unittest

import pandas as pd

from issue_quality import assess_issues, build_ranked_issues, YOUTUBE, BOXOFFICE, OTT, NEWS


NOW = "2026-10-21T12:00:00+09:00"


def issue(**changes):
    row = dict(date="2026-10-21", source="온라인 화제·뉴스", issue_title="'실낙원' 21일 개봉", related_content="실낙원", keywords="영화,개봉", description="연상호 신작 영화 실낙원 21일 개봉", source_url="https://news.example.com/story/123", image_url="")
    row.update(changes)
    return row


def youtube(**changes):
    row = issue(source="YouTube 반응", source_type="youtube_reaction", related_content="닥터X", issue_title="[닥터X] 공식 예고편", source_url="https://www.youtube.com/watch?v=abc12345678", published_at="2026-10-20", description="공개 2일 기준 조회수 120,000회, 댓글 500개.")
    row.update(changes)
    return row


class IssueQualityTests(unittest.TestCase):
    def assessed(self, row):
        return assess_issues(pd.DataFrame([row]), now=NOW).iloc[0]

    def test_preserves_input_rows_index_and_unknown_fields(self):
        frame = pd.DataFrame([issue(), issue(related_content="작품")], index=[41, 78])
        frame["_audit_row_id"] = [1, 2]
        result = assess_issues(frame, now=NOW)
        self.assertEqual(result.index.tolist(), [41, 78])
        self.assertEqual(result["_audit_row_id"].tolist(), [1, 2])
        self.assertEqual(len(result), 2)

    def test_empty_schema_and_grouping(self):
        result = assess_issues(pd.DataFrame(), now=NOW)
        self.assertIn("eligible", result)
        self.assertTrue(build_ranked_issues(result).empty)

    def test_english_netflix_is_ott_and_old_week_is_rejected(self):
        row = issue(source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/unsupportedbrowser", description="한국 1위. 카테고리: Films. 집계 주간: 2026-08-16.")
        result = self.assessed(row)
        self.assertEqual(result.source_group, OTT)
        self.assertFalse(result.eligible)
        self.assertIn("집계 기준일이 오래됨", result.exclusion_reason)
        self.assertIn("/tudum/top10/", result.source_url)
        self.assertIn("재대조 전", result.quality_notes)

    def test_current_netflix_reference_and_no_false_live_verification(self):
        result = self.assessed(issue(source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/unsupportedbrowser", description="한국 1위. 카테고리: Films. 집계 주간: 2026-10-18."))
        self.assertTrue(result.eligible)
        self.assertEqual(result.sort_priority, 3)
        self.assertIn("원문 재대조 전", result.evidence_status)
        self.assertIn("2026-10-18", result.reference_label)

    def test_weekly_grace_is_not_the_daily_window(self):
        row = issue(date="2026-10-21", source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/tudum/top10/south-korea", description="한국 2위. 집계 주간: 2026-10-12.")
        self.assertTrue(self.assessed(row).eligible)
        row["description"] = "한국 2위. 집계 주간: 2026-10-07."
        self.assertFalse(self.assessed(row).eligible)

    def test_reference_conflict_requires_review(self):
        row = issue(source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/tudum/top10/south-korea", reference_date="2026-10-21", description="한국 2위. 집계 주간: 2026-10-18.")
        self.assertIn("불일치", self.assessed(row).exclusion_reason)

    def test_netflix_missing_actual_week_is_not_renewed_by_collection(self):
        result = self.assessed(issue(source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/tudum/top10/south-korea", description="한국 1위"))
        self.assertFalse(result.eligible)
        self.assertIn("집계주 미확인", result.exclusion_reason)

    def test_news_cannot_impersonate_official_ott(self):
        result = self.assessed(issue(source="OTT 공식 신작", source_type="official_announcement", related_content="비전퀘스트", source_url="https://www.wikitree.co.kr/articles/123", issue_title="'비전퀘스트' 공개 예정"))
        self.assertEqual(result.source_group, NEWS)
        self.assertEqual(result.source, "OTT 관련 뉴스")
        self.assertNotEqual(result.evidence_status, "공식 출처 기록")

    def test_generic_news_metadata_is_not_a_confirmed_event(self):
        result = self.assessed(issue(related_content="실낙원", issue_title="실낙원 이야기", description="실낙원에 관한 생각", event_type="content_news"))
        self.assertFalse(result.eligible)
        self.assertIn("사건", result.exclusion_reason)

    def test_kobis_uses_real_target_date_and_daily_counts(self):
        row = issue(source="KOBIS 박스오피스", related_content="오디세이", source_url="https://www.kobis.or.kr/kobis/business/stat/boxs/findDailyBoxOfficeList.do", description="2026-10-20 KOBIS 일별 박스오피스 4위. 일일 관객 12,345명, 누적 관객 300,000명.")
        result = self.assessed(row)
        self.assertTrue(result.eligible)
        self.assertEqual(result.source_group, BOXOFFICE)
        self.assertIn("12,345", result.metric_summary)
        self.assertIn("2026-10-20", result.reference_label)
        row["description"] = row["description"].replace("2026-10-20", "2026-10-01")
        self.assertFalse(self.assessed(row).eligible)

    def test_recent_seven_dates_and_future_are_explicit(self):
        self.assertTrue(self.assessed(issue(date="2026-10-15")).eligible)
        self.assertFalse(self.assessed(issue(date="2026-10-14")).eligible)
        self.assertFalse(self.assessed(issue(date="2026-10-22")).eligible)

    def test_timezone_uses_kst_calendar(self):
        row = issue(date="2026-10-14", published_at="2026-10-14T16:00:00Z")
        self.assertTrue(self.assessed(row).eligible)

    def test_generic_titles_and_game_music_are_excluded(self):
        for row in [youtube(related_content="영화 역사상 가장 감동적인", issue_title="영화 역사상 가장 감동적인 관련 YouTube 화제 영상"), youtube(related_content="MV", issue_title="[MV] 노래", description="Official MV 조회수 9,000,000회"), youtube(related_content="팡이요, 메이플스토리", issue_title="메이플스토리 게임 방송"), youtube(related_content="1위", issue_title="1위")]:
            with self.subTest(title=row["related_content"]):
                self.assertFalse(self.assessed(row).eligible)

    def test_game_adaptation_film_is_not_excluded(self):
        result = self.assessed(youtube(related_content="사이버펑크: 엣지러너 2", issue_title="사이버펑크: 엣지러너 2 공식 애니메이션 예고편", description="게임 사이버펑크를 원작으로 한 애니. 공개 2일 기준 조회수 120,000회, 댓글 500개."))
        self.assertTrue(result.eligible)

    def test_explicit_titles_recover_caption_and_news_generic(self):
        result = self.assessed(youtube(related_content="신체 장기에 낙서를 한 인간쓰레기... ≪닥터X≫ 3~4화", issue_title="≪닥터X≫ 3~4화 요약"))
        self.assertEqual(result.related_content, "닥터X")
        self.assertTrue(result.eligible)
        result = self.assessed(issue(related_content="인터뷰①", issue_title="'실낙원' 배현성 감독 인터뷰①"))
        self.assertEqual(result.related_content, "실낙원")

    def test_only_explicit_aliases_merge_not_arbitrary_translations(self):
        a = self.assessed(youtube(related_content="Doctor X: Mafia in White", issue_title="Doctor X 예고편"))
        b = self.assessed(youtube())
        self.assertEqual(a.issue_key, b.issue_key)
        self.assertNotEqual(self.assessed(issue(related_content="The Eyes", issue_title="The Eyes 공개")).issue_key, self.assessed(issue(related_content="눈동자", issue_title="눈동자 공개")).issue_key)

    def test_initial_youtube_metrics_are_not_growth(self):
        result = self.assessed(youtube(description="공개 2일 기준 조회수 12,000,000회, 댓글 40,000개. 일평균 조회수 6,000,000회."))
        self.assertTrue(result.eligible)
        self.assertEqual(result.sort_priority, 1)
        self.assertIn("증가 미확인", result.strength_label)
        self.assertIn("누적", result.metric_summary)

    def test_observed_change_requires_two_dated_observations(self):
        metrics = dict(views_total=140000, comments_total=500, views_change=20000, observation_start="2026-10-20", observation_end="2026-10-21", observation_days=1, observation_precision="date")
        result = self.assessed(youtube(metrics_json=json.dumps(metrics)))
        self.assertEqual(result.sort_priority, 4)
        self.assertIn("정확한 24시간 증가 아님", result.metric_summary)
        del metrics["observation_start"]
        result = self.assessed(youtube(metrics_json=json.dumps(metrics)))
        self.assertEqual(result.sort_priority, 1)

    def test_negative_change_is_not_growth(self):
        metrics = dict(views_total=100000, views_change=-100, observation_start="2026-10-20", observation_end="2026-10-21")
        self.assertEqual(self.assessed(youtube(metrics_json=metrics)).sort_priority, 1)

    def test_small_observed_increase_does_not_outrank_official_rank(self):
        metrics = dict(views_total=100000, views_change=1000, observation_start="2026-10-20", observation_end="2026-10-21")
        result = self.assessed(youtube(metrics_json=metrics))
        self.assertEqual(result.sort_priority, 1)
        self.assertIn("참고", result.strength_label)
        self.assertIn("+1,000", result.metric_summary)
        metrics["views_change"] = 0
        result = self.assessed(youtube(metrics_json=metrics))
        self.assertIn("증가 없음", result.strength_label)

    def test_promotional_video_uses_promoted_work_not_format_name(self):
        result = self.assessed(youtube(related_content="홍.건.데", issue_title="홍.건.데 화제 영상", description="'[홍.건.데] 이 조합 | 100일의 거짓말 홍보하러 온 건 맞는데' 영상. 조회수 100,000회. 채널: Netflix Korea."))
        self.assertEqual(result.related_content, "100일의 거짓말")

    def test_channel_bracket_is_not_a_work(self):
        result = self.assessed(youtube(related_content="ALTUBE", issue_title="복수 의뢰가 들어왔습니다", description="조회수 100,000회. 채널: [ALTUBE] 김성현TV."))
        self.assertFalse(result.eligible)
        self.assertEqual(result.related_content, "")

    def test_invalid_metrics_and_urls_are_not_success(self):
        self.assertFalse(self.assessed(youtube(description="반응 좋음", metrics_json="broken JSON")).eligible)
        self.assertFalse(self.assessed(issue(source_url="https://news.example.com/404")).eligible)
        self.assertFalse(self.assessed(issue(source_url="javascript:alert(1)")).eligible)
        self.assertFalse(self.assessed(issue(source_url="https://news.google.com/rss/articles/id")).eligible)

    def test_same_video_duplicates_do_not_increase_priority_or_evidence(self):
        a = youtube()
        b = dict(a, source_url="https://youtu.be/abc12345678?utm_source=test")
        result = build_ranked_issues(assess_issues(pd.DataFrame([a, b, a, a]), now=NOW))
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0].issue_count, 1)
        self.assertEqual(len(result.iloc[0].evidence_records), 1)
        self.assertEqual(result.iloc[0].sort_priority, 1)

    def test_identical_news_headlines_do_not_become_multiple_confirmations(self):
        a = issue(issue_title="'실낙원' 21일 개봉 - 신문A")
        b = issue(issue_title="'실낙원' 21일 개봉 - 신문B", source_url="https://another.example.org/article/456")
        result = build_ranked_issues(assess_issues(pd.DataFrame([a, b]), now=NOW)).iloc[0]
        self.assertEqual(result.confirmed_route_count, 1)
        self.assertEqual(result.issue_count, 1)

    def test_real_measurement_precedes_initial_cumulative_count(self):
        huge = youtube(related_content="초대형영화", issue_title="[초대형영화] 예고편", description="공개 2일 기준 조회수 900,000,000회, 댓글 100,000개.")
        observed = youtube(related_content="새로운작품", issue_title="[새로운작품] 예고편", source_url="https://youtu.be/other123456", metrics_json=dict(views_total=200000, views_change=50000, observation_start="2026-10-20", observation_end="2026-10-21"))
        result = build_ranked_issues(assess_issues(pd.DataFrame([huge, observed]), now=NOW))
        self.assertEqual(result.iloc[0].related_content, "새로운작품")

    def test_latest_official_rank_replaces_older_better_rank(self):
        base = issue(source="Netflix Top 10", related_content="The Eyes", source_url="https://www.netflix.com/tudum/top10/south-korea")
        rows = [dict(base, description="한국 1위. 집계 주간: 2026-10-12."), dict(base, description="한국 8위. 집계 주간: 2026-10-19.")]
        result = build_ranked_issues(assess_issues(pd.DataFrame(rows), now=NOW)).iloc[0]
        self.assertIn("8위", result.metric_summary)
        self.assertEqual(result.issue_count, 1)

    def test_latest_video_observation_replaces_older_larger_increase(self):
        base = youtube(published_at="2026-10-18")
        old = dict(base, collected_at="2026-10-20", metrics_json={"views_total": 200000, "views_change": 100000, "observation_start": "2026-10-19", "observation_end": "2026-10-20"})
        new = dict(base, collected_at="2026-10-21", metrics_json={"views_total": 201000, "views_change": 1000, "observation_start": "2026-10-20", "observation_end": "2026-10-21"})
        result = build_ranked_issues(assess_issues(pd.DataFrame([old, new]), now=NOW)).iloc[0]
        self.assertIn("+1,000", result.metric_summary)
        self.assertNotIn("+100,000", result.metric_summary)
        self.assertEqual(result.issue_count, 1)

    def test_older_snapshot_entries_are_not_current_even_when_work_drops_out(self):
        base = issue(source="KOBIS 박스오피스", source_url="https://www.kobis.or.kr/kobis/business/stat/boxs/findDailyBoxOfficeList.do")
        rows = [dict(base, related_content="지난작품", issue_title="지난작품 1위", description="2026-10-19 KOBIS 일별 박스오피스 1위. 일일 관객 50,000명."), dict(base, related_content="오늘작품", issue_title="오늘작품 1위", description="2026-10-20 KOBIS 일별 박스오피스 1위. 일일 관객 60,000명.")]
        result = assess_issues(pd.DataFrame(rows), now=NOW)
        self.assertFalse(result.iloc[0].eligible)
        self.assertIn("이전 집계", result.iloc[0].exclusion_reason)
        self.assertTrue(result.iloc[1].eligible)

    def test_seasons_and_parts_remain_distinct(self):
        base = issue(source="Netflix Top 10", related_content="기묘한 이야기", source_url="https://www.netflix.com/tudum/top10/south-korea", description="한국 1위. 카테고리: TV. 집계 주간: 2026-10-19.")
        rows = [dict(base, issue_title="기묘한 이야기: Season 1", metrics_json={"season": "1", "rank": 1, "category": "TV"}), dict(base, issue_title="기묘한 이야기: Season 2", metrics_json={"season": "2", "rank": 7, "category": "TV"})]
        result = build_ranked_issues(assess_issues(pd.DataFrame(rows), now=NOW))
        self.assertEqual(len(result), 2)
        self.assertEqual(result.issue_key.nunique(), 2)
        self.assertEqual(set(result.related_content), {"기묘한 이야기 시즌 1", "기묘한 이야기 시즌 2"})

    def test_sports_variety_program_is_not_generic_sports(self):
        result = self.assessed(youtube(related_content="신인감독 김연경 시즌2", issue_title="[신인감독 김연경 시즌2] 경기의 흐름을 바꾸는 장면"))
        self.assertTrue(result.eligible)
        self.assertNotIn("일반 스포츠", result.exclusion_reason)

    def test_legacy_news_record_date_is_not_claimed_as_verified_publication(self):
        result = self.assessed(issue())
        self.assertIn("발행일 미대조", result.reference_label)

    def test_evidence_records_and_image_enrichment_preserve_original_url(self):
        news = issue()
        yt = youtube(related_content="실낙원", issue_title="[실낙원] 예고편", image_url="https://i.ytimg.com/vi/abc12345678/hqdefault.jpg")
        result = build_ranked_issues(assess_issues(pd.DataFrame([news, yt]), now=NOW)).iloc[0]
        self.assertEqual(result.source_url, news["source_url"])
        self.assertEqual(result.image_url, yt["image_url"])
        self.assertEqual(result.confirmed_route_count, 2)
        self.assertEqual(len(result.evidence_records), 2)
        for evidence in result.evidence_records:
            self.assertTrue({"title", "url", "source", "reference_label", "metric_summary", "evidence_status"}.issubset(evidence))


if __name__ == "__main__":
    unittest.main()
