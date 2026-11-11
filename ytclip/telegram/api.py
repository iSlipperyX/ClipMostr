"""The parts of Telegram's Bot API the bot uses, over plain HTTPS (urllib, nothing to install).

Every call is a POST to https://api.telegram.org/bot<token>/<method>, with JSON, or a multipart form
when it carries a file (a video). Telegram answers {"ok": true, "result": ...}, or {"ok": false,
"error_code": 400, "description": "..."}; when it's busy, 429 says how long to wait (retry_after).

The token is the bot's password: it never goes in a log line or an error (masked()). Errors come out as
ApiError (Telegram said no: code and description), or NetworkError (no answer at all).
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

BASE = "https://api.telegram.org"
MAX_UPLOAD = 50_000_000  # the biggest file a bot can send (preview.py keeps videos under it)
TEXT_LIMIT = 4096  # characters in a message
CAPTION_LIMIT = 1024  # characters in a video's caption
DATA_LIMIT = 64  # bytes in a button's callback data
WAIT_LIMIT = 60.0  # a 429 asking for longer than this is passed on, not waited out

# (url, body, headers, timeout) -> (HTTP status, body). Tests stand in for Telegram here.
Transport = Callable[[str, bytes, dict, float], tuple[int, bytes]]


class NetworkError(Exception):
    """Telegram couldn't be reached (offline, a proxy, a timeout)."""


class ApiError(Exception):
    """Telegram answered, and said no."""

    def __init__(self, code: int, description: str, retry_after: float = 0.0):
        super().__init__(f"{description} ({code})" if code else description)
        self.code = code
        self.description = description
        self.retry_after = retry_after

    @property
    def unchanged(self) -> bool:
        """An edit that changed nothing (the message already says it): not a problem."""
        return self.code == 400 and "not modified" in self.description.lower()

    @property
    def gone(self) -> bool:
        """The message to edit isn't there any more (deleted, or too old to edit)."""
        d = self.description.lower()
        return self.code == 400 and ("not found" in d or "can't be edited" in d)


def masked(text: str, token: str) -> str:
    """`text` without the token in it (an error message can quote the address it called)."""
    if token:
        text = text.replace(token, "<token>")
        if ":" in token:  # the secret half on its own, too
            text = text.replace(token.split(":", 1)[1], "<token>")
    return text


def looks_like_token(token: str) -> bool:
    """'123456789:AAH…' as @BotFather gives it (a number, a colon, then 30 or more letters)."""
    bot, _, secret = (token or "").strip().partition(":")
    return bot.isdigit() and len(secret) >= 30 and all(c.isalnum() or c in "-_" for c in secret)


def why(exc: Exception) -> str:
    """A failed call in words, for the window and the log."""
    if isinstance(exc, ApiError):
        if exc.code == 401 or exc.code == 404:
            return "Telegram doesn't know this token. Copy it again from @BotFather (/mybots → API Token)."
        if exc.code == 409 and "webhook" in exc.description.lower():
            return ("This bot is set up for another program (it has a webhook). Make a new bot for ClipMonster "
                    "with @BotFather: two programs can't share one.")
        if exc.code == 409:
            return ("Another program is using this bot right now. Make a new bot for ClipMonster with @BotFather: "
                    "two programs can't share one.")
        if exc.code == 403:
            return "Telegram won't let the bot write to this chat: it was blocked, or never started."
        return f"Telegram said: {exc.description}"
    if isinstance(exc, NetworkError):
        return f"Telegram couldn't be reached ({exc}). Check the internet connection."
    return f"{type(exc).__name__}: {exc}"


def _urllib(url: str, body: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # 4xx/5xx: Telegram's JSON says why
        try:
            return exc.code, exc.read()
        finally:
            exc.close()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise NetworkError(str(reason) or type(exc).__name__) from None


def multipart(fields: dict, files: dict[str, Path]) -> tuple[bytes, str]:
    """A multipart/form-data body and its Content-Type. Each file goes in as `<field>.<ext>` (Telegram
    doesn't use the name; an ASCII one can't trip anything up)."""
    boundary = "clipmonster-" + secrets.token_hex(12)
    out = bytearray()
    for name, value in fields.items():
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n").encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for name, path in files.items():
        ext = Path(path).suffix.lower().lstrip(".") or "bin"
        kind = {"mp4": "video/mp4", "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}.get(
            ext, "application/octet-stream")
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{name}.{ext}\"\r\n"
                f"Content-Type: {kind}\r\n\r\n").encode()
        out += Path(path).read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


APP = "app:"  # (label, APP + "https://…") is a button that opens that address as a Mini App inside Telegram


def app_button(label: str, url: str) -> tuple[str, str]:
    return label, APP + url


def inline(rows: list[list[tuple[str, str]]]) -> dict:
    """Buttons under a message: rows of (label, callback data), (label, "https://…") for a link, or
    app_button(label, "https://…") for one that opens a Mini App."""
    def button(label: str, data: str) -> dict:
        if data.startswith(APP):
            return {"text": label, "web_app": {"url": data[len(APP):]}}
        if data.startswith(("https://", "http://")):
            return {"text": label, "url": data}
        return {"text": label, "callback_data": data}
    return {"inline_keyboard": [[button(label, data) for label, data in row] for row in rows if row]}


def keyboard(rows: list[list[str]]) -> dict:
    """The buttons that stand in for the phone's keyboard (the bot's menu)."""
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True,
            "is_persistent": True}


