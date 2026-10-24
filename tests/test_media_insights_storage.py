"""Insights writes must preserve collector updates, source provenance and CSVs."""
import base64
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from media_insights_storage import (
    MediaInsightsStorageError, merge_media_snapshots,
    persist_media_analysis, persist_media_snapshot, persist_media_localizations,
)


CONFIG = {"token": "PRIVATE-TOKEN", "repo": "owner/repo", "branch": "main"}


def document(identifier, *, content_hash=None, collected_at="2026-10-23T08:00:00+00:00"):
    return {"id": identifier, "title": "공식 상품 발표 " + identifier,
            "url": "https://official.example/" + identifier,
            "content_hash": content_hash or identifier + "-hash",
            "collected_at": collected_at,
            "evidence": [{"id": identifier + "-p1", "text": "해당 지역의 상품 조건을 발표했습니다.",
                          "locator": "문단 1", "url": "https://official.example/" + identifier}]}


def snapshot(*items, collected_at="2026-10-23T08:00:00+00:00"):
    return {"schema_version": 1, "collected_at": collected_at,
            "sources": [{"id": "official", "status": "ok", "last_checked_at": collected_at,
                         "last_success_at": collected_at}], "items": list(items)}


def analysis(item, *, context=None, stamp="2026-10-23T09:00:00+00:00"):
    refs = [item["evidence"][0]["id"]]
    hashes = {item["id"]: item["content_hash"]}
    if context:
        refs.append(context["evidence"][0]["id"])
        hashes[context["id"]] = context["content_hash"]
    return {"summary": "공식 상품 조건에 대한 분석", "facts": [{"text": "상품 조건을 발표했습니다.", "evidence_ids": refs}],
            "interpretation": "지역별 조건을 함께 검토할 수 있습니다.", "btv_options": ["제휴 조건 검토"],
            "questions": ["적용 지역은 어디인가요?"], "comparison": [],
            "generated_at": stamp, "model": "test-model", "source_hash": item["content_hash"],
            "context_hashes": hashes}


