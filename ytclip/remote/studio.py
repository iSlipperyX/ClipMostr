"""Work a linked Mac hands this PC: questions for its local AI, and whole videos to make clips from.

ClipMonster for Mac (ytclip/mac) works on its own, and can lean on the PC it's linked to in two ways:

* **The PC's local AI** (Settings → AI → "Your PC's local AI" on the Mac). The Mac reads the video and
  makes the clips itself; only the questions (pick the clips, polish them) come here, to the model this PC
  runs (intelligence/), so the Mac needn't hold a model of several gigabytes. Each question is an AITask on a
  thread of its own: the Mac starts it (POST /api/ai/tasks), waits on it in short long polls
  (GET /api/ai/tasks/<id>?wait=20, so a slow answer never outlasts a connection) and may stop it. Only the
  local AI is asked: a Mac never spends this PC's Claude or Gemini key.

* **The PC's processing** ("Make on: Your PC" in the Mac's Studio). The whole video is made here, start to
  finish: downloaded (or sent from the Mac first: PUT /api/studio/uploads), typed out, its clips found and
  exported, with the Mac's own clip settings (how many, how long, its captions and layout), on this PC's
  graphics card and with its local AI (never its Claude or Gemini key). MacJobs keeps them in mac_jobs/, makes
  one at a time (automation waits for it between its own videos), and keeps the clips until the Mac has fetched
  them into its Library and says so (POST /api/studio/jobs/<id>/done), or for KEEP_DAYS. A Mac that's unlinked
  takes its videos with it.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable

from .host import HostError

AI_KEEP = 600.0  # s a finished AI task waits for the Mac to read its answer
AI_MAX = 6  # AI tasks waiting or running at once (all Macs)
AI_IDLE = 120.0  # s without the Mac asking after a running task before it's taken as gone and stopped
KEEP_DAYS = 3.0  # a video's clips wait this long for the Mac to fetch them
GONE_DAYS = 1.0  # a failed or stopped video is listed this long
JOBS_MAX = 20  # videos waiting or being made at once
UPLOAD_MAX = 40 * 1024 ** 3
UPLOAD_KEEP = 86400.0  # an upload no video uses goes after this long
FREE_AFTER_UPLOAD = 2 * 1024 ** 3  # room left on the disk after an upload, for the clips
LOOP_RETRY = 5.0  # s before the list is looked at again after it couldn't be read or saved
LINK_CHECK = 3.0  # s between looks at whether the Mac a video is being made for is still linked
DAY = 86400.0
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".ts", ".mts", ".wmv", ".flv")

# The Mac's settings a video made here keeps: what clips it wants and how they look. Never where files go, the
# encoder (this PC picks its own), keys, or anything pointing at a file on the Mac.
TRAVEL_GROUPS = ("Clip finding", "Transcription", "Export", "Captions", "Editing", "Layout")
TRAVEL_AI = ("ai_instructions", "ai_polish", "polish_bottom_text", "polish_overpick", "polish_burned_subs")
NEVER = {"encoder", "fast_copy", "write_srt", "stack_enabled", "music_folder", "broll_folder", "stack_video",
         "stack_folder"}
EXTRA = ("sport",)  # settings with no row in the Settings window that still change the clips


def travelling() -> list[str]:
    """The settings' names a Mac sends with a video (and this PC accepts)."""
    from ..settings import SCHEMA

    names = [f.name for f in SCHEMA
             if (f.group in TRAVEL_GROUPS or f.name in TRAVEL_AI) and f.kind not in ("path", "file", "secret")
             and f.name not in NEVER]
    return names + [n for n in EXTRA if n not in names]


def clean_settings(sent, base=None) -> dict:
    """The part of `sent` (the Mac's settings) this PC takes, each value the type it should be."""
    from ..settings import Settings

    base = base or Settings()
    if not isinstance(sent, dict):
        return {}
    known = {f.name for f in fields(base)}
    out = {}
    for name in travelling():
        if name not in sent or name not in known:
            continue
        value, default = sent[name], getattr(base, name)
        if isinstance(default, bool):
            if isinstance(value, bool):
                out[name] = value
        elif isinstance(default, int):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[name] = int(value)
        elif isinstance(default, float):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[name] = float(value)
        elif isinstance(default, str):
            if isinstance(value, str) and len(value) <= 4000:
                out[name] = value
    return out


