"""Draft contents resolve identities without claiming service rights or writing DBs."""
import copy
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from content_pipeline import resolve_director_contents


def catalog():
    return pd.DataFrame([
        dict(content_id="C1", title="괴물", type="영화", year="2006", genre="스릴러", tags="가족,괴수"),
        dict(content_id="C2", title="괴물", type="드라마", year="2021", genre="스릴러", tags="수사"),
        dict(content_id="C3", title="더 글로리", type="드라마", year="2022", genre="드라마", tags="복수"),
    ])


class DirectorContentTests(unittest.TestCase):
    def test_local_title_year_and_type_confirm_identity_without_confirming_btv_rights(self):
        rows = resolve_director_contents([dict(title="괴 물", content_type="영화", year="2006", reason="가족을 지키는 선택")], catalog(), enrich_posters=False)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "괴물")
        self.assertEqual(rows[0]["content_id"], "C1")
        self.assertEqual(rows[0]["reason"], "가족을 지키는 선택")
        self.assertEqual(rows[0]["catalog_status"], "confirmed_local")
        self.assertEqual(rows[0]["availability_status"], "not_checked")
        self.assertIn("미확인", rows[0]["availability_label"])

    def test_ambiguous_title_and_wrong_year_or_type_cannot_take_another_work(self):
        invalid = [dict(title="괴물"), dict(title="괴물", year="2008"),
                   dict(title="괴물", content_type="드라마", year="2006"),
                   dict(content_id="C2", title="괴물", year="2006"), dict(content_id="missing", title="더 글로리")]
        self.assertEqual(resolve_director_contents(invalid, catalog(), enrich_posters=False), [])

    def test_catalog_id_without_title_resolves_the_actual_catalog_record(self):
        row = resolve_director_contents([dict(content_id="C3", reason="복수의 감정")], catalog(), enrich_posters=False)[0]
        self.assertEqual((row["title"], row["year"]), ("더 글로리", "2022"))

    def test_saved_poster_reuse_requires_same_title_year_type_and_ignores_suggestion_art(self):
        saved = pd.DataFrame([
            dict(title="괴물", year="2021", content_type="드라마", poster_url="https://image.example.org/wrong.jpg", kinolights_status="verified", source_url="https://m.kinolights.com/title/2"),
            dict(title="괴물", year="2006", content_type="영화", poster_url="https://image.example.org/right.jpg", kinolights_status="verified", source_url="https://m.kinolights.com/title/1"),
        ])
        row = resolve_director_contents([dict(title="괴물", year="2006", content_type="영화", poster_url="https://evil.example.org/fake.jpg")], catalog(), saved, enrich_posters=False)[0]
        self.assertEqual(row["poster_url"], "https://image.example.org/right.jpg")
        self.assertEqual(row["source_url"], "https://m.kinolights.com/title/1")
        self.assertEqual(row["kinolights_status"], "verified")

    def test_unknown_titles_require_verified_exact_external_metadata_not_search_links(self):
        saved = pd.DataFrame([
            dict(title="없는 작품", content_type="영화", year="2024", kinolights_status="search_link_only", source_url="https://m.kinolights.com/search?keyword=test"),
            dict(title="외부 작품", content_type="영화", year="2023", kinolights_status="verified", source_url="https://m.kinolights.com/title/real-id"),
        ])
        rows = resolve_director_contents([dict(title="없는 작품", year="2024", content_type="영화"), dict(title="외부 작품", year="2023", content_type="영화")], catalog(), saved, enrich_posters=False)
        self.assertEqual([row["title"] for row in rows], ["외부 작품"])
        self.assertEqual(rows[0]["catalog_status"], "verified_external")
        self.assertEqual(rows[0]["availability_status"], "not_checked")

    def test_pure_provider_rejects_loose_identity_and_failures_without_unknown_fillers(self):
        suggestion = [dict(title="새 작품", year="2024", content_type="영화")]
        for result in [dict(title="새 작품 2", year="2024", content_type="영화", status="verified", source_url="https://m.kinolights.com/title/2"),
                       dict(title="새 작품", year="2023", content_type="영화", status="verified", source_url="https://m.kinolights.com/title/3"),
                       dict(title="새 작품", year="2024", content_type="영화", status="search_link_only", source_url="https://m.kinolights.com/search")]:
            self.assertEqual(resolve_director_contents(suggestion, catalog(), provider=lambda *a, **k: result, enrich_posters=False), [])
        provider = Mock(side_effect=RuntimeError("metadata offline"))
        self.assertEqual(resolve_director_contents(suggestion, catalog(), provider=provider, enrich_posters=False), [])

    def test_pure_exact_external_provider_confirms_identity_but_not_catalog_or_availability(self):
        provider = Mock(return_value=dict(title="외부 새 작품", year="2024", content_type="영화", status="verified",
                                          source_url="https://m.kinolights.com/title/confirmed-work",
                                          poster_url="https://image.example.org/external.jpg", availability_status="available"))
        row = resolve_director_contents([dict(title="외부 새 작품", year="2024", content_type="영화", reason="테마와 연결되는 이유")], catalog(), provider=provider, enrich_posters=False)[0]
        self.assertEqual(row["catalog_status"], "verified_external")
        self.assertEqual(row["content_id"], "")
        self.assertEqual(row["kinolights_status"], "verified")
        self.assertEqual(row["availability_status"], "not_checked")
        self.assertEqual(row["reason"], "테마와 연결되는 이유")
        provider.assert_called_once_with("외부 새 작품", requested_year="2024", requested_type="영화")

    def test_exclusions_and_duplicate_titles_are_applied_before_poster_lookups(self):
        enrich = Mock(side_effect=lambda rows, **kwargs: rows)
        rows = resolve_director_contents([dict(title="더 글로리"), dict(title="괴물", year="2006"), dict(title="괴 물", year="2006")], catalog(), excluded_titles=["더글로리"], poster_enricher=enrich)
        self.assertEqual([row["title"] for row in rows], ["괴물"])
        self.assertEqual(len(enrich.call_args.args[0]), 1)

    def test_poster_failure_keeps_verified_identity_and_does_not_mutate_input(self):
        contents = catalog()
        suggestions = [dict(title="더 글로리", reason="복수의 감정")]
        before_contents, before_suggestions = contents.copy(deep=True), copy.deepcopy(suggestions)
        with patch("pathlib.Path.write_text", side_effect=AssertionError("draft must not write")), patch("pandas.DataFrame.to_csv", side_effect=AssertionError("draft must not save CSV")):
            rows = resolve_director_contents(suggestions, contents, poster_enricher=Mock(side_effect=RuntimeError("posters unavailable")))
        pd.testing.assert_frame_equal(contents, before_contents)
        self.assertEqual(suggestions, before_suggestions)
        self.assertEqual(rows[0]["poster_url"], "")
        self.assertEqual(rows[0]["catalog_status"], "confirmed_local")

    def test_poster_enrichment_only_copies_art_fields_for_the_same_identity(self):
        def enrich(rows, **kwargs):
            return [{**rows[0], "poster_url": "https://image.example.org/genuine.jpg", "availability_status": "available", "reason": "replace user reason"}]
        row = resolve_director_contents([dict(title="더 글로리", reason="원래 추천 이유")], catalog(), poster_enricher=enrich)[0]
        self.assertEqual(row["poster_url"], "https://image.example.org/genuine.jpg")
        self.assertEqual(row["reason"], "원래 추천 이유")
        self.assertEqual(row["availability_status"], "not_checked")
        wrong = lambda rows, **kwargs: [{**rows[0], "year": "2000", "poster_url": "https://image.example.org/wrong.jpg"}]
        row = resolve_director_contents([dict(title="더 글로리")], catalog(), poster_enricher=wrong)[0]
        self.assertEqual(row["poster_url"], "")

    def test_unsafe_artwork_and_credentials_never_reach_the_draft(self):
        contents = pd.DataFrame([dict(content_id="C1", title="확인 작품", type="영화", year="2023", poster_url="javascript:alert(1)", source_url="https://user:secret@imdb.com/title/1")])
        row = resolve_director_contents([dict(title="확인 작품")], contents, enrich_posters=False)[0]
        self.assertEqual(row["poster_url"], "")
        self.assertNotIn("secret", row["source_url"])

    def test_normalized_title_collisions_keep_remake_ambiguity(self):
        contents = pd.DataFrame([
            dict(content_id="C1", title="괴 물", type="영화", year="2006"),
            dict(content_id="C2", title="괴물", type="드라마", year="2021"),
        ])
        self.assertEqual(resolve_director_contents([dict(title="괴물")], contents, enrich_posters=False), [])
        row = resolve_director_contents([dict(title="괴물", year="2006")], contents, enrich_posters=False)[0]
        self.assertEqual(row["content_id"], "C1")

        saved = contents.rename(columns={"type": "content_type"}).assign(
            kinolights_status="verified", source_url="https://m.kinolights.com/title/verified")
        self.assertEqual(resolve_director_contents([dict(title="괴 물")], pd.DataFrame(), saved, enrich_posters=False), [])

    def test_local_identity_and_art_keep_precedence_over_saved_metadata(self):
        contents = pd.DataFrame([
            dict(content_id="local", title="괴물", type="영화", year="2006",
                 source_url="https://imdb.com/title/local", poster_url="https://images.example.org/local.jpg"),
        ])
        saved = pd.DataFrame([
            dict(title="괴 물", content_type="영화", year="2006", source_url="https://imdb.com/title/no-art"),
            dict(title="괴물", content_type="영화", year="2006", kinolights_status="verified",
                 source_url="https://m.kinolights.com/title/saved", poster_url="https://images.example.org/saved.jpg"),
            dict(title="괴물", content_type="드라마", year="2021", kinolights_status="verified",
                 source_url="https://m.kinolights.com/title/remake", poster_url="https://images.example.org/remake.jpg"),
        ])
        row = resolve_director_contents([dict(title="괴물", year="2006")], contents, saved, enrich_posters=False)[0]
        self.assertEqual(row["content_id"], "local")
        self.assertEqual(row["catalog_status"], "confirmed_local")
        self.assertEqual(row["poster_url"], "https://images.example.org/local.jpg")
        self.assertEqual(row["source_url"], "https://m.kinolights.com/title/saved")
        self.assertEqual(row["kinolights_status"], "verified")

    def test_catalog_and_candidate_edits_are_used_on_each_resolution(self):
        contents = pd.DataFrame([dict(content_id="C1", title="첫 작품", type="영화", year="2024")])
        saved = pd.DataFrame([
            dict(title="첫 작품", content_type="영화", year="2024", poster_url="https://images.example.org/first.jpg"),
        ])
        first = resolve_director_contents([dict(content_id="C1")], contents, saved, enrich_posters=False)[0]
        self.assertEqual(first["title"], "첫 작품")
        self.assertEqual(first["poster_url"], "https://images.example.org/first.jpg")

        contents.loc[0, "title"] = "새 작품"
        saved.loc[0, ["title", "poster_url"]] = ["새 작품", "https://images.example.org/new.jpg"]
        self.assertEqual(resolve_director_contents([dict(title="첫 작품")], contents, saved, enrich_posters=False), [])
        second = resolve_director_contents([dict(content_id="C1")], contents, saved, enrich_posters=False)[0]
        self.assertEqual(second["title"], "새 작품")
        self.assertEqual(second["poster_url"], "https://images.example.org/new.jpg")


if __name__ == "__main__":
    unittest.main()
