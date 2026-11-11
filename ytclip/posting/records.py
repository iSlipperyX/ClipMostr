"""A record of every posted clip, and clearing its files away once it's surely on TikTok.

Once a post is seen on the account's profile (live.py links it to its TikTok video), finish() writes
the clip's record and then deletes its files:

* the record: a .txt to read and a .json beside it, in Documents\\ClipMonster\\Posted videos\\<account>,
  with the post (when, link, caption, hashtags), the clip (title, hook, length, format, part), where it
  came from (the YouTube video, channel, licence, where in it the clip starts and ends), why it was made
  (category, how the video was picked, the clip finder's score, the AI's rating, the quality check) and how
  it did. The numbers are snapshotted 1 hour, 24 hours and 7 days after posting and the record is
  rewritten as they come in (update_records), for 30 days.
* then the files: the clip, ClipMonster's .srt and thumbnail for it and its row in the folder's
  clips.json and README.md, posting's thumbnail and cover of it, and the video folder once nothing's left
  in it (never the Automation folder itself, or a category's folder right inside it). A Video Creator
  video's folder goes the same way, with its scene pictures and storyboard: its record keeps the prompt,
  the script, the voice, the pictures' source, the sources and the fact check.

No record, no deleting. A post that isn't seen on the profile within two days (TikTok may have taken it
down, or it's private) is kept, with the reason, so nothing is lost if a post silently failed. The
entry itself stays in the library, marked cleaned up, so the post history, the Performance tab and
learning from results carry on.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .accounts import Account, Accounts
from .clipsjson import REJECTED_DIR, credit_line
from .config import Settings, thumbs_dir
from .media import VIDEO_EXTS
from .store import Store, VideoItem, _key
from .tiktok import compose_caption

HOUR = 3600.0
DAY = 86400.0
RECORDS_NAME = "Posted videos"
UNSEEN_KEEP = 2 * DAY  # a post not seen on the profile by then keeps its files (and says why)
SNAPSHOTS = (("1h", HOUR), ("24h", DAY), ("7d", 7 * DAY))
FOLLOW_FOR = 30 * DAY  # records are kept up to date this long after posting
REWRITE_EVERY = 6 * HOUR  # …at most this often, besides each snapshot
HOW = {"browser": "posted by ClipMonster", "seen": "found on TikTok after an interrupted upload",
       "hand": "marked as posted by hand"}
UNSEEN = ("Not seen on the TikTok profile within 2 days of posting, so its files were kept. Check the post on "
          "TikTok: if it's there and public, it will be cleared once it's seen.")


def documents_dir() -> Path:
    """Windows' Documents folder, wherever it is (OneDrive moves it), else ~/Documents."""
    if os.name == "nt":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # CSIDL_PERSONAL
                return Path(buf.value)
        except (OSError, AttributeError):
            pass
    return Path.home() / "Documents"


def default_records_folder() -> Path:
    return documents_dir() / "ClipMonster" / RECORDS_NAME


def records_folder(s: Settings) -> Path:
    return Path(s.records_folder).expanduser() if s.records_folder else default_records_folder()


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", " ".join((text or "").split())).strip(" .")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(" .,;-") or text[:limit]
    return text or "clip"


def move_to_trash(path: Path) -> None:
    from PySide6.QtCore import QFile

    ok = QFile.moveToTrash(str(path))
    if isinstance(ok, tuple):  # some bindings also return the new path
        ok = ok[0]
    if not ok:
        raise OSError("couldn't move it to the Recycle Bin")


@dataclass
class Result:
    cleaned: int = 0  # clips whose files went
    recorded: int = 0  # records written or brought up to date
    freed: int = 0  # bytes
    kept: list[str] = field(default_factory=list)  # headlines kept, with why (once each)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (what, why)

    def summary(self) -> str:
        if not self.cleaned:
            return ""
        return (f"{self.cleaned} posted clip{'s' * (self.cleaned != 1)} recorded and cleared away "
                f"({size_text(self.freed)} freed).")


