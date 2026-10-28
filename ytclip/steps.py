"""Status bars: which step a video is on, how far along the whole job is, and about how long is left.

The engine reports progress as a message and a fraction of the current stage ("Transcribing speech",
0.42). A Tracker turns that into a Status for a status bar: the step ("Transcribe", step 3 of 6),
the fraction of that step, the fraction of the whole job, and an estimate of the time left. Steps
are weighted by how long they took last time (automation keeps the last video's timings), so the
overall bar moves at an honest pace instead of jumping a sixth at a time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# (key, label), in the order a video goes through them
STEPS: tuple[tuple[str, str], ...] = (
    ("download", "Download"), ("analyse", "Listen"), ("transcript", "Transcribe"), ("find", "Find clips"),
    ("polish", "AI polish"), ("export", "Export"), ("check", "Quality check"),
)
LABELS = dict(STEPS)
ORDER = {key: i for i, (key, _) in enumerate(STEPS)}
# Seconds each step takes on a typical 20-minute video, until a real one has been timed.
DEFAULT_SECONDS = {"download": 60.0, "analyse": 25.0, "transcript": 120.0, "find": 20.0, "polish": 30.0,
                   "export": 150.0, "check": 15.0}
# engine.TIMED keys -> steps (audio and scene cuts are both "Listen")
TIMED_STEPS = {"download": "download", "audio": "analyse", "scenes": "analyse", "transcript": "transcript",
               "find": "find", "polish": "polish", "export": "export", "check": "check"}

# The start of each progress message the engine (and download/transcribe) sends, and its step.
MESSAGES: tuple[tuple[str, str], ...] = (
    ("Fetching video info", "download"), ("Downloading", "download"), ("Merging audio and video", "download"),
    ("Fetching captions", "download"), ("Downloaded already", "download"), ("Reading file", "download"),
    ("Measuring audio", "analyse"), ("Detecting scene cuts", "analyse"),
    ("Preparing audio for transcription", "transcript"), ("Loading Whisper", "transcript"),
    ("Detecting spoken language", "transcript"), ("Transcribing", "transcript"),
    ("AI is picking clips", "find"), ("Finding clips", "find"),
    ("AI is polishing", "polish"),
    ("Exporting clip", "export"), ("Making a clip again", "export"), ("Rendering", "export"),
    ("Checking clip", "check"),
)

MIN_ELAPSED = 8.0  # seconds before a time-left estimate is shown
MIN_DONE = 0.03  # ...and this much of the job done


def step_of(message: str) -> str:
    """The step a progress message belongs to, "" when it isn't one of a video's steps."""
    for start, key in MESSAGES:
        if message.startswith(start):
            return key
    return ""


def plan_for(s, url: bool = True, export: bool = True, prepare: bool = True) -> list[str]:
    """The steps a job will go through with these settings: preparing a video (download to AI polish),
    exporting it (export and the quality check), or both."""
    out: list[str] = []
    if prepare:
        out += ["download", "analyse"]
        if getattr(s, "transcribe", "auto") != "never":
            out.append("transcript")
        out.append("find")
        if getattr(s, "ai_polish", False):
            out.append("polish")
    if export:
        out.append("export")
        if getattr(s, "qc_enabled", False):
            out.append("check")
    return out


def weights_from_timings(timings: dict | None) -> dict[str, float]:
    """Seconds per step from a video's timings (engine.Project.timings), on top of the defaults."""
    out = dict(DEFAULT_SECONDS)
    seen: dict[str, float] = {}
    for key, seconds in (timings or {}).items():
        step = TIMED_STEPS.get(key)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            continue
        if step and seconds > 0:
            seen[step] = seen.get(step, 0.0) + seconds
    for step, seconds in seen.items():
        out[step] = max(1.0, seconds)
    return out


def clock_text(seconds: float) -> str:
    """'about 40 s left', 'about 3 min left', 'about 1 h 10 min left'."""
    seconds = max(0.0, seconds)
    if seconds < 55:
        return f"about {max(5, int(round(seconds / 5.0)) * 5)} s left"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} min left"
    return f"about {minutes // 60} h {minutes % 60:02d} min left" if minutes % 60 else f"about {minutes // 60} h left"


