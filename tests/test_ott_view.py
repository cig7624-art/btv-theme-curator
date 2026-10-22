"""OTT cards must keep source facts and never turn missing data into findings."""

import copy
import unittest

from bs4 import BeautifulSoup

from ott_view import ott_comparison_html, ott_platform_html, snapshot_platforms


def work(number=1, **changes):
    result = {"title": f"원문 작품 {number}", "url": f"https://www.tving.com/contents/P{number}", "image_url": f"https://img.tving.com/{number}.jpg"}
    result.update(changes)
    return result


def section(number=1, **changes):
    result = {"id": str(number), "title": f"원래 홈 제목 {number}", "description": "공식 홈의 원래 설명", "kind": "theme", "position": number,
              "url": f"https://www.tving.com/collection/{number}", "works": [work(number * 10 + i) for i in range(5)]}
    result.update(changes)
    return result


def platform(**changes):
    result = {"id": "tving", "name": "TVING", "url": "https://www.tving.com/", "source_url": "https://www.tving.com/",
              "status": "ok", "checked_at": "2026-10-22T01:00:00Z", "last_success_at": "2026-10-22T01:00:00Z", "complete": False,
              "promotions": [{**work(i), "position": i} for i in range(1, 6)], "sections": [section(i) for i in range(1, 9)]}
    result.update(changes)
    return result


