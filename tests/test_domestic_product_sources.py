"""Domestic product changes must not become a feed of trailers or phone deals."""
from copy import deepcopy
import unittest

from domestic_product_sources import (
    DOMESTIC_PRODUCT_SOURCES, classify_domestic_product, domestic_product_candidate,
    domestic_product_topic, is_domestic_product_item,
)


class DomesticProductSourcesTests(unittest.TestCase):
    def test_actual_product_change_headlines_are_classified(self):
        cases = {
            "B tv, 광고형 요금제 신설": "advertising_pricing",
            "지니 TV, 티빙 결합상품 출시": "partnership_distribution",
            "U+tv, 넷플릭스 결합 요금제 개편": "advertising_pricing",
            "쿠팡플레이, 스포츠 패스 출시": "sports_content",
            "웨이브, 동시 시청 정책 변경": "advertising_pricing",
            "디즈니+, 국내 이용권 가격 인상": "advertising_pricing",
            "B tv, AI 검색 기능 도입": "ai_technology",
            "티빙, 신규 가입자 구독 할인 혜택 제공": "advertising_pricing",
            "유독, 유튜브 프리미엄 결합 이용권 출시": "advertising_pricing",
            "SKB, 지상파·실시간 채널 모은 올인원 구독상품 ‘B tv+ max’ 출시": "advertising_pricing",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(domestic_product_topic(title), expected)
                self.assertEqual(classify_domestic_product(title), expected)

    def test_ir_content_phone_and_routine_guides_are_excluded(self):
        for title in [
            "티빙 오리지널 드라마 제작 발표", "쿠팡플레이, 신작 영화 무료 공개",
            "넷플릭스 신작 예고편 공개", "SK브로드밴드, 10기가 인터넷 요금제 출시",
            "KT, 아이폰 신규 요금제 출시", "LG유플러스, 5G 청년 할인 요금제 출시",
            "CJ ENM, 티빙 실적 개선으로 매출 증가", "티빙과 웨이브 합병 발표",
            "B tv 추천 영화 총정리", "지니 TV 가입 방법 알아보기", "쿠팡플레이 스포츠 생중계",
            "디즈니+, 미국 요금제 가격 인상", "넷플릭스, 일본 시장 광고 요금제 출시",
            "넷플릭스 미국 요금제 인상, 한국도 오를까?", "티빙 요금제 개편 가능성 전망",
        ]:
            with self.subTest(title=title):
                self.assertEqual(domestic_product_topic(title), "")

    def test_footers_cannot_promote_an_entertainment_story(self):
        self.assertEqual(domestic_product_topic(
            "티빙, 새로운 오리지널 드라마 공개",
            "티빙은 월정액 이용권과 신규 가입 할인 혜택을 제공하고 있습니다."), "")
        self.assertEqual(domestic_product_topic(
            "KT, 새로운 프리미엄 요금제 출시",
            "KT 지니 TV와 결합해 이용할 수 있는 IPTV 전용 요금제입니다."), "advertising_pricing")
        for title in ("KT 휴대폰 요금제 출시", "LG유플러스 5G 요금제 개편", "SK브로드밴드 인터넷 요금제 출시"):
            self.assertEqual(domestic_product_topic(title, "지니 TV와 U+tv 서비스도 제공합니다."), "")
        self.assertEqual(domestic_product_topic("KT, 인터넷과 지니 TV 결합 요금제 출시"), "advertising_pricing")
        self.assertEqual(domestic_product_topic("KT, AI 에이전트 도입", "KT는 지니 TV 서비스를 제공합니다."), "")

    def test_registration_never_supplies_a_price_or_available_date(self):
        for source in DOMESTIC_PRODUCT_SOURCES:
            self.assertTrue(source["require_domestic_product"])
            self.assertEqual(source["market_scope"], "domestic_product")
            self.assertTrue(source["url"].startswith("https://"))
            for field in ["price", "effective_at", "available", "published_at"]:
                self.assertNotIn(field, source)

    def test_actual_service_led_bundle_headline_needs_launch_proof_in_opening_body(self):
        # Actual LGU+ headline and launch sentence from the June 19 source.
        title = "유독에서 구글 AI Pro와 YouTube Premium 더욱 합리적으로 즐기세요"
        self.assertTrue(domestic_product_candidate(title))
        self.assertEqual(domestic_product_topic(title), "")
        body = "유독에서는 Google AI Pro와 YouTube Premium을 결합한 상품을 선보였습니다."
        self.assertEqual(domestic_product_topic(title, body), "partnership_distribution")
        self.assertEqual(domestic_product_topic(title, "기존 구독자에게 결합 혜택을 설명합니다."), "")
        self.assertEqual(domestic_product_topic("유독 요금제 이용 방법 알아보기", body), "")
        self.assertFalse(domestic_product_candidate("KT 휴대폰 요금제 출시"))
        self.assertFalse(domestic_product_candidate("티빙 드라마 정주행 가이드"))

    def test_common_adverb_is_not_the_youdok_product_brand(self):
        self.assertEqual(domestic_product_topic("유독 느린 인터넷, 새 요금제 출시", "지니 TV도 이용 가능합니다."), "")

    def test_product_marker_and_verified_legacy_source_identify_tab_without_mutation(self):
        item = {"source_id": "skb_products", "country": "KR", "title": "B tv 광고형 요금제 신설"}
        before = deepcopy(item)
        self.assertTrue(is_domestic_product_item(item))
        self.assertEqual(item, before)
        self.assertTrue(is_domestic_product_item({"market_scope": "domestic_product"}))
        self.assertTrue(is_domestic_product_item({"domestic_product": True}))
        self.assertFalse(is_domestic_product_item({"source_id": "skb_products", "country": "US", "title": "B tv 요금제 출시"}))
        self.assertFalse(is_domestic_product_item({"country": "KR", "title": "국내 미디어 실적 발표"}))
        self.assertFalse(is_domestic_product_item(None))


if __name__ == "__main__":
    unittest.main()
