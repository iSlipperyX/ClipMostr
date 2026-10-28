"""Clean up: clear out what ClipMonster leaves behind that nothing needs any more.

plan() only looks; apply() removes what a plan lists, for the kinds you pick (CLEANUP_KINDS):

* partial: unfinished downloads in the download cache that nothing has written to for a while
* previews: what "Render preview" and the Layout tab's preview leave in the temp folder
* downloads: full videos in the download cache
* analysis: the transcripts, captions and measurements saved beside them (a video opens again
  instantly with them; without them it is measured again)
* rejected: clips the quality check rejected (TikTok Manager never takes those)
* posted: clips TikTok Manager says you've posted
* removed: clips you removed from TikTok Manager's library (it never brings those back)
* uploaded: clips that are safely on your storage server (uploads.py), the same file that went up

It never touches a video that's queued, being clipped or open in the Studio, a clip TikTok Manager
still has (in its library, queued, failed or uploading), anything changed in the last few minutes,
or anything that isn't ClipMonster's own: a clip must be listed in a clips.json, a cache file must
be named after a video (in a download cache folder you picked yourself, one ClipMonster saved the
details of, or named as only ClipMonster or yt-dlp would). Unfinished downloads keep to the age
picked too. A download goes with its details (.video.json); a clip goes with its .srt and
thumbnail, and leaves its clips.json (its counts and README follow); a video
folder with no clips left goes altogether, and so do folders it leaves empty (not the Automation
folder or a niche's folder, which TikTok Manager may be watching).

TikTok Manager's library (library.json in its settings folder) is only ever read.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import niches
from .download import VIDEO_EXTS, is_url, local_id, video_id
from .settings import APP_NAME, CLEANUP_KINDS, Settings, app_dir

DAY = 86400.0
KINDS = tuple(k for k, _ in CLEANUP_KINDS)
LABELS = dict(CLEANUP_KINDS)
NOUNS = {"partial": "unfinished download", "previews": "preview render", "downloads": "downloaded video",
         "analysis": "saved analysis file", "rejected": "rejected clip", "posted": "posted clip",
         "removed": "removed clip", "uploaded": "uploaded clip"}
CLIP_KINDS = ("rejected", "posted", "removed", "uploaded")
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
    """How TikTok Manager names a file in its library (store._key) for a path on this PC."""
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


# ---------------------------------------------------------------- TikTok Manager's side


@dataclass
class TikTokManager:
    """What TikTok Manager's library says about clip files, by their _key()."""

    found: bool = False  # its library was read
    unreadable: bool = False  # it exists but couldn't be read, so nothing it might hold is cleared
    keep: set[str] = field(default_factory=set)  # still in its library and not posted
    posted: dict[str, float] = field(default_factory=dict)  # posted -> when
    removed: set[str] = field(default_factory=set)  # taken out of its library by you
    watch_folders: list[str] = field(default_factory=list)


def tiktok_manager_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "TikTokManager"


