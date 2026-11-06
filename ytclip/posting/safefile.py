"""Saving and loading posting's JSON files so that one bad write can never wipe them, and so that
the window and the background worker can both change them (file_lock, signature, merge).

Every save keeps the previous good file as `<name>.bak` and, once a day, a dated copy in
`backups/` (the last BACKUP_DAYS days are kept). If a file won't load, it is moved aside as
`<name>.damaged-<time>` (so the next save can't overwrite it) and the newest good copy is used.
What happened is recorded in `notices` for the window to show at start-up.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

BACKUP_DAYS = 7
notices: list[str] = []  # plain-English lines about recovered files, read once by the main window
_lock = threading.Lock()
_good: set[str] = set()  # files this process loaded or wrote whole, so saving needn't re-read them


def _parse(path: Path):
    """The file's JSON, None when it doesn't exist; raises ValueError when it's damaged."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(str(exc)) from exc
    except UnicodeDecodeError as exc:
        raise ValueError("not text") from exc
    if not text.strip():
        raise ValueError("empty")
    return json.loads(text)


def backups_dir(path: Path) -> Path:
    return path.parent / "backups"


def _dated(path: Path) -> list[Path]:
    """This file's daily backups, newest first."""
    folder = backups_dir(path)
    if not folder.is_dir():
        return []
    return sorted(folder.glob(f"{path.stem}-????-??-??{path.suffix}"), reverse=True)


def load(path: Path, what: str):
    """Load a JSON file, falling back to its backups. None when there is no file (a first start).

    `what` names the file for the user, e.g. "your video library".
    """
    try:
        data = _parse(path)
    except ValueError:
        pass
    else:
        if data is not None:
            _good.add(str(path))
        return data
    stamp = time.strftime("%Y%m%d-%H%M%S")
    aside = path.with_name(f"{path.name}.damaged-{stamp}")
    try:
        os.replace(path, aside)
    except OSError:
        aside = path
    for candidate in [path.with_name(path.name + ".bak"), *_dated(path)]:
        try:
            data = _parse(candidate)
        except ValueError:
            continue
        if data is None:
            continue
        when = time.strftime("%d %b %H:%M", time.localtime(candidate.stat().st_mtime))
        notices.append(f"{what[:1].upper()}{what[1:]} couldn't be read, so the copy saved {when} was loaded. "
                       f"The damaged file was kept as {aside.name}.")
        return data
    notices.append(f"{what[:1].upper()}{what[1:]} couldn't be read and there was no backup, so it starts empty. "
                   f"The damaged file was kept as {aside.name}.")
    return None


def save(path: Path, data, indent: int | None = 2) -> None:
    """Write JSON atomically, keeping the file it replaces as .bak and a daily copy."""
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and _is_good(path):  # only a good file becomes the backup
            try:
                shutil.copy2(path, path.with_name(path.name + ".bak"))
            except OSError:
                pass
            _daily(path)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:  # Windows: another process is reading it this instant
                if attempt == 19:
                    raise
                time.sleep(0.05)
        _good.add(str(path))


def _is_good(path: Path) -> bool:
    if str(path) in _good:
        return True
    try:
        return _parse(path) is not None
    except ValueError:
        return False


def _daily(path: Path) -> None:
    folder = backups_dir(path)
    target = folder / f"{path.stem}-{time.strftime('%Y-%m-%d')}{path.suffix}"
    if target.exists():
        return
    try:
        folder.mkdir(exist_ok=True)
        shutil.copy2(path, target)
        for old in _dated(path)[BACKUP_DAYS:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # a missing daily copy is no reason to fail the save


# --- two processes, one file --------------------------------------------------------------
#
# The window and the background worker both keep the posting library and the account list in memory.
# Each change is saved under file_lock(), after merging in what the other process saved meanwhile:
# for each entry, a field this process changed since it last read the file wins, and every other field
# takes the file's value (merge). So a caption typed in the window and a post the worker just made, on
# the same entry or different ones, both survive.


def signature(path: Path) -> tuple[int, int]:
    """Changes whenever the file is replaced (os.replace gives it a new mtime and usually a new size)."""
    try:
        st = path.stat()
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


class file_lock:
    """Only one process at a time reads-merges-writes `path` (a `<name>.lock` file beside it).
    Reentrant within a thread's `with` blocks is not needed: callers hold it briefly."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.lock_path = path.with_name(path.name + ".lock")
        self.timeout = timeout
        self._f = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.lock_path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                _lock_file(f)
                break
            except OSError:
                if time.monotonic() > deadline:
                    f.close()
                    raise TimeoutError(f"{self.lock_path.name} stayed locked by another process")
                time.sleep(0.02)
        self._f = f
        return self

    def __exit__(self, *exc):
        f, self._f = self._f, None
        if f is not None:
            try:
                _unlock_file(f)
            finally:
                f.close()
        return False


if os.name == "nt":
    import msvcrt

    def _lock_file(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(f) -> None:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_raw(path: Path):
    """The file's JSON without the fallbacks load() uses (None if missing or unreadable): for merging
    in another process's save, where a half-written file must not count as the truth."""
    try:
        return _parse(path)
    except ValueError:
        return None


def merge_entry(base: dict | None, local: dict | None, disk: dict | None) -> dict | None:
    """Three-way merge of one entry (a dict of fields). None means the entry isn't there."""
    if local is None:
        if base is None:
            return disk  # new in the file
        return None  # removed here (whatever the file did to it)
    if base is None or disk is None:
        if base is not None and disk is None and local == base:
            return None  # removed in the file, untouched here
        return local  # new here, or removed in the file after we changed it
    out = dict(disk)
    for key, value in local.items():
        if base.get(key, _MISSING) != value:
            out[key] = value  # changed here
    for key in base:
        if key not in local:
            out.pop(key, None)
    return out


def merge_entries(base: dict[str, dict], local: dict[str, dict], disk: dict[str, dict]) -> dict[str, dict]:
    """merge_entry for every id in any of the three, keeping the local order then the file's new ones."""
    out: dict[str, dict] = {}
    for key in list(local) + [k for k in disk if k not in local] + [k for k in base if k not in local and k not in disk]:
        if key in out:
            continue
        merged = merge_entry(base.get(key), local.get(key), disk.get(key))
        if merged is not None:
            out[key] = merged
    return out


_MISSING = object()