def _text(value, n: int = 200) -> str:
    return " ".join(str(value or "").split())[:n]


def _file_name(name) -> str:
    """A video's name from the Mac, safe as a file name on this PC (no folders, no characters Windows refuses)."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r'[<>:"|?*\x00-\x1f]', "", base).strip(" .")
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = stem[:120].strip(" .") or "video"
    if stem.upper() in _RESERVED:
        stem = f"_{stem}"
    return f"{stem}.{ext.lower()}" if ext else stem


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


# ---------------------------------------------------------------- the local AI


class AITask:
    def __init__(self, kind: str, device: str, payload: dict, now: float):
        self.id = secrets.token_hex(8)
        self.kind, self.device, self.payload = kind, device, payload
        self.state = "waiting"  # waiting | working | done | failed | stopped
        self.stage, self.frac = "Waiting for your PC's local AI", None
        self.result: dict | None = None
        self.error = ""
        self.created = self.asked = now
        self.finished = 0.0
        self.rev = 0  # counts each change, so a Mac's long poll answers straight away with what it hasn't seen
        self.stop = threading.Event()

    def json(self) -> dict:
        out = {"id": self.id, "state": self.state, "stage": self.stage, "frac": self.frac, "rev": self.rev}
        if self.state == "done":
            out["result"] = self.result
        elif self.state in ("failed", "stopped"):
            out["error"] = self.error
            if self.result:  # what kind of failure ({"kind": "cut_off" | "refused"}), for the Mac to raise the same
                out["result"] = self.result
        return out


class Stopped(Exception):
    """The Mac stopped the task (or went away)."""


class AITasks:
    """Questions from Macs for this PC's local AI, each on a thread of its own (the AI answers one at a time:
    llama_engine keeps to that itself)."""

    def __init__(self, settings: Callable, log: Callable[[str], None], now: Callable[[], float] = time.time,
                 ask=None, find=None):
        self.settings, self.log, self.now = settings, log, now
        self._ask = ask or _ask_local  # tests swap in their own
        self._find = find or _find_local
        self.tasks: dict[str, AITask] = {}
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def status(self, fresh: bool = False) -> dict:
        """Whether a Mac can use this PC's local AI, and which model it has (looked at every half minute)."""
        cached = getattr(self, "_status", None)
        if cached and not fresh and self.now() - cached[0] < 30:
            return cached[1]
        out = self._look()
        self._status = (self.now(), out)
        return out

    def _look(self) -> dict:
        from .. import ai

        s = self.settings()
        try:
            if not ai.key_for(s, "local"):
                from ..intelligence import llama_engine

                return {"ready": False, "model": "", "why": "Your PC has no local AI: " + llama_engine.missing_backend()}
            return {"ready": True, "model": ai.local_model_name(s), "downloaded": ai.local_ready(s), "why": ""}
        except Exception as exc:  # noqa: BLE001
            return {"ready": False, "model": "", "why": f"Your PC's local AI can't be checked ({exc})."}

    def start(self, device: str, body: dict) -> dict:
        kind = str(body.get("kind") or "")
        if kind not in ("ask", "find"):
            raise HostError(400, "Ask (kind ask) or find clips (kind find).")
        st = self.status(fresh=True)
        if not st["ready"]:
            raise HostError(409, st["why"])
        now = self.now()
        with self.lock:
            self._prune(now)
            if sum(1 for t in self.tasks.values() if t.state in ("waiting", "working")) >= AI_MAX:
                raise HostError(429, "Your PC's local AI is busy with other questions. Try again in a minute.")
            task = AITask(kind, device, body, now)
            self.tasks[task.id] = task
        threading.Thread(target=self._run, args=(task,), name=f"mac-ai-{task.id}", daemon=True).start()
        return task.json()

    def get(self, task_id: str, device: str, wait: float = 0.0, rev: int | None = None) -> dict:
        """How the question is getting on. With `wait`, a long poll: it answers when there's something the Mac
        hasn't seen (a change after `rev`, the revision it last had; without one, the next change), or after
        `wait` seconds."""
        end = self.now() + max(0.0, min(25.0, wait))
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task.device != device:
                raise HostError(404, "That question isn't on your PC any more.")
            seen = task.rev if rev is None else rev
            task.asked = self.now()
            while task.state in ("waiting", "working") and task.rev == seen:
                left = end - self.now()
                if left <= 0:
                    break
                self.changed.wait(min(left, 1.0))
                task.asked = self.now()
            return task.json()

    def cancel(self, task_id: str, device: str) -> dict:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None or task.device != device:
                raise HostError(404, "That question isn't on your PC any more.")
            task.stop.set()
            return task.json()

    def stop_all(self) -> None:
        with self.lock:
            for t in self.tasks.values():
                t.stop.set()

    def busy(self) -> bool:
        with self.lock:
            return any(t.state == "working" for t in self.tasks.values())

    def _prune(self, now: float) -> None:
        for tid in [t.id for t in self.tasks.values() if t.finished and now - t.finished > AI_KEEP]:
            del self.tasks[tid]

    def _set(self, task: AITask, **changes) -> None:
        with self.lock:
            for k, v in changes.items():
                setattr(task, k, v)
            task.rev += 1
            self.changed.notify_all()

    def _run(self, task: AITask) -> None:
        def check() -> None:
            if task.stop.is_set() or self.now() - task.asked > AI_IDLE:
                raise Stopped()

        def progress(msg: str, frac=None) -> None:
            self._set(task, stage=_text(msg, 120) or task.stage, frac=frac)
            check()

        self._set(task, state="working", stage="Your PC's local AI is reading")
        s = self.settings()
        try:
            if task.kind == "ask":
                result = self._ask(s, task.payload, self.log, check, progress)
            else:
                result = self._find(s, task.payload, self.log, check, progress)
            self._set(task, state="done", result=result, stage="Done", frac=1.0, finished=self.now())
        except Stopped:
            self._set(task, state="stopped", error="Stopped.", finished=self.now())
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the Mac in words
            from .. import ai
            from ..ffmpeg import Cancelled

            if isinstance(exc, Cancelled):
                self._set(task, state="stopped", error="Stopped.", finished=self.now())
                return
            kind = "cut_off" if isinstance(exc, ai.AICutOff) else "refused" if isinstance(exc, ai.AIRefused) else ""
            self._set(task, state="failed", error=str(exc) or type(exc).__name__, finished=self.now(),
                      result={"kind": kind} if kind else None)


