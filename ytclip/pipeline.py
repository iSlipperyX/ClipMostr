"""The unattended runner: research → pick → download → analyse → find clips → export, on its own.

Runner.tick() does one step: a research scan when one is due (queuing whatever the picking
rules pick), then at most one queued video. run() repeats it until asked to stop. Everything
it knows lives in jobs.db, so a crash, reboot or kill loses nothing: the video it was on goes
back in the queue, and the download and analysis caches make the second try quick.

It runs as `ytclip auto` in a console, as `ClipMonster.exe --automation` with no window
(what Start with Windows uses), and the Automation tab watches and steers it through jobs.db.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import fresh, learn, lookup, niches, orders, research, steps, youtube_login
from .download import Video, download, is_blocked
from .engine import Cancelled, Engine, Project, timing_text
from .jobs import SIGN_IN_WAIT, Job, JobDB
from .policy import PickLog, Policy, decide, language_codes, picks_path
from .procs import boot_time, process_alive, process_name  # noqa: F401 (tests patch them here)
from .settings import Settings, all_layouts, all_presets, app_dir, find_layout
from .trending import CATEGORIES, History, ResearchBlocked
from .uploads import Uploader

Log = Callable[[str], None]

DAY = 86400.0
BACKOFF = (15 * 60, 3600, 4 * 3600)  # wait before the 2nd, 3rd, 4th... try
FIRST_PAUSE = 2 * 3600  # how long requests to YouTube pause after it asks the PC to sign in
BLOCK_PAUSE = 6 * 3600  # ... and after it asks again within a day of the last time
CALM_DAY = DAY  # after this long with no sign-in request, downloads go back to the usual spacing
DEEP_PAUSE = 20.0  # seconds between automation's most-replayed checks
FIND_MATCH = 60.0  # a standing request's video must match at least this well (0-100) to be picked
FIND_SINCE = "month"  # standing requests look for videos posted in the last month: new ones each run
RULE_WORDS = {"score": "clip-ability under your minimum", "age": "older than your limit",
              "length": "outside your video length", "daily_cap": "today's limit is reached",
              "niche_cap": "the niche's videos a day are reached", "channel_cap": "the channel's weekly limit",
              "channel": "a blocked channel", "category": "a blocked category", "word": "a blocked word",
              "language": "another language", "source": "your picking rules allow Creative Commons only",
              "live": "live or not out yet", "niche": "its niche is off or paused"}
HEARTBEAT_STALE = 180  # a runner silent for this long is gone
PULSE = 5.0  # seconds between heartbeats, whatever the runner is busy with
IDLE = 300  # longest sleep between checks
GB = 1024 ** 3
DISK_RECHECK = 600  # seconds between free-space checks while the disk is too full
DISK_CLEAN_EVERY = 3600  # at most one free-space clean-up an hour
DISK_KINDS = ("partial", "previews", "downloads", "rejected")  # safe to clear when space runs out
STUCK = "Stuck:"  # how a watchdog stop starts its error

# Errors that won't change however often the video is tried.
_PERMANENT = re.compile(r"video unavailable|private video|has been removed|no longer available|copyright|"
                        r"members[- ]only|join this channel|age[- ]restricted|confirm your age|"
                        r"premieres in|this live event|no clips", re.I)


UPDATE_GAP = 5 * 60  # automation may update itself while it waits at least this long for the next video


class Stopped(Exception):
    """The runner was asked to stop while working on a video."""


class NoClips(RuntimeError):
    pass


def classify(error: str, previous: str = "") -> str:
    """'blocked' | 'permanent' | 'retry' for a failed video's error message. A video the watchdog
    stopped twice running is skipped rather than tried again."""
    if is_blocked(error):
        return "blocked"
    if error.startswith(STUCK) and previous.startswith(STUCK):
        return "permanent"
    if _PERMANENT.search(error):
        return "permanent"
    return "retry"


def hours_wait(now: float, start: int, end: int) -> float:
    """Seconds until work may start under the 'work from/until' hours (0 = now)."""
    if start % 24 == end % 24:
        return 0.0
    t = datetime.fromtimestamp(now)
    h = t.hour
    inside = start <= h < end if start < end else (h >= start or h < end)
    if inside:
        return 0.0
    nxt = t.replace(hour=start, minute=0, second=0, microsecond=0)
    if nxt <= t:
        nxt += timedelta(days=1)
    return max(1.0, (nxt - t).total_seconds())


# ---------------------------------------------------------------- recipes


@dataclass
class Recipe:
    match: str  # category name, channel name, @handle, or *
    preset: str
    view: str

    @property
    def catch_all(self) -> bool:
        return self.match.strip().lower() in ("*", "any", "everything")

    def matches(self, job: Job) -> bool:
        m = self.match.strip().lower()
        if self.catch_all:
            return True
        if CATEGORIES.get(job.category, "").lower() == m:
            return True
        squash = lambda x: re.sub(r"\W", "", x.lower())  # noqa: E731
        return bool(job.channel) and squash(m.lstrip("@")) == squash(job.channel)


def parse_recipes(text: str) -> tuple[list[Recipe], list[str]]:
    """'Gaming = Gaming / action | Gameplay below' lines -> recipes, plus lines that don't make sense."""
    out, bad = [], []
    presets = all_presets()
    for line in (text or "").splitlines():
        line = line.partition("#")[0].strip()
        if not line:
            continue
        match, eq, rest = line.partition("=")
        preset, _, view = rest.partition("|")
        preset, view = preset.strip(), view.strip()
        pname = next((k for k in presets if k.lower() == preset.lower()), None) if preset else ""
        vname = find_layout(view) if view else ""
        if not eq or not match.strip() or pname is None or (view and not vname):
            bad.append(line)
            continue
        out.append(Recipe(match.strip(), pname or "", vname or ""))
    return out, bad


def has_ai_key(s: Settings) -> bool:
    """A Claude or Gemini key the AI can use (ai.order)."""
    from .ai import has_key

    return has_key(s)


