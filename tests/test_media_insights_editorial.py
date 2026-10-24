"""The magazine edition is a material, dated subset of the source archive."""
from copy import deepcopy
import unittest

from media_insights_editorial import select_major_insights


NOW = "2026-10-24T12:00:00Z"


def document(identifier, title, *, company="Disney", date="2026-10-15", topic="partnership_distribution",
             text="Disney+ streaming service announced a partnership to distribute original creator programming.", **extra):
    value = {
        "id": identifier, "title": title, "company": company,
        "url": "https://official.example/news/" + identifier,
        "published_at": date, "topic": topic, "content_hash": identifier + "-hash",
        "body_text": text,
        "evidence": [{"id": identifier + "-p1", "text": text,
                      "url": "https://official.example/news/" + identifier, "locator": "문단 1"}],
    }
    value.update(extra)
    return value


def domestic(identifier, title, **extra):
    return document(identifier, title, company="티빙", country="KR", market_scope="domestic_product",
                    text="국내 티빙 이용자를 대상으로 새 이용 조건을 공식 발표했습니다. 기존 가입자 적용일도 안내합니다.", **extra)


class MediaInsightsEditorialTests(unittest.TestCase):
    def select(self, items, **options):
        return select_major_insights(items, now=NOW, **options)

    def test_ott_brand_channel_partnership_is_a_domestic_media_issue(self):
        item = document("mbc-tving", "CJ ENM, MBC 그룹과 전략적 파트너십 체결… MENA 지역 OTT에 티빙 브랜드관 론칭",
            company="CJ ENM", country="KR", market_scope="domestic_media", date="2026-10-14",
            text="CJ ENM은 MBC 샤히드에 티빙 브랜드관을 론칭하고 국내 콘텐츠를 공급하는 파트너십을 체결했습니다.")
        for scope in ("all", "domestic"):
            selected = self.select([item], scope=scope)
            self.assertEqual([row["id"] for row in selected], ["mbc-tving"])
            self.assertEqual(selected[0]["editorial_category"], "콘텐츠 유통·제휴")

    def test_collecting_a_document_does_not_make_it_a_major_story(self):
        items = [
            document("creator", "Disney and Dude Perfect Announce Creative Partnership"),
            document("sports", "Disney+ to Stream Super Bowl LXI", topic="sports_content"),
            document("ai", "Beta Testing AI-Powered Search on Disney+", topic="ai_technology"),
            document("schedule", "Disney to Announce Quarterly Results"),
            document("reconcile", "Financial Reconciliations of Q3 2026"),
            document("generic", "Investor Relations"),
            document("movie", "Disney+ Original Drama Arrives This Friday", topic="sports_content"),
            document("old-merger", "Netflix and Warner Bros Amend Merger Agreement", company="Netflix", date="2026-01-20"),
        ]
        result = self.select(items)
        self.assertEqual({row["id"] for row in result}, {"creator", "sports", "ai"})
        ai = next(row for row in result if row["id"] == "ai")
        self.assertEqual(ai["editorial_category"], "AI·서비스 실험")
        self.assertIn("베타", ai["editorial_reason"])
        self.assertTrue(all(row["editorial_date_basis"] == "published" for row in result))

    def test_undated_future_and_unconfirmed_pdf_dates_stay_in_archive(self):
        title = "Disney Announces Disney+ Distribution Partnership"
        items = [document("missing", title, date=""), document("future", title, date="2026-10-25"),
                 document("pdf", title, published_at_source="pdf_publication_unconfirmed")]
        self.assertEqual(self.select(items), [])

    def test_recent_source_revision_can_keep_current_season_guide_without_relabeling_as_new(self):
        guide = document("nba", "How to watch the 2026-27 NBA season on Prime Video", company="Amazon",
                         topic="sports_content", date="2025-08-13", source_updated_at="2026-10-14")
        row = self.select([guide])[0]
        self.assertEqual(row["published_at"], "2025-08-13")
        self.assertEqual(row["editorial_date"], "2026-10-14")
        self.assertEqual(row["editorial_date_basis"], "source_updated")
        self.assertIn("신규 발표가 아닌", row["editorial_reason"])

    def test_revision_is_not_a_general_freshness_loophole(self):
        items = [
            document("old-merger", "Netflix Announces Merger Partnership", company="Netflix", date="2026-01-10", source_updated_at="2026-10-20"),
            document("old-season", "How to watch the 2025-26 NBA season on Prime Video", company="Amazon", date="2025-08-13", source_updated_at="2026-10-20"),
            document("stale-revision", "How to watch the 2026-27 NBA season on Prime Video", company="Amazon", date="2025-08-13", source_updated_at="2026-09-01"),
            document("invented-future", "How to watch the 2026-27 NBA season on Prime Video", company="Amazon", date="2025-08-13", source_updated_at="2026-10-25"),
        ]
        self.assertEqual(self.select(items), [])

    def test_only_latest_earnings_with_actual_media_evidence_is_selected(self):
        items = [
            document("q2", "Amazon Announces Second Quarter Results", company="Amazon", date="2026-07-30", topic="earnings_strategy",
                     text="Prime Video viewership increased during the quarter, and streaming advertising revenue increased across the service."),
            document("q1", "Amazon Announces First Quarter Results", company="Amazon", date="2026-04-29", topic="earnings_strategy",
                     text="Prime Video streaming advertising revenue increased during this quarter across its viewing service."),
            document("cloud", "Amazon Announces Third Quarter Results", company="Amazon", topic="earnings_strategy",
                     text="AWS cloud revenue grew and the retail division expanded its logistics capacity during this financial quarter."),
        ]
        result = self.select(items)
        self.assertEqual([row["id"] for row in result], ["q2"])
        self.assertEqual(result[0]["editorial_evidence_ids"], ["q2-p1"])

    def test_forward_looking_warning_is_not_media_performance_evidence(self):
        item = document("warning", "Amazon Announces Second Quarter Results", company="Amazon", topic="earnings_strategy",
                        text="Forward-looking statements include potential Prime Video streaming revenue and future subscribers, which remain uncertain.")
        self.assertEqual(self.select([item]), [])

    def test_domestic_includes_real_products_features_and_account_terms(self):
        items = [domestic("bundle", "티빙·웨이브 통합 이용권 출시"),
                 domestic("feature", "티빙 AI 검색 기능 도입"),
                 domestic("terms", "티빙 이용약관 개편, 동시 시청 제한 변경")]
        result = self.select(items, scope="domestic")
        self.assertEqual({row["id"] for row in result}, {"bundle", "feature", "terms"})
        categories = {row["id"]: row["editorial_category"] for row in result}
        self.assertEqual(categories["feature"], "시청 기능·기기")
        self.assertEqual(categories["terms"], "이용 조건·정책")

    def test_domestic_product_window_keeps_verified_launches_without_aging_the_general_edition(self):
        # Real launch cadence is often quarterly: these announcements are 115
        # and 127 days old at the observation date, not breaking news today.
        max_launch = domestic("max-launch", "B tv+ MAX 신규 월정액 상품 출시", date="2026-07-01")
        max_launch["company"] = "SK브로드밴드"
        bundle = domestic("youtube-bundle", "U+tv 유튜브 프리미엄 AI PRO 결합 상품 출시", date="2026-06-19")
        bundle["company"] = "LG유플러스"
        expired_window = domestic("old-product", "티빙 통합 이용권 출시", date="2026-04-25")
        items = [max_launch, bundle, expired_window]
        result = self.select(items, scope="domestic")
        self.assertEqual({row["id"] for row in result}, {"max-launch", "youtube-bundle"})
        self.assertEqual({row["editorial_date"] for row in result}, {"2026-07-01", "2026-06-19"})
        self.assertTrue(all(row["editorial_date_basis"] == "published" for row in result))
        self.assertEqual(self.select(items, scope="all"), [])
        self.assertTrue(all("이용 가능" not in row["editorial_reason"] for row in result))

    def test_domestic_product_window_is_bounded_to_180_days(self):
        # Boundary uses the actual source timestamp, without rewriting its date.
        at_boundary = domestic("boundary", "티빙 통합 이용권 출시", date="2026-04-27T12:00:00Z")
        too_old = domestic("older", "웨이브 결합 상품 출시", date="2026-04-27T11:59:59Z")
        result = self.select([at_boundary, too_old], scope="domestic")
        self.assertEqual([row["id"] for row in result], ["boundary"])

    def test_domestic_excludes_title_releases_mobile_only_guides_and_overseas_prices(self):
        items = [domestic("drama", "티빙 신규 오리지널 드라마 출시"),
                 domestic("phone", "KT 휴대폰 요금제 출시"),
                 domestic("internet", "KT 인터넷 요금제 출시"),
                 domestic("guide", "티빙 요금제 가입 방법 알아보기"),
                 domestic("foreign", "넷플릭스 미국 요금제 가격 인상")]
        # A source/footer mention of the IPTV brand must not rescue phone news.
        for row in items:
            row["body_text"] += " 지니 TV 서비스도 제공합니다."
        self.assertEqual(self.select(items, scope="domestic"), [])
        self.assertEqual(self.select(items), [])

    def test_domestic_requires_verified_market_not_just_korean_company(self):
        item = document("unmarked", "티빙 통합 이용권 출시", company="티빙")
        self.assertEqual(self.select([item], scope="domestic"), [])
        foreign = domestic("not-kr", "티빙 통합 이용권 출시")
        foreign["country"] = "US"
        self.assertEqual(self.select([foreign], scope="domestic"), [])

    def test_domestic_content_lineup_and_media_performance_are_major_stories(self):
        lineup = document("fall-lineup", "SKB, 여행·미식·세대 공감 담은 가을 시즌 신작 프로그램 3편 선보여",
                          company="SK브로드밴드", country="KR", market_scope="domestic_media", date="2026-10-19",
                          topic="content_release",
                          text="SK브로드밴드는 지역 채널에서 여행과 미식, 세대 공감을 다루는 가을 시즌 신작 프로그램 3편을 선보인다고 밝혔다.")
        performance = document("global-content", "CJ ENM 콘텐츠, 韓·美·日 글로벌서 날았다… 영화·시리즈 잇단 성과",
                               company="CJ ENM", country="KR", date="2026-10-20", topic="content_performance",
                               text="CJ ENM은 한국 영화와 시리즈가 미국과 일본에서 공개된 뒤 흥행 성과를 거뒀으며 현지 플랫폼의 시청 순위에서 1위를 기록했다고 발표했다.")
        result = self.select([lineup, performance], scope="domestic")
        self.assertEqual({row["id"] for row in result}, {"fall-lineup", "global-content"})
        self.assertEqual({row["editorial_category"] for row in result}, {"콘텐츠 공개·편성", "콘텐츠·미디어 성과"})
        self.assertTrue(all(row["editorial_evidence_ids"] for row in result))
        self.assertEqual({row["id"] for row in self.select([lineup, performance])}, {"fall-lineup", "global-content"})

    def test_fresh_domestic_content_precedes_older_products_and_keeps_90_day_window(self):
        fresh = document("fresh", "B tv 채널, 가을 신작 프로그램 3편 공개", company="SK브로드밴드",
                         market_scope="domestic_media", date="2026-10-19", topic="content_release",
                         text="B tv 채널은 가족과 함께 보는 여행과 미식 신작 프로그램 3편을 가을 시즌에 새롭게 공개한다고 발표했다.")
        product = domestic("older-product", "티빙 통합 이용권 출시", date="2026-06-19")
        old_content = deepcopy(fresh)
        old_content.update(id="old-content", url="https://official.example/news/old-content", published_at="2026-06-19")
        result = self.select([product, old_content, fresh], scope="domestic")
        self.assertEqual([row["id"] for row in result], ["fresh", "older-product"])

    def test_domestic_media_registry_can_include_a_verified_older_retail_product(self):
        product = domestic("media-marked-product", "티빙 통합 이용권 출시", date="2026-06-19")
        product["market_scope"] = "domestic_media"
        self.assertEqual([row["id"] for row in self.select([product], scope="domestic")], ["media-marked-product"])
        self.assertEqual(self.select([product]), [])

    def test_domestic_content_requires_a_real_source_event_not_a_guide_or_footer(self):
        guide = document("guide", "티빙 신작 드라마 추천 총정리", country="KR", market_scope="domestic_media",
                         text="티빙이 최근 공개한 드라마와 영화 등 다양한 콘텐츠를 이용하는 방법을 소개합니다.")
        mismatch = document("mismatch", "B tv 신작 프로그램 3편 공개", country="KR", market_scope="domestic_media",
                            text="휴대폰을 구입하면 가입 혜택이 제공됩니다. 본문에는 방송 공개나 편성에 관한 신규 내용이 없습니다.")
        # The second paragraph must not claim a content event at all.
        mismatch["evidence"][0]["text"] = "이번 휴대폰 요금제의 가입 할인 조건을 안내합니다. 고객센터는 평일 오전부터 오후까지 운영합니다."
        self.assertEqual(self.select([guide, mismatch], scope="domestic"), [])

    def test_same_source_and_same_event_are_not_repeated(self):
        original = document("original", "Disney+ to Stream Super Bowl LXI")
        syndicated = document("copy", "Disney+ to Stream Super Bowl LXI Across More Devices")
        duplicate_url = document("urlcopy", "Disney+ Streaming Distribution Partnership", url=original["url"] + "?utm_campaign=test")
        self.assertEqual(len(self.select([original, syndicated])), 1)
        self.assertEqual(len(self.select([original, duplicate_url])), 1)

    def test_does_not_fill_space_with_noise_or_force_a_company_quota(self):
        self.assertEqual(self.select([document("noise", "Welcome to the corporate blog")]), [])
        result = self.select([document(str(i), "Disney+ Announces Distribution Partnership " + str(i)) for i in range(7)], limit=6)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(row["company"] == "Disney" for row in result))

    def test_keeps_input_originals_and_returns_independent_copies(self):
        item = document("source", "Disney+ Announces Distribution Partnership")
        before = deepcopy(item)
        result = self.select([item])
        result[0]["evidence"][0]["text"] = "changed only in returned copy"
        self.assertEqual(item, before)
        self.assertNotIn("editorial_reason", item)

    def test_limits_and_invalid_scope(self):
        self.assertEqual(self.select([document("one", "Disney+ Announces Distribution Partnership")], limit=0), [])
        with self.assertRaises(ValueError):
            self.select([], scope="unreviewed")


if __name__ == "__main__":
    unittest.main()
