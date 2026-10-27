"""The title lookup fetches only on submission and keeps source scope visible."""

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from streamlit.testing.v1 import AppTest

from ott_title_search_view import title_availability_html


CANDIDATE = {"id": "104", "title": "기생충", "year": "2019", "content_type": "movie",
             "source_url": "https://m.kinolights.com/title/104", "fetched_at": "2026-10-26T01:03:00Z"}
AVAILABLE = {**CANDIDATE, "status": "available", "providers": [
    {"name": "TVING", "offer_type": "subscription", "url": "https://www.tving.com/contents/P104"},
    {"name": "wavve", "offer_type": "rent", "url": "https://www.wavve.com/player/movie?movieid=104"},
    {"name": "Apple TV", "offer_type": "buy", "url": "https://tv.apple.com/kr/movie/104"},
]}


def app():
    return AppTest.from_string("from ott_title_search_view import render_ott_title_search\nrender_ott_title_search()", default_timeout=10).run()


def markup(app):
    return " ".join(item.value for item in app.markdown)


class TitleAvailabilityHtmlTests(unittest.TestCase):
    def test_confirmed_offers_keep_actual_links_types_and_source_time(self):
        soup = BeautifulSoup(title_availability_html(AVAILABLE), "html.parser")
        self.assertEqual(soup.h3.get_text(), "기생충 (2019) · 영화")
        self.assertEqual([chip.get_text() for chip in soup.select(".ott-title-service")], ["TVING · 구독 ↗", "wavve · 대여 ↗", "Apple TV · 구매 ↗"])
        self.assertEqual(soup.select_one(".ott-title-service")["href"], "https://www.tving.com/contents/P104")
        self.assertIn("10.26 10:03 KST 확인", soup.get_text())
        self.assertEqual(soup.select_one(".ott-title-lookup-meta a")["href"], CANDIDATE["source_url"])

    def test_unknown_offer_does_not_claim_subscription_or_make_up_a_link(self):
        rendered = title_availability_html({**AVAILABLE, "providers": [{"name": "TVING", "offer_type": "unknown", "url": ""}]})
        soup = BeautifulSoup(rendered, "html.parser")
        self.assertEqual(soup.select_one(".ott-title-service").name, "span")
        self.assertEqual(soup.select_one(".ott-title-service").get_text(), "TVING")
        self.assertNotIn("구독", rendered)

    def test_unavailable_unknown_failure_and_identity_mismatch_remain_distinct(self):
        messages = {
            "unavailable": "키노라이츠에 표시된 시청 서비스가 없습니다.",
            "unknown": "시청 서비스 정보를 확인하지 못했습니다.",
            "fetch_error": "시청 정보를 불러오지 못했습니다.",
            "identity_mismatch": "선택한 작품과 원문 정보가 달라",
            "available": "시청 서비스 정보를 확인하지 못했습니다.",
        }
        for status, message in messages.items():
            with self.subTest(status=status):
                rendered = title_availability_html({**CANDIDATE, "status": status, "providers": []})
                self.assertIn(message, rendered)
                self.assertNotIn("ott-title-service\"", rendered)
                if status != "unavailable":
                    self.assertNotIn("시청 서비스가 없습니다", rendered)

    def test_source_markup_and_links_are_safe(self):
        malicious = '"><script>alert(1)</script>'
        rendered = title_availability_html({"title": malicious, "status": "available", "source_url": "https://kinolights.com.evil.test/title/1", "providers": [
            {"name": malicious, "url": "javascript:alert(1)"},
            {"name": "Bad", "url": "https://name:password@example.com/"},
        ]})
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("password@", rendered)
        self.assertNotIn("kinolights.com.evil", rendered)