# --- what's due -------------------------------------------------------------------------------


def finish(store: Store, accounts: Accounts | list[Account], s: Settings, roots: list[Path],
           now: float | None = None, remove: Callable[[Path], None] | None = None) -> Result:
    """Record and clear every posted clip that's surely on TikTok. `roots` are automation's folders (the
    folders right inside them stay). Marks the entries; the caller saves."""
    now = time.time() if now is None else now
    remove = remove or (move_to_trash if s.recycle else _unlink)
    accounts = list(accounts)
    r = Result()
    with store.lock:
        items = list(store.items.values())
    users = _users(items)
    touched: dict[Path, set[str]] = {}
    for it in items:
        if it.status != "posted" or it.cleaned_at:
            continue
        account = next((a for a in accounts if a.id == it.account), None)
        if not it.tiktok_id:
            if now - (it.posted_at or now) >= UNSEEN_KEEP and not it.keep_reason:
                with store.lock:
                    it.keep_reason = UNSEEN
                r.kept.append(f"{it.headline}: {UNSEEN}")
                try:  # what's known so far, so there's a record either way
                    write_record(store, it, account, s, now)
                except OSError as exc:
                    r.failed.append((it.headline, f"couldn't write its record: {exc}"))
            continue
        if not s.delete_after_post:
            if not it.record:
                try:
                    write_record(store, it, account, s, now)
                    r.recorded += 1
                except OSError as exc:
                    r.failed.append((it.headline, f"couldn't write its record: {exc}"))
            continue
        # The record first: no record, nothing deleted.
        try:
            write_record(store, it, account, s, now, cleared=True)
        except OSError as exc:
            r.failed.append((it.headline, f"couldn't write its record: {exc}"))
            continue
        r.recorded += 1
        files, other = _files(it, users)
        failed: list[Path] = []
        for f in files:
            size = _size(f)
            try:
                if f.exists():
                    remove(f)
            except OSError as exc:
                r.failed.append((str(f), str(exc) or exc.__class__.__name__))
                failed.append(f)
                continue
            r.freed += size
        video = Path(it.path)
        if video in files and video not in failed:
            touched.setdefault(video.parent, set()).add(video.name)
        with store.lock:
            if it.thumb and not Path(it.thumb).exists():
                it.thumb = ""
            if it.cover and not Path(it.cover).exists():
                it.cover = ""
            if failed:  # tried again next time
                continue
            it.cleaned_at = now
            it.keep_reason = f"The file stays: it's also {other}." if other else ""
        r.cleaned += 1
    for folder, names in touched.items():
        try:
            r.freed += _tidy(folder, names, roots, remove)
        except OSError as exc:
            r.failed.append((str(folder), str(exc) or exc.__class__.__name__))
    return r


@dataclass
class Discarded:
    removed: list[str] = field(default_factory=list)  # the library entries taken out
    freed: int = 0  # bytes
    failed: list[tuple[str, str]] = field(default_factory=list)  # (file, why)


