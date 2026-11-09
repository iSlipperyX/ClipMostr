"""Videos made on your PC for ClipMonster for Mac: "Make on: Your PC" in the Studio.

A link goes to the PC as it is and the PC downloads it there; a video file on the Mac is sent over first
(PUT /api/studio/uploads). The PC makes it start to finish with this Mac's clip settings (remote/studio.py
MacJobs), one video at a time between automation's own, on its graphics card and with its local AI. Then the Mac
brings the clips into its own output folder, where the Library shows them, and tells the PC it has them
(POST …/done) so the PC can let them go.

The list is kept in pc_jobs.json, so a video carries on while the Mac app is closed: the PC keeps making it, and
its clips are fetched when the app opens again. Each video belongs to the PC it was sent to; one sent to another
PC waits until that PC is linked again.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from PySide6.QtCore import QObject, QTimer, Signal

from . import common as c
from .client import ApiError, Offline
from .paths import support_dir

POLL_MS = 4000  # how often the PC is asked how its videos are getting on (while it's making any)
ACTIVE = ("sending", "queued", "working", "fetching")
FINISHED = ("done", "failed", "cancelled")
KEEP_DAYS = 7  # a finished video stays in the Studio's list this long (or until it's removed)
DAY = 86400.0


def settings_to_send(s) -> dict:
    """This Mac's clip settings, as the PC takes them (remote/studio.travelling)."""
    from ..remote.studio import travelling

    return {name: getattr(s, name) for name in travelling() if hasattr(s, name)}


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


