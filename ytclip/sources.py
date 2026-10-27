"""Places to find videos besides the trending charts, for the Research scan:

* Channel list    - new uploads from channels you choose (creators who allow clipping, say).
                    A channel is looked up once (channels.list, 1 unit), then its uploads
                    playlist is read at most every CHANNEL_EVERY (playlistItems.list, 1 unit).
* Creative Commons - YouTube searches limited to CC-BY videos (search.list, 100 units each),
                    so each search runs at most every SEARCH_EVERY.

Details for every video found (views, length, licence...) come from one videos.list call per
50 videos (1 unit), every scan, so momentum keeps updating between the slower source checks.
What each source found, and when, is kept in History.sources.
"""

from __future__ import annotations

import math
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from .trending import (API_URL, VIDEO_PARTS, ChartUnavailable, History, ResearchBlocked, ResearchError,
                       TrendingVideo, _get, video_from_api)

Log = Callable[[str], None]

CHANNEL_EVERY = 55 * 60  # re-read a channel's uploads after this long (just under an hour)
SEARCH_EVERY = 6 * 3600  # Creative Commons searches cost 100 units, so at most 4 a day each
RESOLVE_RETRY = 24 * 3600  # try a channel that couldn't be found again after a day
CHANNEL_VIDEOS = 5  # newest uploads taken from each channel
SEARCH_VIDEOS = 15  # results taken from each search
MAX_AGE_DAYS = 14  # older uploads aren't worth clipping for trend-driven posting

_UC = re.compile(r"UC[\w-]{22}")


@dataclass
class ChannelRef:
    """One line of the channel list: what the user typed, and how to look it up."""

    key: str  # normalised entry, used to cache the lookup
    kind: str  # id | handle | username
    value: str
    note: str = ""  # the text after '#': why clips from this channel are allowed

    @property
    def label(self) -> str:
        return {"handle": "@", "username": ""}.get(self.kind, "") + self.value


def split_entries(text: str) -> list[tuple[str, str]]:
    """Lines (or comma-separated items) with an optional '# note' after them."""
    out = []
    for line in (text or "").splitlines():
        body, _, note = line.partition("#")
        for item in body.split(","):
            if item.strip():
                out.append((item.strip(), note.strip()))
    return out


def parse_channel(text: str, note: str = "") -> ChannelRef | None:
    t = text.strip().strip("/")
    if not t:
        return None
    if "youtube.com" in t or "youtu.be" in t:
        path = urllib.parse.urlparse(t if "://" in t else "https://" + t).path
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None
        head = urllib.parse.unquote(parts[0])
        if head.startswith("@"):
            t = head
        elif head == "channel" and len(parts) > 1:
            t = parts[1]
        elif head == "user" and len(parts) > 1:
            return ChannelRef(f"user:{parts[1].lower()}", "username", parts[1], note)
        elif head == "c" and len(parts) > 1:
            t = "@" + urllib.parse.unquote(parts[1])  # old custom URLs mostly match the handle now
        else:
            return None
    if _UC.fullmatch(t):
        return ChannelRef(t, "id", t, note)
    handle = t.lstrip("@")
    if not handle or not re.fullmatch(r"[\w.\-·]+", handle):
        return None
    return ChannelRef("@" + handle.lower(), "handle", handle, note)


def parse_channels(text: str) -> tuple[list[ChannelRef], list[str]]:
    """The channel list -> (channels, entries that couldn't be understood)."""
    refs: list[ChannelRef] = []
    bad: list[str] = []
    for item, note in split_entries(text):
        ref = parse_channel(item, note)
        if ref is None:
            bad.append(item)
        elif all(r.key != ref.key for r in refs):
            refs.append(ref)
    return refs, bad


