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
        item = document("unmarked", "티빙 통합 이용권 출시", company="티빙", country="KR")
        self.assertEqual(self.select([item], scope="domestic"), [])
        foreign = domestic("not-kr", "티빙 통합 이용권 출시")
        foreign["country"] = "US"
        self.assertEqual(self.select([foreign], scope="domestic"), [])

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