def discard(store: Store, item_id: str, s: Settings, roots: list[Path],
            remove: Callable[[Path], None] | None = None) -> Discarded:
    """Delete a video you don't want posted (the Telegram bot's Delete): every library entry of it that
    hasn't posted (one per account it's queued for), and its files, the way Clean up clears a posted clip:
    the clip, its captions and thumbnail, posting's thumbnail and cover, and its row in the folder's
    clips.json and README.md, or the folder when it was the last clip there (with a Creator video's scene
    pictures and storyboard). No record is written: it never went out. The entries are taken out of the
    library for good (automation won't bring them back). ValueError says why it can't be: it's posting
    right now, or it's posted already."""
    remove = remove or (move_to_trash if s.recycle else _unlink)
    with store.lock:
        it = store.items.get(item_id)
        if it is None:
            raise ValueError("It isn't in the posting library any more.")
        same = [o for o in store.items.values() if o.path and _key(o.path) == _key(it.path)] if it.path else [it]
        if any(o.status == "uploading" for o in same):
            raise ValueError("It's being posted right now. Wait for the post to finish, then delete it on TikTok "
                             "if it's not wanted.")
        if it.status == "posted":
            raise ValueError("It's posted already, so its files go by themselves once TikTok shows the post. To "
                             "take it down, delete it on TikTok.")
        going = [o for o in same if o.status != "posted"]
        if it not in going:
            going.append(it)
    r = Discarded()
    video = Path(it.path) if it.path else None
    files: list[Path] = []
    if video is not None and video.is_file():
        files.append(video)
        files += [c for c in _clip_companions(video) if c.is_file()]
    from .cover import covers_dir

    for o in going:
        for picture, home in ((o.thumb, thumbs_dir()), (o.cover, covers_dir())):
            p = Path(picture) if picture else None
            if p is not None and p.is_file() and _inside(p, home) and p not in files:
                files.append(p)
    for f in files:
        size = _size(f)
        try:
            remove(f)
        except OSError as exc:
            r.failed.append((str(f), str(exc) or exc.__class__.__name__))
            continue
        r.freed += size
    if video is not None and not video.exists():
        try:
            r.freed += _tidy(video.parent, {video.name}, roots, remove)
        except OSError as exc:
            r.failed.append((str(video.parent), str(exc) or exc.__class__.__name__))
    if video is not None and video.exists():  # the video itself couldn't go: the entries stay, to try again
        return r
    r.removed = [o.id for o in going]
    store.remove(r.removed)
    return r


def update_records(store: Store, accounts: Accounts | list[Account], s: Settings,
                   now: float | None = None) -> int:
    """Take the 1 h / 24 h / 7 d snapshots that are due and rewrite those records (and any not rewritten for
    a while in their first 30 days). Returns how many records were rewritten."""
    now = time.time() if now is None else now
    accounts = list(accounts)
    n = 0
    with store.lock:
        items = [i for i in store.items.values() if i.status == "posted" and i.record]
    for it in items:
        v = store.video_for(it)
        if v is None:
            continue
        posted = v.create_time or it.posted_at or 0
        if not posted:
            continue
        due = False
        with store.lock:
            for name, after in SNAPSHOTS:
                if name not in it.snapshots and now - posted >= after:
                    it.snapshots[name] = [now, v.views, v.likes, v.comments, v.shares]
                    due = True
        if not due and (now - posted > FOLLOW_FOR + REWRITE_EVERY or now - it.record_at < REWRITE_EVERY):
            continue
        account = next((a for a in accounts if a.id == it.account), None)
        try:
            write_record(store, it, account, s, now, cleared=bool(it.cleaned_at))
            n += 1
        except OSError:
            continue
    return n


# --- the record -------------------------------------------------------------------------------


def write_record(store: Store, it: VideoItem, account: Account | None, s: Settings, now: float,
                 cleared: bool = False) -> Path:
    """Write (or rewrite) the clip's .txt and .json record. Raises OSError when it can't."""
    path = _record_path(records_folder(s), it, account.label if account else "Removed account")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = record_text(store, it, account, s, now, cleared)
    data = record_data(store, it, account, s, now, cleared)
    _write(path, text)
    _write(path.with_suffix(".json"), json.dumps(data, indent=2, ensure_ascii=False))
    with store.lock:
        it.record, it.record_at = str(path), now
    return path


