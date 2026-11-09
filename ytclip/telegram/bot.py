"""The Telegram bot: ClipMonster on your phone.

It runs inside automation (pipeline.Runner.run starts it next to posting) whenever Settings → Telegram is
on with a bot token, and only answers the phones linked to it (state.py). From the chat you can:

* see what automation is doing (/status, which can stay pinned as a live dashboard);
* watch the videos waiting to post, OK the ones held for your OK, post one now, or delete a bad one from the
  PC: its files go the way Clean up clears a posted clip (records.discard);
* ask for things: a Video Creator video (/make an idea), clips of a YouTube link (/clip, or just send the
  link), or a request for clips (/request 10 30-45s Gaming funny reactions). Each is sent back to you
  when it's done;
* pause and resume automation, and read the past week's report (/week).

And it tells you, as Settings → Telegram → Send me says: a video that waits for your OK (with the video),
new clips and videos queued to post, each post as it goes up, milestones, problems that need you (a TikTok
sign-in running out, a post failing, YouTube asking this PC to sign in, low disk space), and the weekly
report on Monday mornings.

Three threads: one long-polls Telegram for messages and button presses and answers them; one sends what
automation and posting have to say, and every few seconds looks for videos to show you; and one sends the
videos themselves, one at a time, so a big upload never keeps the bot from answering.
"""

from __future__ import annotations

import html
import queue
import re
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from . import api as tg
from . import preview, state
from .api import ApiError, NetworkError, inline

POLL_TIMEOUT = 25  # seconds a long poll waits for a message
CHECK_EVERY = 15.0  # seconds between looks for videos to show and asks that are done
DASHBOARD_EVERY = 120.0
STATUS_EVERY = 30.0
PRUNE_EVERY = 3600.0
PENDING_LIFE = 30 * 60.0  # a question the bot asked (which category?) is answered within this
MODE_LIFE = 10 * 60.0  # "send me the idea" waits this long for it
PAGE = 8  # videos per page of /videos
DIGEST_MAX = 8  # clips listed in one "new clips" message
REVIEW_PER_LOOK = 3  # videos held for your OK sent in one look (each is an upload)
NEW_GROUPS_MAX = 3  # more "new clips" messages than this at once are sent as one
OUTBOX_MAX = 200
GB = 1024 ** 3
NO_ANSWER_EVERY = 600.0  # an unlinked chat is told how to link at most this often

# What Settings → Telegram → Send me can include (settings.TELEGRAM_SENDS lists them for the window)
SENDS = ("review", "new", "posted", "news", "problems", "weekly")
EVENT_SENDS = {"posted": "posted", "error": "problems", "login_needed": "problems", "milestone": "news",
               "hot": "news"}

COMMANDS = [
    ("status", "What ClipMonster is doing"),
    ("videos", "Videos waiting to post: watch, OK, delete"),
    ("make", "Make a video about an idea"),
    ("clip", "Clip a YouTube video"),
    ("request", "Ask for clips: /request 10 30-45s Gaming"),
    ("requests", "Your requests and how far along they are"),
    ("pause", "Pause automation"),
    ("resume", "Carry on"),
    ("week", "The past week on TikTok"),
    ("dashboard", "A status message that keeps itself up to date"),
    ("help", "What the bot can do"),
]

B_STATUS, B_VIDEOS, B_MAKE, B_CLIP = "📊 Status", "🎬 Videos", "✨ Make a video", "✂️ Clip a link"
B_REQUESTS, B_WEEK, B_PAUSE = "📋 Requests", "📅 This week", "⏯ Pause or resume"
MENU = [[B_STATUS, B_VIDEOS], [B_MAKE, B_CLIP], [B_REQUESTS, B_WEEK], [B_PAUSE]]
BUTTONS = {B_STATUS: "status", B_VIDEOS: "videos", B_MAKE: "make", B_CLIP: "clip", B_REQUESTS: "requests",
           B_WEEK: "week", B_PAUSE: "toggle"}

URL = re.compile(r"https?://\S+", re.I)
MEDIA = ("photo", "video", "document", "sticker", "voice", "audio", "animation", "video_note", "contact",
         "location")


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


def attr(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def trim(text: str, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip(" ,.;:-") + "…"


def plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def length_text(seconds: float) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}" if s else ""


def pc_name() -> str:
    try:
        return socket.gethostname() or "this PC"
    except OSError:
        return "this PC"


def help_html() -> str:
    return (f"<b>ClipMonster on {esc(pc_name())}</b>\n\n"
            f"{B_VIDEOS}: watch what's waiting to post, OK it, post it now or delete it from the PC\n"
            f"{B_MAKE}: /make the real story of the Bermuda Triangle\n"
            f"{B_CLIP}: send a YouTube link and pick its category\n"
            f"{B_REQUESTS}: /request 10 30-45s Gaming funny reactions only\n"
            f"{B_STATUS}, {B_PAUSE}, {B_WEEK}\n\n"
            "I'll message you when a video waits for your OK, when something you asked for is ready, and when "
            "something needs you. What I send is set in ClipMonster → Settings → Telegram.")


def welcome_html() -> str:
    return f"✅ <b>Linked.</b> This chat now runs ClipMonster on {esc(pc_name())}.\n\n" + help_html()


def unlinked_html() -> str:
    return ("This is a private ClipMonster bot. To link your phone, open ClipMonster on your PC → Settings → "
            "Telegram → <b>Link a phone</b>, and send me the code it shows.")


STRANGER_REPLIES = {
    "wrong": "That code isn't right. Send the code ClipMonster shows in Settings → Telegram → Link a phone (it's "
             "new each time).",
    "expired": "That code has run out. Press <b>Link a phone</b> in ClipMonster again for a new one.",
    "none": unlinked_html(),
}


def stranger(data: dict, chat: dict, text: str, now: float) -> str:
    """A message from a chat that isn't linked, tried as the pairing code. What to answer: "linked", "wrong",
    "expired", "none" (how to link), "quiet" (told how to link a moment ago) or "blocked" (too many wrong
    codes: nothing). Links the chat when the code is right."""
    chat_id = int(chat["id"])
    if not text:
        return "quiet"
    said = state.try_code(data, chat_id, text, now) if state.looks_like_code(text) else "none"
    if said == "linked":
        state.link(data, chat, now)
        return said
    told = float(data["told"].get(str(chat_id)) or 0)
    if said == "none" and now - told < NO_ANSWER_EVERY:
        return "quiet"
    if said in STRANGER_REPLIES:
        data["told"][str(chat_id)] = now
    return said


def split_command(text: str) -> tuple[str, str]:
    """'/make@MyClipBot the moon landing' → ('make', 'the moon landing'); plain text → ('', text)."""
    t = (text or "").strip()
    if not t.startswith("/"):
        return "", t
    head, _, rest = t.partition(" ")
    return head[1:].split("@", 1)[0].lower(), rest.strip()


def parse_request(text: str, cats: list) -> dict:
    """'/request 10 30-45s Gaming funny reactions only' → {count, length, category, links, notes}.
    `length` is None when none was given (the category's own is used), `category` a categories.Category
    or None. ValueError for a length it can't read."""
    from .. import orders

    links = URL.findall(text)
    rest = URL.sub(" ", text)
    length = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)?\b", rest, re.I) or \
        re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)\b", rest, re.I)
    if m:
        length = orders.parse_length(m.group(0))
        rest = rest[: m.start()] + " " + rest[m.end():]
    count = 0
    m = re.search(r"\b(\d{1,3})\b", rest)
    if m:
        count = int(m.group(1))
        rest = rest[: m.start()] + " " + rest[m.end():]
    category = None
    low = f" {rest.lower()} "
    for c in sorted(cats, key=lambda c: -len(c.name)):
        name = c.name.strip().lower()
        if not name:
            continue
        hit = re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", low)
        if hit:
            category = c
            low = low[: hit.start()] + " " + low[hit.end():]
            rest = re.sub(rf"(?<![\w]){re.escape(c.name.strip())}(?![\w])", " ", rest, count=1, flags=re.I)
            break
    words = rest.split()
    while words and words[0].lower().strip(",.:;") in ("clips", "clip", "of", "for", "from", "with", "about", "-"):
        words.pop(0)
    notes = " ".join(words).strip(" ,.;:-")
    return {"count": count or 5, "length": length, "category": category, "links": links, "notes": notes}


