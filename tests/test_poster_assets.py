"""Artwork must belong to the requested work, without changing its identity."""
import copy
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

import kinolights_candidates as kino
import poster_assets as posters


def response(payload, *, status=200, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status), response=result)
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
        posters._cached_image_health.cache_clear()
        posters._WIKIPEDIA_HOSTS.clear()
        self.sleep = self.enterContext(patch.object(posters.time, "sleep"))
        self.image_health = self.enterContext(patch.object(posters, "inspect_poster_url", return_value={"status": "available"}))

    def test_wikipedia_recovers_once_from_rate_limit_with_actual_artwork(self):
        page = dict(title="더 글로리 (드라마)", extract="2022년 드라마", pageimage="더글로리_포스터.jpg",
                    original={"source": "https://upload.wikimedia.org/actual-glory.jpg"})
        with patch.object(posters.requests, "get", side_effect=[
                response({}, status=429, headers={"Retry-After": "2"}),
                response({"query": {"pages": {"1": page}}})]) as get:
            result = posters._wikipedia_poster("더 글로리", "2022", "드라마")
        self.assertEqual(result["poster_url"], page["original"]["source"])
        self.assertEqual(get.call_count, 2)
        self.assertAlmostEqual(self.sleep.call_args.args[0], 2, delta=.05)
        self.assertIn("https://github.com/cig7624-art/btv-theme-curator", get.call_args.kwargs["headers"]["User-Agent"])
        self.assertEqual(get.call_args.kwargs["timeout"], 6)

    def test_wikipedia_503_honors_http_date_and_stops_after_one_retry(self):
        with patch.object(posters.time, "time", return_value=1000), \
             patch.object(posters.requests, "get", return_value=response(
                 {}, status=503, headers={"Retry-After": formatdate(1002, usegmt=True)})) as get:
            with self.assertRaises(requests.HTTPError):
                posters._wikipedia_pages("en", ["Example"])
        self.assertEqual(get.call_count, 2)
        self.assertAlmostEqual(self.sleep.call_args.args[0], 2, delta=.05)

    def test_long_retry_after_fails_without_waiting_or_repeated_requests(self):
        with patch.object(posters.requests, "get", return_value=response(
                {}, status=429, headers={"Retry-After": "120"})) as get:
            with self.assertRaises(requests.HTTPError):
                posters._wikipedia_pages("ko", ["더 글로리"])
            with self.assertRaises(requests.exceptions.RetryError):
                posters._wikipedia_pages("ko", ["다른 작품"])
        self.assertEqual(get.call_count, 1)
        self.sleep.assert_not_called()

    def test_wikipedia_host_rate_limit_is_shared_by_workers_and_not_other_hosts(self):
        clock = [100.0]
        dispatched = []

        def send(url, **kwargs):
            dispatched.append((url, clock[0]))
            return response({"query": {"pages": {}}})

        def advance(delay):
            clock[0] += delay

        self.sleep.side_effect = advance
        with patch.object(posters.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(posters.requests, "get", side_effect=send):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: posters._wikipedia_pages("ko", ["더 글로리"]), range(4)))
            posters._wikipedia_pages("en", ["The Glory (TV series)"])
        self.assertEqual([started for _, started in dispatched[:4]], [100, 100.5, 101, 101.5])
        self.assertEqual(dispatched[-1][1], 101.5)

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

    def test_tv_lookup_includes_the_requested_year_qualifier(self):
        page = dict(title="그녀는 예뻤다 (2015년 드라마)", extract="2015년 대한민국 드라마이다.",
                    pageimage="She_Was_Pretty_poster.jpg", original={"source": "https://upload.wikimedia.org/pretty-poster.jpg"})
        with patch.object(posters.requests, "get", return_value=response({"query": {"pages": {"1": page}}})) as get:
            self.assertEqual(posters._wikipedia_poster("그녀는 예뻤다", "2015", "드라마")["poster_url"], page["original"]["source"])
        get.assert_called_once()
        self.assertIn("그녀는 예뻤다 (2015년 드라마)", get.call_args.kwargs["params"]["titles"].split("|"))

    def test_one_title_search_recovers_only_exact_canonical_spelling(self):
        title = "닥터X:외과의다이몬미치코"
        page = dict(title="닥터-X: 외과의·다이몬 미치코", extract="2012년 일본의 드라마이다.",
                    categories=[{"title": "분류:2012년 시작한 일본 텔레비전 드라마"}],
                    pageimage="Doctor_X_poster.jpg", original={"source": "https://upload.wikimedia.org/doctorx-poster.jpg"})
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {"-1": {"title": title, "missing": ""}}}}),
                response({"query": {"pages": {"123": page}}})]) as get:
            result = posters._wikipedia_poster(title, "2012", "드라마")
        self.assertEqual(result["poster_url"], page["original"]["source"])
        self.assertEqual(get.call_count, 2)
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["generator"], "search")
        self.assertEqual(params["gsrsearch"], f'intitle:"{title}"')
        self.assertEqual(params["gsrlimit"], 5)
        self.assertNotIn("titles", params)

    def test_title_search_rejects_wrong_year_media_similar_title_and_ambiguous_work(self):
        base = dict(title="하얀 거탑 (2007년 드라마)", extract="2007년 텔레비전 드라마이다.",
                    pageimage="White_Tower_poster.jpg", original={"source": "https://upload.wikimedia.org/tower-poster.jpg"})
        alternatives = [dict(base, title="다른 거탑 (2007년 드라마)"),
                        dict(base, title="하얀 거탑 (2003년 드라마)", extract="2003년 드라마"),
                        dict(base, title="하얀 거탑 (2007년 영화)", extract="2007년 영화")]
        for page in alternatives:
            with self.subTest(page=page), patch.object(posters.requests, "get", side_effect=[
                    response({"query": {"pages": {}}}), response({"query": {"pages": {"1": page}}})]) as get:
                self.assertEqual(posters._wikipedia_poster("하얀거탑", "2007", "드라마"), {})
                self.assertEqual(get.call_count, 2)
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {}}}),
                response({"query": {"pages": {"1": base, "2": dict(base, title="하얀 거탑 (2003년 드라마)")}}})]) as get:
            self.assertEqual(posters._wikipedia_poster("하얀거탑", "", "드라마"), {})
        self.assertEqual(get.call_count, 2)

    def test_title_search_abstains_for_distinct_verified_pages_even_with_the_same_year(self):
        first = dict(pageid=1, title="하얀 거탑 (2007년 드라마)", extract="2007년 드라마",
                     pageimage="White_Tower_poster.jpg", original={"source": "https://upload.wikimedia.org/first-poster.jpg"})
        second = dict(first, pageid=2, title="하얀 거탑 (2007년 텔레비전 드라마)")
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {}}}),
                response({"query": {"pages": {"1": first, "2": second}}})]) as get:
            self.assertEqual(posters._wikipedia_poster("하얀거탑", "2007", "드라마"), {})
        self.assertEqual(get.call_count, 2)

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
        self.assertEqual(get.call_args_list[0].kwargs["params"]["lllang"], "en")
        self.assertEqual(get.call_args_list[0].kwargs["params"]["lllimit"], 10)

    def test_novel_identity_and_adaptation_categories_cannot_supply_tv_artwork(self):
        novel = dict(title="재혼 황후", extract="《재혼 황후》는 알파타르트가 연재한 로맨스 판타지 웹소설이다. 웹툰으로도 연재하고 있다.",
                     categories=[{"title": "분류:대한민국의 텔레비전 애니메이션"}, {"title": "분류:네이버 웹툰"}])
        self.assertFalse(posters._verified_wikipedia_page(novel, ["재혼 황후"], "", True))
        for qualifier in ["novel", "book", "comic", "webtoon", "소설", "웹툰", "만화"]:
            with self.subTest(qualifier=qualifier):
                english = dict(title=f"The Remarried Empress ({qualifier})", extract="A South Korean web novel series.",
                               categories=[{"title": "Category:Upcoming anime television series"}])
                self.assertFalse(posters._verified_wikipedia_page(english, ["The Remarried Empress"], "", True))
        for label in ["분류:드라마화된 소설", "Category:Television novels", "Category:South Korean novels adapted into television series"]:
            with self.subTest(label=label):
                page = dict(title="재혼 황후", extract="An original story.", categories=[{"title": label}])
                self.assertFalse(posters._verified_wikipedia_page(page, ["재혼 황후"], "", True))
        television = dict(title="재혼 황후 (드라마)", extract="2026년 대한민국의 판타지 드라마이다.",
                          categories=[{"title": "분류:대한민국의 웹 소설을 바탕으로 한 텔레비전 드라마"}])
        self.assertTrue(posters._verified_wikipedia_page(television, ["재혼 황후"], "2026", True))

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
                self.assertEqual(get.call_count, 2)
                self.assertEqual(get.call_args.kwargs["params"]["generator"], "search")

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

    def test_existing_image_is_repaired_only_after_a_definite_failure(self):
        item = dict(title="타짜", year="2006", poster_url="https://images.example.com/dead.jpg", reason="original reason")
        for status in ("available", "retryable", "invalid"):
            with self.subTest(status=status), patch.object(posters, "inspect_poster_url", side_effect=lambda url: {"status": status if "dead" in url else "available"}), \
                 patch.object(posters, "_cached_poster", return_value={"poster_url": "https://images.example.com/real.jpg"}) as lookup:
                result = posters.enrich_poster_candidates([item], repair_existing=True)[0]
            self.assertEqual(result["poster_url"], "https://images.example.com/real.jpg" if status == "invalid" else item["poster_url"])
            self.assertEqual(result["reason"], item["reason"])
            self.assertEqual(lookup.call_count, int(status == "invalid"))

    def test_unavailable_original_artwork_uses_actual_returned_smaller_artwork(self):
        returned = movie(artworkUrl600="https://is1-ssl.mzstatic.com/image/actual-600.jpg")
        with patch.object(posters.requests, "get", return_value=response({"results": [returned]})), \
             patch.object(posters, "inspect_poster_url", side_effect=[{"status": "invalid"}, {"status": "available"}]):
            result = posters._itunes_poster("타짜", "2006", "영화")
        self.assertEqual(result["poster_url"], returned["artworkUrl100"])

    def test_transient_first_image_still_tries_a_provider_returned_alternative(self):
        returned = movie(artworkUrl600="https://is1-ssl.mzstatic.com/image/actual-600.jpg")
        with patch.object(posters.requests, "get", return_value=response({"results": [returned]})), \
             patch.object(posters, "inspect_poster_url", side_effect=[{"status": "retryable"}, {"status": "available"}]):
            result = posters._itunes_poster("타짜", "2006", "영화")
        self.assertEqual(result["poster_url"], returned["artworkUrl100"])

    def test_removed_cached_image_is_resolved_again_instead_of_saved_again(self):
        dead = {"poster_url": "https://is1-ssl.mzstatic.com/dead.jpg"}
        live = {"poster_url": "https://is1-ssl.mzstatic.com/live.jpg"}
        identity = ("타짜", "2006", "영화")
        with patch.object(posters, "_itunes_poster", return_value=dead):
            self.assertEqual(posters._cached_poster(*identity, 123), dead)
        with patch.object(posters, "inspect_poster_url", side_effect=lambda url: {"status": "invalid" if "dead" in url else "available"}), \
             patch.object(posters, "_itunes_poster", return_value=live) as apple:
            self.assertEqual(posters._poster_for_refresh(identity, 123), live)
        apple.assert_called_once()

    def test_tvmaze_requires_exact_title_premiere_kind_and_unique_show_identity(self):
        show = dict(id=564, name="Mindhunter", premiered="2017-10-13", type="Scripted",
                    url="https://www.tvmaze.com/shows/564/mindhunter",
                    image={"original": "https://static.tvmaze.com/uploads/images/original_untouched/1/123.jpg"})
        alternatives = [[show], [dict(show, name="Mindhunter: Behind the Scenes")],
                        [dict(show, premiered="2018-10-13")], [dict(show, type="Feature Film")],
                        [show, dict(show, id=999)]]
        for index, shows in enumerate(alternatives):
            with self.subTest(index=index), patch.object(posters.requests, "get", return_value=response([{"score": 1, "show": value} for value in shows])) as get, \
                 patch.object(posters, "inspect_poster_url", return_value={"status": "available", "width": 200, "height": 300}):
                result = posters._tvmaze_poster("Mindhunter (TV series)", "2017")
            self.assertEqual(bool(result), index == 0)
            self.assertEqual(get.call_args.kwargs["params"], {"q": "Mindhunter"})
        with patch.object(posters.requests, "get") as get:
            self.assertEqual(posters._tvmaze_poster("Mindhunter (TV series)", ""), {})
        get.assert_not_called()

    def test_tvmaze_rejects_landscape_or_failed_actual_image_bytes(self):
        show = dict(id=564, name="Mindhunter", premiered="2017-10-13", type="Scripted",
                    image={"original": "https://static.tvmaze.com/uploads/actual.jpg"})
        for health in [{"status": "available", "width": 400, "height": 100}, {"status": "invalid"}]:
            with self.subTest(health=health), patch.object(posters.requests, "get", return_value=response([{"show": show}])), \
                 patch.object(posters, "inspect_poster_url", return_value=health):
                self.assertEqual(posters._tvmaze_poster("Mindhunter", "2017"), {})

    def test_verified_wikipedia_language_bridge_supplies_tvmaze_lookup(self):
        korean = dict(title="마인드헌터", extract="2017년 드라마", categories=[{"title": "분류:드라마"}],
                      langlinks=[{"lang": "en", "*": "Mindhunter (TV series)"}])
        english = dict(title="Mindhunter (TV series)", extract="A 2017 television series",
                       categories=[{"title": "Category:2017 television series debuts"}])
        with patch.object(posters, "_wikipedia_pages", side_effect=[[korean], [english]]), \
             patch.object(posters, "_wikipedia_artwork", return_value={}), \
             patch.object(posters, "_tvmaze_poster", return_value={"poster_url": "https://static.tvmaze.com/actual.jpg"}) as tvmaze:
            result = posters._wikipedia_poster("마인드헌터", "2017", "드라마")
        tvmaze.assert_called_once_with("Mindhunter (TV series)", "2017")
        self.assertTrue(result)

    def test_verified_english_movie_bridge_uses_us_itunes_without_title_guessing(self):
        korean = dict(title="인빅터스", extract="2009년 영화", categories=[{"title": "분류:2009년 영화"}],
                      langlinks=[{"lang": "en", "*": "Invictus (film)"}])
        english = dict(title="Invictus (film)", extract="A 2009 film", categories=[{"title": "Category:2009 films"}])
        with patch.object(posters, "_wikipedia_pages", side_effect=[[korean], [english]]), \
             patch.object(posters, "_wikipedia_artwork", return_value={}), \
             patch.object(posters, "_itunes_poster", return_value={"poster_url": "https://is1-ssl.mzstatic.com/actual.jpg"}) as apple:
            self.assertTrue(posters._wikipedia_poster("인빅터스", "2009", "영화"))
        apple.assert_called_once_with("Invictus", "2009", "영화", country="US")
        self.assertEqual(posters._english_work_title("Example (Part One)"), "Example (Part One)")

    def test_confirmed_empty_matches_are_cached_for_this_hour(self):
        with patch.object(posters, "_itunes_poster", return_value={}) as apple, \
             patch.object(posters, "_wikipedia_poster", return_value={}) as wiki:
            self.assertEqual(posters._cached_poster("타짜", "2006", "영화", 123), {})
            self.assertEqual(posters._cached_poster("타짜", "2006", "영화", 123), {})
        self.assertEqual(apple.call_count, 1)
        self.assertEqual(wiki.call_count, 1)

    def test_transient_provider_failures_are_not_cached_as_missing_artwork(self):
        for error in [requests.ConnectionError("offline"), requests.Timeout("timeout"),
                      requests.HTTPError("429", response=response({}, status=429)),
                      requests.HTTPError("503", response=response({}, status=503))]:
            with self.subTest(error=error), \
                 patch.object(posters, "_itunes_poster", return_value={}) as apple, \
                 patch.object(posters, "_wikipedia_poster", side_effect=error) as wiki:
                for _ in range(2):
                    with self.assertRaises(requests.RequestException):
                        posters._cached_poster("타짜", "2006", "영화", 123)
                self.assertEqual(apple.call_count, 2)
                self.assertEqual(wiki.call_count, 2)

    def test_infobox_timeout_remains_retryable_instead_of_becoming_an_empty_match(self):
        page = dict(title="더 글로리 (드라마)", extract="2022년 드라마", pageimage="The_Glory.jpg")
        with patch.object(posters.requests, "get", side_effect=[
                response({"query": {"pages": {"1": page}}}), requests.Timeout("timeout")]):
            with self.assertRaises(requests.Timeout):
                posters._cached_poster("더 글로리", "2022", "드라마", 123)
        self.assertEqual(posters._cached_poster.cache_info().currsize, 0)

    def test_batch_leaves_failed_lookups_blank_and_can_retry_on_refresh(self):
        item = dict(title="더 글로리", year="2022", content_type="드라마")
        artwork = {"poster_url": "https://upload.wikimedia.org/actual-glory.jpg"}
        with patch.object(posters, "_itunes_poster", return_value={}), \
             patch.object(posters, "_wikipedia_poster", side_effect=[requests.Timeout("timeout"), artwork]) as wiki:
            self.assertEqual(posters.enrich_poster_candidates([item])[0]["poster_url"], "")
            self.assertEqual(posters.enrich_poster_candidates([item])[0]["poster_url"], artwork["poster_url"])
        self.assertEqual(wiki.call_count, 2)

    def test_kinolights_nested_and_lazy_artwork_are_actual_urls_and_duplicate_prefers_artwork(self):
        mapped = kino._candidate_from_mapping(dict(title="타짜", poster={"url": "https://images.example.com/actual.jpg"}), "https://m.kinolights.com/search")
        self.assertEqual(mapped["poster_url"], "https://images.example.com/actual.jpg")
        markup = '<script type="application/json">{"title":"타짜"}</script><a href="/title/123">타짜<img alt="타짜" data-src="https://images.example.com/lazy.jpg"></a>'
        result = kino._parse_html(markup, "https://m.kinolights.com/search")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["poster_url"], "https://images.example.com/lazy.jpg")
        self.assertEqual(kino._image_url({"url": "javascript:alert(1)"}, "https://m.kinolights.com"), "")