def _ask_local(s, body: dict, log, check, progress) -> dict:
    """ask_json on this PC's local AI only."""
    from .. import ai

    content = body.get("content")
    if not isinstance(content, (str, list)):
        raise HostError(400, "No question was sent.")
    schema = body.get("schema")
    if not isinstance(schema, dict):
        raise HostError(400, "No answer shape (schema) was sent.")
    task = str(body.get("task") or "find")
    answer = ai.ask_json(s, task if task in ai.MODEL_FIELDS else "find", str(body.get("system") or ""),
                         ai.text_only(content), schema, effort=str(body.get("effort") or "medium"),
                         max_tokens=max(256, min(32000, int(body.get("max_tokens") or 8000))), log=log, check=check,
                         only=["local"], progress=progress)
    return {"data": answer.data, "model": answer.model}


def _find_local(s, body: dict, log, check, progress) -> dict:
    """The local clip finder (transcript_parser), as automation uses it, on the Mac's transcript."""
    import numpy as np

    from .. import ai
    from ..intelligence import llama_engine, transcript_parser
    from ..subtitles import Cue

    try:
        sents = [Cue(float(a), float(b), str(t)) for a, b, t in body.get("sentences") or []]
        score = np.asarray([float(x) for x in body.get("score") or []], dtype=float)
        duration = float(body.get("duration") or 0)
        count = max(1, min(50, int(body.get("count") or 5)))
        lo, hi = float(body.get("min_len") or 15), float(body.get("max_len") or 60)
    except (TypeError, ValueError) as exc:
        raise HostError(400, f"The transcript sent couldn't be read ({exc}).") from exc
    if not sents:
        raise HostError(400, "No transcript was sent.")
    try:
        clips, model = transcript_parser.find_clips(_text(body.get("title"), 300), duration, sents, score, count, lo,
                                                    hi, str(body.get("instructions") or ""), s, log, check)
    except llama_engine.LocalAIError as exc:
        raise ai.AIError(str(exc)) from exc
    return {"clips": [asdict(c) for c in clips], "model": model}


