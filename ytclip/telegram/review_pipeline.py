"""A video's card, and what happens to it: watch, OK, post now, delete, and say what's wrong with it.

**Cards.** Each video waiting to post can be sent into the chat with its card (what it is, when it posts) and
buttons. The card follows the video: OK'd in the window, posted, or deleted somewhere else, every card of it
is brought up to date (settle).

**Critique / Regenerate.** Under every video the Creator or the Studio made, 🛠 takes what's wrong with it in
plain words. studio/retouch.py reads it and the cheapest way that answers it is used:

* *retouch*: "make the captions yellow", "the music is too loud". The video's timeline.json is changed and
  only that is rendered again, as "<title> (v2).mp4" beside the first: no new script, voice or pictures.
* *voice*: "use the British voice". The same storyboard is voiced and rendered again; nothing is rewritten.
* *rewrite*: "the hook is flat, open with the twist". The AI router rewrites the storyboard to answer it
  (creator/critique.py) and the video is made again; caption and level changes said alongside ride along.

The new video is **v2 (Critique Applied)**. Both versions are held and neither posts until one is picked:
✅ Approve v1 or ✅ Approve v2 queues that one to post and deletes the other from the PC (records.discard, the
way Delete does). A critique of v2 makes a v3, and so on; approving one deletes all the others.

**Sending a video.** streamer.py makes the mobile preview when the master is too big for Telegram, and the
one progress message says so while it's made and uploaded; the master never leaves its posting folder.
"""

from __future__ import annotations

import html
import queue
import re
import threading
from contextlib import contextmanager
from pathlib import Path

from . import state, streamer
from .api import ApiError, NetworkError, app_button, inline
from .common import DIGEST_MAX, MODE_LIFE, PAGE, Refused, app_link, attr, esc, length_text, plural, trim


B_FIX = "🛠 Critique / Regenerate"
UPLOAD_SHOWN = 8_000_000  # a video bigger than this gets the progress message while it's sent


