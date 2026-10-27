"""Category archives and article reading use saved public sources only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unittest

from media_insights_view import insight_items, resolve_insight_archive
from test_media_insights_view import analysis, app_fixture, document


NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def dated(identifier, days, **kwargs):
    return document(identifier, published=(NOW - timedelta(days=days)).isoformat(), **kwargs)


class CategoryArchiveTests(unittest.TestCase):
    def test_same_document_ad_evidence_supports_the_category_without_changing_primary(self):
        related = dated("earnings-with-ads", 180, topic="earnings_strategy")
        related["evidence"] = [{"id": "ads-paragraph", "url": related["url"],
                                "text": "Advertisers can now buy advertising on Netflix using Amazon Ads streaming audiences."}]
        unrelated = dated("retail-ai", 150, topic="earnings_strategy")
        unrelated["evidence"] = [{"id": "retail-paragraph", "url": unrelated["url"],
                                  "text": "AWS expands AI hardware and grocery retail subscriptions."}]
        rows = [dated("recent-ads", 5), dated("older-ads", 100), related, unrelated]
        before = deepcopy(rows)
        result = resolve_insight_archive(rows, topic="광고·요금제", now=NOW)
        self.assertEqual([row["id"] for row in result["items"]], ["recent-ads", "older-ads", "earnings-with-ads"])
        self.assertEqual(result["lookback_days"], 365)
        self.assertEqual(related["topic"], "earnings_strategy")
        self.assertEqual(rows, before)

    def test_stops_at_first_period_with_three_matching_articles(self):
        for ages, expected in (((5, 40, 80), 90), ((5, 80, 200), 365), ((5, 200, 700), None)):
            with self.subTest(ages=ages):
                rows = [dated(str(index), days) for index, days in enumerate(ages)]
                rows.append(dated("other-topic", 1, topic="sports_content"))
                before = deepcopy(rows)
                result = resolve_insight_archive(rows, topic="광고·요금제", now=NOW)
                self.assertEqual(result["lookback_days"], expected)
                self.assertEqual(result["initial_count"], 1)
                self.assertEqual([row["id"] for row in result["items"]], ["0", "1", "2"])
                self.assertTrue(result["expanded"])
                self.assertEqual(rows, before)

    def test_three_recent_articles_do_not_expand_and_whole_feed_remains_thirty_days(self):
        rows = [dated("recent-" + str(index), days) for index, days in enumerate((5, 10, 20))]
        rows.append(dated("historic", 100))
        category = resolve_insight_archive(rows, topic="광고·요금제", now=NOW)
        self.assertFalse(category["expanded"])
        self.assertEqual(category["lookback_days"], 30)
        whole = resolve_insight_archive([rows[0], rows[-1]], now=NOW)
        self.assertEqual([row["id"] for row in whole["items"]], ["recent-0"])
        self.assertFalse(whole["expanded"])
        self.assertEqual(whole["lookback_days"], 30)

    def test_explicit_period_is_the_starting_range(self):
        rows = [dated(str(index), days) for index, days in enumerate((5, 40, 80, 200))]
        selected = resolve_insight_archive(rows, topic="광고·요금제", lookback_days=90, now=NOW)
        self.assertFalse(selected["expanded"])
        self.assertEqual(selected["lookback_days"], 90)
        self.assertEqual(len(selected["items"]), 3)
        all_time = resolve_insight_archive(rows, topic="광고·요금제", lookback_days=None, now=NOW)
        self.assertFalse(all_time["expanded"])
        self.assertEqual(len(all_time["items"]), 4)

    def test_true_archive_with_fewer_than_three_remains_truthful(self):
        rows = [dated("only-one", 10), dated("only-two", 600), dated("unrelated", 20, topic="sports_content")]
        result = resolve_insight_archive(rows, topic="광고·요금제", now=NOW)
        self.assertIsNone(result["lookback_days"])
        self.assertEqual(result["available_count"], 2)
        self.assertEqual([row["id"] for row in result["items"]], ["only-one", "only-two"])

    def test_future_unknown_nonpublic_and_duplicate_sources_do_not_fill_the_minimum(self):
        original = dated("original", 40)
        duplicate_url = {**dated("duplicate-url", 50), "url": original["url"] + "?utm_source=feed#section"}
        rows = [original, duplicate_url, {**original, "published_at": (NOW - timedelta(days=60)).isoformat()},
                dated("future", -1), document("unknown", published=None), document("invalid", published="bad-date"),
                {**dated("private", 15), "url": "http://127.0.0.1/private"},
                dated("different-topic", 5, topic="ai_technology")]
        result = resolve_insight_archive(rows, topic="광고·요금제", now=NOW)
        self.assertEqual([row["id"] for row in result["items"]], ["original"])
        self.assertEqual(result["available_count"], 1)
        self.assertIsNone(result["lookback_days"])

    def test_domestic_scope_and_region_survive_historical_expansion(self):
        rows = [dated("kr-recent", 5, country="KR"), dated("kr-old", 100, country="KR"),
                {**dated("kr-product", 500, country="KR"), "market_scope": "domestic_product"},
                dated("us-recent", 1), dated("us-old", 40)]
        result = resolve_insight_archive(rows, domestic=True, country="한국", now=NOW)
        self.assertEqual([row["id"] for row in result["items"]], ["kr-recent", "kr-old", "kr-product"])
        self.assertTrue(all(row["country"] == "KR" for row in result["items"]))
        self.assertIsNone(result["lookback_days"])

    def test_complete_saved_archive_is_available_beyond_three_hundred_rows(self):
        rows = [dated("other-" + str(index), 5, topic="ai_technology") for index in range(301)]
        historical = [dated("archive-" + str(index), days) for index, days in enumerate((40, 100, 500))]
        rows.extend(historical)
        items = insight_items({"items": rows})
        result = resolve_insight_archive(items, topic="광고·요금제", now=NOW)
        self.assertEqual(len(items), 304)
        self.assertEqual([row["id"] for row in result["items"]], [row["id"] for row in historical])
        self.assertEqual([row["published_at"] for row in result["items"]],
                         [row["published_at"] for row in historical])


class ArticleReadingTests(unittest.TestCase):
    def data(self):
        current = datetime.now(timezone.utc)
        rows = []
        for index, days in enumerate((5, 40, 80)):
            item = document("reading-" + str(index), country="KR",
                            published=(current - timedelta(days=days)).isoformat())
            item["title"] = "광고 요금제 공식 발표 " + str(index)
            item["analysis"] = {**analysis(item), "headline": "저장된 분석 제목 " + str(index)}
            rows.append(item)
        rows.append(document("outside-region", country="US", published=current.isoformat()))
        return {"items": rows, "sources": []}

    def test_lower_article_updates_side_note_and_only_explicit_read_opens_dialog(self):
        app = app_fixture(self.data(), period="최근 90일")
        app.radio(key="media_insight_topic").set_value("광고·요금제").run()
        app.selectbox(key="media_insight_country").set_value("한국").run()
        self.assertFalse(app.exception)
        selected_id = "reading-2"
        key = "media_select_" + hashlib.sha256(selected_id.encode()).hexdigest()[:16]
        app.button(key=key).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["media_insight_selected_id"], selected_id)
        self.assertNotIn("media_insight_reading_id", app.session_state)
        self.assertEqual(len(app.get("dialog")), 0)
        side_note = " ".join(row.value for row in app.markdown)
        self.assertIn('id="media-insight-detail" data-item-id="reading-2"', side_note)
        self.assertIn("저장된 분석 제목 2", side_note)
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})
        app.button(key="media_insights_read_selected").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["media_insight_reading_id"], selected_id)
        dialog = app.get("dialog")
        self.assertEqual(len(dialog), 1)
        note = " ".join(row.value for row in dialog[0].markdown)
        self.assertIn('data-item-id="reading-2"', note)
        self.assertIn("저장된 분석 제목 2", note)
        self.assertNotIn("저장된 분석 제목 0", note)
        self.assertIn("확인한 사실", note)
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})
        app.button(key="media_reading_close").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.get("dialog")), 0)
        self.assertEqual(app.radio(key="media_insight_topic").value, "광고·요금제")
        self.assertEqual(app.selectbox(key="media_insight_country").value, "한국")
        self.assertEqual(app.selectbox(key="media_insight_period").value, "최근 90일")
        self.assertEqual(app.session_state["media_insight_selected_id"], selected_id)
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})

    def test_archive_article_selection_updates_the_side_note_without_opening_a_dialog(self):
        app = app_fixture(self.data(), period="최근 90일")
        selected_id = "reading-2"
        key = "media_archive_select_" + hashlib.sha256(selected_id.encode()).hexdigest()[:16]
        app.button(key=key).click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.session_state["media_insight_selected_id"], selected_id)
        self.assertNotIn("media_insight_reading_id", app.session_state)
        self.assertEqual(len(app.get("dialog")), 0)
        markup = " ".join(row.value for row in app.markdown)
        self.assertIn('id="media-insight-detail" data-item-id="reading-2"', markup)
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})

    def test_automatic_expansion_is_visible_without_changing_the_period_preference(self):
        app = app_fixture(self.data())
        app.radio(key="media_insight_topic").set_value("광고·요금제").run()
        app.selectbox(key="media_insight_country").set_value("한국").run()
        self.assertFalse(app.exception)
        markup = " ".join(row.value for row in app.markdown)
        self.assertIn('data-effective-days="90"', markup)
        self.assertIn("발표일은 원문 기준", markup)
        self.assertEqual(app.selectbox(key="media_insight_period").value, "최근 30일")
        self.assertEqual(len([button for button in app.button if str(button.key).startswith("media_select_")]), 3)
        app.radio(key="media_insight_topic").set_value("전체").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="media_insight_period").value, "최근 30일")
        self.assertNotIn('class="media-archive-expansion"', " ".join(row.value for row in app.markdown))
        self.assertTrue(any("수집 자료 보관함 · 1건" in expander.label for expander in app.expander))
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})

    def test_domestic_category_does_not_reset_a_conflicting_region_to_fill_results(self):
        app = app_fixture(self.data())
        app.selectbox(key="media_insight_country").set_value("미국").run()
        app.radio(key="media_insight_topic").set_value("국내 이슈").run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="media_insight_country").value, "미국")
        self.assertEqual(app.radio(key="media_insight_topic").value, "국내 이슈")
        self.assertEqual(app.selectbox(key="media_insight_period").value, "최근 30일")
        self.assertEqual(len([button for button in app.button if str(button.key).startswith("media_select_")]), 0)
        self.assertIn("전체 보관 자료는 0건", " ".join(row.value for row in app.markdown))
        self.assertEqual(app.session_state["test_callbacks"], {"refresh": 0, "analyze": 0, "ask": 0})


if __name__ == "__main__":
    unittest.main()
