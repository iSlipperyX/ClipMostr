"""Health check: is everything automation needs in place? Each check says what's wrong and the fix.

check() looks at the PC and the settings without going online: ffmpeg, the downloader's age, the
keys, the folders and free space, TikTok posting, niches and clip requests. With online=True it
also asks the YouTube Data API, Anthropic and Google whether the keys work (no quota or tokens used
for the AI keys; one quota unit for YouTube).

Automation runs the offline check each time it starts and logs what's wrong; the Automation
tab's Check button and `ytclip doctor` run it in full.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import niches, youtube_login
from .cleanup import size_text
from .settings import Settings

GOOD, WARN, BAD = "good", "warn", "bad"
YTDLP_OLD_DAYS = 60  # YouTube changes often; a downloader this old starts to fail
GB = 1024 ** 3


@dataclass
class Check:
    name: str
    level: str  # good | warn | bad
    detail: str
    fix: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def free_space(path: Path) -> tuple[int, Path] | None:
    """(bytes free, the folder measured) on the drive that holds `path`, or None if none of it exists."""
    for p in [path, *path.parents]:
        try:
            if p.exists():
                return shutil.disk_usage(p).free, p
        except OSError:
            continue
    return None


def ytdlp_age_days(version: str, now: float) -> float | None:
    """How old a yt-dlp version (2026.10.26 or 2026.10.26.123456) is, in days."""
    try:
        y, m, d = (int(x) for x in version.split(".")[:3])
        return (now - datetime(y, m, d).timestamp()) / 86400
    except (ValueError, TypeError):
        return None


def _ffmpeg() -> Check:
    from .ffmpeg import NO_WINDOW, ffmpeg_exe

    try:
        exe = ffmpeg_exe()
        out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW)
        first = (out.stdout.splitlines() or ["ffmpeg"])[0]
        return Check("ffmpeg", GOOD, first.split(" Copyright")[0])
    except Exception as exc:
        return Check("ffmpeg", BAD, f"Not working: {exc}", "Reinstall ClipMonster, or install ffmpeg "
                     "(winget install Gyan.FFmpeg)")


def _downloader(now: float) -> Check:
    try:
        from yt_dlp.version import __version__ as version
    except Exception as exc:
        return Check("Downloader (yt-dlp)", BAD, f"Missing: {exc}", "Reinstall ClipMonster")
    age = ytdlp_age_days(version, now)
    if age is not None and age > YTDLP_OLD_DAYS:
        return Check("Downloader (yt-dlp)", WARN, f"Version {version} is {age:.0f} days old; YouTube downloads may "
                     "start failing", "Update ClipMonster (each release brings the newest downloader)")
    return Check("Downloader (yt-dlp)", GOOD, f"Version {version}")


def _keys(s: Settings, online: bool) -> list[Check]:
    from .pipeline import has_ai_key
    from .research import youtube_key

    out = []
    yt = youtube_key(s)
    if not yt:
        out.append(Check("YouTube Data API key", WARN, "Not set, so automation picks no videos by itself",
                         "Add a free key in Settings → Research, or queue links and requests with links"))
    elif online:
        try:
            from .trending import fetch_chart

            fetch_chart(yt, "US", limit=1)
            out.append(Check("YouTube Data API key", GOOD, "Works"))
        except Exception as exc:
            out.append(Check("YouTube Data API key", BAD, f"Didn't work: {exc}", "Check the key in Settings → "
                             "Research (a new key can take a few minutes to start working)"))
    else:
        out.append(Check("YouTube Data API key", GOOD, "Set"))
    from . import ai

    special = any(n.instructions.strip() for n in niches.active(s))
    first = ai.chosen(s)
    if not has_ai_key(s):
        out.append(Check(f"{ai.name(first)} key", WARN if (special or s.auto_polish) else GOOD,
                         "Not set: no AI polish" + (", and special requests aren't followed" if special else ""),
                         "Add a key in Settings → AI" if (special or s.auto_polish) else ""))
        return out
    use = ai.order(s)
    if use[0] != first:
        out.append(Check(f"{ai.name(first)} key", WARN, f"Not set, so {ai.name(use[0])} is used instead",
                         f"Add a {ai.name(first)} key in Settings → AI, or choose {ai.name(use[0])} there"))
    for provider in use:
        label = (ai.name(provider) if provider == "local" else f"{ai.name(provider)} key") + \
            (" (if the other fails)" if provider != use[0] else "")
        if not online:
            out.append(Check(label, GOOD, "Set"))
            continue
        try:
            out.append(Check(label, GOOD, ai.check_key(s, provider)))
        except Exception as exc:
            out.append(Check(label, BAD, f"Didn't work: {exc}", "Check the key in Settings → AI"))
    return out


def _folders(s: Settings) -> list[Check]:
    out = []
    for label, path in (("Automation folder", s.auto_output_path), ("Download cache", s.cache_path)):
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not probe.exists():
            out.append(Check(label, BAD, f"{path}: its drive isn't there", "Pick another folder in Settings"))
            continue
        try:  # a folder that isn't there yet is made on first use: its nearest parent must be writable
            test = probe / f".clipmonster-check-{os.getpid()}"  # a folder: a drive's root may refuse files
            test.mkdir()
            test.rmdir()
            out.append(Check(label, GOOD, str(path) + ("" if path.exists() else " (made on first use)")))
        except OSError as exc:
            out.append(Check(label, BAD, f"Can't write to {path}: {exc.strerror or exc}",
                             "Pick a folder you can write to (Settings → Automation / Download)"))
    seen: set[str] = set()
    for path in (s.auto_output_path, s.cache_path):
        got = free_space(path)
        if not got:
            continue
        free, where = got
        drive = where.anchor or str(where)
        if drive in seen:
            continue
        seen.add(drive)
        limit = s.disk_min_free_gb * GB
        if limit and free < limit:
            out.append(Check(f"Free space on {drive}", BAD, f"{size_text(free)} free, under your {s.disk_min_free_gb} GB "
                             "limit, so automation is paused", "Run Clean up, or lower Settings → Automation → Keep "
                             "this much disk free"))
        elif free < 2 * max(limit, 10 * GB):
            out.append(Check(f"Free space on {drive}", WARN, f"{size_text(free)} free", "Clean up soon: each video "
                             "downloads at up to a few GB"))
        else:
            out.append(Check(f"Free space on {drive}", GOOD, f"{size_text(free)} free"))
    return out


def _posting(s: Settings) -> list[Check]:
    """TikTok posting: accounts logged in, every niche with an account, a browser to post with."""
    from .posting import config as posting_config
    from .posting.accounts import Accounts
    from .posting.tiktok.login import find_browser

    try:
        accounts = list(Accounts.load())
    except Exception as exc:  # noqa: BLE001
        return [Check("TikTok posting", WARN, f"The account list couldn't be read: {exc}", "Try again in a minute")]
    if not accounts:
        return [Check("TikTok posting", WARN, "No TikTok account yet, so clips aren't posted",
                      "Add one in the Accounts tab and log in to TikTok")]
    out = []
    out_of = [a.label for a in accounts if not a.logged_in]
    if out_of:
        out.append(Check("TikTok login", WARN, f"Not logged in: {', '.join(out_of)}. Nothing posts to "
                         f"{'it' if len(out_of) == 1 else 'them'}", "Log in on the Accounts tab"))
    failing = [a.label for a in accounts if a.logged_in and a.upload_check == "fail"]
    if failing:
        out.append(Check("TikTok upload page", WARN, f"The last posting test failed for {', '.join(failing)}",
                         "Press Test posting on the Accounts tab to see which step"))
    ids = {a.id for a in accounts}
    loose = [n.name for n in niches.load(s) if n.enabled and n.account_id not in ids]
    if loose and len(accounts) > 1:
        out.append(Check("Niche accounts", WARN, f"No account picked for {', '.join(loose)}: their clips wait",
                         "Pick the account each niche posts to in the Niches tab"))
    ps = posting_config.Settings.load()
    if find_browser(ps.browser_channel) is None and find_browser("chrome" if ps.browser_channel == "msedge"
                                                                 else "msedge") is None and os.name == "nt":
        out.append(Check("Posting browser", BAD, "Neither Microsoft Edge nor Google Chrome was found",
                         "Install Edge or Chrome: posting uses it, with each account's own login"))
    if not out:
        n = len(accounts)
        out.append(Check("TikTok posting", GOOD, f"{n} account{'s' * (n != 1)} logged in; clips post by themselves"))
    return out


def _niches_and_requests(s: Settings, db) -> list[Check]:
    from .research import youtube_key

    out = []
    mine = niches.load(s)
    for n in mine:
        if not n.on:
            continue
        refs, bad = n.channel_refs()
        if not refs and not n.search_list() and not n.keyword_list() and not n.category_ids():
            out.append(Check(f"Niche {n.name}", WARN, "Has nothing to find videos with",
                             "Add a channel or a Creative Commons search in the Niches tab"))
        elif bad:
            out.append(Check(f"Niche {n.name}", WARN, f"{len(bad)} channel line(s) can't be read", "Fix them in the "
                             "Niches tab"))
    paused = [n.name for n in mine if n.paused]
    if paused:
        out.append(Check("Paused niches", WARN, ", ".join(paused), "Resume them in Automation → Niches when ready"))
    if db is not None:
        try:
            waiting = [o for o in db.orders(open_only=True) if o.picks]
        except Exception:
            waiting = []
        if waiting and not youtube_key(s):
            out.append(Check("Clip requests", BAD, f"{len(waiting)} request(s) wait for picked videos, but there's no "
                             "YouTube Data API key to pick them", "Add a key (Settings → Research) or give the "
                             "requests links"))
    return out


def in_synced_folder(folder: Path) -> bool:
    """Whether `folder` is inside OneDrive (where Windows puts Desktop and Documents when backup is on)."""
    roots = [os.environ.get(k, "") for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    text = os.path.normcase(str(folder))
    if any(r and (text + os.sep).startswith(os.path.normcase(r.rstrip("\\/")) + os.sep) for r in roots):
        return True
    return any(part.lower() == "onedrive" or part.lower().startswith("onedrive - ") for part in folder.parts)


def _install_place(folder: Path | None) -> list[Check]:
    """The app inside OneDrive: syncing its program files slows updates, and an app that changes its
    own files inside a synced folder is one of the things Windows Defender's behaviour check flags."""
    if folder is None or not in_synced_folder(folder):
        return []
    return [Check("App folder", WARN, f"ClipMonster is installed inside OneDrive ({folder})",
                  f"Close ClipMonster and move the whole ClipMonster folder to {Path.home() / 'ClipMonster'}, "
                  "then open it from there (and pin it again)")]


