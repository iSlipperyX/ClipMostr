"""remote.json: the Macs linked to this PC, and the code the window shows while one is being linked.

* this PC's id (made once), so a Mac knows it's the same host after its address changes;
* the linked Macs: an id, a name, when it was linked and last seen (and from where), and the hash of its key
  (the key itself is only on the Mac, in its Keychain);
* the pairing code Settings → Mac app → Link a Mac shows, for ten minutes, with its salt and the wrong tries.

The window and automation both change it, so each change reads, changes and writes it under its lock (edit()),
as telegram/state.py does. remote-status.json is the server's heartbeat for the window.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from pathlib import Path

from ..posting import safefile
from ..settings import app_dir
from . import protocol

STATUS_FRESH = 90.0  # a heartbeat older than this means the server isn't running
SEEN_EVERY = 60.0  # how often a Mac's "last seen" is written


def path() -> Path:
    return app_dir() / "remote.json"


def status_path() -> Path:
    return app_dir() / "remote-status.json"


def empty() -> dict:
    return {"version": 1, "host_id": "", "devices": [], "pairing": {}}


def load(where: Path | None = None) -> dict:
    data = safefile.read_raw(where or path())
    out = empty()
    if isinstance(data, dict):
        for key, default in out.items():
            value = data.get(key, default)
            out[key] = value if isinstance(value, type(default)) else default
    out["devices"] = [d for d in out["devices"] if isinstance(d, dict) and d.get("id") and d.get("token_hash")]
    return out


@contextmanager
def edit(where: Path | None = None):
    """Read remote.json, let the caller change it, write it back, all under its lock."""
    p = where or path()
    with safefile.file_lock(p):
        data = load(p)
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            safefile.save(p, data, indent=1)


def host_id(data: dict) -> str:
    if not data.get("host_id"):
        data["host_id"] = secrets.token_hex(8)
    return data["host_id"]


# --- pairing -----------------------------------------------------------------------------------------


def start_pairing(data: dict, now: float) -> str:
    """A new code (the old one stops working). Returns it."""
    code = protocol.new_code()
    data["pairing"] = {"code": code, "salt": secrets.token_hex(16), "until": now + protocol.CODE_LIFE, "wrong": 0,
                       "iterations": protocol.ITERATIONS}
    return code


def stop_pairing(data: dict) -> None:
    data["pairing"] = {}


def pairing(data: dict, now: float) -> dict:
    """The code that's up, or {} when there's none (or it ran out)."""
    p = data.get("pairing") or {}
    if not p.get("code") or now > float(p.get("until") or 0) or int(p.get("wrong") or 0) >= protocol.MAX_WRONG:
        return {}
    return p


def new_device(data: dict, name: str, now: float, address: str = "") -> tuple[dict, str]:
    """Link a Mac: its entry and its key (returned once, never stored)."""
    token = secrets.token_urlsafe(32)
    entry = {"id": secrets.token_hex(6), "name": " ".join(str(name or "Mac").split())[:60] or "Mac",
             "token_hash": protocol.token_hash(token), "linked_at": now, "seen_at": now, "address": address}
    data["devices"].append(entry)
    return entry, token


def find_device(data: dict, device_id: str, token: str) -> dict | None:
    """The linked Mac this key belongs to, or None."""
    for d in data.get("devices", []):
        if protocol.same(d.get("id"), device_id) and protocol.same(d.get("token_hash"), protocol.token_hash(token)):
            return d
    return None


def linked_ids(where: Path | None = None) -> set[str] | None:
    """The linked Macs' ids, or None when remote.json can't be read just now (which is not "no Mac is linked")."""
    p = where or path()
    if not p.exists():  # no Mac was ever linked
        return set()
    if not isinstance(safefile.read_raw(p), dict):
        return None
    return {d["id"] for d in load(p)["devices"]}


def unlink(data: dict, device_id: str) -> bool:
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d.get("id") != device_id]
    return len(data["devices"]) != before


# --- the heartbeat ------------------------------------------------------------------------------------


def write_status(info: dict, where: Path | None = None) -> None:
    p = where or status_path()
    try:
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def read_status(where: Path | None = None, now: float | None = None) -> dict:
    """The server's last heartbeat, with "running": whether it's fresh and its process is still there."""
    now = time.time() if now is None else now
    try:
        info = json.loads((where or status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    if not isinstance(info, dict):
        return {"running": False}
    from ..procs import process_alive

    fresh = now - float(info.get("at") or 0) < STATUS_FRESH
    info["running"] = bool(fresh and info.get("listening") and process_alive(info.get("pid") or 0))
    return info
