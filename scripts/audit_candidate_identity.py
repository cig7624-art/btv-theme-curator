"""Read public work metadata without changing candidates or generating titles."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poster_assets import _is_tv, _key, _verified_wikipedia_page, _wikipedia_pages


DEFAULT_CANDIDATES = (("밀그램 프로젝트", "2021", "영화"), ("룸메이트", "2013", "드라마"))


def describe_page(page, requested, *, language="ko", requested_titles=None):
    """Report the page's own release identity; do not relax the requested one."""
    title, year, kind = requested
    canonical = str(page.get("title", "")).strip()
    labels = [str(item.get("title", "")) for item in page.get("categories", []) if isinstance(item, dict)]
    extract = str(page.get("extract", ""))
    names = requested_titles or [title]
    exact = _key(re.sub(r"\s*\([^()]+\)$", "", canonical)) in {
        _key(re.sub(r"\s*\([^()]+\)$", "", name)) for name in names}
    movie = _verified_wikipedia_page(page, names, "", False)
    television = _verified_wikipedia_page(page, names, "", True)
    media_type = "영화" if movie else ""
    if television:
        media_type = "예능" if re.search(r"예능|리얼리티|버라이어티|\b(?:reality|variety|game show|talk show)\b",
                                       " ".join([extract, *labels]), re.I) else "드라마"
    years = set()
    for label in labels:
        match = re.match(r"^(?:분류|Category):\s*((?:19|20)\d{2})(.*)$", label, re.I)
        if not match:
            continue
        tail = match.group(2)
        release = (bool(re.search(r"(?:영화|\bfilms?)\s*$", tail, re.I)) if movie else
                   bool(re.search(r"television series debuts|TV series debuts|년에?\s*(?:시작한|방영을\s*시작한)", tail, re.I)
                        or re.fullmatch(r"년\s*(?:드라마|텔레비전\s*드라마)", tail))) if media_type else False
        if release:
            years.add(match.group(1))
    verified_source = ""
    if len(years) == 1 and media_type and _verified_wikipedia_page(page, names, next(iter(years)), television):
        verified_source = f"https://{language}.wikipedia.org/wiki/" + quote(canonical.replace(" ", "_"))
    return {
        "title": canonical, "missing": "missing" in page,
        "exact_normalized_base_title": exact,
        "canonical_release_years": sorted(years), "canonical_content_type": media_type,
        "requested_identity_verified": bool(media_type == kind and _verified_wikipedia_page(page, names, year, _is_tv(kind))),
        "verified_source_url": verified_source,
        "extract": extract[:2000], "categories": labels,
        "english_links": [str(link.get("*", "")) for link in page.get("langlinks", [])
                          if isinstance(link, dict) and link.get("lang") == "en" and link.get("*")],
    }


def audit_candidate(candidate, *, pages_provider=_wikipedia_pages):
    title, year, kind = candidate
    suffixes = ["영화", "드라마", "텔레비전 프로그램", f"{year}년 영화", f"{year}년 드라마"]
    result = {"requested_title": title, "requested_year": year, "requested_content_type": kind,
              "korean_pages": [], "english_pages": [], "errors": []}
    try:
        pages = pages_provider("ko", [title, *[f"{title} ({suffix})" for suffix in suffixes]], english_links=True)
        pages += pages_provider("ko", [title], english_links=True, search=True)
        unique = {(page.get("pageid"), page.get("title")): page for page in pages if isinstance(page, dict)}
        result["korean_pages"] = [describe_page(page, candidate) for page in unique.values()]
        links = sorted({link for page in result["korean_pages"] if page["verified_source_url"]
                        for link in page["english_links"]})
        if links:
            english = pages_provider("en", links)
            result["english_pages"] = [describe_page(page, candidate, language="en", requested_titles=links)
                                       for page in english if isinstance(page, dict)]
    except requests.RequestException:
        result["errors"].append("public_metadata_unavailable")
    result["requested_identity_confirmed"] = any(page["requested_identity_verified"] for page in result["korean_pages"])
    result["verified_alternate_identities"] = sum(bool(page["verified_source_url"]) and not page["requested_identity_verified"]
                                                for page in result["korean_pages"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", action="append", metavar="TITLE|YEAR|TYPE",
                        help="Public work identity to audit; repeat for multiple candidates.")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/candidate_identity_audit.json")
    args = parser.parse_args(argv)
    candidates = []
    for value in args.candidate or []:
        parts = tuple(part.strip() for part in value.split("|"))
        if len(parts) != 3 or not parts[0] or not re.fullmatch(r"(?:19|20)\d{2}", parts[1]):
            parser.error("--candidate requires TITLE|YEAR|TYPE")
        candidates.append(parts)
    results = [audit_candidate(candidate) for candidate in candidates or DEFAULT_CANDIDATES]
    report = {"schema_version": 1, "audited_at": datetime.now(timezone.utc).isoformat(),
              "mode": "read_only_public_metadata", "candidate_count": len(results), "candidates": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": len(results),
                      "requested_identities_confirmed": sum(result["requested_identity_confirmed"] for result in results),
                      "metadata_errors": sum(len(result["errors"]) for result in results)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
