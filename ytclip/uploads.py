"""Upload finished clips to a storage server (Settings → Upload).

Every clip that's exported and passed the quality check is uploaded once, in the same folders it has
on this PC (Automation/<Niche>/<channel>/<video>/...), under one folder on the server. Uploading
runs on a thread of its own, in automation and in the open window, so it never holds up clipping;
the queue is the `uploads` table in jobs.db, so the two never upload the same clip twice and an
upload cut off by closing ClipMonster carries on next time. A clip that's exported again replaces
its upload instead of adding a copy. Clean up's "uploaded" kind can then clear clips that are
safely on the server and that TikTok Manager doesn't still need (cleanup.py).

Servers are pluggable: each is a Target in TARGETS. Google Drive (gdrive.py) is the first; "folder"
copies to any folder, such as a NAS share or a synced folder.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import gdrive
from .procs import process_alive
from .settings import Settings

FRESH = 60.0  # a clip written this recently may still be being exported
SCAN_EVERY = 120.0  # how often the output folders are looked through for new clips
BACKOFF = (60, 5 * 60, 30 * 60, 2 * 3600, 6 * 3600)  # waits after each failed try; then every 6 h
GIVE_UP = 8  # tries before a clip waits for Retry
HOLD = 15 * 60  # after a problem no clip can upload past, uploads wait this long (or until the settings change)
REJECTED_DIR = "_rejected"
MIME = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".srt": "application/x-subrip", ".jpg": "image/jpeg", ".json": "application/json"}

Log = Callable[[str], None]


class UploadError(RuntimeError):
    """Why an upload failed. kind: "" (try again later) | "stop" (nothing can upload until you fix it:
    a sign-in, a full Drive, a missing folder) | "cancelled"."""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


@dataclass
class Item:
    path: Path  # the clip on this PC
    remote_dir: list[str]  # folders under the server's ClipMonster folder
    name: str
    description: str = ""
    remote_id: str = ""  # its earlier upload, which a new export replaces
    session: str = ""  # an upload started earlier, to carry on with
    size: int = 0


@dataclass
class Done:
    remote_id: str
    link: str = ""


# ---------------------------------------------------------------- servers

class Target(Protocol):
    key: str
    name: str

    def problem(self, s: Settings) -> str:
        """'' when it's set up to upload, else what's missing."""

    def upload(self, s: Settings, item: Item, save_session: Callable[[str], None],
               progress: Callable[[float], None], cancelled: Callable[[], bool], db=None) -> Done:
        """Upload one clip (UploadError on failure). save_session keeps a resumable upload's address,
        so an upload cut off carries on next time; `db` (jobs.db) may remember anything else."""


class GoogleDrive:
    key = "gdrive"
    name = "Google Drive"

    def __init__(self):
        self._folders: dict[tuple[str, ...], str] = {}  # remote folder path -> id, checked this run
        self._lock = threading.Lock()

    def problem(self, s: Settings) -> str:
        if not s.gdrive_client_id.strip() or not s.gdrive_client_secret.strip():
            return ("Google Drive needs your OAuth client ID and secret (Settings → Upload says how to get them, "
                    "free, in about 5 minutes).")
        if gdrive.load_account(s.gdrive_client_id) is None:
            return "Sign in to Google Drive in Settings → Upload."
        return ""

    def drive(self, s: Settings) -> gdrive.Drive:
        return gdrive.Drive(s.gdrive_client_id, s.gdrive_client_secret)

    def folder(self, drive: gdrive.Drive, s: Settings, parts: list[str], db=None) -> str:
        """The id of ClipMonster's folder at `parts` (made when missing), remembered in jobs.db."""
        top = (s.gdrive_folder.strip() or "ClipMonster").strip("/\\")
        path = tuple([top, *parts])
        with self._lock:
            parent = "root"
            for depth in range(1, len(path) + 1):
                key = path[:depth]
                known = self._folders.get(key)
                if known is None:
                    saved = _saved_folder(db, key) if db is not None else ""
                    if saved and drive.exists(saved):
                        known = saved
                    else:
                        known = drive.find_folder(key[-1], parent) or drive.make_folder(key[-1], parent)
                        if db is not None:
                            _save_folder(db, key, known)
                    self._folders[key] = known
                parent = known
            return parent

    def forget_folders(self, db=None) -> None:
        with self._lock:
            self._folders.clear()
        if db is not None:
            with db._lock:
                db.con.execute("DELETE FROM upload_folders")

    def upload(self, s, item, save_session, progress, cancelled, db=None) -> Done:
        try:
            drive = self.drive(s)
            if item.remote_id and not item.session and not drive.exists(item.remote_id):
                item.remote_id = ""  # deleted in Drive: upload it as a new file
            for attempt in range(2):
                session = item.session
                offset: int | dict = 0
                if session:
                    try:
                        offset = drive.uploaded_so_far(session, item.size)
                    except gdrive.DriveError as exc:
                        if exc.kind != "gone":
                            raise
                        session = ""
                if not session:
                    try:
                        parent = "" if item.remote_id else self.folder(drive, s, item.remote_dir, db)
                        session = drive.start_upload(item.name, parent, item.size, _mime(item.path),
                                                     item.description, replace=item.remote_id)
                    except gdrive.DriveError as exc:
                        if exc.kind != "gone" or attempt:
                            raise
                        # a folder (or the earlier upload) was deleted in Drive since: find or make it again
                        item.remote_id = ""
                        self.forget_folders(db)
                        continue
                    save_session(session)
                    offset = 0
                try:
                    data = offset if isinstance(offset, dict) else drive.send(session, item.path, item.size, offset,
                                                                              progress, cancelled)
                except gdrive.DriveError as exc:
                    if exc.kind != "gone" or attempt:
                        raise
                    item.session = ""
                    save_session("")
                    continue
                file_id = str(data.get("id") or item.remote_id)
                if not file_id:
                    raise UploadError("Google Drive didn't say where it put the clip.")
                return Done(file_id, str(data.get("webViewLink") or gdrive.link(file_id)))
            raise UploadError("The upload kept being cut off. It's tried again later.")
        except gdrive.DriveError as exc:
            kind = {"sign_in": "stop", "full": "stop", "setup": "stop", "cancelled": "cancelled"}.get(exc.kind, "")
            raise UploadError(str(exc), kind) from exc


