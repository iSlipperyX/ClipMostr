"""Background work on QThreads, run one at a time from a queue."""

from __future__ import annotations

import traceback
from collections import deque
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from .. import steps
from ..engine import Cancelled, Engine
from ..settings import Settings


class Task(QThread):
    log = Signal(str)
    progress = Signal(str, float)  # message, 0-1 or -1 for "busy"
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, label: str, fn: Callable[["Task"], Any], owner: Any = None, plan: list[str] | None = None):
        super().__init__()
        self.label = label
        self.fn = fn
        self.owner = owner  # usually the Project this task works on
        self.cancel_requested = False
        # For the status bars: the steps it goes through (steps.plan_for), or None for a single step.
        self.tracker = steps.Tracker(plan)

    def status(self, msg: str, frac: float) -> "steps.Status":
        """Where this task is, from a progress signal (frac -1 = can't tell)."""
        return self.tracker.update(msg, None if frac < 0 else frac)

    def engine(self, settings: Settings) -> Engine:
        return Engine(settings, log=self.log.emit,
                      progress=lambda m, f: self.progress.emit(m, -1.0 if f is None else float(f)),
                      cancelled=lambda: self.cancel_requested)

    def run(self) -> None:
        try:
            self.succeeded.emit(self.fn(self))
        except Cancelled:
            self.failed.emit("Cancelled")
        except Exception as exc:
            self.log.emit(traceback.format_exc().strip().splitlines()[-1])
            self.failed.emit(str(exc) or exc.__class__.__name__)


class TaskQueue(QObject):
    """Runs tasks sequentially so downloads/encodes don't fight for the disk and CPU."""

    started = Signal(object)
    finished = Signal(object)
    idle = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pending: deque[Task] = deque()
        self.current: Task | None = None

    def add(self, task: Task) -> Task:
        self.pending.append(task)
        self._next()
        return task

    def busy(self) -> bool:
        return self.current is not None

    def cancel_current(self) -> None:
        if self.current:
            self.current.cancel_requested = True

    def cancel_all(self) -> None:
        self.pending.clear()
        self.cancel_current()

    def _next(self) -> None:
        if self.current or not self.pending:
            if not self.current and not self.pending:
                self.idle.emit()
            return
        task = self.pending.popleft()
        self.current = task
        task.finished.connect(lambda t=task: self._done(t))
        self.started.emit(task)
        task.start()

    def _done(self, task: Task) -> None:
        self.current = None
        self.finished.emit(task)
        task.deleteLater()
        self._next()
