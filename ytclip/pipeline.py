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

from . import learn, niches, research
from .download import is_blocked
from .engine import Cancelled, Engine, Project
from .jobs import Job, JobDB
from .policy import PickLog, Policy, decide, picks_path
from .settings import Settings, all_layouts, all_presets, app_dir, find_layout
from .trending import CATEGORIES, History

Log = Callable[[str], None]

DAY = 86400.0
BACKOFF = (15 * 60, 3600, 4 * 3600)  # wait before the 2nd, 3rd, 4th... try
BLOCK_PAUSE = 6 * 3600  # how long downloads pause after YouTube asks the PC to sign in
CALM_DAY = DAY  # after this long with no sign-in request, downloads go back to the usual spacing
DEEP_PAUSE = 20.0  # seconds between automation's most-replayed checks
HEARTBEAT_STALE = 180  # a runner silent for this long is gone
IDLE = 300  # longest sleep between checks

# Errors that won't change however often the video is tried.
_PERMANENT = re.compile(r"video unavailable|private video|has been removed|no longer available|copyright|"
                        r"members[- ]only|join this channel|age[- ]restricted|confirm your age|"
                        r"premieres in|this live event|no clips", re.I)


class Stopped(Exception):
    """The runner was asked to stop while working on a video."""


class NoClips(RuntimeError):
    pass


def classify(error: str) -> str:
    """'blocked' | 'permanent' | 'retry' for a failed video's error message."""
    if is_blocked(error):
        return "blocked"
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


def settings_for(job: Job, s: Settings) -> tuple[Settings, str]:
    """The settings a job is clipped with, and a label saying what gave them: a recipe matching
    the video's channel or category comes first, then the style of its niche, then a catch-all
    recipe (*), then Automation's. A niche's clips go to its own folder inside the Automation folder."""
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
    overrides = {"output_dir": str(folder), "cache_dir": str(s.cache_path)}
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
    if niche and niche.instructions.strip():
        overrides["ai_instructions"] = (f"{out.ai_instructions.strip()}\nThese clips are for a TikTok account about "
                                        f"{niche.name}: {niche.instructions.strip()}").strip()
    if s.auto_captions:
        overrides["burn_captions"] = True
    if s.auto_bleep:
        overrides["caption_censor"] = True
    if s.auto_delete_sources:
        overrides["keep_source"] = False
    overrides["qc_enabled"] = s.auto_qc
    overrides["ai_polish"] = s.auto_polish and bool(s.ai_api_key.strip() or os.environ.get("ANTHROPIC_API_KEY"))
    out = out.apply(overrides)
    label = " · ".join(x for x in (preset or "My settings", view) if x)
    why = [f"niche: {niche.name}"] if niche else []
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


def gap_factor(db: JobDB, now: float) -> int:
    """Downloads are spaced out twice as much after each sign-in request (up to 4 times), until a
    day passes without one."""
    if now - db.meta_float("blocked_at") > BLOCK_PAUSE + CALM_DAY:
        return 1
    return max(1, min(4, int(db.meta_float("gap_factor") or 1)))