def read_tiktok_manager(folder: Path | None = None) -> TikTokManager:
    folder = folder or tiktok_manager_dir()
    library = folder / "library.json"
    if not library.is_file():
        return TikTokManager()
    try:
        data = json.loads(library.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
            raise ValueError("not a library")
    except (OSError, ValueError):
        return TikTokManager(unreadable=True)
    tm = TikTokManager(found=True)
    for d in data.get("items") or []:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"]:
            continue
        key = _key(d["path"])
        if d.get("status") == "posted":
            try:
                tm.posted[key] = float(d.get("posted_at") or 0)
            except (TypeError, ValueError):
                tm.posted[key] = 0.0
        else:
            tm.keep.add(key)
    ignored = data.get("ignored") if isinstance(data.get("ignored"), list) else []
    # A path can be in more than one entry: anything still in the library wins over posted or removed.
    tm.removed = {os.path.normcase(str(k)) for k in ignored if isinstance(k, str)} - tm.keep - set(tm.posted)
    for key in tm.keep:
        tm.posted.pop(key, None)
    try:
        cfg = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        tm.watch_folders = [str(f) for f in cfg.get("watch_folders") or [] if f] if isinstance(cfg, dict) else []
    except (OSError, ValueError, TypeError):
        pass
    return tm


def _watched(folder: Path, watch_folders: list[str]) -> bool:
    """TikTok Manager watches this folder, a folder around it, or a folder inside it."""
    for w in watch_folders:
        wp = Path(os.path.expanduser(w))
        if _inside(folder, wp) or _inside(wp, folder):
            return True
    return False


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
    kept_for_tm: int = 0  # clips left alone because TikTok Manager still has them
    tm_found: bool = False
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
    """Videos automation has queued or is clipping, and videos in the Studio (their downloads,
    analysis and folders stay)."""
    jobs = [j for j in db.jobs(limit=5000) if j.status in ("queued", "working")]
    ids = {j.id for j in jobs} | {j.video_id for j in jobs}  # a request's copy is <id>~<request>
    working = db.meta("working_video")  # the real id of a video queued by an unusual link
    if working and any(j.status == "working" for j in jobs):
        ids.add(working)
    return ids | studio_ids(session)


def source_id(source: str) -> str:
    """The id of the video a Studio source (a link or a file) is cached under, or "" when it can't
    be told without opening it."""
    if is_url(source):
        return video_id(source)
    try:
        return local_id(Path(source))
    except (OSError, ValueError):
        return ""


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
         older_than_days: float = 0.0, kinds=KINDS, tm: TikTokManager | None = None,
         temp_dir: Path | None = None, uploaded: dict | None = None) -> Plan:
    """What a clean-up of these kinds would remove. Changes nothing. `uploaded`: uploads.uploaded()
    (read from jobs.db when it's needed and not given)."""
    now = time.time() if now is None else now
    kinds = set(kinds)
    if "uploaded" in kinds and uploaded is None:
        uploaded = _uploaded()
    cutoff = now - max(0.0, older_than_days) * DAY
    p = Plan()
    tm = tm if tm is not None else read_tiktok_manager()
    p.tm_found = tm.found
    roots = [s.output_path]
    if not _inside(s.auto_output_path, s.output_path):
        roots.append(s.auto_output_path)
    p.roots = roots + [s.auto_output_path] + [s.auto_output_path / n.folder for n in niches.load(s)]

    if kinds & set(CLIP_KINDS):
        if tm.unreadable:
            p.notes.append("TikTok Manager's library couldn't be read just now, so no clips are cleared (it might "
                           "still need them). Try again with TikTok Manager closed.")
        else:
            if not tm.found and kinds & {"posted", "removed"}:
                p.notes.append("TikTok Manager isn't set up on this PC, so ClipMonster can't tell which clips "
                               "you've posted or removed. Only rejected and uploaded clips can be cleared.")
            elif tm.found and s.auto_output_path.is_dir() and not _watched(s.auto_output_path, tm.watch_folders):
                p.notes.append("TikTok Manager isn't watching your Automation folder, so its clips never reach "
                               "it. Add the folder in TikTok Manager (Settings → Ready to upload).")
            seen: set[str] = set()
            for root in roots:
                for folder, data in _video_folders(root, s):
                    if _key(folder) not in seen:
                        seen.add(_key(folder))
                        _plan_folder(p, folder, data, tm, kinds, busy, cutoff, now, uploaded or {})
    if kinds & set(CACHE_KINDS):
        _plan_cache(p, s.cache_path, kinds, busy, cutoff, now, own_folder=not s.cache_dir)
    if "previews" in kinds:
        _plan_previews(p, temp_dir or Path(tempfile.gettempdir()), cutoff, now)
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


def _uploaded() -> dict:
    from . import uploads
    from .jobs import JobDB

    try:
        db = JobDB()
    except Exception:
        return {}
    try:
        return uploads.uploaded(db)
    finally:
        db.close()


def _plan_folder(p: Plan, folder: Path, data: dict, tm: TikTokManager, kinds: set[str], busy, cutoff: float,
                 now: float, uploaded: dict | None = None) -> None:
    from .uploads import _norm

    uploaded = uploaded or {}
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
        if key in tm.keep:
            p.kept_for_tm += 1
            continue
        check = rec.get("qc") if isinstance(rec.get("qc"), dict) else {}
        if check.get("ok") is False or file.replace("\\", "/").startswith(f"{REJECTED_DIR}/"):
            kind, when = "rejected", mtime
        elif key in tm.posted:
            kind, when = "posted", tm.posted[key] or mtime
        elif key in tm.removed:
            kind, when = "removed", mtime
        elif (up := uploaded.get(_norm(path))) and up[0] == size and abs(up[1] - mtime) < 1:
            kind, when = "uploaded", up[2]  # the very file that went up, as long ago as it went
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
            if st is None or st[1] > cutoff or now - st[1] < FRESH or _key(f) in tm.keep:
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


def _plan_previews(p: Plan, temp: Path, cutoff: float, now: float) -> None:
    found = []
    folder = temp / f"{APP_NAME}_previews"
    if folder.is_dir():
        found += [f for f in folder.glob("preview_*.mp4")]
    found += list(temp.glob(f"{APP_NAME}_layout_*.jpg"))
    for f in sorted(found):
        st = _stat(f)
        if st is None or now - st[1] < FRESH or st[1] > cutoff:
            continue
        p.items.append(Item("previews", f, st[0], when=st[1], title=f.name))


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
    # Nothing left to post from this video.
    for f in [index, folder / "README.md", *sorted((folder / "thumbnails").glob("[0-9][0-9].jpg"))]:
        if f.is_file():
            remove(f)
    for d in (folder / "thumbnails", folder / REJECTED_DIR):
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
