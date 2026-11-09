"""The connection to one PC while the window is open: background requests, the polling that keeps the window
up to date, and the news feed (events) the PC sends.

Every request runs on a worker thread (Tasks), and its answer comes back on the window's thread through a Qt
signal, so the window never waits on the network. The status is asked for every few seconds and the videos
list when its revision changes; news (a post going up, a problem) arrives through a long poll on a thread of
its own.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from . import hosts
from .client import ApiError, Client, Offline, WrongPC

STATUS_EVERY = 3000  # ms
VIDEOS_EVERY = 6000
EVENTS_WAIT = 12.0  # s a long poll waits (under the client's timeout)


class Tasks(QObject):
    """Run a function on a worker thread; its result (or error) is handed to a callback on the Qt thread."""

    _done = Signal(object, object, object, object)  # ok, fail, result, error

    def __init__(self, workers: int = 6, parent=None):
        super().__init__(parent)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="remote")
        self._done.connect(self._deliver)

    def run(self, fn: Callable, ok: Callable | None = None, fail: Callable | None = None) -> None:
        def work():
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - handed to `fail`
                self._done.emit(ok, fail, None, exc)
                return
            self._done.emit(ok, fail, result, None)

        self.pool.submit(work)

    def _deliver(self, ok, fail, result, error) -> None:
        if error is not None:
            if fail is not None:
                fail(error)
        elif ok is not None:
            ok(result)

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


class Session(QObject):
    """One linked PC. Signals carry what the window shows."""

    status = Signal(dict)
    videos = Signal(list)
    event = Signal(dict)
    connection = Signal(str, str)  # "online" | "offline" | "unlinked" | "wrong_pc", and what to say

    def __init__(self, host: hosts.Host, tasks: Tasks, parent=None):
        super().__init__(parent)
        self.host = host
        self.tasks = tasks
        self.client = Client(host.address, host.port, host.fp, host.device_id, host.token())
        self.state = "connecting"
        self.last_status: dict = {}
        self.last_videos: list = []
        self._videos_rev = ""
        self._busy = {"status": False, "videos": False}
        self._stop = threading.Event()
        self._events_thread: threading.Thread | None = None
        self._seq = -1
        self.status_timer = QTimer(self)
        self.status_timer.setInterval(STATUS_EVERY)
        self.status_timer.timeout.connect(self.refresh_status)
        self.videos_timer = QTimer(self)
        self.videos_timer.setInterval(VIDEOS_EVERY)
        self.videos_timer.timeout.connect(self.refresh_videos)

    # --- running -----------------------------------------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self.refresh_status()
        self.refresh_videos(force=True)
        self.status_timer.start()
        self.videos_timer.start()
        self._events_thread = threading.Thread(target=self._events_loop, name="remote-events", daemon=True)
        self._events_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status_timer.stop()
        self.videos_timer.stop()
        self.client.close()

    def call(self, fn: Callable[[Client], object], ok: Callable | None = None, fail: Callable | None = None) -> None:
        """Run fn(client) on a worker thread. A lost connection is reported on `connection` too."""
        def failed(exc):
            self._failed(exc)
            if fail is not None:
                fail(exc)

        self.tasks.run(lambda: fn(self.client), ok, failed)

    def _failed(self, exc: Exception) -> None:
        if isinstance(exc, WrongPC):
            self._set("wrong_pc", exc.message)
        elif isinstance(exc, Offline):
            self._set("offline", exc.message)
        elif isinstance(exc, ApiError) and exc.status == 401:
            self._set("unlinked", exc.message)

    def _set(self, state: str, text: str = "") -> None:
        if state != self.state or state != "online":
            self.state = state
            self.connection.emit(state, text)

    # --- keeping up to date ---------------------------------------------------------------------------------

    def refresh_status(self) -> None:
        if self._busy["status"]:
            return
        self._busy["status"] = True

        def ok(st):
            self._busy["status"] = False
            if self.state != "online":
                self.host.seen_at = time.time()
                try:
                    hosts.remember(self.host)
                except OSError:
                    pass
            self._set("online")
            self.last_status = st
            self.status.emit(st)
            if st.get("videos_rev") and st["videos_rev"] != self._videos_rev:
                self.refresh_videos()

        def fail(_exc):
            self._busy["status"] = False

        self.call(lambda c: c.get("/api/status"), ok, fail)

    def refresh_videos(self, force: bool = False) -> None:
        if self._busy["videos"]:
            return
        self._busy["videos"] = True
        known = "" if force else self._videos_rev

        def ok(got):
            self._busy["videos"] = False
            self._videos_rev = got.get("rev") or ""
            if not got.get("same"):
                self.last_videos = got.get("items") or []
                self.videos.emit(self.last_videos)

        def fail(_exc):
            self._busy["videos"] = False

        self.call(lambda c: c.get("/api/videos", rev=known) if known else c.get("/api/videos"), ok, fail)

    def _events_loop(self) -> None:
        """The news feed: one long poll at a time, on its own connection."""
        client = Client(self.host.address, self.host.port, self.host.fp, self.host.device_id, self.client.token,
                        timeout=EVENTS_WAIT + 10)
        backoff = 2.0
        while not self._stop.is_set():
            try:
                if self._seq < 0:  # the news so far, shown but not announced
                    first = client.get("/api/events")
                    self._seq = int(first.get("seq") or 0)
                    for e in (first.get("events") or [])[-12:]:
                        if not self._stop.is_set():
                            self.event.emit({**e, "old": True})
                got = client.get("/api/events", after=self._seq, wait=EVENTS_WAIT)
                backoff = 2.0
            except ApiError:
                self._stop.wait(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            seq = int(got.get("seq") or 0)
            if seq < self._seq:  # the PC's automation restarted: its news starts again from 0
                self._seq = seq
                continue
            for e in got.get("events") or []:
                self._seq = max(self._seq, int(e.get("seq") or 0))
                if not self._stop.is_set():
                    self.event.emit(e)
            self._seq = max(self._seq, seq)
        client.close()