class OttViewTests(unittest.TestCase):
    def test_missing_snapshot_has_no_fabricated_highlights_or_service_filler(self):
        soup = BeautifulSoup(ott_comparison_html(None), "html.parser")
        self.assertIsNotNone(soup.select_one(".ott-magazine-empty"))
        self.assertEqual(len(soup.select(".ott-feature")), 0)
        self.assertEqual(len(soup.select(".ott-compare-column")), 0)
        self.assertIn("확인된 주요 소식", soup.get_text())
        self.assertNotIn("0개", soup.get_text())

    def test_overview_selects_original_highlights_with_collapsed_evidence_and_no_equal_slots(self):
        soup = BeautifulSoup(ott_comparison_html({"platforms": [platform()]}), "html.parser")
        stories = soup.select(".ott-feature")
        self.assertEqual(len(stories), 2)
        self.assertEqual({node['data-platform'] for node in stories}, {"tving"})
        self.assertEqual([node.get_text() for node in soup.select('.ott-feature-headline')], ["원문 작품 1", "원래 홈 제목 1"])
        self.assertIn("원문 작품 10", soup.select_one('.ott-feature-side .ott-feature-summary').get_text())
        self.assertIsNotNone(stories[0].select_one('.ott-feature-image[role="img"][style]'))
        notes = soup.select_one('.ott-editorial-notes')
        self.assertNotIn('open', notes.attrs)
        self.assertIn('원문 1번째', notes.get_text())
        self.assertNotIn('selection_score', str(soup))
        self.assertNotIn('쿠팡플레이', soup.get_text())

    def test_detail_keeps_original_order_types_and_more_rows_accessible(self):
        data = platform(sections=[section(8), section(1, kind="new"), section(2, kind="ranking"), *[section(i) for i in range(3, 8)]])
        soup = BeautifulSoup(ott_platform_html(data), "html.parser")
        first = soup.select_one(".ott-platform-detail > .ott-section-grid")
        self.assertEqual([node.get_text() for node in first.select(".ott-section-title")], [f"원래 홈 제목 {i}" for i in range(1, 7)])
        more = soup.select_one(".ott-more-sections")
        self.assertEqual([node.get_text() for node in more.select(".ott-section-title")], ["원래 홈 제목 7", "원래 홈 제목 8"])
        self.assertEqual(len(first.select(".ott-work-title")), 24)
        self.assertEqual(len(soup.select(".ott-promotion")), 5)
        self.assertIn("신작", first.get_text())
        self.assertIn("순위", first.get_text())

    def test_unavailable_does_not_display_unverified_content_without_last_good_date(self):
        rendered = ott_platform_html(platform(status="unavailable", last_success_at=""))
        self.assertIn("홈 확인 대기", rendered)
        self.assertNotIn('class="ott-home-section"', rendered)
        self.assertNotIn('class="ott-promotion"', rendered)
        self.assertNotIn("0개", rendered)

    def test_failure_shows_dated_last_good_content_and_not_today_as_data_date(self):
        data = platform(status="unavailable", checked_at="2026-10-22T01:00:00Z", last_success_at="2026-10-19T01:00:00Z")
        rendered = ott_platform_html(data)
        self.assertIn("이전 확인 자료", rendered)
        self.assertIn("10.19 10:00 KST 자료", rendered)
        self.assertNotIn("10.22 10:00 KST 자료", rendered)
        self.assertIn("원래 홈 제목 1", rendered)

    def test_public_marketing_is_not_reported_as_home_collections(self):
        rendered = ott_platform_html(platform(status="promotion_only"))
        self.assertIn("공개 홍보 화면", rendered)
        self.assertIn("공개 홍보작", rendered)
        self.assertNotIn('class="ott-home-section"', rendered)
        self.assertNotIn("확인한 8개 영역", rendered)
        self.assertNotIn("0개", rendered)

    def test_stale_marketing_keeps_its_scope(self):
        rendered = ott_platform_html(platform(status="unavailable", last_success_status="promotion_only"))
        self.assertIn("이전 확인 자료", rendered)
        self.assertIn("공개 홍보작", rendered)
        self.assertNotIn('class="ott-home-section"', rendered)

    def test_titles_descriptions_and_css_urls_cannot_inject_markup(self):
        malicious = '\"><script>alert(1)</script>'
        css = 'https://img.example.com/a");color:red;background:url(https://evil.example/x)'
        data = platform(sections=[section(title=malicious, description=malicious, works=[work(title=malicious, image_url=css, url="javascript:alert(1)")])])
        rendered = ott_platform_html(data)
        self.assertNotIn("<script", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn('a");color:red', rendered)
        self.assertIn("%22%29", rendered)
        self.assertNotIn("url(https://evil.example", rendered)

    def test_bad_schemes_and_credential_urls_are_never_rendered(self):
        data = platform(url="javascript:alert(1)", source_url="data:text/html,bad", promotions=[work(image_url="data:image/svg+xml,<svg/>", url="https://name:password@www.tving.com/")],
                        sections=[section(url="vbscript:bad", works=[work(image_url="javascript:bad", url="javascript:bad")])])
        rendered = ott_platform_html(data)
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("vbscript:", rendered)
        self.assertNotIn("data:", rendered)
        self.assertNotIn("password@", rendered)
        self.assertIn('href="https://www.tving.com/"', rendered)

    def test_failed_or_missing_image_keeps_named_fallback_and_caption(self):
        rendered = ott_platform_html(platform(sections=[section(works=[work(image_url="")])]))
        soup = BeautifulSoup(rendered, "html.parser")
        tile = soup.select_one(".ott-home-section .ott-art-tile")
        self.assertIn("원문 작품 1", tile.select_one(".ott-art-fallback").get_text())
        self.assertEqual(tile.select_one(".ott-work-title").get_text(), "원문 작품 1")
        self.assertIsNone(tile.select_one(".ott-art-image"))
        self.assertNotIn("onerror", rendered)

    def test_source_evidence_includes_position_date_scope_and_original_link(self):
        data = platform(capture_scope="홈 상위 10개 영역", sections=[section(3)])
        soup = BeautifulSoup(ott_platform_html(data), "html.parser")
        evidence = soup.select_one(".ott-home-section .ott-evidence")
        self.assertIn("홈 3번째 콘텐츠 영역", evidence.get_text())
        self.assertIn("10.22 10:00 KST", evidence.get_text())
        self.assertIn("홈 상위 10개 영역", evidence.get_text())
        self.assertIn("https://www.tving.com/collection/3", [a["href"] for a in evidence.find_all("a")])

    def test_global_observation_context_is_visible_with_evidence(self):
        snapshot = {"platforms": [platform()], "observed_environment": "github_actions_global", "country_verified": False}
        rows = snapshot_platforms(snapshot)
        rendered = ott_platform_html(next(row for row in rows if row["id"] == "tving"))
        self.assertIn("해외 서버 · 한국 지역 미확인", rendered)
        soup = BeautifulSoup(rendered, "html.parser")
        self.assertIn("해외 서버", soup.select_one(".ott-evidence").get_text())
        self.assertNotIn("해외 서버", soup.select_one(".ott-status").get_text())

    def test_rendering_does_not_mutate_saved_snapshot(self):
        data = {"platforms": [platform(sections=[section(2), section(1)])]}
        before = copy.deepcopy(data)
        ott_comparison_html(data)
        ott_platform_html(data["platforms"][0])
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
