"""Clean up: clear out what ClipMonster leaves behind that nothing needs any more.

plan() only looks; apply() removes what a plan lists, for the kinds you pick (CLEANUP_KINDS):

* partial: unfinished downloads in the download cache that nothing has written to for a while
* previews: what "Render preview" and the Layout tab's preview leave in the work folder
* downloads: full videos in the download cache
* analysis: the transcripts, captions and measurements saved beside them (a video opens again
  instantly with them; without them it is measured again)
* rejected: clips the quality check rejected (they're never posted)
* posted: posted clips whose files were kept (deleting after posting is off, say), once they're on TikTok
* removed: clips you took out of the posting queue ("Don't post"; they're never brought back)

Posting clears a clip's files itself once the post is up (posting/records.py); this is for the rest.
It never touches a video that's queued, being clipped or open in the Studio, a clip waiting to post
(or posting, failed, or posted but not seen on TikTok yet), anything changed in the last few minutes,
or anything that isn't ClipMonster's own: a clip must be listed in a clips.json, a cache file must
be named after a video (in a download cache folder you picked yourself, one ClipMonster saved the
details of, or named as only ClipMonster or yt-dlp would). Unfinished downloads keep to the age
picked too. A download goes with its details (.video.json); a clip goes with its .srt and
thumbnail, and leaves its clips.json (its counts and README follow); a video
folder with no clips left goes altogether, and so do folders it leaves empty (not the Automation
folder or a category's folder).

The posting library (posting/library.json) is only ever read here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from glob import escape as glob_escape
from pathlib import Path
from typing import Callable

from . import categories
from .download import VIDEO_EXTS, is_url, local_id, video_id
from .settings import APP_NAME, CLEANUP_KINDS, Settings, app_dir, work_dir

DAY = 86400.0
KINDS = tuple(k for k, _ in CLEANUP_KINDS)
LABELS = dict(CLEANUP_KINDS)
NOUNS = {"partial": "unfinished download", "previews": "preview render", "downloads": "downloaded video",
         "analysis": "saved analysis file", "rejected": "rejected clip", "posted": "posted clip",
         "removed": "skipped clip"}
CLIP_KINDS = ("rejected", "posted", "removed")
CACHE_KINDS = ("partial", "downloads", "analysis")
REJECTED_DIR = "_rejected"
FRESH = 10 * 60  # anything changed this recently may still be in use
PARTIAL_IDLE = 30 * 60  # an unfinished download nothing has written to for this long is abandoned

_YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_PARTIAL = re.compile(r"\.(part|ytdl)$|\.part-Frag\d+|\.f\d+(?:-\w+)*\.\w+$|\.temp\.\w+$", re.I)
_ANALYSIS = re.compile(r"\.(analysis\.npz|video\.json|whisper-[\w.-]+\.json|[\w-]+\.vtt)$", re.I)
_OWN = re.compile(r"\.(analysis\.npz|whisper-[\w.-]+\.json)$", re.I)  # names only ClipMonster gives files
_DOWNLOAD_BITS = re.compile(r"f\d+(?:-\w+)*|part|part-frag\d+|ytdl|temp", re.I)
_YTDLP_ONLY = re.compile(r"\.f\d+(?:-\w+)*\.|\.temp\.|\.ytdl$|\.part-Frag\d+", re.I)  # not a browser's .part
MEDIA_EXTS = VIDEO_EXTS | {".m4a", ".mp3", ".opus", ".ogg", ".aac", ".wav"}


def parse_kinds(text: str) -> list[str]:
    """"rejected, posted" -> the kinds, in CLEANUP_KINDS order; unknown words are ignored."""
    chosen = {w.strip().lower() for w in re.split(r"[\s,;]+", text or "") if w.strip()}
    return [k for k in KINDS if k in chosen]


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("bytes", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _key(path) -> str:
    """How the posting library names a file (posting.store._key) for a path on this PC."""
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError):
        pass
    return os.path.normcase(os.path.abspath(str(path)))


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------- the posting library


@dataclass
class PostingLibrary:
    """What the posting library says about clip files, by their _key()."""

    found: bool = False  # it was read
    unreadable: bool = False  # it exists but couldn't be read, so nothing it might hold is cleared
    keep: set[str] = field(default_factory=set)  # waiting to post, posting, failed, or not seen on TikTok yet
    posted: dict[str, float] = field(default_factory=dict)  # posted and seen on TikTok -> when
    removed: set[str] = field(default_factory=set)  # taken out of the queue by you


def posting_library_path() -> Path:
    return app_dir() / "posting" / "library.json"


def read_posting(path: Path | None = None) -> PostingLibrary:
    library = path or posting_library_path()
    if not library.is_file():
        return PostingLibrary()
    try:
        data = json.loads(library.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("not a library")
    except (OSError, ValueError):
        return PostingLibrary(unreadable=True)
    lib = PostingLibrary(found=True)
    for d in data.get("items") or []:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"]:
            continue
        key = _key(d["path"])
        if d.get("status") == "posted" and d.get("tiktok_id"):
            try:
                lib.posted[key] = float(d.get("posted_at") or 0)
            except (TypeError, ValueError):
                lib.posted[key] = 0.0
        else:
            lib.keep.add(key)
    ignored = data.get("ignored") if isinstance(data.get("ignored"), list) else []
    # A path can be in more than one entry: anything still waiting wins over posted or removed.
    lib.removed = {os.path.normcase(str(k)) for k in ignored if isinstance(k, str)} - lib.keep - set(lib.posted)
    for key in lib.keep:
        lib.posted.pop(key, None)
    return lib


# ---------------------------------------------------------------- the plan


@dataclass
class Item:
    kind: str
    path: Path
    size: int = 0  # bytes, with the companions
    companions: list[Path] = field(default_factory=list)  # a clip's .srt and thumbnail
    folder: Path | None = None  # the video folder whose clips.json lists it
    file: str = ""  # its "file" in that clips.json
    when: float = 0.0  # made (or, for posted clips, posted)
    title: str = ""


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # what the plan couldn't do, and why
    kept_for_posting: int = 0  # clips left alone because they're still waiting to post
    posting_found: bool = False
    roots: list[Path] = field(default_factory=list)  # folders never removed, even when empty

    def totals(self) -> dict[str, tuple[int, int]]:
        """kind -> (how many, bytes)."""
        out = {k: (0, 0) for k in KINDS}
        for it in self.items:
            n, b = out[it.kind]
            out[it.kind] = (n + 1, b + it.size)
        return out

    def size(self, kinds) -> int:
        return sum(it.size for it in self.items if it.kind in kinds)

    def count(self, kinds) -> int:
        return sum(1 for it in self.items if it.kind in kinds)


def busy_ids(db, session: Path | None = None) -> set[str]:
    """Videos automation has queued or is clipping, videos in the Studio, and videos waiting or being made
    for a linked Mac (their downloads, analysis and folders stay)."""
    jobs = [j for j in db.jobs(limit=5000) if j.status in ("queued", "working")]
    ids = {j.id for j in jobs} | {j.video_id for j in jobs}  # a request's copy is <id>~<request>
    working = db.meta("working_video")  # the real id of a video queued by an unusual link
    if working and any(j.status == "working" for j in jobs):
        ids.add(working)
    return ids | studio_ids(session) | mac_job_ids()


def source_id(source: str) -> str:
    """The id of the video a Studio source (a link or a file) is cached under, or "" when it can't
    be told without opening it."""
    if is_url(source):
        return video_id(source)
    try:
        return local_id(Path(source))
    except (OSError, ValueError):
        return ""


def mac_job_ids(index: Path | None = None) -> set[str]:
    """The videos a linked Mac sent this PC to make (remote/studio.py) that wait or are being made: a link's
    download is in the same cache as automation's."""
    try:
        data = json.loads((index or app_dir() / "mac_jobs" / "jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids = set()
    for j in (data.get("jobs") or []) if isinstance(data, dict) else []:
        if isinstance(j, dict) and j.get("status") in ("queued", "working"):
            # "video_id": the video's own, once it was opened (a link's can't always be read off it)
            source = str(j.get("source") or "")  # "" for a video the Mac sent as a file: that's not in the cache
            ids |= {source_id(source) if source else "", str(j.get("video_id") or "")}
    return ids - {""}


def studio_ids(session: Path | None = None) -> set[str]:
    """The videos in the Studio's list, opened yet or not (from its saved session)."""
    try:
        data = json.loads((session or app_dir() / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    ids = set()
    for e in data:
        if isinstance(e, dict) and isinstance(e.get("source"), str):
            ids |= {str(e.get("id") or ""), source_id(e["source"])}  # "id": the video's own, once it was opened
    return ids - {""}


def plan(s: Settings, now: float | None = None, busy: set[str] | frozenset = frozenset(),
         older_than_days: float = 0.0, kinds=KINDS, posting: PostingLibrary | None = None,
         temp_dirs: list[Path] | None = None) -> Plan:
    """What a clean-up of these kinds would remove. Changes nothing."""
    now = time.time() if now is None else now
    kinds = set(kinds)
    cutoff = now - max(0.0, older_than_days) * DAY
    p = Plan()
    lib = posting if posting is not None else read_posting()
    p.posting_found = lib.found
    roots = [s.output_path]
    if not _inside(s.auto_output_path, s.output_path):
        roots.append(s.auto_output_path)
    p.roots = roots + [s.auto_output_path] + [s.auto_output_path / n.folder for n in categories.load(s)]

    if kinds & set(CLIP_KINDS):
        if lib.unreadable:
            p.notes.append("The posting library couldn't be read just now, so no clips are cleared (some may "
                           "still be waiting to post). Try again in a minute.")
        else:
            seen: set[str] = set()
            for root in roots:
                for folder, data in _video_folders(root, s):
                    if _key(folder) not in seen:
                        seen.add(_key(folder))
                        _plan_folder(p, folder, data, lib, kinds, busy, cutoff, now)
    if kinds & set(CACHE_KINDS):
        _plan_cache(p, s.cache_path, kinds, busy, cutoff, now, own_folder=not s.cache_dir)
    if "previews" in kinds:
        _plan_previews(p, temp_dirs if temp_dirs is not None else _temp_dirs(), cutoff, now)
    order = {k: i for i, k in enumerate(KINDS)}
    p.items.sort(key=lambda it: (order[it.kind], str(it.path).lower()))
    return p


def _stat(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime


def _video_folders(root: Path, s: Settings):
    """(folder, clips.json data) for every exported video under root."""
    if not root.is_dir():
        return
    cache = s.cache_path
    for index in root.rglob("clips.json"):
        folder = index.parent
        if ".downloads" in index.parts or REJECTED_DIR in folder.parts or _inside(folder, cache):
            continue
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("clips"), list):
            yield folder, data


def _plan_folder(p: Plan, folder: Path, data: dict, lib: PostingLibrary, kinds: set[str], busy, cutoff: float,
                 now: float) -> None:
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    if video.get("id") in busy:
        return  # being clipped again right now
    listed: set[str] = set()
    for rec in data["clips"]:
        file = rec.get("file") if isinstance(rec, dict) else None
        if not isinstance(file, str) or not file:
            continue
        path = folder / file
        listed.add(_key(path))
        st = _stat(path)
        if st is None or not path.is_file() or not _inside(path, folder):
            continue
        size, mtime = st
        key = _key(path)
        if key in lib.keep:
            p.kept_for_posting += 1
            continue
        check = rec.get("qc") if isinstance(rec.get("qc"), dict) else {}
        if check.get("ok") is False or file.replace("\\", "/").startswith(f"{REJECTED_DIR}/"):
            kind, when = "rejected", mtime
        elif key in lib.posted:
            kind, when = "posted", lib.posted[key] or mtime
        elif key in lib.removed:
            kind, when = "removed", mtime
        else:
            continue
        if kind not in kinds or when > cutoff or now - mtime < FRESH:
            continue
        companions = [path.with_suffix(".srt")]
        if isinstance(rec.get("rank"), int):
            companions.append(folder / "thumbnails" / f"{rec['rank']:02d}.jpg")
        companions = [c for c in companions if c.is_file()]
        size += sum((_stat(c) or (0, 0))[0] for c in companions)
        p.items.append(Item(kind, path, size, companions, folder, file, when, str(rec.get("title") or path.stem)))
    # Clips the quality check set aside that the index no longer lists (from an earlier export).
    if "rejected" in kinds and (folder / REJECTED_DIR).is_dir():
        for f in sorted((folder / REJECTED_DIR).iterdir()):
            if f.suffix.lower() not in VIDEO_EXTS or _key(f) in listed or not f.is_file():
                continue
            st = _stat(f)
            if st is None or st[1] > cutoff or now - st[1] < FRESH or _key(f) in lib.keep:
                continue
            srt = f.with_suffix(".srt")
            companions = [srt] if srt.is_file() else []
            size = st[0] + sum((_stat(c) or (0, 0))[0] for c in companions)
            p.items.append(Item("rejected", f, size, companions, folder, "", st[1], f.stem))


def _download_part(name: str) -> bool:
    """An unfinished download as yt-dlp names it: <id>[.f137][.temp].mp4[.part][.ytdl] and so on."""
    bits = name.split(".")[1:]
    exts = [b for b in bits if not _DOWNLOAD_BITS.fullmatch(b)]
    return len(exts) == 1 and f".{exts[0].lower()}" in MEDIA_EXTS and _PARTIAL.search(name) is not None


def _plan_cache(p: Plan, cache: Path, kinds: set[str], busy, cutoff: float, now: float,
                own_folder: bool = True) -> None:
    """own_folder: the cache is ClipMonster's own .downloads folder. A folder picked in Settings
    may hold other files too, so there a file must be one ClipMonster saved the details of, or one
    only ClipMonster or yt-dlp would name that way (a browser leaves "<name>.mp4.part" too)."""
    if not cache.is_dir():
        return
    try:
        files = sorted(f for f in cache.iterdir() if f.is_file())
    except OSError:
        return
    names = {f.name for f in files}
    downloaded = {f.name.split(".", 1)[0] for f in files
                  if f.suffix.lower() in VIDEO_EXTS and f.name.count(".") == 1}
    for f in files:
        vid = f.name.split(".", 1)[0]
        if not vid or vid in busy:
            continue
        st = _stat(f)
        if st is None:
            continue
        size, mtime = st
        known = f"{vid}.video.json" in names
        if not known and not (_YOUTUBE_ID.fullmatch(vid) and (own_folder or _OWN.search(f.name) or (
                _download_part(f.name) and _YTDLP_ONLY.search(f.name)))):
            continue  # not ClipMonster's
        companions: list[Path] = []
        if _download_part(f.name):
            kind, idle = "partial", PARTIAL_IDLE
        elif f.suffix.lower() in VIDEO_EXTS and f.name == vid + f.suffix:
            kind, idle = "downloads", FRESH
            if known:  # its details go with it
                companions.append(cache / f"{vid}.video.json")
        elif f.name == f"{vid}.video.json" and vid in downloaded:
            continue  # goes with its download
        elif _ANALYSIS.search(f.name):
            kind, idle = "analysis", FRESH
        else:
            continue
        if kind not in kinds or now - mtime < idle or mtime > cutoff:
            continue
        size += sum((_stat(c) or (0, 0))[0] for c in companions)
        p.items.append(Item(kind, f, size, companions, when=mtime, title=f.name))


def _temp_dirs() -> list[Path]:
    """Where previews are: the work folder, and (before 2.8.0) Windows' temp folder."""
    return [work_dir(), Path(tempfile.gettempdir())]


def _previews(temp: Path) -> list[Path]:
    found = list((temp / "previews").glob("preview_*.mp4")) + list(temp.glob("layout_*.jpg"))
    found += list((temp / f"{APP_NAME}_previews").glob("preview_*.mp4"))  # before 2.8.0
    return found + list(temp.glob(f"{APP_NAME}_layout_*.jpg"))


def _plan_previews(p: Plan, temps: list[Path], cutoff: float, now: float) -> None:
    for f in sorted({f for temp in temps for f in _previews(temp)}):
        st = _stat(f)
        if st is None or now - st[1] < FRESH or st[1] > cutoff:
            continue
        p.items.append(Item("previews", f, st[0], when=st[1], title=f.name))


# ---------------------------------------------------------------- what a step left behind

def remove_download(video: Path) -> int:
    """Delete a downloaded video and what was saved beside it (its details, transcripts, captions and
    measurements, named <video id>.<...>). Returns the bytes freed."""
    freed = 0
    siblings = [f for f in video.parent.glob(f"{glob_escape(video.stem)}.*")
                if f != video and _ANALYSIS.search(f.name) and f.name.split(".", 1)[0] == video.stem]
    for f in [video, *siblings]:
        try:
            size = f.stat().st_size
            f.unlink()
            freed += size
        except OSError:
            pass
    return freed



WORK_STALE = 12 * 3600  # a step's folder nothing has written to for this long was left by a crash
PREVIEW_STALE = DAY
# What steps before 2.8.0 left in Windows' temp folder.
LEGACY_TEMP = ("clipmonster_*", "ytclip-polish-*", "cm-whisper-*", "cm-login-*")


def _newest(path: Path) -> float:
    """When anything in path last changed (0 when it's gone)."""
    newest = 0.0
    try:
        newest = path.lstat().st_mtime
        if path.is_dir() and not path.is_symlink():
            for f in path.rglob("*"):
                try:
                    newest = max(newest, f.lstat().st_mtime)
                except OSError:
                    pass
    except OSError:
        pass
    return newest


def _size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.lstat().st_size
        return sum(f.lstat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


def sweep_work(now: float | None = None, work: Path | None = None, temp: Path | None = None) -> tuple[int, int]:
    """Remove what a crash or a closed window left in the work folder (and, from before 2.8.0, in
    Windows' temp folder): step folders untouched for WORK_STALE, previews older than a day. Anything
    still in use is newer than that, or locked and left for next time. Returns (removed, bytes freed)."""
    now = time.time() if now is None else now
    work = work_dir() if work is None else work
    temp = Path(tempfile.gettempdir()) if temp is None else temp
    stale = [f for f in work.iterdir() if f.name != "previews"] if work.is_dir() else []
    for pattern in LEGACY_TEMP:
        stale += [f for f in temp.glob(pattern) if f.is_dir()]
    stale = [f for f in stale if now - _newest(f) > WORK_STALE]
    stale += [f for f in _previews(work) + _previews(temp) if now - _newest(f) > PREVIEW_STALE]
    removed = freed = 0
    for f in stale:
        size = _size(f)
        try:
            if f.is_dir() and not f.is_symlink():
                shutil.rmtree(f)
            else:
                f.unlink()
        except OSError:
            continue  # still open somewhere: next time
        removed += 1
        freed += size
    for empty in (temp / f"{APP_NAME}_previews",):
        try:
            empty.rmdir()
        except OSError:
            pass
    return removed, freed


# ---------------------------------------------------------------- doing it


@dataclass
class Result:
    removed: int = 0  # items (a clip with its .srt and thumbnail is one)
    freed: int = 0  # bytes
    by_kind: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, why)
    folders: int = 0  # video folders removed because no clips were left

    def summary(self, trashed: bool = False) -> str:
        """"3 posted clips (1.2 GB freed)"; trashed: they went to the Recycle Bin, which frees nothing yet."""
        parts = [f"{n} {NOUNS[k]}{'s' if n != 1 else ''}" for k in KINDS if (n := self.by_kind.get(k))]
        text = ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else "nothing"
        if trashed:
            return f"{text} ({size_text(self.freed)} moved to the Recycle Bin; emptying it frees the space)"
        return f"{text} ({size_text(self.freed)} freed)"


def _unlink(path: Path) -> None:
    path.unlink()


def apply(p: Plan, kinds, remove: Callable[[Path], None] | None = None, now: float | None = None) -> Result:
    """Remove what the plan lists for these kinds. `remove` deletes one file (or moves it to the
    Recycle Bin); it should raise OSError when it can't."""
    remove = remove or _unlink
    now = time.time() if now is None else now
    kinds = set(kinds)
    r = Result()
    touched: dict[Path, set[str]] = {}
    for it in p.items:
        if it.kind not in kinds:
            continue
        try:
            if it.path.exists():
                remove(it.path)
        except OSError as exc:
            r.failed.append((str(it.path), str(exc) or exc.__class__.__name__))
            continue
        for c in it.companions:
            try:
                if c.exists():
                    remove(c)
            except OSError as exc:
                r.failed.append((str(c), str(exc) or exc.__class__.__name__))
        r.removed += 1
        r.freed += it.size
        r.by_kind[it.kind] = r.by_kind.get(it.kind, 0) + 1
        if it.folder is not None:
            touched.setdefault(it.folder, set()).add(it.file)
    for folder, files in touched.items():
        try:
            if _tidy(folder, files - {""}, p.roots, remove, now):
                r.folders += 1
        except OSError as exc:
            r.failed.append((str(folder), str(exc) or exc.__class__.__name__))
    return r


def _tidy(folder: Path, removed: set[str], roots: list[Path], remove: Callable[[Path], None], now: float) -> bool:
    """After clips left a video folder: take them out of its clips.json, or, when it has no clips
    left, remove the folder's own files and any folders that leaves empty. True if it went."""
    index = folder / "clips.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        return False
    keep = [rec for rec in data["clips"] if not (isinstance(rec, dict) and rec.get("file") in removed)]
    left = [rec for rec in keep if isinstance(rec, dict) and isinstance(rec.get("file"), str)
            and (folder / rec["file"]).is_file()]
    if left:
        if len(keep) != len(data["clips"]):
            data["clips"] = keep
            data["cleaned_up"] = now
            if isinstance(data.get("qc"), dict):  # the counts the Library shows follow
                checks = [rec.get("qc") if isinstance(rec.get("qc"), dict) else {} for rec in keep
                          if isinstance(rec, dict)]
                data["qc"] = {"passed": sum(c.get("ok") is not False for c in checks),
                              "rejected": sum(c.get("ok") is False for c in checks),
                              "warned": sum(c.get("ok") is not False and bool(c.get("warnings")) for c in checks)}
            _rewrite(index, json.dumps(data, indent=2, ensure_ascii=False))
            readme = folder / "README.md"
            try:
                _rewrite(readme, _readme_without(readme.read_text(encoding="utf-8"), removed))
            except OSError:
                pass
        _rmdir(folder / REJECTED_DIR)  # once its last rejected clip is gone
        return False
    # Nothing left to post from this video: its own files go, and what the Video Creator or the Studio kept
    # beside a video they made (storyboard, scene pictures, timeline and its files), as posting's own
    # delete-after-posting does.
    from .posting.records import creation_dirs, creation_files

    for f in [index, folder / "README.md", *sorted((folder / "thumbnails").glob("[0-9][0-9].jpg")),
              *creation_files(folder, data)]:
        if f.is_file():
            remove(f)
    for d in (folder / "thumbnails", folder / REJECTED_DIR, *creation_dirs(folder)):
        _rmdir(d)
    keep_dirs = {_key(r) for r in roots}
    d = folder
    while _key(d) not in keep_dirs and any(_inside(d, r) for r in roots) and _rmdir(d):
        d = d.parent
    return not folder.exists()


def _rewrite(path: Path, text: str) -> None:
    """Replace a file's text, keeping its date (the Library lists videos by when they were made)."""
    st = path.stat()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    os.utime(path, (st.st_atime, st.st_mtime))


def _readme_without(text: str, files: set[str]) -> str:
    """A video folder's README.md without the table rows of these clip files."""
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
    """Remove a folder if it's empty."""
    try:
        d.rmdir()
        return True
    except OSError:
        return False
