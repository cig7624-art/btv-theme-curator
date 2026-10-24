"""Concurrent startup reads retain ordered writes and the shared mutation lock."""
import base64
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from data_storage_lock import with_data_write_lock
from llm_theme_generator import ThemeGenerationError, sync_files_from_github


class StorageSyncPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.paths = [self.directory / name for name in ("themes.csv", "history.csv", "contents.csv")]
        self.config = {
            "token": "test-token", "repo": "test/repo", "branch": "test-branch",
            "theme_path": "remote-themes.csv", "history_path": "remote-history.csv",
            "content_path": "remote-contents.csv",
        }
        self.remote_paths = [self.config[key] for key in ("theme_path", "history_path", "content_path")]
        self.originals = [
            b'theme_id|theme_name|director_adoption_key|operator_note\nT001|adopted|protected|keep\n',
            b'run_id|theme_id|rank|director_draft_id\nAI-DIRECTOR-original|T001|1|draft-original\n',
            b'run_id|theme_key|title|operator_note\nTHEME_DB|T001|original|protected\n',
        ]
        for path, original in zip(self.paths, self.originals):
            path.write_bytes(original)
        # Remote payloads retain multiple rows and arbitrary provenance columns.
        self.payloads = {
            name: original + original.splitlines(keepends=True)[1].replace(b"original", b"second")
            for name, original in zip(self.remote_paths, self.originals)
        }

    def sync(self, config=None, content_path=True):
        return sync_files_from_github(
            self.paths[0], self.paths[1], self.config if config is None else config,
            content_path=self.paths[2] if content_path else None,
        )

    def response(self, remote_path, status=200):
        encoded = base64.b64encode(self.payloads[remote_path]).decode("ascii")
        return SimpleNamespace(status_code=status, text="controlled failure",
                               json=lambda: {"content": encoded})

    def test_three_gets_overlap_and_hold_mutation_lock_through_ordered_apply(self):
        overlap = threading.Barrier(3, timeout=3)
        all_reading, release_reads = threading.Event(), threading.Event()
        applying, release_apply = threading.Event(), threading.Event()
        writer_entered = threading.Event()
        thread_ids, written_paths = set(), []
        calls_lock = threading.Lock()
        original_write = Path.write_bytes

        def get(url, **kwargs):
            self.assertEqual(kwargs["timeout"], 20)
            self.assertEqual(kwargs["params"], {"ref": "test-branch"})
            remote = url.rsplit("/", 1)[-1]
            with calls_lock:
                thread_ids.add(threading.get_ident())
            overlap.wait()
            all_reading.set()
            self.assertTrue(release_reads.wait(5))
            return self.response(remote)

        def write(path, content):
            if path in self.paths:
                if not written_paths:
                    applying.set()
                    self.assertTrue(release_apply.wait(5))
                written_paths.append(path)
            return original_write(path, content)

        @with_data_write_lock
        def competing_writer():
            writer_entered.set()

        with patch("llm_theme_generator.requests.get", side_effect=get), patch.object(Path, "write_bytes", new=write):
            with ThreadPoolExecutor(max_workers=2) as callers:
                sync_future = callers.submit(self.sync)
                try:
                    self.assertTrue(all_reading.wait(4), "The three independent GETs must overlap")
                    writer_future = callers.submit(competing_writer)
                    self.assertFalse(writer_entered.wait(.05), "Sync must hold the mutation lock during reads")
                    release_reads.set()
                    self.assertTrue(applying.wait(4))
                    self.assertFalse(writer_entered.wait(.05), "Sync must keep the lock while applying all files")
                    release_apply.set()
                    result = sync_future.result(timeout=5)
                    writer_future.result(timeout=5)
                finally:
                    release_reads.set()
                    release_apply.set()
        self.assertEqual(len(thread_ids), 3)
        self.assertTrue(writer_entered.is_set())
        self.assertEqual(written_paths, self.paths)
        self.assertEqual(result, {"status": "success", "synced": [str(path) for path in self.paths]})
        self.assertEqual([path.read_bytes() for path in self.paths], [self.payloads[name] for name in self.remote_paths])

    def test_one_failed_get_keeps_every_local_file_and_protected_provenance(self):
        overlap = threading.Barrier(3, timeout=3)

        def get(url, **kwargs):
            remote = url.rsplit("/", 1)[-1]
            overlap.wait()
            return self.response(remote, status=503 if remote == self.remote_paths[1] else 200)

        with patch("llm_theme_generator.requests.get", side_effect=get) as requests:
            with self.assertRaisesRegex(ThemeGenerationError, r"GitHub 파일 조회 실패\(503\)"):
                self.sync()
        self.assertEqual(requests.call_count, 3)
        self.assertEqual([path.read_bytes() for path in self.paths], self.originals)

    def test_missing_remote_file_keeps_local_copy_and_applies_other_payloads_in_order(self):
        def get(url, **kwargs):
            remote = url.rsplit("/", 1)[-1]
            return self.response(remote, status=404 if remote == self.remote_paths[1] else 200)

        with patch("llm_theme_generator.requests.get", side_effect=get):
            result = self.sync()
        self.assertEqual(result["synced"], [str(self.paths[0]), str(self.paths[2])])
        self.assertEqual(self.paths[0].read_bytes(), self.payloads[self.remote_paths[0]])
        self.assertEqual(self.paths[1].read_bytes(), self.originals[1])
        self.assertEqual(self.paths[2].read_bytes(), self.payloads[self.remote_paths[2]])

    def test_multiple_failures_report_the_original_theme_first_error_priority(self):
        content_finished = threading.Event()

        def get(url, **kwargs):
            remote = url.rsplit("/", 1)[-1]
            if remote == self.remote_paths[2]:
                content_finished.set()
                return self.response(remote, status=503)
            self.assertTrue(content_finished.wait(3))
            return self.response(remote, status=501 if remote == self.remote_paths[0] else 502)

        with patch("llm_theme_generator.requests.get", side_effect=get):
            with self.assertRaisesRegex(ThemeGenerationError, r"GitHub 파일 조회 실패\(501\)"):
                self.sync()
        self.assertEqual([path.read_bytes() for path in self.paths], self.originals)

    def test_optional_paths_and_unconfigured_storage_do_not_fetch_unused_files(self):
        config = dict(self.config, history_path="", content_path="")
        with patch("llm_theme_generator.requests.get", return_value=self.response(self.remote_paths[0])) as requests:
            result = self.sync(config=config)
            self.assertEqual(requests.call_count, 1)
            self.assertEqual(result["synced"], [str(self.paths[0])])
            self.assertEqual(self.sync(config={}), {"status": "not_configured"})
            self.assertEqual(requests.call_count, 1)
        self.assertEqual([path.read_bytes() for path in self.paths[1:]], self.originals[1:])


if __name__ == "__main__":
    unittest.main()
