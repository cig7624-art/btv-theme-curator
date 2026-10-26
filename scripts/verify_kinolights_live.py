#!/usr/bin/env python3
"""Exercise the actual logged-out title/provider client without keys or models."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kinolights_availability as kino


def main():
    kino.clear_cache()
    report = {"checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              "source": kino.GRAPHQL_URL, "credential_use": False, "lookups": []}
    with patch.object(kino, "_graphql", wraps=kino._graphql) as calls:
        for query, years in (("기생충", ("2019",)), ("괴물", ("2006", "2023"))):
            candidates = kino.search_titles(query)
            network_count = calls.call_count
            assert kino.search_titles(query) == candidates
            assert calls.call_count == network_count, "Repeated search should use its short cache"
            for year in years:
                selected = next((row for row in candidates if row["title"] == query and row["year"] == year), None)
                assert selected, f"Actual search did not expose {query} ({year})"
                result = kino.get_title_availability(selected)
                assert result["status"] in {"available", "unavailable"}, f"Public title lookup unreadable: {result}"
                network_count = calls.call_count
                assert kino.get_title_availability(selected) == result
                assert calls.call_count == network_count, "Repeated title selection should use its short cache"
                report["lookups"].append({"selected": selected, "result": result})
        report["public_query_count"] = calls.call_count
    assert report["lookups"][1]["selected"]["id"] != report["lookups"][2]["selected"]["id"], "Remakes need distinct source IDs"
    report["status"] = "passed"
    target = Path(__file__).resolve().parents[1] / "artifacts" / "kinolights_live_verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "public_queries": report["public_query_count"],
                      "titles": [{"title": row["selected"]["title"], "year": row["selected"]["year"],
                                  "providers": len(row["result"]["providers"])} for row in report["lookups"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