def record_data(store: Store, it: VideoItem, account: Account | None, s: Settings, now: float,
                cleared: bool = False) -> dict:
    """The record as data, for scripts and spreadsheets."""
    v = store.video_for(it)
    info = it.info or {}
    stats = None
    if v is not None:
        stats = {"views": v.views, "likes": v.likes, "comments": v.comments, "shares": v.shares,
                 "saves": v.saves, "engagement": round(v.engagement, 2),
                 "read_at": _iso(v.history[-1][0]) if v.history else ""}
    lift = _lift(store, it, now)
    return {
        "version": 1,
        "title": it.headline,
        "clip_name": it.title,
        "account": {"name": account.label, "username": account.username} if account else None,
        "posted_at": _iso(v.create_time if v and v.create_time else it.posted_at),
        "posted_how": HOW.get(it.posted_via, it.posted_via),
        "tiktok_url": it.tiktok_url or (v.url if v else ""),
        "tiktok_id": it.tiktok_id,
        "caption": v.description if v and v.description else _caption(it, account, s),
        "hashtags": it.hashtags,
        "privacy": it.privacy or (account.default_privacy if account else s.default_privacy),
        "attempts": it.attempts,
        "clip": {"length": round(it.duration, 2) if it.duration else None,
                 "width": info.get("width"), "height": info.get("height"), "fps": info.get("fps"),
                 "size_bytes": info.get("size_bytes"), "file": it.path, "hook": it.hook,
                 "part": it.part, "parts": it.parts, "posting_score": it.total,
                 "ai_rating": it.ai.get("appeal") if it.ai else None},
        "ai_generated": bool(it.ai_generated),
        "creator": creator_story(it) or None,
        "source": {k.removeprefix("source_"): v2 for k, v2 in it.facts.items() if k != "creator"} | {k: v2 for k, v2 in (("channel", it.source_channel), ("url", it.source_url),
                                                          ("reason", it.source_reason),
                                                          ("permission", it.source_permission),
                                                          ("clip_score", it.source_score),
                                                          ("stands_alone", it.source_rating),
                                                          ("credit", it.credit),
                                                          ("category", it.category_name)) if v2 not in (None, "")},
        "qc_warnings": list(it.warnings),
        "stats": stats,
        "snapshots": {k: {"at": _iso(p[0]), "views": p[1], "likes": p[2], "comments": p[3], "shares": p[4]}
                      for k, p in it.snapshots.items()},
        "lift": lift,
        "added_at": _iso(it.added_at),
        "files_cleared_at": _iso(it.cleaned_at or now) if cleared else "",
        "kept": it.keep_reason,
        "written_at": _iso(now),
    }


