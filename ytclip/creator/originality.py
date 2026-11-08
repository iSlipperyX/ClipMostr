"""Originality: every video the Creator makes is new, never a near-copy of one it made before.

Each finished script is remembered (creator_history.json in ClipMonster's folder: its title, prompt and the
5-word runs of its words). A new script is compared with all of them: the share of its 5-word runs that an
earlier script already had. Rewording a sentence or two changes few runs, so a script that only shuffles an
old one scores high and is written again from another angle (director.write). It's a check on our own
output only, nothing that reads or games TikTok.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..settings import app_dir

KEEP = 400  # scripts remembered
N = 5  # words per run
_lock = threading.Lock()


def path() -> Path:
    return app_dir() / "creator_history.json"


def _words(text: str) -> list[str]:
    return [w for w in re.sub(r"[^\w\s']", " ", str(text or "").lower()).split() if w]


def shingles(text: str, n: int = N) -> set[str]:
    words = _words(text)
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def overlap(new: set[str], old: set[str]) -> float:
    """The share of `new`'s runs that `old` has (0-1)."""
    if not new or not old:
        return 0.0
    return len(new & old) / len(new)


@dataclass
class Entry:
    id: str
    time: float
    title: str
    prompt: str
    runs: list[str]


def load(file: Path | None = None) -> list[Entry]:
    file = file or path()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append(Entry(str(d["id"]), float(d.get("time", 0)), str(d.get("title", "")), str(d.get("prompt", "")),
                             [str(r) for r in d.get("runs") or []]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def closest(script: str, entries: list[Entry] | None = None, skip_id: str = "") -> tuple[float, str]:
    """(similarity 0-1, title) of the earlier script most like `script`."""
    new = shingles(script)
    best, title = 0.0, ""
    for e in load() if entries is None else entries:
        if e.id == skip_id:
            continue
        score = overlap(new, set(e.runs))
        if score > best:
            best, title = score, e.title
    return best, title


def earlier_titles(prompt: str, entries: list[Entry] | None = None, limit: int = 5) -> list[str]:
    """Titles of earlier videos made from much the same prompt, newest first (the director avoids them)."""
    want = set(_words(prompt)) - {"the", "a", "an", "of", "to", "and", "about", "in", "that", "you", "your"}
    if not want:
        return []
    out = []
    for e in sorted(load() if entries is None else entries, key=lambda e: -e.time):
        have = set(_words(e.prompt))
        if want and len(want & have) / len(want) >= 0.6:
            out.append(e.title)
        if len(out) >= limit:
            break
    return out


def remember(entry_id: str, title: str, prompt: str, script: str, file: Path | None = None,
             now: float | None = None) -> None:
    file = file or path()
    with _lock:
        entries = [e for e in load(file) if e.id != entry_id]
        entries.append(Entry(entry_id, now or time.time(), title, prompt, sorted(shingles(script))))
        entries = sorted(entries, key=lambda e: e.time)[-KEEP:]
        tmp = file.with_name(file.name + ".tmp")
        tmp.write_text(json.dumps([e.__dict__ for e in entries], ensure_ascii=False), encoding="utf-8")
        tmp.replace(file)
