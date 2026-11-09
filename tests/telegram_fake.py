"""A stand-in for Telegram's Bot API: what the bot sent, and what it gets back (the bot's Api talks to it
through its transport, so the HTTP side, JSON and multipart, is the real code)."""

import json
import re
import time

TOKEN = "123456789:TESTfakeTOKENfakeTOKENfakeTOKENfake12"  # not a real token: real ones start with AA
ME = {"id": 777, "is_bot": True, "first_name": "Clip Test", "username": "ClipTestBot"}
JUSTYN = {"id": 5001, "type": "private", "first_name": "Justyn", "username": "justyn"}


def parse_multipart(body: bytes, kind: str) -> dict:
    boundary = kind.split("boundary=", 1)[1].encode()
    out = {}
    for part in body.split(b"--" + boundary):
        if b"Content-Disposition" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        if b"filename=" in head:
            out[name] = {"file": True, "size": len(content), "head": content[:16]}
        else:
            text = content.decode("utf-8")
            try:
                out[name] = json.loads(text) if text[:1] in "{[" else text
            except ValueError:
                out[name] = text
    return out


class FakeTelegram:
    def __init__(self, token: str = TOKEN, me: dict | None = None):
        self.token = token
        self.me = dict(me or ME)
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.errors: dict[str, list[tuple[int, str, float]]] = {}
        self.messages: dict[tuple[int, int], dict] = {}
        self._update = 1
        self._message = 100
        self.poll_wait = 0.02  # an empty long poll waits a moment, as Telegram's does

    # --- the HTTP side ------------------------------------------------------------------------------
    def transport(self, url, body, headers, timeout):
        head, method = url.rsplit("/", 1)
        if not head.endswith("/bot" + self.token):
            return 401, json.dumps({"ok": False, "error_code": 401, "description": "Unauthorized"}).encode()
        kind = headers.get("Content-Type", "")
        params = parse_multipart(body, kind) if kind.startswith("multipart/") else json.loads(body or b"{}")
        self.calls.append((method, params))
        if self.errors.get(method):
            code, desc, retry = self.errors[method].pop(0)
            data = {"ok": False, "error_code": code, "description": desc}
            if retry:
                data["parameters"] = {"retry_after": retry}
            return code, json.dumps(data).encode()
        try:
            result = getattr(self, "m_" + method)(params)
        except LookupError as exc:
            return 400, json.dumps({"ok": False, "error_code": 400, "description": str(exc)}).encode()
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # --- methods --------------------------------------------------------------------------------------
    def m_getMe(self, p):
        return self.me

    def m_getUpdates(self, p):
        offset = int(p.get("offset") or 0)
        self.updates = [u for u in self.updates if u["update_id"] >= offset]
        if not self.updates:
            time.sleep(self.poll_wait)
        return list(self.updates)

    def _new(self, chat, **fields):
        self._message += 1
        self.messages[(int(chat), self._message)] = dict(fields)
        return {"message_id": self._message, "chat": {"id": int(chat), "type": "private"}, **fields}

    def m_sendMessage(self, p):
        return self._new(p["chat_id"], text=p["text"], markup=p.get("reply_markup"))

    def m_sendVideo(self, p):
        assert p["video"]["file"]
        return self._new(int(p["chat_id"]), caption=p.get("caption", ""), markup=p.get("reply_markup"),
                         video={"size": p["video"]["size"]})

    def _edit(self, p, **fields):
        key = (int(p["chat_id"]), int(p["message_id"]))
        if key not in self.messages:
            raise LookupError("Bad Request: message to edit not found")
        before = dict(self.messages[key])
        self.messages[key].update(fields)
        if self.messages[key] == before:
            raise LookupError("Bad Request: message is not modified")
        return True

    def m_editMessageText(self, p):
        return self._edit(p, text=p["text"], markup=p.get("reply_markup"))

    def m_editMessageCaption(self, p):
        return self._edit(p, caption=p["caption"], markup=p.get("reply_markup"))

    def m_editMessageReplyMarkup(self, p):
        return self._edit(p, markup=p.get("reply_markup"))

    def m_answerCallbackQuery(self, p):
        return True

    m_setMyCommands = m_sendChatAction = m_pinChatMessage = m_deleteMessage = m_answerCallbackQuery

    # --- for the tests ----------------------------------------------------------------------------------
    def message(self, text: str, chat: dict | None = None) -> dict:
        self._update += 1
        return {"update_id": self._update, "message": {"message_id": 1, "chat": dict(chat or JUSTYN),
                                                       "from": dict(chat or JUSTYN), "text": text}}

    def press(self, data: str, message_id: int, chat: dict | None = None) -> dict:
        self._update += 1
        msg = {"message_id": message_id, "chat": dict(chat or JUSTYN)}
        stored = self.messages.get(((chat or JUSTYN)["id"], message_id), {})
        if "video" in stored:
            msg["video"] = stored["video"]
        return {"update_id": self._update, "callback_query": {"id": f"cb{self._update}", "data": data,
                                                              "message": msg, "from": dict(chat or JUSTYN)}}

    def sent(self, method: str | None = None) -> list[dict]:
        return [p for m, p in self.calls if method is None or m == method]

    def last(self, method: str = "sendMessage") -> dict:
        got = self.sent(method)
        assert got, f"nothing was sent with {method}"
        return got[-1]

    def clear(self) -> None:
        self.calls.clear()


def buttons(markup) -> list[tuple[str, str]]:
    """Every inline button in a reply_markup: (label, callback data or url)."""
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [(b["text"], b.get("callback_data") or b.get("url", "")) for row in (markup or {}).get("inline_keyboard", [])
            for b in row]