def record_text(store: Store, it: VideoItem, account: Account | None, s: Settings, now: float,
                cleared: bool = False) -> str:
    """What the .txt record says."""
    v = store.video_for(it)
    lines = ["ClipMonster: posted clip record", "=" * 31, ""]

    def row(name: str, value) -> None:
        if value not in (None, ""):
            lines.append(f"{name + ':':<15}{value}")

    def sub(name: str, value) -> None:
        if value not in (None, ""):
            lines.append(f"  {name + ':':<15}{value}")

    row("Title", it.headline)
    if it.title and it.title != it.headline:
        row("Clip name", it.title)
    if account is not None:
        handle = f"@{account.username}" if account.username else ""
        named = account.name and handle and account.name.lstrip("@").lower() != account.username.lower()
        row("Account", f"{account.name} ({handle})" if named else handle or account.label)
    else:
        row("Account", "an account since removed")
    row("Posted", _when(v.create_time if v and v.create_time else it.posted_at)
        + (f" ({HOW[it.posted_via]})" if it.posted_via in HOW else ""))
    row("TikTok link", it.tiktok_url or (v.url if v else "") or "not seen on TikTok yet")
    row("Part", it.part_label)
    if it.cover_set is not None and it.cover_at is not None:
        row("Cover", f"{it.cover_why}, {it.cover_at:.1f} s in" if it.cover_set else
            f"TikTok's own pick (ours, {it.cover_why}, didn't go in)")
    if it.attempts > 1:
        row("Tries", it.attempts)

    caption = v.description if v and v.description else _caption(it, account, s)
    lines += ["", "Caption" + (" on TikTok:" if v and v.description else " as posted:")]
    lines += [f"  {line}" for line in caption.splitlines() or [""]]

    lines += ["", "How it did:"]
    if v is not None:
        read = v.history[-1][0] if v.history else 0
        lines.append(f"  Latest{f' ({_when(read)})' if read else ''}: {_numbers(v.views, v.likes, v.comments, v.shares)}"
                     + (f" · {v.saves:,} saves" if v.saves else ""))
        if v.views:
            lines.append(f"  Engagement {v.engagement:.1f}%")
    for name, label in (("1h", "After 1 hour"), ("24h", "After 24 hours"), ("7d", "After 7 days")):
        p = it.snapshots.get(name)
        if p:
            lines.append(f"  {label}: {_numbers(*p[1:5])}")
    lift = _lift(store, it, now)
    if lift is not None:
        lines.append(f"  {lift:.1f}× the account's usual views")
    if v is None:
        lines.append("  Not read yet.")

    info = it.info or {}
    lines += ["", "The clip:"]
    sub("Length", _length(it.duration) if it.duration else "")
    if info.get("width") and info.get("height"):
        fps = f", {info['fps']:.0f} fps" if info.get("fps") else ""
        sub("Format", f"{info['width']}×{info['height']}{fps}")
    sub("File size", size_text(info["size_bytes"]) if info.get("size_bytes") else "")
    sub("Hook", it.hook)
    sub("Hashtags", it.hashtags)
    sub("Posting score", f"{it.total:.0f} / 100" if it.total is not None else "")
    sub("Style", it.facts.get("style"))
    sub("Clip finder", it.facts.get("finder"))
    sub("Quality notes", "; ".join(it.warnings))

    f = it.facts
    story = creator_story(it)
    if story:
        lines += ["", "Made by the Video Creator:"] + creator_lines(story)
    elif f or it.source_channel or it.source_url:
        lines += ["", "Where it came from:"]
        sub("Video", f.get("source_title"))
        sub("Channel", it.source_channel or f.get("source_channel"))
        sub("Link", f.get("watch") or it.source_url or f.get("source_url"))
        if isinstance(f.get("start"), (int, float)) and isinstance(f.get("end"), (int, float)):
            sub("Clip of it", f"{_length(f['start'])} to {_length(f['end'])}")
        sub("Licence", f.get("license"))
        sub("Credit", it.credit or (credit_line(it.source_channel) if "creative commons" in
                                    (f.get("license") or "").lower() else ""))

    lines += ["", "Why it was made:"]
    sub("Category", it.category_name)
    sub("Picked", f.get("how_picked"))
    sub("Allowed by", it.source_permission or f.get("permission"))
    sub("Request", f.get("request"))
    sub("Moment", it.source_reason or f.get("reason"))
    sub("Clip score", f"{it.source_score:.0f} / 100" if it.source_score is not None else "")
    sub("Stands alone", f"{it.source_rating} / 10 (AI)" if it.source_rating is not None else "")
    if lines[-1] == "Why it was made:":
        lines.pop()
        lines.pop()

    lines += ["", f"Made {_when(it.added_at)}."]
    if cleared:
        lines.append(f"Files cleared {_when(it.cleaned_at or now)}: the clip was deleted to free space.")
    if it.keep_reason:
        lines.append(it.keep_reason)
    lines.append(f"Record written {_when(now)}.")
    return "\n".join(lines + [""])


def creator_story(it: VideoItem) -> dict:
    """How a Video Creator video was made (clipsjson.creator_story): from the posting library, or for one queued
    before 3.5, from its folder while that's still there, and then kept in the entry for the record's rewrites."""
    have = it.facts.get("creator")
    if isinstance(have, dict) and have:
        return have
    if not it.ai_generated or not it.path:
        return {}
    from .clipsjson import creator_story as read

    found = read(Path(it.path).parent)
    if found:
        it.facts["creator"] = found  # the caller saves the store
    return found