def settings_for(job: Job, s: Settings, order: "orders.Order | None" = None,
                 clips: int = 0) -> tuple[Settings, str]:
    """The settings a job is clipped with, and a label saying what gave them: a recipe matching
    the video's channel or category comes first, then the style of its niche, then a catch-all
    recipe (*), then Automation's. A niche's clips go to its own folder inside the Automation folder.

    The niche's own clip count and length go on top of the style, and a clip request's (`order`,
    asking this video for `clips` clips) on top of those, into the request's own folder. Special
    requests (the niche's and the request's) go to the AI, and with an AI key they make the AI
    pick the clips, since only it can follow them.

    Clip length is never the style's: it's Min/Max clip length from Settings, unless the niche or the
    request asks for its own, so a preset made for 15-second clips can't undercut what you set."""
    recipes, _ = parse_recipes(s.auto_recipes)
    niche = niches.find(s, job.niche)
    matching = [r for r in recipes if r.matches(job)]
    rule = next((r for r in matching if not (niche and r.catch_all)), None)
    fallback = next((r for r in matching if r.catch_all), None) if niche else None
    layers = [x for x in (rule, niche, fallback) if x]  # strongest first; each has .preset and .view
    preset_from = next((x for x in layers if x.preset), None)
    view_from = next((x for x in layers if x.view), None)
    preset = preset_from.preset if preset_from else s.auto_preset
    view = view_from.view if view_from else s.auto_layout
    out = s
    presets = all_presets()
    if preset in presets:
        out = out.apply(presets[preset])
    if view:
        name = find_layout(view)
        if name:
            out = out.apply(all_layouts()[name])
    folder = s.auto_output_path / niche.folder if niche else s.auto_output_path
    if order is not None:
        folder = folder / "Requests" / order.folder
    overrides = {"output_dir": str(folder), "cache_dir": str(s.cache_path),
                 "min_len": s.min_len, "max_len": max(s.max_len, s.min_len)}
    if niche:
        own_len = niche.clip_overrides()
        overrides |= own_len
        lo, hi = overrides["min_len"], overrides["max_len"]
        if lo > hi:  # the niche's shortest passes Settings' longest (or the other way): the niche's wins
            if "max_len" in own_len and "min_len" not in own_len:
                overrides["min_len"] = hi
            else:
                overrides["max_len"] = lo
    if order is not None:
        overrides |= {"min_len": order.min_len, "max_len": order.max_len,
                      "clip_count": max(1, clips or order.remaining or order.count)}
    # The niche's second videos, unless a channel or category recipe's own preset or view brings its own.
    own = ""
    if rule is not None:
        mine = (presets.get(preset) if preset_from is rule else None,
                all_layouts().get(find_layout(view) or "") if view_from is rule else None)
        for values in mine:  # the view goes on after the preset
            if values and "stack_folder" in values:
                own = str(values["stack_folder"] or "").strip()
    if own:
        overrides["stack_folder"] = own
    elif niche and niche.stack_folder.strip():
        overrides["stack_folder"] = niche.stack_folder.strip()
    told = out.ai_instructions.strip()
    if niche and niche.instructions.strip():
        told = (f"{told}\nThese clips are for a TikTok account about {niche.name}: "
                f"{niche.instructions.strip()}").strip()
    special = orders.instructions(order) if order is not None else ""
    if special:
        told = f"{told}\n{special}".strip()
    if told != out.ai_instructions.strip():
        overrides["ai_instructions"] = told
    wants_ai = bool((niche and niche.instructions.strip()) or special)
    if wants_ai and out.mode != "ai" and has_ai_key(s):
        overrides["mode"] = "ai"
    if s.auto_captions:
        overrides["burn_captions"] = True
    if s.auto_bleep:
        overrides["caption_censor"] = True
    if s.auto_delete_sources:
        overrides["keep_source"] = False
    overrides["qc_enabled"] = s.auto_qc
    overrides["ai_polish"] = s.auto_polish and has_ai_key(s)
    out = out.apply(overrides)
    label = " · ".join(x for x in (preset or "My settings", view) if x)
    why = [f"niche: {niche.name}"] if niche else []
    if order is not None:
        why.append(f"request: {order.count} clips of {order.length_text()}")
    if overrides.get("mode") == "ai":
        why.append("AI picks, for the special requests")
    if rule:
        why.append(f"recipe: {rule.match}")
    if fallback and any(fallback is x for x in (preset_from, view_from)):
        why.append(f"recipe: {fallback.match}")
    if why:
        label += f" ({', '.join(why)})"
    return out, label


# ---------------------------------------------------------------- the runner


def paused_until(db: JobDB, now: float) -> float:
    """When requests to YouTube may start again after it asked this PC to sign in (0 = not paused)."""
    until = db.meta_float("blocked_until")
    return until if now < until else 0.0


def end_pause(db: JobDB, now: float) -> int:
    """Try YouTube again now instead of waiting out a sign-in pause (Automation's Try now, or a
    browser to sign in with was set). Downloads stay spaced out further for the rest of the day.
    Returns how many videos that were waiting for the pause are due again."""
    db.set_meta("blocked_until", 0)
    db.set_meta("nudge", "1")  # a sleeping runner looks now, not in up to 5 minutes
    return db.release_waiting(now)


def gap_factor(db: JobDB, now: float) -> int:
    """Downloads are spaced out twice as much after each sign-in request (up to 4 times), until a
    day passes without one."""
    if now - db.meta_float("blocked_at") > BLOCK_PAUSE + CALM_DAY:
        return 1
    return max(1, min(4, int(db.meta_float("gap_factor") or 1)))


def record_block(db: JobDB, error: str, now: float, cookies: str = "") -> str:
    """YouTube asked this PC to sign in: stop all requests to it for a while (2 hours, or 6 if it
    asked again within a day), and slow down after. Shared by automation and the Research tab.
    `cookies` is the browser it was signing in with, so setting a different one ends the pause.
    Returns the line to log."""
    recent = bool(db.meta_float("blocked_at")) and now - db.meta_float("blocked_at") <= BLOCK_PAUSE + CALM_DAY
    factor = min(4, gap_factor(db, now) * 2) if recent else 2
    until = now + (BLOCK_PAUSE if recent else FIRST_PAUSE)
    db.set_meta("blocked_until", until)
    db.set_meta("blocked_at", now)
    db.set_meta("blocked_cookies", cookies.strip())
    db.set_meta("gap_factor", factor)
    db.set_meta("last_error", error)
    if cookies.strip() == youtube_login.CHOICE:
        sooner = ("ClipMonster's YouTube sign-in may have run out: press Sign in to YouTube in Automation to sign "
                  "in again, which also carries on now")
    elif cookies.strip():
        sooner = (f"{cookies.strip().title()}'s YouTube sign-in may have run out. Press Sign in to YouTube in "
                  "Automation for a sign-in kept just for ClipMonster, which also carries on now")
    else:
        sooner = ("press Sign in to YouTube in Automation (it opens Firefox, once) so downloads are signed in, "
                  "which also carries on now. Or press Try now")
    return (f"⚠ YouTube asked this PC to sign in, so ClipMonster stops asking it for anything until "
            f"{time.strftime('%H:%M', time.localtime(until))} and then spaces downloads {factor}x further "
            f"apart for a day. It carries on by itself; to carry on sooner, {sooner}. "
            f"(YouTube said: {youtube_said(error)})")


def youtube_said(error: str) -> str:
    """The point of a yt-dlp error, for a log line that already has the whole error above it:
    'ERROR: [youtube] abc: Sign in to confirm you're not a bot. This helps...' -> 'Sign in to confirm
    you're not a bot.'"""
    text = re.sub(r"^\s*(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]{6,}:\s*)?", "", error or "").strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip()
    return (first or error or "").strip()


