"""The Telegram bot: ClipMonster on your phone.

It runs inside automation (pipeline.Runner.run starts it next to posting) whenever Settings → Telegram is
on with a bot token, and only answers the phones linked to it (state.py). From the chat you can:

* see what automation is doing (/status, which can stay pinned as a live dashboard);
* watch the videos waiting to post, OK the ones held for your OK, post one now, or delete a bad one from the
  PC: its files go the way Clean up clears a posted clip (records.discard);
* ask for things: a Video Creator video (/make an idea), clips of a YouTube link (/clip, or just send the
  link), or a request for clips (/request 10 30-45s Gaming funny reactions). Each is sent back to you
  when it's done;
* pause and resume automation, and read the past week's report (/week);
* say what's wrong with a video the Creator or the Studio made, and the AI makes it again with that fixed
  (fix, creator/critique.py);
* share the bot with one other person (/people): two people at most, each told what the other did.

With Settings → Telegram → Mini App on, all of that is an app inside Telegram instead (webapp.py): the button
left of the message box opens it, the bot's messages carry a button into it, and videos are watched there
rather than sent into the chat.

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
from .api import ApiError, NetworkError, app_button, inline

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
    ("app", "Open ClipMonster as an app in Telegram"),
    ("people", "Who shares this bot: invite, remove, leave"),
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


class Refused(Exception):
    """Why something a person asked for isn't done, in words for them. `status` is the Mini App's HTTP answer."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


B_APP = "📱 Open ClipMonster"


def help_html(app: bool = False) -> str:
    if app:
        return (f"<b>ClipMonster on {esc(pc_name())}</b>\n\n"
                "Everything is in the app: watch the videos waiting to post, OK or delete them, say what's wrong "
                "with one so the AI makes it again, ask for new ones, and see who did what. Open it with the "
                "<b>ClipMonster</b> button left of the message box, or the button below.\n\n"
                "I'll still message you here when a video waits for your OK, when something you asked for is "
                "ready, and when something needs you. /people is who shares this bot.")
    return (f"<b>ClipMonster on {esc(pc_name())}</b>\n\n"
            f"{B_VIDEOS}: watch what's waiting to post, OK it, post it now or delete it from the PC\n"
            f"{B_MAKE}: /make the real story of the Bermuda Triangle\n"
            f"{B_CLIP}: send a YouTube link and pick its category\n"
            f"{B_REQUESTS}: /request 10 30-45s Gaming funny reactions only\n"
            f"{B_STATUS}, {B_PAUSE}, {B_WEEK}\n"
            "🛠 Under a video the AI made: say what's wrong with it, and it's made again\n"
            "👥 /people: share this bot with one other person\n\n"
            "I'll message you when a video waits for your OK, when something you asked for is ready, and when "
            "something needs you. What I send is set in ClipMonster → Settings → Telegram.")


def welcome_html(app: bool = False) -> str:
    return f"✅ <b>Linked.</b> This chat now runs ClipMonster on {esc(pc_name())}.\n\n" + help_html(app)


def unlinked_html() -> str:
    return ("This is a private ClipMonster bot. To link your phone, open ClipMonster on your PC → Settings → "
            "Telegram → <b>Link a phone</b>, and send me the code it shows.")


STRANGER_REPLIES = {
    "wrong": "That code isn't right. Send the code ClipMonster shows in Settings → Telegram → Link a phone (it's "
             "new each time).",
    "expired": "That code has run out. Press <b>Link a phone</b> in ClipMonster again for a new one.",
    "none": unlinked_html(),
    "full": "Two people already share this ClipMonster bot, which is as many as it takes. One of them has to "
            "leave (or be removed, in /people) before someone else can join.",
}