class OttTitleSearchInteractionTests(unittest.TestCase):
    def test_one_exact_title_among_partial_matches_resolves_on_the_search_submit(self):
        partials = [{**CANDIDATE, "id": str(index), "title": "기생충 이야기 " + str(index)} for index in range(19)]
        with patch("ott_title_search_view._search_titles", return_value=[*partials, CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input[0].set_value(" 기생충 ")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            search.assert_called_once_with("기생충")
            lookup.assert_called_once_with(CANDIDATE)
            self.assertEqual(len(current.selectbox), 0)
            self.assertIn("TVING · 구독", markup(current))
            current.run()
            lookup.assert_called_once()

    def test_exact_title_normalization_handles_unicode_spacing_and_case(self):
        exact = {**CANDIDATE, "title": "Ｍｏｖｉｅ  NAME"}
        partial = {**CANDIDATE, "title": "Movie Name sequel"}
        with patch("ott_title_search_view._search_titles", return_value=[partial, exact]), patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input[0].set_value("movie name")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            lookup.assert_called_once_with(exact)

    def test_without_an_exact_title_all_partial_matches_remain_selectable(self):
        candidates = [{**CANDIDATE, "title": title} for title in ("기생충 이야기", "기생충의 세계")]
        with patch("ott_title_search_view._search_titles", return_value=candidates), patch("ott_title_search_view._get_title_availability") as lookup:
            current = app()
            current.text_input[0].set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            self.assertEqual(current.selectbox[0].options, ["기생충 이야기 (2019) · 영화", "기생충의 세계 (2019) · 영화"])
            lookup.assert_not_called()

    def test_renders_and_typing_never_lookup_and_a_single_submit_keeps_result(self):
        with patch("ott_title_search_view._search_titles", return_value=[CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            self.assertFalse(current.exception)
            current.run()
            current.text_input(key="ott_title_search_query").set_value("기생충").run()
            search.assert_not_called()
            lookup.assert_not_called()
            current.text_input(key="ott_title_search_query").set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            search.assert_called_once_with("기생충")
            lookup.assert_called_once_with(CANDIDATE)
            self.assertIn("TVING · 구독", markup(current))
            current.run()
            search.assert_called_once()
            lookup.assert_called_once()
            self.assertIn("TVING · 구독", markup(current))

    def test_multiple_matches_require_explicit_title_and_year_selection(self):
        older = {**CANDIDATE, "id": "103", "year": "1982", "source_url": "https://m.kinolights.com/title/103"}
        partial = {**CANDIDATE, "id": "101", "title": "기생충 이야기"}
        with patch("ott_title_search_view._search_titles", return_value=[partial, older, CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input[0].set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            lookup.assert_not_called()
            self.assertEqual(current.selectbox[0].options, ["기생충 (1982) · 영화", "기생충 (2019) · 영화"])
            current.button(key="ott_title_choice_submit").click().run()
            self.assertIn("작품을 선택해 주세요", markup(current))
            lookup.assert_not_called()
            current.selectbox[0].select_index(1).run()
            lookup.assert_not_called()
            current.selectbox[0].select_index(1)
            current.button(key="ott_title_choice_submit").click().run()
            self.assertFalse(current.exception)
            lookup.assert_called_once_with(CANDIDATE)
            search.assert_called_once()
            current.run()
            lookup.assert_called_once()

    def test_empty_query_no_matches_and_search_failure_have_different_copy(self):
        with patch("ott_title_search_view._search_titles", return_value=[]) as search, patch("ott_title_search_view._get_title_availability") as lookup:
            current = app()
            current.button(key="ott_title_search_submit").click().run()
            self.assertIn("작품 제목을 입력", markup(current))
            search.assert_not_called()
            current.text_input[0].set_value("없는 제목")
            current.button(key="ott_title_search_submit").click().run()
            self.assertIn("일치하는 작품을 찾지 못했습니다", markup(current))
            search.side_effect = RuntimeError("network failed")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            self.assertIn("검색 결과를 불러오지 못했습니다", markup(current))
            self.assertNotIn("일치하는 작품을 찾지 못했습니다", markup(current))
            lookup.assert_not_called()

    def test_new_search_clears_previous_availability_and_lookup_failure_is_honest(self):
        with patch("ott_title_search_view._search_titles", return_value=[CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input[0].set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertIn("TVING · 구독", markup(current))
            search.return_value = []
            current.text_input[0].set_value("다른 작품")
            current.button(key="ott_title_search_submit").click().run()
            self.assertNotIn("TVING · 구독", markup(current))
            search.return_value = [CANDIDATE]
            lookup.side_effect = RuntimeError("source failed")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            self.assertIn("시청 정보를 불러오지 못했습니다", markup(current))
            self.assertNotIn("시청 서비스가 없습니다", markup(current))


if __name__ == "__main__":
    unittest.main()