class FolderTarget:
    """Any folder Windows can write to: a NAS or network share (\\\\NAS\\clips), a mapped drive, or a folder
    Google Drive, Dropbox or OneDrive syncs."""

    key = "folder"
    name = "a folder"

    def problem(self, s: Settings) -> str:
        if not s.upload_folder.strip():
            return "Pick the folder to upload to in Settings → Upload."
        root = Path(s.upload_folder).expanduser()
        if not root.is_dir():
            return f"The upload folder {root} isn't there (is the NAS or drive connected?)."
        return ""

    def upload(self, s, item, save_session, progress, cancelled, db=None) -> Done:
        root = Path(s.upload_folder).expanduser()
        if not root.is_dir():
            raise UploadError(f"The upload folder {root} isn't there (is the NAS or drive connected?). It's "
                              "tried again later.")
        dest = root.joinpath(*item.remote_dir) / item.name
        part = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            done = 0
            with open(item.path, "rb") as src, open(part, "wb") as out:
                while chunk := src.read(gdrive.CHUNK):
                    if cancelled():
                        raise UploadError("The upload was stopped; it carries on later.", "cancelled")
                    out.write(chunk)
                    done += len(chunk)
                    progress(done / item.size if item.size else 1.0)
            shutil.copystat(item.path, part)
            os.replace(part, dest)
        except UploadError:
            part.unlink(missing_ok=True)
            raise
        except OSError as exc:
            part.unlink(missing_ok=True)
            if getattr(exc, "winerror", None) == 112 or exc.errno == 28:  # the disk is full
                raise UploadError(f"There's no space left in {root}. Uploading carries on once there is.",
                                  "stop") from exc
            raise UploadError(f"Couldn't copy to {dest.parent} ({exc.strerror or exc}).") from exc
        return Done(str(dest), dest.as_uri())


