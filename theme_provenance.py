"""Identify director-authored themes without changing saved data or run history.

Theme origin and recommendation-run origin are separate: a weekly run can reuse
a theme created in the director without becoming a director adoption run.
"""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd


_ORIGIN_FIELDS = ("source_status", "creation_origin", "recommendation_source")
_DIRECTOR_FIELDS = ("director_draft_id", "director_adoption_key")
_EMPTY_TEXT = {"", "nan", "none", "null", "<na>", "nat"}


def _text(value):
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        return ""
    text = str(value).strip()
    return "" if text.casefold() in _EMPTY_TEXT else text


def _record(value):
    return value if isinstance(value, (Mapping, pd.Series)) else {}


def _records(value):
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    if isinstance(value, (list, tuple)):
        return [_record(row) for row in value]
    return []


def _director_run_id(value):
    return _text(value).upper().startswith("AI-DIRECTOR-")


def is_director_theme(row):
    """Recognize current origin labels and earlier saved director identifiers."""
    row = _record(row)
    return (any(_text(row.get(field)).upper() == "AI_DIRECTOR" for field in _ORIGIN_FIELDS)
            or any(bool(_text(row.get(field))) for field in _DIRECTOR_FIELDS))


def director_theme_mask(themes, history=None):
    """Return a boolean mask aligned to ``themes``, including legacy history.

    Earlier adoptions can still use ``AI_GENERATED``. Their immutable draft/key
    columns or adoption-run history identify them without rewriting old CSVs.
    """
    if not isinstance(themes, pd.DataFrame):
        return pd.Series(dtype=bool)
    history_ids = {
        _text(row.get("theme_id"))
        for row in _records(history)
        if (_director_run_id(row.get("run_id")) or is_director_theme(row))
        and _text(row.get("theme_id"))
    }
    return pd.Series(
        [is_director_theme(row) or (_text(row.get("theme_id")) in history_ids)
         for row in themes.to_dict("records")],
        index=themes.index, dtype=bool,
    )


def weekly_recommendation_runs(runs, themes=None, history=None):
    """Keep weekly runs in their input order, without modifying any records.

    ``themes`` is accepted for caller compatibility but deliberately cannot
    determine a run's origin. A nested theme's director provenance does not
    exclude a legitimate weekly recommendation that later reuses that theme.
    """
    director_run_ids = {
        _text(row.get("run_id"))
        for row in _records(history)
        if (_director_run_id(row.get("run_id")) or is_director_theme(row))
        and _text(row.get("run_id"))
    }
    kept = []
    for run in runs or []:
        row = _record(run)
        run_id = _text(row.get("run_id"))
        meta = _record(row.get("meta"))
        if (_director_run_id(run_id) or _director_run_id(meta.get("run_id"))
                or is_director_theme(row) or is_director_theme(meta)
                or any(_text(rec.get("recommendation_source")).upper() == "AI_DIRECTOR"
                       for rec in _records(row.get("recommendations")))
                or run_id in director_run_ids):
            continue
        kept.append(run)
    return kept
