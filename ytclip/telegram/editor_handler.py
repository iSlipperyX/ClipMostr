"""Downloads and the sports editor, from the chat and the Mini App.

A sports video asked for from Telegram is a download: it's made, kept in the library, tied to no TikTok
account and queued for none (posting/intake.py leaves it alone). This is what a person does with one:

* **save it**: the Mini App's Download button streams the full-quality file to the phone, and ⬇ Send the file
  (in the chat or the app) has the bot send it into the chat as a file, not a re-compressed video, when
  Telegram takes it (bots may send 50 MB);
* **edit it**: the Mini App's editor (sportsdesk/editor.py says what there is to change) sends its changes
  here; the Sports worker cuts the game's footage again and the new version comes back as v2 beside the first,
  each still there to save, and one can be kept and the other deleted;
* **post it after all**: 📤 Queue it to post puts it in the queue of the account its category posts to.

Edits ride on what critiques already have (state.add_critique, with how "edit"): the version numbers, "being
changed" on the video, the choice between versions, and who did what in the activity list.
"""

from __future__ import annotations

from pathlib import Path

from . import state
from .api import ApiError, NetworkError
from .common import Refused, esc, trim
from .streamer import LIMIT

FILE_NOTE = "\x00file"  # in the uploads queue: send this video's file (to save), not the video with its card
B_FILE = "⬇ Send the file"
B_EDIT = "✂️ Edit it"
B_QUEUE = "📤 Queue it to post"


def size_text(n: int) -> str:
    return f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