def check(s: Settings, db=None, online: bool = False, now: float | None = None) -> list[Check]:
    from .updater import install_dir

    now = time.time() if now is None else now
    checks = [_ffmpeg(), _downloader(now)]
    checks += _install_place(install_dir())
    checks += _keys(s, online)
    checks += _folders(s)
    checks += _posting(s)
    checks += _niches_and_requests(s, db)
    checks += _youtube_sign_in(s, db, now)
    checks += _whisper_speed(s)
    checks += _pro_edit(s)
    return checks


def _pro_edit(s: Settings) -> list[Check]:
    """The pro edit's camera moves, cuts and B-roll need OpenCV; music and B-roll need their folders."""
    from .rendering import plan
    from . import vision

    out: list[Check] = []
    if plan.picture_changes(s) and not vision.available():
        out.append(Check("Pro edit", WARN, "OpenCV isn't installed, so clips are made without camera moves, cuts "
                         "or B-roll", "Update ClipMonster, or install it with: pip install opencv-python-headless"))
    for field, label in (("music_folder", "Music folder"), ("broll_folder", "B-roll folder")):
        folder = str(getattr(s, field, "") or "").strip()
        if folder and not Path(folder).expanduser().is_dir():
            out.append(Check(label, WARN, f"{folder} isn't there, so clips have no "
                             f"{'music' if field == 'music_folder' else 'B-roll'}", "Pick it again in Settings → Editing"))
    return out


