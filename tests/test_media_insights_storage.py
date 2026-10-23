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
    persist_media_analysis, persist_media_snapshot,
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


if __name__ == "__main__":
    unittest.main()
