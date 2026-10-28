"""Keep recommendations fresh, so Research and requests keep finding videos you haven't used.

Two things made them repeat: every run of a request asked YouTube the same searches in the same
order (so it answered with the same videos), and videos already picked, queued or clipped stayed
in the results, so a request whose best matches were used up "found" nothing new.

* used_ids()  - every video already picked, queued, clipped or sent to the Studio. Those are left
                out of what a request finds (before the AI rates anything) and hidden in Research.
* Memory      - fresh.json: for each request (the Find box's, and each standing request), its
                search angles, whose turn it is, and every video it has shown with its match.
                Each run takes the next angles and the next sort order, digs a page deeper when a
                page is all repeats, and the AI rates only videos it hasn't rated before.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from .settings import app_dir

ORDERS = ("relevance", "date", "viewCount")  # YouTube's sort orders, one per run in turn
ORDER_WORDS = {"relevance": "best match", "date": "newest", "viewCount": "most viewed"}
PER_TURN = 2  # searches a run makes (100 quota units each)
MAX_ANGLES = 8  # searches a request keeps to take turns with
PLAN_DAYS = 7  # an AI plan is reused this long, then the AI is asked for new angles
SHOWN_DAYS = 30  # a video a request showed counts as a repeat for this long
MAX_SHOWN = 500  # videos remembered per request
MAX_REQUESTS = 80  # requests remembered
DAY = 86400.0


def request_key(text: str, niche: str = "") -> str:
    """One request's key: the same words for the same niche are the same request."""
    return f"{niche}|{' '.join(text.lower().split())}"


@dataclass
class Turn:
    """What one run of a request searches: its angles for this run, and the sort order."""

    queries: list[str]
    order: str
    number: int  # 0 for a request's first run

    @property
    def order_words(self) -> str:
        return ORDER_WORDS.get(self.order, self.order)


