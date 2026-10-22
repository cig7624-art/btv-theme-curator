"""Editorial highlights stay grounded in current public-source evidence."""
import copy
from datetime import datetime, timezone
import unittest

from ott_editorial import select_editorial_highlights


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def work(title):
    return {"title": title, "url": f"https://www.tving.com/contents/{title}",
            "image_url": f"https://image.tving.com/{title}.jpg"}


def section(title, kind="theme", position=1, names=None, **changes):
    return {"title": title, "kind": kind, "position": position,
            "url": "https://www.tving.com/collection/actual",
            "works": [work(name) for name in (names or [title + " A", title + " B"])], **changes}


def platform(service="tving", **changes):
    return {"id": service, "status": "ok", "freshness": "current",
            "checked_at": "2026-10-22T11:00:00Z", "promotions": [], "sections": [], **changes}


def choose(*rows, **changes):
    return select_editorial_highlights({"platforms": list(rows)}, now=NOW, **changes)


class OttEditorialTests(unittest.TestCase):
    def test_no_missing_services_or_fake_content_are_added(self):
        self.assertEqual(select_editorial_highlights(None, now=NOW), [])
        self.assertEqual(choose(platform(service="coupang", status="unavailable")), [])
        rows = choose(platform(sections=[section("한 가지 실제 기획")]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["headline"], "한 가지 실제 기획")
        self.assertEqual(rows[0]["platform_id"], "tving")

    def test_main_banner_can_lead_then_distinctive_themes_beat_generic_rankings(self):
        banner = {**work("실제 메인 홍보작"), "position": 1, "placement": "메인 배너"}
        rows = choose(platform(promotions=[banner], sections=[section("첫 화 무료", "free"),
                        section("일반 인기 목록", "ranking", 2)]),
                      platform("wavve", sections=[section("새로운 시선의 기획", "theme", 1)]))
        self.assertEqual(rows[0]["headline"], "실제 메인 홍보작")
        self.assertEqual(rows[0]["image_shape"], "banner")
        self.assertIn("새로운 시선의 기획", [row["headline"] for row in rows])
        self.assertIn("첫 화 무료", [row["headline"] for row in rows])
        self.assertNotIn("일반 인기 목록", [row["headline"] for row in rows])

    def test_selection_is_not_five_equal_service_slots_and_caps_two_per_provider(self):
        rows = choose(platform(sections=[section(f"기획{i}", position=i) for i in range(1, 8)]),
                      platform("wavve", sections=[section("웨이브 단독 기획")]), limit=6)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["platform_id"] == "tving" for row in rows), 2)

    def test_stale_failed_too_old_future_and_undated_data_are_excluded(self):
        for change in ({"freshness": "stale"}, {"status": "unavailable"},
                       {"checked_at": "2026-10-10T12:00:00Z"},
                       {"checked_at": "2026-10-24T12:00:00Z"}, {"checked_at": "invalid"}):
            with self.subTest(change=change):
                self.assertEqual(choose(platform(sections=[section("기획")], **change)), [])

    def test_personal_collection_links_empty_sections_and_bad_images_are_excluded(self):
        rows = [section("고객님을 위한 추천"), section("당신을 위한 콘텐츠"), section("맞춤 추천"),
                section("Recommended for you"), section("컬렉션", kind="collection"),
                section("카테고리"), section("비어 있는 기획", works=[]),
                section("링크 묶음", items_kind="collections"),
                section("이미지 없음", works=[{**work("제목"), "image_url": "javascript:bad"}]),
                section("자리 표시", works=[{**work("자리"), "image_url": "https://img.example.com/placeholder.png"}])]
        self.assertEqual(choose(platform(sections=rows)), [])

    def test_generic_low_priority_rows_do_not_pad_the_digest(self):
        rows = [section("영화", "genre"), section("요일별 콘텐츠", "calendar"),
                section("전체 콘텐츠", "other"), section("포인트샵", "rental")]
        self.assertEqual(choose(platform(sections=rows)), [])

    def test_marketing_stays_marketing_and_does_not_import_content_home_sections(self):
        rows = choose(platform("netflix", status="promotion_only",
                              sections=[section("검증하지 않은 홈")],
                              public_lists=[section("지금 뜨는 콘텐츠", "ranking", names=["원피스", "실제 작품"])]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "public_promotion")
        self.assertEqual(rows[0]["headline"], "지금 뜨는 콘텐츠")
        self.assertNotIn("검증하지 않은 홈", str(rows))
        self.assertIn("원피스", rows[0]["summary"])

    def test_repeated_work_banner_title_and_image_are_deduplicated(self):
        shared = work("반복 작품")
        rows = choose(platform(promotions=[{**shared, "position": 1}, {**shared, "position": 2}],
                              sections=[section("작품 소개", kind="promotion", names=["반복 작품"])]),
                      platform("wavve", promotions=[{**shared, "position": 1}]))
        self.assertEqual(len(rows), 1)

    def test_summary_uses_known_titles_without_turning_placement_into_audience_claims(self):
        rows = choose(platform(sections=[section("1화 무료 보기", "free", names=["첫 번째 작품", "두 번째 작품", "세 번째 작품"])]))
        self.assertEqual(rows[0]["summary"], "〈첫 번째 작품〉·〈두 번째 작품〉 등 첫 화 무료 보기.")
        for text in (rows[0]["headline"], rows[0]["summary"]):
            self.assertNotIn("조회수", text)
            self.assertNotIn("흥행", text)
            self.assertNotIn("급상승", text)

    def test_source_metadata_and_last_success_date_survive_and_selection_is_pure(self):
        source = platform(last_success_at="2026-10-21T13:00:00Z", sections=[section("원문 제목", position=3)])
        before = copy.deepcopy(source)
        row = choose(source)[0]
        self.assertEqual(row["position"], 3)
        self.assertEqual(row["source_title"], "원문 제목")
        self.assertEqual(row["source_url"], "https://www.tving.com/collection/actual")
        self.assertEqual(row["checked_at"], "2026-10-21T13:00:00Z")
        self.assertGreater(row["selection_score"], 0)
        self.assertEqual(source, before)

    def test_ties_and_repeated_calls_are_deterministic_and_limit_is_respected(self):
        data = {"platforms": [platform(sections=[section("기획1"), section("기획2", position=2)]),
                              platform("wavve", sections=[section("다른 기획")])]}
        first = select_editorial_highlights(data, now=NOW, limit=2)
        self.assertEqual(len(first), 2)
        self.assertEqual(first, select_editorial_highlights(data, now=NOW, limit=2))
        self.assertEqual(select_editorial_highlights(data, now=NOW, limit=0), [])


if __name__ == "__main__":
    unittest.main()
