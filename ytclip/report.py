"""The morning report: what automation did while you were away.

Built from jobs.db (the queue, its clip requests and the events the runner notes) and the
clips.json of each video it finished, so it needs no log reading and works with the app closed:

* clips made per niche, and per clip request;
* clips the quality check rejected, and the reasons most often given;
* videos that failed, with their errors;
* pauses: YouTube asking the PC to sign in, the disk running low, stuck work the watchdog stopped;
* space Clean up freed.

Shown on the Automation tab's Overnight page and by `ytclip report`.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import niches
from .cleanup import size_text
from .jobs import JobDB
from .orders import STATUS_WORDS
from .settings import Settings

HOUR = 3600.0


@dataclass
class NicheLine:
    name: str
    account: str = ""
    videos: int = 0
    clips: int = 0
    rejected: int = 0


@dataclass
class RequestLine:
    title: str
    made: int
    count: int
    status: str
    message: str = ""


@dataclass
class Report:
    since: float
    until: float
    videos: int = 0  # finished
    clips: int = 0  # ready to upload
    rejected: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (video, error)
    waiting: int = 0
    niches: list[NicheLine] = field(default_factory=list)
    requests: list[RequestLine] = field(default_factory=list)
    reasons: list[tuple[str, int]] = field(default_factory=list)  # quality check problems, commonest first
    blocks: int = 0  # times YouTube asked the PC to sign in
    disk_pauses: int = 0
    watchdog: int = 0  # stuck exports, measurements or checks stopped
    freed: int = 0  # bytes
    state: str = ""  # what automation is doing now

    @property
    def hours(self) -> float:
        return (self.until - self.since) / HOUR

    def to_json(self) -> dict:
        return asdict(self)

    def headline(self) -> str:
        if not (self.videos or self.failed or self.blocks or self.disk_pauses):
            return f"Nothing was clipped in the last {_span(self.hours)}."
        parts = [f"{self.clips} clip{'s' if self.clips != 1 else ''} ready from {self.videos} "
                 f"video{'s' if self.videos != 1 else ''}"]
        if self.rejected:
            parts.append(f"{self.rejected} rejected")
        if self.failed:
            parts.append(f"{len(self.failed)} video{'s' if len(self.failed) != 1 else ''} failed")
        return f"Last {_span(self.hours)}: " + ", ".join(parts) + "."

    def lines(self) -> list[str]:
        """The report in plain lines, most useful first."""
        out = [self.headline()]
        for n in self.niches:
            if n.videos or n.clips:
                out.append(f"{n.name}{f' ({n.account})' if n.account else ''}: {n.clips} clips from {n.videos} "
                           f"video{'s' if n.videos != 1 else ''}" + (f", {n.rejected} rejected" if n.rejected else ""))
        for r in self.requests:
            out.append(f"Request, {r.title}: {r.made} of {r.count} ready ({STATUS_WORDS.get(r.status, r.status)})")
        if self.reasons:
            out.append("Rejected because: " + "; ".join(f"{why} ({n})" for why, n in self.reasons[:4]))
        for title, error in self.failed[:5]:
            out.append(f"Failed: {title}: {error}")
        if len(self.failed) > 5:
            out.append(f"…and {len(self.failed) - 5} more failed")
        if self.blocks:
            out.append(f"YouTube asked the PC to sign in {self.blocks} time{'s' if self.blocks != 1 else ''}, so "
                       "automation paused itself (Sign in to YouTube in Automation helps)")
        if self.disk_pauses:
            out.append(f"Paused {self.disk_pauses} time{'s' if self.disk_pauses != 1 else ''} because the disk was "
                       "nearly full")
        if self.watchdog:
            out.append(f"Stopped {self.watchdog} stuck job{'s' if self.watchdog != 1 else ''} (no progress for too "
                       "long) and tried again")
        if self.freed:
            out.append(f"Clean up freed {size_text(self.freed)}")
        if self.waiting:
            out.append(f"{self.waiting} video{'s' if self.waiting != 1 else ''} still waiting"
                       + (f" · now: {self.state}" if self.state else ""))
        return out


def _span(hours: float) -> str:
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"


def _short(error: str, n: int = 160) -> str:
    error = " ".join((error or "").split())
    return error if len(error) <= n else error[: n - 1] + "…"


_REASONS = (("AI rated", "the AI rated it low"), ("Came out", "wrong length or size"), ("No sound", "no sound"),
            ("Silent", "no sound"), ("The picture is", "black or frozen picture"),
            ("The file can't", "the file couldn't be read"))


def reason(problem: str) -> str:
    """A quality check problem as a reason that groups with others like it."""
    return next((label for start, label in _REASONS if problem.startswith(start)), problem.split(":")[0])


def _reasons(folder: str) -> list[str]:
    """The quality check's problems for the rejected clips in a video folder's clips.json."""
    try:
        data = json.loads((Path(folder) / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    out = []
    for c in data.get("clips") or []:
        qc = c.get("qc") if isinstance(c, dict) else None
        if isinstance(qc, dict) and qc.get("ok") is False:
            out += [reason(str(p)) for p in qc.get("problems") or []]
    return out


def build(db: JobDB, s: Settings, hours: float = 12.0, now: float | None = None) -> Report:
    now = time.time() if now is None else now
    since = now - hours * HOUR
    r = Report(since=since, until=now, state=db.meta("state"))
    all_niches = niches.load(s)
    by_niche = {n.id: NicheLine(n.name, n.account) for n in all_niches}
    other = NicheLine("No niche")
    reasons: Counter = Counter()
    for j in db.jobs_since(since):
        if j.status == "done":
            r.videos += 1
            r.clips += j.clips
            r.rejected += j.rejected
            line = by_niche.get(j.niche, other)
            line.videos += 1
            line.clips += j.clips
            line.rejected += j.rejected
            if j.rejected and j.folder:
                reasons.update(_reasons(j.folder))
        elif j.status == "failed":
            r.failed.append((j.title or j.url, _short(j.error)))
    r.niches = [x for x in by_niche.values() if x.videos] + ([other] if other.videos and all_niches else [])
    r.reasons = reasons.most_common()
    counts = db.counts()
    r.waiting = counts.get("queued", 0) + counts.get("working", 0)
    names = {n.id: n.name for n in all_niches}
    for o in db.orders():
        if o.updated >= since or o.is_open:
            r.requests.append(RequestLine(o.title(names.get(o.niche, "")), o.made, o.count, o.status, o.message))
    for _, kind in db.events_since(since):
        if kind == "blocked":
            r.blocks += 1
        elif kind == "disk_low":
            r.disk_pauses += 1
        elif kind == "watchdog":
            r.watchdog += 1
        elif kind.startswith("freed:"):
            try:
                r.freed += int(kind.split(":", 1)[1])
            except ValueError:
                pass
    return r
