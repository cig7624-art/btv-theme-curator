"""Theme artwork and links must preserve real titles and stored issue origins."""
import copy
import unittest

import pandas as pd

from theme_editorial import linked_issue_for_theme, theme_card_html


def recommendation(**changes):
    result = {
        "theme": {"theme_name": "거짓 신분으로 시작된 관계", "copy": "관계를 흔드는 비밀", "genre": "드라마", "mood": "긴장감", "source_issue": "‘100일의 거짓말’ YouTube 영상 반응에서 발견한 테마"},
        "source_issue_summary": "‘100일의 거짓말’ YouTube 영상 반응에서 발견한 테마",
        "rationale": "정체를 숨긴 사람들의 관계와 균열을 따라갑니다.",
        "recommended_at": "2026-10-22T07:00:00+09:00",
        "contents": [],
    }
    result.update(changes)
    return result


def issue(**changes):
    result = dict(related_content="100일의 거짓말", source="YouTube 반응", source_group="YouTube 반응", source_type="youtube_reaction", issue_title="[100일의 거짓말] 공식 예고편", source_url="https://www.youtube.com/watch?v=abcdef12345", date="2026-10-21", collected_at="2026-10-21T08:00:00+09:00", quality_status="candidate")
    result.update(changes)
    return result


def content(number=0, **changes):
    row = dict(title=f"실제 작품 {number}", type="영화", year="2020", source_url=f"https://m.kinolights.com/title/{number}", poster_url=f"https://images.example.com/poster/{number}.jpg", reason="신분을 숨긴 주인공의 이야기")
    row.update(changes)
    return row


