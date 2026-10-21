"""Find what's popular right now: YouTube's "most popular" charts per region.

* fetch_chart() - one region's chart from the YouTube Data API (videos.list, chart=mostPopular).
  Needs a free API key; each page of up to 50 videos costs 1 unit of the 10,000-unit daily quota.
* deep_check()  - yt-dlp metadata for one video, no download: the "Most replayed" heatmap,
  chapters and caption tracks. Slower (a few seconds) so it only runs on the best candidates.
* History       - what earlier scans saw, so views can be compared between checks
  (real momentum instead of lifetime averages) and new arrivals can be flagged.
"""

from __future__ import annotations

import functools
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__

Log = Callable[[str], None]

API_URL = os.environ.get("CLIPMONSTER_YOUTUBE_API", "https://www.googleapis.com/youtube/v3").rstrip("/")
KEY_HELP_URL = "https://console.cloud.google.com/apis/library/youtube.googleapis.com"

REGIONS: dict[str, str] = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia", "IE": "Ireland",
    "NZ": "New Zealand", "IN": "India", "PH": "Philippines", "ZA": "South Africa", "NG": "Nigeria",
    "DE": "Germany", "FR": "France", "ES": "Spain", "IT": "Italy", "NL": "Netherlands", "SE": "Sweden",
    "PL": "Poland", "TR": "Turkey", "BR": "Brazil", "MX": "Mexico", "AR": "Argentina", "CO": "Colombia",
    "JP": "Japan", "KR": "South Korea", "ID": "Indonesia", "VN": "Vietnam", "TH": "Thailand",
    "SA": "Saudi Arabia", "AE": "United Arab Emirates", "EG": "Egypt",
}

# YouTube's video categories (the ids are global). Not every category has a chart in
# every region; the API answers those with "notFound" and the scan skips them.
CATEGORIES: dict[str, str] = {
    "": "All categories", "20": "Gaming", "24": "Entertainment", "23": "Comedy", "22": "People & Blogs",
    "17": "Sports", "25": "News & Politics", "28": "Science & Technology", "27": "Education",
    "26": "Howto & Style", "1": "Film & Animation", "10": "Music", "2": "Autos & Vehicles",
    "15": "Pets & Animals", "19": "Travel & Events", "29": "Nonprofits & Activism",
}


class ResearchError(RuntimeError):
    """A plain-English reason a chart couldn't be fetched."""


class ChartUnavailable(ResearchError):
    """This region has no chart for the chosen category (not worth stopping the scan for)."""


def parse_regions(text: str) -> list[str]:
    """"us, gb ca" -> ["US", "GB", "CA"]: two-letter codes, de-duplicated, order kept."""
    out: list[str] = []
    for code in re.split(r"[\s,;/]+", text.upper()):
        if re.fullmatch(r"[A-Z]{2}", code) and code not in out:
            out.append(code)
    return out