@dataclass
class Status:
    message: str = ""  # what it's doing, as the engine said it
    step: str = ""  # a STEPS key, "" when the job has no steps (a single task)
    index: int = 0  # 1-based place of the step among the job's steps
    count: int = 0  # how many steps the job has
    step_frac: float | None = None  # 0-1 through this step, None when it can't tell
    overall: float | None = None  # 0-1 through the whole job, None when it can't tell
    eta: float = 0.0  # seconds left (estimated), 0 = not known yet
    done: list[str] = field(default_factory=list)  # the steps already finished
    plan: list[str] = field(default_factory=list)  # every step of the job, in order

    @property
    def label(self) -> str:
        return LABELS.get(self.step, "")

    @property
    def percent(self) -> str:
        return f"{self.overall:.0%}" if self.overall is not None else ""

    @property
    def time_left(self) -> str:
        return clock_text(self.eta) if self.eta else ""

    def headline(self) -> str:
        """'Step 3 of 6 · Transcribe'."""
        return f"Step {self.index} of {self.count} · {self.label}" if self.step and self.count else self.message

    def detail(self) -> str:
        """'Transcribing speech 42%': the message with this step's own progress."""
        frac = f" {self.step_frac:.0%}" if self.step_frac is not None and "%" not in self.message else ""
        return f"{self.message}{frac}"

    def summary(self) -> str:
        """One line for a status bar or a table cell: 'Step 3 of 6 · Transcribing speech 42% · 61% · about 4 min left'."""
        bits = [f"Step {self.index}/{self.count}" if self.step and self.count else "", self.detail(),
                f"{self.percent} done" if self.overall is not None and self.step else "", self.time_left]
        return " · ".join(b for b in bits if b)

    def bar_text(self) -> str:
        """For inside a progress bar: '61% · about 4 min left'."""
        return " · ".join(b for b in (self.percent, self.time_left) if b)

    def to_json(self) -> dict:
        return {"message": self.message, "step": self.step, "index": self.index, "count": self.count,
                "step_frac": self.step_frac, "overall": self.overall, "eta": round(self.eta, 1), "done": self.done,
                "plan": self.plan}

    @classmethod
    def from_json(cls, d) -> "Status | None":
        if not isinstance(d, dict):
            return None
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        try:
            return cls(**known)
        except TypeError:
            return None


class Tracker:
    """Follows one job's progress messages; update() gives the Status to show.

    plan: the job's steps (plan_for); None for a job without steps, whose overall progress is just
    the fraction it reports. weights: seconds per step (weights_from_timings)."""

    def __init__(self, plan: list[str] | None = None, weights: dict[str, float] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.plan = list(plan or [])
        self.weights = weights or dict(DEFAULT_SECONDS)
        self.clock = clock
        self.start = clock()
        self.step = ""
        self.step_frac: float | None = None
        self._eta: float = 0.0
        self._eta_at = self.start

    def _weight(self, step: str) -> float:
        return max(0.5, float(self.weights.get(step, DEFAULT_SECONDS.get(step, 30.0))))

    def update(self, message: str, frac: float | None) -> Status:
        frac = None if frac is None or frac < 0 else min(1.0, float(frac))
        step = step_of(message) if self.plan else ""
        if step and step not in self.plan:
            if self.step and ORDER.get(step, 99) < ORDER.get(self.step, 99):
                step = ""  # part of the current step (Whisper typing out the clips while they're exported)
            else:  # a step the plan didn't expect (say, Whisper when there were no captions after all)
                self.plan.append(step)
                self.plan.sort(key=lambda k: ORDER.get(k, 99))
        if step and (not self.step or self.plan.index(step) > self.plan.index(self.step)):
            self.step, self.step_frac = step, None  # moved on (never back: Whisper typing out clips during
            # export is part of exporting)
        if step == self.step or not self.plan:
            self.step_frac = frac
        now = self.clock()
        overall = self._overall()
        return Status(message, self.step, self.plan.index(self.step) + 1 if self.step else 0, len(self.plan),
                      self.step_frac if (step == self.step or not self.plan) else None, overall,
                      self._estimate(overall, now), self.plan[:self.plan.index(self.step)] if self.step else [],
                      list(self.plan))

    def _overall(self) -> float | None:
        if not self.plan:
            return self.step_frac
        if not self.step:
            return 0.0
        total = sum(self._weight(k) for k in self.plan)
        before = sum(self._weight(k) for k in self.plan[:self.plan.index(self.step)])
        return min(1.0, (before + self._weight(self.step) * (self.step_frac or 0.0)) / total)

    def _estimate(self, overall: float | None, now: float) -> float:
        """Seconds left: time so far stretched by how much is left. Smoothed, and counting down
        between updates, so it doesn't jump about."""
        elapsed = now - self.start
        if overall is None or elapsed < MIN_ELAPSED or overall < MIN_DONE:
            return 0.0
        if overall >= 1.0:
            return 0.0
        raw = elapsed * (1 - overall) / overall
        if self._eta:
            ticked = max(0.0, self._eta - (now - self._eta_at))
            raw = 0.7 * ticked + 0.3 * raw
        self._eta, self._eta_at = raw, now
        return raw
