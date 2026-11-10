"""The Sports section's state on this PC, and the worker that does what it asks.

sports.db (in the app's folder) holds the filter, the games found (each with its versions of footage and, once
looked at, its ranked highlights) and the work asked for: a search for games, finding a game's highlights, making
clips of the ones ticked. SQLite copes with the window and automation (its own process) reading and writing at
once, as jobs.db does.

The work is done by a DeskWorker, one thing at a time, so a game's three-hour download never races automation's
own videos for the graphics card: automation runs one while it's running (and waits for it between its own
videos, as it does for a Mac's), which is also how a linked Mac's asks get done; the window runs one only while
automation isn't running. A task is claimed with a lease that its worker renews every few seconds, so a window
closed mid-way, or automation stopped, leaves it for the next worker to start again (the download and the
measurements are cached, so that's quick).

Every method the window and the Mac use (state, set_filter, find_games, analyse, choose, make, ...) answers with
plain data, which remote/host.py hands the Mac as JSON. A problem the person can fix is a DeskError.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .. import sports
from . import games as G

Log = Callable[[str], None]

LEASE = 45.0  # s a claimed task stays its worker's without a renewal
RENEW = 10.0  # s between renewals
POLL = 2.0  # s between looks for work when there's none
STAGE_EVERY = 1.0  # s between progress writes
WAIT_RETRY = 600.0  # s before a task waiting out YouTube's sign-in pause is looked at again
KEEP_TASKS = 7 * 86400.0  # finished tasks are forgotten after this long
KINDS = ("search", "highlights", "make", "reel")
BUSY = ("queued", "analysing", "making")  # a game's statuses while work is waiting or under way


class DeskError(Exception):
    """What to tell the person when something can't be done; `status` is the HTTP status for the Mac."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class Stopping(Exception):
    """The worker is stopping (the window closing, automation stopping): the task goes back in the queue."""


class Called(Exception):
    """The person stopped the task."""


class Wait(Exception):
    """The task can't run yet (YouTube asked this PC to sign in): look again at `until`."""

    def __init__(self, until: float, why: str):
        super().__init__(why)
        self.until = until
        self.why = why


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def default_path() -> Path:
    from ..settings import app_dir

    return app_dir() / "sports.db"


