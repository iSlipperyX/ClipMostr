"""Clips sent to the linked Macs: each new clip of a category with "Send new clips to my Macs" on (a sports
category has it on from the start) waits here until every linked Mac has it.

Automation adds a video's clips as soon as they're exported (add()), with a hard link to each clip (a copy where
the disk can't link), so posting's Clean up deleting the clip after it posts doesn't take it from a Mac that
hasn't fetched it yet. ClipMonster Remote lists them in its Sports clips inbox, plays them from here, saves
them to the Mac (by itself too, into a folder you pick) and says it has each one (got), so it's shown as new
on no other run of the app and is let go of once every linked Mac has it.

A clip goes once every linked Mac has it or dismissed it (after a day, so a Mac that's just been linked
can still catch up), or after KEEP_DAYS, or when all of them pass MAX_BYTES (the oldest first).

deliveries/deliveries.json (beside the clips) lists them, changed under its lock (edit()) since automation
adds and the server marks from different threads.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from ..posting import safefile
from ..settings import app_dir

KEEP_DAYS = 7.0
ALL_HAVE_IT = 86400.0  # once every Mac has it, it stays this long anyway
MAX_BYTES = 12 * 1024 ** 3
MAX_ITEMS = 400
DAY = 86400.0


def folder() -> Path:
    return app_dir() / "deliveries"


def index_path(where: Path | None = None) -> Path:
    return (where or folder()) / "deliveries.json"


def _empty() -> dict:
    return {"version": 1, "items": []}


def load(where: Path | None = None) -> dict:
    data = safefile.read_raw(index_path(where))
    out = _empty()
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        out["items"] = [i for i in data["items"] if isinstance(i, dict) and i.get("id") and i.get("file")]
    return out


@contextmanager
def edit(where: Path | None = None):
    p = index_path(where)
    with safefile.file_lock(p):
        data = load(where)
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            safefile.save(p, data, indent=1)


def _link_or_copy(src: Path, dest: Path) -> None:
    try:
        os.link(src, dest)
    except OSError:  # another drive, or a disk that can't link
        shutil.copy2(src, dest)


def _text(value, n: int = 300) -> str:
    t = " ".join(str(value or "").split())
    return t[:n]


def add(export_dir: Path, category, now: float | None = None, where: Path | None = None) -> list[dict]:
    """Take in the clips of one exported video (its clips.json): the ones the quality check passed. Returns
    the new deliveries. A video exported again replaces its earlier clips here."""
    now = time.time() if now is None else now
    base = where or folder()
    try:
        meta = json.loads((export_dir / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    video = meta.get("video") if isinstance(meta.get("video"), dict) else {}
    sport = getattr(category, "sport_info", None)
    made: list[dict] = []
    base.mkdir(parents=True, exist_ok=True)
    with edit(where) as data:
        again = [i for i in data["items"] if i.get("folder") == str(export_dir)]
        for old in again:
            _remove_files(base, old)
        data["items"] = [i for i in data["items"] if i.get("folder") != str(export_dir)]
        for rec in meta.get("clips") or []:
            if not isinstance(rec, dict):
                continue
            name = str(rec.get("file") or "")
            src = export_dir / name
            if not name or name.startswith("_rejected/") or (rec.get("qc") or {}).get("ok") is False or not src.is_file():
                continue
            did = secrets.token_hex(6)
            dest = base / f"{did}{src.suffix.lower() or '.mp4'}"
            try:
                _link_or_copy(src, dest)
            except OSError:
                continue
            thumb = ""
            rank = rec.get("rank")
            pic = export_dir / "thumbnails" / f"{int(rank):02d}.jpg" if isinstance(rank, int) else None
            if pic is not None and pic.is_file():
                try:
                    shutil.copy2(pic, base / f"{did}.jpg")
                    thumb = f"{did}.jpg"
                except OSError:
                    pass
            edit_info = rec.get("edit") if isinstance(rec.get("edit"), dict) else {}
            item = {
                "id": did, "at": now, "file": dest.name, "thumb": thumb, "folder": str(export_dir), "name": name,
                "size": dest.stat().st_size,
                "title": _text(rec.get("hook") or rec.get("title") or video.get("title") or name, 160),
                "hook": _text(rec.get("hook"), 160), "reason": _text(rec.get("reason"), 200),
                "length": float(edit_info.get("duration") or rec.get("length") or 0),
                "score": rec.get("score") or 0, "rank": rank if isinstance(rank, int) else 0,
                "start": rec.get("start") or 0, "watch": _text(rec.get("watch"), 400),
                "part": f"Part {rec['part']} of {rec['parts']}" if rec.get("part") and rec.get("parts") else "",
                "category": _text(getattr(category, "name", ""), 80), "category_id": getattr(category, "id", ""),
                "sport": sport.id if sport else "", "sport_label": sport.label if sport else "",
                "emoji": sport.emoji if sport else "",
                "source_title": _text(video.get("title"), 200), "source_channel": _text(video.get("channel"), 120),
                "source_url": _text(meta.get("source") or video.get("url"), 400),
                "got": {}, "seen": {}, "dismissed": {},
            }
            data["items"].append(item)
            made.append(item)
    return made


def _remove_files(base: Path, item: dict) -> None:
    for key in ("file", "thumb"):
        name = str(item.get(key) or "")
        if name and re.fullmatch(r"[0-9a-f]{12}\.\w{2,5}", name):  # only ever our own files
            try:
                (base / name).unlink()
            except OSError:
                pass


def prune(device_ids: list[str], now: float | None = None, where: Path | None = None,
          keep_days: float = KEEP_DAYS, max_bytes: int = MAX_BYTES) -> int:
    """Let go of the clips every linked Mac has (or dismissed), the old ones, and the oldest past the size
    limit. With no Mac linked, none are kept. Returns how many went."""
    now = time.time() if now is None else now
    base = where or folder()
    devices = set(device_ids)
    gone: list[dict] = []
    with edit(where) as data:
        keep: list[dict] = []
        for i in sorted(data["items"], key=lambda i: -float(i.get("at") or 0)):
            done = set(i.get("got") or {}) | set(i.get("dismissed") or {})
            age = now - float(i.get("at") or 0)
            if not devices or age > keep_days * DAY or (devices <= done and age > ALL_HAVE_IT) \
                    or not (base / str(i.get("file"))).is_file():
                gone.append(i)
            else:
                keep.append(i)
        total = 0
        kept: list[dict] = []
        for i in keep:  # newest first
            total += int(i.get("size") or 0)
            if (total > max_bytes or len(kept) >= MAX_ITEMS) and kept:
                gone.append(i)
            else:
                kept.append(i)
        data["items"] = sorted(kept, key=lambda i: float(i.get("at") or 0))
    for i in gone:
        _remove_files(base, i)
    return len(gone)


def find(item_id: str, where: Path | None = None) -> dict | None:
    return next((i for i in load(where)["items"] if i.get("id") == item_id), None)


def mark(item_id: str, device_id: str, what: str, now: float | None = None, where: Path | None = None) -> bool:
    """A Mac has it (got), watched it (seen) or doesn't want it (dismissed). False if it's gone."""
    if what not in ("got", "seen", "dismissed"):
        raise ValueError(what)
    now = time.time() if now is None else now
    with edit(where) as data:
        for i in data["items"]:
            if i.get("id") == item_id:
                marks = i.setdefault(what, {})
                if not isinstance(marks, dict):
                    marks = i[what] = {}
                marks.setdefault(device_id, now)
                return True
    return False


def for_device(device_id: str, where: Path | None = None) -> list[dict]:
    """The clips one Mac can still fetch, newest first, each with new/seen/got for that Mac."""
    out = []
    base = where or folder()
    for i in load(where)["items"]:
        if device_id in (i.get("dismissed") or {}):
            continue
        d = {k: v for k, v in i.items() if k not in ("got", "seen", "dismissed", "folder", "file", "thumb")}
        d["got"] = device_id in (i.get("got") or {})
        d["seen"] = device_id in (i.get("seen") or {})
        d["new"] = not (d["got"] or d["seen"])
        d["has_thumb"] = bool(i.get("thumb")) and (base / str(i["thumb"])).is_file()
        d["ext"] = Path(str(i.get("file"))).suffix or ".mp4"
        out.append(d)
    out.sort(key=lambda d: -float(d.get("at") or 0))
    return out


def file_of(item: dict, what: str = "file", where: Path | None = None) -> Path | None:
    name = str(item.get(what) or "")
    if not name:
        return None
    p = (where or folder()) / name
    return p if p.is_file() else None
