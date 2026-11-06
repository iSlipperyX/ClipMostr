"""What the window asks the background worker to do with a TikTok account: log in, post now, test
posting, read the stats now, set the profile, log out.

Everything that opens an account's browser runs in one place, so two things never open the same login
at once: the worker when it's running, else the window itself (service.Service with posting off). The
window adds a request here and shows its progress; whichever is running takes it (claim is atomic, so
only one does), writes its log lines and finishes it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import app_dir

KINDS = ("login", "logout", "refresh", "post_now", "test_posting", "apply_profile", "read_now")
KEEP = 2 * 86400  # finished requests are forgotten after this
STALE = 15 * 60  # a running request whose process is gone (a crash) is failed after this


@dataclass
class Request:
    id: int
    kind: str
    account: str = ""
    item: str = ""
    args: dict = field(default_factory=dict)
    created: float = 0.0
    state: str = "new"  # new | running | done | failed | cancelled
    owner: int = 0
    started: float = 0.0
    finished: float = 0.0
    result: str = ""
    log: str = ""
    cancel: int = 0  # the window asked to stop it (a login window: "Cancel")
    finish: int = 0  # the window said "I'm logged in"

    @property
    def done(self) -> bool:
        return self.state in ("done", "failed", "cancelled")

    @property
    def last_line(self) -> str:
        lines = [x for x in self.log.splitlines() if x.strip()]
        return lines[-1] if lines else ""


class Actions:
    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "actions.db")
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self.con.execute("""CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, account TEXT, item TEXT, args TEXT, created REAL,
            state TEXT, owner INTEGER, started REAL, finished REAL, result TEXT, log TEXT,
            cancel INTEGER DEFAULT 0, finish INTEGER DEFAULT 0)""")

    def close(self) -> None:
        self.con.close()

    # --- the window's side ---------------------------------------------------------

    def submit(self, kind: str, account: str = "", item: str = "", now: float | None = None, **args) -> int:
        """Ask for something; returns its id. An identical request still waiting is reused, so pressing a
        button twice doesn't do it twice."""
        if kind not in KINDS:
            raise ValueError(kind)
        now = time.time() if now is None else now
        with self._lock:
            row = self.con.execute("SELECT id FROM requests WHERE kind = ? AND account = ? AND item = ? AND state IN "
                                   "('new', 'running')", (kind, account, item)).fetchone()
            if row:
                return int(row["id"])
            cur = self.con.execute("INSERT INTO requests (kind, account, item, args, created, state, owner, started, "
                                   "finished, result, log) VALUES (?, ?, ?, ?, ?, 'new', 0, 0, 0, '', '')",
                                   (kind, account, item, json.dumps(args), now))
            return int(cur.lastrowid)

    def get(self, request_id: int) -> Request | None:
        with self._lock:
            row = self.con.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        return _req(row) if row else None

    def active(self, account: str = "") -> list[Request]:
        """Requests waiting or running (for one account, or any), oldest first."""
        with self._lock:
            rows = self.con.execute("SELECT * FROM requests WHERE state IN ('new', 'running')"
                                    + (" AND account = ?" if account else "") + " ORDER BY id",
                                    (account,) if account else ()).fetchall()
        return [_req(r) for r in rows]

    def recent(self, since: float) -> list[Request]:
        """Requests finished since `since` (the window tells you how they went)."""
        with self._lock:
            rows = self.con.execute("SELECT * FROM requests WHERE finished >= ? AND state IN ('done', 'failed', "
                                    "'cancelled') ORDER BY finished", (since,)).fetchall()
        return [_req(r) for r in rows]

    def ask_cancel(self, request_id: int) -> None:
        with self._lock:
            self.con.execute("UPDATE requests SET cancel = 1 WHERE id = ?", (request_id,))
            self.con.execute("UPDATE requests SET state = 'cancelled', finished = ?, result = 'Cancelled.' "
                             "WHERE id = ? AND state = 'new'", (time.time(), request_id))

    def ask_finish(self, request_id: int) -> None:
        with self._lock:
            self.con.execute("UPDATE requests SET finish = 1 WHERE id = ?", (request_id,))

    # --- the side that does them ---------------------------------------------------------

    def claim(self, owner: int | None = None, kinds: tuple[str, ...] = KINDS, now: float | None = None) -> Request | None:
        """Take the oldest waiting request of these kinds, or None. Only one process gets each."""
        owner = os.getpid() if owner is None else owner
        now = time.time() if now is None else now
        marks = ", ".join("?" * len(kinds))
        with self._lock:
            for _ in range(5):
                row = self.con.execute(f"SELECT id FROM requests WHERE state = 'new' AND kind IN ({marks}) "
                                       "ORDER BY id LIMIT 1", kinds).fetchone()
                if not row:
                    return None
                cur = self.con.execute("UPDATE requests SET state = 'running', owner = ?, started = ? "
                                       "WHERE id = ? AND state = 'new'", (owner, now, row["id"]))
                if cur.rowcount == 1:
                    return self.get(int(row["id"]))
        return None

    def add_log(self, request_id: int, line: str) -> None:
        with self._lock:
            self.con.execute("UPDATE requests SET log = substr(coalesce(log, '') || ? || char(10), -20000) WHERE id = ?",
                             (line, request_id))

    def flags(self, request_id: int) -> tuple[bool, bool]:
        """(cancel, finish) as the window set them."""
        with self._lock:
            row = self.con.execute("SELECT cancel, finish FROM requests WHERE id = ?", (request_id,)).fetchone()
        return (bool(row["cancel"]), bool(row["finish"])) if row else (True, False)

    def end(self, request_id: int, ok: bool, result: str = "", now: float | None = None,
            cancelled: bool = False) -> None:
        now = time.time() if now is None else now
        state = "cancelled" if cancelled else "done" if ok else "failed"
        with self._lock:
            self.con.execute("UPDATE requests SET state = ?, finished = ?, result = ? WHERE id = ?",
                             (state, now, result, request_id))

    def tidy(self, alive, now: float | None = None) -> None:
        """Forget old requests, and fail running ones whose process is gone (`alive(pid)`)."""
        now = time.time() if now is None else now
        with self._lock:
            self.con.execute("DELETE FROM requests WHERE state IN ('done', 'failed', 'cancelled') AND finished < ?",
                             (now - KEEP,))
            for row in self.con.execute("SELECT id, owner, started FROM requests WHERE state = 'running'").fetchall():
                if now - (row["started"] or 0) > STALE and not alive(row["owner"]):
                    self.con.execute("UPDATE requests SET state = 'failed', finished = ?, result = ? WHERE id = ?",
                                     (now, "Stopped part way (ClipMonster closed). Try again.", row["id"]))


def _req(row: sqlite3.Row) -> Request:
    d = dict(row)
    try:
        d["args"] = json.loads(d.get("args") or "{}")
    except ValueError:
        d["args"] = {}
    for k in ("account", "item", "result", "log"):
        d[k] = d.get(k) or ""
    for k in ("owner", "cancel", "finish"):
        d[k] = int(d.get(k) or 0)
    for k in ("created", "started", "finished"):
        d[k] = float(d.get(k) or 0.0)
    return Request(**d)
