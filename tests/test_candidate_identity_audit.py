"""Public metadata reports conflicting identities without modifying candidates."""
import unittest
from unittest.mock import Mock

import requests

from scripts.audit_candidate_identity import audit_candidate, describe_page


def page(title, year, kind="영화", english="Experimenter (film)"):
    television = kind != "영화"
    return {"title": title, "pageid": year, "extract": f"이 작품은 {year}년 공개된 {kind} 작품이다.",
            "categories": [{"title": f"분류:{year}년 시작한 대한민국의 텔레비전 프로그램" if television else f"분류:{year}년 영화"}],
            "langlinks": [{"lang": "en", "*": english}]}


class CandidateIdentityAuditTests(unittest.TestCase):
    def test_same_title_film_year_does_not_confirm_the_requested_wrong_year(self):
        result = describe_page(page("밀그램 프로젝트", 2015), ("밀그램 프로젝트", "2021", "영화"))
        self.assertEqual(result["canonical_release_years"], ["2015"])
        self.assertEqual(result["canonical_content_type"], "영화")
        self.assertFalse(result["requested_identity_verified"])
        self.assertTrue(result["verified_source_url"].startswith("https://ko.wikipedia.org/wiki/"))

    def test_same_title_movie_and_variety_cannot_confirm_requested_drama(self):
        film = describe_page(page("룸메이트 (영화)", 2013), ("룸메이트", "2013", "드라마"))
        variety = describe_page(page("룸메이트 (텔레비전 프로그램)", 2014, "예능", "Roommate (TV program)"),
                                ("룸메이트", "2013", "드라마"))
        self.assertEqual((film["canonical_release_years"], film["canonical_content_type"]), (["2013"], "영화"))
        self.assertEqual((variety["canonical_release_years"], variety["canonical_content_type"]), (["2014"], "예능"))
        self.assertFalse(film["requested_identity_verified"])
        self.assertFalse(variety["requested_identity_verified"])

    def test_search_requires_exact_base_title_and_english_links_from_verified_work(self):
        wrong = page("다른 프로젝트", 2021, english="Wrong film")
        real = page("밀그램 프로젝트", 2015)
        provider = Mock(side_effect=[[wrong], [real], []])
        result = audit_candidate(("밀그램 프로젝트", "2021", "영화"), pages_provider=provider)
        self.assertFalse(result["requested_identity_confirmed"])
        self.assertEqual(result["verified_alternate_identities"], 1)
        self.assertFalse(result["korean_pages"][0]["exact_normalized_base_title"])
        self.assertEqual(provider.call_args.args, ("en", ["Experimenter (film)"]))

    def test_public_request_failure_is_reported_without_raw_error_or_mutation(self):
        provider = Mock(side_effect=requests.ConnectionError("private diagnostic text"))
        result = audit_candidate(("밀그램 프로젝트", "2021", "영화"), pages_provider=provider)
        self.assertEqual(result["errors"], ["public_metadata_unavailable"])
        self.assertNotIn("private diagnostic", repr(result))
        self.assertEqual(result["korean_pages"], [])


if __name__ == "__main__":
    unittest.main()
