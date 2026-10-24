"""Bounded caches for saved catalog data and public issue calculations.

Only persisted data enters these caches. Credentials, browser state, generated
drafts and actions belong to their callers and must never be passed here.
``cache_data`` serializes results so even nested evidence lists are independent
for every caller.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from data_storage_lock import DATA_WRITE_LOCK
from issue_collection_context import apply_collection_context
from issue_quality import assess_issues, build_ranked_issues
from issue_related_news import attach_related_news


_READ_ATTEMPTS = 3


class _FileChanged(RuntimeError):
    """A writer changed an input while a cached value was being prepared."""


def file_revision(path):
    """Identify replacement, in-place writes and files that do not yet exist."""
    resolved = Path(path).expanduser().resolve()
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        return (str(resolved), None, None, None, None)
    return (str(resolved), stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns, stat.st_ino)


def _require_revision(path, revision):
    if file_revision(path) != revision:
        raise _FileChanged("Saved data changed while it was being read. Please try again.")


def _clock(now):
    value = pd.Timestamp.now(tz="Asia/Seoul") if now is None else pd.Timestamp(now)
    if pd.isna(value):
        raise ValueError("now must be a valid date or timestamp")
    return value.tz_localize("Asia/Seoul") if value.tzinfo is None else value.tz_convert("Asia/Seoul")


def _clock_key(clock):
    # The minute changes at KST midnight as well as within a day. Keep the actual
    # timestamp for evaluation, rather than rounding future-evidence checks.
    return clock.floor("min").isoformat()


@st.cache_data(max_entries=32, show_spinner=False)
def _csv_at_revision(path, revision, dtype):
    # App writers use this same lock. Revisions also detect outside writers and
    # atomic replacements before a result can enter the cache.
    with DATA_WRITE_LOCK:
        _require_revision(path, revision)
        frame = pd.read_csv(path, sep="|", dtype=dtype).fillna("")
        _require_revision(path, revision)
    return frame


def cached_csv(path, *, dtype=None):
    """Read a pipe-delimited CSV, preserving inference and returning a copy."""
    for _ in range(_READ_ATTEMPTS):
        revision = file_revision(path)
        try:
            frame = _csv_at_revision(revision[0], revision, dtype)
            _require_revision(path, revision)
            return frame
        except _FileChanged:
            continue
    raise _FileChanged("Saved data kept changing while it was being read. Please try again.")


def _read_status(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


@st.cache_data(ttl=60, max_entries=16, show_spinner=False)
def _prepared_at_revisions(issue_path, issue_revision, status_path, status_revision,
                           release_version, clock_key, _now):
    # Do not acquire a cache's per-key lock while holding the write lock: a CSV
    # reader in another session can hold those two locks in the opposite order.
    issues = _csv_at_revision(issue_path, issue_revision, None)
    with DATA_WRITE_LOCK:
        _require_revision(issue_path, issue_revision)
        _require_revision(status_path, status_revision)
        status = _read_status(status_path)
        _require_revision(issue_path, issue_revision)
        _require_revision(status_path, status_revision)
    result = assess_issues(apply_collection_context(issues, status), now=_now)
    _require_revision(issue_path, issue_revision)
    _require_revision(status_path, status_revision)
    return result


def cached_prepared_issues(issue_path, status_path, *, release_version="", now=None):
    """Assess saved issues once per file revision and at most once per minute."""
    clock = _clock(now)
    for _ in range(_READ_ATTEMPTS):
        issue_revision = file_revision(issue_path)
        status_revision = file_revision(status_path)
        try:
            frame = _prepared_at_revisions(
                issue_revision[0], issue_revision, status_revision[0], status_revision,
                release_version, _clock_key(clock), _now=clock,
            )
            _require_revision(issue_path, issue_revision)
            _require_revision(status_path, status_revision)
            return frame
        except _FileChanged:
            continue
    raise _FileChanged("Saved data kept changing while issues were prepared. Please try again.")


@st.cache_data(ttl=60, max_entries=32, show_spinner=False)
def _core_at_clock(issues, news_pool, release_version, clock_key, _now):
    return attach_related_news(build_ranked_issues(issues), news_pool, now=_now)


def cached_core_issues(issues, *, news_pool=None, release_version="", now=None):
    """Rank assessed public issues with the original related-news pool semantics."""
    clock = _clock(now)
    return _core_at_clock(
        issues, issues if news_pool is None else news_pool,
        release_version, _clock_key(clock), _now=clock,
    )


def invalidate_saved_data():
    """Drop saved-data results after a write or an explicit refresh."""
    _csv_at_revision.clear()
    _prepared_at_revisions.clear()
    _core_at_clock.clear()