# What the other person is told when one of the two does something: {name} did it, {title} is what to
ACTIVITY = {
    "ok": "✅ <b>{name}</b> OK'd “{title}” to post.",
    "delete": "🗑 <b>{name}</b> deleted “{title}”.",
    "post_now": "🚀 <b>{name}</b> is posting “{title}” now.",
    "retry": "🔁 <b>{name}</b> queued “{title}” to post again.",
    "critique": "🛠 <b>{name}</b> asked for “{title}” to be made again:\n<i>“{text}”</i>",
    "make": "✨ <b>{name}</b> asked for a video about “{title}”.",
    "clip": "✂️ <b>{name}</b> asked to clip {title}.",
    "request": "📋 <b>{name}</b> asked for {title}.",
    "cancel": "✖ <b>{name}</b> cancelled the request for {title}.",
    "pause": "⏸ <b>{name}</b> paused automation.",
    "resume": "▶ <b>{name}</b> resumed automation.",
    "join": "👋 <b>{name}</b> now shares this bot with you. You both see every video, and I'll tell each of "
            "you what the other did.",
    "leave": "👋 <b>{name}</b> left: they can't use this bot any more.",
    "remove": "👋 <b>{name}</b> removed {title} from this bot.",
}


def activity_html(entry: dict) -> str:
    line = ACTIVITY.get(entry.get("what") or "", "")
    return line.format(name=esc(entry.get("name") or "Someone"), title=esc(trim(entry.get("title") or "", 100)),
                       text=esc(trim(entry.get("text") or "", 300))) if line else ""


