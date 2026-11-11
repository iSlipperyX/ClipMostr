"""telegram.json: which phones the bot answers, and what it remembers between restarts.

* the bot the token belongs to (its id and @name), so a new token's bot starts with no phones linked;
* the linked chats: only these can use the bot, and only private chats with a person can be linked. Two
  people at most share a bot (MAX_PEOPLE). The one linked first is its owner: only the owner invites the
  second person and can remove them; either can leave;
* the pairing code the window shows (Settings → Telegram → Link a phone), for ten minutes, and the wrong
  codes each chat sent (five, and it's ignored for an hour);
* where the bot is in Telegram's list of updates (offset), so nothing is handled twice;
* the videos it showed you (to mark their messages when they're OK'd or deleted somewhere else), the ones
  it only counted in a message (noted), when it last looked for new ones (seen_at), and what you asked for
  (a video to make, a link to clip, a request), to send you when it's done;
* which chats' menu button (left of the message box) opens the Mini App, and at which address (menus);
* the critiques people wrote of a video, each with who wrote it and the video made again from it, and the
  last things each person did (activity), which the Mini App shows with their names.

The window and automation both change it, so every change reads the file, changes it and writes it under
its lock (edit()). telegram-status.json is the bot's heartbeat for the window: whether it's running in
automation right now, as which bot, and its last problem.
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

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to mix up
CODE_LENGTH = 6
CODE_LIFE = 10 * 60.0
MAX_STRIKES = 5  # wrong codes from one chat before it's ignored for a while
STRIKE_PAUSE = 3600.0
ASKED_KEEP = 3 * 86400.0  # what you asked for is followed this long at most
SHOWN_KEEP = 14 * 86400.0
STATUS_FRESH = 90.0  # a heartbeat older than this means the bot isn't running
MAX_PEOPLE = 2  # how many people can share one bot
CRITIQUES_KEEP = 60 * 86400.0
ACTIVITY_MAX = 80


def path() -> Path:
    return app_dir() / "telegram.json"


def status_path() -> Path:
    return app_dir() / "telegram-status.json"


def empty() -> dict:
    return {"version": 1, "bot": {}, "chats": [], "pairing": {}, "strikes": {}, "offset": 0, "shown": {},
            "asked": {}, "told": {}, "noted": {}, "seen_at": 0.0, "critiques": {}, "activity": [], "menus": {}}


def load(where: Path | None = None) -> dict:
    data = safefile.read_raw(where or path())
    out = empty()
    if isinstance(data, dict):
        for key, default in out.items():
            value = data.get(key, default)
            if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            out[key] = value if isinstance(value, type(default)) else default
    out["chats"] = [c for c in out["chats"] if isinstance(c, dict) and isinstance(c.get("id"), int)]
    return out


@contextmanager
def edit(where: Path | None = None):
    """Read telegram.json, let the caller change it, write it back, all under its lock."""
    p = where or path()
    with safefile.file_lock(p):
        data = load(p)
        yield data
        safefile.save(p, data, indent=1)


# --- linked chats --------------------------------------------------------------------------------------


def chat_ids(data: dict) -> list[int]:
    return [c["id"] for c in data.get("chats", [])]


def is_linked(data: dict, chat_id: int) -> bool:
    return chat_id in chat_ids(data)


def chat_name(chat: dict) -> str:
    """'Justyn (@justyn)' for a linked chat."""
    name = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip() or str(chat.get("name") or "")
    user = chat.get("username") or ""
    return f"{name} (@{user})" if name and user else name or (f"@{user}" if user else f"chat {chat.get('id')}")


def link(data: dict, chat: dict, now: float) -> dict:
    """Add a private chat to the linked ones (or refresh its name). Returns its entry."""
    entry = next((c for c in data["chats"] if c["id"] == chat["id"]), None)
    if entry is None:
        entry = {"id": int(chat["id"]), "linked_at": now}
        data["chats"].append(entry)
    entry["name"] = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name")).strip()
    entry["username"] = str(chat.get("username") or "")
    entry.pop("blocked", None)
    return entry


def people(data: dict) -> list[dict]:
    """The linked chats, the owner (linked first) first."""
    return sorted(data.get("chats", []), key=lambda c: float(c.get("linked_at") or 0))


def owner(data: dict) -> dict | None:
    linked = people(data)
    return linked[0] if linked else None


def is_owner(data: dict, chat_id: int) -> bool:
    first = owner(data)
    return first is not None and first["id"] == chat_id


def full(data: dict) -> bool:
    return len(data.get("chats", [])) >= MAX_PEOPLE


def first_name(data: dict, chat_id: int) -> str:
    """What to call a linked person in a line about what they did: 'Justyn', '@justyn' or 'someone'."""
    c = next((c for c in data.get("chats", []) if c["id"] == chat_id), None)
    if c is None:
        return "someone"
    return str(c.get("name") or "").split(" ")[0] or (f"@{c['username']}" if c.get("username") else "someone")


def trim_people(data: dict) -> list[dict]:
    """Keep the two people linked first (a telegram.json from before the limit may hold more). Returns the
    chats that were unlinked."""
    linked = people(data)
    extra = linked[MAX_PEOPLE:]
    for c in extra:
        unlink(data, c["id"])
    return extra


def unlink(data: dict, chat_id: int) -> bool:
    before = len(data["chats"])
    data["chats"] = [c for c in data["chats"] if c["id"] != chat_id]
    if (data.get("pairing") or {}).get("by") == chat_id:
        data["pairing"] = {}  # an invite they made goes with them
    for d in (data.get("asked") or {}).values():
        if isinstance(d, dict) and d.get("chat") == chat_id:
            d["chat"] = 0
    return len(data["chats"]) != before


def set_bot(data: dict, me: dict) -> bool:
    """Remember which bot the token is for. A different bot than before: its chats aren't linked to it,
    so they're forgotten (and so is the place in the old bot's updates). True if the bot changed."""
    old = (data.get("bot") or {}).get("id")
    changed = bool(old) and old != me.get("id")
    data["bot"] = {"id": me.get("id"), "username": me.get("username") or "", "name": me.get("first_name") or ""}
    if changed:
        data.update(chats=[], offset=0, shown={}, asked={}, strikes={}, pairing={}, noted={}, seen_at=0.0,
                    menus={})
    return changed


# --- pairing -----------------------------------------------------------------------------------------


def new_code(data: dict, now: float, by: int = 0) -> str:
    """A fresh code. `by` is the linked chat that asked for it as an invite (0 = the window on the PC)."""
    code = "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LENGTH))
    data["pairing"] = {"code": code, "until": now + CODE_LIFE, "by": int(by)}
    return code


def live_code(data: dict, now: float) -> str:
    """The code that is up right now, if one is."""
    pairing = data.get("pairing") or {}
    return str(pairing.get("code") or "") if now <= float(pairing.get("until") or 0) else ""


def clean_code(text: str) -> str:
    """What someone typed as the code: '/start k7m2qx', 'K7M 2QX', 'k7m2qx' → 'K7M2QX'."""
    t = (text or "").strip()
    if t.lower().startswith("/start"):
        t = t[6:]
    t = t.split("@", 1)[0] if t.startswith("@") else t
    return "".join(ch for ch in t.upper() if ch.isalnum())


def looks_like_code(text: str) -> bool:
    """A try at the code ('/start K7M2QX', 'k7m 2qx'), not just any message."""
    t = (text or "").strip()
    if t.lower().startswith("/start"):
        return bool(clean_code(t))
    code = clean_code(t)
    return len(code) == CODE_LENGTH and set(code) <= set(CODE_CHARS) and len(t) <= CODE_LENGTH + 4


def try_code(data: dict, chat_id: int, text: str, now: float) -> str:
    """Check a code a chat sent: "linked", "wrong", "expired", "none" (no code waiting) or "blocked"
    (too many wrong ones). A right code is used up."""
    strikes = data.setdefault("strikes", {})
    mine = strikes.get(str(chat_id)) or [0, 0.0]
    if mine[0] >= MAX_STRIKES and now < mine[1]:
        return "blocked"
    pairing = data.get("pairing") or {}
    code = pairing.get("code") or ""
    if not code:
        return "none"
    if now > float(pairing.get("until") or 0):
        data["pairing"] = {}
        return "expired"
    if secrets.compare_digest(clean_code(text), code):
        data["pairing"] = {}
        strikes.pop(str(chat_id), None)
        return "linked"
    count = (mine[0] if now < mine[1] else 0) + 1  # each wrong code counts for an hour
    strikes[str(chat_id)] = [count, now + STRIKE_PAUSE]
    return "blocked" if count >= MAX_STRIKES else "wrong"


# --- what was shown and asked for ---------------------------------------------------------------------


def tidy(data: dict, now: float) -> None:
    data["shown"] = {k: v for k, v in data.get("shown", {}).items()
                     if isinstance(v, dict) and now - float(v.get("at") or 0) < SHOWN_KEEP}
    data["asked"] = {k: v for k, v in data.get("asked", {}).items()
                     if isinstance(v, dict) and now - float(v.get("at") or 0) < ASKED_KEEP}
    data["strikes"] = {k: v for k, v in data.get("strikes", {}).items()
                       if isinstance(v, list) and len(v) == 2 and now < float(v[1] or 0)}
    for key in ("told", "noted"):
        data[key] = {k: v for k, v in data.get(key, {}).items() if isinstance(v, (int, float)) and now - v < SHOWN_KEEP}
    data["critiques"] = {k: v for k, v in data.get("critiques", {}).items()
                         if isinstance(v, dict) and now - float(v.get("at") or 0) < CRITIQUES_KEEP}
    data["activity"] = [a for a in data.get("activity", []) if isinstance(a, dict)][-ACTIVITY_MAX:]


# --- who did what ---------------------------------------------------------------------------------------


def note_activity(data: dict, chat_id: int, what: str, title: str, now: float, item: str = "",
                  text: str = "") -> dict:
    """Remember that a linked person did something ("ok", "delete", "post_now", "critique", "make", "pause",
    "resume", "invite", "join", "leave", "remove"), for the other one to see."""
    entry = {"at": now, "by": int(chat_id), "name": first_name(data, chat_id), "what": what,
             "title": " ".join(str(title or "").split())[:120], "item": item,
             "text": " ".join(str(text or "").split())[:300]}
    data["activity"] = [*data.get("activity", []), entry][-ACTIVITY_MAX:]
    return entry


def add_critique(data: dict, chat_id: int, item: str, title: str, text: str, job: str, now: float) -> dict:
    cid = secrets.token_hex(5)
    entry = {"id": cid, "item": item, "title": " ".join(str(title or "").split())[:120], "text": text,
             "by": int(chat_id), "name": first_name(data, chat_id), "at": now, "job": job, "status": "queued",
             "error": "", "fixed": ""}
    data.setdefault("critiques", {})[cid] = entry
    return entry


def critiques_of(data: dict, item: str) -> list[dict]:
    """What was said about a video (or about the video it was made again from), oldest first."""
    found = [c for c in (data.get("critiques") or {}).values() if c.get("item") == item or c.get("fixed") == item]
    return sorted(found, key=lambda c: float(c.get("at") or 0))


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
    """The bot's last heartbeat, with "running": whether it's fresh and its process is still there."""
    now = time.time() if now is None else now
    try:
        info = json.loads((where or status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False}
    if not isinstance(info, dict):
        return {"running": False}
    from ..procs import process_alive

    fresh = now - float(info.get("at") or 0) < STATUS_FRESH
    info["running"] = bool(fresh and info.get("polling") and process_alive(info.get("pid") or 0))
    return info