TARGETS: dict[str, Target] = {"gdrive": GoogleDrive(), "folder": FolderTarget()}  # settings.UPLOAD_TARGETS


def target_for(s: Settings) -> Target | None:
    return TARGETS.get(s.upload_target)


def _mime(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------- what's finished

def roots(s: Settings) -> list[tuple[Path, list[str]]]:
    """(folder, remote folders it maps to) for each output folder, the Automation folder first."""
    from .cleanup import _inside

    auto = s.auto_output_path
    out = [(auto, ["Automation"])]
    if not _inside(s.output_path, auto):
        out.append((s.output_path, []))
    return out if s.upload_what != "automation" else out[:1]


def finished(s: Settings, now: float | None = None) -> list[Item]:
    """Every clip that's exported, passed the quality check and is listed in its clips.json."""
    from .cleanup import _inside, _key, _video_folders

    now = time.time() if now is None else now
    found: list[Item] = []
    seen: set[str] = set()
    for root, prefix in roots(s):
        for folder, data in _video_folders(root, s):
            if _key(folder) in seen:
                continue
            seen.add(_key(folder))
            try:
                rel = list(folder.relative_to(root).parts)
            except ValueError:
                continue
            video = data.get("video") if isinstance(data.get("video"), dict) else {}
            for rec in data["clips"]:
                file = rec.get("file") if isinstance(rec, dict) else None
                if not isinstance(file, str) or not file or file.replace("\\", "/").startswith(f"{REJECTED_DIR}/"):
                    continue
                if isinstance(rec.get("qc"), dict) and rec["qc"].get("ok") is False:
                    continue
                path = folder / file
                try:
                    st = path.stat()
                except OSError:
                    continue
                if not path.is_file() or now - st.st_mtime < FRESH or not _inside(path, folder):
                    continue
                found.append(Item(path, prefix + rel + list(Path(file).parent.parts), path.name,
                                  _describe(rec, video, data), size=st.st_size))
    return found


def _describe(rec: dict, video: dict, data: dict) -> str:
    bits = [str(rec.get("title") or "").strip()]
    if rec.get("score") is not None:
        bits.append(f"Score {rec.get('score')}/100. {str(rec.get('reason') or '').strip()}".strip())
    source = data.get("source") or video.get("url") or ""
    if video.get("title") or source:
        bits.append(f"From: {video.get('title') or ''} {source}".strip())
    if rec.get("watch"):
        bits.append(f"At: {rec['watch']}")
    return "\n".join(b for b in bits if b)


# ---------------------------------------------------------------- the queue (jobs.db)

COLUMNS = ("path TEXT PRIMARY KEY, target TEXT, size INTEGER, mtime REAL, state TEXT, tries INTEGER, "
           "next_try REAL, error TEXT, remote_id TEXT, link TEXT, session TEXT, owner TEXT, updated REAL, "
           "remote_dir TEXT, name TEXT, description TEXT")
STATES = ("waiting", "uploading", "done", "failed")


def _table(db) -> None:
    with db._lock:
        db.con.execute(f"CREATE TABLE IF NOT EXISTS uploads ({COLUMNS})")
        db.con.execute("CREATE TABLE IF NOT EXISTS upload_folders (path TEXT PRIMARY KEY, id TEXT)")


def _saved_folder(db, key: tuple[str, ...]) -> str:
    _table(db)
    with db._lock:
        row = db.con.execute("SELECT id FROM upload_folders WHERE path = ?", ("/".join(key),)).fetchone()
    return row["id"] if row else ""


def _save_folder(db, key: tuple[str, ...], folder_id: str) -> None:
    with db._lock:
        db.con.execute("INSERT OR REPLACE INTO upload_folders (path, id) VALUES (?, ?)", ("/".join(key), folder_id))


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def sync(db, s: Settings, now: float | None = None) -> int:
    """Queue clips that are new or exported again since their upload. Returns how many were queued."""
    _table(db)
    now = time.time() if now is None else now
    target = s.upload_target
    added = 0
    with db._lock:
        rows = {r["path"]: r for r in db.con.execute("SELECT path, target, size, mtime, state, remote_id FROM uploads")}
        for item in finished(s, now):
            key = _norm(item.path)
            mtime = item.path.stat().st_mtime
            row = rows.get(key)
            same = row is not None and row["size"] == item.size and abs((row["mtime"] or 0) - mtime) < 1
            if row is not None and row["target"] == target and (same or row["state"] in ("waiting", "uploading")):
                if not same and row["state"] == "waiting":  # changed before it went up: send the new one
                    db.con.execute("UPDATE uploads SET size = ?, mtime = ?, session = '' WHERE path = ?",
                                   (item.size, mtime, key))
                continue
            remote = row["remote_id"] if row is not None and row["target"] == target else ""
            db.con.execute(
                "INSERT OR REPLACE INTO uploads (path, target, size, mtime, state, tries, next_try, error, remote_id, "
                "link, session, owner, updated, remote_dir, name, description) "
                "VALUES (?, ?, ?, ?, 'waiting', 0, 0, '', ?, '', '', '', ?, ?, ?, ?)",
                (key, target, item.size, mtime, remote, now, json.dumps(item.remote_dir), item.name,
                 item.description))
            added += 1
    return added


def claim(db, owner: str, target: str, now: float | None = None) -> dict | None:
    """The next clip to upload to `target`, marked as this uploader's (two uploaders, in automation and
    the window, never take the same one). `owner` starts with the uploader's process id."""
    _table(db)
    now = time.time() if now is None else now
    with db._lock:
        for row in db.con.execute("SELECT path, owner FROM uploads WHERE state = 'uploading'").fetchall():
            pid = (row["owner"] or "").split(":", 1)[0]
            if not pid or not process_alive(pid):  # its uploader ended mid-upload: it carries on from here
                db.con.execute("UPDATE uploads SET state = 'waiting', owner = '' WHERE path = ?", (row["path"],))
        row = db.con.execute("SELECT * FROM uploads WHERE state = 'waiting' AND next_try <= ? AND target = ? "
                             "ORDER BY updated LIMIT 1", (now, target)).fetchone()
        if row is None:
            return None
        cur = db.con.execute("UPDATE uploads SET state = 'uploading', owner = ? WHERE path = ? AND state = 'waiting'",
                             (owner, row["path"]))
        return dict(row) if cur.rowcount == 1 else None


def _set(db, path: str, **values) -> None:
    cols = ", ".join(f"{k} = ?" for k in values)
    with db._lock:
        db.con.execute(f"UPDATE uploads SET {cols} WHERE path = ?", (*values.values(), path))


def retry_failed(db) -> int:
    _table(db)
    with db._lock:
        return db.con.execute("UPDATE uploads SET state = 'waiting', tries = 0, next_try = 0, error = '' "
                              "WHERE state = 'failed'").rowcount


def uploaded(db, target: str = "") -> dict[str, tuple[int, float, float]]:
    """Clips that are safely uploaded: normalised path -> (size, mtime) of the file that went up, and when."""
    _table(db)
    with db._lock:
        rows = db.con.execute("SELECT path, size, mtime, updated, target FROM uploads WHERE state = 'done'"
                              ).fetchall()
    return {r["path"]: (r["size"], r["mtime"], r["updated"] or 0.0) for r in rows if not target or r["target"] == target}


def forget(db, paths) -> None:
    """Clean up removed these clips: their uploads stay on the server, the queue forgets them."""
    _table(db)
    with db._lock:
        db.con.executemany("DELETE FROM uploads WHERE path = ?", [(_norm(Path(p)),) for p in paths])


@dataclass
class Status:
    waiting: int = 0
    uploading: int = 0
    done_today: int = 0
    done: int = 0
    failed: int = 0
    problem: str = ""  # what stops every upload (a sign-in, a full Drive), from the last try
    last_error: str = ""
    current: str = ""
    progress: float = 0.0
    counts: dict = field(default_factory=dict)

    def text(self) -> str:
        if self.problem:
            return f"Uploads paused: {self.problem}"
        bits = []
        if self.uploading:
            bits.append(f"uploading {self.current}" + (f" ({self.progress:.0%})" if self.progress else ""))
        if self.waiting:
            bits.append(f"{self.waiting} waiting")
        if self.failed:
            bits.append(f"{self.failed} failed (last: {self.last_error})")
        bits.append(f"{self.done_today} uploaded today")
        return "Uploads: " + ", ".join(bits)


def status(db, now: float | None = None) -> Status:
    _table(db)
    now = time.time() if now is None else now
    st = Status()
    with db._lock:
        for row in db.con.execute("SELECT state, COUNT(*) AS n FROM uploads GROUP BY state"):
            st.counts[row["state"]] = row["n"]
        st.done_today = db.con.execute("SELECT COUNT(*) FROM uploads WHERE state = 'done' AND updated >= ?",
                                       (now - 86400,)).fetchone()[0]
        last = db.con.execute("SELECT error FROM uploads WHERE state = 'failed' ORDER BY updated DESC LIMIT 1"
                              ).fetchone()
        cur = db.con.execute("SELECT name FROM uploads WHERE state = 'uploading' LIMIT 1").fetchone()
    st.waiting, st.uploading = st.counts.get("waiting", 0), st.counts.get("uploading", 0)
    st.done, st.failed = st.counts.get("done", 0), st.counts.get("failed", 0)
    st.last_error = last["error"] if last else ""
    st.current = cur["name"] if cur else ""
    st.problem = db.meta("upload_problem")
    try:
        st.progress = float(db.meta("upload_progress") or 0)
    except ValueError:
        st.progress = 0.0
    return st


# ---------------------------------------------------------------- uploading

def upload_next(db, s: Settings, owner: str, log: Log, cancelled: Callable[[], bool] = lambda: False,
                now: Callable[[], float] = time.time) -> bool:
    """Upload one waiting clip. False when there's nothing to do (or nothing can be done now)."""
    target = target_for(s)
    if target is None:
        return False
    problem = target.problem(s)
    if problem:
        db.set_meta("upload_problem", problem)
        return False
    if db.meta_float("upload_hold") > now():  # held after a problem nothing can upload past (see below)
        return False
    row = claim(db, owner, target.key, now())
    if row is None:
        return False
    path = Path(row["path"])
    try:
        size, mtime = path.stat().st_size, path.stat().st_mtime
    except OSError:  # cleaned up or moved before it went up
        with db._lock:
            db.con.execute("DELETE FROM uploads WHERE path = ?", (row["path"],))
        return True
    if size != row["size"] or abs(mtime - (row["mtime"] or 0)) >= 1:  # exported again meanwhile
        _set(db, row["path"], size=size, mtime=mtime, session="")
        row["session"] = ""
    item = Item(path, json.loads(row["remote_dir"] or "[]"), row["name"] or path.name, row["description"] or "",
                row["remote_id"] or "", row["session"] or "", size)
    last = [0.0]

    def progress(f: float) -> None:
        if now() - last[0] >= 1 or f >= 1:
            last[0] = now()
            db.set_meta("upload_progress", round(f, 3))

    db.set_meta("upload_progress", 0)
    try:
        done = target.upload(s, item, lambda session: _set(db, row["path"], session=session), progress, cancelled,
                             db=db)
    except UploadError as exc:
        if exc.kind == "cancelled":
            _set(db, row["path"], state="waiting", owner="")
            return False
        tries = (row["tries"] or 0) + 1
        state = "failed" if tries >= GIVE_UP and exc.kind != "stop" else "waiting"
        wait = 0 if exc.kind == "stop" else BACKOFF[min(tries, len(BACKOFF)) - 1]
        _set(db, row["path"], state=state, owner="", tries=0 if exc.kind == "stop" else tries, error=str(exc),
             next_try=now() + wait, updated=now())
        if exc.kind == "stop":  # (a full Drive, a sign-in that stopped working): wait, rather than try every clip
            db.set_meta("upload_problem", str(exc))
            db.set_meta("upload_hold", now() + HOLD)
        log(f"Upload of {item.name} failed: {exc}" + ("" if exc.kind == "stop" else
                                                       f" Trying again in {max(1, wait // 60)} min."))
        return False
    except Exception as exc:  # never let one clip stop the uploader
        _set(db, row["path"], state="waiting", owner="", error=str(exc) or exc.__class__.__name__,
             next_try=now() + BACKOFF[0], updated=now())
        log(f"Upload of {item.name} failed: {exc}")
        return False
    _set(db, row["path"], state="done", owner="", tries=0, error="", remote_id=done.remote_id, link=done.link,
         session="", size=size, mtime=mtime, updated=now())
    db.set_meta("upload_problem", "")
    log(f"Uploaded {item.name} to {target.name}: {'/'.join(item.remote_dir)}")
    return True


class Uploader:
    """The upload queue, worked through on a thread of its own: in automation, and in the window."""

    def __init__(self, load: Callable[[], Settings], log: Log, db_factory: Callable | None = None,
                 owner: str = "", idle_when: Callable[[object], bool] | None = None):
        """idle_when(db): True while it should leave uploading to someone else (the window's uploader
        leaves it to automation while automation runs)."""
        from .jobs import JobDB

        self.load, self.log = load, log
        self.idle_when = idle_when or (lambda db: False)
        self.db_factory = db_factory or JobDB
        self.owner = owner or f"{os.getpid()}:{threading.get_ident()}"
        self._stop = threading.Event()
        self._nudge = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uploads", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait: float = 10.0) -> None:
        self._stop.set()
        self._nudge.set()
        if self._thread.is_alive():
            self._thread.join(wait)

    def nudge(self) -> None:
        """Look for new clips now (an export just finished, or the settings changed)."""
        self._nudge.set()

    @property
    def busy(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        db = self.db_factory()
        last_scan = 0.0
        try:
            while not self._stop.is_set():
                try:
                    s = self.load()
                except Exception:
                    s = None
                if s is None or not s.upload_target or self.idle_when(db):
                    self._nudge.wait(SCAN_EVERY)
                    self._nudge.clear()
                    continue
                now = time.time()
                if db.meta("upload_nudge") == "1":  # asked from the other process (pipeline.request_uploads)
                    db.set_meta("upload_nudge", "0")
                    self._nudge.set()
                if now - last_scan >= SCAN_EVERY or self._nudge.is_set():
                    if self._nudge.is_set():  # the settings changed, or you asked: try again now
                        db.set_meta("upload_hold", 0)
                    self._nudge.clear()
                    last_scan = now
                    if db.meta_float("upload_hold") <= now:
                        db.set_meta("upload_problem", "")
                    try:
                        sync(db, s, now)
                    except Exception as exc:
                        self.log(f"Couldn't look for clips to upload: {exc}")
                worked = False
                try:
                    worked = upload_next(db, s, self.owner, self.log, self._stop.is_set)
                except Exception as exc:
                    self.log(f"Uploading stopped for a moment: {exc}")
                if not worked:
                    self._nudge.wait(min(SCAN_EVERY, 30.0))
        finally:
            try:
                db.close()
            except Exception:
                pass
