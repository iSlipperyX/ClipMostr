"""Video sources: YouTube (and anything else yt-dlp supports) or local files."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .ffmpeg import ffmpeg_exe, probe

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


def _base_opts(log: Log | None, cookies_from_browser: str | None) -> dict:
    opts: dict = {
        "logger": _Logger(log),
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_exe(),
    }
    # YouTube needs a JavaScript runtime to solve its player challenges.
    # yt-dlp only enables Deno by default, so also allow Node/Bun if installed.
    runtimes = {name: {} for name in ("deno", "node", "bun") if shutil.which(name)}
    if runtimes:
        opts["js_runtimes"] = runtimes
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
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
    """Prefer human-written English, then the original-language auto captions."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for lang in manual:
        if lang.split("-")[0] == "en" and lang != "live_chat":
            return lang, False
    for lang in auto:
        if lang.endswith("-orig"):
            return lang, True
    for lang in ("en", "en-US", "en-GB"):
        if lang in auto:
            return lang, True
    for lang in manual:
        if lang != "live_chat":
            return lang, False
    return None


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
    )
    progress("Fetching captions", None)
    video.subtitles = _download_captions(info, cache_dir, cookies, log)
    (cache_dir / f"{video.id}.video.json").write_text(json.dumps(video.to_json(), indent=1), encoding="utf-8")
    return video


def _download_captions(info: dict, cache_dir: Path, cookies: str | None, log: Log | None) -> Path | None:
    """Captions are optional: they improve clip naming, captions and reaction detection."""
    import yt_dlp

    choice = _pick_caption_lang(info)
    if not choice:
        return None
    lang, is_auto = choice
    target = cache_dir / f"{info['id']}.{lang}.vtt"
    if target.exists():
        return target
    opts = _base_opts(None, cookies) | {
        "skip_download": True,
        "writesubtitles": not is_auto,
        "writeautomaticsub": is_auto,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": str(cache_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(info.get("webpage_url") or info["id"], download=True)
    except Exception as exc:
        if log:
            log(f"Captions unavailable ({exc.__class__.__name__}); continuing without them.")
        return None
    return target if target.exists() else None


def local_video(path: Path) -> Video:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    st = path.stat()
    vid = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:11]
    info = probe(path)
    subs = next((p for p in (path.with_suffix(".vtt"), path.with_suffix(".en.vtt")) if p.exists()), None)
    return Video(id=vid, title=path.stem, channel="Local files", upload_date="", duration=info.duration,
                 url=str(path), path=path, subtitles=subs, is_local=True)
