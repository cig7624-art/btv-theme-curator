"""Public Coupang Play channel updates when the Korean content home is unavailable.

This source is an official YouTube channel, not a substitute measurement of the
Coupang homepage. API/RSS observations and older verified cache observations keep
separate provenance. No account, login, private endpoint or location workaround
is used.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET

import requests

CHANNEL_ID = "UCjn-VbcIkAeXQKCmLJV8YwQ"
CHANNEL_URL = "https://www.youtube.com/channel/" + CHANNEL_ID
OFFICIAL_CHANNEL_URL = CHANNEL_URL
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=" + CHANNEL_ID
CHANNELS_API = "https://www.googleapis.com/youtube/v3/channels"
PLAYLIST_API = "https://www.googleapis.com/youtube/v3/playlistItems"
TIMEOUT = (5, 8)
MAX_AGE_DAYS = 21
CACHE_MAX_AGE_HOURS = 96
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_TRAILER = re.compile(r"예고|티저|트레일러|trailer|teaser", re.I)
_ANNOUNCEMENT = re.compile(r"제작|공개|론칭|런칭|라인업|캐스팅|coming\s*soon|announcement", re.I)
_GENERIC = re.compile(r"^(?:쿠팡플레이|coupang\s*play|공식|official|예고편|티저|하이라이트|메인\s*예고편|\d+화\s*예고편?)$", re.I)
_AD = re.compile(r"유료\s*광고|광고\s*(?:포함|문의)|이벤트\s*(?:참여|당첨)|채널\s*구독|구독\s*이벤트", re.I)
_SPORTS_CLIP = re.compile(r"(?:경기|축구|야구)\s*하이라이트|골\s*모음|선방\s*모음|프리미어리그|K\s*리그|NBA|NFL|UEFA|토트넘", re.I)
_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015", "m": "http://search.yahoo.com/mrss/"}


def _text(value, limit=300):
    return unicodedata.normalize('NFC', re.sub(r"\s+", " ", str(value or "")).strip())[:limit]


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _video_url(video_id):
    return "https://www.youtube.com/watch?v=" + video_id


def _matches_video(value, video_id):
    if not _VIDEO_ID.fullmatch(str(video_id or "")):
        return False
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            return False
        if parsed.hostname in {"www.youtube.com", "youtube.com"}:
            return parsed.path == "/watch" and parse_qs(parsed.query).get("v") == [video_id]
        return parsed.hostname == "youtu.be" and parsed.path == "/" + video_id
    except ValueError:
        return False


def _thumbnail(value, video_id):
    try:
        parsed = urlsplit(str(value or ""))
        host = parsed.hostname or ""
        if (parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.port
                and (host.endswith(".ytimg.com") or host == "img.youtube.com")
                and re.match(r"/vi(?:_webp)?/" + re.escape(video_id) + r"/", parsed.path)):
            return value
    except ValueError:
        pass
    # Standard public artwork for an already verified video, never a guessed ID.
    return "https://i.ytimg.com/vi/" + video_id + "/hqdefault.jpg"


def _work_title(title, related=""):
    related = _text(related, 100)
    compact = re.sub(r"\s+", "", title).casefold()
    if related and re.sub(r"\s+", "", related).casefold() in compact and not _GENERIC.fullmatch(related):
        return related
    matches = re.findall(r"\[([^\[\]]{1,100})\]|〈([^〈〉]{1,100})〉|《([^《》]{1,100})》", title)
    for parts in matches:
        candidate = _text(next(part for part in parts if part), 100)
        candidate = re.sub(r'\s*(?:제?\s*\d+\s*[화회]|EP\.?\s*\d+)\s*$', '', candidate, flags=re.I).strip()
        if not _GENERIC.fullmatch(candidate) and not re.fullmatch(r"\d+화|시즌\s*\d+|EP\.?\s*\d+", candidate, re.I):
            return candidate
    # Only explicit title/separator/trailer formatting supplies another title.
    first = re.split(r"\s*[|｜]\s*", title)[0].strip()
    if first != title and first and not _GENERIC.fullmatch(first) and not _TRAILER.search(first):
        return first[:100]
    return ""


def _candidate(video_id, title, published, observed, now, *, image="", related="", source_position=1):
    video_id, title = str(video_id or ""), _text(title)
    published_date, observed_date = _date(published), _date(observed)
    if (not _VIDEO_ID.fullmatch(video_id) or not title or _AD.search(title)
            or published_date is None or observed_date is None
            or published_date > now + timedelta(minutes=5)
            or published_date < now - timedelta(days=MAX_AGE_DAYS)
            or published_date > observed_date + timedelta(minutes=5)
            or observed_date > now + timedelta(minutes=5)):
        return None
    kind = "trailer" if _TRAILER.search(title) else "announcement" if _ANNOUNCEMENT.search(title) else "clip"
    if kind == "clip" and _SPORTS_CLIP.search(title):
        return None
    work_title = _work_title(title, related)
    if not work_title:
        return None
    return {"title": title, "work_title": work_title, "video_id": video_id,
            "url": _video_url(video_id), "image_url": _thumbnail(image, video_id),
            "published_at": str(published), "observed_at": _iso(observed_date),
            "source_position": source_position, "update_kind": kind}


def _select(rows):
    priority = {"trailer": 3, "announcement": 2, "clip": 1}
    rows = sorted((row for row in rows if row), key=lambda row: (
        priority[row["update_kind"]], _date(row["published_at"])), reverse=True)
    selected, seen_works, seen_videos = [], set(), set()
    for row in rows:
        work = re.sub(r"[\W_]+", "", row["work_title"]).casefold()
        if work in seen_works or row["video_id"] in seen_videos:
            continue
        seen_works.add(work); seen_videos.add(row["video_id"])
        selected.append(dict(row, position=len(selected) + 1))
        if len(selected) == 5:
            break
    return selected


def _get(fetcher, url, *, params=None, xml=False):
    result = fetcher(url, params=params, timeout=TIMEOUT)
    if isinstance(result, (dict, str)):
        return result
    result.raise_for_status()
    return result.text[:2_000_000] if xml else result.json()


def _api(fetcher, key, now):
    data = _get(fetcher, CHANNELS_API, params={"part": "contentDetails", "id": CHANNEL_ID, "key": key})
    items = data.get("items", []) if isinstance(data, dict) else []
    channel = next((item for item in items if isinstance(item, dict) and item.get("id") == CHANNEL_ID), None)
    if not channel:
        return []
    playlist = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", "")
    if not isinstance(playlist, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,100}", playlist):
        return []
    data = _get(fetcher, PLAYLIST_API, params={"part": "snippet,contentDetails", "playlistId": playlist,
                                            "maxResults": 50, "key": key})
    rows = []
    for index, entry in enumerate(data.get("items", []) if isinstance(data, dict) else []):
        if not isinstance(entry, dict):
            continue
        snippet, details = entry.get("snippet", {}), entry.get("contentDetails", {})
        if not isinstance(snippet, dict) or not isinstance(details, dict):
            continue
        if snippet.get("channelId") != CHANNEL_ID or snippet.get("videoOwnerChannelId", CHANNEL_ID) != CHANNEL_ID:
            continue
        video_id = details.get("videoId") or snippet.get("resourceId", {}).get("videoId")
        resource_id = snippet.get("resourceId", {}).get("videoId")
        if resource_id and resource_id != video_id:
            continue
        thumbnails = snippet.get("thumbnails", {})
        image = next((thumbnails[k].get("url", "") for k in ("high", "medium", "default")
                      if isinstance(thumbnails.get(k), dict)), "") if isinstance(thumbnails, dict) else ""
        rows.append(_candidate(video_id, snippet.get("title"), details.get("videoPublishedAt") or snippet.get("publishedAt"),
                               _iso(now), now, image=image, source_position=index + 1))
    return _select(rows)


def _rss(fetcher, now):
    body = _get(fetcher, RSS_URL, xml=True)
    if not isinstance(body, str) or len(body) > 2_000_000 or "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        return []
    feed = ET.fromstring(body)
    if feed.findtext("yt:channelId", namespaces=_NS) != CHANNEL_ID:
        return []
    rows = []
    for index, entry in enumerate(feed.findall("a:entry", _NS)[:50]):
        if entry.findtext("yt:channelId", default=CHANNEL_ID, namespaces=_NS) != CHANNEL_ID:
            continue
        video_id = entry.findtext("yt:videoId", namespaces=_NS)
        link = next((a.get("href") for a in entry.findall("a:link", _NS) if a.get("rel", "alternate") == "alternate"), "")
        if not _matches_video(link, video_id):
            continue
        thumbnail = entry.find("m:group/m:thumbnail", _NS)
        rows.append(_candidate(video_id, entry.findtext("a:title", namespaces=_NS),
                               entry.findtext("a:published", namespaces=_NS), _iso(now), now,
                               image=thumbnail.get("url", "") if thumbnail is not None else "", source_position=index + 1))
    return _select(rows)


def _cache(path, now):
    rows = []
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        for index, raw in enumerate(csv.DictReader(source, delimiter="|")):
            if index >= 50_000:
                break
            if raw.get("channel_id") != CHANNEL_ID:
                continue
            observed = _date(raw.get("last_discovered_at"))
            if observed is None or not now - timedelta(hours=CACHE_MAX_AGE_HOURS) <= observed <= now + timedelta(minutes=5):
                continue
            if not _matches_video(raw.get("url"), raw.get("video_id")):
                continue
            rows.append(_candidate(raw.get("video_id"), raw.get("title"), raw.get("upload_date"),
                                   raw.get("last_discovered_at"), now, related=raw.get("related_content"),
                                   image=raw.get("image_url", ""), source_position=index + 1))
    return _select(rows)


def collect_coupang_updates(watchlist_path=Path("youtube_video_watchlist.csv"), *, api_key=None, now=None, fetcher=None):
    """Collect identified official works; return ``None`` when none is verified.

    ``fetcher(url, *, params=None, timeout=(5, 8))`` may return a Response,
    an API dict, or RSS text. API key values and network exception text are never
    printed or put in output. The API uses two low-cost reads, never search.
    """
    if isinstance(now, str):
        now = _date(now)
        if now is None:
            raise ValueError("invalid_observation_time")
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    fetcher = fetcher or requests.get
    key = str(api_key if api_key is not None else os.getenv("YOUTUBE_API_KEY", "")).strip()
    methods = []
    if key:
        methods.append(("youtube_api", lambda: _api(fetcher, key, now)))
    methods.extend((("youtube_rss", lambda: _rss(fetcher, now)),
                    ("verified_watchlist", lambda: _cache(watchlist_path, now))))
    for method, collect in methods:
        try:
            updates = collect()
        except (requests.RequestException, ValueError, TypeError, AttributeError, KeyError, OSError, csv.Error, ET.ParseError):
            continue
        if updates:
            return {"source_url": CHANNEL_URL, "source_scope": "official_channel",
                    "source_method": method, "observed_at": min(row["observed_at"] for row in updates),
                    "updates": updates}
    return None
