import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import ott_collection as c


NOW = "2026-10-22T07:00:00+00:00"
BEFORE = "2026-10-21T07:00:00+00:00"
TVING = next(p for p in c.PLATFORMS if p["id"] == "tving")


def card(number, title=None):
    return f'<a href="/contents/P{number}"><img alt="{title or f"작품 {number}"}" src="https://image.tving.com/{number}.jpg"></a>'


def row(title="관계가 뒤집히는 순간", contents=None, **attrs):
    attributes = " ".join(f'{k}="{v}"' for k, v in attrs.items())
    return f'<section {attributes}><h2>{title}</h2>{contents if contents is not None else card(1) + card(2)}</section>'


def parse(body):
    return c.parse_public_home('<html><main>' + body + '</main></html>', TVING, checked_at=NOW)


class PublicHomeExtractionTests(unittest.TestCase):
    def test_home_order_duplicate_cards_and_theme_kinds(self):
        html = row(contents=card(1) + card(1) + card(2)) + row("새로 공개된 작품", card(3) + card(4)) + row("오늘의 TOP 10", card(5) + card(6))
        result = parse(html)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([r["kind"] for r in result["sections"]], ["theme", "new", "ranking"])
        self.assertEqual([r["position"] for r in result["sections"]], [1, 2, 3])
        self.assertEqual([w["title"] for w in result["sections"][0]["works"]], ["작품 1", "작품 2"])
        self.assertFalse(result["complete"])

    def test_nearby_unrelated_heading_cannot_borrow_cards(self):
        result = parse('<div><h2>연관 없는 제목</h2><section><h2>실제 작품 목록</h2>' + card(1) + card(2) + '</section></div>')
        self.assertEqual([r["title"] for r in result["sections"]], ["실제 작품 목록"])

    def test_hidden_personal_and_faq_sections_are_excluded(self):
        result = parse(row("이어보기") + row("님을 위한 추천") + row("보이지 않는 목록", **{"aria-hidden": "true"}) + row("자주 묻는 질문") + row("공개 홈 테마"))
        self.assertEqual([r["title"] for r in result["sections"]], ["공개 홈 테마"])

    def test_home_heading_and_multiple_linked_artworks_are_required(self):
        for html in [row(contents=card(1)), '<h2>설명만 있는 기획</h2><p>작품 A와 B</p>', '<script type="application/ld+json">{"@type":"Movie","name":"작품"}</script>']:
            self.assertEqual(parse(html)["sections"], [])

    def test_heading_is_not_replaced_by_analysis_and_same_collection_dedupes(self):
        result = parse(row("낯선 조합: 이름 그대로") + row("낯선 조합: 이름 그대로"))
        self.assertEqual(len(result["sections"]), 1)
        self.assertEqual(result["sections"][0]["title"], "낯선 조합: 이름 그대로")

    def test_hero_is_promotion_not_a_collection(self):
        result = parse('<div class="main-hero"><h2>메인 홍보</h2>' + card(1) + card(2) + '</div>' + row("홈의 테마", card(3) + card(4)))
        self.assertEqual([p["title"] for p in result["promotions"]], ["작품 1", "작품 2"])
        self.assertEqual([r["title"] for r in result["sections"]], ["홈의 테마"])

    def test_content_carousel_is_not_automatically_a_main_banner(self):
        result = parse('<section aria-roledescription="carousel"><h2>하루의 끝</h2>' + card(1) + card(2) + '</section>')
        self.assertEqual(result["promotions"], [])
        self.assertEqual(len(result["sections"]), 1)

    def test_signup_marketing_does_not_prove_public_home(self):
        result = c.parse_public_home('<main><h1>멤버십 가입하고 시작하기</h1><input type="email"><section><h2>인기 작품</h2><a href="/title/1"><img alt="작품1"></a><a href="/title/2"><img alt="작품2"></a></section></main>', c.PLATFORMS[0], checked_at=NOW)
        self.assertEqual(result["status"], "promotion_only")
        self.assertEqual(result["sections"], [])
        self.assertIsNone(result["section_count"])

    def test_signup_newsletter_does_not_erase_an_actual_domestic_home(self):
        result = parse(row("공개 홈의 기획") + '<div>뉴스레터 가입 <input type="email"></div>')
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["sections"]), 1)

    def test_external_and_script_links_are_not_title_evidence(self):
        result = parse(row(contents='<a href="javascript:alert(1)"><img alt="가짜 1" src="https://example.org/1.jpg"></a><a href="https://www.tving.com.evil.example/contents/P2"><img alt="가짜 2" src="https://example.org/2.jpg"></a>'))
        self.assertEqual(result["sections"], [])

    def test_lazy_art_uses_real_image_not_placeholder(self):
        first = '<a href="/contents/P1"><img alt="작품 1" src="/placeholder.svg" data-src="https://image.tving.com/real.jpg"></a>'
        self.assertEqual(parse(row(contents=first + card(2)))["sections"][0]["works"][0]["image_url"], "https://image.tving.com/real.jpg")


