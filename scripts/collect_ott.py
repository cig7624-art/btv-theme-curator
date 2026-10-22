"""Collect only public OTT home pages and save an honest, durable snapshot."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ott_collection import PLATFORMS, PublicBrowser, collect_public_homes, load_snapshot, save_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="ott_home_snapshot.json")
    parser.add_argument("--platform", action="append", choices=[p["id"] for p in PLATFORMS])
    parser.add_argument("--browser", action="store_true", help="Render the public home with Playwright Chromium")
    parser.add_argument("--chromium-path", help="Optional installed Chromium executable")
    parser.add_argument("--evidence-dir", help="Write source HTML and screenshots for browser evidence")
    parser.add_argument("--require-home", action="store_true", help="Exit nonzero after saving if no current public home collection was obtained")
    args = parser.parse_args()
    previous = load_snapshot(args.output)
    if args.browser:
        with PublicBrowser(evidence_dir=args.evidence_dir, executable_path=args.chromium_path) as browser:
            result = collect_public_homes(previous, args.platform, fetcher=browser)
    else:
        result = collect_public_homes(previous, args.platform)
    save_snapshot(result, args.output)
    print(json.dumps({"updated_at": result["updated_at"], "platforms": [{"id": p["id"], "status": p["status"], "freshness": p["freshness"], "sections": len(p["sections"]) if p["status"] == "ok" else None, "promotions": len(p["promotions"]), "official_updates": len(p.get("official_updates", [])), "error_code": p["error_code"]} for p in result["platforms"]]}, ensure_ascii=False, indent=2))
    if args.require_home and not any(p["status"] == "ok" and p["freshness"] == "current" and p["sections"] for p in result["platforms"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
