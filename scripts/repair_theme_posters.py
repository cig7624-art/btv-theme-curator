"""Repair saved poster addresses without generating or replacing recommendations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import urlparse

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poster_assets import enrich_poster_candidates


def lookup_diagnostics(unresolved):
    """Inspect a bounded sample of unresolved works using the normal API limits."""
    from poster_assets import _is_tv, _verified_wikipedia_page, _wikipedia_pages

    output = []
    for item in unresolved[:8]:
        title, year, kind = _identity(item)
        tv = _is_tv(kind)
        titles = [title, f"{title} (드라마)", f"{title} (텔레비전 프로그램)"] if tv else [title, f"{title} (영화)"]
        if year:
            titles.append(f"{title} ({year}년 {'드라마' if tv else '영화'})")
        result = dict(title=title, year=year, content_type=kind)
        try:
            pages = _wikipedia_pages("ko", titles, english_links=True)
            result["korean_pages"] = [dict(page, identity_verified=_verified_wikipedia_page(page, [title], year, tv)) for page in pages]
            links = list(dict.fromkeys(link["*"] for page in pages
                                      if _verified_wikipedia_page(page, [title], year, tv)
                                      for link in page.get("langlinks", []) if link.get("lang") == "en" and link.get("*")))
            if links:
                result["english_pages"] = [dict(page, identity_verified=_verified_wikipedia_page(page, links, year, tv))
                                           for page in _wikipedia_pages("en", links)]
        except requests.RequestException as error:
            result["error"] = {"type": type(error).__name__, "message": str(error)[:300]}
        output.append(result)
    return output


def _text(value):
    return "" if value is None or pd.isna(value) else str(value).strip()


def _timestamp(value):
    try:
        result = pd.Timestamp(value) if _text(value) else None
        if result is None or pd.isna(result):
            return None
        return result.tz_localize("Asia/Seoul").tz_convert("UTC") if result.tzinfo is None else result.tz_convert("UTC")
    except (TypeError, ValueError, OverflowError):
        return None


def _url(value):
    try:
        parsed = urlparse(_text(value))
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        return False


def _identity(row):
    return tuple(_text(row.get(key)) for key in ("title", "year", "content_type"))


def _load(root):
    content = pd.read_csv(root / "theme_content_candidates.csv", sep="|", dtype=str).fillna("")
    required = {"run_id", "theme_key", "rank", "title", "year", "content_type", "poster_url"}
    if not required.issubset(content.columns):
        raise ValueError("Saved content is missing required poster identity columns")
    history_path = root / "theme_recommendation_history.csv"
    history = pd.read_csv(history_path, sep="|", dtype=str).fillna("") if history_path.exists() else pd.DataFrame()
    return content, history


def latest_run_id(history, content):
    """Use generation timestamps, never CSV append order, to choose the latest run."""
    runs = {}
    for row in history.to_dict("records"):
        run_id = _text(row.get("run_id"))
        if not run_id or run_id == "THEME_DB":
            continue
        stamp = _timestamp(row.get("recommended_at")) or _timestamp(row.get("recommended_date"))
        if stamp is None:
            match = re.match(r"(\d{8})-(\d{6})", run_id)
            if match:
                try:
                    stamp = _timestamp(datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S"))
                except ValueError:
                    pass
        if stamp is not None:
            runs[run_id] = max(runs.get(run_id, stamp), stamp)
    if runs:
        return max(runs, key=lambda key: (runs[key], key))
    # A repository without generation history can still repair its latest saved
    # run, but THEME_DB is a mirror rather than a recommendation generation.
    for row in content.to_dict("records"):
        run_id, stamp = _text(row.get("run_id")), _timestamp(row.get("fetched_at"))
        if run_id and run_id != "THEME_DB" and stamp is not None:
            runs[run_id] = max(runs.get(run_id, stamp), stamp)
    return max(runs, key=lambda key: (runs[key], key)) if runs else ""


def visible_candidates(content, run_id):
    """Match the five initially displayed unique works for each saved theme."""
    if not run_id:
        return content.iloc[:0].copy()
    scoped = content[content["run_id"].eq(run_id)].copy()
    if scoped.empty:
        return scoped
    scoped["_rank"] = pd.to_numeric(scoped["rank"], errors="coerce")
    scoped = scoped.sort_values("_rank", kind="stable", na_position="last")
    selected = []
    for _, group in scoped.groupby("theme_key", sort=False):
        seen = set()
        for index, row in group.iterrows():
            title = re.sub(r"[^0-9a-z가-힣]", "", unicodedata.normalize("NFKC", _text(row["title"])).lower())
            identity = (title, _text(row["year"]))
            if not title or identity in seen:
                continue
            seen.add(identity)
            selected.append(index)
            if len(seen) == 5:
                break
    return content.loc[selected].copy()


def collect_updates(root=ROOT, *, limit=50):
    root = Path(root)
    content, history = _load(root)
    run_id = latest_run_id(history, content)
    visible = visible_candidates(content, run_id)
    requested = {}
    for row in visible.to_dict("records"):
        if not _text(row.get("poster_url")) and _identity(row) not in requested and len(requested) < max(0, int(limit)):
            identity = _identity(row)
            requested[identity] = dict(zip(("title", "year", "content_type"), identity), poster_url="")
    results = enrich_poster_candidates(list(requested.values()), limit=max(0, int(limit))) if requested else []
    updates = {}
    for row in results:
        identity = _identity(row)
        if identity in requested and _url(row.get("poster_url")):
            updates[identity] = {**requested[identity], "poster_url": _text(row["poster_url"]),
                                 "poster_source": _text(row.get("poster_source")),
                                 "poster_source_url": _text(row.get("poster_source_url"))}
    return {"schema_version": 1, "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_run_id": run_id, "visible_candidates": len(visible),
            "visible_with_poster": sum(_url(value) for value in visible["poster_url"]),
            "checked_unique": len(requested), "found_unique": len(updates), "updates": list(updates.values()),
            "unresolved": [item for identity, item in requested.items() if identity not in updates]}


def _atomic_write(path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        write(temporary)
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def apply_updates(manifest, root=ROOT, *, write=False):
    root = Path(root)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("updates"), list):
        raise ValueError("Unsupported poster update manifest")
    updates = {}
    for row in manifest["updates"]:
        if isinstance(row, dict) and _identity(row)[0] and _url(row.get("poster_url")):
            updates[_identity(row)] = _text(row["poster_url"])
    content, history = _load(root)
    latest = latest_run_id(history, content)
    before = visible_candidates(content, latest)
    enriched = content.copy()
    updated = 0
    for index, row in content.iterrows():
        poster = updates.get(_identity(row))
        if poster and not _text(row["poster_url"]):
            enriched.at[index, "poster_url"] = poster
            updated += 1
    # Only the poster column may change; rank, titles, row order, all recommendation
    # fields, unknown future fields and existing nonempty artwork remain intact.
    pd.testing.assert_frame_equal(content.drop(columns="poster_url"), enriched.drop(columns="poster_url"))
    after = visible_candidates(enriched, latest)
    report = {"schema_version": 1, "applied_at": datetime.now(timezone.utc).isoformat(),
              "mode": "write" if write else "dry_run", "source_run_id": manifest.get("source_run_id", ""),
              "latest_run_id": latest, "checked_unique": int(manifest.get("checked_unique", 0)),
              "found_unique": len(updates), "updated_rows": updated,
              "visible_candidates": len(after),
              "before_visible_with_poster": sum(_url(value) for value in before["poster_url"]),
              "visible_with_poster": sum(_url(value) for value in after["poster_url"])}
    report["unresolved"] = manifest.get("unresolved", [])
    if "provider_diagnostics" in manifest:
        report["provider_diagnostics"] = manifest["provider_diagnostics"]
    if write:
        if updated:
            _atomic_write(root / "theme_content_candidates.csv", lambda path: enriched.to_csv(path, sep="|", index=False, encoding="utf-8-sig"))
        _atomic_write(root / "artifacts" / "poster_repair_status.json",
                      lambda path: path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--collect", type=Path, help="Look up missing visible artwork and save an update manifest; never change recommendations")
    mode.add_argument("--apply", type=Path, help="Apply a previously collected manifest to the latest saved candidates")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--diagnose", action="store_true", help="Include public provider responses in the repair report")
    parser.add_argument("--write", action="store_true", help="Write poster-only changes and an audit report; default apply is dry run")
    args = parser.parse_args()
    if args.collect:
        manifest = collect_updates(args.root, limit=args.limit)
        if args.diagnose:
            manifest["provider_diagnostics"] = lookup_diagnostics(manifest["unresolved"])
        _atomic_write(args.collect, lambda path: path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"))
        print(json.dumps({key: value for key, value in manifest.items() if key != "updates"}, ensure_ascii=False, indent=2))
    else:
        manifest = json.loads(args.apply.read_text(encoding="utf-8"))
        print(json.dumps(apply_updates(manifest, args.root, write=args.write), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