def parse_duration(iso: str) -> float:
    """ISO 8601 durations as the API sends them ("PT1H2M3S", "P1DT2H") -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", iso or "")
    if not m:
        return 0.0
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)


def _timestamp(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- results


@dataclass
class DeepInfo:
    """What yt-dlp knows about a video beyond the API: where viewers rewatch, and structure."""

    heatmap: list[dict] = field(default_factory=list)  # [{start_time, end_time, value}]
    chapters: int = 0
    captions: bool = False  # any usable caption track (manual or automatic)
    likes: int | None = None
    checked: float = 0.0
    error: str = ""


@dataclass
class TrendingVideo:
    id: str
    title: str
    channel: str
    published: float  # unix time
    duration: float  # seconds
    views: int
    likes: int | None = None  # None when the channel hides them
    comments: int | None = None  # None when comments are off
    category: str = ""
    captions: bool = False  # uploaded (not automatic) captions, per the API
    live: str = "none"  # none | live | upcoming
    thumbnail: str = ""
    channel_id: str = ""
    regions: dict[str, int] = field(default_factory=dict)  # region -> rank on its chart (1 = top)
    # Filled in by the scan:
    velocity: float = 0.0  # views per hour
    recent: bool = False  # velocity measured between two checks rather than since upload
    new: bool = False  # first seen in this scan
    first_seen: float = 0.0
    deep: DeepInfo | None = None
    score: float = 0.0  # 0-100 clip-ability
    parts: dict[str, float] = field(default_factory=dict)  # each 0-1, see clipability.WEIGHTS
    reasons: list[str] = field(default_factory=list)
    moments: list[float] = field(default_factory=list)  # most-replayed peaks, seconds
    sent: bool = False  # already sent to the Studio

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def best_rank(self) -> int:
        return min(self.regions.values(), default=0)

    def age_hours(self, now: float | None = None) -> float:
        return max(0.0, ((now or time.time()) - self.published) / 3600) if self.published else 0.0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "TrendingVideo":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        deep = d.get("deep")
        if isinstance(deep, dict):
            d["deep"] = DeepInfo(**{k: v for k, v in deep.items() if k in DeepInfo.__dataclass_fields__})
        return cls(**d)


def video_from_api(item: dict) -> TrendingVideo:
    sn, st, cd = item.get("snippet") or {}, item.get("statistics") or {}, item.get("contentDetails") or {}
    thumbs = sn.get("thumbnails") or {}
    thumb = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default")
                  if k in thumbs and thumbs[k].get("url")), "")
    return TrendingVideo(
        id=item["id"],
        title=sn.get("title") or item["id"],
        channel=sn.get("channelTitle") or "",
        channel_id=sn.get("channelId") or "",
        published=_timestamp(sn.get("publishedAt") or ""),
        duration=parse_duration(cd.get("duration") or ""),
        views=_int(st.get("viewCount")) or 0,
        likes=_int(st.get("likeCount")),
        comments=_int(st.get("commentCount")),
        category=str(sn.get("categoryId") or ""),
        captions=str(cd.get("caption")).lower() == "true",
        live=sn.get("liveBroadcastContent") or "none",
        thumbnail=thumb,
    )


# ---------------------------------------------------------------- YouTube Data API


def _friendly(status: int, body: bytes) -> ResearchError:
    try:
        err = json.loads(body.decode("utf-8", "replace")).get("error", {})
    except ValueError:
        err = {}
    reasons = {e.get("reason") for e in err.get("errors", []) if isinstance(e, dict)}
    message = err.get("message") or f"HTTP {status}"
    key_rejected = "keyInvalid" in reasons or "API_KEY_INVALID" in str(err) or (
        status == 400 and "api key" in message.lower())
    if key_rejected:
        return ResearchError("The YouTube API key was rejected. Check it was copied completely.")
    if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
        return ResearchError("Today's YouTube API quota is used up. It resets at midnight Pacific time; "
                             "check fewer regions or less often until then.")
    if reasons & {"accessNotConfigured"} or "has not been used" in message or "is disabled" in message:
        return ResearchError("YouTube Data API v3 isn't enabled for this key's Google Cloud project. "
                             "Enable it in the Google Cloud console, then try again.")
    if reasons & {"keyExpired"}:
        return ResearchError("The YouTube API key has expired. Create a new one in the Google Cloud console.")
    if status == 404 or reasons & {"notFound", "videoChartNotFound"}:
        return ChartUnavailable(message)
    if status == 400 and ("regionCode" in message or reasons & {"invalidRegionCode", "unsupportedRegionCode"}):
        return ChartUnavailable(message)
    if status >= 500:
        return ResearchError(f"YouTube's servers are busy (error {status}). The next check will try again.")
    return ResearchError(f"YouTube API error {status}: {message}")


def _get(url: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": f"ClipMonster/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _friendly(exc.code, exc.read() or b"") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise ResearchError("Couldn't reach YouTube. Check your internet connection.") from exc
    except ValueError as exc:
        raise ResearchError("YouTube sent an answer that couldn't be read.") from exc


def fetch_chart(api_key: str, region: str, category: str = "", limit: int = 50,
                get: Callable[[str], dict] = _get) -> list[TrendingVideo]:
    """The region's most popular videos, best first; costs 1 quota unit per 50 videos."""
    if not api_key.strip():
        raise ResearchError("Add a YouTube Data API key (Settings → Research) to check what's trending.")
    videos: list[TrendingVideo] = []
    token = ""
    while len(videos) < limit:
        params = {"part": "snippet,statistics,contentDetails", "chart": "mostPopular", "regionCode": region,
                  "maxResults": str(min(50, limit - len(videos))), "key": api_key.strip()}
        if category:
            params["videoCategoryId"] = category
        if token:
            params["pageToken"] = token
        data = get(f"{API_URL}/videos?{urllib.parse.urlencode(params)}")
        for item in data.get("items") or []:
            if not item.get("id"):
                continue
            v = video_from_api(item)
            v.regions[region] = len(videos) + 1
            videos.append(v)
        token = data.get("nextPageToken") or ""
        if not token or not data.get("items"):
            break
    return videos


# ---------------------------------------------------------------- yt-dlp deep check


