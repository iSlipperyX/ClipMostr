"""What the Mac app can see and do on this PC, as plain data (the server turns it into JSON).

Each method answers one request from the Mac. It reads and changes the same things the window and the
Telegram bot do: automation's jobs.db (runner.db), posting's library (the posting worker's own Store while
automation runs, so a change is seen at once), Settings, requests (orders.py) and the Video Creator's queue.
HostError carries what to tell the Mac when something can't be done, with an HTTP status.
"""

from __future__ import annotations

import collections
import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .. import __version__

GB = 1024 ** 3
EVENTS_KEPT = 300
LOG_LINES_MAX = 2000
EDITABLE = ("queued", "new", "failed")  # a video's caption can change until it posts


class HostError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def pc_name() -> str:
    try:
        return socket.gethostname() or "this PC"
    except OSError:
        return "this PC"


def _trim(text: str, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip(" ,.;:-") + "…"


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _mtime(path: str) -> int:
    try:
        return int(Path(path).stat().st_mtime) if path else 0
    except OSError:
        return 0


def rev(data) -> str:
    """A short hash of `data`, so the Mac can ask "has it changed?" and skip a list that hasn't."""
    return hashlib.sha1(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


class Host:
    def __init__(self, runner, now: Callable[[], float] = time.time, posting=None):
        self.runner = runner
        self.now = now
        self._posting = posting  # (store, accounts, service) for tests
        self._own = None
        self._desk = None  # the Sports section (desk), made on first use
        self.lock = threading.RLock()  # one change at a time
        self.events: collections.deque = collections.deque(maxlen=EVENTS_KEPT)
        self.seq = 0
        self._ev_lock = threading.Lock()
        # What a Mac can hand this PC (studio.py): questions for the local AI, and whole videos to make
        from .studio import AITasks, MacJobs

        self.ai_tasks = AITasks(self.settings, self.log, now)
        self.studio = MacJobs(self.settings, self.log, lambda kind, text, item="", device="": self.add_event(
            kind, text, item=item, device=device), now=now)

    # --- what the posting worker and automation say -------------------------------------------------------

    def on_event(self, account, kind: str, text: str, alert: bool = False, item=None) -> None:
        """posting/service.py's listener: posts going up, problems, milestones."""
        self.add_event(kind, text, alert=alert, item=getattr(item, "id", "") or "",
                       account=getattr(account, "handle", "") if account is not None else "")

    def problem(self, text: str) -> None:
        """automation's tell(): something that needs you."""
        self.add_event("problem", text, alert=True)

    def add_event(self, kind: str, text: str, alert: bool = False, item: str = "", account: str = "",
                  device: str = "") -> None:
        """`device`: the one linked Mac this is news for (a video it sent, say); "" is news for them all."""
        with self._ev_lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "at": self.now(), "kind": kind, "text": str(text or ""),
                                "alert": bool(alert), "item": item, "account": account, "device": device})

    def events_after(self, after: int, device: str | None = None) -> dict:
        """The news since `after`, for the Mac `device` (None: all of it). seq counts every event, so a Mac's
        place moves past the ones it isn't told."""
        with self._ev_lock:
            return {"seq": self.seq, "events": [e for e in self.events if e["seq"] > after and (
                device is None or not e.get("device") or e["device"] == device)]}

    # --- shared pieces ------------------------------------------------------------------------------------

    def settings(self):
        return self.runner.load()

    def posting(self):
        """(store, accounts, service or None), refreshed from disk."""
        if self._posting is not None:
            store, accounts, service = self._posting
        else:
            poster = getattr(self.runner, "poster", None)
            service = getattr(poster, "service", None)
            if service is not None:
                store, accounts = service.store, service.accounts
            else:
                if self._own is None:
                    from ..posting.accounts import Accounts
                    from ..posting.store import Store

                    self._own = (Store(), Accounts.load())
                store, accounts = self._own
        try:
            store.refresh()
            accounts.refresh()
        except (OSError, TimeoutError, AttributeError):
            pass
        return store, accounts, service

    def _save(self, store) -> None:
        try:
            store.save()
        except (OSError, TimeoutError) as exc:
            raise HostError(503, f"The posting library couldn't be saved: {exc}") from exc

    def _item(self, item_id: str):
        store, accounts, svc = self.posting()
        it = store.get(item_id)
        if it is None:
            raise HostError(404, "That video isn't in the library any more.")
        return it, store, accounts, svc

    def _categories(self, s=None) -> list:
        from .. import categories

        return categories.load(s or self.settings())

    def _category(self, category_id: str, s=None):
        if not category_id:
            return None
        from .. import categories

        c = categories.find(s or self.settings(), category_id)
        if c is None:
            raise HostError(400, "That category isn't there any more.")
        return c

    def log(self, msg: str) -> None:
        try:
            self.runner.log(f"Mac app: {msg}")
        except Exception:  # noqa: BLE001
            pass

    # --- hello ---------------------------------------------------------------------------------------------

    def hello(self) -> dict:
        from . import protocol

        return {"app": protocol.APP, "api": protocol.API_VERSION, "name": pc_name(), "version": __version__,
                "lends": ["ai", "studio"]}  # what a Mac may hand it (studio.py)

    # --- status -----------------------------------------------------------------------------------------------

    def status(self) -> dict:
        from .. import pipeline
        from ..posting import jobs as pjobs

        db = self.runner.db
        s = self.settings()
        now = self.now()
        out: dict = {"host": {**self.hello(), "time": now, "utc_offset": time.localtime(now).tm_gmtoff},
                     "event_seq": self.seq, "problems": []}
        paused = db.meta("paused") == "1"
        activity = None
        try:
            a = json.loads(db.meta("activity") or "null")
            if isinstance(a, dict) and a.get("text") and now - float(a.get("time") or 0) < 300:
                activity = {"text": str(a["text"]), "frac": a.get("frac")}
        except ValueError:
            pass
        out["automation"] = {"running": True,  # the server runs inside automation
                             "paused": paused, "state": db.meta("state"), "activity": activity,
                             "youtube_blocked_until": pipeline.paused_until(db, now)}
        job = db.working()
        if job is not None:
            bar = job.status_bar
            out["job"] = {"id": job.id, "title": job.title or job.url, "making": job.is_creation, "stage": job.stage,
                          "category": job.category_id, "status": bar.to_json() if bar else None}
        else:
            out["job"] = None
        jobs = db.jobs(limit=3000)
        waiting = [j for j in jobs if j.status == "queued"]
        made = sum(1 for j in waiting if j.is_creation)
        names = {c.id: c.name for c in self._categories(s)}
        upcoming = sorted(waiting, key=lambda j: (j.next_try, j.created))[:6]
        out["queue"] = {"to_clip": len(waiting) - made, "to_make": made,
                        "next": [{"id": j.id, "title": j.title or j.url, "making": j.is_creation,
                                  "category": names.get(j.category_id, ""), "stage": j.stage,
                                  "next_try": j.next_try} for j in upcoming]}
        recent = [j for j in jobs if j.status in ("done", "failed") and now - (j.updated or 0) < 86400]
        out["today"] = {"clipped": sum(1 for j in recent if j.status == "done" and not j.is_creation),
                        "made": sum(1 for j in recent if j.status == "done" and j.is_creation),
                        "clips": sum(j.clips for j in recent if j.status == "done"),
                        "failed": sum(1 for j in recent if j.status == "failed")}
        out["requests_open"] = len(db.orders(open_only=True))
        try:
            store, accounts, svc = self.posting()
            queued = [i for i in store.queue() if i.status == "queued"]
            review = [i for i in store.waiting_review() if i.status == "queued"]
            midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
            nxt = pjobs.next_post(store)
            posting = {"running": bool(svc is not None and getattr(svc, "posting", True)),
                       "queued": len(queued), "review": len(review), "today": store.posts_since(midnight),
                       "next": None}
            if nxt is not None:
                a = accounts.get(nxt.account)
                posting["next"] = {"id": nxt.id, "headline": nxt.headline, "at": nxt.planned_at,
                                   "account": a.handle if a else ""}
            out["posting"] = posting
            acc = []
            for a in accounts:
                mine = [i for i in queued if i.account == a.id]
                n = pjobs.next_post(store, a.id)
                acc.append({"id": a.id, "handle": a.handle, "logged_in": a.logged_in, "autopilot": a.autopilot,
                            "queued": len(mine), "per_day": a.posts_per_day, "next_at": n.planned_at if n else None,
                            "session": a.session_check})
                if a.autopilot and not a.logged_in:
                    out["problems"].append(f"{a.handle} isn't signed in to TikTok (Accounts tab on the PC).")
            out["accounts"] = acc
        except Exception as exc:  # noqa: BLE001 - the rest of the status still helps
            out["posting"] = None
            out["accounts"] = []
            out["problems"].append(f"Posting's library couldn't be read ({exc}).")
        try:
            free, drive = self.runner.free_space(s)
            if free < (1 << 60):
                low = bool(s.disk_min_free_gb and free < s.disk_min_free_gb * GB)
                out["disk"] = {"free": free, "drive": drive, "low": low}
                if low:
                    out["problems"].append(f"Only {free / GB:.0f} GB free on {drive}.")
        except Exception:  # noqa: BLE001
            out["disk"] = None
        if out["automation"]["youtube_blocked_until"]:
            out["problems"].append("YouTube asked this PC to sign in, so downloads wait (Automation tab on the PC).")
        out["videos_rev"] = self._videos_rev()
        try:  # what a Mac can hand this PC, and what it's making for Macs now
            out["lend"] = {"ai": self.ai_tasks.status(), "studio": True, "making": self.studio.working,
                           "ai_busy": self.ai_tasks.busy(), "room": self.studio.room()}
        except Exception as exc:  # noqa: BLE001
            out["lend"] = {"ai": {"ready": False, "model": "", "why": str(exc)}, "studio": True, "making": ""}
        return out

    # --- videos -------------------------------------------------------------------------------------------

    def _videos_list(self) -> list:
        """Held for your OK first, then the queue in posting order, then the library's clips not queued."""
        store, _accounts, _svc = self.posting()
        with store.lock:
            items = [i for i in store.items.values() if i.status in ("queued", "failed", "new", "uploading")
                     and (i.status != "new" or i.exists())]

        def order(i):
            if i.review == "waiting" and i.status == "queued":
                return (0, i.added_at, 0)
            if i.status in ("uploading", "failed"):
                return (1, -(i.upload_started_at or i.added_at), 0)
            if i.queue_pos is not None:
                return (2, i.planned_at or float("inf"), i.queue_pos)
            return (3, -i.added_at, 0)
        return sorted(items, key=order)

    def _videos_rev(self) -> str:
        try:
            store = self.posting()[0]
            with store.lock:
                sig = sorted((i.id, i.status, i.review, i.queue_pos, int(i.planned_at or 0), i.caption, i.hashtags,
                              i.account, i.thumb) for i in store.items.values())
            return rev(sig)
        except Exception:  # noqa: BLE001
            return ""

    def video_json(self, it, accounts, svc, now: float | None = None) -> dict:
        from ..posting.store import INTERRUPTED

        now = self.now() if now is None else now
        a = accounts.get(it.account) if it.account else None
        story = it.facts.get("creator") if isinstance(it.facts.get("creator"), dict) else {}
        if it.status == "posted":
            state = "posted"
        elif it.status == "uploading":
            state = "posting"
        elif it.status == "failed":
            state = "failed"
        elif it.review == "waiting" and it.status == "queued":
            state = "review"
        elif it.queue_pos is not None:
            state = "queued"
        else:
            state = "library"
        logged_in = bool(a is not None and a.logged_in)
        try:
            size = Path(it.path).stat().st_size if it.path else 0
        except OSError:
            size = 0
        return {
            "id": it.id, "headline": it.headline, "title": it.name, "state": state, "status": it.status,
            "review": it.review, "account": it.account, "account_handle": a.handle if a else "",
            "signed_in": logged_in, "category": it.category_name, "category_id": it.category_id,
            "part": it.part_label, "planned_at": it.planned_at, "pinned_at": it.pinned_at, "posted_at": it.posted_at,
            "added_at": it.added_at, "tiktok_url": it.tiktok_url, "duration": it.duration or float(
                it.facts.get("length") or 0), "score": it.total, "ai_generated": it.ai_generated,
            "caption": it.caption or it.hook, "hashtags": it.hashtags, "hook": it.hook,
            "warnings": list(it.warnings[:4]), "last_error": it.last_error,
            "interrupted": it.last_error == INTERRUPTED, "exists": size > 0, "size": size,
            "has_thumb": bool(it.thumb and Path(it.thumb).is_file()) or size > 0,
            "thumb_v": _mtime(it.thumb),
            "source_title": str(it.facts.get("source_title") or ""),
            "source_channel": str(it.facts.get("source_channel") or it.source_channel or ""),
            "source_url": it.source_url,
            "prompt": "your script" if story.get("written_by") == "your script" else str(story.get("prompt") or ""),
            "cover_why": it.cover_why,
            "can": {
                "ok": state == "review",
                "post_now": it.status == "queued" and logged_in and svc is not None,
                "retry": it.status == "failed" and it.last_error != INTERRUPTED,
                "delete": it.status in ("queued", "failed", "new"),
                "edit": it.status in EDITABLE,
            },
        }

    def videos(self, known_rev: str = "") -> dict:
        r = self._videos_rev()
        if known_rev and r and known_rev == r:
            return {"rev": r, "same": True}
        _store, accounts, svc = self.posting()
        now = self.now()
        items = [self.video_json(i, accounts, svc, now) for i in self._videos_list()]
        return {"rev": r, "same": False, "items": items}

    def video(self, item_id: str) -> dict:
        it, _store, accounts, svc = self._item(item_id)
        return self.video_json(it, accounts, svc)

    def posted(self, limit: int = 60) -> dict:
        store, accounts, svc = self.posting()
        items = sorted(store.posted(), key=lambda i: -(i.posted_at or 0))[:max(1, min(200, limit))]
        out = []
        now = self.now()
        for it in items:
            d = self.video_json(it, accounts, svc, now)
            v = store.video_for(it)
            d["stats"] = ({"views": v.views, "likes": v.likes, "comments": v.comments, "shares": v.shares,
                           "saves": v.saves} if v is not None else None)
            out.append(d)
        return {"items": out}

    def video_file(self, item_id: str) -> Path:
        it, *_ = self._item(item_id)
        p = Path(it.path) if it.path else None
        if p is None or not p.is_file():
            raise HostError(404, "Its file isn't on the PC any more.")
        return p

    def thumb_file(self, item_id: str) -> Path:
        it, store, *_ = self._item(item_id)
        if it.thumb and Path(it.thumb).is_file():
            return Path(it.thumb)
        from ..posting.config import thumbs_dir
        from ..posting.media import thumbnail

        dest = thumbs_dir() / f"{it.id}.jpg"
        if dest.is_file():
            return dest
        src = Path(it.path) if it.path else None
        if src is None or not src.is_file():
            raise HostError(404, "No picture for it.")
        try:
            thumbnail(src, dest, min(1.0, max(0.0, (it.duration or 3.0) / 3)))
        except Exception as exc:  # noqa: BLE001
            raise HostError(404, f"No picture for it ({exc}).") from exc
        return dest

    def ok(self, item_id: str) -> dict:
        with self.lock:
            it, store, accounts, svc = self._item(item_id)
            if not store.approve([it.id]):
                raise HostError(409, "It didn't need an OK.")
            self._save(store)
            self.log(f"OK'd “{_trim(it.headline, 80)}” to post.")
            return {"message": "OK: it posts when its time comes.", "video": self.video_json(it, accounts, svc)}

    def post_now(self, item_id: str) -> dict:
        with self.lock:
            it, store, accounts, svc = self._item(item_id)
            a = accounts.get(it.account) if it.account else None
            if a is None or not a.logged_in:
                raise HostError(409, "Sign its account in to TikTok first (Accounts tab on the PC).")
            if svc is None or not getattr(svc, "posting", True):
                raise HostError(409, "Posting isn't running on the PC.")
            if it.status != "queued":
                raise HostError(409, "Only a video in the queue can post now.")
            if store.approve([it.id]):  # posting it now is your OK
                self._save(store)
            svc.actions.submit("post_now", account=a.id, item=it.id)
            svc.wake.set()
            self.log(f"Asked to post “{_trim(it.headline, 80)}” to {a.handle} now.")
            return {"message": f"Posting it to {a.handle} now…", "video": self.video_json(it, accounts, svc)}

    def retry(self, item_id: str) -> dict:
        from ..posting.store import INTERRUPTED

        with self.lock:
            it, store, accounts, svc = self._item(item_id)
            if it.status != "failed":
                raise HostError(409, "It isn't waiting to be tried again.")
            if it.last_error == INTERRUPTED:
                raise HostError(409, "It may be on TikTok already: check the account, then retry it on the PC.")
            with store.lock:
                it.status, it.last_error = "queued", ""
            self._save(store)
            self.log(f"Queued “{_trim(it.headline, 80)}” again.")
            return {"message": "It's queued again.", "video": self.video_json(it, accounts, svc)}

    def delete(self, item_id: str) -> dict:
        from ..posting import intake, records
        from ..posting.config import Settings as PostingSettings

        with self.lock:
            it, store, *_ = self._item(item_id)
            missing = not it.exists()
            ps = PostingSettings.load()
            try:
                r = records.discard(store, it.id, ps, intake.roots(self.settings()))
            except ValueError as exc:
                raise HostError(409, str(exc)) from exc
            if not r.removed:
                why = r.failed[0][1] if r.failed else "the file couldn't be deleted"
                raise HostError(409, f"Couldn't delete it: {why}. Close anything that has it open on the PC, then "
                                     "try again.")
            self._save(store)
            try:
                from ..telegram import preview

                preview.drop(it.id)
            except Exception:  # noqa: BLE001
                pass
            freed = f" ({records.size_text(r.freed)} freed)" if r.freed else ""
            if missing:
                self.log(f"🗑 Took “{_trim(it.headline, 80)}” off the list (its file was gone already).")
                msg = "Taken off the list (its file was gone from the PC already)."
            else:
                self.log(f"🗑 Deleted “{_trim(it.headline, 80)}”{freed}.")
                msg = ("Moved to the PC's Recycle Bin" if ps.recycle else "Deleted from the PC") + f"{freed}."
            if r.failed:
                msg += f" {_plural(len(r.failed), 'file')} couldn't go: {_trim(r.failed[0][1], 100)}"
            return {"message": msg, "freed": r.freed}

    def edit(self, item_id: str, caption: str | None = None, hashtags: str | None = None) -> dict:
        with self.lock:
            it, store, accounts, svc = self._item(item_id)
            if it.status not in EDITABLE:
                raise HostError(409, "It can't be changed now: it's posting or posted.")
            with store.lock:
                if caption is not None:
                    it.caption = str(caption).strip()[:2200]
                if hashtags is not None:
                    it.hashtags = " ".join(str(hashtags).split())[:500]
            self._save(store)
            return {"message": "Saved.", "video": self.video_json(it, accounts, svc)}

    # --- automation ------------------------------------------------------------------------------------------

    def pause(self, paused: bool) -> dict:
        db = self.runner.db
        db.set_meta("paused", "1" if paused else "0")
        if not paused:
            db.set_meta("nudge", "1")
        self.log("Paused." if paused else "Resumed.")
        return {"paused": paused, "message": ("Paused. Nothing new is clipped or made until you resume (a video "
                                              "already being made finishes). Posting carries on.")
                if paused else "Carrying on."}

    def categories(self) -> dict:
        db = self.runner.db
        s = self.settings()
        waiting: dict[str, int] = {}
        for j in db.jobs(limit=3000):
            if j.status == "queued":
                waiting[j.category_id] = waiting.get(j.category_id, 0) + 1
        try:
            store, accounts, _svc = self.posting()
        except Exception:  # noqa: BLE001
            store, accounts = None, None
        out = []
        for c in self._categories(s):
            a = accounts.get(c.account_id) if accounts is not None and c.account_id else None
            queued = 0
            if store is not None and c.account_id:
                queued = sum(1 for i in store.queue(c.account_id) if i.status == "queued" and i.category_id == c.id)
            out.append({"id": c.id, "name": c.name, "account": a.handle if a else c.account, "paused": c.paused,
                        "enabled": c.enabled, "on": c.on, "state": c.state, "daily": c.daily,
                        "creator_daily": c.creator_daily, "waiting": waiting.get(c.id, 0), "queued": queued,
                        "min_len": c.min_len, "max_len": c.max_len})
        return {"items": out}

    def pause_category(self, category_id: str, paused: bool) -> dict:
        from .. import categories

        with self.lock:
            s = self.settings()
            c = self._category(category_id, s)
            if c is None:
                raise HostError(400, "Pick a category.")
            c.paused = bool(paused)
            s.categories = categories.to_settings([c if x.id == c.id else x for x in categories.load(s)])
            s.save()
            if not paused:
                self.runner.db.set_meta("nudge", "1")
            self.log(f"{'Paused' if paused else 'Resumed'} {c.name}.")
            return {"message": f"{c.name} is paused: nothing is picked or clipped for it until you resume it."
                    if paused else f"{c.name} carries on."}

    def scan(self, category_id: str = "") -> dict:
        from .. import pipeline
        from ..research import youtube_key

        s = self.settings()
        c = self._category(category_id, s)
        if not youtube_key(s):
            raise HostError(409, "Automation needs a YouTube Data API key to look for videos (Settings → Research on "
                                 "the PC).")
        pipeline.request_scan(self.runner.db, c.id if c else "")
        return {"message": f"Automation checks {c.name if c else 'every category'}'s sources at its next step."}

    def _held(self, cat) -> bool:
        from ..posting.intake import REVIEW_DAYS

        mode = getattr(self.settings(), "creator_review", "new")
        if mode in ("always", "off"):
            return mode == "always"
        if cat is None or not cat.account_id:
            return False
        store = self.posting()[0]
        with store.lock:
            first = min((i.posted_at or 0 for i in store.items.values() if i.account == cat.account_id
                         and i.ai_generated and i.status == "posted" and i.posted_at), default=0)
        return not first or self.now() - first < REVIEW_DAYS * 86400

    def when(self, cat=None, mine: int = 1) -> str:
        """When what was just queued (`mine` jobs) will start, roughly."""
        from ..pipeline import hours_wait

        s = self.settings()
        db = self.runner.db
        now = self.now()
        ahead = max(0, sum(1 for j in db.jobs(limit=3000) if j.status == "working"
                           or (j.status == "queued" and j.next_try <= now)) - mine)
        if cat is not None and cat.paused:
            return f"{cat.name} is paused, so it waits until you resume it."
        if cat is not None and cat.needs_account:
            return f"{cat.name} has no TikTok account yet, so it waits until you pick one (Categories tab on the PC)."
        if db.meta("paused") == "1":
            return "Automation is paused: it starts once you resume."
        if hours_wait(now, s.auto_start_hour, s.auto_end_hour):
            return f"It starts at {s.auto_start_hour}:00, when working hours begin."
        return "It's next." if not ahead else f"{_plural(ahead, 'video is', 'videos are')} ahead of it."

    def make(self, idea: str, category_id: str = "") -> dict:
        from ..creator.orchestrator import CreateRequest, queue as queue_creation

        idea = " ".join(str(idea or "").split())
        if not idea:
            raise HostError(400, "Say what the video should be about.")
        if len(idea) > 1000:
            raise HostError(400, "That idea is too long: keep it under 1,000 characters.")
        with self.lock:
            cat = self._category(category_id)
            req = CreateRequest(prompt=idea, category=cat.id if cat else "")
            job_id = queue_creation(self.runner.db, req, cat.id if cat else "", self.now(), origins=["creator", "mac"])
            self.runner.db.set_meta("nudge", "1")
            self.log(f"Asked for a video about “{_trim(idea, 80)}”" + (f" for {cat.name}" if cat else "") + ".")
            held = self._held(cat)
            return {"job": job_id, "held": held,
                    "message": f"A video about “{_trim(idea, 120)}”" + (f" for {cat.name}" if cat else "") + ". "
                               + self.when(cat) + (" It waits for your OK before it posts." if held else "")}

    def clip(self, links: list, category_id: str = "") -> dict:
        from ..download import is_url

        links = [str(u).strip().rstrip(").,>]") for u in (links or []) if str(u).strip()][:20]
        if not links:
            raise HostError(400, "Give a YouTube link.")
        with self.lock:
            cat = self._category(category_id)
            added, before, bad = [], [], []
            for url in links:
                if not is_url(url):
                    bad.append(url)
                    continue
                try:
                    (added if self.runner.add_manual(url, category_id=cat.id if cat else "") else before).append(url)
                except ValueError:
                    bad.append(url)
            if added:
                self.runner.db.set_meta("nudge", "1")
                self.log(f"Asked to clip {_plural(len(added), 'video')}" + (f" for {cat.name}" if cat else "") + ".")
            lines = []
            if added:
                lines.append(f"Queued {_plural(len(added), 'video')} to clip" + (f" for {cat.name}" if cat else "")
                             + f". {self.when(cat, len(added))}")
            if before:
                lines.append(f"{_plural(len(before), 'video was', 'videos were')} clipped before (or already "
                             "waiting), so not queued again. To clip one again, send a request with its link.")
            if bad:
                lines.append("These aren't video links: " + ", ".join(_trim(b, 60) for b in bad[:3]))
            return {"added": added, "before": before, "bad": bad, "message": " ".join(lines) or "Nothing to clip."}

    def requests(self) -> dict:
        from .. import categories, orders

        s = self.settings()
        out = []
        for o in self.runner.db.orders()[:40]:
            name = categories.name_of(s, o.category_id)
            out.append({"id": o.id, "title": o.title(name), "status": o.status,
                        "status_text": orders.STATUS_WORDS.get(o.status, o.status), "message": o.message,
                        "notes": o.notes, "count": o.count, "made": o.made, "min_len": o.min_len,
                        "max_len": o.max_len, "category": name, "category_id": o.category_id, "links": o.links,
                        "created": o.created, "updated": o.updated, "can_cancel": o.status == "open"})
        return {"items": out}

    def new_request(self, count: int, min_len: float | None = None, max_len: float | None = None,
                    category_id: str = "", links: list | None = None, notes: str = "") -> dict:
        from .. import orders, pipeline
        from ..jobs import Job
        from ..research import youtube_key

        s = self.settings()
        with self.lock:
            cat = self._category(category_id, s)
            links = [str(u).strip() for u in (links or []) if str(u).strip()][:20]
            if not min_len or not max_len:
                style = pipeline.settings_for(Job(id="", url="", category_id=cat.id if cat else ""), s)[0]
                min_len, max_len = style.min_len, style.max_len
            if not links and not youtube_key(s):
                raise HostError(409, "To pick videos by itself, automation needs a YouTube Data API key (Settings → "
                                     "Research on the PC). Add links to the request instead.")
            try:
                order = orders.make(int(count or 5), float(min_len), float(max_len), category_id=cat.id if cat else "",
                                    links=links, notes=str(notes or "")[:1000], now=self.now())
            except (TypeError, ValueError) as exc:
                raise HostError(400, str(exc)) from exc
            order = self.runner.place(order)
            notes_warn = bool(order.notes and not pipeline.has_ai_key(s))
            msg = f"Asked for {order.title(cat.name if cat else '')}."
            if notes_warn:
                msg += (" Special requests need an AI (Settings → AI on the PC): without one, smart signals pick the "
                        "clips.")
            if cat is not None and cat.paused:
                msg += f" {cat.name} is paused, so nothing happens until you resume it."
            return {"id": order.id, "message": msg}

    def cancel_request(self, order_id: str) -> dict:
        with self.lock:
            o = self.runner.db.get_order(order_id)
            if o is None or o.status != "open":
                raise HostError(409, "It isn't open any more.")
            n = self.runner.cancel(o.id)
            self.log(f"Cancelled a request ({o.title()}).")
            return {"message": f"Cancelled. {_plural(n, 'waiting video')} left the queue."}

    def week(self) -> dict:
        from .. import weekly

        store, accounts, _svc = self.posting()
        w = weekly.build(store, accounts, self.runner.db, self.now(), weekly.Record())
        return {"span": w.span(), "headline": w.headline(),
                "sections": [{"heading": h, "lines": [{"text": t, "link": link} for t, link in rows]}
                             for h, rows in w.sections()],
                "data": w.to_json()}

    def log_tail(self, lines: int = 300) -> dict:
        from ..pipeline import log_path

        n = max(1, min(LOG_LINES_MAX, int(lines or 300)))
        p = log_path()
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - n * 400))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return {"lines": []}
        out = text.splitlines()
        if size > n * 400 and out:
            out = out[1:]  # the first line was cut in half
        return {"lines": out[-n:]}

    # --- clips sent to the Macs (deliveries.py) ---------------------------------------------------------------

    def deliveries(self, device_id: str, known_rev: str = "") -> dict:
        """The clips waiting for this Mac, newest first, and which categories send them."""
        from . import deliveries

        items = deliveries.for_device(device_id)
        r = rev([(i["id"], i["got"], i["seen"]) for i in items])
        if known_rev and known_rev == r:
            return {"rev": r, "same": True}
        sending = []
        for c in self._categories():
            if c.send_to_macs:
                sport = c.sport_info
                sending.append({"id": c.id, "name": c.name, "sport": sport.label if sport else "",
                                "emoji": sport.emoji if sport else "", "on": c.on})
        return {"rev": r, "same": False, "items": items, "new": sum(1 for i in items if i["new"]),
                "sending": sending, "keep_days": deliveries.KEEP_DAYS}

    def _delivery(self, item_id: str) -> dict:
        from . import deliveries

        item = deliveries.find(item_id)
        if item is None:
            raise HostError(404, "That clip isn't on the PC any more (each one is kept a week).")
        return item

    def delivery_file(self, item_id: str) -> Path:
        from . import deliveries

        p = deliveries.file_of(self._delivery(item_id))
        if p is None:
            raise HostError(404, "That clip's file isn't on the PC any more.")
        return p

    def delivery_thumb(self, item_id: str) -> Path:
        from . import deliveries

        item = self._delivery(item_id)
        p = deliveries.file_of(item, "thumb")
        if p is not None:
            return p
        src = deliveries.file_of(item)
        if src is None:
            raise HostError(404, "No picture for it.")
        from ..posting.media import thumbnail

        dest = deliveries.folder() / f"{item['id']}.jpg"
        try:
            thumbnail(src, dest, min(1.5, max(0.0, float(item.get("length") or 3.0) / 3)))
        except Exception as exc:  # noqa: BLE001
            raise HostError(404, f"No picture for it ({exc}).") from exc
        with deliveries.edit() as data:
            for i in data["items"]:
                if i.get("id") == item["id"]:
                    i["thumb"] = dest.name
        return dest

    def delivery_mark(self, item_id: str, device_id: str, what: str) -> dict:
        from . import deliveries

        if what not in ("got", "seen", "dismissed"):
            raise HostError(404, "No such request.")
        if not deliveries.mark(item_id, device_id, what, self.now()):
            raise HostError(404, "That clip isn't on the PC any more.")
        if what == "dismissed":
            try:
                from . import state

                deliveries.prune([d["id"] for d in state.load()["devices"]], self.now())
            except (OSError, TimeoutError):
                pass
        return {"message": {"got": "Saved on this Mac.", "seen": "", "dismissed": "Removed from this Mac's inbox."}[what]}

    # --- the Sports section (sportsdesk/desk.py) ----------------------------------------------------------------

    @property
    def desk(self):
        """The Sports section's state, the same sports.db the PC's window shows (made on first use)."""
        if self._desk is None:
            from ..sportsdesk.desk import Desk

            self._desk = Desk(settings=self.settings, now=self.now)
        return self._desk

    def sports(self, known_rev: str = "") -> dict:
        """The Sports page: the filter, the games found, each one's ranked highlights, and what's being done."""
        return self._desk_call(lambda d: d.state(known_rev))

    def sports_do(self, action: str, body: dict, game_id: str = "", device_name: str = "") -> dict:
        """One thing asked of the Sports section, from a Mac."""
        body = body if isinstance(body, dict) else {}
        ids = body.get("ids") if isinstance(body.get("ids"), list) else []
        acts = {
            "filter": lambda d: d.set_filter(body.get("filter") if isinstance(body.get("filter"), dict) else body),
            "find": lambda d: d.find_games(body.get("filter") if isinstance(body.get("filter"), dict) else None),
            "pick": lambda d: d.pick(ids or [game_id], bool(body.get("picked", True))),
            "analyse": lambda d: d.analyse(ids or [game_id], bool(body.get("again"))),
            "choose": lambda d: d.choose(game_id, ids, bool(body.get("chosen", True))),
            "make": lambda d: d.make(game_id, ids or None, body.get("category") if "category" in body else None),
            "reel": lambda d: d.reel(game_id, ids or None, body.get("category") if "category" in body else None),
            "events": lambda d: d.find_events(str(body.get("query") or ""), ids or ([game_id] if game_id else None),
                                              str(body.get("feed") or ""),
                                              body.get("anchors") if isinstance(body.get("anchors"), list) else None),
            "version": lambda d: d.version(game_id, str(body.get("video") or "")),
            "remove": lambda d: d.remove(ids or [game_id]),
            "clear": lambda d: d.clear(),
            "cancel": lambda d: d.cancel(game_id or "search"),
        }
        if action not in acts:
            raise HostError(404, "No such request.")
        out = self._desk_call(acts[action])
        said = {"find": "Looking for games", "analyse": "Finding highlights", "make": "Making clips",
                "reel": "Making a reel", "events": "Searching for a moment"}.get(action)
        if said and isinstance(out, dict) and out.get("message"):
            self.log(f"{device_name or 'A Mac'}: {out['message']}")
        return out

    def sports_played(self, sport: str = "any", league: str = "") -> dict:
        """The games of the last 24 hours (the scoreboards), for a Mac's Sports page."""
        return self._desk_call(lambda d: d.played(sport or "any", league or ""))

    def sports_order_game(self, body: dict, device_name: str = "") -> dict:
        """A listed game's recap reel (or plays), asked for from a Mac."""
        body = body if isinstance(body, dict) else {}
        out = self._desk_call(lambda d: d.order_game(str(body.get("game") or ""), str(body.get("want") or "reel"),
                                                     by=device_name or "A Mac"))
        self.log(f"{device_name or 'A Mac'}: {out['message']}")
        return out

    def sports_thumb(self, game_id: str, highlight_id: str) -> Path:
        return self._desk_call(lambda d: d.thumb(game_id, highlight_id))

    def _desk_call(self, fn):
        from ..sportsdesk.desk import DeskError

        try:
            return fn(self.desk)
        except DeskError as exc:
            raise HostError(exc.status, exc.message) from exc
