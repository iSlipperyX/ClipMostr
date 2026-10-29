"""Video sources: YouTube (and anything else yt-dlp supports) or local files."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .ffmpeg import ffmpeg_exe, probe
from .youtube_login import cookie_option

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]  # (stage message, 0-1 or None)

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts"}


@dataclass
class Video:
    id: str
    title: str
    channel: str
    upload_date: str  # YYYYMMDD or ""
    duration: float
    url: str
    path: Path
    heatmap: list[dict] = field(default_factory=list)
    chapters: list[dict] = field(default_factory=list)
    subtitles: Path | None = None
    is_local: bool = False
    language: str = ""  # two-letter code from the platform, if known
    license: str = ""  # as the platform words it, e.g. "Creative Commons Attribution license (reuse allowed)"

    def to_json(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        d["subtitles"] = str(self.subtitles) if self.subtitles else None
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Video":
        d = dict(d)
        d["path"] = Path(d["path"])
        d["subtitles"] = Path(d["subtitles"]) if d.get("subtitles") else None
        return cls(**d)


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://", "www."))


_ID_IN_URL = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/|/v/)([\w-]{11})")


def video_id(url: str) -> str:
    """The YouTube video id in a link, or the link itself when it has none."""
    m = _ID_IN_URL.search(url)
    return m.group(1) if m else url.strip()


def needs_expansion(src: str) -> bool:
    """Playlists, channels and folders turn into many videos; plain videos don't."""
    if is_url(src):
        return any(k in src for k in ("list=", "/@", "/channel/", "/c/", "/playlist", "/user/"))
    return Path(src).is_dir()


class _Logger:
    """Route yt-dlp's chatter to our log callback, keeping only warnings and errors."""

    def __init__(self, log: Log | None):
        self.log = log or (lambda m: None)

    def debug(self, msg: str) -> None:
        pass

    info = debug

    def warning(self, msg: str) -> None:
        self.log(f"⚠ {msg}")

    def error(self, msg: str) -> None:
        self.log(f"✗ {msg}")


# yt-dlp errors that mean YouTube is pushing back on this PC, not on one video.
BLOCKED = re.compile(r"sign in to confirm|not a bot|http error 429|too many requests|rate.?limit", re.I)


def is_blocked(error: str) -> bool:
    return bool(BLOCKED.search(error or ""))


def _base_opts(log: Log | None, cookies_from_browser: str | None) -> dict:
    opts: dict = {
        "logger": _Logger(log),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_exe(),
        # A pause between the requests behind each video page, as a person browsing would make
        # them: bursts of requests are what gets a PC asked to sign in.
        "sleep_interval_requests": 1,
    }
    # YouTube needs a JavaScript runtime to solve its player challenges.
    # yt-dlp only enables Deno by default, so also allow Node/Bun if installed.
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    cookies = cookie_option(cookies_from_browser)
    if cookies:
        opts["cookiesfrombrowser"] = cookies
    return opts