class Runner:
    def __init__(self, db: JobDB | None = None, load: Callable[[], Settings] = Settings.load,
                 log: Log | None = None, now: Callable[[], float] = time.time,
                 scan: Callable | None = None, engine: Callable[..., Engine] = Engine,
                 history: Callable[[], History] | None = None, picks: Callable[[], PickLog] | None = None,
                 priority: Callable[[str], str] | None = None,
                 fetch: Callable[..., Video] = download, self_update: bool = False):
        self.db = db or JobDB()
        self.load = load
        self._log = log or (lambda m: None)
        self.now = now
        self._scan = scan
        self._engine = engine
        self._history = history or (lambda: History(research.history_path()))
        self._picks = picks or (lambda: PickLog(picks_path()))
        self._fresh = fresh.load  # what each standing request searched and showed (fresh.py)
        self._activity_at, self._activity_text = 0.0, ""
        self.pid = os.getpid()
        self._last_beat = 0.0
        self._stop: Callable[[], bool] = lambda: False  # run()'s stop check, so a long scan can be stopped
        self._last_flag_check = 0.0
        self._priority = priority  # speed.set_priority for the headless runner; None leaves the process alone
        self._fetch = fetch
        self._self_update = self_update  # the headless runner installs updates itself (self_update())
        self.prefetch: Prefetch | None = None  # the next video, downloading while this one is clipped

    # ------------------------------------------------------------ bookkeeping
    def log(self, msg: str) -> None:
        self._log(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.now()))}  {msg}")

    def beat(self, force: bool = False) -> None:
        now = self.now()
        if force or now - self._last_beat >= 5:
            self._last_beat = now
            self.db.set_meta("runner_pid", self.pid)
            self.db.set_meta("heartbeat", now)

    def stop_requested(self) -> bool:
        return self.db.meta("stop") == "1"

    def claim(self) -> str:
        """Become the one runner; '' on success, else why not."""
        beat = self.db.meta_float("heartbeat")
        other = self.db.meta("runner_pid")
        if other and other != str(self.pid):
            fresh = self.now() - beat < HEARTBEAT_STALE
            there = beat > boot_time() and process_alive(other)
            if there and fresh:
                return f"Automation is already running (process {other})."
            if there and is_app_process(other):  # its heartbeat stopped, but it's still there: stuck
                return (f"Automation is already running (process {other}) but hasn't answered for "
                        f"{max(1, round((self.now() - beat) / 60))} min. If it's stuck, end it in Task Manager "
                        "and press Start again.")
            if fresh:
                self.log(f"The last automation (process {other}) ended without stopping (a crash or a restart): "
                         "taking over.")
        self.db.set_meta("stop", "0")
        self.db.set_meta("started", self.now())
        self.beat(force=True)
        n = self.db.reset_interrupted()
        if n:
            self.log(f"{n} video{'s' if n != 1 else ''} interrupted last time went back in the queue.")
        return ""

    def release(self) -> None:
        if self.db.meta("runner_pid") == str(self.pid):
            self.db.set_meta("runner_pid", "")
            self.db.set_meta("heartbeat", 0)
            self.db.set_meta("state", "Stopped")

    def activity(self, text: str, frac: float | None = None) -> None:
        """What automation is doing besides clipping (checking what to clip, a standing request), for
        the Automation tab's status bar; "" when it's done. Written at most once a second."""
        now = self.now()
        if text and text == self._activity_text and now - self._activity_at < 1.0 and frac not in (0.0, 1.0):
            return
        self._activity_at, self._activity_text = now, text
        self.db.set_meta("activity", json.dumps({"text": text, "frac": frac, "time": now}) if text else "")

    def state(self, text: str) -> None:
        if self.db.meta("state") != text:
            self.db.set_meta("state", text)

    # ------------------------------------------------------------ one step
    def tick(self) -> float:
        """Do the next thing that's due; returns seconds until something else might be."""
        self.beat()
        now = self.now()
        s = self.load()
        if self._priority:
            said = self._priority(s.speed)
            if said:
                self.log(f"{said} (Settings → Speed).")
        if self.db.meta("paused") == "1":
            self.state("Paused")
            return 30.0

        next_scan = self.db.meta_float("last_scan") + max(10, s.research_interval) * 60
        if self.db.meta("scan_now"):  # "Find videos now" in the Automation tab, or a new request
            next_scan = now
        if research.youtube_key(s) and now >= next_scan:
            self.research(s)
            s = self.load()  # a scan takes minutes: niches made meanwhile count (see research)
            now = self.now()
            next_scan = now + max(10, s.research_interval) * 60
        elif not research.youtube_key(s) and not self.db.meta("warned_key"):
            self.log("No YouTube Data API key, so nothing new is picked. Queued videos are still clipped.")
            self.db.set_meta("warned_key", "1")

        if s.cleanup_auto and now - self.db.meta_float("last_cleanup") >= DAY:
            self.cleanup(s)

        self.lift_pause_for_sign_in(s, now)
        self.check_orders()
        paused = niches.paused_ids(s)
        # Without a key there's no check to wait for (and "Find videos now" can't happen).
        scan_wait = next_scan - now if research.youtube_key(s) else IDLE
        job = self.db.next_due(now, paused)
        if job:
            wait, why = self.wait_reason(s, now, downloaded=self.prefetched(job))
            if wait >= UPDATE_GAP and self._self_update and (self.prefetch is None or not self.prefetch.busy):
                self.self_update(s)  # a long wait (working hours, a sign-in pause) is a good time
            if wait:
                self.state(why)
                return max(1.0, min(wait, scan_wait, IDLE))
            low = self.disk_guard(s, now)
            if low:
                self.state(low)
                return max(1.0, min(DISK_RECHECK, scan_wait))
            self.process(job, s)
            return 1.0
        if self._self_update:
            self.self_update(s)
        upcoming = self.db.next_try(paused)
        blocked = self.blocked(now)
        if blocked and upcoming:  # the videos waiting are waiting for the sign-in pause: say so
            self.state(self.wait_reason(s, now)[1])
        elif self.db.next_due(now) is not None:  # only paused niches' videos are waiting
            self.state("Waiting: the niches with videos waiting are paused")
        else:
            self.state(self.idle_text(s, next_scan, upcoming))
        return max(1.0, min(scan_wait, (upcoming - now) if upcoming else IDLE, IDLE))

    def idle_text(self, s: Settings, next_scan: float, upcoming: float | None = None) -> str:
        """What the status says with nothing to clip, so an empty queue doesn't look stuck."""
        if upcoming:
            return f"Waiting to try a video again at {time.strftime('%H:%M', time.localtime(upcoming))}"
        if not research.youtube_key(s):
            return "Waiting for videos"
        at = time.strftime("%H:%M", time.localtime(next_scan))
        note = self.db.meta("pick_note")
        if note:
            return f"Nothing new to pick in the last check ({note}). Checks again at {at}"
        return f"Waiting for the next check at {at}"

    def lift_pause_for_sign_in(self, s: Settings, now: float) -> None:
        """A sign-in pause ends early once a browser to sign in with is set (or changed) after it began."""
        browser = s.cookies_browser.strip()
        if not browser or not self.blocked(now) or browser == self.db.meta("blocked_cookies"):
            return
        n = end_pause(self.db, now)
        self.log(f"You set ClipMonster to sign in with {browser}, so it's trying YouTube again now"
                 + (f" ({n} video{'s' if n != 1 else ''} waiting)." if n else "."))

    def blocked(self, now: float) -> float:
        return paused_until(self.db, now)

    def gap_factor(self, now: float) -> int:
        return gap_factor(self.db, now)

    def wait_reason(self, s: Settings, now: float, downloaded: bool = False) -> tuple[float, str]:
        """How long until the next video may start, and why. `downloaded`: it was downloaded early,
        so the minutes between downloads are already behind it."""
        blocked = self.blocked(now)
        if blocked:
            until = time.strftime("%H:%M", time.localtime(blocked))
            return blocked - now, (f"Paused until {until} because YouTube asked this PC to sign in. "
                                   "Nothing is wrong; it carries on by itself")
        wait = hours_wait(now, s.auto_start_hour, s.auto_end_hour)
        if wait:
            return wait, f"Outside working hours ({s.auto_start_hour}:00–{s.auto_end_hour}:00)"
        factor = self.gap_factor(now)
        gap = self.next_download(s, now) - now
        if gap > 0 and not downloaded:
            return gap, (f"Spacing downloads out: next in {max(1, round(gap / 60))} min"
                         + (f" (slowed down {factor}x after YouTube asked this PC to sign in)" if factor > 1 else ""))
        return 0.0, ""

    def next_download(self, s: Settings, now: float) -> float:
        """When the next download may start (the minutes between downloads, slowed after sign-in requests)."""
        return self.db.meta_float("last_download") + s.auto_gap_minutes * 60 * self.gap_factor(now)

    # ------------------------------------------------------------ the next video, early
    def prefetched(self, job: Job) -> bool:
        pre = self.prefetch
        return pre is not None and pre.job.id == job.id and pre.video is not None and pre.video.path.exists()

    def start_prefetch(self, current: Job, s: Settings) -> None:
        """Download the video after `current` while it's clipped, once the minutes between
        downloads are up (Download the next video while clipping)."""
        if not s.auto_prefetch or (self.prefetch is not None and self.prefetch.busy):
            return
        now = self.now()
        nxt = self.db.next_due(now, niches.paused_ids(s))  # `current` is working, so it's not this
        if nxt is None or nxt.id == current.id or self.prefetched(nxt):
            return
        order = self.db.get_order(nxt.order) if nxt.order else None
        if nxt.order and (order is None or not order.is_open):
            return
        settings = settings_for(nxt, s, order)[0]
        self.prefetch = Prefetch(nxt, self.next_download(s, now), settings, self)
        self.prefetch.start()

    def take_prefetch(self, job: Job) -> Video | None:
        """The video downloaded early for `job`, waiting for it to finish if it's still downloading."""
        pre = self.prefetch
        if pre is None or pre.job.id != job.id:
            return None
        if pre.call_off():  # still waiting for the minutes between downloads: download it now instead
            self.prefetch = None
            return None
        self.state(f"Finishing the early download of {job.title or job.url}")
        pre.wait(self.beat)
        self.prefetch = None
        return pre.video if pre.video is not None and pre.video.path.exists() else None

    def stop_prefetch(self) -> None:
        if self.prefetch is not None:
            self.prefetch.cancel()
            self.prefetch.join(10)
            self.prefetch = None

    def block(self, error: str, now: float, s: Settings | None = None) -> None:
        self.log(record_block(self.db, error, now, (s or self.load()).cookies_browser))
        self.db.note("blocked", now)

    # ------------------------------------------------------------ updating itself
    def self_update(self, s: Settings) -> None:
        """With Get updates by themselves on and no ClipMonster window open, automation installs a
        new version itself while it has nothing to clip: it downloads it, starts the install step
        and stops (Stopped). The install step starts automation again in the new version."""
        from . import updater

        target = updater.install_dir()
        if target is None or not (s.check_updates and s.auto_update):
            return
        channel = updater.channel_setting(s)
        if not updater.due_for_check(updater.CHECK_HOURS[channel]):
            return
        repo = updater.configured_repo(s.update_repo)
        try:
            rel = updater.check(repo, channel) if repo else None
            updater.save_state(last_check=time.time())
            if rel is None or rel.switch or updater.load_state().get("skipped") == rel.version:
                return  # a switch to an older channel's version is asked about in the window
            if updater.setup_running():
                return  # ClipMonster-Setup.exe is installing a version already
            if updater.other_instances(target):
                self.log(f"ClipMonster {rel.version} is out: it's offered in the ClipMonster window that's open.")
                return
            self.state(f"Downloading ClipMonster {rel.version}")
            self.log(f"ClipMonster {rel.label} is out: downloading it, to install it while there's nothing to clip.")
            new_app = updater.prepare(rel, target, cancelled=self.stop_requested)
            if self.stop_requested() or updater.other_instances(target) or updater.setup_running():
                return  # stopped, or the window was opened meanwhile (it offers the downloaded update)
            updater.save_state(pending=rel.version)
            updater.launch_swap(new_app, target, self.pid, relaunch=updater.EXE_NAME, relaunch_args=("--automation",))
        except updater.UpdateCancelled:
            return
        except updater.UpdateError as exc:
            self.log(f"Couldn't update ClipMonster by itself: {exc}")
            return
        self.log(f"Installing ClipMonster {rel.version}: automation stops now and starts again in the new version.")
        self.state(f"Installing ClipMonster {rel.version}")
        raise Stopped()

    # ------------------------------------------------------------ disk guard
    def free_space(self, s: Settings) -> tuple[int, str]:
        """The least free space on the drives the clips and downloads go to, and that drive."""
        from .doctor import free_space

        least: tuple[int, str] | None = None
        for path in (s.auto_output_path, s.cache_path):
            got = free_space(path)
            if got and (least is None or got[0] < least[0]):
                least = (got[0], got[1].anchor or str(got[1]))
        return least or (1 << 62, "")

    def disk_guard(self, s: Settings, now: float) -> str:
        """'' when there's room to clip; otherwise why automation waits. Under the limit it first
        clears what's safe to clear (at most once an hour), then pauses until there's room."""
        limit = max(0, s.disk_min_free_gb) * GB
        if not limit:
            return ""
        free, drive = self.free_space(s)
        if free < limit and now - self.db.meta_float("last_disk_cleanup") >= DISK_CLEAN_EVERY:
            self.db.set_meta("last_disk_cleanup", now)
            self.log(f"Only {free / GB:.1f} GB free on {drive}, under the {s.disk_min_free_gb} GB to keep free: "
                     "clearing downloads, unfinished downloads, previews and rejected clips.")
            self.cleanup(s, kinds=list(DISK_KINDS), days=0)
            free, drive = self.free_space(s)
        if free >= limit:
            if self.db.meta("disk_low") == "1":
                self.db.set_meta("disk_low", "0")
                self.log(f"There's room again ({free / GB:.1f} GB free on {drive}): carrying on.")
            return ""
        if self.db.meta("disk_low") != "1":
            self.db.set_meta("disk_low", "1")
            self.db.note("disk_low", now)
            self.log(f"⚠ Paused: only {free / GB:.1f} GB free on {drive}, under the {s.disk_min_free_gb} GB to keep "
                     "free. Make room (Clean up, or delete files) and automation carries on by itself.")
        return (f"Paused: only {free / GB:.1f} GB free on {drive} (keeping {s.disk_min_free_gb} GB free). "
                "It carries on when there's room")

    # ------------------------------------------------------------ research → queue
    def research(self, s: Settings) -> int:
        """Scan (or reuse a scan the app made recently), run the picking rules, queue the picks."""
        self.activity("Checking what to clip", 0.0)
        try:
            return self._research(s)
        finally:
            self.activity("")

    def _research(self, s: Settings) -> int:
        now = self.now()
        self.state("Checking what to clip")
        forced = self.db.meta("scan_now")
        self.db.set_meta("scan_now", "")
        only = forced if forced not in ("", "1") else ""  # "Find videos now" for one niche
        if not only:  # a one-niche check doesn't put off the next check of the others
            self.db.set_meta("last_scan", now)
        if only and niches.find(s, only):
            s = s.copy(niches=[d for d in s.niches if isinstance(d, dict) and niches.Niche.from_json(d)
                               and niches.Niche.from_json(d).id == only])
            self.log(f"Finding videos now for {niches.name_of(s, only)}.")
        history = self._history()
        recent = history.last_scan.get("time", 0)
        reused = False
        try:
            if (not forced and now - recent < max(10, s.research_interval) * 60
                    and history.last_scan.get("videos")):
                result = research.last_result(history)
                reused = True  # said in the log only when it picks something, not every half hour
            else:
                scan = self._scan or research.scan
                stop = self._stop
                opts = research.ScanOptions.from_settings(s)
                # Most-replayed checks load YouTube pages: one at a time, spaced out, and none at
                # all while YouTube has asked this PC to sign in. (The charts use the API.)
                opts.deep_workers, opts.deep_pause = 1, DEEP_PAUSE
                if self.blocked(now):
                    opts.deep_count = 0
                # Spaced-out checks take minutes: keep the heartbeat going so the runner isn't taken for dead.
                result = scan(opts, history, self.log, progress=lambda m, f: (self.beat(), self.activity(m, f)),
                              cancelled=lambda: stop() or self.stop_requested(), now=now)
                self.log(f"Checked {len(result.videos)} videos ({result.quota} quota units).")
                for p in result.problems:
                    self.log(f"Research: {p}")
                if getattr(result, "blocked", ""):
                    self.block(result.blocked, self.now(), s)
        except research.ScanCancelled:
            raise Stopped()
        except Exception as exc:
            self.log(f"⚠ Research failed: {exc}")
            self.db.set_meta("last_error", f"Research: {exc}")
            return 0
        picks = self._picks()
        s = self.load()  # a scan takes minutes: niches and rules may have changed since it started
        niches.assign_from_settings(result.videos, s, history)
        videos = list(result.videos)
        found = self.standing(s, history, forced == "1", only)
        if found:  # standing requests' videos join the scan's, under the same rules and limits
            seen = {v.id: v for v in videos}
            for v in found:
                old = seen.get(v.id)
                if old is None:
                    videos.append(v)
                else:  # on the charts too: it counts as found for the request, for the request's niche
                    old.origins = sorted(set(old.origins) | {"request"})
                    old.niche, old.match, old.match_why = v.niche, v.match, v.match_why
        used = fresh.used_ids(self.db)  # queued or clipped before (picks and sends are checked by the rules)
        for v in videos:
            v.used = v.id in used
        policy = Policy.from_settings(s)
        wanted = self.videos_wanted(s)  # clip requests that pick: niche id -> [(request, videos it needs)]
        for nid, asks in wanted.items():  # what requests need comes on top of the daily limits
            # (and so do their own picks in the last day, which the limits already count)
            extra = sum(n + self.picked_for(o, now) for o, n in asks)
            policy.daily += extra
            for k in ([nid] if nid else list(policy.niche_daily)):  # no niche: any niche's video will do
                if k in policy.niche_daily:
                    policy.niche_daily[k] += extra
            if not nid and policy.use_niches:  # and so will a video that fits no niche
                policy.niche_daily[""] = policy.niche_daily.get("", 0) + extra
        decisions = decide(videos, policy, picks, now=now, notes=result.notes,
                           lessons=learn.for_settings(s))
        added = 0
        self.db.set_meta("pick_note", "" if any(d.picked for d in decisions) else why_none(decisions))
        mine = {v.id for v in found}
        passed = [d for d in decisions if d.video.id in mine and not d.picked and d.rule != "done"]
        if passed:  # say why good matches weren't picked, so a request that picks nothing isn't a mystery
            why: dict[str, int] = {}
            for d in passed:
                words = RULE_WORDS.get(d.rule, d.reason)
                why[words] = why.get(words, 0) + 1
            top = sorted(why.items(), key=lambda kv: -kv[1])[:3]
            self.log(f"Standing requests: {len(passed)} good match{'es' if len(passed) != 1 else ''} not picked: "
                     + "; ".join(f"{reason} ({n})" if n > 1 else reason for reason, n in top))
        if reused and any(d.picked for d in decisions):
            self.log("Using the Research tab's last check.")
        for d in decisions:
            if not d.picked:
                continue
            v = d.video
            order = ""
            for key in dict.fromkeys((v.niche, "")):  # its niche's requests first, then ones for no niche
                for i, (o, n) in enumerate(wanted.get(key, [])):
                    if n > 0 and not order:
                        order = o.id
                        wanted[key][i] = (o, n - 1)
            job = Job(id=v.id, url=v.url, title=v.title, channel=v.channel, channel_id=v.channel_id,
                      category=v.category, permission=d.reason, origins=list(v.origins), how="auto",
                      stage="Waiting", niche=v.niche, order=order)
            if not self.db.add(job, now):  # queued before after all: it mustn't use up one of today's picks
                continue
            added += 1
            niche = niches.name_of(s, v.niche)
            self.log(f"Picked{' for ' + niche if niche else ''}{' (for a request)' if order else ''}: "
                     f"{v.title} ({v.channel}, clip-ability {v.score:.0f}). {d.reason}"
                     + (f" {d.learned}" if d.learned else ""))
            picks.record(d, "auto", now)
            history.mark_sent(v.id)
        if added:
            picks.save()
            history.save()
        return added

    # ------------------------------------------------------------ standing requests (lookup.py)
    def standing(self, s: Settings, history: History, everything: bool = False, only: str = "") -> list:
        """Run the standing requests that are due (every Settings → Picking hours; all of them for
        Find videos now, or one niche's) and return their good matches, each in its niche. Each run
        of a request costs about 200 YouTube quota units, and two AI calls when there's a key and
        today's AI calls allow."""
        asks = niches.standing_requests(s)
        key = research.youtube_key(s)
        if not asks or not key:
            return []
        try:
            last = json.loads(self.db.meta("finds_last") or "{}")
        except ValueError:
            last = {}
        now = self.now()
        every = max(1, s.pick_find_hours) * 3600
        ids = {f"{nid}|{text.lower()}" for nid, text in asks}
        last = {k: t for k, t in last.items() if k in ids}  # requests since removed are forgotten
        stop = self._stop
        language = next(iter(language_codes(s.pick_languages)), "")
        out: list = []
        memory = self._fresh()
        done = fresh.used_ids(self.db, self._picks(), history)  # never found again: nothing to rate or pick
        try:
            for nid, text in asks:
                k = f"{nid}|{text.lower()}"
                due = everything or (only and nid == only) or now - float(last.get(k) or 0) >= every
                if not due or (only and nid != only):
                    continue
                used = self.db.count("ai", now - DAY)
                with_ai = has_ai_key(s) and used + 2 <= s.auto_ai_daily
                self.state(f"Finding videos for “{text[:40]}”")
                try:
                    r = lookup.find(text, s, key, FIND_SINCE, s.research_min_minutes, language, self.log,
                                    progress=lambda m, f, t=text: (self.beat(),
                                                                   self.activity(f"Finding videos for “{t[:40]}”: {m}", f)),
                                    cancelled=lambda: stop() or self.stop_requested(), use_ai=with_ai,
                                    memory=memory, request_key=fresh.request_key(text, nid), skip=done)
                except research.ScanCancelled:
                    raise
                except ResearchBlocked as exc:  # a bad key or used-up quota: the rest would fail too
                    self.log(f"⚠ Standing requests stopped: {exc}")
                    break
                except Exception as exc:
                    self.log(f"⚠ Standing request “{text}”: {exc}")
                    last[k] = now
                    continue
                last[k] = now
                for _ in range(r.ai_calls):
                    self.db.note("ai", self.now())
                good = [v for v in r.videos if v.match >= FIND_MATCH]
                for v in good:
                    v.niche = nid
                    v.sent = bool((history.videos.get(v.id) or {}).get("sent"))
                out += good
                niche = niches.name_of(s, nid)
                new = sum(1 for v in good if v.new)
                self.log(f"Standing request “{text}”{' for ' + niche if niche else ''}: {len(good)} good "
                         f"match{'es' if len(good) != 1 else ''} ({new} new) of {len(r.videos) + r.weak} found"
                         + (f", {r.skipped} already used left out" if r.skipped else "")
                         + f" (searched {', '.join(repr(q) for q in r.plan.queries)}, "
                         + f"{fresh.ORDER_WORDS.get(r.plan.order, r.plan.order)} first; {r.quota} quota units"
                         + (f", rated by {r.rated_by}" if r.rated_by else "") + ").")
                for p in r.problems:
                    self.log(f"Standing request “{text}”: {p}")
        finally:
            self.db.set_meta("finds_last", json.dumps(last))
            memory.save()
        return out

    # ------------------------------------------------------------ clip requests (orders.py)
    def per_video(self, s: Settings, niche_id: str) -> int:
        """Clips one picked video gives a request: the niche's clips per video, else the style's count."""
        n = niches.find(s, niche_id)
        if n and n.clips_per_video:
            return n.clips_per_video
        job = Job(id="", url="", niche=niche_id)
        return max(1, settings_for(job, s)[0].clip_count)

    def picked_for(self, order: orders.Order, now: float) -> int:
        """Videos automation picked for a request in the last day."""
        return sum(1 for j in self.db.order_jobs(order.id) if j.how == "auto" and j.created >= now - DAY)

    def check_orders(self) -> None:
        """Count open requests again: their videos may have been removed from the queue meanwhile."""
        for o in self.db.orders(open_only=True):
            if not any(j.status in ("queued", "working") for j in self.db.order_jobs(o.id)):
                self.order_progress(o.id)

    def videos_wanted(self, s: Settings) -> dict[str, list[tuple[orders.Order, int]]]:
        """Open requests that pick their videos, by niche id, with how many more videos each needs."""
        out: dict[str, list[tuple[orders.Order, int]]] = {}
        on = {n.id for n in niches.active(s)}
        for o in reversed(self.db.orders(open_only=True)):  # oldest request first
            if not o.picks or (o.niche and o.niche not in on):
                continue
            waiting = sum(1 for j in self.db.order_jobs(o.id) if j.status in ("queued", "working"))
            need = orders.videos_needed(o, self.per_video(s, o.niche), waiting)
            if need:
                out.setdefault(o.niche, []).append((o, need))
        return out

    def place(self, order: orders.Order) -> orders.Order:
        """Save a new request and queue its links; a request that picks asks for a check right away."""
        now = self.now()
        self.db.add_order(order)
        for url in order.links:
            vid = _video_id(url)
            job = Job(id=vid, url=url, title=url, permission="Added by you (request)", how="manual",
                      stage="Waiting", niche=order.niche, order=order.id)
            if not self.db.add(job, now):  # clipped before (or waiting): the request gets its own copy
                job.id = orders.job_id(vid, order.id)
                self.db.add(job, now)
        if order.picks:
            request_scan(self.db, order.niche)
            message = "Waiting for automation to pick videos"
        else:
            message = f"{len(order.links)} video{'s' if len(order.links) != 1 else ''} waiting"
        self.log(f"New request: {order.title(niches.name_of(self.load(), order.niche))}"
                 + (f". Special requests: {order.notes}" if order.notes else ""))
        return self.db.update_order(order.id, now, message=message) or order

    def cancel(self, order_id: str) -> int:
        """Cancel a request; its videos still waiting leave the queue (one being clipped finishes)."""
        removed = 0
        for j in self.db.order_jobs(order_id):
            if j.status == "queued":
                self.db.remove(j.id)
                removed += 1
        self.db.update_order(order_id, self.now(), status="cancelled", message="")
        return removed

    def order_progress(self, order_id: str) -> orders.Order | None:
        """Count a request's ready clips and decide whether it's done."""
        order = self.db.get_order(order_id)
        if order is None or order.status == "cancelled":
            return order
        jobs = self.db.order_jobs(order_id)
        made = sum(j.clips for j in jobs if j.status == "done")
        waiting = sum(1 for j in jobs if j.status in ("queued", "working"))
        status, message = "open", ""
        if made >= order.count:
            status, message = "done", f"{made} clips ready"
        elif order.links and not waiting:
            status = "short"
            message = (f"Made {made} of {order.count}: its videos didn't have more good moments of "
                       f"{order.length_text()}. Add links or ask again with other videos")
        elif order.links:
            message = f"{made} of {order.count} ready · {waiting} video{'s' if waiting != 1 else ''} to go"
        else:
            message = (f"{made} of {order.count} ready · " + (f"{waiting} video{'s' if waiting != 1 else ''} "
                       "waiting" if waiting else "picking more videos at the next check"))
        if status != "open" and order.status == "open":
            self.log(f"Request {'done' if status == 'done' else 'finished short'}: {message}.")
        return self.db.update_order(order_id, self.now(), made=made, status=status, message=message)

    def add_manual(self, url: str, title: str = "", niche: str = "") -> bool:
        from .download import is_url

        if not is_url(url):
            raise ValueError("Give a video link (https://…).")
        vid = _video_id(url)
        return self.db.add(Job(id=vid, url=url, title=title or url, permission="Added by you", how="manual",
                               stage="Waiting", niche=niche), self.now())

    # ------------------------------------------------------------ one video
    def process(self, job: Job, s: Settings) -> None:
        now = self.now()
        if job.niche and niches.find(s, job.niche) is None:
            s = self.load()  # the settings we were given may be older than the niche
            if niches.find(s, job.niche) is None:
                if niches.load(s):  # deleted while it waited (or mid-scan)
                    self.db.remove(job.id)
                    self.log(f"Took {job.title or job.url} out of the queue: its niche was deleted.")
                else:  # no niches at all: settings.json may not have been readable, so don't lose the video
                    self.db.update(job.id, now, status="failed", error="Its niche isn't in your settings",
                                   stage="Its niche is missing")
                    self.log(f"✗ {job.title or job.url}: its niche isn't in your settings. Retry it once it's back.")
                return
        order = self.db.get_order(job.order) if job.order else None
        clips = 0
        if job.order:
            if order is None or not order.is_open:  # cancelled, done or deleted while this waited
                self.db.remove(job.id)
                self.log(f"Took {job.title or job.url} out of the queue: its request is "
                         f"{'gone' if order is None else orders.STATUS_WORDS[order.status].lower()}.")
                return
            left = sum(1 for j in self.db.order_jobs(order.id) if j.status in ("queued", "working"))
            clips = orders.clips_for_video(order, self.per_video(s, job.niche), left)
        settings, label = settings_for(job, s, order, clips)
        used = self.db.count("ai", now - DAY)
        if settings.mode == "ai" and used >= s.auto_ai_daily:
            settings = settings.apply({"mode": "auto"})
            label += " · smart signals (today's AI limit is used up)"
        if settings.ai_polish and used + (settings.mode == "ai") >= s.auto_ai_daily:
            settings = settings.apply({"ai_polish": False})
            label += " · no polish (today's AI limit is used up)"
        elif settings.ai_polish:
            label += " · AI polish"
        early = self.take_prefetch(job)
        if early is None:
            self.db.set_meta("last_download", now)
        now = self.now()
        self.db.update(job.id, now, status="working", stage="Starting", recipe=label, error="")
        self.state(f"Clipping: {job.title or job.url}")
        self.log(f"Clipping {job.title or job.url} with {label}; clips {settings.min_len:g}–{settings.max_len:g} s")

        last = {"t": 0.0, "msg": "", "video": ""}
        self.db.set_meta("working_video", "")
        self.activity("")
        # The status bars: the video's steps, weighted by how long each took last time.
        try:
            timings = json.loads(self.db.meta("last_timings") or "{}")
        except ValueError:
            timings = {}
        tracker = steps.Tracker(steps.plan_for(settings), steps.weights_from_timings(timings))

        def progress(msg: str, frac: float | None) -> None:
            t = self.now()
            status = tracker.update(msg, frac)
            if msg != last["msg"] or t - last["t"] >= 2:
                last.update(t=t, msg=msg)
                self.db.update(job.id, t, stage=msg + (f" {frac:.0%}" if frac is not None else ""),
                               progress=json.dumps(status.to_json()))
            mark_busy()
            self.beat()

        def mark_busy() -> None:  # the video's real id, once known: a clean-up mustn't touch it
            if p.video is not None and p.video.id != last["video"]:
                last["video"] = p.video.id
                self.db.set_meta("working_video", p.video.id)
                self.start_prefetch(job, s)  # this one's downloaded: the next can start (in its time)

        def cancelled() -> bool:
            t = self.now()
            if t - self._last_flag_check < 2:
                return False
            self._last_flag_check = t
            return self.stop_requested()

        watch = Watchdog(s.watchdog_minutes * 60, lambda: (last["t"] or now), lambda: last["msg"], self.now)
        engine = self._engine(settings, log=self.log, progress=progress, cancelled=cancelled)
        p = Project(job.url)
        p.video = early
        p.meta = {"automation": {"permission": job.permission, "origins": job.origins, "how": job.how,
                                 "recipe": label, "picked_at": job.created}}
        niche = niches.find(s, job.niche)
        if niche:  # for the Library, and for TikTok Manager to know which account the clips are for
            p.meta["automation"] |= {"niche": niche.name, "niche_id": niche.id, "account": niche.account}
        if order is not None:
            p.meta["automation"]["request"] = {"id": order.id, "count": order.count, "min_len": order.min_len,
                                               "max_len": order.max_len, "notes": order.notes}
        watch.start()
        try:
            engine.prepare(p)
            mark_busy()
            if settings.mode == "ai":
                self.db.note("ai", self.now())
            if p.polished:
                self.db.note("ai", self.now())
            if not [c for c in p.clips if c.enabled]:
                raise NoClips("No clips found in this video.")
            folder = engine.export(p)
        except Cancelled:
            watch.stop()
            self.db.update(job.id, self.now(), status="queued", stage="Stopped; will start again", progress="")
            self.log(f"Stopped while clipping {job.title}; it'll start again next time.")
            raise Stopped()
        except Exception as exc:
            watch.stop()
            error = str(exc) or exc.__class__.__name__
            if watch.fired:
                error = watch.reason()
                self.db.note("watchdog", self.now())
                self.log(f"⚠ {job.title or job.url}: {error}")
            self.failed(job, error, s)
            if job.order:
                self.order_progress(job.order)
            return
        watch.stop()
        title = p.video.title if p.video else job.title
        made = len([c for c in p.clips if c.enabled])
        rejected = p.qc.get("rejected", 0) if p.qc else 0
        self.db.update(job.id, self.now(), status="done", stage="Done", folder=str(folder), progress="",
                       clips=made - rejected, rejected=rejected, title=title,
                       channel=p.video.channel if p.video else job.channel)
        self.log(f"✓ {made - rejected} clips from {title} → {folder}"
                 + (f" ({rejected} rejected by the quality check)" if rejected else ""))
        if p.timings:
            self.log(f"  {title} {timing_text(p.timings)}.")
            self.db.set_meta("last_timings", json.dumps({k: round(v, 1) for k, v in p.timings.items()}))
        if job.order:
            self.order_progress(job.order)
        self.prune(s)

    def failed(self, job: Job, error: str, s: Settings) -> None:
        now = self.now()
        kind = classify(error, job.error)
        attempts = job.attempts + 1
        if kind == "blocked":
            self.block(error, now, s)
            self.db.update(job.id, now, status="queued", stage=SIGN_IN_WAIT, progress="",
                           error=error, next_try=self.db.meta_float("blocked_until"))
            return
        if kind == "permanent" or attempts >= s.auto_retries:
            stuck = error.startswith(STUCK)
            self.db.update(job.id, now, status="failed", attempts=attempts, error=error, progress="",
                           stage="Skipped: stuck twice" if stuck and kind == "permanent" else
                           "Gave up" if kind == "retry" else "Can't be clipped")
            self.log(f"✗ {job.title or job.url}: {error}" + (" (gave up)" if kind == "retry" else ""))
            self.db.set_meta("last_error", error)
            return
        wait = 60.0 if error.startswith(STUCK) else BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        self.db.update(job.id, now, status="queued", attempts=attempts, error=error, next_try=now + wait,
                       stage=f"Try {attempts + 1} in {_span(wait)}", progress="")
        self.log(f"⚠ {job.title or job.url}: {error}. Trying again in {_span(wait)}.")

    def prune(self, s: Settings) -> int:
        """Remove cached analysis, transcripts and downloads of videos finished long ago."""
        cutoff = self.now() - s.auto_keep_days * DAY
        removed = 0
        for job in self.db.jobs(limit=5000):
            if job.status != "done" or job.pruned or job.updated > cutoff:
                continue
            for f in s.cache_path.glob(f"{glob_escape(job.video_id)}.*"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            self.db.update(job.id, job.updated, pruned=1)
        return removed

    def cleanup(self, s: Settings, kinds: list[str] | None = None, days: float | None = None) -> None:
        """The daily clean-up (Settings → Automation → Clean up by itself): what's ticked there and
        older than its days (at least one). Never a video that's queued, being clipped or in the
        Studio, or a clip TikTok Manager still has waiting. The disk guard passes its own kinds, of
        any age."""
        from . import cleanup

        now = self.now()
        if kinds is None:
            self.db.set_meta("last_cleanup", now)
            kinds = cleanup.parse_kinds(s.cleanup_kinds)
            days = max(1, s.cleanup_days)
        if not kinds:
            return
        try:
            busy = cleanup.busy_ids(self.db)
            plan = cleanup.plan(s, now, busy=busy, older_than_days=days or 0, kinds=kinds)
            result = cleanup.apply(plan, kinds)
        except Exception as exc:  # a clean-up problem must never stop the clipping
            self.log(f"⚠ Clean up failed: {exc}")
            return
        if result.freed:
            self.db.note(f"freed:{result.freed}", now)
        if result.removed:
            self.log(f"Cleaned up: {result.summary()}.")
        for path, error in result.failed[:5]:
            self.log(f"⚠ Couldn't remove {path}: {error}")

    def health(self) -> None:
        """The health check (doctor.py, offline) as automation starts: problems go in the log and in
        jobs.db for the Automation tab."""
        from . import doctor

        try:
            found = doctor.problems(doctor.check(self.load(), self.db, now=self.now()))
        except Exception as exc:  # the check must never keep automation from running
            self.log(f"Health check couldn't run: {exc}")
            return
        self.db.set_meta("health", json.dumps([c.to_json() for c in found]))
        self.db.set_meta("health_at", self.now())
        for c in found:
            self.log(f"{'✗' if c.level == doctor.BAD else '⚠'} Health check: {c.name}: {c.detail}"
                     + (f". Fix: {c.fix}" if c.fix else ""))

    # ------------------------------------------------------------ the loop
    def run(self, stop: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep,
            once: bool = False) -> int:
        why = self.claim()
        if why:
            self.log(why)
            return 1
        self.log(f"Automation started (process {self.pid}).")
        # The heartbeat keeps going on its own, so a long step (Whisper can go minutes without
        # reporting progress) never makes automation look stopped, or lets a second one take over.
        pulse = Pulse(lambda: self.beat(force=True), PULSE)
        pulse.start()
        # Finished clips go up to the storage server alongside, never holding up clipping (uploads.py).
        uploader = Uploader(self.load, lambda m: self.log(m), db_factory=lambda: JobDB(self.db.path))
        uploader.start()
        stop = self._stop = stop or (lambda: False)
        try:
            self.health()
            while True:
                try:
                    wait = self.tick()
                except Stopped:
                    break
                if once and (wait > 1.0 or not self.db.next_due(self.now(), niches.paused_ids(self.load()))):
                    break
                waited = 0.0
                while waited < wait:
                    if stop() or self.stop_requested():
                        raise Stopped()
                    if self.db.meta("nudge") == "1":  # the app ended a pause: look again now
                        self.db.set_meta("nudge", "0")
                        break
                    step = min(2.0, wait - waited)
                    sleep(step)
                    waited += step
                    self.beat()
                if stop() or self.stop_requested():
                    break
        except (Stopped, KeyboardInterrupt):
            pass
        finally:
            self.stop_prefetch()
            uploader.stop()  # an upload cut off here carries on next time, from where it stopped
            pulse.stop()  # before release, so a last beat can't mark it running again
            self.log("Automation stopped.")
            self.release()
        return 0


class Prefetch:
    """The next video downloading on a thread of its own while this one is clipped. It waits for
    the minutes between downloads first, and on anything going wrong gives up quietly: the video
    is then downloaded in its turn, as before. A sign-in request still pauses everything."""

    def __init__(self, job: Job, start_at: float, settings: Settings, runner: Runner):
        self.job, self.start_at, self.s, self.runner = job, start_at, settings, runner
        self.video: Video | None = None
        self.error = ""
        self.started = False  # the download itself has begun
        self._cancel = threading.Event()
        self._lock = threading.Lock()  # starting the download and calling it off can't cross
        self._thread = threading.Thread(target=self._run, name="Download the next video", daemon=True)
        self._tracker = steps.Tracker()
        self._shown = 0.0

    @property
    def busy(self) -> bool:
        return self._thread.is_alive()

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def call_off(self) -> bool:
        """Cancel it if the download hasn't begun yet. False: it has, so wait for it instead."""
        with self._lock:
            if self.started:
                return False
            self._cancel.set()
            return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread.ident is not None:
            self._thread.join(timeout)

    def wait(self, beat: Callable[[], None]) -> None:
        while self.busy:
            self._thread.join(2)
            beat()

    def _progress(self, msg: str, frac: float | None) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        now = self.runner.now()
        if msg.startswith("Downloading") and now - self._shown >= 2:  # its own status bar in the queue
            self._shown = now
            status = self._tracker.update(msg, frac)
            status.message = f"Downloading early: {msg[len('Downloading '):].strip('()') or 'starting'}"
            self.runner.db.update(self.job.id, now, stage=status.message + (f" {frac:.0%}" if frac is not None else ""),
                                  progress=json.dumps(status.to_json()))

    def _run(self) -> None:
        r, s, job = self.runner, self.s, self.job
        title = job.title or job.url
        while not self._cancel.is_set() and r.now() < self.start_at:
            self._cancel.wait(min(5.0, max(0.05, self.start_at - r.now())))
        now = r.now()
        if (self._cancel.is_set() or r.blocked(now) or hours_wait(now, s.auto_start_hour, s.auto_end_hour)
                or r.db.meta("paused") == "1"):
            return
        fresh = r.db.get(job.id)
        if fresh is None or fresh.status != "queued":  # removed or retried meanwhile
            return
        with self._lock:
            if self._cancel.is_set():
                return
            self.started = True
        r.db.set_meta("last_download", now)
        r.db.update(job.id, now, stage="Downloading early, while the video before it is clipped")
        r.log(f"Downloading {title} now, while the video before it is clipped.")
        try:
            self.video = r._fetch(job.url, s.cache_path, s.quality, s.cookies_browser or None, None, self._progress)
        except Exception as exc:
            if not self._cancel.is_set():
                self.error = str(exc) or exc.__class__.__name__
                if is_blocked(self.error):
                    r.block(self.error, r.now(), s)
                else:
                    r.log(f"Couldn't download {title} early ({youtube_said(self.error)}); it's downloaded "
                          "again in its turn.")
            r.db.update(job.id, r.now(), stage="Waiting", progress="")
            return
        r.db.update(job.id, r.now(), stage="Downloaded, waiting its turn", progress="")


class Pulse:
    """Calls `beat` every `every` seconds on a thread of its own until stopped."""

    def __init__(self, beat: Callable[[], None], every: float):
        import threading

        self._beat, self.every = beat, every
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def start(self) -> None:
        if self.every > 0:
            self._thread.start()

    def _run(self) -> None:
        while not self._done.wait(self.every):
            try:
                self._beat()
            except Exception:  # a locked or busy jobs.db: the next beat tries again
                pass

    def stop(self) -> None:
        self._done.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)


