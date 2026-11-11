"""Linking a phone from the window: Settings → Telegram → Link a phone.

The window shows a six-letter code for ten minutes; you send it to your bot in Telegram (or tap Open
Telegram, which sends it for you), and that chat is linked. When automation's bot is running as the same bot,
it reads the code from telegram.json and links the chat itself. Otherwise the window listens to Telegram
while the code is up (Linker). Only one program can listen to a bot at a time, so the Linker steps aside
whenever automation's bot is running.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from . import api as tg
from . import state
from .api import ApiError, NetworkError


def start_link(bot_username: str, code: str) -> str:
    """The link that opens the bot in Telegram with the code ready to send."""
    return f"https://t.me/{bot_username}?start={code}" if bot_username and code else ""


class Linker:
    """Checks the token, shows a code, and links the chat that sends it. The window reads its fields."""

    def __init__(self, token: str, api_factory: Callable[[str], tg.Api] = tg.Api, state_file: Path | None = None,
                 status_file: Path | None = None, now: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] | None = None):
        self.token = (token or "").strip()
        self.api_factory = api_factory
        self.state_file = state_file
        self.status_file = status_file
        self.now = now
        self.phase = "checking"  # checking | waiting | linked | expired | error | stopped
        self.error = ""
        self.bot = ""  # @username, without the @
        self.code = ""
        self.until = 0.0
        self.linked = ""  # the name of the chat that was linked
        self.by_automation = False  # automation's bot is doing the listening
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._thread: threading.Thread | None = None

    @property
    def link(self) -> str:
        return start_link(self.bot, self.code)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.phase in ("checking", "waiting"):
            self.phase = "stopped"

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            self._link()
        except Exception as exc:  # noqa: BLE001 - shown in the window
            self.phase, self.error = "error", tg.why(exc)

    def _link(self) -> None:
        if not tg.looks_like_token(self.token):
            self.phase = "error"
            self.error = ("That doesn't look like a bot token. In Telegram, @BotFather gives one like "
                          "123456789:AAH… when you send /newbot.")
            return
        api = self.api_factory(self.token)
        try:
            me = api.get_me()
        except (ApiError, NetworkError) as exc:
            self.phase, self.error = "error", tg.why(exc)
            return
        self.bot = me.get("username") or ""
        with state.edit(self.state_file) as data:
            state.set_bot(data, me)
            if state.full(data):
                self.phase = "error"
                self.error = (f"{state.MAX_PEOPLE} people share this bot already, which is as many as it takes. "
                              "Unlink one below first.")
                return
            self.code = state.new_code(data, self.now())
            self.until = data["pairing"]["until"]
            before = set(state.chat_ids(data))
            offset = int(data.get("offset") or 0)
        self.phase = "waiting"
        while not self._stop.is_set():
            now = self.now()
            data = state.load(self.state_file)
            new = [c for c in data["chats"] if c["id"] not in before]
            if new:
                self.linked = state.chat_name(new[-1])
                self.phase = "linked"
                return
            if now > self.until or not data.get("pairing"):
                self.phase = "expired"
                return
            running = state.read_status(self.status_file, now)
            self.by_automation = bool(running.get("running") and running.get("bot_id") == me.get("id"))
            if self.by_automation:  # automation's bot links it: just watch telegram.json
                self._sleep(1.0)
                continue
            try:
                updates = api.get_updates(offset, timeout=5)
            except ApiError as exc:
                if exc.code == 409 and "webhook" in exc.description.lower():
                    self.phase, self.error = "error", tg.why(exc)
                    return
                self._sleep(3.0)  # automation's bot may have just started: it takes over
                continue
            except NetworkError as exc:
                self.error = tg.why(exc)
                self._sleep(3.0)
                continue
            self.error = ""
            for u in updates:
                offset = max(offset, int(u.get("update_id") or 0) + 1)
                self._answer(api, u)
            with state.edit(self.state_file) as d:
                d["offset"] = max(int(d.get("offset") or 0), offset)

    def _answer(self, api: tg.Api, update: dict) -> None:
        from .bot import STRANGER_REPLIES, MENU, stranger, welcome_html

        if "callback_query" in update:
            api.answer(update["callback_query"].get("id", ""), "ClipMonster's automation isn't running on the PC "
                                                                "right now.", alert=True)
            return
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") != "private":
            return
        text = (msg.get("text") or "").strip()
        with state.edit(self.state_file) as data:
            if state.is_linked(data, int(chat["id"])):
                said = "automation off"
            else:
                said = stranger(data, chat, text, self.now())
        try:
            if said == "linked":
                with state.edit(self.state_file) as data:  # the other person sees it in the app's Activity
                    state.note_activity(data, int(chat["id"]), "join", "", self.now())
                api.send(int(chat["id"]), welcome_html(), tg.keyboard(MENU))
            elif said == "automation off":
                api.send(int(chat["id"]), "ClipMonster's automation isn't running on the PC right now, so I can't do "
                                          "that yet. Start it from the Automation tab (or turn on Start with Windows), "
                                          "and send it again.")
            elif said in STRANGER_REPLIES:
                api.send(int(chat["id"]), STRANGER_REPLIES[said])
        except (ApiError, NetworkError):
            pass


def send_test(token: str, chats: list[int], api_factory: Callable[[str], tg.Api] = tg.Api) -> tuple[int, str]:
    """A hello to each linked chat: (how many it reached, the first problem)."""
    from .bot import pc_name

    api = api_factory(token)
    reached, problem = 0, ""
    for chat in chats:
        try:
            api.send(chat, f"👋 A test from ClipMonster on {tg_escape(pc_name())}: the bot can reach you.")
            reached += 1
        except (ApiError, NetworkError) as exc:
            problem = problem or tg.why(exc)
    return reached, problem


def tg_escape(text: str) -> str:
    import html

    return html.escape(text, quote=False)


def check_token(token: str, api_factory: Callable[[str], tg.Api] = tg.Api) -> tuple[str, str]:
    """('@name', '') when Telegram knows the token, else ('', why not)."""
    if not tg.looks_like_token(token):
        return "", "That doesn't look like a bot token (it's like 123456789:AAH…, from @BotFather)."
    try:
        me = api_factory(token.strip()).get_me()
    except (ApiError, NetworkError) as exc:
        return "", tg.why(exc)
    return "@" + (me.get("username") or "?"), ""