def creator_lines(story: dict) -> list[str]:
    """The record's "Made by the Video Creator" lines."""
    out = []

    def sub(name: str, value) -> None:
        if value not in (None, "", []):
            out.append(f"  {name + ':':<15}{value}")

    sub("Prompt", story.get("prompt"))
    sub("Written by", story.get("written_by"))
    voice, engine = story.get("voice"), story.get("voice_engine")
    sub("Voice", f"{voice} ({engine})" if voice and engine else voice or engine)
    sub("Look", story.get("look"))
    sub("Pictures", ", ".join(story.get("pictures") or []))
    sub("Music", story.get("music"))
    check = story.get("factcheck") or {}
    if check.get("skipped"):
        sub("Fact check", f"not run ({check['skipped']})")
    elif check:
        bits = [f"{check.get('checked', 0)} sentences checked" + (f" by {check['model']}" if check.get("model") else "")]
        if check.get("fixed"):
            bits.append(f"{check['fixed']} fixed")
        if check.get("to_check"):
            bits.append(f"{check['to_check']} to check yourself")
        sub("Fact check", ", ".join(bits))
    sub("Labelled", "AI-generated")
    sources = story.get("sources") or []
    if sources:
        out.append("  Sources:")
        for src in sources:
            name = " · ".join(x for x in (src.get("title"), src.get("publisher"), src.get("date")) if x)
            out.append(f"    - {name}" + (f" <{src['url']}>" if src.get("url") else ""))
    script = (story.get("script") or "").strip()
    if script:
        out.append("  Script" + (f" ({story['scenes']} scenes):" if story.get("scenes") else ":"))
        out += [f"    {line}" for line in script.splitlines() if line.strip()]
    return out


def _caption(it: VideoItem, account: Account | None, s: Settings) -> str:
    return compose_caption(it, account.apply(s) if account else s)


def _lift(store: Store, it: VideoItem, now: float) -> float | None:
    from .results import baseline

    v = store.video_for(it)
    if v is None or not v.views:
        return None
    usual = baseline(store, it.account, now)
    return round(v.views / usual, 2) if usual else None


def _numbers(views, likes, comments, shares) -> str:
    return f"{int(views):,} views · {int(likes):,} likes · {int(comments):,} comments · {int(shares):,} shares"


def _iso(t: float | None) -> str:
    return dt.datetime.fromtimestamp(t).isoformat(timespec="seconds") if t else ""


def _when(t: float | None) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%a %d %b %Y, %H:%M") if t else "not known"