class Bot:
    def __init__(self, runner, api_factory: Callable[[str], tg.Api] = tg.Api, state_file: Path | None = None,
                 status_file: Path | None = None, previews: Path | None = None, now: Callable[[], float] = time.time,
                 posting=None):
        self.runner = runner
        self.api_factory = api_factory
        self.state_file = state_file
        self.status_file = status_file
        self.previews = previews
        self.now = now
        self._posting = posting  # (store, accounts, service) for tests; else automation's posting worker's
        self._own = None
        self.api: tg.Api | None = None
        self.token = ""
        self.me: dict = {}
        self.error = ""
        self.outbox: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()  # telegram.json, between the two threads
        self.pending: dict[str, dict] = {}  # questions asked with buttons (which category?), by token
        self.modes: dict[int, tuple[str, float]] = {}  # chat -> ("make" | "clip" | "request", until)
        self._threads: list[threading.Thread] = []
        self.uploads: queue.Queue = queue.Queue()  # (chat, item id, note): videos to send, one at a time
        self.sending: set[tuple[int, str]] = set()  # (chat, item id) waiting in uploads or being sent
        self._sending_lock = threading.Lock()
        self._uploader: threading.Thread | None = None
        self._polling = False
        self._last = {"check": 0.0, "status": 0.0, "prune": 0.0, "dashboard": 0.0}
        self._conflict_said = False
        self._retry_at, self._retry_wait = 0.0, 15.0

    # --- running ---------------------------------------------------------------------------------------

    def start(self) -> None:
        self.stop_event.clear()
        for name, target in (("telegram-in", self._listen), ("telegram-out", self._tell_loop),
                             ("telegram-video", self._video_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
            if name == "telegram-video":
                self._uploader = t

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.wake.set()
        for t in self._threads:
            t.join(timeout)
        self._threads = []
        self._uploader = None
        self._write_status(force=True, polling=False)

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def log(self, msg: str) -> None:
        try:
            self.runner.log(f"Telegram: {tg.masked(msg, self.token)}")
        except Exception:  # noqa: BLE001 - a log line never stops the bot
            pass

    def settings(self):
        return self.runner.load()

    def sends(self, s=None) -> set[str]:
        s = s or self.settings()
        return {k.strip() for k in str(getattr(s, "telegram_send", "") or "").split(",") if k.strip()}

    def configure(self) -> bool:
        """Use the token in Settings → Telegram (a new one, or none). True when the bot can run."""
        s = self.settings()
        token = (getattr(s, "telegram_token", "") or "").strip() if getattr(s, "telegram_on", False) else ""
        now = self.now()
        if token != self.token:  # turned on or off, or a new token
            self.token, self.api, self.me, self.error = token, None, {}, ""
            self._retry_at, self._retry_wait = 0.0, 15.0
        if not token or self.api is not None:
            return self.api is not None
        if now < self._retry_at:
            return False
        api = self.api_factory(token)
        try:
            me = api.get_me()
        except (ApiError, NetworkError) as exc:
            said = tg.why(exc)
            if said != self.error:
                self.log(f"⚠ Couldn't start the bot: {said}")
            self.error = said
            bad_token = isinstance(exc, ApiError) and exc.code in (401, 404)
            self._retry_at = now + (3600.0 if bad_token else self._retry_wait)  # a bad token: until it changes
            self._retry_wait = min(300.0, self._retry_wait * 2)
            return False
        with self.lock, state.edit(self.state_file) as data:
            if state.set_bot(data, me):
                self.log("The token is for a different bot than before: link your phone again (Settings → "
                         "Telegram).")
        self.me, self.api, self.error = me, api, ""
        try:
            api.set_commands(COMMANDS)
        except (ApiError, NetworkError):
            pass
        self.log(f"Bot @{me.get('username', '?')} is on.")
        return True

    def _listen(self) -> None:
        backoff = 2.0
        while not self.stop_event.is_set():
            try:
                ready = self.configure()
            except Exception as exc:  # noqa: BLE001
                self.error = tg.why(exc)
                ready = False
            if not ready:
                self._polling = False
                self.stop_event.wait(5.0)  # Settings → Telegram is read again
                continue
            api = self.api
            with self.lock:
                offset = state.load(self.state_file).get("offset") or 0
            try:
                self._polling = True
                updates = api.get_updates(offset, POLL_TIMEOUT)
                backoff, self._conflict_said = 2.0, False
                if self.error.startswith(("Another program", "Telegram couldn't")):
                    self.error = ""
            except NetworkError as exc:
                self.error = tg.why(exc)
                self.stop_event.wait(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            except ApiError as exc:
                self.error = tg.why(exc)
                if exc.code == 409:
                    if not self._conflict_said:
                        self.log(f"⚠ {self.error}")
                        self._conflict_said = True
                    self.stop_event.wait(30.0)
                elif exc.code in (401, 404):
                    self.log(f"⚠ {self.error}")
                    self.api, self._retry_at = None, self.now() + 3600.0  # until the token changes
                else:
                    self.stop_event.wait(10.0)
                continue
            if self.stop_event.is_set():
                break
            last = offset
            for u in updates:
                last = max(last, int(u.get("update_id") or 0) + 1)
                try:
                    self.handle(u)
                except Exception as exc:  # noqa: BLE001 - one bad message never stops the bot
                    self.log(f"⚠ Couldn't handle a message: {type(exc).__name__}: {exc}")
            if last != offset:
                with self.lock, state.edit(self.state_file) as data:
                    data["offset"] = last
        self._polling = False

    def _tell_loop(self) -> None:
        while not self.stop_event.is_set():
            self.wake.wait(2.0)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            try:
                self.tell_once()
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠ {type(exc).__name__}: {exc}")
                self.stop_event.wait(10.0)

    def tell_once(self) -> None:
        """Send what's waiting, and look for videos to show (each CHECK_EVERY seconds)."""
        now = self.now()
        if self.api is not None:
            while True:
                try:
                    kind, text, buttons = self.outbox.get_nowait()
                except queue.Empty:
                    break
                self.broadcast(text, buttons, kind)
            if now - self._last["check"] >= CHECK_EVERY:
                self._last["check"] = now
                self.check(now)
            if now - self._last["dashboard"] >= DASHBOARD_EVERY:
                self._last["dashboard"] = now
                self.update_dashboards()
        if now - self._last["prune"] >= PRUNE_EVERY:
            self._last["prune"] = now
            self.prune(now)
        self._write_status()

    def _write_status(self, force: bool = False, polling: bool | None = None) -> None:
        now = self.now()
        if not force and now - self._last["status"] < STATUS_EVERY:
            return
        self._last["status"] = now
        import os

        state.write_status({"pid": os.getpid(), "at": now, "polling": self._polling if polling is None else polling,
                            "bot": self.me.get("username", ""), "bot_id": self.me.get("id"), "error": self.error},
                           self.status_file)

    # --- what others tell it ------------------------------------------------------------------------------

    def queue_message(self, kind: str, text: str, buttons: dict | None = None) -> None:
        """Send `text` (HTML) to the linked chats, if Send me includes `kind`. While the bot is off (no token,
        or not on yet) nothing is kept, so turning it on later doesn't bring a pile of old news."""
        if self.api is None or self.outbox.qsize() >= OUTBOX_MAX:
            return
        self.outbox.put((kind, text, buttons))
        self.wake.set()

    def on_event(self, account, kind: str, text: str, alert: bool = False, item=None) -> None:
        """A line from posting's feed (posting/service.py): a post going up, a problem, a milestone."""
        send = EVENT_SENDS.get(kind)
        if not send or (kind in ("milestone", "hot") and not alert):
            return
        buttons = None
        if item is not None and getattr(item, "tiktok_url", ""):
            buttons = inline([[("▶ Open on TikTok", item.tiktok_url)]])
        self.queue_message(send, esc(text), buttons)

    def problem(self, text: str) -> None:
        """Something automation needs you for (pipeline.Runner.alert)."""
        self.queue_message("problems", esc(text))

    def send_week(self, w) -> None:
        self.queue_message("weekly", week_html(w), inline([[("📊 Status", "st")]]))

    def broadcast(self, text: str, buttons: dict | None = None, kind: str = "") -> int:
        """To every linked chat (that hasn't blocked the bot). Returns how many it reached."""
        if kind and kind not in self.sends():
            return 0
        with self.lock:
            chats = [c for c in state.load(self.state_file)["chats"] if not c.get("blocked")]
        sent = 0
        for c in chats:
            if self.say(c["id"], text, buttons) is not None:
                sent += 1
        return sent

    def say(self, chat: int, text: str, buttons: dict | None = None) -> dict | None:
        try:
            return self.api.send(chat, text, buttons)
        except ApiError as exc:
            if exc.code == 403:
                self._blocked(chat)
            else:
                self.log(f"⚠ Couldn't send a message: {exc}")
        except NetworkError as exc:
            self.log(f"⚠ Couldn't send a message: {exc}")
        return None

    def _blocked(self, chat: int) -> None:
        with self.lock, state.edit(self.state_file) as data:
            for c in data["chats"]:
                if c["id"] == chat:
                    c["blocked"] = True
        self.log(f"Chat {chat} blocked the bot: nothing is sent there until it writes again.")

    # --- posting's library ----------------------------------------------------------------------------------

    def posting(self):
        """(store, accounts, service or None): automation's posting worker's, so changes are seen at once."""
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

    def save(self, store) -> None:
        try:
            store.save()
        except (OSError, TimeoutError) as exc:
            self.log(f"⚠ Couldn't save the posting library: {exc}")

    # --- handling what you send -----------------------------------------------------------------------------

    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self.on_press(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    def on_message(self, msg: dict) -> None:
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return  # only a person's own chat with the bot can be linked
        chat_id = int(chat["id"])
        text = (msg.get("text") or msg.get("caption") or "").strip()  # a link shared with a picture too
        now = self.now()
        with self.lock:
            data = state.load(self.state_file)
            linked = state.is_linked(data, chat_id)
        if not linked:
            self.on_stranger(chat, text, now)
            return
        if any(c["id"] == chat_id and c.get("blocked") for c in data["chats"]):
            with self.lock, state.edit(self.state_file) as d:
                for c in d["chats"]:
                    if c["id"] == chat_id:
                        c.pop("blocked", None)
        cmd, arg = split_command(text)
        if not cmd and text in BUTTONS:
            cmd, arg = BUTTONS[text], ""
        if cmd:
            self.modes.pop(chat_id, None)
        elif chat_id in self.modes:
            mode, until = self.modes.pop(chat_id)
            if now < until:
                cmd, arg = mode, text
        handler = {"start": self.cmd_help, "help": self.cmd_help, "menu": self.cmd_help, "status": self.cmd_status,
                   "videos": self.cmd_videos, "make": self.cmd_make, "clip": self.cmd_clip,
                   "request": self.cmd_request, "requests": self.cmd_requests, "pause": self.cmd_pause,
                   "resume": self.cmd_resume, "toggle": self.cmd_toggle, "week": self.cmd_week,
                   "dashboard": self.cmd_dashboard}.get(cmd)
        try:
            if handler is not None:
                handler(chat_id, arg)
            elif cmd:
                self.say(chat_id, f"I don't know /{esc(cmd)}. /help says what I can do.")
            elif URL.search(text):
                self.cmd_clip(chat_id, text)
            elif text:
                token = self._ask("idea", chat_id, text=text)
                self.say(chat_id, f"What should I do with “{esc(trim(text, 120))}”?", inline([
                    [("✨ Make a video about it", f"idea:{token}:make")],
                    [("📋 Ask for clips about it", f"idea:{token}:request")],
                    [("✖ Nothing", f"x:{token}")]]))
            elif any(k in msg for k in MEDIA):  # a photo, a sticker, a voice note…
                self.say(chat_id, "I can only read words and links. /help says what I can do.")
        except Exception as exc:  # noqa: BLE001 - say so, rather than go quiet
            self.log(f"⚠ Couldn't do “{trim(text, 60)}”: {type(exc).__name__}: {exc}")
            self.say(chat_id, f"⚠ Something went wrong on the PC: {esc(trim(str(exc) or type(exc).__name__, 300))}")

    def on_stranger(self, chat: dict, text: str, now: float) -> None:
        """A chat that isn't linked: the pairing code links it; anything else gets how to link, now and then."""
        chat_id = int(chat["id"])
        with self.lock, state.edit(self.state_file) as data:
            said = stranger(data, chat, text, now)
        if said == "linked":
            self.log(f"Linked {state.chat_name(chat)}'s chat.")
            self.say(chat_id, welcome_html(), tg.keyboard(MENU))
        elif said in STRANGER_REPLIES:
            self.say(chat_id, STRANGER_REPLIES[said])

    def on_press(self, q: dict) -> None:
        msg = q.get("message") or {}
        chat_id = int((msg.get("chat") or {}).get("id") or 0)
        with self.lock:
            linked = state.is_linked(state.load(self.state_file), chat_id)
        if not linked:
            self.api.answer(q.get("id", ""), "This chat isn't linked to ClipMonster.", alert=True)
            return
        data = str(q.get("data") or "")
        kind, _, arg = data.partition(":")
        mid = int(msg.get("message_id") or 0)
        video = "video" in msg
        handler = {"v": self.press_video, "ok": self.press_ok, "del": self.press_delete, "DEL": self.press_deleted,
                   "keep": self.press_keep, "now": self.press_now, "again": self.press_again, "list": self.press_list,
                   "st": self.press_status, "pause": self.press_pause, "resume": self.press_resume,
                   "mk": self.press_make, "cl": self.press_clip, "rq": self.press_request, "idea": self.press_idea,
                   "x": self.press_nothing, "cancel": self.press_cancel, "CANCEL": self.press_cancelled,
                   "job": self.press_job, "week": self.press_week, "pin": self.press_pin,
                   "reqs": self.press_requests}.get(kind)
        if handler is None:
            self.api.answer(q.get("id", ""), "That button is from an older version: send /help.")
            return
        try:
            note = handler(chat_id, mid, arg, video)
        except Exception as exc:  # noqa: BLE001 - say so, rather than leave the button spinning
            self.log(f"⚠ A button didn't work ({kind}): {type(exc).__name__}: {exc}")
            self.api.answer(q.get("id", ""), "Something went wrong on the PC: "
                            + trim(str(exc) or type(exc).__name__, 150), alert=True)
            return
        self.api.answer(q.get("id", ""), note or "")

    # --- asking with buttons ------------------------------------------------------------------------------

    def _ask(self, kind: str, chat: int, **what) -> str:
        now = self.now()
        for k in [k for k, v in self.pending.items() if now - v["at"] > PENDING_LIFE]:
            del self.pending[k]
        token = secrets.token_hex(4)
        self.pending[token] = {"kind": kind, "chat": chat, "at": now, **what}
        return token

    def _take(self, token: str, chat: int) -> dict | None:
        p = self.pending.get(token)
        if p is None or p["chat"] != chat or self.now() - p["at"] > PENDING_LIFE:
            return None
        return self.pending.pop(token)

    def _categories(self, s=None) -> list:
        from .. import categories

        return [c for c in categories.load(s or self.settings()) if c.enabled]

    def _category_buttons(self, kind: str, token: str, cats: list) -> dict:
        rows, row = [], []
        for i, c in enumerate(cats[:24]):
            label = trim(c.name, 28) + (" (paused)" if c.paused else " (no account)" if c.needs_account else "")
            row.append((label, f"{kind}:{token}:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        rows.append(row)
        rows.append([("No category", f"{kind}:{token}:-"), ("✖ Cancel", f"x:{token}")])
        return inline(rows)

    def _picked(self, token: str, chat: int, choice: str):
        """The pending ask and the category picked for it (None for "No category"); (None, None) if it's gone."""
        p = self._take(token, chat)
        if p is None:
            return None, None
        cats = p.get("cats") or []
        cat = None
        if choice != "-":
            try:
                cid = cats[int(choice)]
            except (ValueError, IndexError):
                return p, None
            cat = next((c for c in self._categories() if c.id == cid), None)
        return p, cat

    def _remember(self, key: str, chat: int, title: str, kind: str) -> None:
        with self.lock, state.edit(self.state_file) as data:
            data["asked"][key] = {"chat": chat, "at": self.now(), "title": title, "kind": kind}

    # --- commands --------------------------------------------------------------------------------------------

    def cmd_help(self, chat: int, arg: str = "") -> None:
        self.say(chat, help_html(), tg.keyboard(MENU))

    def cmd_status(self, chat: int, arg: str = "") -> None:
        self.say(chat, self.status_html(), self.status_buttons())

    def status_buttons(self, pin: bool = True) -> dict:
        paused = self.runner.db.meta("paused") == "1"
        rows = [[("🎬 Videos", "list:0"), ("▶ Resume" if paused else "⏸ Pause", "resume" if paused else "pause"),
                 ("🔄 Refresh", "st")]]
        if pin:
            rows.append([("📌 Keep this up to date", "pin")])
        return inline(rows)

    def status_html(self) -> str:
        from ..posting import jobs as pjobs
        from ..posting import schedule

        db = self.runner.db
        s = self.settings()
        now = self.now()
        lines = [f"<b>ClipMonster</b> · {esc(pc_name())} · {time.strftime('%H:%M', time.localtime(now))}"]
        job = db.working()
        state_text = db.meta("state")
        if db.meta("paused") == "1":
            lines.append("⏸ <b>Paused.</b> Nothing new is clipped or made until you resume. Posting carries on.")
        elif job is not None:
            bar = job.status_bar
            lines.append(f"● {'Making' if job.is_creation else 'Clipping'} “{esc(trim(job.title or job.url, 70))}”"
                         + (f"\n    {esc(bar.summary())}" if bar else ""))
        elif state_text:
            lines.append(f"● {esc(trim(state_text, 160))}")
        waiting = [j for j in db.jobs(limit=3000) if j.status == "queued"]
        made = sum(1 for j in waiting if j.is_creation)
        bits = []
        if len(waiting) - made:
            bits.append(f"{plural(len(waiting) - made, 'video')} to clip")
        if made:
            bits.append(f"{plural(made, 'Creator video')} to make")
        lines.append("📝 " + (" · ".join(bits) if bits else "Nothing waiting to clip or make"))
        open_orders = db.orders(open_only=True)
        if open_orders:
            o = open_orders[0]
            lines.append(f"📋 {plural(len(open_orders), 'request')} open" + (f": {esc(trim(o.message, 90))}"
                                                                           if o.message else ""))
        try:
            store, accounts, _svc = self.posting()
            q = [i for i in store.queue() if i.status == "queued"]
            review = store.waiting_review()
            midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
            today = store.posts_since(midnight)
            post = f"📤 {plural(len(q), 'video')} waiting to post"
            nxt = pjobs.next_post(store)
            if nxt is not None and nxt.planned_at:
                a = accounts.get(nxt.account)
                post += f" · next on {esc(a.handle if a else '?')} {esc(schedule.fmt_when(nxt.planned_at, now))}"
            lines.append(post + f" · {today} posted today")
            if review:
                lines.append(f"⏸ {plural(len(review), 'video waits', 'videos wait')} for your OK: /videos")
            out = [a.handle for a in accounts if a.autopilot and not a.logged_in]
            if out:
                lines.append(f"⚠ Not signed in to TikTok: {esc(', '.join(out))} (Accounts tab on the PC)")
        except Exception as exc:  # noqa: BLE001 - the rest of the status still helps
            lines.append(f"📤 Posting's library couldn't be read ({esc(exc)})")
        try:
            free, drive = self.runner.free_space(s)
            if free < (1 << 60):
                low = s.disk_min_free_gb and free < s.disk_min_free_gb * GB
                lines.append(f"{'⚠' if low else '💽'} {free / GB:.0f} GB free on {esc(drive)}")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

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

    def cmd_make(self, chat: int, idea: str = "") -> None:
        idea = " ".join(idea.split())
        if not idea:
            self.modes[chat] = ("make", self.now() + MODE_LIFE)
            self.say(chat, "What should the video be about? Send the idea, like <i>the real story of the "
                           "Bermuda Triangle</i>.")
            return
        cats = self._categories()
        if not cats:
            self.make(chat, idea, None)
            return
        token = self._ask("make", chat, idea=idea, cats=[c.id for c in cats[:24]])
        self.say(chat, f"✨ A video about “{esc(trim(idea, 200))}”. Which category is it for?",
                 self._category_buttons("mk", token, cats))

    def make(self, chat: int, idea: str, cat) -> str:
        from ..creator.orchestrator import CreateRequest, queue as queue_creation

        db = self.runner.db
        req = CreateRequest(prompt=idea, category=cat.id if cat else "")
        job_id = queue_creation(db, req, cat.id if cat else "", self.now(), origins=["creator", "telegram"])
        self._remember(f"job:{job_id}", chat, req.label(), "make")
        db.set_meta("nudge", "1")
        self.log(f"Asked for a video about “{trim(idea, 80)}”" + (f" for {cat.name}" if cat else "") + ".")
        when = self._when(cat)
        text = (f"✨ Got it: a video about “{esc(trim(idea, 200))}”" + (f" for {esc(cat.name)}" if cat else "")
                + f". {when} I'll send it here when it's made" + (", for your OK before it posts."
                                                                  if self._held(cat) else "."))
        self.say(chat, text)
        return text

    def _held(self, cat) -> bool:
        """Will the video wait for your OK (Settings → Creator → Check videos before they post)?"""
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

    def _when(self, cat=None, mine: int = 1) -> str:
        """When what was just queued (`mine` jobs) will start, roughly."""
        from ..pipeline import hours_wait

        s = self.settings()
        db = self.runner.db
        now = self.now()
        ahead = max(0, sum(1 for j in db.jobs(limit=3000) if j.status == "working"
                           or (j.status == "queued" and j.next_try <= now)) - mine)
        if cat is not None and cat.paused:
            return f"{esc(cat.name)} is paused, so it waits until you resume it (Automation → Categories)."
        if cat is not None and cat.needs_account:
            return (f"{esc(cat.name)} has no TikTok account yet, so it waits until you pick one (Categories tab on "
                    "the PC).")
        if db.meta("paused") == "1":
            return "Automation is paused: it starts once you /resume."
        wait = hours_wait(now, s.auto_start_hour, s.auto_end_hour)
        if wait:
            return f"It starts at {s.auto_start_hour}:00, when working hours begin."
        return "It's next." if not ahead else f"{plural(ahead, 'video is', 'videos are')} ahead of it."

    def cmd_clip(self, chat: int, text: str = "") -> None:
        links = URL.findall(text or "")
        if not links:
            self.modes[chat] = ("clip", self.now() + MODE_LIFE)
            self.say(chat, "Send the YouTube link (or several, one after another in one message).")
            return
        cats = self._categories()
        if not cats:
            self.clip(chat, links, None)
            return
        token = self._ask("clip", chat, links=links[:10], cats=[c.id for c in cats[:24]])
        self.say(chat, f"✂️ Clip {plural(len(links[:10]), 'video')}. Which category are the clips for?",
                 self._category_buttons("cl", token, cats))

    def clip(self, chat: int, links: list[str], cat) -> str:
        from ..download import is_url
        from ..pipeline import _video_id

        added, before, bad = [], [], []
        for url in links:
            url = url.rstrip(").,>]")
            if not is_url(url):
                bad.append(url)
                continue
            try:
                if self.runner.add_manual(url, category_id=cat.id if cat else ""):
                    added.append(url)
                    self._remember(f"job:{_video_id(url)}", chat, url, "clip")
                else:
                    before.append(url)
            except ValueError:
                bad.append(url)
        if added:
            self.runner.db.set_meta("nudge", "1")
            self.log(f"Asked to clip {plural(len(added), 'video')}" + (f" for {cat.name}" if cat else "") + ".")
        lines = []
        if added:
            lines.append(f"✂️ Queued {plural(len(added), 'video')} to clip" + (f" for {esc(cat.name)}" if cat else "")
                         + f". {self._when(cat, len(added))} I'll send the clips here when they're made.")
        if before:
            lines.append(f"{plural(len(before), 'video was', 'videos were')} clipped before (or already waiting), so "
                         "not queued again. To clip one again: /request 3 " + esc(before[0]))
        if bad:
            lines.append("These aren't video links: " + esc(", ".join(trim(b, 60) for b in bad[:3])))
        text = "\n".join(lines) or "Nothing to clip."
        self.say(chat, text)
        return text

    def cmd_request(self, chat: int, text: str = "") -> None:
        if not text.strip():
            self.modes[chat] = ("request", self.now() + MODE_LIFE)
            self.say(chat, "Say what you'd like, like <i>10 clips 30-45s Gaming, only clutch plays</i>: how many, "
                           "how long, the category, links if you have them, and anything special for the AI.")
            return
        cats = self._categories()
        try:
            want = parse_request(text, cats)
        except ValueError as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return
        if want["category"] is None and cats:
            token = self._ask("request", chat, want={**want, "category": None}, cats=[c.id for c in cats[:24]])
            self.say(chat, f"📋 {self._request_line(want)}. Which category are they for?",
                     self._category_buttons("rq", token, cats))
            return
        self.request(chat, want, want["category"])

    def _request_line(self, want: dict, cat=None) -> str:
        n = want["count"]
        length = want["length"]
        where = f" for {esc(cat.name)}" if cat else ""
        text = f"{plural(n, 'clip')}" + (f" of {length[0]:g}–{length[1]:g} s" if length else "") + where
        if want["links"]:
            text += f" from {plural(len(want['links']), 'link')}"
        if want["notes"]:
            text += f": “{esc(trim(want['notes'], 160))}”"
        return text

    def request(self, chat: int, want: dict, cat) -> str:
        from .. import orders, pipeline
        from ..jobs import Job
        from ..research import youtube_key

        s = self.settings()
        length = want["length"]
        if length is None:
            style = pipeline.settings_for(Job(id="", url="", category_id=cat.id if cat else ""), s)[0]
            length = (style.min_len, style.max_len)
        if not want["links"] and not youtube_key(s):
            text = ("⚠ To pick videos by itself, automation needs a YouTube Data API key (Settings → Research). "
                    "Send links with the request instead.")
            self.say(chat, text)
            return text
        try:
            order = orders.make(want["count"], length[0], length[1], category_id=cat.id if cat else "",
                                links=want["links"], notes=want["notes"], now=self.now())
        except ValueError as exc:
            text = f"⚠ {esc(exc)}"
            self.say(chat, text)
            return text
        order = self.runner.place(order)
        self._remember(f"order:{order.id}", chat, order.title(cat.name if cat else ""), "request")
        lines = [f"📋 Asked for {self._request_line({**want, 'length': length}, cat)}. I'll tell you when they're "
                 "ready."]
        if want["notes"] and not pipeline.has_ai_key(s):
            lines.append("⚠ Special requests need a Claude or Gemini key (Settings → AI): without one, smart signals "
                         "pick the clips and the special requests aren't used.")
        if cat is not None and cat.paused:
            lines.append(f"⚠ {esc(cat.name)} is paused, so nothing happens until you resume it.")
        text = "\n".join(lines)
        self.say(chat, text, inline([[("📋 Requests", "reqs")]]))
        return text

    def cmd_requests(self, chat: int, arg: str = "") -> None:
        text, buttons = self.requests_html()
        self.say(chat, text, buttons)

    def requests_html(self) -> tuple[str, dict]:
        from .. import categories, orders

        db = self.runner.db
        s = self.settings()
        mine = db.orders()[:8]
        if not mine:
            return ("No requests yet. Ask for clips like <i>/request 10 30-45s Gaming funny reactions</i>, or send a "
                    "link to clip it.", inline([[("➕ New request", "rq:new:-")]]))
        lines = ["<b>📋 Your requests</b>"]
        rows = []
        for o in mine:
            name = categories.name_of(s, o.category_id)
            mark = {"open": "⏳", "done": "✅", "short": "⚠", "cancelled": "✖"}.get(o.status, "•")
            lines.append(f"{mark} {esc(o.title(name))}: {esc(orders.STATUS_WORDS.get(o.status, o.status))}"
                         + (f". {esc(trim(o.message, 120))}" if o.message else "")
                         + (f"\n    “{esc(trim(o.notes, 100))}”" if o.notes else ""))
            if o.status == "open":
                rows.append([(f"✖ Cancel: {trim(o.title(name), 40)}", f"cancel:{o.id}")])
        rows.append([("➕ New request", "rq:new:-")])
        return "\n".join(lines), inline(rows)

    def cmd_pause(self, chat: int, arg: str = "") -> None:
        self.runner.db.set_meta("paused", "1")
        self.log("Paused from Telegram.")
        self.say(chat, "⏸ Paused. Nothing new is clipped or made until you /resume (a video already being made "
                       "finishes). Posting carries on.", inline([[("▶ Resume", "resume")]]))

    def cmd_resume(self, chat: int, arg: str = "") -> None:
        self.runner.db.set_meta("paused", "0")
        self.runner.db.set_meta("nudge", "1")
        self.log("Resumed from Telegram.")
        self.say(chat, "▶ Carrying on.", inline([[("📊 Status", "st")]]))

    def cmd_toggle(self, chat: int, arg: str = "") -> None:
        (self.cmd_resume if self.runner.db.meta("paused") == "1" else self.cmd_pause)(chat)

    def cmd_week(self, chat: int, arg: str = "") -> None:
        self.api.typing(chat)
        self.say(chat, self.week_now())

    def week_now(self) -> str:
        from .. import weekly

        store, accounts, _svc = self.posting()
        w = weekly.build(store, accounts, self.runner.db, self.now(), weekly.Record())
        return week_html(w)

    def cmd_dashboard(self, chat: int, arg: str = "") -> None:
        if arg.strip().lower() in ("off", "stop"):
            with self.lock, state.edit(self.state_file) as data:
                for c in data["chats"]:
                    if c["id"] == chat:
                        c.pop("dashboard", None)
            self.say(chat, "The dashboard stops updating.")
            return
        sent = self.say(chat, self.status_html(), self.status_buttons(pin=False))
        if sent:
            self._pin(chat, int(sent.get("message_id") or 0))

    def _pin(self, chat: int, message: int) -> None:
        with self.lock, state.edit(self.state_file) as data:
            for c in data["chats"]:
                if c["id"] == chat:
                    c["dashboard"] = message
        try:
            self.api.call("pinChatMessage", {"chat_id": chat, "message_id": message, "disable_notification": True})
        except (ApiError, NetworkError):
            pass

    def update_dashboards(self) -> None:
        with self.lock:
            chats = [c for c in state.load(self.state_file)["chats"] if c.get("dashboard") and not c.get("blocked")]
        if not chats:
            return
        text = self.status_html()
        for c in chats:
            try:
                self.api.edit(c["id"], int(c["dashboard"]), text, self.status_buttons(pin=False))
            except ApiError as exc:
                if exc.gone:
                    with self.lock, state.edit(self.state_file) as data:
                        for d in data["chats"]:
                            if d["id"] == c["id"]:
                                d.pop("dashboard", None)
            except NetworkError:
                return

    # --- buttons ---------------------------------------------------------------------------------------------

    def press_status(self, chat, mid, arg, video) -> str:
        try:
            self.api.edit(chat, mid, self.status_html(), self.status_buttons(pin=not self._is_dashboard(chat, mid)))
        except ApiError:
            self.cmd_status(chat)
        return "Up to date"

    def _is_dashboard(self, chat: int, mid: int) -> bool:
        with self.lock:
            return any(c["id"] == chat and c.get("dashboard") == mid for c in state.load(self.state_file)["chats"])

    def press_pin(self, chat, mid, arg, video) -> str:
        self._pin(chat, mid)
        try:
            self.api.edit_buttons(chat, mid, self.status_buttons(pin=False))
        except ApiError:
            pass
        return "This message keeps itself up to date (every 2 min)"

    def press_pause(self, chat, mid, arg, video) -> str:
        self.runner.db.set_meta("paused", "1")
        self.log("Paused from Telegram.")
        self.press_status(chat, mid, "", video)
        return "Paused"

    def press_resume(self, chat, mid, arg, video) -> str:
        self.runner.db.set_meta("paused", "0")
        self.runner.db.set_meta("nudge", "1")
        self.log("Resumed from Telegram.")
        if mid and not video:
            self.press_status(chat, mid, "", video)
        return "Carrying on"

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
        self.log(f"OK'd “{trim(it.headline, 80)}” to post.")
        self.refresh_card(chat, mid, it, video)
        self.settle(it.id, skip=(chat, mid))
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
        preview.drop(it.id, self.previews)
        from ..posting.records import size_text

        freed = f" ({size_text(r.freed)} freed)" if r.freed else ""
        if missing:
            self.log(f"🗑 Took “{trim(it.headline, 80)}” off the list (its file was gone already).")
            note = "🗑 <b>Taken off the list</b> (its file was gone from the PC already)."
        else:
            self.log(f"🗑 Deleted “{trim(it.headline, 80)}”{freed}.")
            note = f"🗑 <b>Deleted from the PC</b>{esc(freed)}."
        if r.failed:
            note += f"\n⚠ {plural(len(r.failed), 'file')} couldn't go: {esc(trim(r.failed[0][1], 100))}"
        self._mark(chat, mid, it, note, video)
        self.settle(it.id, skip=(chat, mid), gone=note)
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
        self.log(f"Asked to post “{trim(it.headline, 80)}” to {a.handle} now.")
        self._mark(chat, mid, it, f"🚀 Posting it to {esc(a.handle)} now…", video)
        self.settle(it.id, skip=(chat, mid))  # its other cards, and this one stays until the upload starts
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
        return "It's queued again"

    def press_make(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send /make again"
        text = self.make(chat, p["idea"], cat)
        self._settle_question(chat, mid, text)
        return ""

    def press_clip(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send the link again"
        text = self.clip(chat, p["links"], cat)
        self._settle_question(chat, mid, text)
        return ""

    def press_request(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        if token == "new":
            self.cmd_request(chat, "")
            return ""
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send the request again"
        text = self.request(chat, p["want"], cat)
        self._settle_question(chat, mid, text)
        return ""

    def press_idea(self, chat, mid, arg, video) -> str:
        token, _, what = arg.partition(":")
        p = self._take(token, chat)
        if p is None:
            return "That question has expired: send it again"
        try:
            self.api.edit_buttons(chat, mid, None)
        except ApiError:
            pass
        if what == "make":
            self.cmd_make(chat, p["text"])
        else:
            self.cmd_request(chat, p["text"])
        return ""

    def press_nothing(self, chat, mid, arg, video) -> str:
        self.pending.pop(arg, None)
        try:
            self.api.edit_buttons(chat, mid, None)
        except ApiError:
            pass
        return "OK, nothing"

    def _settle_question(self, chat: int, mid: int, text: str) -> None:
        """The question's buttons go once it's answered (the answer is its own message)."""
        try:
            self.api.edit_buttons(chat, mid, None)
        except ApiError:
            pass

    def press_requests(self, chat, mid, arg, video) -> str:
        self.cmd_requests(chat)
        return ""

    def press_cancel(self, chat, mid, arg, video) -> str:
        o = self.runner.db.get_order(arg)
        if o is None or o.status != "open":
            return "It isn't open any more"
        buttons = inline([[("✖ Yes, cancel it", f"CANCEL:{o.id}"), ("↩ Keep it", "x:-")]])
        self.say(chat, f"Cancel the request for {esc(o.title())}? Its videos still waiting leave the queue (one "
                       "being clipped finishes).", buttons)
        return ""

    def press_cancelled(self, chat, mid, arg, video) -> str:
        o = self.runner.db.get_order(arg)
        if o is None or o.status != "open":
            return "It isn't open any more"
        n = self.runner.cancel(o.id)
        with self.lock, state.edit(self.state_file) as data:
            data["asked"].pop(f"order:{o.id}", None)
        self.log(f"Cancelled a request from Telegram ({o.title()}).")
        try:
            self.api.edit(chat, mid, f"✖ Cancelled. {plural(n, 'waiting video')} left the queue.")
        except ApiError:
            pass
        return "Cancelled"

    def press_job(self, chat, mid, arg, video) -> str:
        items = self._items_of_job(arg)
        if not items:
            return "Those clips aren't in the library any more"
        text, buttons = digest_html("✂️ The clips", items, self.posting()[1], self.now())
        self.say(chat, text, buttons)
        return ""

    def press_week(self, chat, mid, arg, video) -> str:
        self.cmd_week(chat)
        return ""

    def _recycle(self) -> bool:
        from ..posting.config import Settings as PostingSettings

        try:
            return bool(PostingSettings.load().recycle)
        except Exception:  # noqa: BLE001
            return False

    # --- a video's card ----------------------------------------------------------------------------------------

    def card(self, it) -> tuple[str, dict | None]:
        store, accounts, svc = self.posting()
        return card_html(it, accounts, self.now()), card_buttons(it, accounts, svc)

    def show(self, chat: int, it, note: str = "") -> dict | None:
        """Send the video itself with its card (its caption and buttons). Remembered, so the card follows when
        the video is OK'd, posted or deleted somewhere else. Use show_later from a chat's button: an upload
        can take a while."""
        text, buttons = self.card(it)
        if note:
            text = f"{note}\n\n{text}"
        if not it.exists():
            sent = self.say(chat, "⚠ The video file isn't on the PC any more.\n\n" + text,
                            inline([[("🗑 Take it off the list", f"DEL:{it.id}")]]))
            return self._shown(it, chat, sent, video=False)
        with self._busy(chat, "upload_video"):  # "sending a video…" at the top of the chat meanwhile
            try:
                send = preview.for_telegram(Path(it.path), it.id, self.previews)
            except (preview.TooBig, RuntimeError, OSError) as exc:
                return self._shown(it, chat, self.say(chat, f"⚠ {esc(exc)}\n\n" + text, buttons), video=False)
            if send.copy:
                text += "\n<i>A smaller copy, to fit Telegram. The one that posts is full quality.</i>"
            try:
                sent = self.api.send_video(chat, send.path, text, buttons, send.width, send.height, send.duration)
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

    def show_later(self, chat: int, it, note: str = "") -> bool:
        """show(), from the thread that sends videos, so the chat keeps answering during a long upload (and
        right away when the bot isn't running its threads, as in tests). False if it's on its way already."""
        key = (chat, it.id)
        with self._sending_lock:
            if key in self.sending:
                return False
            self.sending.add(key)
        if self.uploading():
            self.uploads.put((chat, it.id, note))
            return True
        try:
            self.show(chat, it, note)
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
                chat, item_id, note = self.uploads.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                it = self.posting()[0].get(item_id)
                if it is not None and not self.stop_event.is_set():
                    self.show(chat, it, note)
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
        text = card_html(it, self.posting()[1], self.now()) + f"\n\n{note}"
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
                text, buttons = card_html(it, accounts, self.now()), card_buttons(it, accounts, svc)
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

    # --- looking for things to tell ------------------------------------------------------------------------------

    def check(self, now: float) -> None:
        with self.lock:
            data = state.load(self.state_file)
        if not data["chats"]:
            if data["seen_at"] or data["noted"]:  # no one to tell: what comes in meanwhile isn't news later
                with self.lock, state.edit(self.state_file) as d:
                    d["seen_at"], d["noted"] = 0.0, {}
            return
        store, accounts, _svc = self.posting()
        answered = self.check_asked(data, store, accounts, now)
        sends = self.sends()
        with store.lock:
            items = list(store.items.values())
        held = [i for i in items if i.review == "waiting" and i.status == "queued"]
        held_ids = {i.id for i in held}
        seen_at = float(data.get("seen_at") or 0)
        if not seen_at:
            self.first_look(items, held, sends, data, now)
            return
        shown, noted = data["shown"], data["noted"]
        if "review" in sends:
            fresh = sorted((i for i in held if i.id not in shown and i.id not in noted and i.id not in answered),
                           key=lambda i: i.added_at)
            for it in fresh[:REVIEW_PER_LOOK]:  # the rest on the next looks (each is an upload)
                self.show_all(it, "⏸ <b>This waits for your OK before it posts.</b>")
        new = [i for i in items if i.added_at > seen_at and i.id not in answered and i.id not in shown
               and i.status in ("queued", "new") and not ("review" in sends and i.id in held_ids)]
        if new and "new" in sends:
            groups = group_new(new)
            if len(groups) > NEW_GROUPS_MAX:  # a lot came in at once (automation was off a while): one message
                text, buttons = catch_up_html(new)
                self.broadcast(text, buttons)
            else:
                for title, group in groups:
                    text, buttons = digest_html(title, group, accounts, now)
                    self.broadcast(text, buttons)
        newest = float(max([seen_at] + [i.added_at for i in items if i.added_at > seen_at]))
        if newest != seen_at:
            with self.lock, state.edit(self.state_file) as d:
                d["seen_at"] = newest
        # cards whose video changed meanwhile (OK'd in the window, posted, deleted)
        for item_id, entry in list(shown.items()):
            it = store.get(item_id)
            if it is None or card_state(it) != entry.get("was"):
                self.settle(item_id)

    def first_look(self, items: list, held: list, sends: set[str], data: dict, now: float) -> None:
        """The first look since a phone was linked: only what comes in from now on is news, and the videos
        already waiting for your OK are one message (not an upload each)."""
        waiting = [i for i in held if i.id not in data["shown"]] if "review" in sends else []
        with self.lock, state.edit(self.state_file) as d:
            d["seen_at"] = float(max([i.added_at for i in items] + [now - 60]))
            for i in waiting:
                d["noted"][i.id] = now
        if waiting:
            self.broadcast(f"⏸ <b>{plural(len(waiting), 'video waits', 'videos wait')} for your OK</b> before "
                           f"{'it posts' if len(waiting) == 1 else 'they post'}. Tap to watch "
                           f"{'it' if len(waiting) == 1 else 'them'}, then OK or delete.",
                           inline([[("🎬 Watch them" if len(waiting) > 1 else "🎬 Watch it", "list:0")]]))

    def show_all(self, it, note: str = "") -> None:
        with self.lock:
            chats = [c["id"] for c in state.load(self.state_file)["chats"] if not c.get("blocked")]
        for chat in chats:
            self.show_later(chat, it, note)

    def check_asked(self, data: dict, store, accounts, now: float) -> set[str]:
        """Answer what was asked for from the chat, once it's done. Returns the videos sent that way."""
        answered: set[str] = set()
        db = self.runner.db
        done: list[str] = []
        for key, ask in list(data["asked"].items()):
            chat = int(ask.get("chat") or 0)
            kind, _, ident = key.partition(":")
            text, buttons, items = "", None, []
            if kind == "job":
                job = db.get(ident)
                if job is None:
                    text = f"“{esc(trim(ask.get('title', ''), 80))}” was taken out of the queue."
                elif job.status == "failed":
                    what = "make" if job.is_creation else "clip"
                    text = (f"✖ Couldn't {what} “{esc(trim(job.title or ask.get('title', ''), 80))}”: "
                            f"{esc(trim(job.error, 300))}")
                elif job.status == "done":
                    items = self._items_of_job(ident, store)
                    if not items and now - float(job.updated or now) < 15 * 60:
                        continue  # posting brings the video in within a couple of minutes
                    if not items:
                        n = job.clips
                        text = (f"✓ Done: {plural(n, 'clip')} from “{esc(trim(job.title, 80))}”, in the Library "
                                "(not queued to post: its category has no TikTok account?)" if n else
                                f"Nothing good enough to post came out of “{esc(trim(job.title, 80))}”.")
                else:
                    continue
            elif kind == "order":
                o = db.get_order(ident)
                if o is None or o.status == "cancelled":
                    done.append(key)
                    continue
                if o.status == "open":
                    continue
                items = self._items_of_order(o.id, store)
                mark = "✅" if o.status == "done" else "⚠"
                text = f"{mark} Your request for {esc(ask.get('title') or o.title())}: {esc(o.message)}."
                if items:
                    buttons = digest_html("", items, accounts, now)[1]
            done.append(key)
            if not chat:
                continue
            if items and kind == "job" and len(items) == 1:
                it = items[0]
                note = ("✨ <b>Here's the video you asked for.</b>" if it.ai_generated else "✂️ <b>Your clip is ready.</b>")
                if it.review == "waiting":
                    note += " It waits for your OK before it posts."
                self.show_later(chat, it, note)
            elif items and kind == "job":
                t, b = digest_html(f"✂️ Your {plural(len(items), 'clip')} from “{trim(items[0].facts.get('source_title') or ask.get('title', ''), 60)}”",
                                   items, accounts, now)
                self.say(chat, t, b)
            elif text:
                self.say(chat, text, buttons)
            answered.update(i.id for i in items)
        if done:
            with self.lock, state.edit(self.state_file) as d:
                for key in done:
                    d["asked"].pop(key, None)
        return answered

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

    def prune(self, now: float) -> None:
        try:
            store = self.posting()[0]
            with store.lock:
                keep = {i.id for i in store.items.values() if i.status != "posted"}
            preview.prune(keep, self.previews, now)
        except Exception:  # noqa: BLE001
            pass
        with self.lock, state.edit(self.state_file) as data:
            state.tidy(data, now)


# --- what it says ----------------------------------------------------------------------------------------------


def _items_in(store, folder: Path) -> list:
    import os

    where = os.path.normcase(os.path.abspath(folder))

    def inside(path: str) -> bool:
        d = os.path.normcase(os.path.abspath(Path(path).parent))
        return d == where or d.startswith(where.rstrip(os.sep) + os.sep)

    with store.lock:
        found = [i for i in store.items.values() if i.path and i.status != "posted" and inside(i.path)]
    return sorted(found, key=lambda i: (i.part or 0, i.path))


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


def card_buttons(it, accounts, service) -> dict | None:
    from ..posting.store import INTERRUPTED

    rows = []
    if it.status == "posted":
        return inline([[("▶ Open on TikTok", it.tiktok_url)]]) if it.tiktok_url else None
    if it.status == "uploading":
        return None
    a = accounts.get(it.account) if it.account else None
    can_post = it.status == "queued" and a is not None and a.logged_in and service is not None
    if it.review == "waiting" and it.status == "queued":
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


def catch_up_html(items: list) -> tuple[str, dict]:
    """Lots of new videos at once, as one message."""
    made = sum(1 for i in items if i.ai_generated)
    clips = len(items) - made
    bits = ([plural(clips, "new clip")] if clips else []) + ([plural(made, "new Creator video")] if made else [])
    sources = len({str(Path(i.path).parent) for i in items if not i.ai_generated})
    text = f"<b>📥 {' and '.join(bits)}</b>" + (f" from {plural(sources, 'video')}" if clips and sources > 1 else "")
    return text + ". /videos lists them.", inline([[("🎬 All videos", "list:0")]])


def digest_html(title: str, items: list, accounts, now: float) -> tuple[str, dict]:
    from ..posting import schedule

    lines = [f"<b>{esc(title)}</b>"] if title else []
    rows = []
    for n, it in enumerate(items[:DIGEST_MAX], 1):
        a = accounts.get(it.account) if it.account else None
        when = schedule.fmt_when(it.planned_at, now) if it.planned_at else ""
        bits = [a.handle if a else "not queued", when, length_text(it.duration)]
        lines.append(f"{n}. {esc(trim(it.headline, 80))}\n    {esc(' · '.join(b for b in bits if b))}")
        rows.append([(trim(f"▶ {n}. {it.headline}", 60), f"v:{it.id}")])
    if len(items) > DIGEST_MAX:
        lines.append(f"…and {len(items) - DIGEST_MAX} more in /videos")
    rows.append([("🎬 All videos", "list:0")])
    return "\n".join(lines), inline(rows)


def week_html(w) -> str:
    lines = [f"<b>📅 Your week, {esc(w.span())}</b>", esc(w.headline())]
    for heading, rows in w.sections():
        lines += ["", f"<b>{esc(heading)}</b>"]
        for text, link in rows:
            lines.append("• " + (f'<a href="{attr(link)}">{esc(text)}</a>' if link else esc(text)))
    return "\n".join(lines)
