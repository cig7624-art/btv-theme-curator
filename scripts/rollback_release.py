"""Preview or stage a code-only release restore without replacing live data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONTROL_FILES = {
    "VERSION", "CHANGELOG.md", "ROLLBACK.md", "release_state.json",
    "scripts/rollback_release.py", "tests/test_rollback_release.py",
}
DATA_FILES = {"collection_status.json", "ott_home_snapshot.json"}
DATA_SUFFIXES = {".csv", ".sqlite", ".sqlite3", ".parquet", ".db"}
CODE_DIRECTORIES = ("scripts/", "tests/", ".github/workflows/", ".streamlit/", ".devcontainer/")
CODE_FILES = {"requirements.txt", "packages.txt", ".gitignore", ".dockerignore",
              "Dockerfile", ".python-version", "package.json", "package-lock.json"}
CODE_SUFFIXES = {".py", ".css", ".js", ".html", ".toml", ".yml", ".yaml", ".md"}


class RollbackError(RuntimeError):
    pass


def _git(repo, *arguments):
    result = subprocess.run(["git", *arguments], cwd=repo, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RollbackError(result.stderr.strip() or "Git command failed")
    return result.stdout


def _repository(repo):
    return Path(_git(Path(repo), "rev-parse", "--show-toplevel").strip()).resolve()


def _target(repo, version):
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
                         r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
                         r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version)
    if not match or any(part.isdigit() and len(part) > 1 and part.startswith("0")
                        for part in (match.group(4) or "").split(".")):
        raise RollbackError("Use a release tag such as v1.0.0 or v1.1.0-rc.1")
    return _git(repo, "rev-parse", "--verify", f"refs/tags/{version}^{{commit}}").strip()


def protected(path):
    name = Path(path).name.lower()
    parts = {part.lower() for part in Path(path).parts}
    return (path in CONTROL_FILES or path in DATA_FILES
            or path.startswith(("artifacts/", "design/"))
            or Path(path).suffix.lower() in DATA_SUFFIXES
            or name.startswith(".env") or name.startswith("secrets.")
            or bool(parts & {"secrets", "env", ".env", ".venv"}))


def code_path(path):
    return not protected(path) and (
        path.startswith(CODE_DIRECTORIES)
        or "/" not in path and (path in CODE_FILES or Path(path).suffix in CODE_SUFFIXES))


def _tree(repo, revision):
    entries = {}
    for record in _git(repo, "ls-tree", "-rz", revision).split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            entries[name] = metadata
    return entries


def plan_rollback(repo, version):
    """Resolve an existing version tag once and describe code changes only."""
    repo = _repository(repo)
    target = _target(repo, version)
    previous = _git(repo, "rev-parse", "HEAD").strip()
    current, desired = _tree(repo, previous), _tree(repo, target)
    return {"target_tag": version, "target_commit": target, "previous_head": previous,
            "restore": sorted(path for path in desired if code_path(path)
                              and current.get(path) != desired[path]),
            "remove": sorted(path for path in current if code_path(path) and path not in desired)}


def _preflight(repo, plan):
    # Resolve the tag again, then rebuild the plan to reject stale or modified
    # plans before touching any files or the index.
    if plan_rollback(repo, plan["target_tag"]) != plan:
        raise RollbackError("HEAD or release tag changed; create a fresh preview")
    if _git(repo, "diff", "--cached", "--name-only", "-z"):
        raise RollbackError("Staged changes exist; use a clean rollback worktree")
    dirty = _git(repo, "diff", "--name-only", "-z").split("\0")
    if any(code_path(path) or path in {"VERSION", "release_state.json"} for path in dirty if path):
        raise RollbackError("Local code or release metadata is modified; use a clean rollback worktree")
    tracked = set(_git(repo, "ls-files", "-z").split("\0"))
    for relative in [*plan["restore"], "VERSION", "release_state.json"]:
        destination = repo / relative
        if (destination.exists() or destination.is_symlink()) and relative not in tracked:
            raise RollbackError(f"Untracked file would be overwritten: {relative}")
        for ancestor in destination.parents:
            if ancestor == repo:
                break
            if ancestor.is_symlink() or ancestor.is_file():
                raise RollbackError(f"Unsafe destination parent: {relative}")
        if destination.is_symlink() and relative in {"VERSION", "release_state.json"}:
            raise RollbackError(f"Release metadata cannot be a symlink: {relative}")


def apply_rollback(repo, plan):
    """Stage code and release metadata; never commit, push, or restore data."""
    repo = _repository(repo)
    _preflight(repo, plan)
    if plan["restore"]:
        _git(repo, "restore", f"--source={plan['target_commit']}", "--staged", "--worktree", "--", *plan["restore"])
    if plan["remove"]:
        _git(repo, "rm", "--", *plan["remove"])
    state = {"version": plan["target_tag"][1:], "target_tag": plan["target_tag"],
             "target_commit": plan["target_commit"], "previous_head": plan["previous_head"],
             "prepared_at": datetime.now(timezone.utc).isoformat()}
    (repo / "VERSION").write_text(state["version"] + "\n", encoding="utf-8")
    (repo / "release_state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", "--", "VERSION", "release_state.json")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Existing immutable release tag, e.g. v1.0.0")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--apply", action="store_true", help="Stage the previewed code restore; do not commit or push")
    args = parser.parse_args()
    try:
        plan = plan_rollback(args.repo, args.version)
        state = apply_rollback(args.repo, plan) if args.apply else None
        print(json.dumps({"mode": "prepared" if args.apply else "preview", **plan,
                          **({"release_state": state} if state else {})}, indent=2))
        if args.apply:
            print("Review git diff --cached before creating a normal rollback commit.")
        return 0
    except (RollbackError, OSError) as error:
        print(f"Rollback not prepared: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