def _whisper_speed(s: Settings) -> list[Check]:
    from . import speed, whisper_cpp

    if s.transcribe == "never" or not speed.use_gpu(s):
        return []
    why = whisper_cpp.unavailable()
    if why:
        return [Check("Whisper on the graphics card", WARN, f"Full power uses the CPU for Whisper: {why}",
                      "Settings → Speed → Try the graphics card again (after a driver update)")]
    return [Check("Whisper on the graphics card", GOOD, "Full power types out speech on the graphics card")]


def _youtube_sign_in(s: Settings, db, now: float) -> list[Check]:
    fix = "Automation → Sign in to YouTube (opens Firefox; use a spare Google account)"
    if s.cookies_browser == youtube_login.CHOICE:
        got = youtube_login.read_sign_in(now=now)
        if not got.signed_in:
            return [Check("YouTube sign-in", WARN, "ClipMonster's Firefox isn't signed in to YouTube, so downloads "
                                                   "aren't signed in", fix)]
        until = f" until {datetime.fromtimestamp(got.expires):%d %b %Y}" if got.expires else ""
        return [Check("YouTube sign-in", GOOD, f"Downloads are signed in with ClipMonster's Firefox{until}")]
    if not s.cookies_browser and db is not None and now - db.meta_float("blocked_at") < 7 * 86400:
        return [Check("YouTube sign-in", WARN, "YouTube asked this PC to sign in this week", fix)]
    return []


def problems(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.level != GOOD]