def parse_searches(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        q = " ".join(line.partition("#")[0].split())
        if q and q.lower() not in (x.lower() for x in out):
            out.append(q)
    return out


# ---------------------------------------------------------------- YouTube Data API calls


def resolve_channel(api_key: str, ref: ChannelRef, get: Callable[[str], dict] = _get) -> dict:
    """{id, title, uploads} for a channel list entry; 1 quota unit."""
    params = {"part": "snippet,contentDetails", "key": api_key.strip()}
    params[{"id": "id", "handle": "forHandle", "username": "forUsername"}[ref.kind]] = (
        "@" + ref.value if ref.kind == "handle" else ref.value)
    data = get(f"{API_URL}/channels?{urllib.parse.urlencode(params)}")
    items = data.get("items") or []
    if not items:
        raise ChartUnavailable(f"No YouTube channel {ref.label}.")
    item = items[0]
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    if not uploads and str(item.get("id", "")).startswith("UC"):
        uploads = "UU" + item["id"][2:]  # every channel's uploads playlist id
    return {"id": item.get("id", ""), "title": (item.get("snippet") or {}).get("title") or ref.label,
            "uploads": uploads}


def latest_uploads(api_key: str, playlist: str, limit: int = CHANNEL_VIDEOS,
                   get: Callable[[str], dict] = _get) -> list[str]:
    """Newest video ids in an uploads playlist; 1 quota unit."""
    params = {"part": "contentDetails", "playlistId": playlist, "maxResults": str(max(1, min(50, limit))),
              "key": api_key.strip()}
    data = get(f"{API_URL}/playlistItems?{urllib.parse.urlencode(params)}")
    ids = [((it.get("contentDetails") or {}).get("videoId") or "") for it in data.get("items") or []]
    return [i for i in ids if i][:limit]


def search_cc(api_key: str, query: str, published_after: float, limit: int = SEARCH_VIDEOS,
              get: Callable[[str], dict] = _get, language: str = "") -> list[str]:
    """Creative Commons video ids matching a search, most viewed first; 100 quota units."""
    from datetime import datetime, timezone

    after = datetime.fromtimestamp(published_after, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"part": "id", "type": "video", "videoLicense": "creativeCommon", "q": query, "order": "viewCount",
              "publishedAfter": after, "maxResults": str(max(1, min(50, limit))), "key": api_key.strip()}
    if language:
        params["relevanceLanguage"] = language  # favours that language; the picking rules still check it
    data = get(f"{API_URL}/search?{urllib.parse.urlencode(params)}")
    ids = [((it.get("id") or {}).get("videoId") or "") for it in data.get("items") or []]
    return [i for i in ids if i][:limit]


def fetch_videos(api_key: str, ids: list[str], get: Callable[[str], dict] = _get) -> list[TrendingVideo]:
    """Full details for videos by id; 1 quota unit per 50."""
    out: list[TrendingVideo] = []
    for i in range(0, len(ids), 50):
        params = {"part": VIDEO_PARTS, "id": ",".join(ids[i:i + 50]), "key": api_key.strip()}
        data = get(f"{API_URL}/videos?{urllib.parse.urlencode(params)}")
        out += [video_from_api(item) for item in data.get("items") or [] if item.get("id")]
    return out


# ---------------------------------------------------------------- one gather per scan


@dataclass
class Gathered:
    videos: list[TrendingVideo] = field(default_factory=list)
    quota: int = 0
    problems: list[str] = field(default_factory=list)
    watch_ids: set[str] = field(default_factory=set)  # channel ids on the channel list
    notes: dict[str, str] = field(default_factory=dict)  # channel id -> note from the channel list


def gather(api_key: str, channels: list[ChannelRef], searches: list[str], history: History, now: float,
           log: Log | None = None, get: Callable[[str], dict] = _get, language: str = "") -> Gathered:
    """Videos from the channel list and Creative Commons searches, each tagged in .origins.

    A bad key or a used-up quota fails every call the same way, so that is raised; anything
    else (one channel that doesn't exist, one search that fails) is reported in .problems."""
    log = log or (lambda m: None)
    g = Gathered()
    found: dict[str, set[str]] = {}  # video id -> origins
    oldest = now - MAX_AGE_DAYS * 86400

    with history.lock:
        known_channels = history.sources["channels"]
        known_searches = history.sources["searches"]
        # Forget sources no longer in the settings so research.json doesn't grow forever.
        for key in [k for k in known_channels if k not in {r.key for r in channels}]:
            del known_channels[key]
        for key in [k for k in known_searches if k not in {q.lower() for q in searches}]:
            del known_searches[key]

    for ref in channels:
        with history.lock:
            rec = dict(known_channels.get(ref.key) or {})
        try:
            if not rec.get("id") and now - rec.get("tried", 0) >= RESOLVE_RETRY:
                rec.pop("error", None)
                g.quota += 1
                rec |= resolve_channel(api_key, ref, get) | {"tried": now}
            if rec.get("uploads") and now - rec.get("checked", 0) >= CHANNEL_EVERY:
                g.quota += 1
                rec["videos"] = latest_uploads(api_key, rec["uploads"], CHANNEL_VIDEOS, get)
                rec["checked"] = now
        except ResearchBlocked:
            raise
        except ResearchError as exc:
            rec |= {"error": str(exc), "tried": now}
        if rec.get("error") and not rec.get("id"):
            g.problems.append(f"Channel list: {rec['error']}")
        if rec.get("id"):
            g.watch_ids.add(rec["id"])
            if ref.note:
                g.notes[rec["id"]] = ref.note
        for vid in rec.get("videos") or []:
            found.setdefault(vid, set()).add("watchlist")
        with history.lock:
            known_channels[ref.key] = rec

    for query in searches:
        key = query.lower()
        with history.lock:
            rec = dict(known_searches.get(key) or {})
        if now - rec.get("checked", 0) >= SEARCH_EVERY:
            try:
                g.quota += 100
                rec = {"videos": search_cc(api_key, query, oldest, SEARCH_VIDEOS, get, language), "checked": now}
                log(f"Searched Creative Commons videos for \"{query}\" (100 quota units).")
            except ResearchBlocked:
                raise
            except ResearchError as exc:
                g.problems.append(f"Creative Commons search \"{query}\": {exc}")
                rec["checked"] = now  # don't spend another 100 units on it until it's due again
        for vid in rec.get("videos") or []:
            found.setdefault(vid, set()).add("cc")
        with history.lock:
            known_searches[key] = rec

    if not found:
        return g
    ids = list(found)
    g.quota += math.ceil(len(ids) / 50)
    for v in fetch_videos(api_key, ids, get):
        if v.published and v.published < oldest:
            continue
        v.origins = sorted(found.get(v.id, set()))
        g.videos.append(v)
    return g
