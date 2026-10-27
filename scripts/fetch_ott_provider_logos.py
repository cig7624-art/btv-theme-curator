"""One-time, bounded public provider-logo capture; no model or account data."""
from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image
import requests


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "ott-providers"
GATEWAY = "https://gateway.kinolights.com/graphql"
BRANDS = {
    "netflix": ("넷플릭스", "https://www.netflix.com/kr/"),
    "tving": ("티빙", "https://www.tving.com/"),
    "wavve": ("웨이브", "https://www.wavve.com/"),
    "watcha-play": ("왓챠", "https://watcha.com/"),
    "coupang-play": ("쿠팡플레이", "https://www.coupangplay.com/"),
    "disney": ("디즈니+", "https://www.disneyplus.com/ko-kr"),
    "apple": ("Apple TV", "https://tv.apple.com/kr"),
}
ALIASES = {"watcha": "watcha-play", "disney-plus": "disney", "disney+": "disney",
           "coupang": "coupang-play", "coupangplay": "coupang-play", "apple-tv": "apple"}
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.6"}
TYPE_FIELDS = "kind name ofType { kind name ofType { kind name } }"
REQUEST_LIMIT = 45
_calls = 0


def request(method, url, **kwargs):
    global _calls
    _calls += 1
    if _calls > REQUEST_LIMIT:
        raise RuntimeError("public_request_budget_exhausted")
    response = requests.request(method, url, timeout=(5, 15), headers=HEADERS, **kwargs)
    response.raise_for_status()
    if len(response.content) > 3_000_000:
        raise ValueError("public_response_too_large")
    return response


def graph(query, variables=None):
    response = request("POST", GATEWAY, json={"query": query, "variables": variables or {}})
    data = response.json()
    if data.get("errors") or not isinstance(data.get("data"), dict):
        raise ValueError("public_graphql_response_unavailable")
    return data["data"]


def named_type(value):
    while value and not value.get("name"):
        value = value.get("ofType")
    return value or {}


def schema(name):
    return graph("query PublicProviderShape($name:String!) { __type(name:$name) { name fields { name type { "
                 + TYPE_FIELDS + " } } } }", {"name": name}).get("__type") or {}


def public_url(value):
    try:
        parsed = urlsplit(str(value))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return ""
        host = parsed.hostname.lower()
        parsed.port
        suffixes = ("kinolights.com", "netflix.com", "nflxext.com", "nflximg.net", "tving.com", "cj.net",
                    "wavve.com", "watcha.com", "watcha.net", "coupangplay.com", "disneyplus.com", "dssott.com",
                    "disney.com", "apple.com", "mzstatic.com")
        return str(value) if any(host == suffix or host.endswith("." + suffix) for suffix in suffixes) else ""
    except ValueError:
        return ""


def provider_rows():
    query = schema("Query")
    field = next(row for row in query.get("fields", []) if row["name"] == "providers")
    definition = schema(named_type(field["type"])["name"])
    selection = []
    for field in definition.get("fields", [])[:60]:
        name = field["name"]
        if re.search(r"user|account|isMy|subscrib|interest|watch", name, re.I):
            continue
        kind = named_type(field["type"])
        if kind.get("kind") in {"SCALAR", "ENUM"}:
            selection.append(name)
        elif kind.get("kind") == "OBJECT" and re.search(r"logo|icon|image", name, re.I):
            image = schema(kind["name"])
            fields = [row["name"] for row in image.get("fields", [])
                      if row["name"] in {"id", "pathUrl", "url", "src", "width", "height", "path"}
                      and named_type(row["type"]).get("kind") in {"SCALAR", "ENUM"}]
            if fields:
                selection.append(name + " { " + " ".join(fields) + " }")
    rows = graph("query PublicProviderBranding { providers { " + " ".join(selection) + " } }")["providers"]
    return rows if isinstance(rows, list) else []


def identity(row):
    for key in ("technicalName", "providerTechnicalName", "slug", "name", "nameEn", "nameKr", "id"):
        value = str(row.get(key, "")).strip()
        normalized = value.casefold().replace(" ", "-")
        canonical = ALIASES.get(normalized, normalized)
        if canonical in BRANDS:
            return canonical
        for identifier, (name, _) in BRANDS.items():
            if value == name:
                return identifier
    return ""


def image_urls(row, path=""):
    found = []
    if isinstance(row, dict):
        for key, value in row.items():
            found.extend(image_urls(value, path + "." + key))
    elif isinstance(row, list):
        for value in row[:20]:
            found.extend(image_urls(value, path))
    elif isinstance(row, str) and public_url(row) and re.search(r"logo|icon|image", path, re.I):
        found.append((0 if re.search(r"logo|icon", path, re.I) else 1, row))
    return sorted(found)


def icon_candidates(home):
    response = request("GET", home)
    soup = BeautifulSoup(response.content, "html.parser")
    values = []
    for node in soup.find_all("link", href=True):
        if "icon" in " ".join(node.get("rel", [])).casefold():
            url = public_url(urljoin(response.url, node["href"]))
            if url and url not in values:
                values.append(url)
    fallback = public_url(urljoin(home, "/favicon.ico"))
    return values[:3] + ([fallback] if fallback not in values else [])


def save_image(identifier, url):
    response = request("GET", url)
    image = Image.open(BytesIO(response.content))
    image.load()
    if image.width < 16 or image.height < 16 or image.width > 4000 or image.height > 4000:
        raise ValueError("not_a_provider_icon")
    image.thumbnail((96, 96))
    output = BytesIO()
    image.convert("RGBA").save(output, format="PNG", optimize=True)
    payload = output.getvalue()
    (OUT / (identifier + ".png")).write_bytes(payload)
    return {"source_url": response.url, "width": image.width, "height": image.height,
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "credential_use": False,
              "purpose": "Identify services in public subscription availability results; trademarks belong to their owners.",
              "providers": {}, "errors": []}
    try:
        rows = provider_rows()
    except Exception as error:
        rows = []
        report["errors"].append({"stage": "public_provider_metadata", "code": type(error).__name__})
    lookup = {identity(row): row for row in rows if isinstance(row, dict) and identity(row)}
    for identifier, (name, home) in BRANDS.items():
        urls = [url for _, url in image_urls(lookup.get(identifier, {}))][:3]
        result = {"name": name, "official_home": home, "status": "unavailable"}
        tried = set()
        for phase in range(2):
            if phase and not urls:
                try:
                    urls = icon_candidates(home)
                except Exception:
                    urls = [urljoin(home, "/favicon.ico")]
            for url in urls:
                if url in tried or not public_url(url):
                    continue
                tried.add(url)
                try:
                    result.update(save_image(identifier, url), status="downloaded")
                    break
                except Exception:
                    pass
            if result["status"] == "downloaded":
                break
            urls = []
        report["providers"][identifier] = result
    report["public_request_count"] = _calls
    report["status"] = "passed" if all(item["status"] == "downloaded" for item in report["providers"].values()) else "partial"
    (OUT / "sources.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "providers": {key: value["status"] for key, value in report["providers"].items()}, "requests": _calls}))


if __name__ == "__main__":
    main()