def payload(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def localization(item, *, stamp="2026-10-24T10:00:00+00:00", title="공식 상품 변경 발표"):
    return {"title": title, "summary": "공식 자료에서 새 상품의 조건을 발표했습니다.",
            "source_hash": item["content_hash"], "generated_at": stamp, "model": "test-model", "language": "ko"}


class Remote:
    def __init__(self, value):
        self.snapshot = deepcopy(value)
        self.sha = "sha-1"
        self.reads, self.writes = [], []
        self.on_write = None

    def read(self, config, path):
        self.reads.append((path, self.sha))
        return {"snapshot": deepcopy(self.snapshot), "sha": self.sha}

    def write(self, updated, *, config, path, sha):
        self.writes.append({"snapshot": deepcopy(updated), "path": path, "sha": sha})
        if self.on_write:
            result = self.on_write(self, updated)
            if result:
                return result
        if sha != self.sha:
            return {"status": "conflict"}
        self.snapshot = deepcopy(updated)
        self.sha += "-next"
        return {"status": "success", "commit_url": "https://github.com/owner/repo/commit/example"}


class MediaInsightsStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "media_insights_snapshot.json"
        self.first, self.second = document("one"), document("two")
        self.initial = snapshot(self.first, self.second)
        self.path.write_bytes(payload(self.initial))
        self.csv = self.root / "theme_pool.csv"
        self.csv.write_bytes(b"theme_id|theme_name\nT001|preserve me\n")

    def save(self, note, remote, **kwargs):
        return persist_media_analysis("one", note, snapshot_path=self.path, github_config=CONFIG,
                                      remote_reader=remote.read, remote_writer=remote.write, **kwargs)

    def test_latest_remote_collector_and_other_editors_analysis_survive(self):
        third = document("three", collected_at="2026-10-23T09:00:00+00:00")
        other_note = analysis(self.second)
        latest = snapshot(self.first, {**self.second, "analysis": other_note}, third)
        latest["collector_note"] = "preserve future metadata"
        remote = Remote(latest)
        before_csv = self.csv.read_bytes()
        note = analysis(self.first)
        result = self.save(note, remote)
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["refresh_required"])
        by_id = {row["id"]: row for row in remote.snapshot["items"]}
        self.assertEqual(set(by_id), {"one", "two", "three"})
        self.assertEqual(by_id["one"]["analysis"], note)
        self.assertEqual(by_id["two"]["analysis"], other_note)
        self.assertEqual(remote.snapshot["collector_note"], "preserve future metadata")
        self.assertEqual(self.csv.read_bytes(), before_csv)
        self.assertNotIn("PRIVATE-TOKEN", repr(result))
        self.assertEqual(remote.writes[0]["path"], "media_insights_snapshot.json")

    def test_localizations_save_in_their_own_snapshot_and_preserve_existing_notes_and_csv(self):
        note = analysis(self.first)
        other_locale = localization(self.second)
        latest = snapshot({**self.first, "analysis": note}, {**self.second, "localization": other_locale}, document("three"))
        remote = Remote(latest)
        before_csv = self.csv.read_bytes()
        rendering = {**localization(self.first), "api_key": "PRIVATE-DO-NOT-STORE"}
        result = persist_media_localizations({"one": rendering}, snapshot_path=self.path, github_config=CONFIG,
                                            remote_reader=remote.read, remote_writer=remote.write)
        self.assertEqual(result["status"], "success")
        rows = {row["id"]: row for row in result["snapshot"]["items"]}
        self.assertEqual(rows["one"]["localization"], localization(self.first))
        self.assertEqual(rows["one"]["analysis"], note)
        self.assertEqual(rows["two"]["localization"], other_locale)
        self.assertIn("three", rows)
        self.assertEqual(self.csv.read_bytes(), before_csv)
        self.assertNotIn("PRIVATE-DO-NOT-STORE", repr(result))
        self.assertEqual(remote.writes[0]["path"], "media_insights_snapshot.json")

    def test_localization_conflict_rereads_other_translations_without_regeneration(self):
        remote = Remote(self.initial)
        def concurrent_update(store, updated):
            if len(store.writes) == 1:
                store.snapshot["items"][1]["localization"] = localization(self.second)
                store.snapshot["items"].append(document("three"))
                store.sha = "translation-concurrent"
                return {"status": "conflict"}
        remote.on_write = concurrent_update
        result = persist_media_localizations({"one": localization(self.first)}, snapshot_path=self.path,
            github_config=CONFIG, remote_reader=remote.read, remote_writer=remote.write)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(remote.writes), 2)
        self.assertEqual(remote.writes[1]["sha"], "translation-concurrent")
        rows = {row["id"]: row for row in result["snapshot"]["items"]}
        self.assertEqual(rows["two"]["localization"], localization(self.second))
        self.assertIn("three", rows)

    def test_batch_localizations_refuse_missing_changed_or_invalid_korean_before_writes(self):
        remote = Remote(self.initial)
        before = self.path.read_bytes()
        invalid = []
        for change in ({"title": "Official announcement"}, {"summary": "An official announcement"},
                       {"summary": " "}, {"generated_at": "not-a-timestamp"}, {"model": ""},
                       {"language": "en"}, {"source_hash": "old-source"}, {"source_hash": 123}):
            invalid.append({"one": {**localization(self.first), **change}})
        invalid.append({"missing": localization(self.first)})
        invalid.append({"one": localization(self.first), "two": {**localization(self.second), "source_hash": "old"}})
        for batch in invalid:
            with self.subTest(batch=batch), self.assertRaises(MediaInsightsStorageError):
                persist_media_localizations(batch, snapshot_path=self.path, github_config=CONFIG,
                    remote_reader=remote.read, remote_writer=remote.write)
        self.assertEqual(remote.writes, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any("localization" in row for row in remote.snapshot["items"]))

    def test_translation_source_change_during_cas_retry_refuses_stale_batch(self):
        remote = Remote(self.initial)
        def change_source(store, updated):
            store.snapshot["items"][0]["content_hash"] = "changed-during-translation"
            store.sha = "new-source-sha"
            return {"status": "conflict"}
        remote.on_write = change_source
        before = self.path.read_bytes()
        with self.assertRaises(MediaInsightsStorageError):
            persist_media_localizations({"one": localization(self.first)}, snapshot_path=self.path,
                github_config=CONFIG, remote_reader=remote.read, remote_writer=remote.write)
        self.assertEqual(len(remote.writes), 1)
        self.assertNotIn("localization", remote.snapshot["items"][0])
        self.assertEqual(self.path.read_bytes(), before)

    def test_conflict_keeps_newer_same_document_translation_and_independent_analysis(self):
        remote = Remote(self.initial)
        recent = localization(self.first, stamp="2026-10-24T11:00:00Z", title="더 최근에 저장한 한국어 제목")
        other_note = analysis(self.second)
        def concurrent_update(store, updated):
            if len(store.writes) == 1:
                store.snapshot["items"][0]["localization"] = recent
                store.snapshot["items"][1]["analysis"] = other_note
                store.snapshot["sources"][0]["status"] = "partial"
                store.sha = "newer-translation"
                return {"status": "conflict"}
        remote.on_write = concurrent_update
        result = persist_media_localizations({"one": localization(self.first)}, snapshot_path=self.path,
            github_config=CONFIG, remote_reader=remote.read, remote_writer=remote.write)
        rows = {row["id"]: row for row in result["snapshot"]["items"]}
        self.assertEqual(rows["one"]["localization"], recent)
        self.assertEqual(rows["two"]["analysis"], other_note)
        self.assertEqual(result["snapshot"]["sources"][0]["status"], "partial")
        self.assertEqual(len(remote.writes), 1)

    def test_token_korean_does_not_make_an_english_rendering_valid(self):
        remote = Remote(self.initial)
        for field in ("title", "summary"):
            rendering = {**localization(self.first), field: "Official announcement with English source details 가나"}
            with self.subTest(field=field), self.assertRaises(MediaInsightsStorageError):
                persist_media_localizations({"one": rendering}, snapshot_path=self.path, github_config=CONFIG,
                    remote_reader=remote.read, remote_writer=remote.write)
        self.assertEqual(remote.writes, [])

    def test_collection_merge_retains_newest_valid_korean_for_same_source(self):
        old = localization(self.first, stamp="2026-10-24T08:00:00Z", title="이전 한국어 제목")
        recent = localization(self.first, title="새 한국어 제목")
        current = snapshot({**self.first, "localization": recent})
        incoming = snapshot({**self.first, "last_checked_at": "2026-10-25T08:00:00Z", "localization": old})
        merged = merge_media_snapshots(current, incoming)
        self.assertEqual(merged["items"][0]["localization"], recent)
        del incoming["items"][0]["localization"]
        self.assertEqual(merge_media_snapshots(current, incoming)["items"][0]["localization"], recent)
        incoming["items"][0]["localization"] = {**recent, "summary": "English-only invalid translation"}
        self.assertEqual(merge_media_snapshots(current, incoming)["items"][0]["localization"], recent)

    def test_changed_source_archives_prior_korean_and_drops_stale_current_translation(self):
        previous_locale = localization(self.first)
        current = snapshot({**self.first, "localization": previous_locale})
        revised = document("one", content_hash="one-v2", collected_at="2026-10-25T08:00:00Z")
        revised["localization"] = previous_locale
        revised["versions"] = [{"content_hash": self.first["content_hash"], "title": self.first["title"], "future_field": "keep"}]
        result = merge_media_snapshots(current, snapshot(revised))
        item = result["items"][0]
        self.assertEqual(item["content_hash"], "one-v2")
        self.assertNotIn("localization", item)
        self.assertEqual(item["versions"][0]["localization"], previous_locale)
        self.assertEqual(item["versions"][0]["future_field"], "keep")
        self.assertEqual(current["items"][0]["localization"], previous_locale)

    def test_saved_newer_translation_and_identical_localization_are_not_overwritten(self):
        recent = localization(self.first)
        latest = snapshot({**self.first, "localization": recent}, self.second)
        remote = Remote(latest)
        for rendering in (recent, localization(self.first, stamp="2026-10-23T08:00:00Z", title="오래된 한국어 제목")):
            result = persist_media_localizations({"one": rendering}, snapshot_path=self.path, github_config=CONFIG,
                remote_reader=remote.read, remote_writer=remote.write)
            self.assertEqual(result["snapshot"]["items"][0]["localization"], recent)
        self.assertEqual(remote.writes, [])

    def test_local_only_translation_does_not_touch_remote_or_protected_json(self):
        with patch("requests.request", side_effect=AssertionError("No network")):
            result = persist_media_localizations({"one": localization(self.first)}, snapshot_path=self.path, github_config={})
        self.assertEqual(result["status"], "local_only")
        self.assertEqual(json.loads(self.path.read_bytes())["items"][0]["localization"], localization(self.first))
        for filename in ("theme_pool.csv", "ott_home_snapshot.json", "collection_status.json"):
            target = self.root / filename
            if not target.exists():
                target.write_text("protected")
            before = target.read_bytes()
            with self.subTest(path=filename), self.assertRaises(MediaInsightsStorageError):
                persist_media_localizations({"one": localization(self.first)}, snapshot_path=target, github_config={})
            self.assertEqual(target.read_bytes(), before)

    def test_scheduled_merge_shows_newest_publication_first_and_unknown_last(self):
        older = {**self.first, "published_at": "2026-10-20T10:00:00Z"}
        newer = {**self.second, "published_at": "2026-10-23T10:00:00Z"}
        same_day_older_version = {**document("three"), "published_at": "2026-10-23T10:00:00+00:00"}
        newer["collected_at"] = "2026-10-24T09:00:00+00:00"
        unknown = document("unknown", collected_at="2026-11-01T12:00:00+00:00")
        merged = merge_media_snapshots(snapshot(older, same_day_older_version), snapshot(newer, unknown))
        self.assertEqual([row["id"] for row in merged["items"]], ["two", "three", "one", "unknown"])
        self.assertNotIn("published_at", merged["items"][-1])

    def test_newer_same_hash_observation_clears_unconfirmed_date_preserving_analysis(self):
        note = analysis(self.first)
        prior = {**self.first, "published_at": "2026-06-27T00:00:00+00:00",
                 "published_at_source": "pdf_dateline", "last_checked_at": "2026-10-23T08:00:00+00:00",
                 "analysis": note}
        corrected = {**self.first, "published_at": "", "published_at_source": "pdf_publication_unconfirmed",
                     "last_checked_at": "2026-10-24T08:00:00+00:00"}
        merged = merge_media_snapshots(snapshot(prior), snapshot(corrected))
        item = merged["items"][0]
        self.assertEqual(item["published_at"], "")
        self.assertEqual(item["published_at_source"], "pdf_publication_unconfirmed")
        self.assertEqual(item["collected_at"], self.first["collected_at"])
        self.assertEqual(item["last_checked_at"], corrected["last_checked_at"])
        self.assertEqual(item["analysis"], note)

    def test_sha_conflict_rereads_new_docs_and_analysis_before_one_retry(self):
        remote = Remote(self.initial)
        def concurrent_update(store, updated):
            if len(store.writes) == 1:
                store.snapshot["items"].append(document("three"))
                store.snapshot["items"][1]["analysis"] = analysis(self.second)
                store.sha = "sha-concurrent"
                return {"status": "conflict"}
        remote.on_write = concurrent_update
        result = self.save(analysis(self.first), remote)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(remote.writes), 2)
        self.assertEqual(remote.writes[1]["sha"], "sha-concurrent")
        self.assertEqual(len(remote.snapshot["items"]), 3)
        self.assertIn("analysis", remote.snapshot["items"][1])

    def test_stale_source_and_unknown_citations_are_refused_without_writes(self):
        remote = Remote(self.initial)
        before = self.path.read_bytes()
        variants = [dict(analysis(self.first), source_hash="old-hash"),
                    dict(analysis(self.first), facts=[{"text": "Unsupported", "evidence_ids": ["unknown"]}])]
        for note in variants:
            with self.subTest(note=note), self.assertRaises(MediaInsightsStorageError):
                self.save(note, remote)
        self.assertEqual(remote.writes, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_comparison_context_requires_matching_hash_and_current_citation(self):
        remote = Remote(self.initial)
        note = analysis(self.first, context=self.second)
        missing_hash = deepcopy(note)
        missing_hash["context_hashes"].pop("two")
        with self.assertRaises(MediaInsightsStorageError):
            self.save(missing_hash, remote)
        changed = deepcopy(self.initial)
        changed["items"][1]["content_hash"] = "two-new"
        remote.snapshot = changed
        with self.assertRaises(MediaInsightsStorageError):
            self.save(note, remote)
        self.assertEqual(remote.writes, [])

    def test_local_change_during_remote_save_is_preserved_and_reported(self):
        remote = Remote(self.initial)
        concurrent = snapshot(self.first, self.second, document("local-new"))
        def change_local(store, updated):
            self.path.write_bytes(payload(concurrent))
        remote.on_write = change_local
        result = self.save(analysis(self.first), remote)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["refresh_required"])
        self.assertEqual(json.loads(self.path.read_bytes()), concurrent)
        self.assertIn("analysis", remote.snapshot["items"][0])
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_identical_saved_analysis_does_not_commit_again(self):
        note = analysis(self.first)
        latest = snapshot({**self.first, "analysis": note}, self.second)
        self.path.write_bytes(payload(latest))
        remote = Remote(latest)
        with patch("media_insights_storage.os.replace") as replace:
            result = self.save(note, remote)
        self.assertEqual(result["status"], "success")
        self.assertEqual(remote.writes, [])
        replace.assert_not_called()

    def test_refresh_keeps_newer_remote_content_and_valid_independent_analysis(self):
        updated_first = document("one", content_hash="one-new", collected_at="2026-10-23T10:00:00+00:00")
        latest = snapshot(updated_first, {**self.second, "analysis": analysis(self.second)})
        incoming = snapshot(self.first, self.second, document("three"))
        remote = Remote(latest)
        result = persist_media_snapshot(incoming, snapshot_path=self.path, github_config=CONFIG,
                                       remote_reader=remote.read, remote_writer=remote.write)
        by_id = {row["id"]: row for row in result["snapshot"]["items"]}
        self.assertEqual(by_id["one"]["content_hash"], "one-new")
        self.assertIn("analysis", by_id["two"])
        self.assertIn("three", by_id)

    def test_refresh_drops_analysis_when_its_comparison_source_changed(self):
        note = analysis(self.first, context=self.second)
        current = snapshot({**self.first, "analysis": note}, self.second)
        incoming = snapshot(document("two", content_hash="new", collected_at="2026-10-23T10:00:00+00:00"))
        merged = merge_media_snapshots(current, incoming)
        self.assertNotIn("analysis", merged["items"][0])
        self.assertIn("analysis", current["items"][0])

    def test_failed_source_preserves_last_success_and_documents(self):
        failed = snapshot(collected_at="2026-10-23T10:00:00+00:00")
        failed["sources"][0].update(status="unavailable", last_success_at="")
        merged = merge_media_snapshots(self.initial, failed)
        self.assertEqual(merged["sources"][0]["status"], "unavailable")
        self.assertEqual(merged["sources"][0]["last_success_at"], "2026-10-23T08:00:00+00:00")
        self.assertEqual(merged["items"], self.initial["items"])

    def test_local_only_storage_never_claims_github_durability(self):
        note = analysis(self.first)
        with patch("requests.request", side_effect=AssertionError("No network")):
            result = persist_media_analysis("one", note, snapshot_path=self.path, github_config={})
        self.assertEqual(result["status"], "local_only")
        self.assertEqual(json.loads(self.path.read_bytes())["items"][0]["analysis"], note)

    def test_invalid_paths_and_corrupt_local_data_cannot_overwrite_existing_files(self):
        for remote_path in ["theme_pool.csv", "ott_home_snapshot.json", "../media.json", "/tmp/media.json"]:
            remote = Remote(self.initial)
            with self.subTest(path=remote_path), self.assertRaises(MediaInsightsStorageError):
                persist_media_analysis("one", analysis(self.first), snapshot_path=self.path,
                    github_config={**CONFIG, "media_insights_path": remote_path},
                    remote_reader=remote.read, remote_writer=remote.write)
            self.assertEqual(remote.writes, [])
        self.path.write_bytes(b'{"broken":')
        with patch("requests.request") as request, self.assertRaises(MediaInsightsStorageError):
            persist_media_analysis("one", analysis(self.first), snapshot_path=self.path, github_config=CONFIG)
        request.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b'{"broken":')

    def test_repeated_conflict_has_bounded_retry_and_leaves_local_file_intact(self):
        remote = Remote(self.initial)
        remote.on_write = lambda *args: {"status": "conflict"}
        before = self.path.read_bytes()
        with self.assertRaises(MediaInsightsStorageError):
            self.save(analysis(self.first), remote)
        self.assertEqual(len(remote.reads), 2)
        self.assertEqual(len(remote.writes), 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_real_transport_is_sha_conditional_and_errors_are_sanitized(self):
        get = Mock(status_code=200)
        get.json.return_value = {"sha": "current-sha", "content": base64.b64encode(payload(self.initial)).decode()}
        put = Mock(status_code=201)
        put.json.return_value = {"commit": {"html_url": "https://github.com/owner/repo/commit/example"}}
        with patch("media_insights_storage.requests.request", side_effect=[get, put]) as request:
            result = persist_media_analysis("one", analysis(self.first), snapshot_path=self.path, github_config=CONFIG)
        self.assertEqual(result["status"], "success")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["json"]["sha"], "current-sha")
        self.assertEqual(kwargs["json"]["branch"], "main")
        self.assertTrue(request.call_args.args[1].endswith("/contents/media_insights_snapshot.json"))
        denied = Mock(status_code=500, text="PRIVATE-TOKEN SECRET BODY")
        before = self.path.read_bytes()
        with patch("media_insights_storage.requests.request", return_value=denied), \
                self.assertRaises(MediaInsightsStorageError) as error:
            persist_media_analysis("one", analysis(self.first), snapshot_path=self.path, github_config=CONFIG)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(self.path.read_bytes(), before)

    def test_large_contents_metadata_reads_raw_same_endpoint_and_preserves_sha(self):
        large = deepcopy(self.initial)
        large["public_excerpt_archive"] = "공식 원문 " * 100_000
        encoded = payload(large)
        self.assertGreater(len(encoded), 1024 * 1024)
        metadata = Mock(status_code=200)
        metadata.json.return_value = {"sha": "large-current-sha", "encoding": "none", "content": "",
                                      "size": len(encoded), "download_url": "https://unsafe.example/ignored"}
        raw = Mock(status_code=200, headers={"Content-Length": str(len(encoded))})
        raw.iter_content.return_value = [encoded[offset:offset + 65_536]
                                         for offset in range(0, len(encoded), 65_536)]
        put = Mock(status_code=200)
        put.json.return_value = {"commit": {"html_url": "https://github.com/owner/repo/commit/example"}}
        with patch("media_insights_storage.requests.request", side_effect=[metadata, raw, put]) as request:
            result = persist_media_analysis("one", analysis(self.first), snapshot_path=self.path, github_config=CONFIG)
        calls = request.call_args_list
        self.assertEqual(result["status"], "success")
        self.assertEqual(calls[0].args[1], calls[1].args[1])
        self.assertEqual(calls[1].kwargs["headers"]["Accept"], "application/vnd.github.raw+json")
        self.assertEqual(calls[1].kwargs["params"], {"ref": "main"})
        self.assertTrue(calls[1].kwargs["stream"])
        self.assertEqual(calls[2].kwargs["json"]["sha"], "large-current-sha")
        self.assertEqual(result["snapshot"]["public_excerpt_archive"], large["public_excerpt_archive"])
        raw.close.assert_called_once()

    def test_failed_or_oversized_raw_read_never_writes_or_overwrites_local(self):
        before = self.path.read_bytes()
        metadata = Mock(status_code=200)
        metadata.json.return_value = {"sha": "large-sha", "encoding": "none", "content": ""}
        failed = Mock(status_code=500, headers={}, text="PRIVATE-TOKEN SECRET BODY")
        oversized = Mock(status_code=200, headers={"Content-Length": str(25 * 1024 * 1024)})
        unbounded = Mock(status_code=200, headers={})
        unbounded.iter_content.return_value = [b"x" * 1024 * 1024] * 25
        for raw in (failed, oversized, unbounded):
            with self.subTest(status=raw.status_code), \
                    patch("media_insights_storage.requests.request", side_effect=[metadata, raw]) as request, \
                    self.assertRaises(MediaInsightsStorageError) as error:
                persist_media_analysis("one", analysis(self.first), snapshot_path=self.path, github_config=CONFIG)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertNotIn("SECRET", str(error.exception))
            raw.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