class PosterImageHealthTests(unittest.TestCase):
    def setUp(self):
        posters._cached_image_health.cache_clear()
        self.enterContext(patch.object(posters.time, "sleep"))

    def image_response(self, *, status=200, media_type="image/png", data=None):
        if data is None:
            stream = BytesIO()
            Image.new("RGB", (100, 150), "red").save(stream, format="PNG")
            data = stream.getvalue()
        result = response({}, status=status, headers={"Content-Type": media_type})
        result.iter_content.return_value = iter([data])
        return result

    def test_actual_image_bytes_are_decoded_and_success_is_cached(self):
        with patch.object(posters.requests, "get", return_value=self.image_response()) as get:
            for _ in range(2):
                result = posters.inspect_poster_url("https://images.example.com/live.png")
        self.assertEqual(result, {"status": "available", "reason": "decoded_image", "width": 100, "height": 150})
        get.assert_called_once()
        self.assertTrue(get.call_args.kwargs["stream"])

    def test_http200_html_and_corrupt_image_are_definitely_invalid(self):
        for media_type, data, reason in [("text/html", b"<html>blocked</html>", "not_an_image"),
                                        ("image/png", b"not image bytes", "undecodable_image")]:
            posters._cached_image_health.cache_clear()
            with self.subTest(media_type=media_type), patch.object(posters.requests, "get", return_value=self.image_response(media_type=media_type, data=data)):
                self.assertEqual(posters.inspect_poster_url("https://images.example.com/wrong.png"), {"status": "invalid", "reason": reason})

    def test_truncated_jpeg_fails_actual_pixel_decode(self):
        stream = BytesIO()
        Image.new("RGB", (100, 150), "red").save(stream, format="JPEG")
        with patch.object(posters.requests, "get", return_value=self.image_response(media_type="image/jpeg", data=stream.getvalue()[:-2])):
            self.assertEqual(posters.inspect_poster_url("https://images.example.com/truncated.jpg"), {"status": "invalid", "reason": "undecodable_image"})

    def test_probe_budget_exhaustion_preserves_image_as_retryable(self):
        for budget in ("MAX_POSTER_BYTES", "MAX_POSTER_PIXELS"):
            posters._cached_image_health.cache_clear()
            with self.subTest(budget=budget), patch.object(posters, budget, 10), \
                 patch.object(posters.requests, "get", side_effect=lambda *a, **k: self.image_response()):
                self.assertEqual(posters.inspect_poster_url("https://images.example.com/large.png")["status"], "retryable")

    def test_missing_image_is_invalid_but_forbidden_and_outages_stay_retryable(self):
        for status, expected in [(404, "invalid"), (403, "retryable"), (429, "retryable"), (503, "retryable")]:
            posters._cached_image_health.cache_clear()
            with self.subTest(status=status), patch.object(posters.requests, "get", side_effect=lambda *a, **k: self.image_response(status=status)) as get:
                for _ in range(2):
                    self.assertEqual(posters.inspect_poster_url("https://images.example.com/remote.png")["status"], expected)
            self.assertEqual(get.call_count, 1 if status == 404 else 2 if status == 403 else 4)

    def test_network_timeout_is_retried_and_not_cached(self):
        with patch.object(posters.requests, "get", side_effect=requests.Timeout("offline")) as get:
            for _ in range(2):
                self.assertEqual(posters.inspect_poster_url("https://images.example.com/live.png")["status"], "retryable")
        self.assertEqual(get.call_count, 4)

    def test_event_photo_is_rejected_without_network_access(self):
        url = "https://upload.wikimedia.org/wikipedia/commons/e/ee/Nakache_Omar_Sy_Toledano_Lumi%C3%A8res_2012.jpg"
        with patch.object(posters.requests, "get") as get:
            self.assertFalse(posters.is_poster_url(url))
            self.assertEqual(posters.inspect_poster_url(url), {"status": "invalid", "reason": "cast_or_event_photo"})
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