# ---------------------------------------------------------------- whole videos


class MacJobs:
    """Videos Macs asked this PC to make clips from, made one at a time on a thread of their own."""

    def __init__(self, settings: Callable, log: Callable[[str], None], events: Callable | None = None,
                 root: Path | None = None, now: Callable[[], float] = time.time, engine=None,
                 linked: Callable | None = None):
        from ..settings import app_dir

        self.settings, self.log, self.now = settings, log, now
        self.events = events or (lambda kind, text, item="", device="": None)
        self.linked = linked or (lambda: None)  # the linked Macs' ids; None: can't be told now (every video stays)
        self._incoming: dict[str, int] = {}  # upload id -> bytes of it still to come (room is kept for them)
        self.root = root or app_dir() / "mac_jobs"
        self._engine = engine  # tests: (settings, log, progress, cancelled) -> an Engine
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._cancel: set[str] = set()
        self._forget: set[str] = set()  # videos the Mac let go of while they were being made: they go once stopped
        self.working: str = ""

    # --- the list -----------------------------------------------------------------------------------------

    def _index(self) -> Path:
        return self.root / "jobs.json"

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self._index().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [j for j in data.get("jobs") or [] if isinstance(j, dict) and j.get("id")] if isinstance(data, dict) else []

    def _save(self, jobs: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._index().with_name("jobs.json.tmp")
        tmp.write_text(json.dumps({"version": 1, "jobs": jobs}, indent=1), encoding="utf-8")
        for attempt in range(20):
            try:
                tmp.replace(self._index())
                return
            except PermissionError:  # Windows: jobs.json is being read this instant
                if attempt == 19:
                    raise
                time.sleep(0.05)

    def _change(self, job_id: str, **changes) -> dict | None:
        with self.lock:
            jobs = self._load()
            for j in jobs:
                if j["id"] == job_id:
                    j.update(changes, updated=self.now())
                    self._save(jobs)
                    return dict(j)
        return None

    def _public(self, j: dict) -> dict:
        keep = ("id", "source", "name", "title", "channel", "status", "stage", "progress", "created", "updated",
                "error", "clips", "rejected", "files", "folder", "waiting")
        return {k: j.get(k) for k in keep}

    def jobs_for(self, device: str) -> dict:
        with self.lock:
            everything = self._load()
        jobs = [j for j in everything if j.get("device") == device]
        queued = [j["id"] for j in sorted(everything, key=lambda j: j.get("created") or 0)
                  if j.get("status") == "queued"]
        out = []
        for j in sorted(jobs, key=lambda j: -(j.get("created") or 0)):
            d = self._public(j)
            if j.get("status") == "queued":
                d["waiting"] = queued.index(j["id"]) if j["id"] in queued else 0
            out.append(d)
        return {"jobs": out, "busy": self.working}

    def job(self, job_id: str, device: str) -> dict:
        j = self._find(job_id, device)
        return self._public(j)

    def _find(self, job_id: str, device: str) -> dict:
        with self.lock:
            for j in self._load():
                if j["id"] == job_id and j.get("device") == device:
                    return j
        raise HostError(404, "That video isn't on your PC any more.")

    def busy(self) -> bool:
        return bool(self.working)

    def room(self) -> int:
        """The biggest video a Mac may send now: what the disk has free, less the videos still arriving and
        room for its clips."""
        try:
            d = self.root if self.root.exists() else self.root.parent
            return max(0, min(UPLOAD_MAX, self._free(d) - FREE_AFTER_UPLOAD))
        except OSError:
            return 0

    def _free(self, d: Path) -> int:
        """What the disk has free once every video on its way here has arrived."""
        with self.lock:
            return shutil.disk_usage(d).free - sum(self._incoming.values())

    # --- what the Mac asks for ------------------------------------------------------------------------------

    def add(self, device: str, device_name: str, body: dict) -> dict:
        from ..download import is_url

        source = str(body.get("source") or "").strip()
        upload = str(body.get("upload") or "").strip()
        if upload:
            if not re.fullmatch(r"[0-9a-f]{16}", upload) or self._upload_file(upload) is None:
                raise HostError(404, "The video sent from your Mac isn't on the PC any more. Send it again.")
            name = _text(body.get("name") or self._upload_file(upload).name, 200)
        elif is_url(source) and len(source) <= 2000:
            name = source
        else:
            raise HostError(400, "Send a YouTube link, or a video from your Mac.")
        now = self.now()
        with self.lock:
            jobs = self._unlinked(self._prune(self._load(), now))
            if sum(1 for j in jobs if j.get("status") in ("queued", "working")) >= JOBS_MAX:
                if upload and not any(j.get("upload") == upload for j in jobs):
                    self._drop_upload({"upload": upload})  # the Mac sends it again when there's room
                raise HostError(429, f"Your PC already has {JOBS_MAX} videos to make. Wait for some to finish.")
            job = {"id": secrets.token_hex(8), "device": device, "device_name": _text(device_name, 80),
                   "source": "" if upload else source, "upload": upload, "name": name,
                   "title": _text(body.get("title") or "", 200), "channel": "",
                   "settings": clean_settings(body.get("settings")), "status": "queued", "stage": "Waiting",
                   "progress": None, "created": now, "updated": now, "error": "", "clips": 0, "rejected": 0,
                   "files": [], "folder": ""}
            jobs.append(job)
            self._save(jobs)
        self.log(f"{job['device_name'] or 'A Mac'} sent a video to make: {job['title'] or name}")
        self.wake.set()
        return self._public(job)

    def cancel(self, job_id: str, device: str) -> dict:
        j = self._find(job_id, device)
        if j.get("status") == "queued":
            self._change(job_id, status="cancelled", stage="Stopped", error="You stopped it.")
            self._drop_upload(j)
        elif j.get("status") == "working":
            self._cancel.add(job_id)
            self._change(job_id, stage="Stopping…")
        return self.job(job_id, device)

    def done(self, job_id: str, device: str) -> dict:
        """The Mac has the clips, or doesn't want the video any more: it goes (stopped first, if it's being made)."""
        j = self._find(job_id, device)
        if j.get("status") == "working":
            self._forget.add(job_id)
            self._cancel.add(job_id)
            self._change(job_id, stage="Stopping…")
            return {"message": "Stopping it: it goes once it has."}
        self._let_go(j)
        return {"message": "Done."}

    def _let_go(self, j: dict) -> None:
        with self.lock:
            self._save([x for x in self._load() if x["id"] != j["id"]])
        self._remove_files(j)
        self._forget.discard(j["id"])

    def file(self, job_id: str, device: str, rel: str) -> Path:
        """One of a made video's files, by its path in the list the Mac was given (relative to its clips)."""
        j = self._find(job_id, device)
        names = {f.get("path") for f in j.get("files") or []}
        if not rel or rel not in names:
            raise HostError(404, "That file isn't one of the clips.")
        base = (self._out(job_id) / "out").resolve()
        p = (base / rel).resolve()
        if not p.is_relative_to(base) or not p.is_file():
            raise HostError(404, "That file is gone from the PC.")
        return p

    # --- uploads -----------------------------------------------------------------------------------------------

    def _uploads(self) -> Path:
        return self.root / "uploads"

    def _upload_file(self, upload_id: str) -> Path | None:
        """The video a Mac sent, in a folder of its own under its own name (which becomes its title)."""
        if not re.fullmatch(r"[0-9a-f]{16}", str(upload_id or "")):
            return None
        try:
            return next((f for f in (self._uploads() / upload_id).iterdir()
                         if f.is_file() and not f.name.endswith(".part")), None)
        except OSError:
            return None

    def check_upload(self, name: str, size: int, hold: str = "") -> str:
        """Whether a video `size` bytes big called `name` may come (raises HostError if not); its name here.
        With `hold` (the id it arrives under), its room is kept from then on: receive() gives it back."""
        safe = _file_name(name)
        if Path(safe).suffix.lower() not in VIDEO_EXTS:
            raise HostError(400, "That isn't a video ClipMonster can open.")
        if size <= 0:
            raise HostError(411, "Say how big the video is (Content-Length).")
        if size > UPLOAD_MAX:
            raise HostError(413, "That video is too big to send.")
        d = self._uploads()
        d.mkdir(parents=True, exist_ok=True)
        with self.lock:  # one at a time, so two videos can't both take the same room
            self._prune_uploads()  # first: what a stray upload holds may be the room this one needs
            if self._free(d) < size + FREE_AFTER_UPLOAD:
                raise HostError(507, "Your PC hasn't room for that video.")
            if hold:
                self._incoming[hold] = size
        return safe

    def receive(self, name: str, size: int, read: Callable[[int], bytes]) -> dict:
        """A video from the Mac, `size` bytes read with `read`. Returns its upload id."""
        upload_id = secrets.token_hex(8)
        safe = self.check_upload(name, size, hold=upload_id)
        folder = self._uploads() / upload_id
        got = 0
        try:
            folder.mkdir()
            with open(folder / f"{safe}.part", "wb") as f:
                while got < size:
                    chunk = read(min(1024 * 1024, size - got))
                    if not chunk:
                        raise HostError(400, "The video stopped arriving part way.")
                    f.write(chunk)
                    got += len(chunk)
                    self._incoming[upload_id] = size - got  # what's written is no longer free on the disk
            (folder / f"{safe}.part").replace(folder / safe)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            with self.lock:
                self._incoming.pop(upload_id, None)
        return {"upload": upload_id, "size": got}

    def _prune_uploads(self) -> None:
        """Uploads no waiting video is made from go after UPLOAD_KEEP (a refused video's, or half of one)."""
        with self.lock:
            used = {j.get("upload") for j in self._load() if j.get("status") in ("queued", "working")}
            used |= set(self._incoming)
        try:
            for f in self._uploads().iterdir():
                if f.name not in used and self.now() - f.stat().st_mtime > UPLOAD_KEEP:
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
        except OSError:
            pass

    def _drop_upload(self, j: dict) -> None:
        if j.get("upload") and re.fullmatch(r"[0-9a-f]{16}", str(j["upload"])):
            shutil.rmtree(self._uploads() / j["upload"], ignore_errors=True)

    # --- tidying -----------------------------------------------------------------------------------------------

    def _out(self, job_id: str) -> Path:
        return self.root / job_id

    def _remove_files(self, j: dict) -> None:
        shutil.rmtree(self._out(j["id"]), ignore_errors=True)
        self._drop_upload(j)

    def _prune(self, jobs: list[dict], now: float) -> list[dict]:
        keep, gone = [], []
        for j in jobs:
            age = now - float(j.get("updated") or j.get("created") or 0)
            if (j.get("status") == "done" and age > KEEP_DAYS * DAY) or \
                    (j.get("status") in ("failed", "cancelled") and age > GONE_DAYS * DAY):
                gone.append(j)
            else:
                keep.append(j)
        for j in gone:
            self._remove_files(j)
        if gone:
            self._save(keep)
        return keep

    def _unlinked(self, jobs: list[dict]) -> list[dict]:
        """`jobs` without the videos of Macs that aren't linked any more: nobody could fetch, stop or remove
        them. One being made stays: it stops itself (make) and goes then."""
        keep = [j for j in jobs if j.get("status") == "working" or not self._gone(j)]
        if len(keep) != len(jobs):
            self._save(keep)
            for j in jobs:
                if j not in keep:
                    self._remove_files(j)
                    self.log(f"{j.get('device_name') or 'A Mac'} isn't linked any more, so its video was dropped: "
                             f"{j.get('title') or j.get('name')}")
        return keep

    def _gone(self, job: dict) -> bool:
        """Whether the Mac that sent this video isn't linked any more."""
        try:
            linked = self.linked()
        except Exception:  # noqa: BLE001 - can't be told now: the video stays
            return False
        return linked is not None and job.get("device") not in linked

    # --- making ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.stopping.clear()
        with self.lock:  # a video being made when automation stopped starts again
            jobs = self._load()
            again = [j for j in jobs if j.get("status") == "working"]
            for j in again:
                j.update(status="queued", stage="Waiting (starting again)", progress=None)
            if again:
                self._save(jobs)
        self._thread = threading.Thread(target=self._loop, name="mac-jobs", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.stopping.set()
        self.wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _next(self) -> dict | None:
        with self.lock:
            jobs = self._unlinked(self._prune(self._load(), self.now()))
            self._prune_uploads()
            waiting = sorted((j for j in jobs if j.get("status") == "queued"), key=lambda j: j.get("created") or 0)
            return waiting[0] if waiting else None

    def _loop(self) -> None:
        while not self.stopping.is_set():
            try:
                job = self._next()
            except Exception:  # noqa: BLE001 - the list couldn't be read or saved just now: look again shortly
                self.stopping.wait(LOOP_RETRY)
                continue
            if job is None:
                self.wake.wait(30.0)
                self.wake.clear()
                continue
            try:
                self.make(job)
            except Exception as exc:  # noqa: BLE001 - one video never stops the others
                try:
                    self._change(job["id"], status="failed", stage="Failed", error=f"{type(exc).__name__}: {exc}",
                                 progress=None)
                except Exception:  # noqa: BLE001 - nor does a list that can't be saved: it's tried again
                    self.stopping.wait(LOOP_RETRY)
            finally:
                self.working = ""

    def _engine_for(self, s, log, progress, cancelled):
        if self._engine is not None:
            return self._engine(s, log=log, progress=progress, cancelled=cancelled)
        from ..engine import Engine

        return Engine(s, log=log, progress=progress, cancelled=cancelled)

    def settings_for(self, job: dict):
        """This PC's settings with the Mac's clip settings on top, and its clips made in the job's folder. The
        AI is this PC's local AI and no other: a Mac never spends this PC's Claude or Gemini key. Until the
        local AI is ready (installed, its model downloaded), clips are found by the smart signals and aren't
        polished."""
        from .. import ai

        s = self.settings()
        cache = str(s.cache_path)  # before the output folder changes (the cache may live in it)
        s = s.apply(clean_settings(job.get("settings"), s))
        s = s.copy(output_dir=str(self._out(job["id"]) / "out"), cache_dir=cache, keep_source=False,
                   auto_export=True)
        s = s.copy(ai_provider="local", ai_fallback=False)
        try:
            ready = ai.local_ready(s)
        except Exception:  # noqa: BLE001 - a local AI that can't be checked isn't asked
            ready = False
        if not ready:
            s = s.copy(mode="auto" if s.mode == "ai" else s.mode, ai_polish=False)
        return s

    def make(self, job: dict) -> None:
        from .. import steps
        from ..engine import Project
        from ..ffmpeg import Cancelled

        job_id = job["id"]
        self.working = job_id
        s = self.settings_for(job)
        source = job.get("source") or ""
        if job.get("upload"):
            f = self._upload_file(job["upload"])
            if f is None:
                self._change(job_id, status="failed", stage="Failed", error="The video sent from the Mac is gone.")
                return
            source = str(f)
        tracker = steps.Tracker(steps.plan_for(s, url=not job.get("upload")))
        last = {"t": 0.0}
        who = job.get("device_name") or "your Mac"

        def progress(msg: str, frac=None) -> None:
            status = tracker.update(msg, frac)
            t = self.now()
            if t - last["t"] >= 1.5:
                last["t"] = t
                try:
                    self._change(job_id, stage=_text(msg, 120) + (f" {frac:.0%}" if frac is not None else ""),
                                 progress=status.to_json())
                except OSError:  # only what the Mac shows meanwhile: never worth failing the video for
                    pass

        looked = {"t": time.monotonic()}

        def cancelled() -> bool:
            if time.monotonic() - looked["t"] >= LINK_CHECK:
                looked["t"] = time.monotonic()
                if self._gone(job):  # its Mac was unlinked: nobody could fetch the clips
                    self._forget.add(job_id)
                    self._cancel.add(job_id)
            return job_id in self._cancel or self.stopping.is_set()

        self._change(job_id, status="working", stage="Starting", error="", progress=None)
        self.log(f"Making clips for {who}: {job.get('title') or job.get('name')}")
        engine = self._engine_for(s, self.log, progress, cancelled)
        p = Project(source)
        p.meta = {"made_for": {"mac": who, "on": "pc"}}
        from ..pipeline import Watchdog

        last["t"] = self.now()
        watch = Watchdog(float(getattr(s, "watchdog_minutes", 0) or 0) * 60, lambda: last["t"], lambda: "making clips",
                         self.now)
        watch.start()
        try:
            engine.prepare(p)
            if p.video is not None:  # video_id: what its download is cached under, for Clean up to leave it
                self._change(job_id, title=_text(p.video.title, 200), channel=_text(p.video.channel, 120),
                             video_id=_text(getattr(p.video, "id", ""), 200))
            if not [c for c in p.clips if c.enabled]:
                raise RuntimeError("No clips found in this video.")
            folder = engine.export(p)
        except Cancelled:
            self._cancel.discard(job_id)
            if job_id in self._forget:  # the Mac doesn't want it any more
                self._let_go(job)
            elif self.stopping.is_set():  # automation stopping: it starts again next time
                self._change(job_id, status="queued", stage="Waiting (starting again)", progress=None)
            else:
                self._change(job_id, status="cancelled", stage="Stopped", error="You stopped it.", progress=None)
                self._remove_files({"id": job_id, "upload": job.get("upload")})
            return
        except Exception as exc:  # noqa: BLE001
            if job_id in self._forget:
                self._let_go(job)
                return
            error = watch.reason() if watch.fired else str(exc) or type(exc).__name__
            self._change(job_id, status="failed", stage="Failed", error=error, progress=None)
            self.log(f"✗ Couldn't make clips for {who}: {error}")
            self.events("studio", f"✗ {job.get('title') or job.get('name')}: {error}", job_id,
                        job.get("device") or "")
            self._drop_upload(job)
            return
        finally:
            watch.stop()
        if job_id in self._forget:  # let go of as it finished
            self._cancel.discard(job_id)
            self._let_go(job)
            return
        out = self._out(job_id) / "out"
        files = []
        for f in sorted(Path(folder).rglob("*")):
            rel = f.relative_to(out).as_posix()
            if f.is_file() and not any(part.startswith((".", "_rejected")) for part in f.relative_to(out).parts):
                files.append({"path": rel, "size": f.stat().st_size})
        made = len([c for c in p.clips if c.enabled])
        rejected = p.qc.get("rejected", 0) if p.qc else 0
        title = p.video.title if p.video else job.get("title") or job.get("name")
        self._change(job_id, status="done", stage="Done", progress=None, files=files, clips=made - rejected,
                     rejected=rejected, folder=Path(folder).relative_to(out).as_posix(), title=_text(title, 200))
        self._drop_upload(job)
        self.log(f"✓ {made - rejected} clips for {who} from {title}; they go to the Mac now.")
        self.events("studio", f"✓ {made - rejected} clips from {title} are ready for your Mac", job_id,
                    job.get("device") or "")