def record_block(db: JobDB, error: str, now: float) -> str:
    """YouTube asked this PC to sign in: stop all requests to it for a while, and slow down after.
    Shared by automation and the Research tab. Returns the line to log."""
    recent = now - db.meta_float("blocked_at") <= BLOCK_PAUSE + CALM_DAY
    factor = min(4, gap_factor(db, now) * 2) if recent else 2
    until = now + BLOCK_PAUSE
    db.set_meta("blocked_until", until)
    db.set_meta("blocked_at", now)
    db.set_meta("gap_factor", factor)
    db.set_meta("last_error", error)
    return (f"⚠ YouTube asked this PC to sign in, so ClipMonster stops asking it for anything until "
            f"{time.strftime('%H:%M', time.localtime(until))} and then spaces downloads {factor}x further "
            f"apart for a day. Nothing needs doing; it carries on by itself. ({error})")


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
            return f"Automation is already running (process {other})."
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

        job = self.db.next_due(now)
        if job:
            wait, why = self.wait_reason(s, now)
            if wait:
                self.state(why)
                return min(wait, next_scan - now, IDLE)
            self.process(job, s)
            return 1.0
        upcoming = self.db.next_try()
        self.state("Waiting for the next check" if research.youtube_key(s) else "Waiting for videos")
        return max(1.0, min(next_scan - now, (upcoming - now) if upcoming else IDLE, IDLE))

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

    def block(self, error: str, now: float) -> None:
        self.log(record_block(self.db, error, now))

    # ------------------------------------------------------------ research → queue
    def research(self, s: Settings) -> int:
        """Scan (or reuse a scan the app made recently), run the picking rules, queue the picks."""
        now = self.now()
        self.state("Checking what to clip")
        self.db.set_meta("last_scan", now)
        history = self._history()
        recent = history.last_scan.get("time", 0)
        try:
            if now - recent < max(10, s.research_interval) * 60 and history.last_scan.get("videos"):
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
                    self.block(result.blocked, self.now())
        except research.ScanCancelled:
            raise Stopped()
        except Exception as exc:
            self.log(f"⚠ Research failed: {exc}")
            self.db.set_meta("last_error", f"Research: {exc}")
            return 0
        picks = self._picks()
        s = self.load()  # a scan takes minutes: niches and rules may have changed since it started
        niches.assign_from_settings(result.videos, s, history)
        decisions = decide(result.videos, Policy.from_settings(s), picks, now=now, notes=result.notes,
                           lessons=learn.for_settings(s))
        added = 0
        for d in decisions:
            if not d.picked:
                continue
            v = d.video
            job = Job(id=v.id, url=v.url, title=v.title, channel=v.channel, channel_id=v.channel_id,
                      category=v.category, permission=d.reason, origins=list(v.origins), how="auto",
                      stage="Waiting", niche=v.niche)
            if self.db.add(job, now):
                added += 1
                niche = niches.name_of(s, v.niche)
                self.log(f"Picked{' for ' + niche if niche else ''}: {v.title} ({v.channel}, clip-ability "
                         f"{v.score:.0f}). {d.reason}" + (f" {d.learned}" if d.learned else ""))
            picks.record(d, "auto", now)
            history.mark_sent(v.id)
        if added:
            picks.save()
            history.save()
        return added

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
        settings, label = settings_for(job, s)
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

        engine = self._engine(settings, log=self.log, progress=progress, cancelled=cancelled)
        p = Project(job.url)
        p.meta = {"automation": {"permission": job.permission, "origins": job.origins, "how": job.how,
                                 "recipe": label, "picked_at": job.created}}
        niche = niches.find(s, job.niche)
        if niche:  # for the Library, and for TikTok Manager to know which account the clips are for
            p.meta["automation"] |= {"niche": niche.name, "niche_id": niche.id, "account": niche.account}
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
            self.db.update(job.id, self.now(), status="queued", stage="Stopped; will start again")
            self.log(f"Stopped while clipping {job.title}; it'll start again next time.")
            raise Stopped()
        except Exception as exc:
            self.failed(job, str(exc) or exc.__class__.__name__, s)
            return
        title = p.video.title if p.video else job.title
        made = len([c for c in p.clips if c.enabled])
        rejected = p.qc.get("rejected", 0) if p.qc else 0
        self.db.update(job.id, self.now(), status="done", stage="Done", folder=str(folder),
                       clips=made - rejected, rejected=rejected, title=title,
                       channel=p.video.channel if p.video else job.channel)
        self.log(f"✓ {made - rejected} clips from {title} → {folder}"
                 + (f" ({rejected} rejected by the quality check)" if rejected else ""))
        self.prune(s)

    def failed(self, job: Job, error: str, s: Settings) -> None:
        now = self.now()
        kind = classify(error)
        attempts = job.attempts + 1
        if kind == "blocked":
            self.block(error, now)
            self.db.update(job.id, now, status="queued", stage="Waiting: YouTube asked this PC to sign in",
                           error=error, next_try=self.db.meta_float("blocked_until"))
            return
        if kind == "permanent" or attempts >= s.auto_retries:
            self.db.update(job.id, now, status="failed", attempts=attempts, error=error,
                           stage="Gave up" if kind == "retry" else "Can't be clipped")
            self.log(f"✗ {job.title or job.url}: {error}" + (" (gave up)" if kind == "retry" else ""))
            self.db.set_meta("last_error", error)
            return
        wait = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
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
            for f in s.cache_path.glob(f"{glob_escape(job.id)}.*"):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
            self.db.update(job.id, job.updated, pruned=1)
        return removed

    def cleanup(self, s: Settings) -> None:
        """The daily clean-up (Settings → Automation → Clean up by itself): what's ticked there and
        older than its days (at least one). Never a video that's queued, being clipped or in the
        Studio, or a clip TikTok Manager still has waiting."""
        from . import cleanup

        now = self.now()
        self.db.set_meta("last_cleanup", now)
        kinds = cleanup.parse_kinds(s.cleanup_kinds)
        if not kinds:
            return
        try:
            busy = cleanup.busy_ids(self.db)
            plan = cleanup.plan(s, now, busy=busy, older_than_days=max(1, s.cleanup_days), kinds=kinds)
            result = cleanup.apply(plan, kinds)
        except Exception as exc:  # a clean-up problem must never stop the clipping
            self.log(f"⚠ Clean up failed: {exc}")
            return
        if result.removed:
            self.log(f"Cleaned up: {result.summary()}.")
        for path, error in result.failed[:5]:
            self.log(f"⚠ Couldn't remove {path}: {error}")

    # ------------------------------------------------------------ the loop
    def run(self, stop: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep,
            once: bool = False) -> int:
        why = self.claim()
        if why:
            self.log(why)
            return 1
        self.log(f"Automation started (process {self.pid}).")
        stop = self._stop = stop or (lambda: False)
        try:
            while True:
                try:
                    wait = self.tick()
                except Stopped:
                    break
                if once and (wait > 1.0 or not self.db.next_due(self.now())):
                    break
                waited = 0.0
                while waited < wait:
                    if stop() or self.stop_requested():
                        raise Stopped()
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


def runner_alive(db: JobDB, now: float | None = None) -> bool:
    now = now or time.time()
    return bool(db.meta("runner_pid")) and now - db.meta_float("heartbeat") < HEARTBEAT_STALE


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
