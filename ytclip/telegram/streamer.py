"""Getting a video, and news of one being made, to the phone.

**The video.** A bot can send files up to 50 MB. A clip under LIMIT goes as it is (what you watch is what
posts). A bigger one, like a long 1080p Creator video, goes as a mobile preview made to fit: 720p, CRF 24, AAC
at 128 kb/s, which lands a minute of video far under the limit. Should a long or busy video still come out too
big, it's made again at the bitrate that fits (and smaller than 720p when it has to be). The preview is made
once and kept in %LOCALAPPDATA%\\ClipMonster\\telegram for a few days (prune()); ffmpeg makes it at low
priority so clipping doesn't slow down. The master is never touched: it stays in its category's posting
folder on the PC, and it's the master that posts. The upload itself is streamed from the file a piece at a
time (api.Upload), and says how far along it is.

**The news.** A job asked for from the chat gets one message that keeps itself up to date (Live), not a
line per step:

    ⚙️ Working on: "Why Roman Concrete Lasted" [████████░░] 78% (Rendering Video)

It's edited at most every EDIT_EVERY seconds, only when what it says has changed, and it stands back when
Telegram asks for room (a 429), so a long render never floods the chat or trips the bot's limits.
"""

from __future__ import annotations

import html
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..ffmpeg import NO_WINDOW, ffmpeg_exe, probe
from ..settings import local_dir
from .api import ApiError, NetworkError

LIMIT = 48_000_000  # under the Bot API's 50 MB, with room for the form around the file
KEEP_DAYS = 3
PROXY_SIDE = 1280  # a 720p preview: 720 across for an upright video, 1280 down
PROXY_CRF = 24
PROXY_AUDIO_BPS = 128_000
AUDIO_BPS = 96_000  # the fallback copy's, when a preview at CRF 24 is still too big
MAX_VIDEO_BPS = 4_000_000
MIN_VIDEO_BPS = 250_000
TIMEOUT = 20 * 60  # an encode taking longer than this has gone wrong
BELOW_NORMAL = 0x00004000  # Windows' BELOW_NORMAL_PRIORITY_CLASS

EDIT_EVERY = 3.0  # seconds between two edits of a progress message
BAR = 10  # blocks in the bar
LIVE_KEEP = 6 * 3600.0  # a progress message nobody finished is forgotten after this long
BADGE = "📱 Mobile Preview"


@dataclass
class Sendable:
    path: Path
    width: int = 0
    height: int = 0
    duration: float = 0.0
    copy: bool = False  # a smaller copy, not the file itself
    master: tuple[int, int] = (0, 0)  # the master's size, when this is a copy of it

    @property
    def badge(self) -> str:
        """What a preview's caption says about it and its master, '' for the file itself."""
        if not self.copy:
            return ""
        return f"{BADGE} ({master_name(*self.master)} Master Saved on Host)"


class TooBig(RuntimeError):
    pass


def master_name(width: int, height: int) -> str:
    """'1080p' for a 1080x1920 (or 1920x1080) master: the short side, as people say it."""
    short = min(int(width or 0), int(height or 0))
    return f"{short}p" if short else "Full-Quality"


def folder() -> Path:
    return local_dir("telegram")


def plan(duration: float, size_limit: int = LIMIT) -> tuple[int, int]:
    """(video bitrate, the long side in pixels) for a copy of a `duration`-second video under the limit."""
    budget = size_limit * 8 * 0.9 / max(1.0, duration) - AUDIO_BPS  # 10% for the MP4's own bytes
    bps = int(max(MIN_VIDEO_BPS, min(MAX_VIDEO_BPS, budget)))
    side = 1280 if bps >= 1_200_000 else 960 if bps >= 700_000 else 640
    return bps, side


def _scale(side: int) -> str:
    # The long side becomes `side` (never larger than it was), the other follows, kept even.
    return f"scale=w='if(gte(iw,ih),min(iw,{side}),-2)':h='if(gte(iw,ih),-2,min(ih,{side}))'"


def proxy_command(video: Path, out: Path, side: int = PROXY_SIDE, crf: int = PROXY_CRF) -> list[str]:
    """The mobile preview: 720p, CRF 24, AAC 128 kb/s, quick to make and to start playing."""
    return [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-map", "0:v:0",
            "-map", "0:a:0?", "-vf", _scale(side), "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", str(PROXY_AUDIO_BPS), "-ac", "2",
            "-movflags", "+faststart", str(out)]