class ThemeEditorialTests(unittest.TestCase):
    def test_card_order_preserves_issue_name_reason_and_real_poster_titles(self):
        rec = recommendation(contents=[content(1), content(2)])
        rendered = theme_card_html(1, rec, pd.DataFrame([issue()]))
        self.assertLess(rendered.index("연결된 이슈"), rendered.index("거짓 신분으로 시작된 관계"))
        self.assertLess(rendered.index("거짓 신분으로 시작된 관계"), rendered.index("정체를 숨긴 사람들"))
        self.assertLess(rendered.index("정체를 숨긴 사람들"), rendered.index("실제 작품 1"))
        self.assertIn('href="https://www.youtube.com/watch?v=abcdef12345"', rendered)
        self.assertIn('background-image:url(&quot;https://images.example.com/poster/1.jpg&quot;)', rendered)
        self.assertIn('role="img" aria-label="실제 작품 1 포스터"', rendered)
        self.assertIn("추천 작품 <strong>2편", rendered)

    def test_unknown_artwork_uses_title_placeholder_and_never_an_invented_url(self):
        rendered = theme_card_html(2, recommendation(contents=[content(poster_url="")]))
        self.assertIn("theme-poster-placeholder", rendered)
        self.assertIn("실제 작품 0", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("images.example.com", rendered)

    def test_artwork_failure_keeps_a_visible_title_without_event_handlers(self):
        rendered = theme_card_html(1, recommendation(contents=[content(1)]))
        self.assertIn('<span class="theme-poster-placeholder" aria-hidden="true"><span>실제 작품 1</span>', rendered)
        self.assertIn('class="theme-poster-image"', rendered)
        self.assertLess(rendered.index('class="theme-poster-placeholder"'), rendered.index('class="theme-poster-image"'))
        self.assertNotIn("display:none", rendered)
        self.assertNotIn("onerror", rendered)
        self.assertNotIn("<img", rendered)

    def test_poster_url_cannot_escape_quoted_css_or_html_attributes(self):
        malicious = 'https://images.example.com/a\");color:red;background:url(https://evil.example/x)'
        rendered = theme_card_html(1, recommendation(contents=[content(poster_url=malicious)]))
        self.assertIn('%22%29', rendered)
        self.assertIn('%28https://evil.example/x%29', rendered)
        self.assertNotIn('a\");color:red', rendered)
        self.assertNotIn('url(https://evil.example', rendered)

    def test_more_than_five_candidates_keep_all_accessible_in_native_details(self):
        rendered = theme_card_html(1, recommendation(contents=[content(number) for number in range(8)]))
        self.assertEqual(rendered.count('class="theme-poster-tile"'), 5)
        self.assertIn('<details class="theme-more-candidates">', rendered)
        self.assertIn("전체 8편 보기", rendered)
        self.assertEqual(rendered.count("<li>"), 8)
        self.assertIn('href="https://m.kinolights.com/title/7"', rendered)

    def test_exactly_five_candidates_are_visible_without_a_redundant_expander(self):
        rendered = theme_card_html(1, recommendation(contents=[content(number) for number in range(5)]))
        self.assertEqual(rendered.count('class="theme-poster-tile"'), 5)
        self.assertIn("추천 작품 <strong>5편", rendered)
        self.assertNotIn('<details class="theme-more-candidates">', rendered)

    def test_duplicate_same_work_and_year_does_not_inflate_count(self):
        rendered = theme_card_html(1, recommendation(contents=[content(1), content(1)]))
        self.assertIn("추천 작품 <strong>1편", rendered)
        self.assertEqual(rendered.count('class="theme-poster-tile"'), 1)

    def test_empty_candidates_offer_the_actual_fill_action_without_fake_posters(self):
        rendered = theme_card_html(1, recommendation())
        self.assertIn("콘텐츠 채우기", rendered)
        self.assertNotIn("<img", rendered)
        self.assertNotIn("16편", rendered)
        self.assertNotIn("이슈 보기", rendered)

    def test_summary_source_prevents_linking_to_different_provider(self):
        rows = [issue(), issue(source="Netflix Top 10", source_group="OTT 랭킹·신작", source_type="ott_ranking", source_url="https://www.netflix.com/tudum/top10", collected_at="2026-10-21T09:00:00+09:00")]
        linked = linked_issue_for_theme(recommendation(), pd.DataFrame(rows))
        self.assertEqual(linked["source_label"], "YouTube")
        self.assertIn("youtube.com", linked["url"])

    def test_future_snapshot_cannot_replace_the_generation_issue(self):
        rows = [issue(), issue(source_url="https://www.youtube.com/watch?v=newer123456", collected_at="2026-10-22T08:00:00+09:00")]
        linked = linked_issue_for_theme(recommendation(), rows)
        self.assertEqual(linked["url"], rows[0]["source_url"])
        only_future = linked_issue_for_theme(recommendation(), rows[1:])
        self.assertIsNone(only_future)

    def test_future_observation_hidden_in_old_row_date_is_not_original_evidence(self):
        row = issue(metrics_json={"observation_end": "2026-10-23T00:00:00+09:00"})
        self.assertIsNone(linked_issue_for_theme(recommendation(), [row]))

    def test_shorter_work_and_other_season_do_not_match_quoted_title(self):
        for title in ["100일", "100일의 거짓말 시즌 2", "거짓말"]:
            with self.subTest(title=title):
                self.assertIsNone(linked_issue_for_theme(recommendation(), [issue(related_content=title)]))

    def test_bare_work_title_with_korean_particle_and_ascii_word_boundaries(self):
        rec = recommendation(source_issue_summary="닥터X가 YouTube에서 반응을 얻었다")
        self.assertIsNotNone(linked_issue_for_theme(rec, [issue(related_content="닥터X")]))
        rec = recommendation(source_issue_summary="With You가 YouTube에서 화제")
        self.assertIsNone(linked_issue_for_theme(rec, [issue(related_content="It")]))

    def test_historical_issue_can_link_even_when_current_window_no_longer_eligible(self):
        row = issue(eligible=False, exclusion_reason="최근 7일 범위 밖")
        self.assertIsNotNone(linked_issue_for_theme(recommendation(), [row]))
        row["quality_status"] = "rejected"
        self.assertIsNone(linked_issue_for_theme(recommendation(), [row]))

    def test_held_unverified_or_invalid_sources_are_not_linked_as_issue_evidence(self):
        rec = recommendation(source_issue_summary="‘100일의 거짓말’ 관련 뉴스")
        for reason, url in [
            ("근거 URL이 오류 또는 로그인 페이지", "https://news.example.com/404"),
            ("뉴스 중계 링크만 있어 원문 미확인", "https://news.google.com/rss/articles/old"),
            ("기사의 실제 발행일을 확인할 수 없음", "https://news.example.com/story"),
            ("최근 7일 범위 밖의 게시물; 기사의 실제 발행일을 확인할 수 없음", "https://news.example.com/story"),
        ]:
            with self.subTest(reason=reason):
                row = issue(source="온라인 화제·뉴스", source_group="온라인 화제·뉴스", source_type="news", eligible=False, exclusion_reason=reason, source_url=url)
                self.assertIsNone(linked_issue_for_theme(rec, [row]))

    def test_only_age_and_superseded_exclusions_can_link_to_historical_records(self):
        for reason in ["최근 7일 범위 밖의 게시물", "집계 기준일이 오래됨", "이전 집계(최신 집계로 대체)", "집계 기준일이 오래됨; 이전 집계(최신 집계로 대체)", ""]:
            with self.subTest(reason=reason):
                self.assertIsNotNone(linked_issue_for_theme(recommendation(), [issue(eligible=False, exclusion_reason=reason)]))

    def test_markup_escapes_untrusted_text_and_rejects_active_or_credentials_urls(self):
        rec = recommendation(theme={"theme_name": '<script>alert("title")</script>'}, source_issue_summary='<iframe src="bad">', rationale='<b>bad</b>', contents=[content(title='<img src=x onerror=alert(1)>', poster_url="javascript:alert(1)", source_url="https://user:pass@example.com/title")])
        rendered = theme_card_html(1, rec)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;iframe", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertNotIn("<img src=x", rendered)

    def test_no_network_or_input_mutation_when_rendering(self):
        rec = recommendation(contents=[content()])
        rows = [issue()]
        before = copy.deepcopy((rec, rows))
        theme_card_html(1, rec, rows)
        self.assertEqual((rec, rows), before)


if __name__ == "__main__":
    unittest.main()