class PCJobs(QObject):
    added = Signal(str)  # a video's key: a new row
    changed = Signal(str)  # its row shows what changed
    removed = Signal(str)
    landed = Signal(str)  # its clips are in this Mac's output folder now
    failed = Signal(str)
    _moved = Signal(str, dict)  # from a thread sending a video or bringing clips: what changed
    _ended = Signal(str)  # that thread is done

    def __init__(self, link, settings: Callable, path: Path | None = None, parent=None,
                 now: Callable[[], float] = time.time):
        super().__init__(parent)
        self.link = link  # pc.PCLink
        self.settings = settings
        self.path = path or support_dir() / "pc_jobs.json"
        self.now = now
        self.jobs: dict[str, dict] = {}
        self._threads: dict[str, threading.Event] = {}  # key -> its stop flag, while it's sending or fetching
        self._polling = False
        self._moved.connect(self._apply)
        self._ended.connect(self._thread_ended)
        self._load()
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        link.changed.connect(self._link_changed)
        link.event.connect(self._event)

    # --- the list -------------------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        now = self.now()
        dropped = False
        for j in (data.get("jobs") or []) if isinstance(data, dict) else []:
            if not isinstance(j, dict) or not j.get("key"):
                continue
            if j.get("status") in FINISHED and now - float(j.get("updated") or 0) > KEEP_DAYS * DAY:
                dropped = True
                continue
            if j.get("status") == "sending":  # ClipMonster closed while it was going to the PC
                j.update(status="failed", frac=None, stage="",
                         error="Sending it stopped when ClipMonster closed. Right-click it and pick Send again.")
            j["frac"] = None if j.get("status") != "fetching" else j.get("frac")
            self.jobs[j["key"]] = j
        if dropped:
            self._save()

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": 1, "jobs": self.ordered()}, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def ordered(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j.get("created") or 0)

    def get(self, key: str) -> dict | None:
        return self.jobs.get(key)

    def active(self) -> list[dict]:
        return [j for j in self.ordered() if j.get("status") in ACTIVE]

    def _apply(self, key: str, changes: dict) -> None:
        job = self.jobs.get(key)
        if job is None:
            return
        was = job.get("status")
        job.update(changes)
        if changes.keys() - {"frac", "stage"}:  # not every tick of a transfer
            job["updated"] = self.now()
            self._save()
        self.changed.emit(key)
        now = job.get("status")
        if now != was:
            if now == "done":
                self.landed.emit(key)
            elif now == "failed":
                self.failed.emit(key)

    def _thread_ended(self, key: str) -> None:
        self._threads.pop(key, None)

    # --- sending --------------------------------------------------------------------------------------------

    def can_send(self) -> str:
        """"" when videos can go to the PC now, else why not."""
        if not self.link.linked:
            return "No PC is linked: link yours under Your PC in the sidebar."
        if not self.link.online:
            return f"{self.link.name or 'Your PC'} can't be reached right now."
        if not (self.link.lends() or {}).get("studio"):
            return f"{self.link.name or 'Your PC'} runs an older ClipMonster that can't make videos for a Mac: update it."
        return ""

    def send(self, sources: list[str]) -> list[str]:
        """Each YouTube link or video file to the PC to make. Returns their keys."""
        from ..download import is_url

        session = self.link.session
        if session is None:
            return []
        s = self.settings()
        sent = settings_to_send(s)
        keys = []
        for src in sources:
            kind = "url" if is_url(src) else "file"
            now = self.now()
            key = secrets.token_hex(6)
            name = src if kind == "url" else Path(src).name
            self.jobs[key] = {
                "key": key, "host": session.host.id, "host_name": self.link.name, "kind": kind, "source": src,
                "name": name, "title": "" if kind == "url" else Path(src).stem, "id": "", "status": "sending",
                "frac": 0.0 if kind == "file" else None, "stage": "Sending it to your PC", "progress": None,
                "waiting": 0, "error": "", "clips": 0, "rejected": 0, "files": [], "folder": "", "here": "",
                "created": now, "updated": now, "settings": sent}
            keys.append(key)
            self._save()
            self.added.emit(key)
            self._start_send(key)
        return keys

    def send_again(self, key: str) -> None:
        job = self.jobs.get(key)
        if job is None or job.get("status") not in ("failed", "cancelled") or self.can_send():
            return
        self._apply(key, {"status": "sending", "id": "", "error": "", "frac": 0.0 if job["kind"] == "file" else None,
                          "stage": "Sending it to your PC", "progress": None, "files": [], "folder": "", "here": "",
                          "host": self.link.session.host.id, "host_name": self.link.name,
                          "settings": settings_to_send(self.settings())})
        self._start_send(key)

    def _start_send(self, key: str) -> None:
        job = self.jobs[key]
        client = self.link.client()
        if client is None or key in self._threads:
            return
        stop = threading.Event()
        self._threads[key] = stop
        room = int((self.link.lends() or {}).get("room") or 0)
        kind, source = job["kind"], job["source"]
        body = {"settings": job.get("settings") or {}, "title": job.get("title") or ""}

        def work() -> None:
            try:
                if kind == "file":
                    from ..remote.studio import VIDEO_EXTS

                    path = Path(source)
                    try:
                        size = path.stat().st_size
                    except OSError:
                        raise ApiError(404, "The video isn't on this Mac any more.") from None
                    if path.suffix.lower() not in VIDEO_EXTS:
                        raise ApiError(400, f"Your PC can't make clips from a {path.suffix or 'file like this'} "
                                            "file: send it a video.")
                    if room and size > room:
                        raise ApiError(507, f"Your PC hasn't room for this video ({c.size(size)}; it has "
                                            f"{c.size(room)} to spare).")
                    last = [0.0]

                    def sending(done: int, total: int) -> None:
                        t = time.monotonic()
                        if t - last[0] >= 0.25 or done >= total:
                            last[0] = t
                            self._moved.emit(key, {"frac": done / total if total else None,
                                                   "stage": f"Sending it to your PC · {c.size(done)} of {c.size(total)}"})

                    up = client.upload(path, path.name, sending, stop.is_set)
                    body.update(upload=up["upload"], name=path.name)
                else:
                    body["source"] = source
                got = client.post("/api/studio/jobs", body, timeout=30.0)
                if stop.is_set() and got.get("id"):  # stopped or removed while it went: the PC needn't make it
                    path = f"/api/studio/jobs/{quote(str(got['id']), safe='')}"
                    try:
                        client.post(path + "/cancel")
                        client.post(path + "/done")
                    except ApiError:
                        pass
                    raise ApiError(499, "Stopped.")
                self._moved.emit(key, {**self._from_pc(got), "frac": None})
            except ApiError as exc:
                if stop.is_set():
                    self._moved.emit(key, {"status": "cancelled", "stage": "Stopped", "frac": None, "error": ""})
                else:
                    self._moved.emit(key, {"status": "failed", "error": exc.message, "frac": None, "stage": ""})
            except Exception as exc:  # noqa: BLE001 - a failure in words, on the row
                self._moved.emit(key, {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "frac": None,
                                       "stage": ""})
            finally:
                self._ended.emit(key)

        threading.Thread(target=work, name=f"pc-send-{key}", daemon=True).start()

    @staticmethod
    def _from_pc(d: dict) -> dict:
        """The PC's word on a video, as this list keeps it."""
        out = {"id": str(d.get("id") or ""), "status": str(d.get("status") or "queued"),
               "stage": str(d.get("stage") or ""), "progress": d.get("progress"), "waiting": int(d.get("waiting") or 0),
               "error": str(d.get("error") or ""), "clips": int(d.get("clips") or 0),
               "rejected": int(d.get("rejected") or 0), "files": d.get("files") or [], "folder": str(d.get("folder") or "")}
        if d.get("title"):
            out["title"] = str(d["title"])
        return out

    # --- following them on the PC ---------------------------------------------------------------------------

    def _here(self, job: dict) -> bool:
        """The video was sent to the PC that's linked now."""
        s = self.link.session
        return s is not None and job.get("host") == s.host.id

    def poll(self) -> None:
        if self._polling or not self.link.online:
            return
        mine = [j for j in self.ordered() if self._here(j) and j.get("id")]
        for j in mine:  # clips the PC finished while they couldn't be fetched (offline, or the app was closed)
            if j["status"] == "fetching" and j["key"] not in self._threads:
                self._start_fetch(j["key"])
        if not any(j["status"] in ("queued", "working") for j in mine):
            return
        self._polling = True

        def ok(got: dict) -> None:
            self._polling = False
            by_id = {d.get("id"): d for d in got.get("jobs") or [] if isinstance(d, dict)}
            for j in [j for j in self.ordered() if self._here(j) and j.get("id")]:
                if j["status"] not in ("queued", "working"):
                    continue
                d = by_id.get(j["id"])
                if d is None:
                    self._apply(j["key"], {"status": "failed", "error": "Your PC doesn't have this video any more.",
                                           "stage": ""})
                    continue
                changes = self._from_pc(d)
                if changes["status"] == "done":
                    changes.update(status="fetching", stage="Bringing the clips to this Mac", frac=0.0)
                elif changes["status"] == "cancelled":
                    changes["error"] = ""
                self._apply(j["key"], changes)
                if changes["status"] == "fetching":
                    self._start_fetch(j["key"])

        def fail(_exc) -> None:
            self._polling = False

        self.link.session.call(lambda cl: cl.get("/api/studio/jobs"), ok, fail)

    def _event(self, e: dict) -> None:
        if e.get("kind") == "studio" and not e.get("old"):
            QTimer.singleShot(200, self.poll)

    def _link_changed(self) -> None:
        if self.link.online:
            QTimer.singleShot(0, self.poll)
        for j in self.ordered():  # rows say which PC a video waits for
            if j.get("status") in ACTIVE:
                self.changed.emit(j["key"])

    def waiting_for(self, job: dict) -> str:
        """Why a video isn't moving, if it's waiting for the PC: "" when it's on its way."""
        if job.get("status") not in ACTIVE or job.get("status") == "sending":
            return ""
        name = job.get("host_name") or "your PC"
        if not self._here(job):
            return f"Waiting for {name} to be linked again"
        if not self.link.online:
            return f"Waiting for {name} to come back"
        return ""

    # --- bringing the clips over ------------------------------------------------------------------------------

    def _start_fetch(self, key: str) -> None:
        job = self.jobs.get(key)
        client = self.link.client()
        if job is None or client is None or key in self._threads or not job.get("id"):
            return
        stop = threading.Event()
        self._threads[key] = stop
        out_root = Path(self.settings().output_path)
        files = [f for f in job.get("files") or [] if isinstance(f, dict) and f.get("path")]
        total = sum(int(f.get("size") or 0) for f in files) or 1
        job_id, folder_rel = job["id"], job.get("folder") or ""
        source, kind, host_name = job["source"], job["kind"], job.get("host_name") or "your PC"

        def work() -> None:
            done = 0
            last = [0.0]

            def fetching(got: int, _total: int) -> None:
                t = time.monotonic()
                if t - last[0] >= 0.3:
                    last[0] = t
                    have = done + got
                    self._moved.emit(key, {"frac": min(1.0, have / total),
                                           "stage": f"Bringing the clips to this Mac · {c.size(have)} of {c.size(total)}"})

            try:
                for f in files:
                    rel = str(f["path"])
                    dest = out_root / rel
                    if not _inside(dest, out_root):
                        continue
                    size = int(f.get("size") or 0)
                    if not (dest.is_file() and dest.stat().st_size == size):  # (one fetched before a break stays)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        client.download(f"/api/studio/jobs/{quote(job_id, safe='')}/file?{urlencode({'path': rel})}",
                                        dest, fetching, stop.is_set)
                    done += size
                folder = out_root / folder_rel if folder_rel else out_root
                _mark_index(folder, source, kind, host_name)
                try:
                    client.post(f"/api/studio/jobs/{quote(job_id, safe='')}/done", timeout=15.0)
                except ApiError:
                    pass  # the PC lets them go by itself after a few days
                self._moved.emit(key, {"status": "done", "here": str(folder), "frac": None, "stage": "Done"})
            except Offline as exc:  # tried again when the PC is back
                self._moved.emit(key, {"frac": None, "stage": f"Waiting for {host_name} to bring the clips over",
                                       "error": exc.message})
            except ApiError as exc:
                if exc.status == 404:
                    self._moved.emit(key, {"status": "failed", "frac": None, "stage": "",
                                           "error": "Your PC doesn't have these clips any more."})
                elif not stop.is_set():
                    self._moved.emit(key, {"status": "failed", "frac": None, "stage": "", "error": exc.message})
            except OSError as exc:
                self._moved.emit(key, {"status": "failed", "frac": None, "stage": "",
                                       "error": f"The clips couldn't be saved on this Mac ({exc.strerror or exc})."})
            finally:
                self._ended.emit(key)

        threading.Thread(target=work, name=f"pc-fetch-{key}", daemon=True).start()

    # --- stopping and removing ----------------------------------------------------------------------------------

    def stop(self, key: str) -> None:
        """Stop a video: while it's going to the PC, or on the PC."""
        job = self.jobs.get(key)
        if job is None:
            return
        if job["status"] == "sending":
            ev = self._threads.get(key)
            if ev is not None:
                ev.set()
            return
        if job["status"] in ("queued", "working") and job.get("id") and self._here(job) and self.link.session:
            self._apply(key, {"stage": "Stopping…"})
            self.link.session.call(lambda cl: cl.post(f"/api/studio/jobs/{quote(job['id'], safe='')}/cancel"),
                                   lambda d: self._apply(key, {**self._from_pc(d), "error": ""}),
                                   lambda exc: self._apply(key, {"stage": getattr(exc, "message", str(exc))}))

    def remove(self, key: str) -> None:
        """Take a video off the list. The PC stops it, or lets its clips go, if it's still at it."""
        job = self.jobs.pop(key, None)
        if job is None:
            return
        ev = self._threads.pop(key, None)
        if ev is not None:
            ev.set()
        session = self.link.session
        if job.get("id") and session is not None and job.get("host") == session.host.id and \
                job.get("status") in ("queued", "working", "fetching", "failed", "cancelled"):
            path = f"/api/studio/jobs/{quote(job['id'], safe='')}"

            def let_go(cl):
                try:
                    cl.post(path + "/cancel")
                except ApiError:
                    pass
                return cl.post(path + "/done")

            session.call(let_go, None, lambda _e: None)
        self._save()
        self.removed.emit(key)

    def shutdown(self) -> None:
        self.timer.stop()
        for ev in self._threads.values():
            ev.set()


def _mark_index(folder: Path, source: str, kind: str, made_on: str) -> None:
    """The fetched video's clips.json, pointing at the video here (not at the copy that went to the PC), so the
    Library's Re-open in Studio opens it, and saying where it was made."""
    index = folder / "clips.json"
    try:
        meta = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(meta, dict):
        return
    meta["made_on"] = {"pc": made_on}
    if kind == "file":
        old = str(meta.get("source") or "")
        meta["source"] = source
        if isinstance(meta.get("video"), dict):
            meta["video"]["url"] = source
        for rec in meta.get("clips") or []:
            if isinstance(rec, dict) and rec.get("watch") == old:
                rec["watch"] = source
    try:
        tmp = index.with_name("clips.json.tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, index)
    except OSError:
        pass