def command(video: Path, out: Path, bps: int, side: int) -> list[str]:
    """A copy at a set bitrate: what a video too long or too busy for the CRF preview falls back to."""
    return [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-map", "0:v:0",
            "-map", "0:a:0?", "-vf", _scale(side), "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(bps),
            "-maxrate", str(int(bps * 1.4)), "-bufsize", str(bps * 2), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", str(AUDIO_BPS), "-ac", "2", "-movflags", "+faststart", str(out)]


def for_telegram(video: Path, key: str, where: Path | None = None, run=subprocess.run) -> Sendable:
    """What to send for `video` (`key` names its copy, e.g. the library entry's id): the file itself when
    Telegram takes it, else its mobile preview. Raises TooBig or RuntimeError (in words for the chat)."""
    video = Path(video)
    size = video.stat().st_size
    info = probe(video)
    if size <= LIMIT:
        return Sendable(video, info.width, info.height, info.duration)
    master = (info.width, info.height)
    out = (where or folder()) / f"{key}.mp4"
    try:
        if out.stat().st_mtime >= video.stat().st_mtime and 0 < out.stat().st_size <= LIMIT:
            got = probe(out)
            return Sendable(out, got.width, got.height, got.duration, copy=True, master=master)
    except OSError:
        pass
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part.mp4")
    flags = NO_WINDOW | (BELOW_NORMAL if os.name == "nt" else 0)
    # The preview first; then, only if that's still too big, a copy at the bitrate that fits (twice at most).
    limit = LIMIT
    tries = [proxy_command(video, part)]
    for attempt in range(3):
        if attempt:
            bps, side = plan(info.duration, limit)
            tries.append(command(video, part, bps, side))
        done = run(tries[-1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=TIMEOUT,
                   creationflags=flags)
        if done.returncode != 0 or not part.is_file():
            part.unlink(missing_ok=True)
            tail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
            raise RuntimeError("Couldn't make a copy small enough for Telegram" + (f": {tail[0]}" if tail else ""))
        if part.stat().st_size <= LIMIT:
            os.replace(part, out)
            got = probe(out)
            return Sendable(out, got.width, got.height, got.duration, copy=True, master=master)
        part.unlink(missing_ok=True)
        if attempt:
            limit = int(limit * 0.75)  # a busy video came out bigger than planned: once more, smaller
    raise TooBig("Even a small copy of this video is over Telegram's 50 MB. Watch it on the PC.")


def drop(key: str, where: Path | None = None) -> None:
    for f in (where or folder()).glob(f"{key}.*"):
        try:
            f.unlink()
        except OSError:
            pass


def prune(keep: set[str], where: Path | None = None, now: float | None = None) -> int:
    """Remove copies of videos no longer in the library (not in `keep`), and any older than KEEP_DAYS."""
    now = time.time() if now is None else now
    gone = 0
    d = where or folder()
    for f in d.glob("*.mp4") if d.is_dir() else []:
        key = f.name.split(".", 1)[0]
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if ".part" in f.name:
            stale = age > 3600  # a copy being made right now is left alone
        else:
            stale = key not in keep or age > KEEP_DAYS * 86400
        if stale:
            try:
                f.unlink()
                gone += 1
            except OSError:
                pass
    return gone


# --- the one message that keeps itself up to date -----------------------------------------------------------------


def bar(frac: float | None, blocks: int = BAR) -> str:
    """'[████████░░] 78%' ('[░░░░░░░░░░]' with no percent while how far isn't known)."""
    if frac is None:
        return "[" + "░" * blocks + "]"
    frac = max(0.0, min(1.0, float(frac)))
    full = min(blocks, int(frac * blocks + 0.5))  # to the nearest block: 78% is eight of ten
    if frac < 1.0:
        full = min(full, blocks - 1)  # only a finished one is full
    return "[" + "█" * full + "░" * (blocks - full) + f"] {int(frac * 100 + 1e-9)}%"


def _short(text: str, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip(" ,.;:-") + "…"


def stage_text(stage: str) -> str:
    """A step's name as a progress message shows it: without the percent the engine put after it (the bar
    says how far), and short."""
    s = " ".join(str(stage or "").split())
    s = re.sub(r"[\s:·-]*\(?\d{1,3}(?:\.\d)?\s*%\)?\s*$", "", s).rstrip(" .…:·-")
    return _short(s, 60)


def progress_html(title: str, frac: float | None, stage: str = "", icon: str = "⚙️",
                  verb: str = "Working on") -> str:
    """⚙️ Working on: "Why Roman Concrete Lasted" [████████░░] 78% (Rendering Video)"""
    line = f"{icon} {verb}: “{html.escape(_short(title, 80), quote=False)}” {bar(frac)}"
    return line + (f" ({html.escape(_short(stage, 70), quote=False)})" if stage else "")


class Live:
    """The progress messages: one per thing being made and chat, started once, then edited in place.

    `key` names what's being made ("job:create-ab12", "sports:o1f…"). `api()` gives the bot's Api (None while
    the bot is off). `keep(entries)` is told the messages whenever one starts or ends, so they outlive a
    restart (the bot puts them in telegram.json and hands them back to `restore`)."""

    def __init__(self, api: Callable[[], object], now: Callable[[], float] = time.time,
                 keep: Callable[[dict], None] | None = None, log: Callable[[str], None] | None = None,
                 every: float = EDIT_EVERY):
        self.api, self.now, self.keep, self.every = api, now, keep, every
        self.log = log or (lambda m: None)
        self.lock = threading.Lock()
        self.msgs: dict[str, dict] = {}  # "key|chat" -> {key, chat, mid, title, text, at, started}

    @staticmethod
    def _id(key: str, chat: int) -> str:
        return f"{key}|{chat}"

    def restore(self, entries: dict) -> None:
        now = self.now()
        with self.lock:
            for k, e in (entries or {}).items():
                if isinstance(e, dict) and e.get("mid") and now - float(e.get("started") or 0) < LIVE_KEEP:
                    self.msgs[k] = {**e, "at": 0.0, "text": ""}

    def _saved(self) -> None:
        if self.keep is not None:
            with self.lock:
                out = {k: {f: e[f] for f in ("key", "chat", "mid", "title", "started", "icon", "verb")}
                       for k, e in self.msgs.items()}
            try:
                self.keep(out)
            except Exception:  # noqa: BLE001 - remembering it is a nicety
                pass

    def keys(self) -> set[str]:
        with self.lock:
            return {e["key"] for e in self.msgs.values()}

    def chats(self, key: str) -> list[int]:
        with self.lock:
            return [e["chat"] for e in self.msgs.values() if e["key"] == key]

    def start(self, key: str, chat: int, title: str, stage: str = "Waiting its turn", buttons: dict | None = None,
              icon: str = "⚙️", verb: str = "Working on") -> int:
        """Send the progress message for `key` to `chat` (once: a second start is the same message). Returns
        its message id, 0 when it couldn't be sent."""
        ident = self._id(key, chat)
        with self.lock:
            if ident in self.msgs:
                return int(self.msgs[ident]["mid"])
        api = self.api()
        if api is None:
            return 0
        text = progress_html(title, None, stage, icon, verb)
        try:
            sent = api.send(chat, text, buttons, quiet=True)
        except (ApiError, NetworkError) as exc:
            self.log(f"⚠ Couldn't send a progress message: {exc}")
            return 0
        mid = int((sent or {}).get("message_id") or 0)
        if not mid:
            return 0
        now = self.now()
        with self.lock:
            self.msgs[ident] = {"key": key, "chat": chat, "mid": mid, "title": title, "text": text, "at": now,
                                "started": now, "icon": icon, "verb": verb, "buttons": buttons}
        self._saved()
        return mid

    def update(self, key: str, frac: float | None, stage: str = "", buttons: dict | None = None) -> int:
        """Bring `key`'s messages up to date. Each is edited only when what it says changed and EDIT_EVERY
        seconds have passed since its last edit. Returns how many were edited."""
        api = self.api()
        if api is None:
            return 0
        now = self.now()
        with self.lock:
            mine = [(k, dict(e)) for k, e in self.msgs.items() if e["key"] == key]
        edited = 0
        for ident, e in mine:
            text = progress_html(e["title"], frac, stage, e.get("icon") or "⚙️", e.get("verb") or "Working on")
            if text == e.get("text") or now - float(e.get("at") or 0) < self.every:
                continue
            markup = buttons if buttons is not None else e.get("buttons")
            try:
                api.edit(e["chat"], e["mid"], text, markup)
                edited += 1
            except ApiError as exc:
                if exc.gone or exc.code == 403:  # deleted in the chat, or the bot was blocked: no more edits
                    with self.lock:
                        self.msgs.pop(ident, None)
                    self._saved()
                    continue
                if exc.code == 429:  # Telegram wants room: this message waits that long before its next edit
                    with self.lock:
                        if ident in self.msgs:
                            self.msgs[ident]["at"] = now + float(exc.retry_after or 5.0) - self.every
                    continue
            except NetworkError:
                return edited
            with self.lock:
                if ident in self.msgs:
                    self.msgs[ident].update(text=text, at=now)
        return edited

    def finish(self, key: str, text: str = "", buttons: dict | None = None, chat: int | None = None) -> list[int]:
        """End `key`'s progress messages (just `chat`'s, when given): each becomes `text` (HTML), or is taken
        away when there's none (the video itself follows). Returns the chats that had one."""
        api = self.api()
        with self.lock:
            mine = [(k, e) for k, e in self.msgs.items() if e["key"] == key and chat in (None, e["chat"])]
            for k, _e in mine:
                self.msgs.pop(k, None)
        for _k, e in mine:
            if api is None:
                continue
            try:
                if text:
                    api.edit(e["chat"], e["mid"], text, buttons)
                else:
                    api.delete(e["chat"], e["mid"])
            except (ApiError, NetworkError):
                pass
        if mine:
            self._saved()
        return [e["chat"] for _k, e in mine]

    def prune(self) -> None:
        now = self.now()
        with self.lock:
            old = [k for k, e in self.msgs.items() if now - float(e.get("started") or 0) > LIVE_KEEP]
            for k in old:
                self.msgs.pop(k, None)
        if old:
            self._saved()
