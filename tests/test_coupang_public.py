"""Official public-channel fallback never pretends to be the Coupang home."""
import csv
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from coupang_public import CHANNEL_ID, CHANNELS_API, PLAYLIST_API, RSS_URL, collect_coupang_updates

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
VID = "p7y7I-NAKKA"
OTHER = "URotrGSSHf0"


def cached(**changes):
    return {"video_id": VID, "title": "퇴사한 김원훈 [직장인들 시즌3] 9화 예고편", "channel_id": CHANNEL_ID,
            "url": "https://www.youtube.com/watch?v=" + VID, "upload_date": "2026-10-21",
            "last_discovered_at": "2026-10-22T01:19:06+00:00", "related_content": "직장인들 시즌3", **changes}


def unavailable(*args, **kwargs):
    raise requests.ConnectionError("network unavailable")


class CoupangPublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "watchlist.csv"

    def cache(self, rows):
        fields = list(dict.fromkeys(k for row in rows for k in row))
        with self.path.open("w", newline="", encoding="utf-8") as f:
            out = csv.DictWriter(f, fieldnames=fields, delimiter="|")
            out.writeheader(); out.writerows(rows)

    def collect(self, **changes):
        return collect_coupang_updates(self.path, api_key="", now=NOW, fetcher=unavailable, **changes)

    def test_cache_preserves_real_observation_and_identifies_actual_official_work(self):
        self.cache([cached()])
        result = self.collect()
        self.assertEqual(result["source_scope"], "official_channel")
        self.assertEqual(result["source_method"], "verified_watchlist")
        self.assertEqual(result["observed_at"], "2026-10-22T01:19:06+00:00")
        row = result["updates"][0]
        self.assertEqual(row["work_title"], "직장인들 시즌3")
        self.assertEqual(row["title"], cached()["title"])
        self.assertEqual(row["update_kind"], "trailer")
        self.assertEqual(row["published_at"], "2026-10-21")
        self.assertIn(VID, row["image_url"])
        self.assertNotIn("home", result)

    def test_cache_rejects_name_spoofs_inconsistent_urls_stale_and_bad_dates(self):
        invalid = [cached(channel_id="other-channel"), cached(url="https://www.youtube.com/watch?v=" + OTHER),
                   cached(url="https://youtube.com.evil.example/watch?v=" + VID),
                   cached(last_discovered_at="2026-10-01T00:00:00Z"), cached(last_discovered_at=""),
                   cached(upload_date="2026-10-23"), cached(upload_date="2026-09-01"),
                   cached(last_discovered_at="2026-10-24T00:00:00Z"), cached(upload_date="bad")]
        self.cache(invalid)
        self.assertIsNone(self.collect())

    def test_generic_unidentified_work_ads_and_sports_clips_are_not_filled_in(self):
        self.cache([cached(title="원훈 주임, 왜 내 이름 똑바로 불러?", related_content=""),
                    cached(title="[축구] 프리미어리그 하이라이트", related_content="축구"),
                    cached(title="[직장인들 시즌3] 구독 이벤트", related_content="직장인들 시즌3")])
        self.assertIsNone(self.collect())

    def test_duplicate_work_prefers_trailer_to_newer_clip_and_keeps_distinct_work(self):
        self.cache([cached(), cached(video_id="nlNRrVC9bQw", url="https://www.youtube.com/watch?v=nlNRrVC9bQw",
                                    title="[직장인들 시즌3] 재미있는 순간", upload_date="2026-10-22"),
                    cached(video_id=OTHER, url="https://www.youtube.com/watch?v=" + OTHER,
                           title="[너 말고 다른 연애] 9화 예고", related_content="너 말고 다른 연애")])
        rows = self.collect()["updates"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["work_title"] for r in rows}, {"직장인들 시즌3", "너 말고 다른 연애"})
        self.assertTrue(all(r["update_kind"] == "trailer" for r in rows))

    def test_api_uses_verified_uploads_playlist_and_rejects_other_video_owner(self):
        called = []
        def fetch(url, *, params=None, timeout=None):
            called.append((url, params))
            if url == CHANNELS_API:
                return {"items": [{"id": CHANNEL_ID, "contentDetails": {"relatedPlaylists": {"uploads": "UUjn-VbcIkAeXQKCmLJV8YwQ"}}}]}
            self.assertEqual(url, PLAYLIST_API)
            def item(owner, vid):
                return {"snippet": {"channelId": CHANNEL_ID, "videoOwnerChannelId": owner,
                                    "title": "[직장인들 시즌3] 메인 예고편", "resourceId": {"videoId": vid},
                                    "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg"}}},
                        "contentDetails": {"videoId": vid, "videoPublishedAt": "2026-10-21T08:00:00Z"}}
            return {"items": [item("wrong-owner", OTHER), item(CHANNEL_ID, VID)]}
        result = collect_coupang_updates(self.path, api_key="do-not-output-key", now=NOW, fetcher=fetch)
        self.assertEqual([u for u, p in called], [CHANNELS_API, PLAYLIST_API])
        self.assertEqual(result["source_method"], "youtube_api")
        self.assertEqual(result["updates"][0]["video_id"], VID)
        self.assertNotIn("do-not-output-key", str(result))
        self.assertEqual(result["observed_at"], "2026-10-22T12:00:00+00:00")

    def test_rss_verifies_feed_and_entry_channel_with_real_link_and_thumbnail(self):
        xml = f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
        <yt:channelId>{CHANNEL_ID}</yt:channelId><entry><yt:videoId>{VID}</yt:videoId><yt:channelId>{CHANNEL_ID}</yt:channelId>
        <title>[직장인들 시즌3] 9화 예고편</title><link rel="alternate" href="https://www.youtube.com/watch?v={VID}"/>
        <published>2026-10-21T10:00:00Z</published><media:group><media:thumbnail url="https://i.ytimg.com/vi/{VID}/hqdefault.jpg"/></media:group></entry></feed>'''
        def fetch(url, **kwargs):
            self.assertEqual(url, RSS_URL)
            return xml
        result = collect_coupang_updates(self.path, api_key="", now=NOW, fetcher=fetch)
        self.assertEqual(result["source_method"], "youtube_rss")
        self.assertEqual(result["updates"][0]["published_at"], "2026-10-21T10:00:00Z")
        self.assertEqual(result["updates"][0]["image_url"], "https://i.ytimg.com/vi/" + VID + "/hqdefault.jpg")
        self.assertIsNone(collect_coupang_updates(self.path, api_key="", now=NOW,
                                               fetcher=lambda *a, **k: xml.replace(CHANNEL_ID, "spoof")))

    def test_missing_network_and_missing_cache_returns_none_without_fake_news(self):
        self.assertIsNone(self.collect())


if __name__ == "__main__":
    unittest.main()