class Desk:
    def __init__(self, path: Path | None = None, settings: Callable | None = None,
                 now: Callable[[], float] = time.time):
        from ..settings import Settings

        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.media = self.path.parent / "sports"  # each game's pictures of its highlights
        self.settings = settings or Settings.load
        self.now = now
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(self.path), timeout=20, isolation_level=None, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        with self._tx() as con:
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL)")
            con.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, game TEXT,"
                        " payload TEXT, status TEXT, owner TEXT, lease REAL, next_try REAL, created REAL,"
                        " updated REAL, error TEXT, said TEXT)")

    def close(self) -> None:
        with self.lock:
            self.con.close()

    @contextmanager
    def _tx(self):
        """One write transaction: other processes wait for it (up to the connection's timeout)."""
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self.con
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            self.con.execute("COMMIT")

    # ------------------------------------------------------------ storage

    def _meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, con, key: str, value: str) -> None:
        con.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))

    def _bump(self, con) -> None:
        row = con.execute("SELECT value FROM meta WHERE key='rev'").fetchone()
        self._set_meta(con, "rev", str(int(row["value"] if row else 0) + 1))

    def rev(self) -> str:
        return self._meta("rev", "0")

    def _games(self, con=None) -> list[dict]:
        with self.lock:
            rows = (con or self.con).execute("SELECT data FROM games").fetchall()
        out = []
        for r in rows:
            try:
                out.append(json.loads(r["data"]))
            except ValueError:
                continue
        return out

    def game(self, game_id: str) -> dict:
        with self.lock:
            row = self.con.execute("SELECT data FROM games WHERE id=?", (game_id,)).fetchone()
        if row is None:
            raise DeskError("That game isn't in the list any more.", 404)
        return json.loads(row["data"])

    def _put(self, con, g: dict) -> None:
        con.execute("INSERT INTO games(id, data, updated) VALUES(?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "data=excluded.data, updated=excluded.updated", (g["id"], json.dumps(g), self.now()))

    def change(self, game_id: str, fn: Callable[[dict], None]) -> dict:
        """Change one game in a transaction (another process may be changing others)."""
        with self._tx() as con:
            row = con.execute("SELECT data FROM games WHERE id=?", (game_id,)).fetchone()
            if row is None:
                raise DeskError("That game isn't in the list any more.", 404)
            g = json.loads(row["data"])
            fn(g)
            self._put(con, g)
            self._bump(con)
        return g

    def filter(self) -> G.GameFilter:
        try:
            return G.GameFilter.from_json(json.loads(self._meta("filter", "{}")))
        except ValueError:
            return G.GameFilter()

    # ------------------------------------------------------------ what the window and the Mac see

    def state(self, known_rev: str = "") -> dict:
        """Everything the Sports page shows. With `known_rev` the same as now, only {"rev", "same": True}."""
        rev = self.rev()
        if known_rev and known_rev == rev:
            return {"rev": rev, "same": True}
        s = self.settings()
        f = self.filter()
        gs = G.order(self._games())
        with self.lock:
            tasks = [dict(r) for r in self.con.execute(
                "SELECT id, kind, game, status, owner, lease, next_try, said FROM tasks "
                "WHERE status IN ('queued', 'working', 'cancel') ORDER BY id").fetchall()]
        now = self.now()
        working = [t for t in tasks if t["status"] in ("working", "cancel") and (t["lease"] or 0) > now]
        search = self._search_state(tasks)
        return {"rev": rev, "filter": f.to_json(), "describe": f.describe(), "games": [public(g) for g in gs],
                "search": search, "waiting": sum(1 for t in tasks if t["status"] == "queued"),
                "worker": (working[0]["owner"] or "").split(":")[0] if working else "",
                "ai": ai_state(s), "youtube": "api" if _youtube_key(s) else "free",
                "categories": category_list(s), "picked": sum(1 for g in gs if g.get("picked"))}

    def _search_state(self, tasks: list[dict]) -> dict:
        try:
            last = json.loads(self._meta("search", "{}"))
        except ValueError:
            last = {}
        live = next((t for t in tasks if t["kind"] == "search"), None)
        if live is not None:
            last = {**last, "status": "working" if live["status"] == "working" else "queued",
                    "stage": self._meta("search_stage", "") if live["status"] == "working" else "Waiting to start"}
        else:
            last.setdefault("status", "idle")
        return last

    # ------------------------------------------------------------ what the window and the Mac ask

    def set_filter(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise DeskError("That filter couldn't be read.")
        f = G.GameFilter.from_json({**self.filter().to_json(), **values})
        with self._tx() as con:
            self._set_meta(con, "filter", json.dumps(f.to_json()))
            self._bump(con)
        return {"filter": f.to_json(), "describe": f.describe()}

    def find_games(self, values: dict | None = None) -> dict:
        """Look for games that match the filter (`values` changes it first)."""
        if values:
            self.set_filter(values)
        with self._tx() as con:
            live = con.execute("SELECT id FROM tasks WHERE kind='search' AND status IN ('queued', 'working')").fetchone()
            if live is None:
                self._add_task(con, "search", "", {})
                self._set_meta(con, "search_stage", "")
            self._bump(con)
        return {"message": "Already looking for games." if live else f"Looking for {self.filter().describe()}…"}

    def pick(self, ids: list[str], picked: bool = True) -> dict:
        n = 0
        with self._tx() as con:
            for gid in _ids(ids):
                row = con.execute("SELECT data FROM games WHERE id=?", (gid,)).fetchone()
                if row:
                    g = json.loads(row["data"])
                    g["picked"] = bool(picked)
                    g["new"] = False
                    self._put(con, g)
                    n += 1
            self._bump(con)
        return {"changed": n}

    def analyse(self, ids: list[str], again: bool = False) -> dict:
        """Find the highlights of each game (in turn): download it, listen to it, rank its plays."""
        queued, busy = 0, 0
        with self._tx() as con:
            for gid in _ids(ids):
                row = con.execute("SELECT data FROM games WHERE id=?", (gid,)).fetchone()
                if row is None:
                    continue
                g = json.loads(row["data"])
                if g.get("status") in BUSY:
                    busy += 1
                    continue
                if g.get("highlights") and not again:
                    continue
                g.update(picked=True, new=False, status="queued", stage="Waiting to start", frac=None, error="")
                self._put(con, g)
                self._add_task(con, "highlights", gid, {"video": g["video"]})
                queued += 1
            self._bump(con)
        if not queued:
            raise DeskError("Those games are being worked on already." if busy else
                            "Pick the games to find highlights in first.")
        return {"queued": queued, "message": f"Finding the highlights of {_plural(queued, 'game')}…"}

    def choose(self, game_id: str, ids: list[str], chosen: bool = True) -> dict:
        want = set(_ids(ids))

        def fn(g):
            for h in g.get("highlights") or []:
                if h["id"] in want:
                    h["chosen"] = bool(chosen)

        g = self.change(game_id, fn)
        return {"chosen": sum(1 for h in g.get("highlights") or [] if h.get("chosen"))}

    def make(self, game_id: str, ids: list[str] | None = None, category_id: str | None = None) -> dict:
        """Make clips of a game's ticked highlights (or of `ids`), for `category_id` (else the filter's)."""
        cat_id = self.filter().category_id if category_id is None else re.sub(r"[^\w-]", "", str(category_id))
        s = self.settings()
        if cat_id:
            from .. import categories

            if categories.find(s, cat_id) is None:
                raise DeskError("That category isn't there any more. Pick another under Clips go to.")
        with self._tx() as con:
            row = con.execute("SELECT data FROM games WHERE id=?", (game_id,)).fetchone()
            if row is None:
                raise DeskError("That game isn't in the list any more.", 404)
            g = json.loads(row["data"])
            if g.get("status") in BUSY:
                raise DeskError("That game is being worked on already.")
            hl = g.get("highlights") or []
            want = [h["id"] for h in hl if (h["id"] in set(_ids(ids)) if ids is not None else h.get("chosen"))]
            if not want:
                raise DeskError("Tick the highlights to make first.")
            g.update(status="queued", stage=f"Waiting to make {_plural(len(want), 'clip')}", frac=None, error="")
            self._put(con, g)
            self._add_task(con, "make", game_id, {"ids": want, "category_id": cat_id})
            self._bump(con)
        return {"message": f"Making {_plural(len(want), 'clip')} from {g.get('matchup') or g.get('title')}…"}

    def reel(self, game_id: str, ids: list[str] | None = None, category_id: str | None = None) -> dict:
        """Make a game's recap reel (reel.py): its ticked plays (or `ids`), topped up from its best, as one
        65-85 s video for `category_id` (else the filter's)."""
        from .montage import MIN_PLAYS

        cat_id = self.filter().category_id if category_id is None else re.sub(r"[^\w-]", "", str(category_id))
        s = self.settings()
        if cat_id:
            from .. import categories

            if categories.find(s, cat_id) is None:
                raise DeskError("That category isn't there any more. Pick another under Clips for.")
        with self._tx() as con:
            row = con.execute("SELECT data FROM games WHERE id=?", (game_id,)).fetchone()
            if row is None:
                raise DeskError("That game isn't in the list any more.", 404)
            g = json.loads(row["data"])
            if g.get("status") in BUSY:
                raise DeskError("That game is being worked on already.")
            hl = g.get("highlights") or []
            if len(hl) < 2:
                raise DeskError("Find this game's highlights first: the reel is made of its best plays.")
            want = [h["id"] for h in hl if h["id"] in set(_ids(ids))] if ids else []
            g.update(status="queued", stage="Waiting to make the reel", frac=None, error="")
            self._put(con, g)
            self._add_task(con, "reel", game_id, {"ids": want, "category_id": cat_id})
            self._bump(con)
        n = max(MIN_PLAYS, len(want) or sum(1 for h in hl if h.get("chosen")))
        return {"message": f"Making a reel of the top {min(n, len(hl), 7)} plays from "
                           f"{g.get('matchup') or g.get('title')}…"}

    def version(self, game_id: str, video_id: str) -> dict:
        """Use another upload of the game (its full game instead of its highlights, say)."""
        def fn(g):
            if g.get("status") in BUSY:
                raise DeskError("Wait until it's finished with this game.")
            if g.get("video") == video_id:
                return
            if not G.use(g, video_id):
                raise DeskError("That footage isn't one of this game's.")
            g.update(highlights=[], ranked_by="", ai_error="", status="found", stage="", error="")
            self._drop_media(g["id"])

        return public(self.change(game_id, fn))

    def remove(self, ids: list[str]) -> dict:
        gone = 0
        with self._tx() as con:
            for gid in _ids(ids):
                row = con.execute("SELECT data FROM games WHERE id=?", (gid,)).fetchone()
                if row is None:
                    continue
                if json.loads(row["data"]).get("status") in ("analysing", "making"):
                    continue  # stop it first
                con.execute("UPDATE tasks SET status='cancelled', updated=? WHERE game=? AND status='queued'",
                            (self.now(), gid))
                con.execute("DELETE FROM games WHERE id=?", (gid,))
                gone += 1
            self._bump(con)
        for gid in _ids(ids):
            if gone:
                self._drop_media(gid)
        return {"removed": gone}

    def clear(self) -> dict:
        """Take every game that isn't picked and has no highlights off the list."""
        drop = [g["id"] for g in self._games() if not g.get("picked") and not g.get("highlights")
                and g.get("status") not in BUSY]
        return self.remove(drop) if drop else {"removed": 0}

    def cancel(self, game_id: str) -> dict:
        """Stop the work on a game ("search": the search for games)."""
        with self._tx() as con:
            if game_id == "search":
                rows = con.execute("SELECT id, status FROM tasks WHERE kind='search' AND status IN ('queued', 'working')"
                                   ).fetchall()
            else:
                rows = con.execute("SELECT id, status FROM tasks WHERE game=? AND status IN ('queued', 'working')",
                                   (game_id,)).fetchall()
            for r in rows:
                con.execute("UPDATE tasks SET status=?, updated=? WHERE id=?",
                            ("cancelled" if r["status"] == "queued" else "cancel", self.now(), r["id"]))
            if game_id != "search":
                row = con.execute("SELECT data FROM games WHERE id=?", (game_id,)).fetchone()
                if row is not None and not any(r["status"] == "working" for r in rows):
                    g = json.loads(row["data"])
                    g.update(status="ready" if g.get("highlights") else "found", stage="", frac=None)
                    self._put(con, g)
            self._bump(con)
        return {"stopped": len(rows)}

    def thumb(self, game_id: str, highlight_id: str) -> Path:
        if not re.fullmatch(r"[\w-]{1,40}", game_id or "") or not re.fullmatch(r"h\d{1,3}", highlight_id or ""):
            raise DeskError("No such picture.", 404)
        path = self.media / game_id / f"{highlight_id}.jpg"
        if not path.is_file():
            raise DeskError("No such picture.", 404)
        return path

    def _drop_media(self, game_id: str) -> None:
        if re.fullmatch(r"[\w-]{1,40}", game_id or ""):
            shutil.rmtree(self.media / game_id, ignore_errors=True)

    def _add_task(self, con, kind: str, game_id: str, payload: dict) -> None:
        now = self.now()
        con.execute("INSERT INTO tasks(kind, game, payload, status, owner, lease, next_try, created, updated, error, "
                    "said) VALUES(?, ?, ?, 'queued', '', 0, 0, ?, ?, '', '')",
                    (kind, game_id, json.dumps(payload), now, now))

    # ------------------------------------------------------------ the worker's side

    def claim(self, owner: str) -> dict | None:
        """The next task for `owner` to do (a search first, then the oldest), or None. A task whose worker stopped
        renewing its lease is taken over."""
        now = self.now()
        with self._tx() as con:
            con.execute("UPDATE tasks SET status='cancelled' WHERE status='cancel' AND lease<?", (now,))
            row = con.execute(
                "SELECT * FROM tasks WHERE (status='queued' AND next_try<=?) OR (status='working' AND lease<?) "
                "ORDER BY (kind='search') DESC, id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            con.execute("UPDATE tasks SET status='working', owner=?, lease=?, updated=? WHERE id=?",
                        (owner, now + LEASE, now, row["id"]))
            self._bump(con)
        task = dict(row)
        task["owner"] = owner
        try:
            task["payload"] = json.loads(row["payload"] or "{}")
        except ValueError:
            task["payload"] = {}
        return task

    def renew(self, task: dict) -> str:
        """Keep the task this worker's a while longer. Returns its status: 'working', or 'cancel' when the person
        stopped it, or 'lost' when another worker has it now."""
        now = self.now()
        with self._tx() as con:
            row = con.execute("SELECT status, owner FROM tasks WHERE id=?", (task["id"],)).fetchone()
            if row is None or row["owner"] != task["owner"] or row["status"] not in ("working", "cancel"):
                return "lost"
            con.execute("UPDATE tasks SET lease=? WHERE id=?", (now + LEASE, task["id"]))
        return row["status"]

    def finish(self, task: dict, status: str, error: str = "", said: str = "", next_try: float = 0.0) -> None:
        with self._tx() as con:
            if status == "queued":
                con.execute("UPDATE tasks SET status='queued', owner='', lease=0, next_try=?, updated=?, said=? "
                            "WHERE id=? AND owner=?", (next_try, self.now(), said, task["id"], task["owner"]))
            else:
                con.execute("UPDATE tasks SET status=?, error=?, said=?, lease=0, updated=? WHERE id=? AND owner=?",
                            (status, error, said, self.now(), task["id"], task["owner"]))
            con.execute("DELETE FROM tasks WHERE status IN ('done', 'failed', 'cancelled') AND updated<?",
                        (self.now() - KEEP_TASKS,))
            self._bump(con)

    def busy(self) -> bool:
        """A task is being worked on (by any worker whose lease is fresh)."""
        with self.lock:
            row = self.con.execute("SELECT 1 FROM tasks WHERE status IN ('working', 'cancel') AND lease>?",
                                   (self.now(),)).fetchone()
        return row is not None

    def waiting(self) -> int:
        with self.lock:
            row = self.con.execute("SELECT count(*) AS n FROM tasks WHERE status='queued' AND next_try<=?",
                                   (self.now(),)).fetchone()
        return int(row["n"])

    def stage(self, game_id: str, stage: str, frac: float | None = None, status: str | None = None) -> None:
        def fn(g):
            g["stage"] = stage
            g["frac"] = None if frac is None else round(max(0.0, min(1.0, float(frac))), 3)
            if status:
                g["status"] = status

        try:
            self.change(game_id, fn)
        except DeskError:
            pass

    def search_stage(self, stage: str) -> None:
        with self._tx() as con:
            self._set_meta(con, "search_stage", stage)
            self._bump(con)

    def save_search(self, result: dict, new_games: list[dict]) -> int:
        """The search's games into the list (games.merge) and what it found into the search's record."""
        with self._tx() as con:
            gs = self._games(con)
            for g in gs:
                g["new"] = False
            gs, added = G.merge(gs, new_games)
            keep = G.trim(gs)
            kept = {g["id"] for g in keep}
            con.execute("DELETE FROM games")
            for g in keep:
                self._put(con, g)
            self._set_meta(con, "search", json.dumps({**result, "added": added, "status": "idle"}))
            self._set_meta(con, "search_stage", "")
            self._bump(con)
        for g in gs:
            if g["id"] not in kept:
                self._drop_media(g["id"])
        return added

    def search_failed(self, why: str) -> None:
        with self._tx() as con:
            try:
                last = json.loads(con.execute("SELECT value FROM meta WHERE key='search'").fetchone()["value"])
            except (TypeError, ValueError):
                last = {}
            self._set_meta(con, "search", json.dumps({**last, "error": why, "at": self.now(), "status": "idle"}))
            self._set_meta(con, "search_stage", "")
            self._bump(con)


def _ids(ids) -> list[str]:
    if isinstance(ids, str):
        ids = [ids]
    return [str(i) for i in (ids or []) if re.fullmatch(r"[\w-]{1,40}", str(i))][:200]


def _youtube_key(s) -> str:
    from ..research import youtube_key

    return youtube_key(s)


def public(g: dict) -> dict:
    """A game as the page shows it."""
    out = {k: g.get(k) for k in ("id", "video", "url", "title", "channel", "published", "duration", "views",
                                 "thumbnail", "sport", "league", "home", "away", "matchup", "score", "kind", "found",
                                 "new", "picked", "status", "stage", "frac", "error", "ranked_by", "ai_error",
                                 "analysed", "made", "reels")}
    out["versions"] = [{k: v.get(k) for k in ("video", "url", "title", "channel", "kind", "duration", "views")}
                       for v in g.get("versions") or []]
    out["highlights"] = [{k: h.get(k) for k in ("id", "start", "end", "length", "signal", "parts", "ai", "score",
                                                "rank", "title", "play", "players", "why", "chosen", "made", "file")}
                         for h in g.get("highlights") or []]
    sp = sports.get(g.get("sport", ""))
    out["emoji"] = sp.emoji if sp else "🏆"
    out["kind_label"] = G.KIND_LABELS.get(g.get("kind", ""), "")
    return out


def ai_state(s) -> dict:
    """Which AI ranks the plays, for the page: {name, model, ready, local, why}."""
    from .. import ai

    order = ai.order(s)
    first = order[0] if order else ai.chosen(s)
    try:
        model = ai.model_for(s, "find", first) if order else ""
    except Exception:  # noqa: BLE001 - a local model that can't be resolved yet
        model = ""
    return {"name": ai.name(first), "model": model, "ready": bool(order), "local": first in ai.LOCALS,
            "why": "" if order else ai.missing_key(s)}


def category_list(s) -> list[dict]:
    from .. import categories

    out = []
    for c in categories.load(s):
        sp = c.sport_info
        out.append({"id": c.id, "name": c.name, "account": c.account, "sport": sp.id if sp else "",
                    "emoji": sp.emoji if sp else "", "macs": bool(c.send_to_macs), "state": c.state})
    out.sort(key=lambda c: (not c["sport"], c["name"].lower()))
    return out


# ---------------------------------------------------------------- the worker


class DeskWorker:
    """Does the desk's tasks, one at a time, on a thread of its own.

    `owner` names the worker ("automation:<pid>", "window:<pid>"); `may_work()` says whether to take a new task
    now (the window's says no while automation runs); `events(kind, text)` hears what finished (automation tells
    a linked Mac); `deliver(folder, category)` sends new clips to the Macs' inbox and says how many."""

    def __init__(self, desk: Desk, settings: Callable, owner: str, log: Log | None = None,
                 may_work: Callable[[], bool] = lambda: True, events: Callable[[str, str], None] | None = None,
                 deliver: Callable | None = None, engine_factory: Callable | None = None, poll: float = POLL):
        self.desk = desk
        self.settings = settings
        self.owner = owner
        self.log = log or (lambda m: None)
        self.may_work = may_work
        self.events = events or (lambda kind, text: None)
        self.deliver = deliver
        self.engine_factory = engine_factory
        self.poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: dict | None = None
        self._status = "working"

    # --- running ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sports-desk", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def busy(self) -> bool:
        return self.current is not None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self.step()
            except Exception as exc:  # noqa: BLE001 - sports.db unreadable for a moment: try again shortly
                self.log(f"⚠ Sports: {exc}")
                worked = False
            if not worked:
                self._stop.wait(self.poll)

    def step(self) -> bool:
        """Do one task, if there's one and this worker may. True when it did one."""
        if not self.may_work():
            return False
        task = self.desk.claim(self.owner)
        if task is None:
            return False
        self.run(task)
        return True

    def run(self, task: dict) -> None:
        self.current = task
        self._status = "working"
        renewing = threading.Event()

        def renew_loop():
            while not renewing.wait(RENEW):
                self._status = self.desk.renew(task)

        keeper = threading.Thread(target=renew_loop, name="sports-lease", daemon=True)
        keeper.start()
        try:
            said = {"search": self.do_search, "highlights": self.do_highlights, "make": self.do_make,
                    "reel": self.do_reel}[task["kind"]](task)
            self.desk.finish(task, "done", said=said or "")
        except Stopping:
            self.desk.finish(task, "queued", said="Stopped; starts again later")
            self._settle(task, "Stopped; starts again")
        except Called:
            self.desk.finish(task, "cancelled")
            self._settle(task, "")
            self.log("Sports: stopped, as you asked.")
        except Wait as w:
            self.desk.finish(task, "queued", said=w.why, next_try=w.until)
            if task["game"]:
                self.desk.stage(task["game"], w.why, None, "queued")
            else:
                self.desk.search_stage(w.why)
        except Exception as exc:  # noqa: BLE001 - the task failed: say why on its game
            why = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
            self.desk.finish(task, "failed", error=why)
            if task["game"]:
                def fn(g):
                    g.update(status="failed" if not g.get("highlights") or task["kind"] == "highlights" else "ready",
                             stage="", frac=None, error=why)
                try:
                    self.desk.change(task["game"], fn)
                except DeskError:
                    pass
            else:
                self.desk.search_failed(why)
            self.log(f"✗ Sports: {why}")
        finally:
            renewing.set()
            self.current = None

    def _settle(self, task: dict, stage: str) -> None:
        """A game's status after its task stopped part-way."""
        if not task["game"]:
            self.desk.search_stage("")
            return

        def fn(g):
            if task["kind"] == "highlights" and stage:
                g.update(status="queued", stage=stage, frac=None)
            else:
                g.update(status="ready" if g.get("highlights") else "found", stage="", frac=None)

        try:
            self.desk.change(task["game"], fn)
        except DeskError:
            pass

    def check(self) -> None:
        """Raise if this task should stop: the worker is stopping, or the person stopped it."""
        if self._stop.is_set():
            raise Stopping()
        if self._status == "cancel":
            raise Called()
        if self._status == "lost":
            raise Stopping()

    def _progress(self, game_id: str, status: str):
        last = {"t": 0.0, "msg": ""}

        def progress(msg: str, frac: float | None = None) -> None:
            self.check()
            t = time.monotonic()
            if msg != last["msg"] or t - last["t"] >= STAGE_EVERY:
                last.update(t=t, msg=msg)
                if game_id:
                    self.desk.stage(game_id, msg, frac, status)
                else:
                    self.desk.search_stage(msg)

        return progress

    def _engine(self, s, progress, log=None):
        from ..engine import Cancelled, Engine

        def cancelled() -> bool:
            try:
                self.check()
            except (Stopping, Called):
                return True
            return False

        make = self.engine_factory or Engine
        return make(s, log=log or self.log, progress=progress, cancelled=cancelled), Cancelled

    def _youtube_pause(self) -> None:
        """Wait out YouTube's sign-in pause, as automation does, before downloading anything."""
        try:
            from ..jobs import JobDB
            from ..pipeline import paused_until

            db = JobDB()
            try:
                until = paused_until(db, time.time())
            finally:
                db.close()
        except Exception:  # noqa: BLE001 - jobs.db busy: go ahead
            return
        if until > time.time():
            raise Wait(min(until, time.time() + WAIT_RETRY),
                       "Waiting: YouTube asked this PC to sign in (Automation shows how to)")

    # --- the tasks ------------------------------------------------------------------------------------------------

    def do_search(self, task: dict) -> str:
        from .. import ai

        s = self.settings()
        f = self.desk.filter()
        now = time.time()
        key = _youtube_key(s)
        qs = G.queries(f)
        progress = self._progress("", "")
        found: dict[str, G.Found] = {}
        problems: list[str] = []
        for i, q in enumerate(qs, 1):
            progress(f"Searching YouTube for “{q}” ({i} of {len(qs)})")
            try:
                got = (G.search_api(key, q, f.published_after(now)) if key else
                       G.search_free(q, newest=f.since in ("day", "3days", "week"), cookies=s.cookies_browser or None,
                                     log=None))
            except Exception as exc:  # noqa: BLE001 - one search failing leaves the others
                problems.append(f"“{q}”: {getattr(exc, 'message', None) or exc}")
                continue
            for v in got:
                found.setdefault(v.id, v)
        self.check()
        if not found and problems:
            raise DeskError("YouTube couldn't be searched: " + "; ".join(problems[:2]))
        reads = {v.id: G.read_title(v.title, v.duration) for v in found.values()}
        model, ai_note = "", ""
        if ai.has_key(s) and found:
            progress(f"The AI is reading {_plural(len(found), 'title')}")
            try:
                said, model = G.ai_read(list(found.values()), f, s, log=self.log, check=self.check)
                for vid, r in said.items():
                    keep = reads[vid]
                    reads[vid] = {**keep, **{k: v for k, v in r.items() if v or k == "game"},
                                  "score": keep.get("score", "")}
            except ai.AIError as exc:
                ai_note = f"The AI couldn't read the titles ({exc}), so they were read by their words."
                self.log(ai_note)
        self.check()
        left: dict[str, int] = {}
        new: list[dict] = []
        for v in found.values():
            why = G.fits(v, reads[v.id], f, now)
            if why:
                left[why] = left.get(why, 0) + 1
                continue
            new.append(G.make_game(v, reads[v.id], f, now))
        result = {"at": time.time(), "queries": qs, "searched": len(found), "fits": len(new),
                  "left_out": left, "by": model, "note": ai_note, "problems": problems,
                  "quota": len(qs) * 101 if key else 0, "error": "", "describe": f.describe()}
        added = self.desk.save_search(result, new)
        said = (f"Found {_plural(added, 'new game')}" if added else
                "No new games" if new else "No games matched") + f" ({_plural(len(found), 'video')} searched)"
        self.log(f"Sports: {said}.")
        if added:
            self.events("sports", f"{f.sport_info.emoji} {said}")
        return said

    def _settings_for(self, s, category_id: str, video_id: str, url: str):
        """The settings a game is clipped with: its category's (folder, look, account), as automation's."""
        from .. import categories
        from ..jobs import Job
        from ..pipeline import settings_for

        cat = categories.find(s, category_id) if category_id else None
        out, label = settings_for(Job(id=video_id, url=url, category_id=cat.id if cat else ""), s)
        return out, label, cat

    def do_highlights(self, task: dict) -> str:
        from ..engine import Project
        from ..export import thumbnail
        from . import rank

        g = self.desk.game(task["game"])
        s = self.settings()
        f = self.desk.filter()
        sport = sports.get(g.get("sport", "")) or f.sport_info
        base, _label, _cat = self._settings_for(s, f.category_id, g["video"], g["url"])
        lo, hi = sport.lengths
        own = _cat.clip_overrides() if _cat else {}
        es = base.apply({"sport": sport.id, "mode": "auto", "clip_count": rank.CANDIDATES, "quality_floor": 0.2,
                         "series_parts": 1, "ai_polish": False, "min_len": own.get("min_len", lo),
                         "max_len": own.get("max_len", hi)})
        self._youtube_pause()
        progress = self._progress(g["id"], "analysing")
        progress("Starting", None)
        engine, Cancelled = self._engine(es, progress)
        p = Project(g["url"])
        try:
            engine.open(p)
            engine.measure(p)
            progress("Finding the plays", None)
            engine.find(p, use_ai=False)
        except Cancelled:
            self.check()
            raise Stopping()
        clips = [c for c in p.clips if c.enabled]
        if not clips:
            raise DeskError("No plays stood out: the crowd and the commentary never rose much. Try another version "
                            "of the game's footage.")
        progress("The AI is ranking the plays" if rank.ai.has_key(es) else "Ranking the plays", None)
        hl, by_ai, why_not = rank.rank(g, clips, p.signals, p.cues, es, f.per_game, log=self.log, check=self.check)
        self.check()
        folder = self.desk.media / g["id"]
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        for h in hl:
            try:
                thumbnail(p.video.path, folder / f"{h['id']}.jpg", h["start"] + 0.55 * (h["end"] - h["start"]))
            except Exception:  # noqa: BLE001 - a missing picture leaves the play
                pass

        def fn(x):
            x.update(highlights=hl, ranked_by=by_ai, ai_error=why_not, analysed=time.time(), status="ready",
                     stage="", frac=None, error="", picked=True)
            if p.video is not None and p.video.duration:
                x["duration"] = p.video.duration

        self.desk.change(g["id"], fn)
        who = f" (ranked by {by_ai})" if by_ai else ""
        said = f"{_plural(len(hl), 'highlight')} ready: {g.get('matchup') or g.get('title')}{who}"
        self.log(f"Sports: {said}.")
        self.events("sports", f"{sport.emoji} {said}")
        return said

    def do_make(self, task: dict) -> str:
        from ..analyze import Clip
        from ..engine import Project
        from ..organize import safe_name

        g = self.desk.game(task["game"])
        want = list(task["payload"].get("ids") or [])
        hl = [h for h in g.get("highlights") or [] if h["id"] in want]
        if not hl:
            raise DeskError("Those highlights aren't there any more: find the game's highlights again.")
        s = self.settings()
        sport = sports.get(g.get("sport", "")) or sports.SPORTS["any"]
        base, label, cat = self._settings_for(s, task["payload"].get("category_id", ""), g["video"], g["url"])
        stamp_ = time.strftime("%Y-%m-%d %H.%M")
        root = (s.auto_output_path / cat.folder if cat else s.auto_output_path / "Sports") / "Highlights" / \
            safe_name(f"{stamp_} {g.get('matchup') or g.get('title')}", 70)
        es = base.apply({"output_dir": str(root), "sport": sport.id, "mode": "auto", "series_parts": 1})
        self._youtube_pause()
        progress = self._progress(g["id"], "making")
        progress("Starting", None)
        engine, Cancelled = self._engine(es, progress)
        p = Project(g["url"])
        hl.sort(key=lambda h: h.get("rank") or 99)
        p.meta = {"automation": {"permission": "Picked by you in Sports", "origins": ["sports"], "how": "manual",
                                 "recipe": label, "picked_at": time.time()},
                  "sports": {"matchup": g.get("matchup"), "league": g.get("league"), "sport": sport.id,
                             "highlights": [{k: h.get(k) for k in ("id", "play", "players", "ai", "signal")}
                                            for h in hl]}}
        if cat:
            p.meta["automation"] |= {"category": cat.name, "category_id": cat.id, "account": cat.account,
                                     "account_id": cat.account_id}
        try:
            engine.open(p)
            engine.measure(p)
            p.clips = [Clip(float(h["start"]), float(h["end"]), float(h.get("score") or 0), h.get("title") or "",
                            origin="manual", reason=h.get("why") or "", edited=True) for h in hl]
            p.mode_used = "sports"
            if es.ai_polish:
                engine.polish(p)
            folder = engine.export(p)
        except Cancelled:
            self.check()
            raise Stopping()
        files = _clip_files(folder)
        rejected = sum(1 for x in files if x.startswith("_rejected"))
        sent = 0
        if self.deliver is not None and cat is not None:
            try:
                sent = int(self.deliver(folder, cat) or 0)
            except Exception as exc:  # noqa: BLE001 - the clips are made either way
                self.log(f"⚠ The clips couldn't be sent to your Macs: {exc}")
        made_ids = [h["id"] for h in hl]

        def fn(x):
            for h, file in zip(sorted((h for h in x.get("highlights") or [] if h["id"] in made_ids),
                                      key=lambda h: h.get("rank") or 99), files + [""] * len(made_ids)):
                h["made"] = True
                h["chosen"] = False
                h["file"] = str(folder / file) if file else ""
            x.setdefault("made", []).append({"at": time.time(), "folder": str(folder), "clips": len(files) - rejected,
                                             "rejected": rejected, "sent": sent, "category": cat.name if cat else ""})
            x.update(status="ready", stage="", frac=None, error="")

        self.desk.change(g["id"], fn)
        n = len(files) - rejected
        said = (f"Made {_plural(n, 'clip')} from {g.get('matchup') or g.get('title')}"
                + (f" ({rejected} rejected by the quality check)" if rejected else "")
                + (f", for {cat.name}" if cat else ""))
        self.log(f"Sports: {said} → {folder}")
        self.events("sports", f"{sport.emoji} {said}")
        return said


    def do_reel(self, task: dict) -> str:
        from ..engine import Project
        from . import reel as R

        g = self.desk.game(task["game"])
        hl = g.get("highlights") or []
        if len(hl) < 2:
            raise DeskError("This game's highlights aren't there any more: find them again.")
        s = self.settings()
        f = self.desk.filter()
        sport = sports.get(g.get("sport", "")) or sports.SPORTS["any"]
        base, _label, cat = self._settings_for(s, task["payload"].get("category_id", ""), g["video"], g["url"])
        root = (s.auto_output_path / cat.folder if cat else s.auto_output_path / "Sports") / "Reels"
        es = base.apply({"sport": sport.id, "mode": "auto", "series_parts": 1})
        self._youtube_pause()
        progress = self._progress(g["id"], "making")
        progress("Starting the reel", None)
        engine, Cancelled = self._engine(es, progress)
        p = Project(g["url"])
        try:
            engine.open(p)
            engine.measure(p)
            res = R.make(p, g, hl, es, root, ids=list(task["payload"].get("ids") or []) or None, category=cat,
                         voice=f.reel_voice, framing_mode=f.reel_framing, order=f.reel_order, log=self.log,
                         progress=progress, check=self.check)
        except Cancelled:
            self.check()
            raise Stopping()
        except R.montage.ReelError as exc:
            raise DeskError(str(exc)) from exc
        sent = 0
        if self.deliver is not None and cat is not None:
            try:
                sent = int(self.deliver(res.folder, cat) or 0)
            except Exception as exc:  # noqa: BLE001 - the reel is made either way
                self.log(f"⚠ The reel couldn't be sent to your Macs: {exc}")
        length = float(res.checks.get("length") or res.reel.duration)

        def fn(x):
            x.setdefault("reels", []).append({
                "at": time.time(), "folder": str(res.folder), "file": str(res.file), "length": round(length, 1),
                "plays": len(res.reel.plays), "rewards": bool(res.checks.get("rewards")), "sent": sent,
                "category": cat.name if cat else "", "narrator": bool(res.info.get("narrator")),
                "story_by": res.reel.story.written_by, "warnings": res.warnings[:3]})
            x.update(status="ready", stage="", frac=None, error="")

        self.desk.change(g["id"], fn)
        said = (f"Made a {length:.0f} s reel of {g.get('matchup') or g.get('title')}'s top {len(res.reel.plays)} "
                f"plays" + (f", for {cat.name}" if cat else "")
                + ("" if res.checks.get("rewards") else " (under a minute: not for Creator Rewards)"))
        self.log(f"Sports: {said} → {res.file}")
        for w in res.warnings:
            self.log(f"⚠ {w}")
        self.events("sports", f"{sport.emoji} {said}")
        return said


def _clip_files(folder: Path) -> list[str]:
    """The clip files a finished export listed, in its order."""
    try:
        data = json.loads((Path(folder) / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(c.get("file") or "") for c in sorted(data.get("clips") or [], key=lambda c: c.get("rank") or 0)]


def send_to_macs(folder, category) -> int:
    """New clips into the linked Macs' inbox (remote/deliveries.py), when the category sends them and a Mac is
    linked, as automation's Runner.deliver does (it also tells a connected Mac at once). Returns how many."""
    if category is None or not getattr(category, "send_to_macs", False) or not folder:
        return 0
    from ..remote import deliveries, state as remote_state

    macs = [d["id"] for d in remote_state.load()["devices"]]
    if not macs:
        return 0
    made = deliveries.add(Path(folder), category, time.time())
    deliveries.prune(macs, time.time())
    return len(made)


def owner_name(kind: str) -> str:
    return f"{kind}:{os.getpid()}"