class Watchdog:
    """Stops the ffmpeg work of a video that has made no progress for `limit` seconds: the call
    waiting on it then fails, and the runner records why (and tries once more). Downloads time
    out by themselves; this is for a stuck encode, measurement or check."""

    def __init__(self, limit: float, last: Callable[[], float], stage: Callable[[], str],
                 now: Callable[[], float] = time.time, every: float = 15.0):
        self.limit, self._last, self._stage, self._now, self.every = limit, last, stage, now, every
        self.fired = False
        self._stage_at_fire = ""
        self._done = None
        self._thread = None

    def start(self) -> None:
        import threading

        if self.limit <= 0:
            return
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _watch(self) -> None:
        while not self._done.wait(self.every):
            if self.check():
                return

    def check(self) -> bool:
        """One look (what the thread does every few seconds); True if it stopped the work. With no
        ffmpeg running (a download or an AI call is slow), there's nothing to stop: it keeps watching."""
        from .ffmpeg import kill_running

        if self.limit > 0 and not self.fired and self._now() - self._last() >= self.limit:
            stage = self._stage()
            if kill_running():
                self._stage_at_fire, self.fired = stage, True
        return self.fired

    def stop(self) -> None:
        if self._done is not None:
            self._done.set()

    def reason(self) -> str:
        minutes = self.limit / 60
        stage = re.sub(r"\s*\d+%$", "", self._stage_at_fire).strip() or "working"  # "Exporting clip 2/5 40%"
        return f"{STUCK} no progress for {minutes:g} min while {stage[0].lower() + stage[1:]}, so it was stopped"