def _length(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _record_path(folder: Path, it: VideoItem, account_label: str) -> Path:
    if it.record:  # rewritten in place
        return Path(it.record)
    when = dt.datetime.fromtimestamp(it.posted_at or it.added_at)
    sub = folder / safe_name(account_label, 40)
    base = f"{when:%Y-%m-%d %H.%M} - {safe_name(it.headline)}"
    path, n = sub / f"{base}.txt", 2
    while path.exists():
        path, n = sub / f"{base} ({n}).txt", n + 1
    return path


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# --- clearing ---------------------------------------------------------------------------------


def _unlink(path: Path) -> None:
    path.unlink()


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _users(items: list[VideoItem]) -> dict[str, list[VideoItem]]:
    """Which entries use each video file (the same clip queued for two accounts is two entries)."""
    out: dict[str, list[VideoItem]] = {}
    for i in items:
        if i.path and not i.cleaned_at:
            out.setdefault(_key(i.path), []).append(i)
    return out


def _files(it: VideoItem, users: dict[str, list[VideoItem]]) -> tuple[list[Path], str]:
    """The files clearing `it` removes, and what else still uses the video ("" if nothing)."""
    files: list[Path] = []
    other = ""
    video = Path(it.path)
    if it.path and video.is_file() and video.suffix.lower() in VIDEO_EXTS:
        others = [o for o in users.get(_key(it.path), []) if o.id != it.id and not o.cleaned_at
                  and not (o.status == "posted" and o.tiktok_id)]
        if others:
            other = "queued for another account" if others[0].status != "new" else "in the posting library"
        else:
            files.append(video)
            files += [c for c in _clip_companions(video) if c.is_file()]
    thumb = Path(it.thumb) if it.thumb else None
    if thumb and thumb.is_file() and _inside(thumb, thumbs_dir()):
        files.append(thumb)
    from .cover import covers_dir

    picture = Path(it.cover) if it.cover else None
    if picture and picture.is_file() and _inside(picture, covers_dir()):
        files.append(picture)
    return files, other


def _inside(path: Path, folder: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(os.path.abspath(folder))
        return True
    except ValueError:
        return False


def _clip_record(video: Path) -> dict | None:
    """The clip's entry in the clips.json beside it."""
    try:
        data = json.loads((video.parent / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    clips = data.get("clips") if isinstance(data, dict) else None
    for rec in clips if isinstance(clips, list) else []:
        if isinstance(rec, dict) and isinstance(rec.get("file"), str) and \
                _key(video.parent / rec["file"]) == _key(video):
            return rec
    return None


def _clip_companions(video: Path) -> list[Path]:
    """The clip's .srt and its thumbnail in the video folder, and the timeline of its own a version made by a
    retouch has (studio/retouch.py: "<name>.timeline.json")."""
    rec = _clip_record(video)
    out = [video.with_suffix(".srt"), video.with_name(video.stem + ".timeline.json")]
    if rec is not None and isinstance(rec.get("rank"), int):
        out.append(video.parent / "thumbnails" / f"{rec['rank']:02d}.jpg")
    return out


SCENE_PICTURES = ("scene_*.png", "scene_*.jpg", "scene_*.jpeg", "scene_*.webp")  # the Creator's (orchestrator)
SCENE_CLIPS = ("scene_*.mp4", "scene_*.mov", "scene_*.webm", "scene_*.m4v")  # the Studio's stock clips


def creation_files(folder: Path, data: dict) -> list[Path]:
    """What the Video Creator (and the Studio) keep beside a video they made, which go with it once nothing
    in the folder is left to post: the storyboard, the scene pictures, and a Studio video's timeline, stock
    clips and assets. [] for a folder of clips cut from someone's video. `data` is its clips.json."""
    made = data.get("creator") if isinstance(data, dict) else None
    if not made:
        return []
    scenes = folder / "scenes"
    out = [folder / "storyboard.json"]
    if scenes.is_dir():
        out += sorted({p for pattern in SCENE_PICTURES for p in scenes.glob(pattern)})
    if isinstance(made, dict) and made.get("studio"):
        out += _studio_files(folder)
    return out


RENDER_LEFTOVERS = ".render-*"  # a work folder an interrupted re-render left behind (studio/orchestrator.py)


def _render_leftovers(folder: Path) -> list[Path]:
    try:
        return sorted(d for d in folder.glob(RENDER_LEFTOVERS) if d.is_dir())
    except OSError:
        return []


def creation_dirs(folder: Path) -> list[Path]:
    """The folders creation_files() may leave empty, innermost first."""
    left = []
    for d in _render_leftovers(folder):
        left += sorted((s for s in d.rglob("*") if s.is_dir()), key=lambda s: len(s.parts), reverse=True) + [d]
    return [folder / "scenes", folder / "assets" / "sfx", folder / "assets", *left]


def _studio_files(folder: Path) -> list[Path]:
    """What the Studio keeps beside a video to render it again (studio/orchestrator.py): its timeline, the
    scenes' stock clips, and everything in assets/ (voice, music, effects)."""
    scenes, assets = folder / "scenes", folder / "assets"
    out = [folder / "timeline.json", *sorted(folder.glob("*.timeline.json"))]  # (and its versions' own)
    if scenes.is_dir():
        out += sorted({p for pattern in SCENE_CLIPS for p in scenes.glob(pattern)})
    if assets.is_dir():
        out += sorted(p for p in assets.rglob("*") if p.is_file())
    for d in _render_leftovers(folder):  # what a render that was cut off left: nothing else reclaims it
        out += sorted(p for p in d.rglob("*") if p.is_file())
    return out


def _tidy(folder: Path, removed: set[str], roots: list[Path], remove: Callable[[Path], None]) -> int:
    """A video folder after clips left it: their rows go from its clips.json and README.md, or, with no
    clips left, its own files go (a Video Creator video's storyboard and scene pictures too) and so do the
    folders that leaves empty (not a root, nor a folder right inside one, like a category's folder).
    Returns the bytes it freed."""
    index = folder / "clips.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        return 0
    gone = {_key(folder / n) for n in removed}
    keep = [rec for rec in data["clips"] if not (isinstance(rec, dict) and isinstance(rec.get("file"), str)
                                                 and _key(folder / rec["file"]) in gone)]
    left = [rec for rec in keep if isinstance(rec, dict) and isinstance(rec.get("file"), str)
            and (folder / rec["file"]).is_file() and not rec["file"].replace("\\", "/").startswith(f"{REJECTED_DIR}/")]
    if left:
        if len(keep) != len(data["clips"]):
            files = {rec["file"] for rec in data["clips"] if rec not in keep}
            data["clips"] = keep
            data["cleaned_up"] = time.time()
            if isinstance(data.get("qc"), dict):  # the counts the Library shows follow
                checks = [rec.get("qc") if isinstance(rec.get("qc"), dict) else {} for rec in keep
                          if isinstance(rec, dict)]
                data["qc"] = {"passed": sum(c.get("ok") is not False for c in checks),
                              "rejected": sum(c.get("ok") is False for c in checks),
                              "warned": sum(c.get("ok") is not False and bool(c.get("warnings")) for c in checks)}
            _rewrite(index, json.dumps(data, indent=2, ensure_ascii=False))
            readme = folder / "README.md"
            try:
                _rewrite(readme, _readme_without(readme.read_text(encoding="utf-8"), files))
            except OSError:
                pass
        return 0
    # Nothing left to post from this video: its folder goes (rejected clips in it too, and a Creator video's
    # storyboard and scene pictures: its record has the script).
    rejected = folder / REJECTED_DIR
    extra = sorted(rejected.iterdir()) if rejected.is_dir() else []
    scenes = folder / "scenes"
    pictures = sorted({p for pattern in SCENE_PICTURES for p in scenes.glob(pattern)}) if scenes.is_dir() else []
    freed = 0
    for f in [index, folder / "README.md", *sorted((folder / "thumbnails").glob("[0-9][0-9].jpg")),
              *[e for e in extra if e.is_file()], *sorted(folder.glob("*.srt")),
              *dict.fromkeys([*pictures, *creation_files(folder, data)])]:
        if f.is_file():
            size = _size(f)
            remove(f)
            freed += size
    for d in (folder / "thumbnails", rejected, *creation_dirs(folder)):
        _rmdir(d)
    keep_dirs = {_key(r) for r in roots} | {_key(c) for r in roots if r.is_dir() for c in _subdirs(r)}
    d = folder
    while _key(d) not in keep_dirs and any(_inside(d, r) for r in roots) and _rmdir(d):
        d = d.parent
    return freed


def _subdirs(root: Path) -> list[Path]:
    try:
        return [c for c in root.iterdir() if c.is_dir()]
    except OSError:
        return []


def _rewrite(path: Path, text: str) -> None:
    """Replace a file's text, keeping its date (the Library lists videos by when they were made)."""
    st = path.stat()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    os.utime(path, (st.st_atime, st.st_mtime))


def _readme_without(text: str, files: set[str]) -> str:
    """A README.md without the table rows of these clip files."""
    out, dropping = [], False
    for line in text.splitlines():
        if re.match(r"\| \d+ \|", line):  # a clip's first row; the rows under it start with "| |"
            dropping = any(f"](<{f}>)" in line for f in files)
        elif not line.startswith("| |"):
            dropping = False
        if not dropping:
            out.append(line)
    return "\n".join(out) + "\n"


def _rmdir(d: Path) -> bool:
    try:
        d.rmdir()
        return True
    except OSError:
        return False
