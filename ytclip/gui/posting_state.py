"""The window's side of posting: the shared library and accounts, and asking the posting worker to do things.

The posting worker (posting/service.py) runs in automation's background process and does all the TikTok
work: posting, reading stats, logging in. The window shows the same files (they merge, so changes made
on either side are kept) and asks the worker for things through posting/actions.db. While automation
isn't running, the window runs a worker of its own that only does what it's asked (posting=False), so
the Log in and Test posting buttons still work; it stops once automation starts.
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from ..posting import actions as actions_mod
from ..posting import live
from ..posting.accounts import Account, Accounts
from ..posting.config import Settings, app_dir
from ..posting.store import Store

REFRESH_MS = 2000


class PostingState(QObject):
    changed = Signal()  # the library or accounts changed (here, or in the worker)
    accounts_changed = Signal()  # an account was added, removed or renamed
    request_done = Signal(object)  # an actions.Request this window asked for has finished
    request_log = Signal(object)  # a request this window asked for wrote a new line
    alert = Signal(str, str)  # (title, text) worth a notification: posted, failed, logged out, a hit
    sign_in_needed = Signal(str)  # an account's TikTok sign-in ran out (its id): the window offers to redo it
    message = Signal(str)  # a line for the window's log

    def __init__(self, parent=None, worker_alive=None, start_timer: bool = True):
        super().__init__(parent)
        self.settings = Settings.load()
        self.store = Store()
        self.accounts = Accounts.load()
        self.actions = actions_mod.Actions()
        self._worker_alive = worker_alive or _automation_running
        self._mine: dict[int, tuple[str, int]] = {}  # request id -> (state, log length) last seen
        self._seen_events: dict[str, float] = {}  # account id -> newest feed event already announced
        self._local = None  # the window's own worker thread (while automation isn't running)
        self._local_lock = threading.Lock()
        if self.store.adopt(self.accounts.ids()):
            self.store.save()
        for a in self.accounts:
            self._seen_events[a.id] = _newest_event(a.id)
        self.timer = QTimer(self, interval=REFRESH_MS)
        self.timer.timeout.connect(self.poll)
        if start_timer:
            self.timer.start()

    # --- reading and saving -------------------------------------------------------------

    def poll(self) -> None:
        """Take in what the worker saved, follow this window's requests and the feeds' alerts."""
        self.settings = Settings.load()
        try:
            got = self.store.refresh()
            acc = self.accounts.refresh()
        except (OSError, TimeoutError):
            got = acc = False
        if acc:
            self.accounts_changed.emit()
        if got or acc:
            self.changed.emit()
        self._follow_requests()
        self._follow_alerts()
        self._manage_local()

    def save(self) -> None:
        try:
            self.store.save()
            self.accounts.save()
        except (OSError, TimeoutError) as exc:
            self.message.emit(f"⚠ Couldn't save the posting library: {exc}")
        self.changed.emit()

    def save_settings(self) -> None:
        self.settings.save()

    # --- accounts -----------------------------------------------------------------------

    def add_account(self, name: str = "") -> Account:
        src = self.accounts.items[-1] if len(self.accounts) else None
        a = self.accounts.add(name, self.settings, copy_schedule_from=src)
        self._seen_events[a.id] = time.time()
        self.save()
        self.accounts_changed.emit()
        self.message.emit(f"Added an account{f' “{a.name}”' if a.name else ''}. Log it in to TikTok next.")
        return a

    def remove_account(self, account_id: str) -> None:
        a = self.accounts.remove(account_id)
        if a is None:
            return
        self.store.forget_account(a.id)
        self.save()
        live.EventLog.remove(live.EventLog.path_for(a.id))
        # A browser profile can be a few hundred MB: deleted off the window's thread.
        threading.Thread(target=Accounts.delete_login, args=(a,), daemon=True).start()
        self.accounts_changed.emit()
        self.message.emit(f"Removed {a.label}, and its TikTok login on this PC.")

    def settings_for(self, account: Account) -> Settings:
        return account.apply(self.settings)

    # --- asking the worker --------------------------------------------------------------

    def worker_running(self) -> bool:
        """Automation (with its posting worker) is running in the background."""
        try:
            return bool(self._worker_alive())
        except Exception:  # noqa: BLE001
            return False

    def ask(self, kind: str, account: str = "", item: str = "", **args) -> int:
        """Ask the worker for something; request_done says when it's finished."""
        self.save()  # what you just changed (a caption, a schedule) is what it works with
        rid = self.actions.submit(kind, account=account, item=item, **args)
        self._mine.setdefault(rid, ("new", 0))
        self._manage_local()
        return rid

    def cancel(self, request_id: int) -> None:
        self.actions.ask_cancel(request_id)

    def finish(self, request_id: int) -> None:
        """'I'm logged in': the login window closes and the login is checked."""
        self.actions.ask_finish(request_id)

    def busy(self, account_id: str) -> actions_mod.Request | None:
        """What the worker is doing (or about to do) for this account at the window's request."""
        return next(iter(self.actions.active(account_id)), None)

    def _follow_requests(self) -> None:
        for rid, (state, logged) in list(self._mine.items()):
            req = self.actions.get(rid)
            if req is None:
                del self._mine[rid]
                continue
            if len(req.log) != logged:
                self.request_log.emit(req)
            if req.done:
                del self._mine[rid]
                if state != req.state:
                    self.request_done.emit(req)
                continue
            self._mine[rid] = (req.state, len(req.log))

    def _manage_local(self) -> None:
        """Run the window's own worker while automation isn't running and something was asked for."""
        with self._local_lock:
            running = self._local is not None and self._local.running
            if self.worker_running():
                if running:
                    self._local.stop(timeout=0)
                    self._local = None
                return
            if running or not self._mine:
                return
            from ..posting.service import Service, Thread

            svc = Service(log=self.message.emit, posting=False, status_file=app_dir() / "status-window.json")
            self._local = Thread(svc)
            self._local.start()

    def stop(self) -> None:
        self.timer.stop()
        with self._local_lock:
            if self._local is not None:
                self._local.stop(timeout=5)
                self._local = None

    # --- notifications ---------------------------------------------------------------------

    def _follow_alerts(self) -> None:
        if not self.settings.live_alerts:
            for a in self.accounts:
                self._seen_events[a.id] = time.time()
            return
        for a in self.accounts:
            log = live.EventLog(a.id)
            since = self._seen_events.get(a.id, 0.0)
            new = [e for e in log.events if e.at > since]
            if not new:
                continue
            self._seen_events[a.id] = max(e.at for e in new)
            for e in new[-3:]:  # never a flood after a long time away
                if e.kind == "login_needed":
                    self.sign_in_needed.emit(a.id)
                if e.alert and not e.toasted:  # a Windows notification already said it
                    self.alert.emit(a.handle, e.text)


def _newest_event(account_id: str) -> float:
    log = live.EventLog(account_id)
    return max((e.at for e in log.events), default=time.time())


def _automation_running() -> bool:
    from .. import pipeline
    from ..jobs import JobDB

    return pipeline.runner_alive(JobDB())
