"""Compact title lookup: explicit requests, inline choices, logos and reset."""

import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from streamlit.testing.v1 import AppTest

from ott_title_search_view import title_availability_html


CANDIDATE = {"id": "104", "title": "기생충", "year": "2019", "content_type": "movie",
             "source_url": "https://m.kinolights.com/title/104", "fetched_at": "2026-10-26T01:03:00Z"}
AVAILABLE = {**CANDIDATE, "status": "available", "providers": [
    {"provider_id": "tving", "name": "TVING", "offer_type": "subscription", "url": "https://www.tving.com/contents/P104"},
    {"provider_id": "wavve", "name": "wavve", "offer_type": "rent", "url": "https://www.wavve.com/player/movie?movieid=104"},
    {"provider_id": "apple", "name": "Apple TV", "offer_type": "buy", "url": "https://tv.apple.com/kr/movie/104"},
]}


def app():
    return AppTest.from_string("from ott_title_search_view import render_ott_title_search\nrender_ott_title_search()", default_timeout=10).run()


def markup(app):
    return " ".join(item.value for item in app.markdown)


class TitleAvailabilityHtmlTests(unittest.TestCase):
    def test_confirmed_subscriptions_use_real_service_logos_links_and_source_time(self):
        soup = BeautifulSoup(title_availability_html(AVAILABLE), "html.parser")
        self.assertEqual(soup.h3.get_text(), "기생충 (2019) · 영화")
        services = soup.select(".ott-title-service")
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["data-provider-id"], "tving")
        self.assertEqual(services[0]["href"], "https://www.tving.com/contents/P104")
        self.assertEqual(services[0].img["alt"], "티빙")
        self.assertTrue(services[0].img["src"].startswith("data:image/"))
        self.assertNotIn("대여", soup.get_text())
        self.assertNotIn("구매", soup.get_text())
        self.assertIn("10.26 10:03 KST 확인", soup.get_text())
        self.assertEqual(soup.select_one(".ott-title-lookup-meta a")["href"], CANDIDATE["source_url"])

    def test_unknown_offer_is_not_presented_as_a_subscription(self):
        rendered = title_availability_html({**AVAILABLE, "providers": [{"name": "TVING", "offer_type": "unknown", "url": ""}]})
        soup = BeautifulSoup(rendered, "html.parser")
        self.assertEqual(soup.select(".ott-title-service"), [])
        self.assertNotIn("시청 서비스가 없습니다", soup.get_text())

    def test_multiple_subscription_offers_from_one_service_show_one_logo(self):
        repeated = {"provider_id": "tving", "name": "티빙", "offer_type": "subscription", "url": "https://www.tving.com/contents/HD104"}
        soup = BeautifulSoup(title_availability_html({**AVAILABLE, "providers": [*AVAILABLE["providers"], repeated]}), "html.parser")
        self.assertEqual(len(soup.select('.ott-title-service[data-provider-id="tving"]')), 1)

    def test_rental_or_purchase_only_title_has_no_subscription_service_logo(self):
        soup = BeautifulSoup(title_availability_html({**AVAILABLE, "providers": AVAILABLE["providers"][1:]}), "html.parser")
        self.assertEqual(soup.select(".ott-title-service"), [])
        self.assertEqual(soup.select_one(".ott-title-result")["data-status"], "available")
        self.assertIn("구독", soup.get_text())
        self.assertNotIn("시청 서비스가 없습니다", soup.get_text())

    def test_unavailable_unknown_failure_and_identity_mismatch_remain_distinct(self):
        messages = {
            "unavailable": "키노라이츠에 표시된 시청 서비스가 없습니다.",
            "unknown": "시청 서비스 정보를 확인하지 못했습니다.",
            "fetch_error": "시청 정보를 불러오지 못했습니다.",
            "identity_mismatch": "선택한 작품과 원문 정보가 달라",
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
            {"name": malicious, "offer_type": "subscription", "url": "javascript:alert(1)"},
            {"name": "Bad", "offer_type": "subscription", "url": "https://name:password@example.com/"},
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
            self.assertIn('data-provider-id="tving"', markup(current))
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
            self.assertIn('data-provider-id="tving"', markup(current))
            current.run()
            search.assert_called_once()
            lookup.assert_called_once()
            self.assertIn('data-provider-id="tving"', markup(current))

    def test_multiple_matches_resolve_directly_from_the_results_dropdown(self):
        older = {**CANDIDATE, "id": "103", "year": "1982", "source_url": "https://m.kinolights.com/title/103"}
        partial = {**CANDIDATE, "id": "101", "title": "기생충 이야기"}
        with patch("ott_title_search_view._search_titles", return_value=[partial, older, CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input[0].set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            lookup.assert_not_called()
            self.assertEqual(current.selectbox[0].options, ["기생충 (1982) · 영화", "기생충 (2019) · 영화"])
            current.selectbox[0].select_index(1).run()
            self.assertFalse(current.exception)
            lookup.assert_called_once_with(CANDIDATE)
            search.assert_called_once()
            self.assertFalse(any(button.label == "OTT 확인" for button in current.button))
            current.run()
            lookup.assert_called_once()

    def test_reset_returns_to_the_empty_search_without_query_or_source_calls(self):
        with patch("ott_title_search_view._search_titles", return_value=[CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input(key="ott_title_search_query").set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertIn('data-provider-id="tving"', markup(current))
            current.button(key="ott_title_search_reset").click().run()
            self.assertFalse(current.exception)
            self.assertEqual(current.text_input(key="ott_title_search_query").value, "")
            self.assertEqual(len(current.selectbox), 0)
            self.assertNotIn('class="ott-title-result"', markup(current))
            self.assertNotIn('ott-title-lookup-note', markup(current))
            search.assert_called_once_with("기생충")
            lookup.assert_called_once_with(CANDIDATE)
            current.run()
            search.assert_called_once()
            lookup.assert_called_once()

    def test_reset_clears_candidate_choices_and_a_repeated_query_does_not_reuse_selection(self):
        older = {**CANDIDATE, "id": "103", "year": "1982"}
        with patch("ott_title_search_view._search_titles", return_value=[older, CANDIDATE]) as search, patch("ott_title_search_view._get_title_availability", return_value=AVAILABLE) as lookup:
            current = app()
            current.text_input(key="ott_title_search_query").set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            current.selectbox[0].select_index(1).run()
            lookup.assert_called_once_with(CANDIDATE)
            current.button(key="ott_title_search_reset").click().run()
            self.assertEqual(len(current.selectbox), 0)
            current.text_input(key="ott_title_search_query").set_value("기생충")
            current.button(key="ott_title_search_submit").click().run()
            self.assertIsNone(current.selectbox[0].value)
            self.assertNotIn('class="ott-title-result"', markup(current))
            self.assertEqual(search.call_count, 2)
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
            self.assertIn('data-provider-id="tving"', markup(current))
            search.return_value = []
            current.text_input[0].set_value("다른 작품")
            current.button(key="ott_title_search_submit").click().run()
            self.assertNotIn('data-provider-id="tving"', markup(current))
            search.return_value = [CANDIDATE]
            lookup.side_effect = RuntimeError("source failed")
            current.button(key="ott_title_search_submit").click().run()
            self.assertFalse(current.exception)
            self.assertIn("시청 정보를 불러오지 못했습니다", markup(current))
            self.assertNotIn("시청 서비스가 없습니다", markup(current))


if __name__ == "__main__":
    unittest.main()
