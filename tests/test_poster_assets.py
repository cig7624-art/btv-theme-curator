"""Artwork must belong to the requested work, without changing its identity."""
import copy
import unittest
from unittest.mock import Mock, patch

import requests

import kinolights_candidates as kino
import poster_assets as posters


def response(payload):
    result = Mock()
    result.json.return_value = payload
    return result


def movie(**changes):
    result = dict(kind="feature-movie", trackName="타짜", releaseDate="2006-09-28T07:00:00Z",
                  artworkUrl100="https://is1-ssl.mzstatic.com/image/actual-100.jpg",
                  trackViewUrl="https://tv.apple.com/kr/movie/actual-id")
    result.update(changes)
    return result


class PosterAssetTests(unittest.TestCase):
    def setUp(self):
        posters._cached_poster.cache_clear()

    def test_movie_provider_accepts_exact_title_year_and_actual_returned_artwork(self):
        returned = movie()
        with patch.object(posters.requests, "get", return_value=response({"results": [returned]})):
            result = posters._itunes_poster("타 짜", "2006", "영화")
        self.assertEqual(result["poster_url"], returned["artworkUrl100"])
        self.assertEqual(result["poster_source_url"], returned["trackViewUrl"])
        self.assertNotIn("600", result["poster_url"])

    def test_movie_provider_rejects_different_title_remake_year_music_and_foreign_artwork_host(self):
        invalid = [movie(trackName="타짜: 신의 손"), movie(releaseDate="2014-09-03"),
                   movie(kind="song"), movie(artworkUrl100="https://unrelated.example.com/poster.jpg")]
        for item in invalid:
            with self.subTest(item=item), patch.object(posters.requests, "get", return_value=response({"results": [item]})):
                self.assertEqual(posters._itunes_poster("타짜", "2006", "영화"), {})

    def test_tv_series_never_takes_same_named_movie_poster(self):
        with patch.object(posters.requests, "get") as get:
            self.assertEqual(posters._itunes_poster("타짜", "2008", "드라마"), {})
        get.assert_not_called()

    def test_missing_year_does_not_pick_the_first_of_multiple_same_named_films(self):
        first = movie(trackId=111)
        remake = movie(trackId=222, releaseDate="2014-09-03", artworkUrl100="https://is1-ssl.mzstatic.com/other-poster.jpg")
        with patch.object(posters.requests, "get", return_value=response({"results": [remake, first]})):
            self.assertEqual(posters._itunes_poster("타짜", "", "영화"), {})
            self.assertEqual(posters._itunes_poster("타짜", "2006", "영화")["poster_url"], first["artworkUrl100"])

    def test_wikipedia_does_not_confuse_tv_adaptation_and_same_named_film(self):
        page = dict(title="타짜 (드라마)", extract="타짜는 2006년 영화를 원작으로 한 2008년 드라마이다.",
                    pageimage="타짜_드라마_포스터.jpg", original={"source": "https://upload.wikimedia.org/tv-poster.jpg"})
        with patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": page}}})):
            self.assertEqual(posters._wikipedia_poster("타짜", "2008", "영화"), {})
            self.assertEqual(posters._wikipedia_poster("타짜", "2008", "드라마")["poster_url"], page["original"]["source"])

    def test_wikipedia_abstains_when_missing_year_leaves_multiple_films(self):
        first = dict(title="타짜 (2006년 영화)", extract="2006년 영화", pageimage="타짜_포스터.jpg",
                     original={"source": "https://upload.wikimedia.org/first-poster.jpg"})
        remake = dict(title="타짜 (2014년 영화)", extract="2014년 영화", pageimage="타짜_리메이크_포스터.jpg",
                      original={"source": "https://upload.wikimedia.org/remake-poster.jpg"})
        with patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": first, "2": remake}}})):
            self.assertEqual(posters._wikipedia_poster("타짜", "", "영화"), {})

    def test_wikipedia_accepts_named_film_poster_but_not_cast_photo(self):
        page = dict(title="타짜 (영화)", extract="타짜는 2006년 개봉한 대한민국 영화이다.",
                    pageimage="타짜_영화_포스터.jpg", original={"source": "https://upload.wikimedia.org/actual-poster.jpg"})
        with patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": page}}})):
            self.assertEqual(posters._wikipedia_poster("타짜", "2006", "영화")["poster_url"], page["original"]["source"])
        page["pageimage"] = "배우_단체사진.jpg"
        with patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": page}}})):
            self.assertEqual(posters._wikipedia_poster("타짜", "2006", "영화"), {})

    def test_wikipedia_rejects_similar_title_wrong_year_and_disambiguation(self):
        base = dict(title="타짜 (영화)", extract="2006년 대한민국 영화", pageimage="타짜_포스터.jpg",
                    original={"source": "https://upload.wikimedia.org/actual-poster.jpg"})
        for changes in [dict(title="타짜: 신의 손"), dict(extract="2014년 영화"), dict(pageprops={"disambiguation": ""})]:
            with self.subTest(changes=changes), patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": dict(base, **changes)}}})):
                self.assertEqual(posters._wikipedia_poster("타짜", "2006", "영화"), {})

    def test_verified_media_infobox_supplies_actual_artwork_without_poster_filename(self):
        page = dict(pageid=100, title="더 글로리", extract="2022년 대한민국 드라마이다.",
                    categories=[{"title": "분류:대한민국의 드라마"}], pageimage="The_Glory.jpg")
        markup = '<table class="infobox"><tr><td class="infobox-image"><img src="//upload.wikimedia.org/art/The_Glory.jpg" width="250" height="370"></td></tr></table>'
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {"100": page}}}),
                response({"parse": {"title": "더 글로리", "text": {"*": markup}}})]) as get:
            result = posters._wikipedia_poster("더 글로리", "2022", "드라마")
        self.assertEqual(result["poster_url"], "https://upload.wikimedia.org/art/The_Glory.jpg")
        self.assertIn("ko.wikipedia.org/wiki/", result["poster_source_url"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pageid"], 100)

    def test_korean_article_follows_declared_english_identity_for_the_glory(self):
        korean = dict(pageid=100, title="더 글로리", extract="대한민국 드라마이다.",
                      categories=[{"title": "분류:2022년 드라마"}],
                      langlinks=[{"lang": "en", "*": "The Glory"}])
        english = dict(pageid=200, title="The Glory", extract="A South Korean television series.",
                       categories=[{"title": "Category:2022 South Korean television series debuts"}],
                       pageimage="The_Glory.png")
        markup = '<table class="infobox"><tr><td class="infobox-image"><img src="https://upload.wikimedia.org/art/The_Glory.png" width="220" height="330"><div>Promotional artwork</div></td></tr></table>'
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {"100": korean}}}),
                response({"parse": {"title": "더 글로리", "text": {"*": "<p>No infobox art</p>"}}}),
                response({"query": {"pages": {"200": english}}}),
                response({"parse": {"title": "The Glory", "text": {"*": markup}}})]) as get:
            result = posters._wikipedia_poster("더 글로리", "2022", "드라마")
        self.assertEqual(result["poster_url"], "https://upload.wikimedia.org/art/The_Glory.png")
        self.assertEqual(result["poster_source_url"], "https://en.wikipedia.org/wiki/The_Glory")
        self.assertEqual(get.call_args_list[2].kwargs["params"]["titles"], "The Glory")

    def test_infobox_rejects_cast_photo_logo_and_unrelated_article(self):
        page = dict(title="더 글로리", extract="2022년 드라마", categories=[{"title": "분류:드라마"}])
        samples = [
            ('더 글로리', '<table class="infobox"><td class="infobox-image"><img src="https://upload.wikimedia.org/cast.jpg" width="200" height="300">Cast photo</td></table>'),
            ('더 글로리', '<table class="infobox"><td class="infobox-image"><img src="https://upload.wikimedia.org/logo.png" width="200" height="300">Official logo</td></table>'),
            ('다른 드라마', '<table class="infobox"><td class="infobox-image"><img src="https://upload.wikimedia.org/actual.jpg" width="200" height="300">Promotional poster</td></table>'),
        ]
        for parsed_title, markup in samples:
            with self.subTest(title=parsed_title, markup=markup), patch.object(posters.requests, "get", side_effect=[
                    response({"query": {"pages": {"100": page}}}),
                    response({"parse": {"title": parsed_title, "text": {"*": markup}}})]):
                self.assertEqual(posters._wikipedia_poster("더 글로리", "2022", "드라마"), {})

    def test_english_link_must_retain_requested_year_and_media_kind(self):
        korean = dict(title="더 글로리", extract="2022년 드라마", categories=[{"title": "분류:드라마"}],
                      langlinks=[{"lang": "en", "*": "The Glory"}])
        for english in [dict(title="The Glory (2020 film)", extract="2020 film"),
                        dict(title="The Glory", extract="A 2023 television series", categories=[{"title": "Category:2023 television series debuts"}])]:
            with self.subTest(page=english), patch.object(posters.requests, "get", side_effect=[
                    response({"query": {"pages": {"1": korean}}}),
                    response({"parse": {"text": {"*": ""}}}),
                    response({"query": {"pages": {"2": english}}})]) as get:
                self.assertEqual(posters._wikipedia_poster("더 글로리", "2022", "드라마"), {})
                self.assertEqual(get.call_count, 3)

    def test_missing_english_identity_does_not_guess_a_translation(self):
        page = dict(title="더 글로리", extract="2022년 드라마", categories=[{"title": "분류:드라마"}])
        with patch.object(posters.requests, "get", side_effect=[response({"query": {"pages": {"1": page}}}),
                                                               response({"parse": {"text": {"*": ""}}})]) as get:
            self.assertEqual(posters._wikipedia_poster("더 글로리", "2022", "드라마"), {})
        self.assertEqual(get.call_count, 2)

    def test_release_category_rejects_incidental_sequel_or_later_season_year(self):
        pages = [
            ("타짜", "2014", "영화", dict(title="타짜 (영화)", extract="타짜는 2006년 영화이다. 2014년 속편이 개봉했다.",
                                      categories=[{"title": "분류:2006년 영화"}], pageimage="타짜_포스터.jpg",
                                      original={"source": "https://upload.wikimedia.org/tazza2006.jpg"})),
            ("Example", "2021", "드라마", dict(title="Example (TV series)", extract="The series premiered in 2020. Its next season aired in 2021.",
                                             categories=[{"title": "Category:2020 television series debuts"}], pageimage="Example_poster.jpg",
                                             original={"source": "https://upload.wikimedia.org/example2020.jpg"})),
        ]
        for title, year, kind, page in pages:
            with self.subTest(title=title), patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": page}}})) as get:
                self.assertEqual(posters._wikipedia_poster(title, year, kind), {})
                self.assertEqual(get.call_count, 1)

    def test_batch_preserves_existing_artwork_identity_and_status_without_mutating_input(self):
        items = [dict(title="기생충", year="2019", poster_url="https://image.example.com/actual.jpg", kinolights_status="verified"),
                 dict(title="타짜", year="2006", source_url="https://m.kinolights.com/search?q=타짜", kinolights_status="search_link_only")]
        before = copy.deepcopy(items)
        with patch.object(posters, "_cached_poster", return_value={"poster_url": "https://is1-ssl.mzstatic.com/actual.jpg"}) as lookup:
            result = posters.enrich_poster_candidates(items)
        lookup.assert_called_once()
        self.assertEqual(items, before)
        self.assertEqual(result[0], items[0])
        self.assertEqual(result[1]["kinolights_status"], "search_link_only")
        self.assertEqual(result[1]["source_url"], items[1]["source_url"])
        self.assertEqual(result[1]["title"], "타짜")

    def test_batch_deduplicates_work_lookups_and_respects_explicit_limit(self):
        items = [dict(title="타짜", year="2006"), dict(title="타짜", year="2006"), dict(title="화차", year="2012")]
        with patch.object(posters, "_cached_poster", return_value={"poster_url": "https://is1-ssl.mzstatic.com/actual.jpg"}) as lookup:
            result = posters.enrich_poster_candidates(items, limit=1)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(result[0]["poster_url"], result[1]["poster_url"])
        self.assertEqual(result[2]["poster_url"], "")

    def test_unavailable_providers_leave_blank_and_cache_failure_for_this_hour(self):
        with patch.object(posters, "_itunes_poster", side_effect=requests.ConnectionError("offline")) as apple, \
             patch.object(posters, "_wikipedia_poster", return_value={}) as wiki:
            self.assertEqual(posters._cached_poster("타짜", "2006", "영화", 123), {})
            self.assertEqual(posters._cached_poster("타짜", "2006", "영화", 123), {})
        self.assertEqual(apple.call_count, 1)
        self.assertEqual(wiki.call_count, 1)

    def test_kinolights_nested_and_lazy_artwork_are_actual_urls_and_duplicate_prefers_artwork(self):
        mapped = kino._candidate_from_mapping(dict(title="타짜", poster={"url": "https://images.example.com/actual.jpg"}), "https://m.kinolights.com/search")
        self.assertEqual(mapped["poster_url"], "https://images.example.com/actual.jpg")
        markup = '<script type="application/json">{"title":"타짜"}</script><a href="/title/123">타짜<img alt="타짜" data-src="https://images.example.com/lazy.jpg"></a>'
        result = kino._parse_html(markup, "https://m.kinolights.com/search")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["poster_url"], "https://images.example.com/lazy.jpg")
        self.assertEqual(kino._image_url({"url": "javascript:alert(1)"}, "https://m.kinolights.com"), "")


if __name__ == "__main__":
    unittest.main()
