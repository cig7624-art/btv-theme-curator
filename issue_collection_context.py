"""Apply the latest collection outcome to stored current-list evidence.

A preserved feed is useful after a source failure, but its previous popular-list
membership cannot stand in for the latest failed or changed API response.
"""
from collections.abc import Mapping
import json

import pandas as pd

from issue_quality import YOUTUBE, classify_source


_CATEGORIES = {'all', '1', '24'}


def _timestamp(value):
    try:
        result = pd.Timestamp(value)
        if pd.isna(result):
            return None
        return result.tz_localize('UTC') if result.tzinfo is None else result.tz_convert('UTC')
    except (TypeError, ValueError, OverflowError):
        return None


def _metrics(value):
    try:
        metrics = dict(value) if isinstance(value, Mapping) else json.loads(value)
        return metrics if isinstance(metrics, dict) else None
    except (TypeError, ValueError):
        return None


def apply_collection_context(frame, status):
    """Copy ``frame`` and invalidate superseded/failed popular membership only.

    Legacy status without explicit popular-list diagnostics does not establish
    or refresh membership. A later valid empty response supersedes older rows
    just as a nonempty response does. Status older than a row cannot revoke it.
    """
    result = frame.copy()
    if result.empty or 'metrics_json' not in result or not isinstance(status, Mapping):
        return result
    sources = status.get('sources')
    youtube = sources.get('youtube') if isinstance(sources, Mapping) else None
    if not isinstance(youtube, Mapping):
        return result
    raw_snapshots = youtube.get('popular_snapshots')
    snapshots = {
        str(category): parsed
        for category, value in raw_snapshots.items()
        if str(category) in _CATEGORIES and (parsed := _timestamp(value)) is not None
    } if isinstance(raw_snapshots, Mapping) else {}
    raw_failed = youtube.get('popular_failed_lists')
    failed = {str(category) for category in raw_failed if str(category) in _CATEGORIES} if isinstance(raw_failed, (list, tuple, set)) else set()
    checked = _timestamp(youtube.get('popular_checked_at'))
    if not snapshots and not (failed and checked is not None):
        return result
    metrics_column = result.columns.get_loc('metrics_json')
    for position, row in enumerate(result.to_dict('records')):
        if classify_source(row) != YOUTUBE:
            continue
        original = row.get('metrics_json')
        metrics = _metrics(original)
        if metrics is None:
            continue
        category = str(metrics.get('popular_category_id', ''))
        snapshot = _timestamp(metrics.get('popular_snapshot_at'))
        complete = metrics.get('popular_snapshot_complete')
        if category not in _CATEGORIES or snapshot is None or str(complete).lower() not in {'true', '1'}:
            continue
        failed_later = category in failed and checked is not None and checked >= snapshot
        replaced = category in snapshots and snapshots[category] > snapshot
        if failed_later or replaced:
            metrics['popular_snapshot_complete'] = False
            result.iat[position, metrics_column] = metrics if isinstance(original, Mapping) else json.dumps(metrics, ensure_ascii=False, sort_keys=True)
    return result