class EditorHandler:
    """The part of Bot (bot.py) that hands videos over as files and takes the editor's changes."""

    # --- which videos the editor can open ----------------------------------------------------------------------

    def editable(self, it) -> str:
        """'reel' or 'clip' when `it` is a sports video the editor can cut again (not posted, its plan still
        beside it on the PC), else ''."""
        if isinstance(it, str):
            it = self.posting()[0].get(it)
        if it is None or not it.path or it.status in ("posted", "uploading"):
            return ""
        path = Path(it.path)
        try:
            stamp = ((path.parent / "clips.json").stat().st_mtime_ns, path.stat().st_size)
        except OSError:
            return ""
        cache = self.__dict__.setdefault("_editable", {})
        got = cache.get(it.path)
        if got is None or got[0] != stamp:
            from ..sportsdesk import editor as E

            if len(cache) > 400:
                cache.clear()
            got = cache[it.path] = (stamp, E.kind_of(path.parent, path.name))
        return got[1]

    def edit_doc(self, item_id: str) -> dict:
        """What the Mini App's editor opens for a video (sportsdesk's Desk.describe_edit). Raises Refused."""
        from ..sportsdesk.desk import DeskError

        it = self.posting()[0].get(item_id)
        if it is None or not it.path:
            raise Refused("That video isn't in the library any more.", 404)
        if it.status in ("posted", "uploading"):
            raise Refused("It's on TikTok already (or going up right now), so it can't be edited from here.")
        if not it.exists():
            raise Refused("Its file isn't on the PC any more.", 404)
        path = Path(it.path)
        try:
            doc = self.desk.describe_edit(path.parent, path.name)
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        with self.lock:
            data = state.load(self.state_file)
        doc["item"] = it.id
        doc["headline"] = it.headline
        doc["version"] = self.version_label(it, data)
        doc["editing"] = doc.get("editing") or any(
            c.get("item") == it.id and c.get("status") in ("queued", "working") for c in data["critiques"].values())
        return doc

    # --- an edit, asked for --------------------------------------------------------------------------------

    def edit(self, chat: int, item_id: str, changes: dict) -> dict:
        """Queue the editor's `changes` to a sports video, in `chat`'s name: the new version is cut on the PC
        and comes back beside the first. Raises Refused."""
        with self._fix_lock:  # two people editing the same video: the second is told it's under way
            return self._edit(chat, item_id, changes)

    def _edit(self, chat: int, item_id: str, changes: dict) -> dict:
        from ..sportsdesk.desk import DeskError

        store, _accounts, _svc = self.posting()
        it = store.get(item_id)
        if it is None or not it.path:
            raise Refused("That video isn't in the library any more.", 404)
        if it.status in ("posted", "uploading"):
            raise Refused("It's on TikTok already (or going up right now), so it can't be edited from here.")
        with self.lock:
            data = state.load(self.state_file)
        if any(c.get("item") == it.id and c.get("status") in ("queued", "working")
               for c in data["critiques"].values()):
            raise Refused("An edit of it is being made already. Wait for that version, then edit again.")
        version = max(state.version_of(data, i) for i in state.family(data, it.id)) + 1
        path = Path(it.path)
        try:
            r = self.desk.edit(path.parent, path.name, changes if isinstance(changes, dict) else {},
                               by=self.name(chat), by_id=chat, version=version)
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        now = self.now()
        held = ""
        with store.lock:
            if it.status == "queued" and it.review != "waiting":
                held, it.review = it.review or "-", "waiting"  # it doesn't post as it is meanwhile
        if held:
            self.save(store)
        with self.lock, state.edit(self.state_file) as d:
            entry = state.add_critique(d, chat, it.id, it.headline, r["message"], "", now, how="edit",
                                       file=r["file"])
            entry["held"], entry["version"], entry["desk"] = held, version, r["id"]
        self._remember(f"edit:{r['id']}", chat, it.headline, "edit")
        self.log(f"{self.name(chat)} edited “{trim(it.headline, 80)}”: {trim(r['message'], 160)}")
        self.settle(it.id)
        self.did(chat, "edit", it.headline, it.id, r["message"])
        self.wake.set()
        keep = bool(it.keep)
        return {"critique": self.critique_json(entry), "edit": r["id"], "version": version, "held": bool(held),
                "message": (f"{r['message']} The PC is cutting v{version} now; it comes back beside this one"
                            + (", each yours to download." if keep else
                               ": approve the one to post and the other is deleted"
                               + (", and this one is held meanwhile so it doesn't post as it is." if held else ".")))}

    def edit_progress(self, ident: str) -> dict | None:
        try:
            return self.desk.edit_state(ident)
        except Exception:  # noqa: BLE001 - sports.db busy for a moment: the next look
            return {"status": "working", "stage": "", "frac": None, "error": "", "file": "", "folder": ""}

    # --- the file itself, to save --------------------------------------------------------------------------

    def send_file(self, chat: int, item_id: str) -> dict:
        """Send a video's own file into `chat` (a document: Telegram doesn't compress it again), to save from
        there. Raises Refused when it's gone or over what a bot may send."""
        it = self.posting()[0].get(item_id)
        if it is None or not it.path:
            raise Refused("That video isn't in the library any more.", 404)
        try:
            size = Path(it.path).stat().st_size
        except OSError:
            raise Refused("Its file isn't on the PC any more.", 404) from None
        if size > LIMIT:
            raise Refused(f"It's {size_text(size)}, and a bot may send 50 MB at most. "
                          + ("Use Download in the app instead: that streams the full file from the PC."
                             if self.app_url else "Copy it from the PC instead."), 413)
        key = (chat, f"file:{it.id}")
        with self._sending_lock:
            if key in self.sending:
                return {"message": "It's on its way to our chat already."}
            self.sending.add(key)
        if self.uploading():
            self.uploads.put((chat, it.id, FILE_NOTE, ""))
        else:
            self.send_file_now(chat, it.id)
        self.did(chat, "download", it.headline, it.id)
        return {"message": f"Sending the file ({size_text(size)}) to our chat: save it from there."}

    def send_file_now(self, chat: int, item_id: str) -> None:
        """(From the thread that sends videos.)"""
        try:
            it = self.posting()[0].get(item_id)
            if it is None or not it.exists():
                self.say(chat, "⚠ That video's file isn't on the PC any more.")
                return
            path = Path(it.path)
            name = self.host_name(it)
            text = (f"⬇ <b>{esc(trim(it.headline, 120))}</b>\nThe full-quality file: tap it, then Save."
                    + (f"\n<i>{esc(trim(' '.join(x for x in (it.caption or it.hook, it.hashtags) if x), 300))}</i>"
                       if (it.caption or it.hook or it.hashtags) else ""))
            try:
                with self._busy(chat, "upload_document"):
                    self.api.send_document(chat, path, text, name=name)
            except ApiError as exc:
                if exc.code == 403:
                    self._blocked(chat)
                    return
                self.say(chat, f"⚠ Telegram didn't take the file ({esc(exc.description)})."
                         + (" Use Download in the app instead." if self.app_url else ""))
            except NetworkError as exc:
                self.log(f"⚠ Couldn't send a file: {exc}")
        finally:
            with self._sending_lock:
                self.sending.discard((chat, f"file:{item_id}"))

    def host_name(self, it) -> str:
        from ..organize import safe_name

        ext = Path(it.path).suffix.lower() or ".mp4"
        return (safe_name(it.headline or it.name or "video", 80) or "video") + ext

    def press_file(self, chat, mid, arg, video) -> str:
        try:
            r = self.send_file(chat, arg)
        except Refused as exc:
            return trim(str(exc), 190)
        return trim(r["message"], 190)

    # --- post it after all -----------------------------------------------------------------------------------

    def queue_download(self, chat: int, item_id: str, category: str = "") -> dict:
        """Queue a download to post (the account its category posts to). Raises Refused."""
        from ..remote.host import HostError

        host = self.webapp.host if self.webapp is not None else None
        if host is None:
            from .mini_app import AppHost

            host = AppHost(self.runner, now=self.now, posting=self._posting)
        try:
            out = host.to_queue(item_id, category)
        except HostError as exc:
            raise Refused(str(exc), exc.status) from exc
        title = (out.get("video") or {}).get("headline") or ""
        self.settle(item_id)
        self.did(chat, "queue", title, item_id)
        return out

    def press_queue(self, chat, mid, arg, video) -> str:
        try:
            r = self.queue_download(chat, arg)
        except Refused as exc:
            return trim(str(exc), 190)
        it = self.posting()[0].get(arg)
        if it is not None:
            self.refresh_card(chat, mid, it, video)
        return trim(r["message"], 190)