def stranger(data: dict, chat: dict, text: str, now: float) -> str:
    """A message from a chat that isn't linked, tried as the pairing code. What to answer: "linked", "wrong",
    "expired", "none" (how to link), "full" (two people share the bot already), "quiet" (told a moment ago) or
    "blocked" (too many wrong codes: nothing). Links the chat when the code is right."""
    chat_id = int(chat["id"])
    if not text:
        return "quiet"
    if state.looks_like_code(text) and state.full(data):
        said = "full"  # the code isn't tried (or used up), and no wrong one is counted
    else:
        said = state.try_code(data, chat_id, text, now) if state.looks_like_code(text) else "none"
    if said == "linked":
        state.link(data, chat, now)
        return said
    told = float(data["told"].get(str(chat_id)) or 0)
    if said in ("none", "full") and now - told < NO_ANSWER_EVERY:
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
        # The Mini App (webapp.py tells the bot its public address, or that it has none)
        self.app_url = ""
        self.app_error = ""
        self.app_known = False  # whether webapp.py has said yet: until then the chats' menu buttons are left alone
        self.fixing: dict[int, str] = {}  # chat -> the video whose critique it's about to send
        self.webapp = None  # the Mini App's server, when automation runs one (pipeline.start_telegram)
        self._menus_at = 0.0
        self._fix_lock = threading.Lock()  # one critique at a time: the chat and the app can both send one

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
        if self.webapp is not None:
            try:
                self.webapp.stop()
                if self.app_url:  # its address goes with it: the chats' app buttons go back to the plain menu
                    self.app_url, self._menus_at = "", 0.0
                    self.sync_menus()
            except Exception:  # noqa: BLE001 - the bot still stops
                pass
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
            extra = state.trim_people(data)
        for c in extra:
            self.log(f"Two people at most share the bot now: {state.chat_name(c)} was unlinked (the two linked "
                     "first stay).")
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
            self.sync_menus()
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
                            "bot": self.me.get("username", ""), "bot_id": self.me.get("id"), "error": self.error,
                            "app": self.app_url, "app_error": self.app_error, "app_on": self.webapp is not None},
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

    def say(self, chat: int, text: str, buttons: dict | None = None, quiet: bool = False) -> dict | None:
        try:
            return self.api.send(chat, text, buttons, quiet=quiet)
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
            if cmd != "fix":
                self.fixing.pop(chat_id, None)
        elif chat_id in self.modes:
            mode, until = self.modes.pop(chat_id)
            if now < until:
                cmd, arg = mode, text
        handler = {"start": self.cmd_help, "help": self.cmd_help, "menu": self.cmd_help, "status": self.cmd_status,
                   "videos": self.cmd_videos, "make": self.cmd_make, "clip": self.cmd_clip,
                   "request": self.cmd_request, "requests": self.cmd_requests, "pause": self.cmd_pause,
                   "resume": self.cmd_resume, "toggle": self.cmd_toggle, "week": self.cmd_week,
                   "dashboard": self.cmd_dashboard, "app": self.cmd_app, "people": self.cmd_people,
                   "fix": self.cmd_fix}.get(cmd)
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
            if said == "linked" and self.app_url:
                for c in data["chats"]:
                    if c["id"] == chat_id:
                        c["app_told"] = True  # the welcome has the button into the app already
        if said == "linked":
            self.log(f"Linked {state.chat_name(chat)}'s chat.")
            self.say(chat_id, welcome_html(bool(self.app_url)), self.menu())
            self.did(chat_id, "join")
            self.wake.set()  # its menu button
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
                   "reqs": self.press_requests, "fix": self.press_fix, "ppl": self.press_people}.get(kind)
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
        self.say(chat, help_html(bool(self.app_url)), self.menu())

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
        text = self.queue_make(chat, idea, cat)[1]
        self.say(chat, text)
        return text

    def queue_make(self, chat: int, idea: str, cat) -> tuple[str, str]:
        """Queue a Creator video `chat` asked for: (the job's id, what to tell them, as HTML)."""
        from ..creator.orchestrator import CreateRequest, queue as queue_creation

        db = self.runner.db
        req = CreateRequest(prompt=idea, category=cat.id if cat else "")
        job_id = queue_creation(db, req, cat.id if cat else "", self.now(), origins=["creator", "telegram"])
        self._remember(f"job:{job_id}", chat, req.label(), "make")
        db.set_meta("nudge", "1")
        self.log(f"{self.name(chat)} asked for a video about “{trim(idea, 80)}”"
                 + (f" for {cat.name}" if cat else "") + ".")
        when = self._when(cat)
        text = (f"✨ Got it: a video about “{esc(trim(idea, 200))}”" + (f" for {esc(cat.name)}" if cat else "")
                + f". {when} I'll send it here when it's made" + (", for your OK before it posts."
                                                                  if self._held(cat) else "."))
        self.did(chat, "make", idea)
        return job_id, text

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
            self.log(f"{self.name(chat)} asked to clip {plural(len(added), 'video')}"
                     + (f" for {cat.name}" if cat else "") + ".")
            self.did(chat, "clip", plural(len(added), "video") + (f" for {cat.name}" if cat else ""))
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
        self.did(chat, "request", order.title(cat.name if cat else ""))
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
        self.log(f"{self.name(chat)} paused automation from Telegram.")
        self.did(chat, "pause")
        self.say(chat, "⏸ Paused. Nothing new is clipped or made until you /resume (a video already being made "
                       "finishes). Posting carries on.", inline([[("▶ Resume", "resume")]]))

    def cmd_resume(self, chat: int, arg: str = "") -> None:
        self.runner.db.set_meta("paused", "0")
        self.runner.db.set_meta("nudge", "1")
        self.log(f"{self.name(chat)} resumed automation from Telegram.")
        self.did(chat, "resume")
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
        self.log(f"{self.name(chat)} paused automation from Telegram.")
        self.did(chat, "pause")
        self.press_status(chat, mid, "", video)
        return "Paused"

    def press_resume(self, chat, mid, arg, video) -> str:
        self.runner.db.set_meta("paused", "0")
        self.runner.db.set_meta("nudge", "1")
        self.log(f"{self.name(chat)} resumed automation from Telegram.")
        self.did(chat, "resume")
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
        preview.drop(it.id, self.previews)
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
        self.log(f"{self.name(chat)} cancelled a request from Telegram ({o.title()}).")
        self.did(chat, "cancel", o.title())
        try:
            self.api.edit(chat, mid, f"✖ Cancelled. {plural(n, 'waiting video')} left the queue.")
        except ApiError:
            pass
        return "Cancelled"

    def press_job(self, chat, mid, arg, video) -> str:
        items = self._items_of_job(arg)
        if not items:
            return "Those clips aren't in the library any more"
        text, buttons = digest_html("✂️ The clips", items, self.posting()[1], self.now(), self.app_url)
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

    # --- the Mini App ------------------------------------------------------------------------------------------

    def name(self, chat: int) -> str:
        """What to call a linked person in a line about what they did."""
        with self.lock:
            return state.first_name(state.load(self.state_file), chat)

    def menu(self) -> dict:
        """What goes under a help message: the button into the Mini App, or the chat's own menu of buttons."""
        return inline([[self.app_row(B_APP)]]) if self.app_url else tg.keyboard(MENU)

    def app_row(self, label: str, item: str = "") -> tuple[str, str]:
        """A button that opens the Mini App (at one video, with `item`)."""
        return app_button(label, app_link(self.app_url, item))

    def set_app_url(self, url: str, error: str = "") -> None:
        """webapp.py says where the Mini App can be reached from a phone ("" = nowhere right now, and why)."""
        if url != self.app_url:
            self.log(f"The Mini App is at {url}." if url else "The Mini App has no address right now"
                     + (f": {error}" if error else "."))
        self.app_url, self.app_error, self.app_known = url, error, True
        self._menus_at = 0.0
        self._last["status"] = 0.0  # the window's Telegram page reads it from the heartbeat
        self.wake.set()

    def sync_menus(self) -> None:
        """Point each linked chat's menu button (left of the message box) at the Mini App, and give a chat that
        was unlinked its plain one back. The first time a chat gets the app, it's told."""
        now = self.now()
        if not self.app_known or self.api is None or now < self._menus_at:
            return
        with self.lock:
            data = state.load(self.state_file)
        linked = {c["id"]: c for c in data["chats"]}
        menus = data["menus"]
        todo = [(cid, self.app_url) for cid, c in linked.items()
                if not c.get("blocked") and str(menus.get(str(cid)) or "") != self.app_url]
        todo += [(int(k), "") for k in menus if k.lstrip("-").isdigit() and int(k) not in linked]
        for chat, url in todo:
            gone = False
            try:
                self.api.set_menu_button(chat, app_link(url) if url else "")
            except ApiError as exc:
                if chat in linked:
                    if exc.code == 403:
                        self._blocked(chat)
                    else:
                        self.log(f"⚠ Couldn't set the chat's app button: {exc}")
                        self._menus_at = now + 300.0  # not on every look
                    continue
                gone = True  # an unlinked chat that can't be reached: nothing to put back
            except NetworkError:
                self._menus_at = now + 30.0
                return
            first = False
            with self.lock, state.edit(self.state_file) as d:
                if chat in linked:
                    d["menus"][str(chat)] = url
                    for c in d["chats"]:
                        if c["id"] == chat and url and not c.get("app_told"):
                            c["app_told"] = first = True
                elif not url or gone:
                    d["menus"].pop(str(chat), None)
            if first:
                self.say(chat, "📱 <b>ClipMonster is an app inside Telegram now.</b> Watch the videos, OK or delete "
                               "them, and say what's wrong with one so the AI makes it again.",
                         {"remove_keyboard": True})
                self.say(chat, "Open it with the <b>ClipMonster</b> button left of the message box (it always has "
                               "the right address), or here:", inline([[self.app_row(B_APP)]]))

    def cmd_app(self, chat: int, arg: str = "") -> None:
        if self.app_url:
            self.say(chat, "📱 ClipMonster opens inside Telegram. The <b>ClipMonster</b> button left of the message "
                           "box always has the right address; this one works until the PC restarts.",
                     inline([[self.app_row(B_APP)]]))
            return
        s = self.settings()
        if not getattr(s, "telegram_app", True):
            why = "It's turned off: ClipMonster on the PC → Settings → Telegram → Mini App."
        elif self.app_error:
            why = self.app_error
        else:
            why = "It's starting: its address isn't up yet. Try again in a minute."
        self.say(chat, f"📱 The app can't be opened right now. {esc(why)}\nEverything still works from this chat: "
                       "/help.")

    def seen(self, chat: int, user: dict) -> None:
        """The Mini App saw this linked person: keep their name as Telegram has it now."""
        name = " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip()
        with self.lock:
            c = next((c for c in state.load(self.state_file)["chats"] if c["id"] == chat), None)
        if c is None or (c.get("name") == name and c.get("username") == str(user.get("username") or "")):
            return
        with self.lock, state.edit(self.state_file) as data:
            for c in data["chats"]:
                if c["id"] == chat:
                    c["name"], c["username"] = name or c.get("name") or "", str(user.get("username") or "")

    # --- who did what ------------------------------------------------------------------------------------------

    def did(self, chat: int, what: str, title: str = "", item: str = "", text: str = "") -> None:
        """Remember that a linked person did something, and tell the other one (without a sound)."""
        with self.lock, state.edit(self.state_file) as data:
            if not state.is_linked(data, chat):
                return
            entry = state.note_activity(data, chat, what, title, self.now(), item, text)
            others = [c["id"] for c in data["chats"] if c["id"] != chat and not c.get("blocked")]
        line = activity_html(entry)
        if line and self.api is not None:
            for other in others:
                self.say(other, line, quiet=True)

    # --- sharing the bot ---------------------------------------------------------------------------------------

    def cmd_people(self, chat: int, arg: str = "") -> None:
        text, buttons = self.people_html(chat)
        self.say(chat, text, buttons)

    def people_html(self, chat: int) -> tuple[str, dict]:
        with self.lock:
            data = state.load(self.state_file)
        mine = state.is_owner(data, chat)
        lines = ["<b>👥 Who shares this bot</b>"]
        rows = []
        for c in state.people(data):
            lines.append(f"• {esc(state.chat_name(c))}" + (" (you)" if c["id"] == chat else "")
                         + (" · linked first: invites and removes" if state.is_owner(data, c["id"]) else ""))
            if mine and c["id"] != chat:
                rows.append([(trim(f"✖ Remove {state.first_name(data, c['id'])}", 40), f"ppl:remove:{c['id']}")])
        lines.append("")
        lines.append(f"{state.MAX_PEOPLE} people at most. You both see every video; what each of you asks for, "
                     "says about a video and does carries your name, and the other is told.")
        if mine and not state.full(data):
            rows.insert(0, [("➕ Invite someone", "ppl:invite")])
        elif not mine and not state.full(data):
            lines.append("Only the person linked first can invite.")
        rows.append([("🚪 Leave", "ppl:leave")])
        return "\n".join(lines), inline(rows)

    def press_people(self, chat, mid, arg, video) -> str:
        what, _, who = arg.partition(":")
        try:
            if what == "invite":
                r = self.invite(chat)
                self.say(chat, "➕ <b>An invite for one person.</b> Send them this link; it works once, for "
                               f"{int(state.CODE_LIFE // 60)} minutes:\n{esc(r['link'])}\n\nOr they open "
                               f"@{esc(self.me.get('username', ''))} and send it this code: <code>{esc(r['code'])}</code>")
                return ""
            if what == "remove":
                with self.lock:
                    data = state.load(self.state_file)
                other = next((c for c in data["chats"] if str(c["id"]) == who), None)
                if other is None:
                    return "They don't share this bot any more"
                self.say(chat, f"Remove {esc(state.chat_name(other))}? They can't use this bot until they're invited "
                               "again.", inline([[("✖ Yes, remove", f"ppl:REMOVE:{who}"), ("↩ Keep", "x:-")]]))
                return ""
            if what == "REMOVE":
                r = self.remove(chat, int(who or 0))
                self._replace(chat, mid, esc(r["message"]))
                return "Removed"
            if what == "leave":
                self.say(chat, "Leave this bot? You'll need a new invite, or the code from the PC, to use it again.",
                         inline([[("🚪 Yes, leave", "ppl:LEAVE"), ("↩ Stay", "x:-")]]))
                return ""
            if what == "LEAVE":
                r = self.leave(chat)
                self._replace(chat, mid, esc(r["message"]))
                return "You left"
        except (Refused, ValueError) as exc:
            return trim(str(exc), 180)
        return ""

    def _replace(self, chat: int, mid: int, text: str) -> None:
        try:
            self.api.edit(chat, mid, text)
        except ApiError:
            self.say(chat, text)

    def invite(self, chat: int) -> dict:
        """A code (and link) that links one more person, asked for by the bot's owner. Raises Refused."""
        from .pair import start_link

        now = self.now()
        with self.lock, state.edit(self.state_file) as data:
            if not state.is_linked(data, chat):
                raise Refused("This chat isn't linked to ClipMonster.", 403)
            if not state.is_owner(data, chat):
                first = state.owner(data)
                raise Refused(f"Only {state.first_name(data, first['id']) if first else 'the first person'} can "
                              "invite someone (they were linked first).", 403)
            if state.full(data):
                raise Refused(f"{state.MAX_PEOPLE} people already share this bot. Remove one to invite someone else.")
            code = state.new_code(data, now, by=chat)
            until = data["pairing"]["until"]
        self.log(f"{self.name(chat)} made an invite for one more person (good for "
                 f"{int(state.CODE_LIFE // 60)} minutes).")
        return {"code": code, "link": start_link(self.me.get("username") or "", code), "until": until,
                "minutes": int(state.CODE_LIFE // 60), "bot": self.me.get("username") or ""}

    def leave(self, chat: int) -> dict:
        """Unlink the chat that asks. The other person (if there is one) is told, and owns the bot from then."""
        now = self.now()
        with self.lock, state.edit(self.state_file) as data:
            if not state.is_linked(data, chat):
                raise Refused("This chat isn't linked to ClipMonster.", 403)
            was_owner = state.is_owner(data, chat)
            who = state.chat_name(next(c for c in data["chats"] if c["id"] == chat))
            entry = state.note_activity(data, chat, "leave", "", now)
            state.unlink(data, chat)
            others = [c["id"] for c in data["chats"] if not c.get("blocked")]
        self.modes.pop(chat, None)
        self.fixing.pop(chat, None)
        self.log(f"{who} left the bot.")
        for other in others:
            self.say(other, activity_html(entry) + (" You invite and remove people now." if was_owner else ""))
        self.wake.set()  # its menu button goes back
        return {"message": "You left. To use this bot again you need a new invite, or the code from ClipMonster on "
                           "the PC (Settings → Telegram → Link a phone)."}

    def remove(self, chat: int, other: int) -> dict:
        """The owner unlinks the other person. Raises Refused."""
        now = self.now()
        with self.lock, state.edit(self.state_file) as data:
            if not state.is_owner(data, chat):
                raise Refused("Only the person linked first can remove someone.", 403)
            them = next((c for c in data["chats"] if c["id"] == other), None)
            if them is None or other == chat:
                raise Refused("They don't share this bot any more.", 404)
            who, mine = state.chat_name(them), state.first_name(data, chat)
            state.note_activity(data, chat, "remove", who, now)
            state.unlink(data, other)
        self.modes.pop(other, None)
        self.fixing.pop(other, None)
        self.log(f"{mine} removed {who} from the bot.")
        if self.api is not None:
            self.say(other, f"You no longer share ClipMonster on {esc(pc_name())}: {esc(mine)} removed this chat.",
                     {"remove_keyboard": True})
        self.wake.set()
        return {"message": f"{who} can't use this bot any more."}

    # --- a critique: make it again -------------------------------------------------------------------------------

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
        self.say(chat, f"🛠 What should be different about “{esc(trim(it.headline, 80))}”? Say it in your own words, "
                       "like <i>the hook is flat, open with the twist</i> or <i>the third picture doesn't fit what's "
                       "said</i>. The AI makes it again with that fixed and keeps the rest.")
        return ""

    def cmd_fix(self, chat: int, text: str = "") -> None:
        item = self.fixing.pop(chat, "")
        if not item:
            self.say(chat, "Open a video the AI made (/videos) and tap <b>🛠 Say what to fix</b> under it"
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
        if not director.script_writers(s):
            raise Refused(director.no_writer_text(s))
        with self.lock:
            data = state.load(self.state_file)
        if any(c.get("item") == it.id and c.get("status") in ("queued", "working")
               for c in data["critiques"].values()):
            raise Refused("It's being made again already. Wait for that version, then say what's still wrong.")
        made = _made(Path(it.path).parent)
        common = dict(prompt=board.prompt, title=board.title, category=it.category_id or it.category_name,
                      style=str(made.get("style") or ""), storyboard=board.to_json(), critique=text,
                      revises=it.headline)
        if isinstance(made.get("studio"), dict):  # the Studio made it: the same shape of picture again
            from ..studio.orchestrator import StudioRequest

            req = StudioRequest(**common, orientation=_shape(str(made["studio"].get("canvas") or "")))
        else:
            req = CreateRequest(**common)
        db = self.runner.db
        job_id = queue_creation(db, req, it.category_id, now, origins=["creator", "telegram", "critique"])
        held = ""
        with store.lock:
            if it.status == "queued" and it.review != "waiting":
                held, it.review = it.review or "-", "waiting"  # it doesn't post as it is meanwhile
        if held:
            self.save(store)
        with self.lock, state.edit(self.state_file) as d:
            entry = state.add_critique(d, chat, it.id, it.headline, text, job_id, now)
            entry["held"] = held
        self._remember(f"job:{job_id}", chat, it.headline, "fix")
        db.set_meta("nudge", "1")
        self.log(f"{self.name(chat)} asked for “{trim(it.headline, 80)}” to be made again: “{trim(text, 120)}”")
        self.settle(it.id)
        self.did(chat, "critique", it.headline, it.id, text)
        cat = next((c for c in self._categories(s) if c.id == it.category_id), None)
        return {"critique": self.critique_json(entry), "job": job_id, "held": bool(held),
                "message": "Got it: the AI makes it again with that fixed. " + html.unescape(self._when(cat))
                           + " The new version waits for an OK beside this one"
                           + (", and this one is held meanwhile so it doesn't post as it is." if held else ".")}

    def critique_json(self, c: dict) -> dict:
        return {"id": c.get("id") or "", "item": c.get("item") or "", "title": c.get("title") or "",
                "text": c.get("text") or "", "by": c.get("by") or 0, "name": c.get("name") or "Someone",
                "at": c.get("at") or 0, "status": c.get("status") or "", "stage": c.get("stage") or "",
                "error": c.get("error") or "", "fixed": c.get("fixed") or ""}

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

    def app_make(self, chat: int, idea: str, category_id: str = "") -> dict:
        """The Mini App's "make a video": queued like /make, answered in the app. Raises Refused."""
        idea = " ".join(str(idea or "").split())
        if not idea:
            raise Refused("Say what the video should be about.", 400)
        if len(idea) > 1000:
            raise Refused("That idea is too long: keep it under 1,000 characters.", 400)
        cat = None
        if category_id:
            cat = next((c for c in self._categories() if c.id == category_id), None)
            if cat is None:
                raise Refused("That category isn't there any more.", 400)
        job_id, _text = self.queue_make(chat, idea, cat)
        return {"job": job_id, "held": self._held(cat),
                "message": f"Got it: a video about “{trim(idea, 120)}”" + (f" for {cat.name}" if cat else "") + ". "
                           + html.unescape(self._when(cat)) + " It shows up under Videos when it's made"
                           + (", and waits for an OK before it posts." if self._held(cat) else ".")}

    # --- a video's card ----------------------------------------------------------------------------------------

    def card(self, it) -> tuple[str, dict | None]:
        store, accounts, svc = self.posting()
        return self.card_text(it, accounts), self.buttons_for(it, accounts, svc)

    def card_text(self, it, accounts) -> str:
        """The card's words, with what was said about the video and by whom."""
        text = card_html(it, accounts, self.now())
        with self.lock:
            said = state.critiques_of(state.load(self.state_file), it.id)
        for c in said[-3:]:
            if c.get("fixed") == it.id:
                text += (f"\n🛠 Made again from <b>{esc(c.get('name') or 'someone')}</b>'s critique of the first "
                         f"version: <i>“{esc(trim(c.get('text'), 160))}”</i>")
            else:
                text += f"\n🛠 <b>{esc(c.get('name') or 'Someone')}</b>: <i>“{esc(trim(c.get('text'), 160))}”</i> · "
                text += {"queued": "waiting to be made again", "working": "being made again now",
                         "done": "made again: the new version waits for an OK",
                         "failed": "couldn't be made again"}.get(c.get("status") or "", "")
        return text

    def buttons_for(self, it, accounts, svc) -> dict | None:
        if self.app_url and it.status not in ("posted", "uploading"):
            return inline([[self.app_row("▶ Watch it in the app", it.id)]])
        return card_buttons(it, accounts, svc, fixable=self.fixable(it))

    def show(self, chat: int, it, note: str = "") -> dict | None:
        """Send the video itself with its card (its caption and buttons). Remembered, so the card follows when
        the video is OK'd, posted or deleted somewhere else. Use show_later from a chat's button: an upload
        can take a while."""
        text, buttons = self.card(it)
        if note:
            text = f"{note}\n\n{text}"
        if self.app_url and it.exists():  # watched in the Mini App: no upload into the chat
            return self._shown(it, chat, self.say(chat, text, buttons), video=False)
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
        self.sync_critiques()
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
                text, buttons = catch_up_html(new, self.app_url)
                self.broadcast(text, buttons)
            else:
                for title, group in groups:
                    text, buttons = digest_html(title, group, accounts, now, self.app_url)
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
                           inline([[self.app_row("🎬 Watch them" if len(waiting) > 1 else "🎬 Watch it")
                                    if self.app_url else
                                    ("🎬 Watch them" if len(waiting) > 1 else "🎬 Watch it", "list:0")]]))

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
                fix = ask.get("kind") == "fix"
                if job is None:
                    text = (f"“{esc(trim(ask.get('title', ''), 80))}” was taken out of the queue"
                            + (" before it was made again. The first version is as it was." if fix else "."))
                elif job.status == "failed":
                    what = "make" if job.is_creation else "clip"
                    text = (f"✖ Couldn't {what} “{esc(trim(ask.get('title', '') if fix else job.title or ask.get('title', ''), 80))}”"
                            f"{' again' if fix else ''}: {esc(trim(job.error, 300))}"
                            + ("\nThe first version is as it was." if fix else ""))
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
                    buttons = digest_html("", items, accounts, now, self.app_url)[1]
            done.append(key)
            if not chat:
                continue
            who = esc(state.first_name(data, chat))
            others = [c["id"] for c in data["chats"] if c["id"] != chat and not c.get("blocked")]
            if items and kind == "job" and len(items) == 1:
                it = items[0]
                wait = " It waits for your OK before it posts." if it.review == "waiting" else ""
                if ask.get("kind") == "fix":
                    note = (f"🛠 <b>Here's “{esc(trim(ask.get('title', ''), 80))}” made again from your critique.</b> "
                            "Both versions wait for an OK: keep the one you like and delete the other.")
                    theirs = (f"🛠 <b>“{esc(trim(ask.get('title', ''), 80))}” was made again from {who}'s "
                              "critique.</b> Both versions wait for an OK.")
                else:
                    note = ("✨ <b>Here's the video you asked for.</b>" if it.ai_generated
                            else "✂️ <b>Your clip is ready.</b>") + wait
                    theirs = (f"✨ <b>The video {who} asked for is made.</b>" if it.ai_generated
                              else f"✂️ <b>The clip {who} asked for is ready.</b>") + wait.replace("your OK", "an OK")
                self.show_later(chat, it, note)
                for other in others:  # both people see every video
                    self.show_later(other, it, theirs)
            elif items and kind == "job":
                src = trim(items[0].facts.get("source_title") or ask.get("title", ""), 60)
                t, b = digest_html(f"✂️ Your {plural(len(items), 'clip')} from “{src}”", items, accounts, now,
                                   self.app_url)
                self.say(chat, t, b)
                t, b = digest_html(f"✂️ {plural(len(items), 'clip')} {state.first_name(data, chat)} asked for, from "
                                   f"“{src}”", items, accounts, now, self.app_url)
                for other in others:
                    self.say(other, t, b)
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


def _made(folder: Path) -> dict:
    """How the Creator or the Studio made the video in `folder` (its clips.json's "creator"), or {}."""
    import json

    try:
        made = json.loads((folder / "clips.json").read_text(encoding="utf-8")).get("creator")
    except (OSError, ValueError, AttributeError):
        return {}
    return made if isinstance(made, dict) else {}


def app_link(url: str, item: str = "") -> str:
    """The Mini App's address as a button opens it (at one video, with `item`). It ends in "/" so the page's
    own files are found beside it, also when the address has a folder in it."""
    return f"{url.rstrip('/')}/" + (f"?v={item}" if item else "")


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


def card_buttons(it, accounts, service, fixable: bool = False) -> dict | None:
    """The buttons under a video's card. `fixable`: it can be made again from a critique (Bot.fixable)."""
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
    if fixable:
        rows.append([("🛠 Say what to fix", f"fix:{it.id}")])
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


def week_html(w) -> str:
    lines = [f"<b>📅 Your week, {esc(w.span())}</b>", esc(w.headline())]
    for heading, rows in w.sections():
        lines += ["", f"<b>{esc(heading)}</b>"]
        for text, link in rows:
            lines.append("• " + (f'<a href="{attr(link)}">{esc(text)}</a>' if link else esc(text)))
    return "\n".join(lines)
