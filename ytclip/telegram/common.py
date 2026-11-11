"""What every part of the bot uses: how it writes (HTML that Telegram takes), how it refuses, and how long it
waits for an answer. bot.py, the three handlers and the Mini App all import from here, so none imports another's
internals."""

from __future__ import annotations

import html
import re
import socket

PENDING_LIFE = 30 * 60.0  # a question the bot asked (which category?) is answered within this
MODE_LIFE = 10 * 60.0  # "send me the idea" waits this long for it
PAGE = 8  # videos per page of /videos
DIGEST_MAX = 8  # clips listed in one "new clips" message
GB = 1024 ** 3

URL = re.compile(r"https?://\S+", re.I)


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


def app_link(url: str, item: str = "") -> str:
    """The Mini App's address as a button opens it (at one video, with `item`). It ends in "/" so the page's
    own files are found beside it, also when the address has a folder in it."""
    return f"{url.rstrip('/')}/" + (f"?v={item}" if item else "")
