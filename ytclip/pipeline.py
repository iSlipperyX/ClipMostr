"""The unattended runner: research → pick → download → analyse → find clips → export, on its own.

Runner.tick() does one step: a research scan when one is due (queuing whatever the picking
rules pick), then at most one queued video. run() repeats it until asked to stop. Everything
it knows lives in jobs.db, so a crash, reboot or kill loses nothing: the video it was on goes
back in the queue, and the download and analysis caches make the second try quick.

It runs as `ytclip auto` in a console, as `ClipMonster.exe --automation` with no window
(what Start with Windows uses), and the Automation tab watches and steers it through jobs.db.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import learn, niches, orders, research
from .download import is_blocked
from .engine import Cancelled, Engine, Project
from .jobs import SIGN_IN_WAIT, Job, JobDB
from .policy import PickLog, Policy, decide, picks_path
from .settings import Settings, all_layouts, all_presets, app_dir, find_layout
from .trending import CATEGORIES, History

Log = Callable[[str], None]

DAY = 86400.0
BACKOFF = (15 * 60, 3600, 4 * 3600)  # wait before the 2nd, 3rd, 4th... try
FIRST_PAUSE = 2 * 3600  # how long requests to YouTube pause after it asks the PC to sign in
BLOCK_PAUSE = 6 * 3600  # ... and after it asks again within a day of the last time
CALM_DAY = DAY  # after this long with no sign-in request, downloads go back to the usual spacing
DEEP_PAUSE = 20.0  # seconds between automation's most-replayed checks
HEARTBEAT_STALE = 180  # a runner silent for this long is gone
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
    return bool(s.ai_api_key.strip() or os.environ.get("ANTHROPIC_API_KEY"))


def settings_for(job: Job, s: Settings, order: "orders.Order | None" = None,
                 clips: int = 0) -> tuple[Settings, str]:
    """The settings a job is clipped with, and a label saying what gave them: a recipe matching
    the video's channel or category comes first, then the style of its niche, then a catch-all
    recipe (*), then Automation's. A niche's clips go to its own folder inside the Automation folder.

    The niche's own clip count and length go on top of the style, and a clip request's (`order`,
    asking this video for `clips` clips) on top of those, into the request's own folder. Special
    requests (the niche's and the request's) go to the AI, and with an AI key they make the AI
    pick the clips, since only it can follow them."""
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
    overrides = {"output_dir": str(folder), "cache_dir": str(s.cache_path)}
    if niche:
        overrides |= niche.clip_overrides()
        lo, hi = overrides.get("min_len", out.min_len), overrides.get("max_len", out.max_len)
        if lo > hi:  # the niche's shortest passes the style's longest (or the other way): the niche's wins
            if "max_len" in overrides:
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
    return (f"⚠ YouTube asked this PC to sign in, so ClipMonster stops asking it for anything until "
            f"{time.strftime('%H:%M', time.localtime(until))} and then spaces downloads {factor}x further "
            f"apart for a day. It carries on by itself; to carry on sooner, pick a browser that's signed in to "
            f"YouTube in Settings → Download → Use browser login, or press Try now in Automation. ({error})")


class Runner:
    def __init__(self, db: JobDB | None = None, load: Callable[[], Settings] = Settings.load,
                 log: Log | None = None, now: Callable[[], float] = time.time,
                 scan: Callable | None = None, engine: Callable[..., Engine] = Engine,
                 history: Callable[[], History] | None = None, picks: Callable[[], PickLog] | None = None):
        self.db = db or JobDB()
        self.load = load
        self._log = log or (lambda m: None)
        self.now = now
        self._scan = scan
        self._engine = engine
        self._history = history or (lambda: History(research.history_path()))
        self._picks = picks or (lambda: PickLog(picks_path()))
        self.pid = os.getpid()
        self._last_beat = 0.0
        self._stop: Callable[[], bool] = lambda: False  # run()'s stop check, so a long scan can be stopped
        self._last_flag_check = 0.0

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
        if other and other != str(self.pid) and self.now() - beat < HEARTBEAT_STALE:
            if beat > boot_time() and process_alive(other):
                return f"Automation is already running (process {other})."
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

    def state(self, text: str) -> None:
        if self.db.meta("state") != text:
            self.db.set_meta("state", text)

    # ------------------------------------------------------------ one step
    def tick(self) -> float:
        """Do the next thing that's due; returns seconds until something else might be."""
        self.beat()
        now = self.now()
        s = self.load()
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
            wait, why = self.wait_reason(s, now)
            if wait:
                self.state(why)
                return max(1.0, min(wait, scan_wait, IDLE))
            low = self.disk_guard(s, now)
            if low:
                self.state(low)
                return max(1.0, min(DISK_RECHECK, scan_wait))
            self.process(job, s)
            return 1.0
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

    def wait_reason(self, s: Settings, now: float) -> tuple[float, str]:
        blocked = self.blocked(now)
        if blocked:
            until = time.strftime("%H:%M", time.localtime(blocked))
            return blocked - now, (f"Paused until {until} because YouTube asked this PC to sign in. "
                                   "Nothing is wrong; it carries on by itself")
        wait = hours_wait(now, s.auto_start_hour, s.auto_end_hour)
        if wait:
            return wait, f"Outside working hours ({s.auto_start_hour}:00–{s.auto_end_hour}:00)"
        factor = self.gap_factor(now)
        gap = self.db.meta_float("last_download") + s.auto_gap_minutes * 60 * factor - now
        if gap > 0:
            return gap, (f"Spacing downloads out: next in {max(1, round(gap / 60))} min"
                         + (f" (slowed down {factor}x after YouTube asked this PC to sign in)" if factor > 1 else ""))
        return 0.0, ""

    def block(self, error: str, now: float, s: Settings | None = None) -> None:
        self.log(record_block(self.db, error, now, (s or self.load()).cookies_browser))
        self.db.note("blocked", now)

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
        try:
            if (not forced and now - recent < max(10, s.research_interval) * 60
                    and history.last_scan.get("videos")):
                result = research.last_result(history)
                self.log("Using the Research tab's last check.")
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
                result = scan(opts, history, self.log, progress=lambda m, f: self.beat(),
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
        decisions = decide(result.videos, policy, picks, now=now, notes=result.notes,
                           lessons=learn.for_settings(s))
        added = 0
        self.db.set_meta("pick_note", "" if any(d.picked for d in decisions) else why_none(decisions))
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
            if self.db.add(job, now):
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
        self.db.update(job.id, now, status="working", stage="Starting", recipe=label, error="")
        self.db.set_meta("last_download", now)
        self.state(f"Clipping: {job.title or job.url}")
        self.log(f"Clipping {job.title or job.url} with {label}")

        last = {"t": 0.0, "msg": "", "video": ""}
        self.db.set_meta("working_video", "")

        def progress(msg: str, frac: float | None) -> None:
            t = self.now()
            if msg != last["msg"] or t - last["t"] >= 2:
                last.update(t=t, msg=msg)
                self.db.update(job.id, t, stage=msg + (f" {frac:.0%}" if frac is not None else ""))
            mark_busy()
            self.beat()

        def mark_busy() -> None:  # the video's real id, once known: a clean-up mustn't touch it
            if p.video is not None and p.video.id != last["video"]:
                last["video"] = p.video.id
                self.db.set_meta("working_video", p.video.id)

        def cancelled() -> bool:
            t = self.now()
            if t - self._last_flag_check < 2:
                return False
            self._last_flag_check = t
            return self.stop_requested()

        watch = Watchdog(s.watchdog_minutes * 60, lambda: (last["t"] or now), lambda: last["msg"], self.now)
        engine = self._engine(settings, log=self.log, progress=progress, cancelled=cancelled)
        p = Project(job.url)
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
            self.db.update(job.id, self.now(), status="queued", stage="Stopped; will start again")
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
        self.db.update(job.id, self.now(), status="done", stage="Done", folder=str(folder),
                       clips=made - rejected, rejected=rejected, title=title,
                       channel=p.video.channel if p.video else job.channel)
        self.log(f"✓ {made - rejected} clips from {title} → {folder}"
                 + (f" ({rejected} rejected by the quality check)" if rejected else ""))
        if job.order:
            self.order_progress(job.order)
        self.prune(s)

    def failed(self, job: Job, error: str, s: Settings) -> None:
        now = self.now()
        kind = classify(error, job.error)
        attempts = job.attempts + 1
        if kind == "blocked":
            self.block(error, now, s)
            self.db.update(job.id, now, status="queued", stage=SIGN_IN_WAIT,
                           error=error, next_try=self.db.meta_float("blocked_until"))
            return
        if kind == "permanent" or attempts >= s.auto_retries:
            stuck = error.startswith(STUCK)
            self.db.update(job.id, now, status="failed", attempts=attempts, error=error,
                           stage="Skipped: stuck twice" if stuck and kind == "permanent" else
                           "Gave up" if kind == "retry" else "Can't be clipped")
            self.log(f"✗ {job.title or job.url}: {error}" + (" (gave up)" if kind == "retry" else ""))
            self.db.set_meta("last_error", error)
            return
        wait = 60.0 if error.startswith(STUCK) else BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        self.db.update(job.id, now, status="queued", attempts=attempts, error=error, next_try=now + wait,
                       stage=f"Try {attempts + 1} in {_span(wait)}")
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
        import json

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
        self.health()
        stop = self._stop = stop or (lambda: False)
        try:
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
            self.log("Automation stopped.")
            self.release()
        return 0


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
    return Runner(log=file_logger()).run()


def request_scan(db: JobDB, niche_id: str = "") -> None:
    """Ask automation to check for videos now: for one niche, or all ("1"). Two different asks
    before the check happens make it a check of all of them."""
    asked = db.meta("scan_now")
    db.set_meta("scan_now", niche_id if niche_id and asked in ("", niche_id) else "1")
    db.set_meta("nudge", "1")  # a running automation looks now, not after its wait


def boot_time() -> float:
    """When this PC last started (a time.time()), or 0 when it can't be told. A heartbeat from before
    then is from a process that's gone, whatever has its process id now."""
    try:
        if sys.platform == "win32":
            import ctypes

            ticks = ctypes.WinDLL("kernel32").GetTickCount64
            ticks.restype = ctypes.c_ulonglong
            return time.time() - ticks() / 1000
        if sys.platform.startswith("linux"):
            with open("/proc/stat", encoding="ascii") as f:
                return float(next(line.split()[1] for line in f if line.startswith("btime")))
    except (OSError, AttributeError, StopIteration, ValueError):
        pass
    return 0.0


def process_alive(pid: str | int) -> bool:
    """Whether a process with this id is running on this PC. True when it can't be told."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, it's just not ours
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # it exists, but belongs to someone else
        return True
    return True


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


TASK_NAME = "ClipMonster Automation"


def schedule_command(install: bool) -> list[str]:
    """schtasks arguments that start (or stop starting) the runner when you sign in to Windows."""
    if not install:
        return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    cmd = launch_command()
    run = " ".join(f'"{c}"' if " " in c or not c.startswith("-") else c for c in cmd)
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", run, "/RL", "LIMITED", "/F"]


def set_start_with_windows(on: bool) -> str:
    """'' on success, else the error."""
    import subprocess

    if os.name != "nt":
        return "Start with Windows only works on Windows."
    try:
        r = subprocess.run(schedule_command(on), capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return str(exc)
    if r.returncode and not (not on and "cannot find" in (r.stderr + r.stdout).lower()):
        return (r.stderr or r.stdout).strip() or f"schtasks failed ({r.returncode})"
    return ""


def starts_with_windows() -> bool:
    import subprocess

    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0