def turn_for(angles: list[str], number: int, per_turn: int = PER_TURN) -> Turn:
    """Run `number`'s searches: the angles in turn, `per_turn` at a time, and the sort orders in turn.

    The first run is the first angles by relevance (what a request always did). Later runs move
    along the angles, and each full lap of them uses the next sort order, so a request with only
    one or two angles still asks YouTube something different each time."""
    angles = [a for a in angles if a.strip()] or [""]
    n = len(angles)
    per = max(1, min(per_turn, n))
    laps = max(1, -(-n // per))  # runs to go through every angle once
    start = (number % laps) * per
    queries = [angles[(start + i) % n] for i in range(per)]
    queries = list(dict.fromkeys(queries))
    return Turn(queries, ORDERS[(number // laps) % len(ORDERS)], number)


class Memory:
    """fresh.json: what each request has searched and shown. Shared by the app and automation,
    so a save re-reads the file first and keeps what the other one wrote meanwhile."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.requests: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.requests = self._read()

    def _read(self) -> dict[str, dict]:
        if not self.path:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            reqs = data.get("requests") if isinstance(data, dict) else None
            return {k: v for k, v in (reqs or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            return {}

    def get(self, key: str) -> dict:
        with self.lock:
            return self.requests.setdefault(key, {})

    # ---------------------------------------------------------------- the searches
    def plan(self, key: str, now: float) -> dict | None:
        """The request's saved plan (angles, subject, vibe, about, by_ai) while it's fresh."""
        rec = self.get(key)
        plan = rec.get("plan")
        if not isinstance(plan, dict) or not plan.get("angles"):
            return None
        if plan.get("by_ai") and now - float(rec.get("planned") or 0) > PLAN_DAYS * DAY:
            return None  # time for new angles
        return plan

    def tried(self, key: str) -> list[str]:
        """Every angle the request has searched, for asking the AI for new ones."""
        rec = self.get(key)
        return list(dict.fromkeys([*(rec.get("tried") or []), *((rec.get("plan") or {}).get("angles") or [])]))

    def store_plan(self, key: str, plan: dict, now: float) -> None:
        with self.lock:
            rec = self.get(key)
            old = (rec.get("plan") or {}).get("angles") or []
            rec["plan"] = plan
            rec["planned"] = now
            rec["tried"] = list(dict.fromkeys([*rec.get("tried", []), *old]))[-3 * MAX_ANGLES:]
            if old and old != plan.get("angles"):
                rec["turn"] = 0  # new angles start from the first

    def next_turn(self, key: str, angles: list[str]) -> Turn:
        """This run's searches, and moves the request on to the next ones."""
        with self.lock:
            rec = self.get(key)
            number = int(rec.get("turn") or 0)
            rec["turn"] = number + 1
            return turn_for(angles, number)

    # ---------------------------------------------------------------- what it showed
    def shown(self, key: str, now: float) -> dict[str, tuple[float, float, str]]:
        """Videos the request showed in the last SHOWN_DAYS: id -> (first shown, match, why)."""
        out = {}
        for vid, rec in (self.get(key).get("shown") or {}).items():
            try:
                t, match, why = float(rec[0]), float(rec[1]), str(rec[2])
            except (TypeError, ValueError, IndexError):
                continue
            if now - t < SHOWN_DAYS * DAY:
                out[vid] = (t, match, why)
        return out

    def record(self, key: str, videos, now: float) -> None:
        """Remember what a run showed (and each one's match, so the AI needn't rate it again)."""
        with self.lock:
            rec = self.get(key)
            shown = {k: v for k, v in (rec.get("shown") or {}).items()
                     if isinstance(v, list) and len(v) >= 3 and now - float(v[0] or 0) < SHOWN_DAYS * DAY}
            for v in videos:
                first = shown.get(v.id, [now])[0]
                shown[v.id] = [first, round(float(v.match), 1), v.match_why[:120]]
            if len(shown) > MAX_SHOWN:  # the oldest go first
                shown = dict(sorted(shown.items(), key=lambda kv: kv[1][0])[-MAX_SHOWN:])
            rec["shown"] = shown
            rec["used"] = now

    def save(self) -> None:
        if not self.path:
            return
        with self.lock:
            theirs = self._read()
            for key, rec in theirs.items():  # the other process's requests and runs, merged in
                mine = self.requests.get(key)
                if mine is None:
                    self.requests[key] = rec
                    continue
                newer, older = (rec, mine) if float(rec.get("used") or 0) > float(mine.get("used") or 0) else (mine, rec)
                merged = dict(newer)
                shown = dict(older.get("shown") or {})
                for vid, entry in (newer.get("shown") or {}).items():
                    if vid in shown and isinstance(shown[vid], list) and isinstance(entry, list) and entry:
                        entry = [min(shown[vid][0], entry[0]), *entry[1:]]  # first shown: the earlier time
                    shown[vid] = entry
                merged["shown"] = shown
                merged["turn"] = max(int(rec.get("turn") or 0), int(mine.get("turn") or 0))
                self.requests[key] = merged
            if len(self.requests) > MAX_REQUESTS:
                keep = sorted(self.requests.items(), key=lambda kv: float(kv[1].get("used") or 0))[-MAX_REQUESTS:]
                self.requests = dict(keep)
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps({"requests": self.requests}), encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


def memory_path() -> Path:
    return app_dir() / "fresh.json"


def load() -> Memory:
    return Memory(memory_path())


# ---------------------------------------------------------------- what's been used


def used_ids(db=None, picks=None, history=None) -> set[str]:
    """YouTube ids of every video already picked (picks.json), queued or clipped by automation
    (jobs.db, whatever happened to it) or sent to the Studio (research.json)."""
    out: set[str] = set()
    if picks is not None:
        with picks.lock:
            out |= {p["id"] for p in picks.picks if p.get("id")}
    if history is not None:
        with history.lock:
            out |= {vid for vid, rec in history.videos.items() if rec.get("sent")}
    if db is not None:
        try:
            out |= db.video_ids()
        except Exception:  # an unreadable jobs.db only means fewer repeats are caught
            pass
    return out


def used_now(history=None) -> set[str]:
    """used_ids() from the files on disk, for the app (automation passes its own open copies)."""
    from .jobs import JobDB
    from .policy import PickLog, picks_path

    db = None
    try:
        db = JobDB()
        return used_ids(db, PickLog(picks_path()), history)
    except Exception:
        return used_ids(None, PickLog(picks_path()), history)
    finally:
        if db is not None:
            db.close()