def expand_sources(sources: list[str], limit: int | None = None, cookies: str | None = None,
                   log: Log | None = None) -> list[str]:
    """Turn playlist/channel URLs into video URLs and folders into video files."""
    import yt_dlp

    out: list[str] = []
    opts = _base_opts(log, cookies) | {"extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for src in sources:
            src = src.strip().strip('"')
            if not src:
                continue
            p = Path(src)
            if not is_url(src) and p.is_dir():
                out += sorted(str(f) for f in p.iterdir() if f.suffix.lower() in VIDEO_EXTS)
                continue
            if not is_url(src):
                out.append(src)
                continue
            if not needs_expansion(src):
                out.append(src)  # plain video: skip the extra network round trip
                continue
            info = ydl.extract_info(src, download=False, process=True)
            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries") or [] if e]
                if limit:
                    entries = entries[:limit]
                if log:
                    log(f"Playlist '{info.get('title', src)}': {len(entries)} videos")
                for e in entries:
                    out.append(e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={e['id']}")
            else:
                out.append(src)
    return out


def _pick_caption_lang(info: dict) -> tuple[str, bool] | None:
    """The caption track to use: human-written captions in the video's language or English first,
    then YouTube's own transcript ("<lang>-orig") in the video's language or English.

    A transcript in any other language is a last resort: YouTube sometimes guesses a video's
    language wrong and lists, say, an "ar-orig" track for an English interview."""
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    auto = info.get("automatic_captions") or {}
    spoken = (info.get("language") or "").split("-")[0].lower()
    wanted = [x for x in (spoken, "en") if x]

    def first(tracks, langs, suffix=""):
        for want in langs:
            for lang in tracks:
                if lang.split("-")[0].lower() == want and (not suffix or lang.endswith(suffix)):
                    return lang
        return None

    for found, is_auto in ((first(manual, wanted), False), (first(auto, wanted, "-orig"), True)):
        if found:
            return found, is_auto
    if not spoken:  # language unknown: YouTube's own transcript is the best guess
        orig = next((lang for lang in auto if lang.endswith("-orig")), None)
        if orig:
            return orig, True
    for lang in ("en", "en-US", "en-GB"):  # English translation of the transcript
        if lang in auto:
            return lang, True
    if manual:
        return next(iter(manual)), False
    orig = next((lang for lang in auto if lang.endswith("-orig")), None)
    return (orig, True) if orig else None


def download(url: str, cache_dir: Path, max_height: int, cookies: str | None = None,
             log: Log | None = None, progress: Progress | None = None) -> Video:
    import yt_dlp

    cache_dir.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda m, f: None)

    def hook(d: dict) -> None:
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            mb = f"{done / 1e6:.0f} / {total / 1e6:.0f} MB" if total else f"{done / 1e6:.0f} MB"
            speed = d.get("speed")
            if speed:
                mb += f", {speed / 1e6:.1f} MB/s"
            progress(f"Downloading ({mb})", done / total if total else None)
        elif d["status"] == "finished":
            progress("Merging audio and video", None)

    opts = _base_opts(log, cookies) | {
        "format": "bv*+ba/b",
        # Best quality up to max_height, preferring H.264/AAC in MP4 for compatibility.
        "format_sort": [f"res:{max_height}", "vcodec:h264", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "outtmpl": str(cache_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "progress_hooks": [hook],
    }
    progress("Fetching video info", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(info["requested_downloads"][0]["filepath"])

    video = Video(
        id=info["id"],
        title=info.get("title") or info["id"],
        channel=info.get("channel") or info.get("uploader") or "Unknown channel",
        upload_date=info.get("upload_date") or "",
        duration=float(info.get("duration") or 0) or probe(path).duration,
        url=info.get("webpage_url") or url,
        path=path,
        heatmap=info.get("heatmap") or [],
        chapters=info.get("chapters") or [],
        language=(info.get("language") or "").split("-")[0].lower(),
        license=info.get("license") or "",
    )
    progress("Fetching captions", None)
    video.subtitles = _download_captions(info, cache_dir, cookies, log)
    (cache_dir / f"{video.id}.video.json").write_text(json.dumps(video.to_json(), indent=1), encoding="utf-8")
    return video


def _download_captions(info: dict, cache_dir: Path, cookies: str | None, log: Log | None) -> Path | None:
    """Captions are optional: they improve clip naming, captions and reaction detection."""
    choice = _pick_caption_lang(info)
    if not choice:
        return None
    lang, is_auto = choice
    if log:
        log(f"Captions: {lang} ({'YouTube auto-captions' if is_auto else 'written by the uploader'}).")
    target = cache_dir / f"{info['id']}.{lang}.vtt"
    if target.exists():
        return target
    # The track's address came with the video's info, so this is one request; asking yt-dlp
    # again would load the whole video page a second time.
    tracks = (info.get("automatic_captions") if is_auto else info.get("subtitles")) or {}
    track = next((t for t in tracks.get(lang) or [] if t.get("ext") == "vtt" and t.get("url")), None)
    if not track:
        if log:
            log("Captions unavailable (no WebVTT version); continuing without them.")
        return None
    try:
        data = _fetch(track["url"], cookies)
        if not data.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
            raise ValueError("not a WebVTT file")
        part = target.with_name(target.name + ".part")
        part.write_bytes(data)
        part.replace(target)
    except Exception as exc:
        if is_blocked(str(exc)):
            raise  # YouTube is pushing back on the PC: the whole video waits, see pipeline.classify
        if log:
            log(f"Captions unavailable ({exc.__class__.__name__}: {exc}); continuing without them.")
        return None
    return target


def _fetch(url: str, cookies: str | None, tries: int = 3, sleep: Callable[[float], None] = time.sleep) -> bytes:
    """One small download through yt-dlp (its cookies and headers), tried again after a dropped
    connection or a server error."""
    import yt_dlp
    from yt_dlp.networking.exceptions import HTTPError, TransportError

    for attempt in range(tries):
        try:
            with yt_dlp.YoutubeDL(_base_opts(None, cookies)) as ydl:
                return ydl.urlopen(url).read()
        except HTTPError as exc:
            if not 500 <= exc.status < 600 or attempt == tries - 1:
                raise
        except TransportError:
            if attempt == tries - 1:
                raise
        sleep((2, 5)[min(attempt, 1)])
    raise AssertionError("unreachable")


def local_id(path: Path) -> str:
    """The id a video file on this PC goes by: its cached transcript and analysis are named after it.
    Raises OSError when the file isn't there."""
    path = path.resolve()
    st = path.stat()
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:11]


def local_video(path: Path) -> Video:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    vid = local_id(path)
    info = probe(path)
    subs = next((p for p in (path.with_suffix(".vtt"), path.with_suffix(".en.vtt")) if p.exists()), None)
    return Video(id=vid, title=path.stem, channel="Local files", upload_date="", duration=info.duration,
                 url=str(path), path=path, subtitles=subs, is_local=True)