class CollectionStateTests(unittest.TestCase):
    def test_network_failure_keeps_last_good_content_and_does_not_count_zero(self):
        old = c.collect_public_homes(platform_ids=["tving"], fetcher=lambda _: '<main>' + row() + '</main>', now=BEFORE)
        def blocked(_):
            raise requests.exceptions.ProxyError("Tunnel connection failed: 403 Forbidden")
        result = c.collect_public_homes(old, ["tving"], fetcher=blocked, now=NOW)
        tving = next(p for p in result["platforms"] if p["id"] == "tving")
        self.assertEqual(tving["status"], "unavailable")
        self.assertEqual(tving["freshness"], "stale")
        self.assertEqual(tving["last_success_at"], BEFORE)
        self.assertEqual(tving["checked_at"], NOW)
        self.assertEqual(tving["error_code"], "network_blocked")
        self.assertIsNone(tving["section_count"])
        self.assertEqual(len(tving["sections"]), 1)
        self.assertEqual(tving["sections"][0]["title"], "관계가 뒤집히는 순간")

    def test_failure_of_one_source_does_not_discard_other_sources(self):
        def source(platform):
            if platform["id"] == "tving":
                return '<main>' + row() + '</main>'
            raise requests.exceptions.Timeout()
        result = c.collect_public_homes(fetcher=source, now=NOW)
        self.assertEqual(len(result["platforms"]), 5)
        self.assertEqual([p["id"] for p in result["platforms"] if p["status"] == "ok"], ["tving"])

    def test_global_runner_records_location_limit_and_stale_keeps_origin(self):
        with patch.dict(c.os.environ, {"GITHUB_ACTIONS": "true"}):
            old = c.collect_public_homes(platform_ids=["tving"], fetcher=lambda _: '<main>' + row() + '</main>', now=BEFORE)
        with patch.dict(c.os.environ, {"GITHUB_ACTIONS": "false"}):
            result = c.collect_public_homes(old, ["tving"], fetcher=lambda _: {"html": "", "http_status": 500}, now=NOW)
        tving = next(p for p in result["platforms"] if p["id"] == "tving")
        self.assertEqual(result["observed_environment"], "runtime")
        self.assertEqual(tving["observed_environment"], "github_actions_global")
        self.assertEqual(tving["attempt_environment"], "runtime")
        self.assertFalse(tving["country_verified"])
        self.assertEqual(tving["last_success_status"], "ok")

    def test_http_error_is_not_automatically_authentication_required(self):
        result = c.collect_public_homes(platform_ids=["tving"], fetcher=lambda _: {"html": "", "http_status": 403}, now=NOW)
        tving = next(p for p in result["platforms"] if p["id"] == "tving")
        self.assertEqual(tving["error_code"], "http_error")
        self.assertEqual(tving["access_mode"], "unconfirmed")

    def test_snapshot_roundtrip_and_invalid_input_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            self.assertEqual(c.load_snapshot(path)["platforms"], [])
            path.write_text("malformed", encoding="utf-8")
            self.assertEqual(c.load_snapshot(path)["platforms"], [])
            snapshot = c.collect_public_homes(platform_ids=["tving"], fetcher=lambda _: '<main>' + row() + '</main>', now=NOW)
            c.save_snapshot(snapshot, path)
            self.assertEqual(c.load_snapshot(path)["updated_at"], NOW)
            loaded = c.load_snapshot(path)
            self.assertEqual(next(p for p in loaded["platforms"] if p["id"] == "tving")["sections"][0]["works"][0]["title"], "작품 1")

    def test_snapshot_sanitizes_images_links_and_duplicate_platforms(self):
        entry = parse(row())
        entry["sections"][0]["works"][0]["image_url"] = "javascript:alert(1)"
        entry["sections"][0]["works"][0]["url"] = "https://tving.com.evil.example/contents/P1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps({"schema_version": 1, "updated_at": NOW, "platforms": [entry, entry]}), encoding="utf-8")
            loaded = c.load_snapshot(path)
        self.assertEqual(len(loaded["platforms"]), 1)
        work = loaded["platforms"][0]["sections"][0]["works"][0]
        self.assertEqual(work["image_url"], "")
        self.assertEqual(work["url"], "")

    def test_unsafe_url_protocol_credentials_and_private_hosts(self):
        for value in ["javascript:alert(1)", "data:image/png;base64,x", "file:///tmp/a", "https://user:password@example.org/", "http://127.0.0.1/a", "http://localhost/a", "https://example.org\n/a"]:
            self.assertEqual(c.safe_http_url(value), "", value)
        self.assertEqual(c.safe_http_url("/a.jpg", "https://image.tving.com/"), "https://image.tving.com/a.jpg")


if __name__ == "__main__":
    unittest.main()
