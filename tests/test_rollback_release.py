"""Release restores stage code changes while keeping newer runtime data intact."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from scripts import rollback_release as rollback


class RollbackReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Rollback Test")
        self.git("config", "user.email", "rollback@example.invalid")
        self.put("app.py", "# baseline application\n")
        self.put("styles.css", "baseline styles\n")
        self.put("legacy.py", "# baseline-only module\n")
        self.put(".github/workflows/update_issues.yml", "name: keep original issue collection\n")
        for path in ("theme_db.csv", "theme_pool.csv", "theme_content_candidates.csv",
                     "theme_recommendation_history.csv", "issue_feed.csv", "content_db.csv",
                     "youtube_video_stats.csv", "youtube_video_watchlist.csv",
                     "collection_status.json", "data/history.sqlite", "data/history.parquet"):
            self.put(path, "baseline data\n")
        self.commit_all("baseline")
        self.git("tag", "-a", "v1.0.0", "-m", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip()
        self.put("app.py", "# new OTT application\n")
        self.put("styles.css", "new styles\n")
        (self.repo / "legacy.py").unlink()
        self.new_code = ["ott_view.py", "ott_styles.css", "scripts/collect_ott.py",
                         "tests/test_ott_view.py", "tests/fixtures/ott/home.html", "packages.txt",
                         ".github/workflows/update_ott.yml", ".github/workflows/verify_ott_public.yml"]
        for path in self.new_code:
            self.put(path, "new OTT release file\n")
        for path in rollback.CONTROL_FILES | {"ott_home_snapshot.json", "artifacts/observed.json",
                                              "design/review.html", ".env.production", ".streamlit/secrets.toml"}:
            self.put(path, "new operational or runtime value\n")
        for path in self.repo.rglob("*.csv"):
            path.write_bytes(b"newer user records\r\nrow|preserve\r\n")
        self.commit_all("new release")
        self.git("tag", "-a", "v1.1.0-rc.1", "-m", "candidate")
        self.candidate = self.git("rev-parse", "HEAD").strip()

    def git(self, *arguments):
        return subprocess.check_output(["git", *arguments], cwd=self.repo, text=True, stderr=subprocess.STDOUT)

    def put(self, path, text):
        destination = self.repo / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def commit_all(self, message):
        self.git("add", ".")
        self.git("commit", "-qm", message)

    def snapshot(self):
        return {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.repo).parts}

    def test_preview_does_not_change_files_index_head_or_tags(self):
        before = self.snapshot()
        index = self.git("write-tree")
        plan = rollback.plan_rollback(self.repo, "v1.0.0")
        self.assertIn("app.py", plan["restore"])
        self.assertIn("legacy.py", plan["restore"])
        self.assertEqual(set(plan["remove"]), set(self.new_code))
        self.assertEqual(plan["target_commit"], self.baseline)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.candidate)

    def test_apply_preserves_all_runtime_bytes_and_removes_new_code_and_scheduler(self):
        self.put("theme_content_candidates.csv", "unsaved latest user records\n")
        before = self.snapshot()
        state = rollback.apply_rollback(self.repo, rollback.plan_rollback(self.repo, "v1.0.0"))
        for path, contents in before.items():
            if rollback.protected(path) and path not in {"VERSION", "release_state.json"}:
                self.assertEqual((self.repo / path).read_bytes(), contents, path)
        self.assertEqual((self.repo / "app.py").read_text(), "# baseline application\n")
        self.assertTrue((self.repo / ".github/workflows/update_issues.yml").exists())
        for path in self.new_code:
            self.assertFalse((self.repo / path).exists(), path)
        self.assertEqual((self.repo / "VERSION").read_text(), "1.0.0\n")
        self.assertEqual(json.loads((self.repo / "release_state.json").read_text()), state)
        self.assertEqual(state["previous_head"], self.candidate)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.candidate)
        self.assertEqual(self.git("rev-parse", "refs/tags/v1.0.0^{commit}").strip(), self.baseline)
        staged = self.git("diff", "--cached", "--name-only").splitlines()
        self.assertTrue(all(rollback.code_path(path) or path in {"VERSION", "release_state.json"} for path in staged))

    def test_staged_data_or_dirty_code_blocks_apply_without_partial_changes(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                path = "theme_db.csv" if staged else "app.py"
                self.put(path, "user edit\n")
                if staged:
                    self.git("add", "--", path)
                before = self.snapshot()
                index = self.git("write-tree")
                with self.assertRaises(rollback.RollbackError):
                    rollback.apply_rollback(self.repo, rollback.plan_rollback(self.repo, "v1.0.0"))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.git("write-tree"), index)
                self.git("restore", "--source=HEAD", "--staged", "--worktree", "--", path)

    def test_untracked_target_collision_is_rejected_before_changing_other_files(self):
        self.put("legacy.py", "untracked user code\n")
        before = self.snapshot()
        with self.assertRaisesRegex(rollback.RollbackError, "Untracked"):
            rollback.apply_rollback(self.repo, rollback.plan_rollback(self.repo, "v1.0.0"))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_only_valid_existing_release_tags_are_accepted(self):
        for version in ("main", "HEAD", "refs/tags/v1.0.0", "v01.0.0", "v1.0.0-01", "v1.0.1٤", "v9.9.9"):
            with self.subTest(version=version), self.assertRaises(rollback.RollbackError):
                rollback.plan_rollback(self.repo, version)

    def test_moved_tag_invalidates_a_prepared_plan(self):
        plan = rollback.plan_rollback(self.repo, "v1.0.0")
        self.git("tag", "-f", "v1.0.0", self.candidate)
        before = self.snapshot()
        with self.assertRaisesRegex(rollback.RollbackError, "changed"):
            rollback.apply_rollback(self.repo, plan)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_new_commit_invalidates_a_previous_head_preview(self):
        plan = rollback.plan_rollback(self.repo, "v1.0.0")
        self.put("README.md", "concurrent code commit\n")
        self.commit_all("new head after preview")
        before = self.snapshot()
        with self.assertRaisesRegex(rollback.RollbackError, "changed"):
            rollback.apply_rollback(self.repo, plan)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_roundtrip_restores_release_code_but_keeps_the_newest_data(self):
        rollback.apply_rollback(self.repo, rollback.plan_rollback(self.repo, "v1.0.0"))
        self.git("commit", "-qm", "reviewed rollback code")
        self.put("theme_content_candidates.csv", "new records created after rollback\n")
        self.put("ott_home_snapshot.json", "new observation after rollback\n")
        before = self.snapshot()
        rollback.apply_rollback(self.repo, rollback.plan_rollback(self.repo, "v1.1.0-rc.1"))
        self.assertEqual((self.repo / "app.py").read_text(), "# new OTT application\n")
        for path in self.new_code:
            self.assertTrue((self.repo / path).exists(), path)
        self.assertFalse((self.repo / "legacy.py").exists())
        for path, contents in before.items():
            if rollback.protected(path) and path not in {"VERSION", "release_state.json"}:
                self.assertEqual((self.repo / path).read_bytes(), contents, path)
        self.assertEqual((self.repo / "VERSION").read_text(), "1.1.0-rc.1\n")


if __name__ == "__main__":
    unittest.main()
