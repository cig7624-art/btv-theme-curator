"""Collect official media documents and merge the dedicated snapshot safely.

Collection needs no paid API. When OPENAI_API_KEY is configured, at most two
new or changed documents receive the same grounded analysis used by the app.
GitHub persistence uses a file-SHA compare-and-swap, never a checkout overwrite.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_insights_collection import SNAPSHOT_PATH, collect_media_insights, load_snapshot
from media_insights_storage import persist_media_analysis, persist_media_snapshot


def _bounded_number(lower, upper):
    def parse(value):
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError("An integer is required.") from None
        if not lower <= number <= upper:
            raise argparse.ArgumentTypeError(f"Use a value from {lower} to {upper}.")
        return number
    return parse


def _github_config():
    return {
        "token": os.environ.get("GITHUB_TOKEN", "").strip(),
        "repo": os.environ.get("GITHUB_REPOSITORY", "").strip(),
        "branch": (os.environ.get("GITHUB_BRANCH") or os.environ.get("GITHUB_REF_NAME") or "main").strip(),
        "media_insights_path": "media_insights_snapshot.json",
    }


def _analysis_candidates(previous, collected, latest):
    previous_hashes = {item["id"]: item.get("content_hash") for item in previous.get("items", [])}
    observed_hashes = {item["id"]: item.get("content_hash") for item in collected.get("items", [])}
    candidates = []
    for item in latest.get("items", []):
        identifier, source_hash = item.get("id"), item.get("content_hash")
        # Never spend on a retained document or one changed by another writer
        # since this run observed it. Valid saved analyses also need no repeat.
        if (not source_hash or observed_hashes.get(identifier) != source_hash
                or previous_hashes.get(identifier) == source_hash):
            continue
        analysis = item.get("analysis") or {}
        if (analysis.get("source_hash") == source_hash
                and analysis.get("model") and analysis.get("generated_at")):
            continue
        candidates.append(identifier)
    items = {item["id"]: item for item in latest.get("items", [])}
    return sorted(candidates, key=lambda key: (items[key].get("published_at", ""),
                                               items[key].get("collected_at", "")), reverse=True)


def _context_items(item, snapshot):
    candidates = [row for row in snapshot.get("items", [])
                  if row.get("id") != item.get("id") and row.get("evidence")]
    return sorted(candidates, key=lambda row: (row.get("topic") == item.get("topic"),
                                               row.get("published_at", "")), reverse=True)[:5]


def run_collection(*, output, max_documents=48, analyze_limit=2):
    """Return a credential-free report and exit code; never print raw failures."""
    previous = load_snapshot(output)
    collected = collect_media_insights(previous, max_documents=max_documents)
    config = _github_config()
    saved = persist_media_snapshot(collected, snapshot_path=output, github_config=config)
    snapshot = saved["snapshot"]
    report = {
        "collected_at": snapshot.get("collected_at", ""),
        "documents": len(snapshot.get("items", [])),
        "storage": saved["status"],
        "sources": [{"id": row.get("id"), "status": row.get("status"),
                     "documents": row.get("retained_item_count", 0)}
                    for row in collected.get("sources", [])],
        "analyses_created": 0,
        "analysis_failures": 0,
        "analysis_status": "disabled",
    }
    if not snapshot.get("items"):
        report["error_code"] = "no_official_documents_collected"
        return report, 1

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or not analyze_limit:
        report["analysis_status"] = "no_api_key" if not key else "disabled"
        return report, 0

    # Import only when paid analysis is enabled; collection remains independent.
    try:
        from media_insights_analysis import analyze_media_item
    except Exception:
        report["analysis_status"] = "unavailable"
        return report, 0
    report["analysis_status"] = "complete"
    model = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5.6-luna"
    candidates = _analysis_candidates(previous, collected, snapshot)[:min(2, max(0, analyze_limit))]
    for identifier in candidates:
        item = next((row for row in snapshot["items"] if row["id"] == identifier), None)
        if item is None:
            continue
        try:
            analysis = analyze_media_item(item, _context_items(item, snapshot), api_key=key, model=model)
            saved = persist_media_analysis(identifier, analysis, snapshot_path=output, github_config=config)
            snapshot = saved["snapshot"]
            report["analyses_created"] += 1
        except Exception:
            # Source collection stays useful when analysis is unavailable,
            # stale, rejected by citation checks, or concurrently superseded.
            report["analysis_failures"] += 1
    if report["analysis_failures"]:
        report["analysis_status"] = "partial"
    return report, 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=SNAPSHOT_PATH)
    parser.add_argument("--max-documents", type=_bounded_number(1, 160), default=48,
                        help="Bound the document discovery budget (default: 48).")
    parser.add_argument("--analyze-limit", type=_bounded_number(0, 2), default=2,
                        help="Analyze up to 0–2 new/changed documents when an API key is present.")
    args = parser.parse_args(argv)
    try:
        report, code = run_collection(output=args.output, max_documents=args.max_documents,
                                      analyze_limit=args.analyze_limit)
    except Exception:
        # Upstream network/storage errors can contain headers or response
        # bodies. Only fixed diagnostic codes belong in a public runner log.
        report, code = {"error_code": "media_collection_or_storage_failed"}, 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
