"""The Telegram bot: ClipMonster on your phone. This file is the dispatcher: who may speak, what each command
and button goes to, the three threads and their lifecycle. What the commands do is in the handlers the Bot is
made of: studio_handler.py (videos, clips and requests asked for in words), sports_handler.py (games, reels,
finding a moment), review_pipeline.py (the cards, critiques, versions and approving one), with streamer.py
(previews and the one progress message), state.py (the linked people) and api.py (Telegram itself, with its
rate limits kept and waited out).

It runs inside automation (pipeline.Runner.run starts it next to posting) whenever Settings → Telegram is
on with a bot token, and only answers the phones linked to it (state.py). From the chat you can:

* see what automation is doing (/status, which can stay pinned as a live dashboard);
* watch the videos waiting to post, OK the ones held for your OK, post one now, or delete a bad one from the
  PC: its files go the way Clean up clears a posted clip (records.discard);
* ask for things: a Video Creator video (/make an idea), clips of a YouTube link (/clip, or just send the
  link), or a request for clips (/request 10 30-45s Gaming funny reactions). Each is sent back to you
  when it's done. A video can be asked for in a plain sentence too ("Make a 65s video about 3 dark psychology
  tricks for @PsychologyFacts", "Turn this link https://… into a reel for History"): the category is the one
  named, else the one the idea fits, else asked with buttons;
* pause and resume automation, and read the past week's report (/week);
* say what's wrong with a video the Creator or the Studio made (🛠 Critique / Regenerate): only what the
  critique names is made again, the result comes back as "v2 (Critique Applied)", and both versions are held
  until one is approved, which deletes the other (review_pipeline.py);
* search a game for a moment said in words (/find Mahomes 4th quarter touchdown): each one found is a card
  with when it happened and what was said, to clip by itself or add to the game's reel;
* ask for a game's recap reel (/games lists the last 24 hours' games to tap; /sports NBA Lakers vs Celtics says
  one in words): automation looks for the game, finds its plays and makes the reel, with nobody picking along
  the way (sportsdesk/desk.py: an order), and what it makes is checked against the minimums before it's handed
  over (compliance/requirements.py: over a minute, upright, with sound);
* share the bot with one other person (/people): two people at most, each told what the other did.

With Settings → Telegram → Mini App on, all of that is an app inside Telegram instead (mini_app.py): the button
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

import queue
import secrets
import threading
import time
from pathlib import Path
from typing import Callable

from . import api as tg
from . import state, streamer
from .api import ApiError, NetworkError, app_button, inline
from .common import (DIGEST_MAX, GB, MODE_LIFE, PAGE, PENDING_LIFE, URL, Refused, app_link, attr, esc,  # noqa: F401
                     length_text, pc_name, plural, trim)
from .review_pipeline import (ReviewPipeline, _items_in, _made, _shape, card_buttons, card_html,  # noqa: F401
                              card_state, catch_up_html, digest_html, group_new)
from .sports_handler import SPORTS_HELP, SportsHandler, parse_sports  # noqa: F401
from .studio_handler import StudioHandler, parse_request  # noqa: F401

POLL_TIMEOUT = 25  # seconds a long poll waits for a message
CHECK_EVERY = 15.0  # seconds between looks for videos to show and asks that are done
DASHBOARD_EVERY = 120.0
STATUS_EVERY = 30.0
PRUNE_EVERY = 3600.0
REVIEW_PER_LOOK = 3  # videos held for your OK sent in one look (each is an upload)
NEW_GROUPS_MAX = 3  # more "new clips" messages than this at once are sent as one
OUTBOX_MAX = 200
NO_ANSWER_EVERY = 600.0  # an unlinked chat is told how to link at most this often
PROGRESS_EVERY = streamer.EDIT_EVERY  # seconds between edits of a progress message
FLOOD_MAX, FLOOD_SPAN = 30, 10.0  # more messages and presses than this from one chat in this many seconds wait
FLOOD_SAID_EVERY = 30.0

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
    ("games", "The games of the last 24 hours: tap one for its recap reel"),
    ("reel", "A game's recap reel, in words: /reel Lakers Celtics"),
    ("find", "Find a moment in a game: /find Mahomes 4th quarter touchdown"),
    ("sports", "A game's recap reel, in words: /sports NBA Lakers vs Celtics"),
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

MEDIA = ("photo", "video", "document", "sticker", "voice", "audio", "animation", "video_note", "contact",
         "location")


B_APP = "📱 Open ClipMonster"


def help_html(app: bool = False) -> str:
    if app:
        return (f"<b>ClipMonster on {esc(pc_name())}</b>\n\n"
                "Everything is in the app: watch the videos waiting to post, OK or delete them, say what's wrong "
                "with one and approve the version you like, ask for new ones (a video about an idea or an "
                "article's link, a game's best plays and a recap reel, or one moment found in a game), and see "
                "who did what. You can also just say it here: <i>Make a 65s video about … for History</i>. Open it with the "
                "<b>ClipMonster</b> button left of the message box, or the button below.\n\n"
                "I'll still message you here when a video waits for your OK, when something you asked for is "
                "ready, and when something needs you. /people is who shares this bot.")
    return (f"<b>ClipMonster on {esc(pc_name())}</b>\n\n"
            f"{B_VIDEOS}: watch what's waiting to post, OK it, post it now or delete it from the PC\n"
            f"{B_MAKE}: /make the real story of the Bermuda Triangle\n"
            f"{B_CLIP}: send a YouTube link and pick its category\n"
            f"{B_REQUESTS}: /request 10 30-45s Gaming funny reactions only\n"
            f"{B_STATUS}, {B_PAUSE}, {B_WEEK}\n"
            "💬 Or just say it: <i>Make a 65s video about 3 dark psychology tricks for History</i>, or "
            "<i>Turn this link https://… into a reel</i>\n"
            "🛠 Under a video the AI made: say what's wrong with it. It comes back as v2, and you approve the "
            "one to post\n"
            "🏆 /games: the games of the last 24 hours, tap one for its recap reel (over a minute)\n"
            "🏆 /reel Lakers Celtics, or /sports NBA Lakers vs Celtics: the same, said in words\n"
            "🔎 /find Mahomes 4th quarter touchdown: search a game for a moment, then clip it or add it to "
            "the reel\n"
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
    "sports": "🏆 <b>{name}</b> asked for sports highlights: {title}.",
    "find": "🔎 <b>{name}</b> is searching a game: {title}.",
    "approve": "✅ <b>{name}</b> picked {title} to post; the other version was deleted.",
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


class Bot(StudioHandler, SportsHandler, ReviewPipeline):
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
        # The Mini App (mini_app.py tells the bot its public address, or that it has none)
        self.app_url = ""
        self.app_error = ""
        self.app_known = False  # whether mini_app.py has said yet: until then the chats' menu buttons are left alone
        self.fixing: dict[int, str] = {}  # chat -> the video whose critique it's about to send
        self.webapp = None  # the Mini App's server, when automation runs one (pipeline.start_telegram)
        self._menus_at = 0.0
        self._desk = None  # the Sports section's state (sportsdesk/desk.py), opened when it's first asked of
        self.slate_get = None  # reads a scoreboard for the games played (sportsdesk/slate.py; a stand-in in tests)
        self._fix_lock = threading.Lock()  # one critique at a time: the chat and the app can both send one
        self.asks: dict[str, dict] = {}  # job id -> what was asked (studio_handler.Ask), to change its category
        # The progress messages: one per thing being made and chat, edited in place (streamer.Live)
        self.live = streamer.Live(lambda: self.api, now=self.now, keep=self._keep_live, log=self.log)
        self._live_at = 0.0
        self._flood: dict[int, list[float]] = {}  # chat -> when its last messages and presses came
        self._flood_said: dict[int, float] = {}
        self._intruders: dict[int, float] = {}  # who pressed or wrote into a linked chat's place, and when
        self.clock = time.monotonic  # for the flood guard (real seconds, whatever `now` is)

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
        if self._desk is not None:
            try:
                self._desk.close()
            except Exception:  # noqa: BLE001
                pass
            self._desk = None
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
        self._restore_live()
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
            self.wake.wait(1.0)
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
            if now - self._live_at >= PROGRESS_EVERY:
                self._live_at = now
                self.progress_tick()
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
        """One update from Telegram. Anything that isn't shaped like a message or a button press is dropped."""
        if not isinstance(update, dict):
            return
        press, msg = update.get("callback_query"), update.get("message")
        if isinstance(press, dict):
            self.on_press(press)
        elif isinstance(msg, dict):
            self.on_message(msg)

    def on_message(self, msg: dict) -> None:
        chat = msg.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return  # only a person's own chat with the bot can be linked
        try:
            chat_id = int(chat["id"])
        except (KeyError, TypeError, ValueError):
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()[:4096]  # a link shared with a picture too
        now = self.now()
        with self.lock:
            data = state.load(self.state_file)
            linked = state.is_linked(data, chat_id)
        if not linked:
            self.on_stranger(chat, text, now)
            return
        if not state.authorized(data, chat, msg.get("from")):
            self._intruder(chat_id, msg.get("from"))  # written into a linked chat by someone who isn't its person
            return
        if self._flooding(chat_id):
            if self.clock() - self._flood_said.get(chat_id, -1e9) > FLOOD_SAID_EVERY:
                self._flood_said[chat_id] = self.clock()
                self.say(chat_id, "⏳ That's a lot at once. Give me a few seconds, then send it again.")
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
                   "fix": self.cmd_fix, "sports": self.cmd_sports, "games": self.cmd_games,
                   "reel": self.cmd_reel, "reels": self.cmd_reel, "find": self.cmd_find,
                   "search": self.cmd_find}.get(cmd)
        try:
            if handler is not None:
                handler(chat_id, arg)
            elif cmd:
                self.say(chat_id, f"I don't know /{esc(cmd)}. /help says what I can do.")
            elif text and (self.on_text(chat_id, text) or self.sports_text(chat_id, text)):
                pass  # said in plain words: a video to make, a link, or a moment of a game to find
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
        msg = q.get("message") if isinstance(q.get("message"), dict) else {}
        try:
            chat_id = int((msg.get("chat") or {}).get("id") or 0)
        except (TypeError, ValueError, AttributeError):
            chat_id = 0
        with self.lock:
            allowed = state.authorized(state.load(self.state_file), msg.get("chat"), q.get("from"))
        if not allowed:  # an unlinked chat, or someone else pressing a button under a linked person's message
            self._intruder(chat_id, q.get("from"))
            self.api.answer(q.get("id", ""), "This chat isn't linked to ClipMonster.", alert=True)
            return
        if self._flooding(chat_id):
            self.api.answer(q.get("id", ""), "One moment: that's a lot of taps at once.")
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
                   "reqs": self.press_requests, "fix": self.press_fix, "ppl": self.press_people,
                   "gl": self.press_league, "gg": self.press_game, "so": self.press_sports_cancel,
                   "lk": self.press_link, "rc": self.press_recat, "rcp": self.press_recat_pick,
                   "fg": self.press_find_game, "fm": self.press_moment, "apv": self.press_approve}.get(kind)
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

    def _flooding(self, chat: int) -> bool:
        """More messages and presses from this chat than anyone taps by hand (a stuck key, a script): the
        newest wait, so the PC isn't given a queue of them."""
        t = self.clock()
        seen = [x for x in self._flood.get(chat, []) if t - x < FLOOD_SPAN]
        seen.append(t)
        self._flood[chat] = seen[-(FLOOD_MAX + 1):]
        return len(seen) > FLOOD_MAX

    def _intruder(self, chat: int, sender: dict | None) -> None:
        """Someone who isn't a linked person tried to use the bot: noted in the log, once an hour each."""
        try:
            who = int((sender or {}).get("id") or 0)
        except (TypeError, ValueError):
            who = 0
        t = self.clock()
        if t - self._intruders.get(who, -1e9) < 3600.0:
            return
        if len(self._intruders) > 200:
            self._intruders.clear()
        self._intruders[who] = t
        self.log(f"⚠ Ignored a message or button press from Telegram user {who or '?'}"
                 + (" (a bot)" if (sender or {}).get("is_bot") else "") + f" in chat {chat or '?'}: not a linked "
                 "person.")

    # --- the progress messages -----------------------------------------------------------------------------

    def _keep_live(self, entries: dict) -> None:
        with self.lock, state.edit(self.state_file) as data:
            data["live"] = entries

    def _restore_live(self) -> None:
        """The progress messages from before a restart: carried on, or taken away when what they were about
        has been answered meanwhile."""
        with self.lock:
            data = state.load(self.state_file)
        self.live.restore(data.get("live") or {})
        for key in self.live.keys() - set(data["asked"]):
            self.live.finish(key)

    def progress_tick(self) -> None:
        """One message per thing a person asked for that's being made right now, edited in place as it goes
        (never a new message per step): ⚙️ Working on: “title” [████████░░] 78% (Rendering the video)."""
        with self.lock:
            asked = dict(state.load(self.state_file)["asked"])
        if not asked:
            return
        db = self.runner.db
        orders = None
        going = self.live.keys()
        for key, ask in asked.items():
            chat = int(ask.get("chat") or 0)
            kind, _, ident = key.partition(":")
            buttons, icon, verb = None, "⚙️", "Working on"
            if kind == "job":
                job = db.get(ident)
                if job is None or job.status not in ("working", "done") or (job.status == "done" and key not in going):
                    continue
                bar = job.status_bar
                title = ask.get("title") or job.title
                if job.status == "done":
                    frac, stage = 1.0, "Bringing it into the library"
                else:
                    frac = bar.overall if bar is not None else None
                    stage = streamer.stage_text((bar.message or bar.label) if bar is not None else job.stage)
                if ask.get("kind") == "fix":
                    icon, verb = "🛠", "Applying the critique to"
            elif kind == "sports":
                if orders is None:
                    try:
                        orders = {o["id"]: o for o in self.desk.orders(20)}
                    except Exception:  # noqa: BLE001 - sports.db busy for a moment: the next tick
                        orders = {}
                o = orders.get(ident)
                if o is None or o.get("status") not in ("searching", "working"):
                    continue
                title, frac = o.get("title") or ask.get("title") or "", o.get("frac")
                stage = streamer.stage_text(str(o.get("doing") or "").split(": ", 1)[-1])
                icon = o.get("emoji") or "🏆"
                buttons = inline([[("✖ Cancel it", f"so:{ident}")]])
            else:
                continue
            if chat and key not in going:
                self.live.start(key, chat, title, stage, buttons, icon, verb)
            self.live.update(key, frac, stage)

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

        cats = [c for c in categories.load(s or self.settings()) if c.enabled]
        try:  # each with the @account it posts to, as posting knows it now (a rename there shows here at once)
            by_id = {a.id: a for a in self.posting()[1]}
        except Exception:  # noqa: BLE001 - posting isn't up yet: the names the categories carry
            by_id = {}
        for c in cats:
            a = by_id.get(c.account_id)
            if a is not None and getattr(a, "username", ""):
                c.account = a.handle
        return cats

    def _category_buttons(self, kind: str, token: str, cats: list) -> dict:
        """The categories to pick from, each with the TikTok account it posts to."""
        rows, row = [], []
        handles = any(str(c.account or "").startswith("@") for c in cats[:24])
        for i, c in enumerate(cats[:24]):
            handle = c.account if str(c.account or "").startswith("@") else ""
            label = trim(c.name, 28) + (f" · {trim(handle, 24)}" if handle else "") + (
                " (paused)" if c.paused else " (no account)" if c.needs_account else "")
            row.append((label, f"{kind}:{token}:{i}"))
            if len(row) == (1 if handles else 2):  # with the accounts beside them, one to a row
                rows.append(row)
                row = []
        if row:
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
        elif state_text and not state_text.startswith("Sports:"):
            lines.append(f"● {esc(trim(state_text, 160))}")
        lines += self.sports_lines()
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


    def press_week(self, chat, mid, arg, video) -> str:
        self.cmd_week(chat)
        return ""


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
        """mini_app.py says where the Mini App can be reached from a phone ("" = nowhere right now, and why)."""
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


    # --- sports: a game's best plays and a recap reel, asked for in one go -------------------------------------


    # --- the games played: pick one, don't type it -------------------------------------------------------------


    # --- a critique: make it again -------------------------------------------------------------------------------


    # --- a video's card ----------------------------------------------------------------------------------------


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
            self.first_look(items, [i for i in held if i.id not in answered], sends, data, now)
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
                    if fix:  # the new version alone (a retouch renders it beside the first, in one folder)
                        with self.lock:  # as sync_critiques just left it
                            made = next((c for c in state.load(self.state_file)["critiques"].values()
                                         if c.get("job") == ident), None)
                        fixed = store.get((made or {}).get("fixed") or "") if made else None
                        if made is not None and made.get("status") == "failed":
                            text = (f"✖ Couldn't make “{esc(trim(ask.get('title', ''), 80))}” again: "
                                    f"{esc(trim(made.get('error') or 'it failed', 300))}\nThe first version is as it "
                                    "was.")
                            items = []
                        elif fixed is None:
                            if now - float(job.updated or now) < 15 * 60:
                                continue
                            items = []
                        else:
                            items = [fixed]
                    if not text and not items and now - float(job.updated or now) < 15 * 60:
                        continue  # posting brings the video in within a couple of minutes
                    if not items and not text:
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
            elif kind == "sports":
                o = self.desk.find_order(ident)
                if o is None or o.get("status") == "cancelled":
                    done.append(key)
                    self.live.finish(key)
                    continue
                if o.get("status") not in ("done", "failed"):
                    continue
                if o.get("want") == "find":  # a search: its moments as cards, not a report of videos made
                    done.append(key)
                    self.live.finish(key)
                    if chat:
                        self.sports_found(chat, o, [c["id"] for c in data["chats"] if c["id"] != chat
                                                    and not c.get("blocked")], esc(state.first_name(data, chat)))
                    continue
                folders = [st["folder"] for st in o.get("steps") or [] if st.get("folder")]
                items = [i for f in dict.fromkeys(folders) for i in _items_in(store, Path(f))]
                if folders and not items and now - float(o.get("finished") or now) < 15 * 60:
                    continue  # posting brings them in within a couple of minutes
                text = self.sports_report(o, len(items), bool(folders))
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
                    label = esc(self.version_label(it) or "v2 (Critique Applied)")
                    note = (f"🛠 <b>{label}</b>: here's “{esc(trim(ask.get('title', ''), 80))}” made again from your "
                            "critique. Both versions wait for an OK: approve the one to post, and the other is "
                            "deleted.")
                    theirs = (f"🛠 <b>{label}</b>: “{esc(trim(ask.get('title', ''), 80))}” was made again from {who}'s "
                              "critique. Both versions wait for an OK.")
                else:
                    note = ("✨ <b>Here's the video you asked for.</b>" if it.ai_generated
                            else "✂️ <b>Your clip is ready.</b>") + wait
                    theirs = (f"✨ <b>The video {who} asked for is made.</b>" if it.ai_generated
                              else f"✂️ <b>The clip {who} asked for is ready.</b>") + wait.replace("your OK", "an OK")
                self.show_later(chat, it, note, live=key)
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
            elif text and kind == "sports":
                self.say(chat, text.format(whose="Your sports request"), buttons)
                for other in others:  # both people see every video
                    self.say(other, text.format(whose=f"{who}'s sports request"), buttons)
            elif text:
                self.say(chat, text, buttons)
            if not (items and kind == "job" and len(items) == 1):
                self.live.finish(key)  # the answer has taken the progress message's place
            answered.update(i.id for i in items)
        if done:
            with self.lock, state.edit(self.state_file) as d:
                for key in done:
                    d["asked"].pop(key, None)
        return answered


    def prune(self, now: float) -> None:
        try:
            store = self.posting()[0]
            with store.lock:
                keep = {i.id for i in store.items.values() if i.status != "posted"}
            streamer.prune(keep, self.previews, now)
        except Exception:  # noqa: BLE001
            pass
        self.live.prune()
        with self.lock, state.edit(self.state_file) as data:
            state.tidy(data, now)


# --- what it says ----------------------------------------------------------------------------------------------


def week_html(w) -> str:
    lines = [f"<b>📅 Your week, {esc(w.span())}</b>", esc(w.headline())]
    for heading, rows in w.sections():
        lines += ["", f"<b>{esc(heading)}</b>"]
        for text, link in rows:
            lines.append("• " + (f'<a href="{attr(link)}">{esc(text)}</a>' if link else esc(text)))
    return "\n".join(lines)