class ReviewPipeline:
    """The bot's reviewing side (mixed into bot.Bot)."""

    def cmd_videos(self, chat: int, arg: str = "") -> None:
        text, buttons = self.video_list(0)
        self.say(chat, text, buttons)

    def videos(self) -> list:
        """What can still be watched and deleted: held for your OK first, then the queue in posting order,
        then the library's clips that aren't queued."""
        store, _accounts, _svc = self.posting()
        with store.lock:
            items = [i for i in store.items.values() if i.status in ("queued", "failed", "new", "uploading")
                     and (i.status != "new" or i.exists())]
        def order(i):
            if i.review == "waiting" and i.status == "queued":
                return (0, i.added_at)
            if i.queue_pos is not None:
                return (1, i.planned_at or float("inf"), i.queue_pos)
            return (2, -i.added_at)
        return sorted(items, key=order)

    def video_list(self, page: int) -> tuple[str, dict | None]:
        from ..posting import schedule

        items = self.videos()
        if not items:
            return "Nothing is waiting to post. New clips show up here once automation makes them.", None
        if self.app_url:  # they're watched in the Mini App, not listed here
            held = sum(1 for i in items if i.review == "waiting" and i.status == "queued")
            return (f"<b>🎬 {plural(len(items), 'video')}</b> waiting to post"
                    + (f"\n⏸ {plural(held, 'waits', 'wait')} for an OK" if held else ""),
                    inline([[self.app_row("🎬 Watch them in the app")]]))
        _store, accounts, _svc = self.posting()
        pages = max(1, -(-len(items) // PAGE))
        page = max(0, min(page, pages - 1))
        held = sum(1 for i in items if i.review == "waiting" and i.status == "queued")
        text = f"<b>🎬 {plural(len(items), 'video')}</b> waiting to post"
        if held:
            text += f"\n⏸ {plural(held, 'waits', 'wait')} for your OK"
        text += "\nTap one to watch it, OK it or delete it."
        rows = []
        now = self.now()
        for it in items[page * PAGE:(page + 1) * PAGE]:
            a = accounts.get(it.account) if it.account else None
            if it.review == "waiting" and it.status == "queued":
                mark = "⏸"
            elif it.status == "failed":
                mark = "✖"
            elif it.status == "uploading":
                mark = "⏫"
            elif it.planned_at:
                mark = schedule.fmt_when(it.planned_at, now).replace("Today ", "").replace("Tomorrow ", "Tmrw ")
            else:
                mark = "📚"
            label = f"{mark} {trim(it.headline, 34)}" + (f" · {a.handle}" if a else "")
            rows.append([(trim(label, 60), f"v:{it.id}")])
        if pages > 1:
            nav = []
            if page:
                nav.append(("◀", f"list:{page - 1}"))
            nav.append((f"{page + 1} of {pages}", f"list:{page}"))
            if page < pages - 1:
                nav.append(("▶", f"list:{page + 1}"))
            rows.append(nav)
        return text, inline(rows)

    def press_list(self, chat, mid, arg, video) -> str:
        try:
            page = int(arg or 0)
        except ValueError:
            page = 0
        text, buttons = self.video_list(page)
        if video:
            self.say(chat, text, buttons)
            return ""
        try:
            self.api.edit(chat, mid, text, buttons)
        except ApiError:
            self.say(chat, text, buttons)
        return ""

    def press_video(self, chat, mid, arg, video) -> str:
        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is None:
            return "It isn't in the library any more"
        if not self.show_later(chat, it):
            return "It's on its way"
        return "Sending the video…" if self.uploading() and it.exists() else ""

    def press_ok(self, chat, mid, arg, video) -> str:
        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is None:
            return "It isn't in the library any more"
        if not store.approve([it.id]):
            self.refresh_card(chat, mid, it, video)
            return "It didn't need an OK"
        self.save(store)
        self.log(f"{self.name(chat)} OK'd “{trim(it.headline, 80)}” to post.")
        self.refresh_card(chat, mid, it, video)
        self.settle(it.id, skip=(chat, mid))
        self.did(chat, "ok", it.headline, it.id)
        return "OK: it posts when its time comes"

    def press_delete(self, chat, mid, arg, video) -> str:
        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is None:
            return "It isn't in the library any more"
        where = "to the Recycle Bin" if self._recycle() else "for good"
        buttons = inline([[("🗑 Yes, delete it", f"DEL:{it.id}"), ("↩ Keep it", f"keep:{it.id}")]])
        try:
            self.api.edit_buttons(chat, mid, buttons)
        except ApiError:
            self.say(chat, f"Delete “{esc(trim(it.headline, 80))}” from the PC {where}?", buttons)
        return f"Delete it from the PC {where}?"

    def press_keep(self, chat, mid, arg, video) -> str:
        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is not None:
            self.refresh_card(chat, mid, it, video)
        return "Kept"

    def press_deleted(self, chat, mid, arg, video) -> str:
        from ..posting import intake, records

        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is None:
            return "It isn't in the library any more"
        from ..posting.config import Settings as PostingSettings

        missing = not it.exists()
        try:
            r = records.discard(store, it.id, PostingSettings.load(), intake.roots(self.settings()))
        except ValueError as exc:
            self.refresh_card(chat, mid, it, video)
            self.say(chat, f"⚠ {esc(exc)}")
            return "Not deleted"
        if not r.removed:
            why = r.failed[0][1] if r.failed else "the file couldn't be deleted"
            self.say(chat, f"⚠ Couldn't delete “{esc(trim(it.headline, 80))}”: {esc(why)}. Close anything that has "
                           "it open on the PC, then try again.")
            self.refresh_card(chat, mid, it, video)
            return "Not deleted"
        self.save(store)
        streamer.drop(it.id, self.previews)
        from ..posting.records import size_text

        freed = f" ({size_text(r.freed)} freed)" if r.freed else ""
        if missing:
            self.log(f"🗑 {self.name(chat)} took “{trim(it.headline, 80)}” off the list (its file was gone "
                     "already).")
            note = "🗑 <b>Taken off the list</b> (its file was gone from the PC already)."
        else:
            self.log(f"🗑 {self.name(chat)} deleted “{trim(it.headline, 80)}”{freed}.")
            note = f"🗑 <b>Deleted from the PC</b>{esc(freed)}."
        if r.failed:
            note += f"\n⚠ {plural(len(r.failed), 'file')} couldn't go: {esc(trim(r.failed[0][1], 100))}"
        self._mark(chat, mid, it, note, video)
        self.settle(it.id, skip=(chat, mid), gone=note)
        self.did(chat, "delete", it.headline, it.id)
        return "Deleted"

    def press_now(self, chat, mid, arg, video) -> str:
        store, accounts, svc = self.posting()
        it = store.get(arg)
        if it is None:
            return "It isn't in the library any more"
        a = accounts.get(it.account) if it.account else None
        if a is None or not a.logged_in:
            return "Sign the account in to TikTok first (Accounts tab on the PC)"
        if svc is None or not getattr(svc, "posting", False):
            return "Posting isn't running: start automation on the PC"
        if it.status != "queued":
            return "Only a queued video can post now"
        if store.approve([it.id]):  # posting it now is your OK
            self.save(store)
        svc.actions.submit("post_now", account=a.id, item=it.id)
        svc.wake.set()
        self.log(f"{self.name(chat)} asked to post “{trim(it.headline, 80)}” to {a.handle} now.")
        self._mark(chat, mid, it, f"🚀 Posting it to {esc(a.handle)} now…", video)
        self.settle(it.id, skip=(chat, mid))  # its other cards, and this one stays until the upload starts
        self.did(chat, "post_now", it.headline, it.id)
        return "Posting now"

    def press_again(self, chat, mid, arg, video) -> str:
        from ..posting.store import INTERRUPTED

        store, _accounts, _svc = self.posting()
        it = store.get(arg)
        if it is None or it.status != "failed":
            return "It isn't waiting to be tried again"
        if it.last_error == INTERRUPTED:
            return "It may be on TikTok already: check the account, then retry on the PC"
        with store.lock:
            it.status, it.last_error = "queued", ""
        self.save(store)
        self.refresh_card(chat, mid, it, video)
        self.settle(it.id, skip=(chat, mid))
        self.did(chat, "retry", it.headline, it.id)
        return "It's queued again"

    def _recycle(self) -> bool:
        from ..posting.config import Settings as PostingSettings

        try:
            return bool(PostingSettings.load().recycle)
        except Exception:  # noqa: BLE001
            return False

    def fixable(self, it) -> bool:
        """Can this video be made again from a critique? One the Creator or the Studio made, not posted yet,
        with its storyboard still beside it."""
        if isinstance(it, str):
            it = self.posting()[0].get(it)
        if it is None or not it.ai_generated or it.status in ("posted", "uploading") or not it.path:
            return False
        return (Path(it.path).parent / "storyboard.json").is_file()

    def press_fix(self, chat, mid, arg, video) -> str:
        it = self.posting()[0].get(arg)
        if it is None:
            return "It isn't in the library any more"
        if not self.fixable(it):
            return "Only a video the AI made, not posted yet, can be made again"
        self.fixing[chat] = it.id
        self.modes[chat] = ("fix", self.now() + MODE_LIFE)
        self.say(chat, f"🛠 What should be different about “{esc(trim(it.headline, 80))}”? Say it in your own words:\n"
                       "• <i>make the captions yellow</i>, <i>the music is too loud</i>: only that is changed and "
                       "rendered again\n"
                       "• <i>use the British voice</i>: the same script, voiced again\n"
                       "• <i>the hook is flat, open with the twist</i>: the AI rewrites that and keeps the rest\n"
                       "The new version comes back as v2, and you approve the one to post.")
        return ""

    def cmd_fix(self, chat: int, text: str = "") -> None:
        item = self.fixing.pop(chat, "")
        if not item:
            self.say(chat, f"Open a video the AI made (/videos) and tap <b>{B_FIX}</b> under it"
                           + (", or use the app." if self.app_url else "."))
            return
        if not text.strip():
            self.fixing[chat] = item
            self.modes[chat] = ("fix", self.now() + MODE_LIFE)
            self.say(chat, "Say what should be different, in a sentence or two.")
            return
        try:
            r = self.fix(chat, item, text)
        except Refused as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return
        self.say(chat, "🛠 " + esc(r["message"]))

    def fix(self, chat: int, item_id: str, text: str) -> dict:
        """Queue a video to be made again with `text` (what `chat` says is wrong with it) answered. The one it
        replaces is held meanwhile, so it doesn't post as it is. Raises Refused."""
        with self._fix_lock:  # two people sending one about the same video: the second is told it's under way
            return self._fix(chat, item_id, text)

    def _fix(self, chat: int, item_id: str, text: str) -> dict:
        from ..creator import critique, director
        from ..creator.orchestrator import CreateRequest, queue as queue_creation
        from ..studio import retouch
        from ..studio.orchestrator import StudioRequest

        text = critique.clean(text)
        if len(text) < 3:
            raise Refused("Say what should be different about the video.", 400)
        now = self.now()
        s = self.settings()
        store, _accounts, _svc = self.posting()
        it = store.get(item_id)
        if it is None:
            raise Refused("That video isn't in the library any more.", 404)
        if it.status in ("posted", "uploading"):
            raise Refused("It's on TikTok already (or going up right now), so it can't be made again from here.")
        if not it.ai_generated:
            raise Refused("Only a video the Creator or the Studio made can be made again from a critique. A clip is "
                          "cut from someone's video as it is: delete it if it's no good.")
        board = critique.storyboard_of(Path(it.path)) if it.path else None
        if board is None:
            raise Refused("Its storyboard isn't beside it on the PC any more, so it can't be made again.")
        folder = Path(it.path).parent
        plan = retouch.read(text)
        how = plan.how
        if how == "rewrite" and not director.script_writers(s):
            raise Refused(director.no_writer_text(s))
        with self.lock:
            data = state.load(self.state_file)
        if any(c.get("item") == it.id and c.get("status") in ("queued", "working")
               for c in data["critiques"].values()):
            raise Refused("It's being made again already. Wait for that version, then say what's still wrong.")
        version = max(state.version_of(data, i) for i in state.family(data, it.id)) + 1
        made = _made(folder)
        studio = isinstance(made.get("studio"), dict)
        shape = _shape(str((made.get("studio") or {}).get("canvas") or "")) if studio else ""
        common = dict(prompt=board.prompt, title=board.title, category=it.category_id or it.category_name,
                      style=str(made.get("style") or ""), critique=text, revises=it.headline)
        file = ""
        if how == "retouch" and retouch.can_retouch(folder):
            # Only the timeline changes: one render, beside the first version
            file = retouch.version_name(folder, board.title or it.headline, version)
            req = StudioRequest(**common, retouch={"folder": str(folder), "source": Path(it.path).name,
                                                   "name": file, "edits": plan.edits})
            way = ("only that is changed on its timeline and rendered again (" + "; ".join(plan.said) + "): the "
                   "script, the voice and the pictures stay.")
        elif how in ("retouch", "voice"):
            # Nothing to rewrite, but there's no timeline to change (the classic Creator keeps none), or the
            # voice itself changes: the same storyboard is made again, with the change
            how = "voice"
            common["storyboard"] = board.to_json()
            if studio or plan.edits:
                req = StudioRequest(**common, keep_script=True, voice=plan.voice, look=plan.edits or None,
                                    orientation=shape or "vertical")
            else:
                req = CreateRequest(**common, keep_script=True, voice=plan.voice)
            way = ("the same script is made again with that changed (" + "; ".join(plan.said) + "); nothing is "
                   "rewritten." + ("" if plan.voice or studio else " This one has no timeline to change (the "
                                   "classic Video Creator made it), so its pictures are made again too."))
        else:
            common["storyboard"] = board.to_json()
            if studio or plan.edits:  # the caption and level changes ride along on the new timeline
                req = StudioRequest(**{**common, "critique": plan.rest or text}, voice=plan.voice,
                                    look=plan.edits or None, orientation=shape or "vertical")
            else:
                req = CreateRequest(**common, voice=plan.voice)
            way = "the AI makes it again with that fixed" + (" (and " + "; ".join(plan.said) + ")." if plan.said
                                                             else ".")
        db = self.runner.db
        job_id = queue_creation(db, req, it.category_id, now, origins=["creator", "telegram", "critique"])
        held = ""
        with store.lock:
            if it.status == "queued" and it.review != "waiting":
                held, it.review = it.review or "-", "waiting"  # it doesn't post as it is meanwhile
        if held:
            self.save(store)
        with self.lock, state.edit(self.state_file) as d:
            entry = state.add_critique(d, chat, it.id, it.headline, text, job_id, now, how=how, file=file)
            entry["held"], entry["version"] = held, version
        self._remember(f"job:{job_id}", chat, it.headline, "fix")
        db.set_meta("nudge", "1")
        self.log(f"{self.name(chat)} asked for “{trim(it.headline, 80)}” to be changed ({how}): “{trim(text, 120)}”")
        self.settle(it.id)
        self.did(chat, "critique", it.headline, it.id, text)
        cat = next((c for c in self._categories(s) if c.id == it.category_id), None)
        return {"critique": self.critique_json(entry), "job": job_id, "held": bool(held), "how": how,
                "version": version,
                "message": f"Got it: {way} " + html.unescape(self._when(cat))
                           + f" v{version} then waits beside this one: approve the one to post and the other is "
                             "deleted" + (", and this one is held meanwhile so it doesn't post as it is." if held else ".")}

    def critique_json(self, c: dict) -> dict:
        return {"id": c.get("id") or "", "item": c.get("item") or "", "title": c.get("title") or "",
                "text": c.get("text") or "", "by": c.get("by") or 0, "name": c.get("name") or "Someone",
                "at": c.get("at") or 0, "status": c.get("status") or "", "stage": c.get("stage") or "",
                "error": c.get("error") or "", "fixed": c.get("fixed") or "", "how": c.get("how") or "rewrite",
                "version": int(c.get("version") or 2)}

    def sync_critiques(self) -> None:
        """Bring each critique still being answered up to date from its job: being made, made (and which video
        it became), or why not. One that failed gives the first version back the way it was."""
        with self.lock:
            data = state.load(self.state_file)
        going = [c for c in data["critiques"].values() if c.get("status") in ("queued", "working")]
        if not going:
            return
        db = self.runner.db
        store = self.posting()[0]
        now = self.now()
        changes: dict[str, dict] = {}
        for c in going:
            job = db.get(str(c.get("job") or ""))
            if job is None:
                new = {"status": "failed", "error": "It was taken out of the queue.", "stage": ""}
            elif job.status == "failed":
                new = {"status": "failed", "error": trim(job.error, 300), "stage": ""}
            elif job.status == "done":
                items = self._items_of_job(job.id, store)
                if c.get("file"):  # a retouch renders beside the first version: its own file is the new one
                    items = [i for i in items if Path(i.path).name == c["file"]]
                else:
                    items = [i for i in items if i.id != c.get("item")]
                if items:
                    new = {"status": "done", "fixed": items[0].id, "stage": ""}
                elif now - float(job.updated or now) > 15 * 60:
                    new = {"status": "failed", "stage": "",
                           "error": "It was made, but the new video didn't come into the library."}
                else:
                    new = {"status": "working", "stage": "Bringing it into the library"}
            elif job.status == "working":
                new = {"status": "working", "stage": trim(job.stage, 80)}
            else:
                new = {"status": "queued", "stage": ""}
            if any(c.get(k) != v for k, v in new.items()):
                changes[c["id"]] = new
        if not changes:
            return
        failed = []
        with self.lock, state.edit(self.state_file) as d:
            for cid, new in changes.items():
                if cid in d["critiques"]:
                    d["critiques"][cid].update(new)
                    if new["status"] == "failed":
                        failed.append(dict(d["critiques"][cid]))
        for c in failed:  # the hold put on the first version comes off
            it = store.get(c.get("item") or "")
            if it is not None and c.get("held") and it.review == "waiting" and it.status == "queued":
                with store.lock:
                    it.review = "" if c["held"] == "-" else c["held"]
                self.save(store)
                self.settle(it.id)

    # --- versions: the first one, and the ones critiques made of it ---------------------------------------------

    def versions(self, it, data: dict | None = None, store=None) -> list[tuple[int, object]]:
        """(number, video) for every version of `it` still waiting (not posted, not going up), oldest first.
        [] when it's the only one: there's nothing to choose between."""
        if data is None:
            with self.lock:
                data = state.load(self.state_file)
        ids = state.family(data, it.id)
        if len(ids) < 2:
            return []
        store = store or self.posting()[0]
        found = [(state.version_of(data, i), store.get(i)) for i in ids]
        found = [(n, x) for n, x in found if x is not None and x.status not in ("posted", "uploading")]
        return found if len(found) > 1 else []

    def version_label(self, it, data: dict | None = None) -> str:
        """'v2 (Critique Applied)' for a video a critique made, 'v1' for the one it was made from, '' for a
        video nobody has critiqued into a second version."""
        if data is None:
            with self.lock:
                data = state.load(self.state_file)
        n = state.version_of(data, it.id)
        if n > 1:
            return f"v{n} (Critique Applied)"
        return "v1" if len(state.family(data, it.id)) > 1 else ""

    def versions_json(self, it, data: dict | None = None, store=None) -> list[dict]:
        """The versions to choose between, for the Mini App."""
        with self.lock:
            data = data or state.load(self.state_file)
        out = []
        for n, x in self.versions(it, data, store):
            said = next((c for c in data["critiques"].values() if c.get("fixed") == x.id), None)
            out.append({"id": x.id, "version": n, "label": self.version_label(x, data), "title": x.headline,
                        "critique": (said or {}).get("text") or "", "by": (said or {}).get("name") or "",
                        "how": (said or {}).get("how") or ""})
        return out

    def press_approve(self, chat, mid, arg, video) -> str:
        """✅ Approve vN: that version posts, the others are deleted from the PC."""
        try:
            r = self.approve_version(chat, arg, skip=(chat, mid))
        except Refused as exc:
            return trim(str(exc), 190)
        it = self.posting()[0].get(arg)
        if it is not None:
            self.refresh_card(chat, mid, it, video)
        return trim(r["message"], 190)

    def approve_version(self, chat: int, item_id: str, skip: tuple[int, int] | None = None) -> dict:
        """Pick `item_id` out of its versions: it's OK'd to post, and every other version still waiting is
        deleted from the PC (its files the way Delete clears them). Raises Refused."""
        from ..posting import intake, records
        from ..posting.config import Settings as PostingSettings

        with self._fix_lock:
            store, _accounts, _svc = self.posting()
            it = store.get(item_id)
            if it is None:
                raise Refused("That version isn't in the library any more.", 404)
            if it.status in ("posted", "uploading"):
                raise Refused("It's on TikTok already (or going up right now).")
            with self.lock:
                data = state.load(self.state_file)
            if any(c.get("status") in ("queued", "working") and (c.get("item") in state.family(data, it.id))
                   for c in data["critiques"].values()):
                raise Refused("Another version of it is still being made. Wait for it, then pick.")
            mine = self.version_label(it, data) or "this one"
            others = [(n, x) for n, x in self.versions(it, data, store) if x.id != it.id]
            if store.approve([it.id]):
                self.save(store)
            gone, kept = [], []
            for n, x in others:
                try:
                    r = records.discard(store, x.id, PostingSettings.load(), intake.roots(self.settings()))
                except ValueError as exc:
                    kept.append((n, str(exc)))
                    continue
                if not r.removed:
                    kept.append((n, r.failed[0][1] if r.failed else "its file couldn't be deleted"))
                    continue
                gone.append(n)
                streamer.drop(x.id, self.previews)
                self.settle(x.id, gone=f"🗑 <b>Deleted</b>: {esc(mine)} was picked to post instead.")
            if gone:
                self.save(store)
        self.log(f"{self.name(chat)} picked {mine} of “{trim(it.headline, 80)}” to post"
                 + (f"; {', '.join(f'v{n}' for n in gone)} deleted." if gone else "."))
        self.settle(it.id, skip=skip)
        self.did(chat, "approve", f"{mine} of “{trim(it.headline, 80)}”", it.id)
        message = f"{mine} posts when its time comes." if mine != "this one" else "OK: it posts when its time comes."
        if gone:
            message += " " + ", ".join(f"v{n}" for n in gone) + (" was" if len(gone) == 1 else " were") + " deleted."
        if kept:
            message += f" v{kept[0][0]} couldn't be deleted: {kept[0][1]}"
        return {"item": it.id, "deleted": gone, "kept": [n for n, _w in kept], "message": message}

    # --- a video's card ----------------------------------------------------------------------------------------

    def card(self, it) -> tuple[str, dict | None]:
        store, accounts, svc = self.posting()
        return self.card_text(it, accounts), self.buttons_for(it, accounts, svc)

    def card_text(self, it, accounts) -> str:
        """The card's words, with what was said about the video and by whom."""
        text = card_html(it, accounts, self.now())
        with self.lock:
            data = state.load(self.state_file)
        said = state.critiques_of(data, it.id)
        label = self.version_label(it, data)
        if label:
            text = f"🧬 <b>{esc(label)}</b>\n" + text
        for c in said[-3:]:
            if c.get("fixed") == it.id:
                text += (f"\n🛠 Made again from <b>{esc(c.get('name') or 'someone')}</b>'s critique of the first "
                         f"version: <i>“{esc(trim(c.get('text'), 160))}”</i>")
            else:
                text += f"\n🛠 <b>{esc(c.get('name') or 'Someone')}</b>: <i>“{esc(trim(c.get('text'), 160))}”</i> · "
                text += {"queued": "waiting to be made again", "working": "being made again now",
                         "done": "made again: the new version waits for an OK",
                         "failed": "couldn't be made again"}.get(c.get("status") or "", "")
        if self.versions(it, data):
            text += "\n<i>Approve the version to post: the other is deleted from the PC.</i>"
        return text

    def buttons_for(self, it, accounts, svc) -> dict | None:
        fixable = self.fixable(it)
        with self.lock:
            data = state.load(self.state_file)
        versions = [(n, x.id) for n, x in self.versions(it, data)]
        if self.app_url and it.status not in ("posted", "uploading"):
            rows = [[self.app_row("▶ Watch it in the app", it.id)]]
            if versions:
                rows.append([(f"✅ Approve v{n}", f"apv:{i}") for n, i in versions[-3:]])
            if fixable:
                rows.append([(B_FIX, f"fix:{it.id}")])
            return inline(rows)
        return card_buttons(it, accounts, svc, fixable=fixable, versions=versions)

    def show(self, chat: int, it, note: str = "", live: str = "") -> dict | None:
        """Send the video itself with its card (its caption and buttons). Remembered, so the card follows when
        the video is OK'd, posted or deleted somewhere else. Use show_later from a chat's button: an upload
        can take a while. `live` names the progress message this video's making had (bot.progress_tick): it
        carries on through the preview and the upload, then goes."""
        text, buttons = self.card(it)
        if note:
            text = f"{note}\n\n{text}"
        if self.app_url and it.exists():  # watched in the Mini App: no upload into the chat
            if live:
                self.live.finish(live, chat=chat)
            return self._shown(it, chat, self.say(chat, text, buttons), video=False)
        if not it.exists():
            if live:
                self.live.finish(live, chat=chat)
            sent = self.say(chat, "⚠ The video file isn't on the PC any more.\n\n" + text,
                            inline([[("🗑 Take it off the list", f"DEL:{it.id}")]]))
            return self._shown(it, chat, sent, video=False)
        try:
            size = Path(it.path).stat().st_size
        except OSError:
            size = 0
        key = live if live and chat in self.live.chats(live) else ""
        if not key and size > UPLOAD_SHOWN:  # a long upload: say how far, in one message
            key = f"send:{it.id}"
            self.live.start(key, chat, it.headline, "Getting it ready", icon="📤", verb="Sending")
        try:
            with self._busy(chat, "upload_video"):  # "sending a video…" at the top of the chat meanwhile
                if key:
                    self.live.update(key, None, "Making the mobile preview" if size > streamer.LIMIT
                                     else "Getting it ready")
                try:
                    send = streamer.for_telegram(Path(it.path), it.id, self.previews)
                except (streamer.TooBig, RuntimeError, OSError) as exc:
                    return self._shown(it, chat, self.say(chat, f"⚠ {esc(exc)}\n\n" + text, buttons), video=False)
                if send.copy:
                    text += f"\n<i>{esc(send.badge)}. The one that posts is the full-quality master.</i>"

                def sent_so_far(done: int, total: int) -> None:
                    if key and total:
                        self.live.update(key, done / total, "Sending the video")

                try:
                    sent = self.api.send_video(chat, send.path, text, buttons, send.width, send.height,
                                               send.duration, progress=sent_so_far if key else None)
                except ApiError as exc:
                    if exc.code == 403:
                        self._blocked(chat)
                        return None
                    sent = self.say(chat, f"⚠ Telegram didn't take the video ({esc(exc.description)}).\n\n" + text,
                                    buttons)
                    return self._shown(it, chat, sent, video=False)
                except NetworkError as exc:
                    self.log(f"⚠ Couldn't send a video: {exc}")
                    return None
            return self._shown(it, chat, sent, video=True)
        finally:
            for k in {key, live} - {""}:
                self.live.finish(k, chat=chat)  # the video (or why not) has taken its place

    def show_later(self, chat: int, it, note: str = "", live: str = "") -> bool:
        """show(), from the thread that sends videos, so the chat keeps answering during a long upload (and
        right away when the bot isn't running its threads, as in tests). False if it's on its way already."""
        key = (chat, it.id)
        with self._sending_lock:
            if key in self.sending:
                if live:
                    self.live.finish(live, chat=chat)
                return False
            self.sending.add(key)
        if self.uploading():
            self.uploads.put((chat, it.id, note, live))
            return True
        try:
            self.show(chat, it, note, live)
        finally:
            with self._sending_lock:
                self.sending.discard(key)
        return True

    def uploading(self) -> bool:
        """The thread that sends videos is running."""
        return self._uploader is not None and self._uploader.is_alive()

    def _video_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                chat, item_id, note, live = self.uploads.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                it = self.posting()[0].get(item_id)
                if it is not None and not self.stop_event.is_set():
                    self.show(chat, it, note, live)
                elif live:
                    self.live.finish(live, chat=chat)
            except Exception as exc:  # noqa: BLE001 - the next video still goes
                self.log(f"⚠ Couldn't send a video: {type(exc).__name__}: {exc}")
            finally:
                with self._sending_lock:
                    self.sending.discard((chat, item_id))

    @contextmanager
    def _busy(self, chat: int, action: str = "typing"):
        """Telegram shows "sending a video…" (or "typing…") for 5 s at a time: keep it up while this runs."""
        done = threading.Event()
        api = self.api

        def keep() -> None:
            while not done.wait(4.5):
                api.typing(chat, action)

        if api is not None:
            api.typing(chat, action)
            threading.Thread(target=keep, name="telegram-busy", daemon=True).start()
        try:
            yield
        finally:
            done.set()

    def _shown(self, it, chat: int, sent: dict | None, video: bool) -> dict | None:
        """Remember a card sent for `it`, so it follows what happens to the video."""
        if not sent:
            return sent
        mid = int(sent.get("message_id") or 0)
        with self.lock, state.edit(self.state_file) as data:
            entry = data["shown"].setdefault(it.id, {"at": self.now(), "msgs": [], "was": card_state(it)})
            entry["at"] = self.now()
            entry["was"] = card_state(it)
            if not any(m[:2] == [chat, mid] for m in entry["msgs"]):
                entry["msgs"].append([chat, mid, video])
        return sent

    def refresh_card(self, chat: int, mid: int, it, video: bool) -> None:
        text, buttons = self.card(it)
        try:
            if video:
                self.api.edit_caption(chat, mid, text, buttons)
            else:
                self.api.edit(chat, mid, text, buttons)
        except ApiError:
            pass

    def _mark(self, chat: int, mid: int, it, note: str, video: bool) -> None:
        """The card with a line on what just happened, and no buttons."""
        text = self.card_text(it, self.posting()[1]) + f"\n\n{note}"
        try:
            if video:
                self.api.edit_caption(chat, mid, text, None)
            else:
                self.api.edit(chat, mid, text, None)
        except ApiError:
            pass

    def settle(self, item_id: str, skip: tuple[int, int] | None = None, gone: str = "") -> None:
        """Bring every card shown of a video up to date (OK'd, posted, deleted…)."""
        with self.lock:
            entry = state.load(self.state_file)["shown"].get(item_id)
        if not entry:
            return
        store, accounts, svc = self.posting()
        it = store.get(item_id)
        for m in entry.get("msgs") or []:
            chat, mid, video = m[0], m[1], (m[2] if len(m) > 2 else True)
            if skip and (chat, mid) == tuple(skip):
                continue
            if it is None or gone:
                text, buttons = gone or "🗑 Gone from the library.", None
            else:
                text, buttons = self.card_text(it, accounts), self.buttons_for(it, accounts, svc)
            try:
                if video:
                    self.api.edit_caption(chat, mid, text, buttons)
                else:
                    self.api.edit(chat, mid, text, buttons)
            except ApiError:
                pass
            except NetworkError:
                return
        with self.lock, state.edit(self.state_file) as data:
            if it is None or gone:
                data["shown"].pop(item_id, None)
            elif item_id in data["shown"]:
                data["shown"][item_id]["was"] = card_state(it)

    def show_all(self, it, note: str = "") -> None:
        with self.lock:
            chats = [c["id"] for c in state.load(self.state_file)["chats"] if not c.get("blocked")]
        for chat in chats:
            self.show_later(chat, it, note)

    def _items_of_job(self, job_id: str, store=None) -> list:
        job = self.runner.db.get(job_id)
        if job is None or not job.folder:
            return []
        store = store or self.posting()[0]
        return _items_in(store, Path(job.folder))

    def _items_of_order(self, order_id: str, store) -> list:
        out = []
        for j in self.runner.db.order_jobs(order_id):
            if j.folder:
                out += _items_in(store, Path(j.folder))
        return out


# --- what a card says ------------------------------------------------------------------------------------------


def _items_in(store, folder: Path) -> list:
    import os

    where = os.path.normcase(os.path.abspath(folder))

    def inside(path: str) -> bool:
        d = os.path.normcase(os.path.abspath(Path(path).parent))
        return d == where or d.startswith(where.rstrip(os.sep) + os.sep)

    with store.lock:
        found = [i for i in store.items.values() if i.path and i.status != "posted" and inside(i.path)]
    return sorted(found, key=lambda i: (i.part or 0, i.path))


def _made(folder: Path) -> dict:
    """How the Creator or the Studio made the video in `folder` (its clips.json's "creator"), or {}."""
    import json

    try:
        made = json.loads((folder / "clips.json").read_text(encoding="utf-8")).get("creator")
    except (OSError, ValueError, AttributeError):
        return {}
    return made if isinstance(made, dict) else {}


def _shape(canvas: str) -> str:
    """'1080x1920' → 'vertical' (the Studio's name for a picture's shape; '' when it can't be read)."""
    m = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", canvas or "")
    if not m:
        return ""
    w, h = int(m.group(1)), int(m.group(2))
    return "vertical" if w < h else "square" if w == h else "landscape"


def card_state(it) -> str:
    """What a card shows that can change: its buttons and its status line."""
    if it.status == "posted":
        return "posted:" + (it.tiktok_url or "")
    return f"{it.status}:{it.review}:{it.queue_pos is not None}:{int((it.planned_at or 0) // 300)}"


def card_html(it, accounts, now: float) -> str:
    from ..posting import schedule

    a = accounts.get(it.account) if it.account else None
    lines = [f"<b>{esc(trim(it.headline, 140))}</b>"]
    who = [a.handle if a else "", it.category_name, it.part_label]
    if any(who):
        lines.append(esc(" · ".join(w for w in who if w)))
    if it.status == "posted":
        when = schedule.fmt_when(it.posted_at, now) if it.posted_at else ""
        lines.append(f"✔ Posted {esc(when)}" + (f' · <a href="{attr(it.tiktok_url)}">open on TikTok</a>'
                                                 if it.tiktok_url else ""))
    elif it.status == "uploading":
        lines.append("⏫ Posting now…")
    elif it.status == "failed":
        lines.append(f"✖ Didn't post: {esc(trim(it.last_error, 220))}")
    elif it.review == "waiting" and it.queue_pos is not None:
        lines.append("⏸ Waits for your OK before it posts")
    elif it.queue_pos is not None:
        lines.append(f"🕓 Posts {esc(schedule.fmt_when(it.planned_at, now))}" if it.planned_at else "🕓 In the queue")
    else:
        lines.append("📚 In the library, not queued" + ("" if a else " (its category has no TikTok account)"))
    meta = [length_text(it.duration or float(it.facts.get("length") or 0))]
    if it.total:
        meta.append(f"score {it.total:.0f}")
    if it.ai_generated:
        meta.append("AI-generated, labelled as such")
    meta = [m for m in meta if m]
    if meta:
        lines.append(esc(" · ".join(meta)))
    story = it.facts.get("creator") if isinstance(it.facts.get("creator"), dict) else {}
    if story.get("written_by") == "your script" or story.get("prompt") == "your script":
        lines.append("✨ Made from your own script")
    elif story.get("prompt"):
        lines.append(f"✨ Made from “{esc(trim(story['prompt'], 160))}”")
    elif it.facts.get("source_title"):
        src = f"From “{esc(trim(it.facts['source_title'], 90))}”"
        if it.facts.get("source_channel"):
            src += f" ({esc(trim(it.facts['source_channel'], 40))})"
        lines.append(src)
    caption = " ".join(x for x in (it.caption or it.hook, it.hashtags) if x)
    if caption and " ".join(caption.split()) != " ".join(it.headline.split()):  # not just the headline again
        lines.append(f"<i>{esc(trim(caption, 260))}</i>")
    if it.warnings:
        lines.append(f"⚠ {esc(trim('; '.join(it.warnings[:2]), 160))}")
    return "\n".join(lines)


def card_buttons(it, accounts, service, fixable: bool = False, versions: list | None = None) -> dict | None:
    """The buttons under a video's card. `fixable`: it can be made again from a critique (Bot.fixable).
    `versions`: [(number, item id)] when there's more than one version of it to choose between: then
    ✅ Approve vN takes the place of OK (approving one deletes the others)."""
    from ..posting.store import INTERRUPTED

    rows = []
    if it.status == "posted":
        return inline([[("▶ Open on TikTok", it.tiktok_url)]]) if it.tiktok_url else None
    if it.status == "uploading":
        return None
    a = accounts.get(it.account) if it.account else None
    can_post = it.status == "queued" and a is not None and a.logged_in and service is not None
    if versions and it.status == "queued":
        rows.append([(f"✅ Approve v{n}", f"apv:{i}") for n, i in versions[-3:]])
        rows.append([("🗑 Delete this one", f"del:{it.id}")])
    elif it.review == "waiting" and it.status == "queued":
        rows.append([("✅ OK to post", f"ok:{it.id}"), ("🗑 Delete", f"del:{it.id}")])
        if can_post:
            rows.append([("🚀 Post it now", f"now:{it.id}")])
    elif it.status == "failed":
        row = [("🗑 Delete", f"del:{it.id}")]
        if it.last_error != INTERRUPTED:
            row.insert(0, ("🔁 Try again", f"again:{it.id}"))
        rows.append(row)
    else:
        row = [("🗑 Delete", f"del:{it.id}")]
        if can_post:
            row.insert(0, ("🚀 Post it now", f"now:{it.id}"))
        rows.append(row)
    if fixable:
        rows.append([(B_FIX, f"fix:{it.id}")])
    rows.append([("🎬 All videos", "list:0")])
    return inline(rows)


def group_new(items: list) -> list[tuple[str, list]]:
    """New videos in messages: each Creator video on its own, clips together by the video they're from."""
    groups: dict[str, list] = {}
    for it in sorted(items, key=lambda i: i.added_at):
        key = it.id if it.ai_generated else str(Path(it.path).parent)
        groups.setdefault(key, []).append(it)
    out = []
    for group in groups.values():
        first = group[0]
        if first.ai_generated:
            out.append(("🎬 A new Creator video", group))
        else:
            src = trim(first.facts.get("source_title") or Path(first.path).parent.name, 60)
            out.append((f"📥 {plural(len(group), 'new clip')} from “{src}”", group))
    return out


def catch_up_html(items: list, app_url: str = "") -> tuple[str, dict]:
    """Lots of new videos at once, as one message."""
    made = sum(1 for i in items if i.ai_generated)
    clips = len(items) - made
    bits = ([plural(clips, "new clip")] if clips else []) + ([plural(made, "new Creator video")] if made else [])
    sources = len({str(Path(i.path).parent) for i in items if not i.ai_generated})
    text = f"<b>📥 {' and '.join(bits)}</b>" + (f" from {plural(sources, 'video')}" if clips and sources > 1 else "")
    if app_url:
        return text + ".", inline([[app_button("🎬 Watch them in the app", app_link(app_url))]])
    return text + ". /videos lists them.", inline([[("🎬 All videos", "list:0")]])


def digest_html(title: str, items: list, accounts, now: float, app_url: str = "") -> tuple[str, dict]:
    """A message listing some videos. With the Mini App's address, one button opens them there; else a button
    each sends the video into the chat."""
    from ..posting import schedule

    lines = [f"<b>{esc(title)}</b>"] if title else []
    rows = []
    for n, it in enumerate(items[:DIGEST_MAX], 1):
        a = accounts.get(it.account) if it.account else None
        when = schedule.fmt_when(it.planned_at, now) if it.planned_at else ""
        bits = [a.handle if a else "not queued", when, length_text(it.duration)]
        lines.append(f"{n}. {esc(trim(it.headline, 80))}\n    {esc(' · '.join(b for b in bits if b))}")
        rows.append([(trim(f"▶ {n}. {it.headline}", 60), f"v:{it.id}")])
    if app_url:
        if len(items) > DIGEST_MAX:
            lines.append(f"…and {len(items) - DIGEST_MAX} more in the app")
        first = app_link(app_url, items[0].id if len(items) == 1 else "")
        return "\n".join(lines), inline([[app_button("▶ Watch it in the app" if len(items) == 1 else
                                                     "🎬 Watch them in the app", first)]])
    if len(items) > DIGEST_MAX:
        lines.append(f"…and {len(items) - DIGEST_MAX} more in /videos")
    rows.append([("🎬 All videos", "list:0")])
    return "\n".join(lines), inline(rows)
