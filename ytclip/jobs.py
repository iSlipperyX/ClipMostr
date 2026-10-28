"""jobs.db: the unattended runner's queue and state, shared with the Automation tab.

One row per video, keyed by its YouTube id, so a video can only ever be queued once.
SQLite copes with the runner (its own process) and the app reading and writing at once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .settings import app_dir

STATUSES = ("queued", "working", "done", "failed")
SIGN_IN_WAIT = "Waiting: YouTube asked this PC to sign in"  # the stage of a video waiting out a sign-in pause


@dataclass
class Job:
    id: str
    url: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    category: str = ""
    permission: str = ""  # why this video may be clipped (from the picking rules, or "Added by you")
    origins: list[str] = field(default_factory=list)
    how: str = "auto"  # auto (picked by the rules) | manual (added by you)
    status: str = "queued"
    stage: str = ""  # what it's doing now, or how it ended
    attempts: int = 0
    error: str = ""
    next_try: float = 0.0
    created: float = 0.0
    updated: float = 0.0
    folder: str = ""
    clips: int = 0  # ready to upload
    rejected: int = 0  # moved to _rejected by the quality check
    recipe: str = ""
    pruned: int = 0  # cache files removed
    niche: str = ""  # id of the niche it was picked for (niches.py), "" for none

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        d = dict(row)
        d["origins"] = json.loads(d.get("origins") or "[]")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


_COLUMNS = {f.name: ("REAL" if f.type in ("float",) else "INTEGER" if f.type in ("int",) else "TEXT")
            for f in fields(Job)}


class JobDB:
    def __init__(self, path: Path | None = None):
        self.path = path or db_path()
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        cols = ", ".join(f"{name} {kind}{' PRIMARY KEY' if name == 'id' else ''}" for name, kind in _COLUMNS.items())
        self.con.execute(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")
        have = {r["name"] for r in self.con.execute("PRAGMA table_info(jobs)")}
        for name, kind in _COLUMNS.items():  # columns added by later versions
            if name not in have:
                self.con.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        self.con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.con.execute("CREATE TABLE IF NOT EXISTS events (time REAL, kind TEXT)")

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------ jobs
    def add(self, job: Job, now: float | None = None) -> bool:
        """Queue a job; False if this video was queued before (whatever happened to it)."""
        now = now or time.time()
        job.created = job.created or now
        job.updated = now
        d = asdict(job)
        d["origins"] = json.dumps(d["origins"])
        with self._lock:
            cur = self.con.execute(f"INSERT OR IGNORE INTO jobs ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                                   list(d.values()))
            return cur.rowcount == 1

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self.con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def jobs(self, limit: int = 500) -> list[Job]:
        """Newest first, with anything in progress or waiting ahead of finished work."""
        with self._lock:
            rows = self.con.execute(
                "SELECT * FROM jobs ORDER BY CASE status WHEN 'working' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
                "CASE status WHEN 'queued' THEN created END ASC, updated DESC LIMIT ?", (limit,)).fetchall()
        return [Job.from_row(r) for r in rows]

    def next_due(self, now: float) -> Job | None:
        with self._lock:
            row = self.con.execute("SELECT * FROM jobs WHERE status = 'queued' AND next_try <= ? "
                                   "ORDER BY created LIMIT 1", (now,)).fetchone()
        return Job.from_row(row) if row else None

    def next_try(self) -> float | None:
        """When the soonest waiting job becomes due."""
        with self._lock:
            row = self.con.execute("SELECT MIN(next_try) AS t FROM jobs WHERE status = 'queued'").fetchone()
        return row["t"] if row and row["t"] is not None else None

    def update(self, job_id: str, now: float | None = None, **changes) -> None:
        changes["updated"] = now or time.time()
        if "origins" in changes:
            changes["origins"] = json.dumps(changes["origins"])
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self._lock:
            self.con.execute(f"UPDATE jobs SET {sets} WHERE id = ?", [*changes.values(), job_id])

    def remove(self, job_id: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def retry(self, job_id: str) -> None:
        self.update(job_id, status="queued", next_try=0.0, attempts=0, error="", stage="Waiting")

    def reset_interrupted(self) -> int:
        """Jobs left 'working' by a runner that crashed or was killed go back in the queue."""
        with self._lock:
            return self.con.execute("UPDATE jobs SET status = 'queued', stage = 'Interrupted; will start again' "
                                    "WHERE status = 'working'").rowcount

    def release_waiting(self, now: float) -> int:
        """Videos waiting out a sign-in pause are due now (the pause was ended early)."""
        with self._lock:
            return self.con.execute("UPDATE jobs SET next_try = 0, stage = 'Waiting', updated = ? "
                                    "WHERE status = 'queued' AND stage = ? AND next_try > ?",
                                    (now, SIGN_IN_WAIT, now)).rowcount

    def drop_waiting(self, niche: str) -> int:
        """Take a niche's waiting videos out of the queue (the niche was deleted)."""
        with self._lock:
            return self.con.execute("DELETE FROM jobs WHERE niche = ? AND status = 'queued'", (niche,)).rowcount

    def niche_counts(self, since: float) -> dict[str, dict[str, int]]:
        """Per niche id: videos waiting or working, done since `since`, and clips ready from those."""
        with self._lock:
            rows = self.con.execute(
                "SELECT COALESCE(niche, '') AS niche, "
                "SUM(status IN ('queued', 'working')) AS waiting, "
                "SUM(status = 'done' AND updated >= ?) AS done, "
                "SUM(CASE WHEN status = 'done' AND updated >= ? THEN clips ELSE 0 END) AS clips "
                "FROM jobs GROUP BY COALESCE(niche, '')", (since, since)).fetchall()
        return {r["niche"]: {"waiting": r["waiting"] or 0, "done": r["done"] or 0, "clips": r["clips"] or 0}
                for r in rows}

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.con.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {s: 0 for s in STATUSES} | {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------ meta (runner state, flags)
    def meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.meta(key) or default)
        except ValueError:
            return default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.con.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = "
                             "excluded.value", (key, str(value)))

    # ------------------------------------------------------------ events (for daily budgets)
    def note(self, kind: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self.con.execute("INSERT INTO events (time, kind) VALUES (?, ?)", (now, kind))
            self.con.execute("DELETE FROM events WHERE time < ?", (now - 30 * 86400,))

    def count(self, kind: str, since: float) -> int:
        with self._lock:
            row = self.con.execute("SELECT COUNT(*) AS n FROM events WHERE kind = ? AND time >= ?",
                                   (kind, since)).fetchone()
        return row["n"]


def db_path() -> Path:
    return app_dir() / "jobs.db"