class Api:
    def __init__(self, token: str, transport: Transport | None = None, base: str = BASE,
                 sleep: Callable[[float], None] = time.sleep):
        self.token = (token or "").strip()
        self.transport = transport or _urllib
        self.base = base.rstrip("/")
        self.sleep = sleep

    def call(self, method: str, params: dict | None = None, files: dict[str, Path] | None = None,
             timeout: float = 30.0):
        """One Bot API call; its result. A 429 is waited out (twice at most) when it's short."""
        url = f"{self.base}/bot{self.token}/{method}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(3):
            if files:
                body, kind = multipart(params, files)
            else:
                body, kind = json.dumps(params, ensure_ascii=False).encode("utf-8"), "application/json"
            try:
                status, raw = self.transport(url, body, {"Content-Type": kind}, timeout)
            except NetworkError as exc:
                raise NetworkError(masked(str(exc), self.token)) from None
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise ApiError(status, f"Telegram sent something that isn't JSON (HTTP {status})") from None
            if isinstance(data, dict) and data.get("ok"):
                return data.get("result")
            data = data if isinstance(data, dict) else {}
            retry = float((data.get("parameters") or {}).get("retry_after") or 0)
            err = ApiError(int(data.get("error_code") or status), masked(str(data.get("description") or
                                                                              f"HTTP {status}"), self.token), retry)
            if err.code == 429 and 0 < retry <= WAIT_LIMIT and attempt < 2:
                self.sleep(retry + 0.5)
                continue
            raise err
        raise AssertionError("unreachable")

    # --- the calls ---------------------------------------------------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict]:
        """New messages and button presses, waiting up to `timeout` s for one (a long poll)."""
        got = self.call("getUpdates", {"offset": offset or None, "timeout": timeout,
                                       "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 15)
        return got if isinstance(got, list) else []

    def send(self, chat: int, text: str, buttons: dict | None = None, preview: bool = False,
             quiet: bool = False) -> dict:
        """`quiet`: it arrives without a sound (what the other person did isn't worth a buzz)."""
        return self.call("sendMessage", {"chat_id": chat, "text": clip_text(text, TEXT_LIMIT), "parse_mode": "HTML",
                                         "reply_markup": buttons, "disable_notification": True if quiet else None,
                                         "link_preview_options": None if preview else {"is_disabled": True}})

    def edit(self, chat: int, message: int, text: str, buttons: dict | None = None) -> dict | None:
        return self._edit("editMessageText", {"chat_id": chat, "message_id": message, "parse_mode": "HTML",
                                              "text": clip_text(text, TEXT_LIMIT), "reply_markup": buttons,
                                              "link_preview_options": {"is_disabled": True}})

    def edit_caption(self, chat: int, message: int, caption: str, buttons: dict | None = None) -> dict | None:
        return self._edit("editMessageCaption", {"chat_id": chat, "message_id": message, "parse_mode": "HTML",
                                                 "caption": clip_text(caption, CAPTION_LIMIT),
                                                 "reply_markup": buttons})

    def edit_buttons(self, chat: int, message: int, buttons: dict | None) -> dict | None:
        return self._edit("editMessageReplyMarkup", {"chat_id": chat, "message_id": message,
                                                     "reply_markup": buttons or {"inline_keyboard": []}})

    def _edit(self, method: str, params: dict) -> dict | None:
        try:
            return self.call(method, params)
        except ApiError as exc:
            if exc.unchanged:
                return None
            raise

    def send_video(self, chat: int, video: Path, caption: str, buttons: dict | None = None, width: int = 0,
                   height: int = 0, duration: float = 0.0) -> dict:
        return self.call("sendVideo", {"chat_id": chat, "caption": clip_text(caption, CAPTION_LIMIT),
                                       "parse_mode": "HTML", "supports_streaming": True,
                                       "width": width or None, "height": height or None,
                                       "duration": int(round(duration)) or None, "reply_markup": buttons},
                         files={"video": Path(video)}, timeout=600.0)

    def answer(self, callback: str, text: str = "", alert: bool = False) -> None:
        """Stop the spinner on a pressed button (with a short note at the top of the chat)."""
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback, "text": text[:190] or None,
                                              "show_alert": alert or None})
        except ApiError:
            pass  # too late to answer (over 15 s): the press was still handled

    def typing(self, chat: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat, "action": action}, timeout=10)
        except (ApiError, NetworkError):
            pass

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def set_menu_button(self, chat: int, url: str = "", text: str = "ClipMonster") -> None:
        """The button left of a chat's message box: it opens the Mini App at `url`, or (no url) lists the
        commands, as it does for any bot."""
        button = {"type": "web_app", "text": text, "web_app": {"url": url}} if url else {"type": "commands"}
        self.call("setChatMenuButton", {"chat_id": chat, "menu_button": button})

    def delete(self, chat: int, message: int) -> None:
        try:
            self.call("deleteMessage", {"chat_id": chat, "message_id": message})
        except ApiError:
            pass


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^<>]*>")


def clip_text(text: str, limit: int) -> str:
    """Telegram refuses a message over its limit: cut it, saying so. At a line break when there is one (each
    line's HTML is whole); else mid-line, not inside a tag or an &entity;, with the tags left open closed."""
    if len(text) <= limit:
        return text
    room = limit - 2 - 40  # for "\n…" and closing tags
    head = text[:room]
    if "\n" in head[room // 4:]:
        return head.rsplit("\n", 1)[0] + "\n…"
    lt, gt = head.rfind("<"), head.rfind(">")
    if lt > gt:
        head = head[:lt]  # not half a tag
    amp = head.rfind("&")
    if amp >= 0 and ";" not in head[amp:]:
        head = head[:amp]  # not half an &amp;
    open_tags: list[str] = []
    for m in _TAG.finditer(head):
        name = m.group(2).lower()
        if m.group(1):
            if name in open_tags:
                del open_tags[len(open_tags) - 1 - open_tags[::-1].index(name)]
        else:
            open_tags.append(name)
    return head + "…" + "".join(f"</{t}>" for t in reversed(open_tags))
