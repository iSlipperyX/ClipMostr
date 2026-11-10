"""Learning from how posted videos did, without training anything: no fine-tuning, no extra graphics memory.

The AI that writes hooks, titles and scripts is shown, in its prompt, the openings that worked best on your
accounts and the ones that flopped, picked to match what it's writing now. That's in-context ("few-shot")
learning: the model is the same, only what it reads changes, so it costs a few hundred words of prompt and
nothing else, on the local model as much as on Claude or Gemini.

Where the numbers come from: posting writes results.json (posting/results.py) as it reads each account's stats
from TikTok: one row per video that's been up two days, with its views and its *lift*, its views over that
account's usual (median) views, so a small account and a big one compare. learn.py reads it; this builds on
that:

1. **Distil** (every 24 hours, or when asked): each posted video's hook and title is sorted by its lift into
   top performers (2x the account's usual views or more) and underperformers (half or less); the rest teach
   nothing either way. Each one keeps its category, the category's sport, and its shape ("a 6-word
   question with a number"), in an SQLite file beside the settings (learner.db) with an index of its words.
2. **Recall** (each time something is written): the 3-5 top performers closest to the new video, by the words
   they share (rarer words count more), its category and its sport, go into the prompt as examples to learn
   from, and the closest recent underperformers as "avoid structures similar to". Nothing goes in until
   there's something to learn from, and the examples are always marked as other videos', never to copy.
3. **Pacing** (the Video Creator): TikTok shows ClipMonster views, likes, comments and shares, not the
   retention curve (the Studio analytics page behind each video's sign-in has it, and isn't read), so
   pacing is calibrated from lift instead: the words a minute and the seconds per scene of the Creator videos
   that did best are where the next script aims, pulled gently from the usual pace while there are few of
   them, and kept within what sounds natural (130-180 words a minute, a new picture every 3-8 seconds).

Every function takes the results file, the database and the time as arguments, so the tests run on fixtures.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .. import learn
from .embeddings import word_tokens

TOP_LIFT = 2.0  # this many times the account's usual views or more: a top performer
BOTTOM_LIFT = 0.5  # this much or less: an underperformer
EVERY = 24 * 3600  # distilled again this often
EXAMPLES = (3, 5)  # top performers shown: at least / at most
AVOID = 3  # underperformers shown
AVOID_DAYS = 45  # only underperformers this recent: what flopped long ago may work now
CATEGORY_BONUS, SPORT_BONUS = 0.35, 0.25
WPM_RANGE = (130.0, 180.0)
SCENE_RANGE = (3.0, 8.0)  # seconds a picture stays up
PACE_MIN = 2  # top-performing Creator videos (with their pace known) before pacing moves at all
PACE_SHRINK = 3  # n / (n + PACE_SHRINK) of the way from the usual pace to theirs
DEFAULT_WPM = 150.0
DEFAULT_SCENE = 6.0  # seconds a picture stays up when nothing has been learned (director.scene_range: 4.5-8)
SCHEMA_VERSION = 1

Log = Callable[[str], None]


def db_path() -> Path:
    from ..settings import app_dir

    return app_dir() / "learner.db"


# ---------------------------------------------------------------- what a hook looks like

_QUESTION = re.compile(r"\?\s*$")
_NUMBER = re.compile(r"\b\d[\d,.]*\b|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b", re.I)
_YOU = re.compile(r"\b(?:you|your|you're|you'll)\b", re.I)


def shape(text: str) -> str:
    """The structure of an opening in a few words: "a 6-word question with a number, all caps"."""
    t = str(text or "").strip()
    words = len(t.split())
    kind = "question" if _QUESTION.search(t) else "exclamation" if t.endswith("!") else "statement"
    bits = []
    if _NUMBER.search(t):
        bits.append("a number")
    if _YOU.search(t):
        bits.append("“you”")
    out = f"a {words}-word {kind}" + (f" with {' and '.join(bits)}" if bits else "")
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 6 and all(c.isupper() for c in letters):
        out += ", all caps"
    return out


def tokens(text: str) -> list[str]:
    """The words a hook is indexed by: stems without the common words, and neighbouring pairs."""
    toks = word_tokens(text or "")
    return list(dict.fromkeys(toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]))


# ---------------------------------------------------------------- 1. distilling

@dataclass
class Exemplar:
    text: str
    kind: str  # hook | title
    tier: str  # top | bottom
    lift: float
    category_id: str = ""
    sport: str = ""
    origin: str = ""  # clip | creator | "" (before posting said)
    posted_at: float = 0.0
    shape: str = ""
    id: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def tier_of(lift: float) -> str:
    if lift >= TOP_LIFT:
        return "top"
    if lift <= BOTTOM_LIFT:
        return "bottom"
    return ""


def _squash(text: str) -> str:
    return re.sub(r"[^\w]", "", (text or "").lower())


def distill(outcomes: Iterable[learn.Outcome], sports: dict[str, str] | None = None) -> list[Exemplar]:
    """The top performers' and underperformers' openings: each video's hook, or its title when it had none.
    The same words posted twice count once, at their more telling lift."""
    sports = sports or {}
    best: dict[tuple[str, str], Exemplar] = {}
    for o in outcomes:
        tier = tier_of(o.lift)
        if not tier:
            continue
        texts = [("hook", o.hook)] if o.hook.strip() else [("title", o.title)]
        for kind, text in texts:
            text = " ".join(str(text or "").split())
            if len(text.split()) < 2:
                continue
            key = (tier, _squash(text))
            ex = Exemplar(text, kind, tier, round(float(o.lift), 3), o.category_id,
                          sports.get(o.category_id, ""), o.origin, float(o.posted_at or 0), shape(text))
            old = best.get(key)
            if old is None or (ex.lift > old.lift if tier == "top" else ex.lift < old.lift):
                best[key] = ex
    out = sorted(best.values(), key=lambda e: (e.tier, -e.lift if e.tier == "top" else e.lift))
    for i, e in enumerate(out):
        e.id = f"{e.tier[0]}{i}"
    return out


# ---------------------------------------------------------------- 2. the memory (SQLite + a word index)

class Memory:
    """learner.db: the exemplars and the index of their words. Safe to share between threads; each process
    opens its own (SQLite's own locking keeps the window and automation from tripping over each other)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.con = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            with self.lock, self.con:
                self.con.execute("PRAGMA journal_mode=WAL")
                self.con.executescript("""
                    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                    CREATE TABLE IF NOT EXISTS exemplars (
                        id TEXT PRIMARY KEY, text TEXT, kind TEXT, tier TEXT, lift REAL, category_id TEXT,
                        sport TEXT, origin TEXT, posted_at REAL, shape TEXT);
                    CREATE TABLE IF NOT EXISTS tokens (token TEXT, id TEXT);
                    CREATE INDEX IF NOT EXISTS tokens_by_token ON tokens (token);
                    CREATE INDEX IF NOT EXISTS exemplars_by_tier ON exemplars (tier);
                """)
                self.con.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        except sqlite3.Error:
            self.con.close()
            raise

    # --- open/close
    def close(self) -> None:
        with self.lock:
            self.con.close()

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- meta
    def meta(self, key: str, default: str = "") -> str:
        with self.lock:
            row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self.lock, self.con:
            self.con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def distilled_at(self) -> float:
        try:
            return float(self.meta("distilled_at", "0") or 0)
        except ValueError:
            return 0.0

    def due(self, now: float, source_written: float = 0.0) -> bool:
        """A day since the last distilling, or results.json is newer than it (posting read new stats)."""
        last = self.distilled_at()
        return now - last >= EVERY or (source_written > last > 0) or last <= 0

    # --- writing
    def replace(self, exemplars: list[Exemplar], now: float, source_written: float = 0.0) -> int:
        """Become `exemplars`, all at once (a reader never sees half of them)."""
        with self.lock, self.con:
            self.con.execute("DELETE FROM exemplars")
            self.con.execute("DELETE FROM tokens")
            for e in exemplars:
                self.con.execute("INSERT INTO exemplars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (e.id, e.text, e.kind, e.tier, e.lift, e.category_id, e.sport, e.origin,
                                  e.posted_at, e.shape))
                self.con.executemany("INSERT INTO tokens VALUES (?, ?)", [(t, e.id) for t in tokens(e.text)])
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('distilled_at', ?)", (str(now),))
            self.con.execute("INSERT OR REPLACE INTO meta VALUES ('source_written', ?)", (str(source_written),))
        return len(exemplars)

    # --- reading
    def all(self, tier: str = "") -> list[Exemplar]:
        with self.lock:
            rows = self.con.execute("SELECT * FROM exemplars" + (" WHERE tier = ?" if tier else ""),
                                    (tier,) if tier else ()).fetchall()
        return [_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.con.execute("SELECT tier, COUNT(*) AS n FROM exemplars GROUP BY tier").fetchall()
        return {r["tier"]: int(r["n"]) for r in rows}

    def search(self, query: str, tier: str = "top", *, category_id: str = "", sport: str = "", k: int = 5,
               since: float = 0.0) -> list[tuple[Exemplar, float]]:
        """The `k` exemplars of `tier` closest to `query`: the words they share, each weighted by how rare it
        is among the exemplars (so "red card" says more than "game"), plus a bonus for the same category and
        the same sport. Only related ones count: sharing a word, the category or the sport; an exemplar
        that shares none of those says nothing about this video. Best first."""
        want = tokens(query)
        with self.lock:
            total = max(1, self.con.execute("SELECT COUNT(*) FROM exemplars").fetchone()[0])
            freq: dict[str, int] = {}
            hits: dict[str, float] = {}
            if want:
                marks = ",".join("?" * len(want))
                for r in self.con.execute(f"SELECT token, COUNT(*) AS n FROM tokens WHERE token IN ({marks}) "
                                          "GROUP BY token", want):
                    freq[r["token"]] = int(r["n"])
                for r in self.con.execute(f"SELECT token, id FROM tokens WHERE token IN ({marks})", want):
                    weight = math.log(1 + total / freq[r["token"]])
                    hits[r["id"]] = hits.get(r["id"], 0.0) + weight
            rows = self.con.execute("SELECT * FROM exemplars WHERE tier = ? AND posted_at >= ?",
                                    (tier, since)).fetchall()
        norm = sum(math.log(1 + total / freq.get(t, 1)) for t in want) or 1.0
        scored = []
        for r in rows:
            e = _row(r)
            score = hits.get(e.id, 0.0) / norm
            if category_id and e.category_id == category_id:
                score += CATEGORY_BONUS
            if sport and e.sport == sport:
                score += SPORT_BONUS
            if score <= 0:
                continue
            # the strongest lift (or the weakest, for what to avoid) breaks ties
            strength = math.log2(max(e.lift, 0.05))
            score += 0.02 * (strength if tier == "top" else -strength)
            scored.append((e, round(score, 4)))
        scored.sort(key=lambda x: (x[1], x[0].posted_at), reverse=True)
        return scored[:k]


def _row(r: sqlite3.Row) -> Exemplar:
    return Exemplar(text=r["text"], kind=r["kind"], tier=r["tier"], lift=float(r["lift"]),
                    category_id=r["category_id"] or "", sport=r["sport"] or "", origin=r["origin"] or "",
                    posted_at=float(r["posted_at"] or 0), shape=r["shape"] or "", id=r["id"])


# ---------------------------------------------------------------- keeping it up to date

def sports_of(s) -> dict[str, str]:
    """category id -> its sport ("" for the rest), from the settings."""
    if s is None:
        return {}
    try:
        from .. import categories

        return {c.id: c.sport for c in categories.load(s)}
    except Exception:  # noqa: BLE001 - categories that can't be read: no sport bonus, nothing else lost
        return {}


def _written_at(results: Path | None) -> float:
    try:
        data = json.loads((results or learn.results_path()).read_text(encoding="utf-8"))
        return float(data.get("written_at") or 0) if isinstance(data, dict) else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


def refresh(s=None, *, now: float | None = None, force: bool = False, memory: Memory | None = None,
            results: Path | None = None, log: Log | None = None) -> int | None:
    """Distil results.json into the memory when it's due (a day since the last time, or new results).
    Returns how many exemplars it holds now, or None when it wasn't due. Never raises for a missing or
    broken file: there's simply nothing to learn from yet."""
    now = time.time() if now is None else now
    own = memory is None
    mem = memory or Memory()
    try:
        written = _written_at(results)
        if not force and not mem.due(now, written):
            return None
        lessons = learn.Lessons.load(results, now=now)
        _fill_categories(lessons)
        found = distill(lessons.outcomes, sports_of(s))
        n = mem.replace(found, now, written)
        if log and found:
            top = sum(1 for e in found if e.tier == "top")
            log(f"Learning: {top} top-performing opening{'s' if top != 1 else ''} and {n - top} that "
                "underperformed, from your posted videos.")
        return n
    finally:
        if own:
            mem.close()


def _fill_categories(lessons: learn.Lessons) -> None:
    """A post from before posting carried its category gets it from picks.json (as learn.for_settings does)."""
    if not any(not o.category_id for o in lessons.outcomes):
        return
    try:
        from ..policy import PickLog, pick_category, picks_path

        picked = {p["id"]: pick_category(p) for p in PickLog(picks_path()).picks}
    except Exception:  # noqa: BLE001 - no picks file
        return
    for o in lessons.outcomes:
        o.category_id = o.category_id or picked.get(o.video_id, "")


# ---------------------------------------------------------------- recalling, for a prompt

@dataclass
class FewShot:
    good: list[Exemplar] = field(default_factory=list)
    bad: list[Exemplar] = field(default_factory=list)
    purpose: str = "hook"

    @property
    def empty(self) -> bool:
        return not self.good and not self.bad

    def text(self) -> str:
        """The paragraph that goes in the prompt ("" when there's nothing to say)."""
        if self.empty:
            return ""
        what = {"hook": "hooks", "script": "openings and titles", "reel": "reel hooks"}.get(self.purpose, "hooks")
        lines: list[str] = []
        if self.good:
            lines.append(f"Earlier {what} that did best on your TikTok accounts ({TOP_LIFT:g}x the account's "
                         "usual views or more), closest to this video first:")
            lines += [f'- "{e.text}" ({_times(e.lift)})' for e in self.good]
        if self.bad:
            lines.append(f"Avoid structures similar to these, which got {BOTTOM_LIFT:g}x the usual views or less:")
            lines += [f'- "{e.text}" ({e.shape}, {_times(e.lift)})' for e in self.bad]
        lines.append("Learn from how the ones that worked open and what they promise. Don't copy their words, and "
                     "keep to what really happens in this video.")
        return "\n".join(lines)


def _times(lift: float) -> str:
    return f"{lift:.1f}×" if lift < 10 else f"{lift:.0f}×"


def recall(query: str, *, category_id: str = "", sport: str = "", purpose: str = "hook",
           memory: Memory | None = None, now: float | None = None, k: int = EXAMPLES[1],
           avoid: int = AVOID) -> FewShot:
    """The exemplars for a prompt about `query`: up to `k` top performers (none unless there are at least
    EXAMPLES[0], since one or two lucky posts are no pattern) and up to `avoid` recent underperformers."""
    now = time.time() if now is None else now
    own = memory is None
    mem = memory or Memory()
    try:
        k = max(EXAMPLES[0], min(EXAMPLES[1], k))
        good = [e for e, _ in mem.search(query, "top", category_id=category_id, sport=sport, k=k)]
        if len(good) < EXAMPLES[0]:
            good = []
        bad = [e for e, _ in mem.search(query, "bottom", category_id=category_id, sport=sport, k=avoid,
                                        since=now - AVOID_DAYS * 86400)] if avoid > 0 else []
        return FewShot(good, bad, purpose)
    finally:
        if own:
            mem.close()


def guide(s, query: str, *, category_id: str = "", sport: str = "", purpose: str = "hook",
          log: Log | None = None, memory: Memory | None = None, results: Path | None = None,
          now: float | None = None) -> str:
    """The few-shot paragraph for a prompt, distilling first when that's due; "" when learning from results
    is off (Settings → Automation) or there's nothing to learn from yet. Never raises: learning is a bonus."""
    if s is not None and not getattr(s, "learn_from_results", True):
        return ""
    own = memory is None
    try:
        mem = memory or Memory()
    except (sqlite3.Error, OSError):
        return ""
    try:
        refresh(s, now=now, memory=mem, results=results)
        return recall(query, category_id=category_id, sport=sport, purpose=purpose, memory=mem, now=now).text()
    except (sqlite3.Error, OSError, ValueError):
        return ""
    finally:
        if own:
            mem.close()


# ---------------------------------------------------------------- 3. pacing

@dataclass
class Pacing:
    """Where the next Creator script aims: words a minute and seconds a picture stays up (None = as usual)."""

    wpm: float | None = None
    scene_seconds: float | None = None
    top: int = 0  # top-performing videos it's from
    bottom: int = 0
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def pace_of(o: learn.Outcome) -> tuple[float, float] | None:
    """(words a minute, seconds per scene) of a posted Creator video, when posting kept its script."""
    if not o.creator or o.length <= 5 or o.words <= 0:
        return None
    wpm = o.words / o.length * 60
    per = o.length / o.scenes if o.scenes > 0 else 0.0
    return wpm, per


def _clamp(x: float, lo_hi: tuple[float, float]) -> float:
    return max(lo_hi[0], min(lo_hi[1], x))


def calibrate(outcomes: Iterable[learn.Outcome], category_id: str = "", default_wpm: float = DEFAULT_WPM,
              default_scene: float = 0.0) -> Pacing:
    """The pace the top-performing Creator videos had (the category's own once it has PACE_MIN of them,
    else every category's), moved n/(n + PACE_SHRINK) of the way from `default_wpm`, and kept in WPM_RANGE
    and SCENE_RANGE. Pacing(None, None) until there are PACE_MIN."""
    rows = [(o, p) for o in outcomes if (p := pace_of(o))]
    own = [(o, p) for o, p in rows if category_id and o.category_id == category_id]
    pool = own if sum(1 for o, _ in own if o.lift >= TOP_LIFT) >= PACE_MIN else rows
    top = [p for o, p in pool if o.lift >= TOP_LIFT]
    bottom = [p for o, p in pool if o.lift <= BOTTOM_LIFT]
    out = Pacing(top=len(top), bottom=len(bottom))
    if len(top) < PACE_MIN:
        return out
    pull = len(top) / (len(top) + PACE_SHRINK)
    best_wpm = statistics.median(w for w, _ in top)
    base_wpm = _clamp(default_wpm or DEFAULT_WPM, WPM_RANGE)
    out.wpm = round(_clamp(base_wpm + (best_wpm - base_wpm) * pull, WPM_RANGE), 1)
    pers = [x for _, x in top if x > 0]
    if pers:
        best_per = statistics.median(pers)
        base_per = _clamp(default_scene or DEFAULT_SCENE, SCENE_RANGE)
        out.scene_seconds = round(_clamp(base_per + (best_per - base_per) * pull, SCENE_RANGE), 2)
    where = "this category's" if pool is own and own else "your"
    slow = bottom and statistics.median(w for w, _ in bottom) < best_wpm
    out.note = (f"Paced like {where} {len(top)} best Creator video{'s' if len(top) != 1 else ''}: about "
                f"{out.wpm:.0f} words a minute" + (f", a new picture every {out.scene_seconds:.1f} s"
                                                    if out.scene_seconds else "") +
                (" (the ones that flopped were slower)." if slow else
                 " (the ones that flopped were faster)." if bottom else "."))
    return out


def pacing(s=None, category_id: str = "", *, default_wpm: float = DEFAULT_WPM, default_scene: float = 0.0,
           results: Path | None = None, now: float | None = None) -> Pacing:
    """calibrate() over results.json; Pacing() when learning from results is off or nothing's there."""
    if s is not None and not getattr(s, "learn_from_results", True):
        return Pacing()
    lessons = learn.Lessons.load(results, now=now)
    return calibrate(lessons.outcomes, category_id, default_wpm, default_scene)


# ---------------------------------------------------------------- for people (ytclip learner)

def report(s=None, *, memory: Memory | None = None, results: Path | None = None, now: float | None = None,
           query: str = "", category_id: str = "") -> dict:
    """What the learner knows, for `ytclip learner` and the tests."""
    now = time.time() if now is None else now
    own = memory is None
    mem = memory or Memory()
    try:
        refresh(s, now=now, memory=mem, results=results)
        shot = recall(query, category_id=category_id, sport=sports_of(s).get(category_id, ""), memory=mem, now=now)
        return {"database": str(mem.path), "distilled_at": mem.distilled_at(), "counts": mem.counts(),
                "top": [e.to_json() for e in sorted(mem.all("top"), key=lambda e: -e.lift)[:10]],
                "bottom": [e.to_json() for e in sorted(mem.all("bottom"), key=lambda e: e.lift)[:10]],
                "prompt": shot.text(), "pacing": pacing(s, category_id, results=results, now=now).to_json()}
    finally:
        if own:
            mem.close()
