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

from . import learn, research
from .engine import Cancelled, Engine, Project
from .jobs import Job, JobDB
from .policy import PickLog, Policy, decide, picks_path
from .settings import Settings, all_layouts, all_presets, app_dir, find_layout
from .trending import CATEGORIES, History

Log = Callable[[str], None]

DAY = 86400.0
BACKOFF = (15 * 60, 3600, 4 * 3600)  # wait before the 2nd, 3rd, 4th... try
BLOCK_PAUSE = 6 * 3600  # how long downloads pause after YouTube asks the PC to sign in
HEARTBEAT_STALE = 180  # a runner silent for this long is gone
IDLE = 300  # longest sleep between checks

# yt-dlp errors that mean YouTube is pushing back on this PC: stop downloading for a while.
_BLOCKED = re.compile(r"sign in to confirm|not a bot|http error 429|too many requests|rate.?limit", re.I)
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
    if _BLOCKED.search(error):
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

    def matches(self, job: Job) -> bool:
        m = self.match.strip().lower()
        if m in ("*", "any", "everything"):
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
    """The settings a job is clipped with, and a label saying which recipe gave them."""
    recipes, _ = parse_recipes(s.auto_recipes)
    rule = next((r for r in recipes if r.matches(job)), None)
    preset = (rule.preset if rule and rule.preset else s.auto_preset)
    view = (rule.view if rule and rule.view else s.auto_layout)
    out = s
    presets = all_presets()
    if preset in presets:
        out = out.apply(presets[preset])
    if view:
        name = find_layout(view)
        if name:
            out = out.apply(all_layouts()[name])
    overrides = {"output_dir": str(s.auto_output_path), "cache_dir": str(s.cache_path)}
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
    if rule:
        label += f" (recipe: {rule.match})"
    return out, label


# ---------------------------------------------------------------- the runner


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
            now = self.now()
            next_scan = now + max(10, s.research_interval) * 60
        elif not research.youtube_key(s) and not self.db.meta("warned_key"):
            self.log("No YouTube Data API key, so nothing new is picked. Queued videos are still clipped.")
            self.db.set_meta("warned_key", "1")

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

    def wait_reason(self, s: Settings, now: float) -> tuple[float, str]:
        blocked = self.db.meta_float("blocked_until")
        if now < blocked:
            until = time.strftime("%H:%M", time.localtime(blocked))
            return blocked - now, f"Downloads paused until {until}: YouTube asked this PC to sign in"
        wait = hours_wait(now, s.auto_start_hour, s.auto_end_hour)
        if wait:
            return wait, f"Outside working hours ({s.auto_start_hour}:00–{s.auto_end_hour}:00)"
        gap = self.db.meta_float("last_download") + s.auto_gap_minutes * 60 - now
        if gap > 0:
            return gap, f"Spacing downloads out: next in {max(1, round(gap / 60))} min"
        return 0.0, ""

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
                result = scan(research.ScanOptions.from_settings(s), history, self.log, now=now)
                self.log(f"Checked {len(result.videos)} videos ({result.quota} quota units).")
                for p in result.problems:
                    self.log(f"Research: {p}")
        except Exception as exc:
            self.log(f"⚠ Research failed: {exc}")
            self.db.set_meta("last_error", f"Research: {exc}")
            return 0
        picks = self._picks()
        decisions = decide(result.videos, Policy.from_settings(s), picks, now=now, notes=result.notes,
                           lessons=learn.for_settings(s))
        added = 0
        for d in decisions:
            if not d.picked:
                continue
            v = d.video
            job = Job(id=v.id, url=v.url, title=v.title, channel=v.channel, channel_id=v.channel_id,
                      category=v.category, permission=d.reason, origins=list(v.origins), how="auto",
                      stage="Waiting")
            if self.db.add(job, now):
                added += 1
                self.log(f"Picked: {v.title} ({v.channel}, clip-ability {v.score:.0f}). {d.reason}"
                         + (f" {d.learned}" if d.learned else ""))
            picks.record(d, "auto", now)
            history.mark_sent(v.id)
        if added:
            picks.save()
            history.save()
        return added

    def add_manual(self, url: str, title: str = "") -> bool:
        from .download import is_url

        if not is_url(url):
            raise ValueError("Give a video link (https://…).")
        vid = _video_id(url)
        return self.db.add(Job(id=vid, url=url, title=title or url, permission="Added by you", how="manual",
                               stage="Waiting"), self.now())

    # ------------------------------------------------------------ one video
    def process(self, job: Job, s: Settings) -> None:
        now = self.now()
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

        last = {"t": 0.0, "msg": ""}

        def progress(msg: str, frac: float | None) -> None:
            t = self.now()
            if msg != last["msg"] or t - last["t"] >= 2:
                last.update(t=t, msg=msg)
                self.db.update(job.id, t, stage=msg + (f" {frac:.0%}" if frac is not None else ""))
            self.beat()

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
        try:
            engine.prepare(p)
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
            until = now + BLOCK_PAUSE
            self.db.set_meta("blocked_until", until)
            self.db.update(job.id, now, status="queued", stage="Waiting: YouTube asked this PC to sign in",
                           error=error, next_try=until)
            self.log(f"⚠ YouTube is asking this PC to sign in. Downloads are paused for "
                     f"{BLOCK_PAUSE // 3600} hours. ({error})")
            self.db.set_meta("last_error", error)
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

    # ------------------------------------------------------------ the loop
    def run(self, stop: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep,
            once: bool = False) -> int:
        why = self.claim()
        if why:
            self.log(why)
            return 1
        self.log(f"Automation started (process {self.pid}).")
        stop = stop or (lambda: False)
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
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})", url)
    return m.group(1) if m else url.strip()


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