# What stopped videos from being picked, in the words the status line uses.
_RULE_WORDS = {
    "done": "already picked", "niche": "fit no niche", "length": "too long or short", "score": "low clip-ability",
    "age": "too old", "word": "blocked words", "category": "blocked category", "channel": "blocked channel",
    "language": "other languages", "live": "live or not out yet", "source": "not from your sources",
    "channel_cap": "channel's weekly limit", "niche_cap": "niche's daily limit", "daily_cap": "daily limit",
}


def why_none(decisions) -> str:
    """'21 videos: 12 already picked, 4 too long or short, 3 fit no niche' for a check that picked nothing."""
    if not decisions:
        return "no videos found"
    counts: dict[str, int] = {}
    for d in decisions:
        word = _RULE_WORDS.get(d.rule, "other rules")
        counts[word] = counts.get(word, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    n = len(decisions)
    return f"{n} video{'s' if n != 1 else ''}: " + ", ".join(f"{c} {w}" for w, c in top)


def _span(seconds: float) -> str:
    return f"{seconds / 3600:g} h" if seconds >= 3600 else f"{seconds / 60:.0f} min"


def glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _video_id(url: str) -> str:
    from .download import video_id

    return video_id(url)


# ---------------------------------------------------------------- running it


def log_path() -> Path:
    return app_dir() / "automation.log"


def file_logger(console: Log | None = None) -> Log:
    """Append to automation.log (rolled over at 2 MB), and echo to the console if given."""
    path = log_path()

    def write(line: str) -> None:
        try:
            if path.exists() and path.stat().st_size > 2_000_000:
                os.replace(path, path.with_suffix(".log.1"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if console:
            console(line)
    return write


def run_headless() -> int:
    """`ClipMonster.exe --automation`: the runner with no window, logging to automation.log."""
    from . import speed

    from . import updater

    updater.tidy()  # after an update it installed itself: the download and the unpacked copy
    try:
        from .cleanup import sweep_work

        sweep_work()  # what a crash left in the work folder
    except Exception:
        pass
    return Runner(log=file_logger(), priority=speed.set_priority, self_update=True).run()


def request_scan(db: JobDB, niche_id: str = "") -> None:
    """Ask automation to check for videos now: for one niche, or all ("1"). Two different asks
    before the check happens make it a check of all of them."""
    asked = db.meta("scan_now")
    db.set_meta("scan_now", niche_id if niche_id and asked in ("", niche_id) else "1")
    db.set_meta("nudge", "1")  # a running automation looks now, not after its wait


def request_uploads(db: JobDB) -> None:
    """Ask the uploader (in automation, or in the window) to look again now: the Upload settings
    changed, Google Drive was signed in to, or failed uploads should be tried again."""
    db.set_meta("upload_hold", 0)
    db.set_meta("upload_nudge", "1")


def is_app_process(pid: str | int) -> bool:
    """Whether the process with this id is ClipMonster (or Python running it). True when it can't be
    told; False for some other program that was given the id after ClipMonster's ended."""
    name = process_name(pid).lower()
    return not name or name.removesuffix(".exe") in ("clipmonster", "ytclip") or name.startswith(("python", "pythonw"))


def runner_hung(db: JobDB, now: float | None = None) -> str:
    """The process id of an automation whose heartbeat stopped while it's still running, else ''."""
    now = now or time.time()
    pid = db.meta("runner_pid")
    beat = db.meta_float("heartbeat")
    if pid and now - beat >= HEARTBEAT_STALE and beat > boot_time() and process_alive(pid) and is_app_process(pid):
        return pid
    return ""


def runner_alive(db: JobDB, now: float | None = None) -> bool:
    """Whether automation is running: its heartbeat is fresh and its process is still there (after a crash
    the heartbeat stays fresh for a few minutes)."""
    now = now or time.time()
    pid = db.meta("runner_pid")
    beat = db.meta_float("heartbeat")
    return bool(pid) and now - beat < HEARTBEAT_STALE and beat > boot_time() and process_alive(pid)


def launch_command() -> list[str]:
    """How to start a runner in the background from this install."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve().parent / "ClipMonster.exe"), "--automation"]
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if os.name == "nt" and pythonw.exists() else exe), "-m", "ytclip.gui.app", "--automation"]


def launch_background() -> None:
    import subprocess

    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(launch_command(), creationflags=flags, close_fds=True, cwd=str(Path.home()),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


TASK_NAME = "ClipMonster Automation"  # the scheduled task versions before 2.4.1 used (removed when seen)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClipMonster Automation"


def startup_command() -> str:
    """The command line Windows runs at sign-in to start automation: the usual per-user Run entry,
    the way most apps start with Windows (a scheduled task looked suspicious to Defender)."""
    return " ".join(f'"{c}"' if " " in c or not c.startswith("-") else c for c in launch_command())


def _run_key(write: bool = False):
    import winreg

    if write:  # the Run key may not exist yet (a new Windows account has none until something adds to it)
        return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)


def _remove_old_task() -> None:
    """Versions before 2.4.1 started automation with a scheduled task: remove it if it's still there."""
    import subprocess

    try:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def set_start_with_windows(on: bool) -> str:
    """'' on success, else the error."""
    if os.name != "nt":
        return "Start with Windows only works on Windows."
    import winreg

    try:
        with _run_key(write=True) as key:
            if on:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        return str(exc)
    _remove_old_task()
    return ""


def run_entry() -> str:
    """The command Start with Windows runs, or '' when it's off."""
    if os.name != "nt":
        return ""
    import winreg

    try:
        with _run_key() as key:
            return str(winreg.QueryValueEx(key, RUN_VALUE)[0])
    except OSError:
        return ""


def starts_with_windows() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with _run_key() as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return False
    return "--automation" in str(value)


def refresh_start_with_windows() -> None:
    """After an update or a move, point the Run entry at this copy of ClipMonster (it's only rewritten
    when it's already on and points somewhere else)."""
    if starts_with_windows():
        try:
            import winreg

            with _run_key() as key:
                value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            if value != startup_command():
                set_start_with_windows(True)
        except OSError:
            pass