def deep_check(url: str, cookies: str | None = None, log: Log | None = None) -> DeepInfo:
    """Metadata only (nothing is downloaded): heatmap, chapters, caption tracks."""
    import yt_dlp

    from .download import _base_opts

    opts = _base_opts(None, cookies) | {"skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # yt-dlp raises many types; a failed check just means "no extra data"
        msg = str(exc).replace("ERROR: ", "").strip() or exc.__class__.__name__
        if log:
            log(f"Couldn't check replays for {url}: {msg}")
        return DeepInfo(checked=time.time(), error=msg)
    return deep_from_info(info)


def deep_from_info(info: dict) -> DeepInfo:
    subs = {k for k in (info.get("subtitles") or {}) if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    return DeepInfo(
        heatmap=[{"start_time": float(h["start_time"]), "end_time": float(h["end_time"]),
                  "value": float(h["value"])}
                 for h in info.get("heatmap") or [] if {"start_time", "end_time", "value"} <= h.keys()],
        chapters=len(info.get("chapters") or []),
        captions=bool(subs or auto),
        likes=_int(info.get("like_count")),
        checked=time.time(),
    )


# ---------------------------------------------------------------- history

SNAPSHOTS = 16  # view counts kept per video
FORGET_AFTER = 7 * 86400  # drop videos not seen on any chart for a week
DEEP_FRESH = 6 * 3600  # re-check the heatmap after this long
MIN_GAP = 15 * 60  # two snapshots closer than this are too noisy to compare


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


class History:
    """research.json: view snapshots, cached deep checks, the last results and what was sent."""

    def __init__(self, path: Path | None):
        self.path = path
        self.videos: dict[str, dict] = {}
        self.last_scan: dict = {}
        self.lock = threading.RLock()  # a scan thread and the GUI both touch it
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.videos = data.get("videos") or {}
                self.last_scan = data.get("last_scan") or {}
            except (OSError, ValueError, AttributeError):
                pass

    @_locked
    def record(self, v: TrendingVideo, now: float) -> None:
        """Add this sighting, then fill v.velocity / v.recent / v.new / v.first_seen / v.sent."""
        rec = self.videos.setdefault(v.id, {"first_seen": now, "snapshots": []})
        snaps: list[list[float]] = rec["snapshots"]
        # Momentum over roughly the last day: compare with the oldest snapshot inside it
        # that is far enough back to be meaningful.
        base = next((s for s in snaps if now - 86400 <= s[0] <= now - MIN_GAP and s[1] <= v.views), None)
        if base:
            v.velocity, v.recent = (v.views - base[1]) / ((now - base[0]) / 3600), True
        else:
            v.velocity, v.recent = v.views / max(1.0, v.age_hours(now)), False
        v.new = not snaps
        v.first_seen = rec["first_seen"]
        v.sent = bool(rec.get("sent"))
        if not snaps or now - snaps[-1][0] >= 60:
            snaps.append([now, v.views])
            del snaps[:-SNAPSHOTS]
        rec["last_seen"] = now
        rec["title"] = v.title

    @_locked
    def cached_deep(self, video_id: str, now: float) -> DeepInfo | None:
        d = (self.videos.get(video_id) or {}).get("deep")
        if not d or now - d.get("checked", 0) > DEEP_FRESH or d.get("error"):
            return None
        return DeepInfo(**{k: v for k, v in d.items() if k in DeepInfo.__dataclass_fields__})

    @_locked
    def store_deep(self, video_id: str, deep: DeepInfo) -> None:
        if video_id in self.videos:
            self.videos[video_id]["deep"] = asdict(deep)

    @_locked
    def mark_sent(self, video_id: str) -> None:
        self.videos.setdefault(video_id, {"first_seen": time.time(), "snapshots": []})["sent"] = True
        for v in self.last_scan.get("videos", []):
            if v.get("id") == video_id:
                v["sent"] = True

    @_locked
    def remember(self, videos: list[TrendingVideo], meta: dict, now: float) -> None:
        self.last_scan = {"time": now, **meta, "videos": [v.to_json() for v in videos]}
        for vid in [k for k, r in self.videos.items() if now - r.get("last_seen", r.get("first_seen", 0)) > FORGET_AFTER]:
            del self.videos[vid]

    @_locked
    def last_videos(self) -> list[TrendingVideo]:
        out = []
        for d in self.last_scan.get("videos") or []:
            try:
                out.append(TrendingVideo.from_json(d))
            except (TypeError, KeyError):
                continue
        return out

    @_locked
    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"videos": self.videos, "last_scan": self.last_scan}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
