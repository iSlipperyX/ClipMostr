"""The Mini App: ClipMonster inside Telegram, served by this PC.

A Telegram Mini App is a web page Telegram opens inside the chat with the bot. This is that page's server:
it listens on this PC only (127.0.0.1), and a tunnel (tunnel.py) or an address of your own (Settings →
Telegram → Mini App address) carries it to the phone over HTTPS, which Telegram requires.

Who may use it: Telegram signs what it hands the page (initData) with the bot's token, so the server can
tell that a request comes from Telegram, from which person, and how long ago (verify_init_data). Only the
people linked to the bot get a session; everyone else gets "not linked". A session lasts a day and is
bound to this run of the server. Videos and pictures are asked for by <video> and <img>, which can't send a
header, so they carry a second token in their address that opens videos and pictures and nothing else.

  GET  / , /app.css , /app.js             the page, its style and its script (nothing private is in them)
  POST /api/session {init_data}           who you are, who shares the bot, the PC's name
  GET  /api/videos                        every video waiting (to review, queued, in the library), with what
                                          was said about each and by whom
  GET  /api/posted                        the latest posts and their numbers
  GET  /api/videos/<id>/file | thumb ?m=  the video (with Range, so it streams) and its picture; file?dl=1 hands
                                          it over as a download (the full-quality file, under its headline)
  POST /api/videos/<id>/ok | post-now | retry | delete
  POST /api/videos/<id>/send              the bot sends the file into the chat, to save from there
  POST /api/videos/<id>/queue {category}  a video made to download joins a posting queue after all
  GET  /api/videos/<id>/edit              what the sports editor opens: the plays, the words, the look
  POST /api/videos/<id>/edit {changes}    cut it again on the PC with those changes: the answer is v2
  GET  /api/sports/source/<game> ?m=      the game's own footage, streamed, for the editor to show a cut
  POST /api/videos/<id>/critique {text}   change it or make it again (review_pipeline.py): the answer is v2
  POST /api/videos/<id>/approve           this version posts; its other versions are deleted from the PC
  GET  /api/status                        POST /api/pause {paused}
  GET  /api/categories                    POST /api/make {idea, category, seconds}: an idea or an article's
                                          link, read as a message is; category "auto" finds it from the words
  GET  /api/sports                        the sports form's choices, and the sports requests so far
  GET  /api/sports/played ?sport= &league=  the games of the last 24 hours, to pick from (sportsdesk/slate.py)
  POST /api/sports/order {game} or {sport, league, teams, since, want, games, plays, category}
  POST /api/sports/orders/<id>/cancel     a game's best plays and a recap reel, made without anyone picking
  POST /api/sports/find {query, game}     search a game for a moment said in words; what's found comes back on
                                          the request (its "moments")
  GET  /api/sports/moments ?game=         a game's found moments, and which are set aside for its reel
  POST /api/sports/moments {game, ids, want}   make them: each a clip ("clips"), or all one reel ("reel")
  POST /api/sports/basket {game, id, add}      set a moment aside for the game's reel, or take it out

Every request but the page itself and the sign-in carries a session, and the session's person is looked up
among the linked chats each time: someone removed from the bot is out at their next request. What changes
something is rate-limited per person (RATE), and so are sign-in attempts as a whole.
  GET  /api/activity                      who did what, lately
  POST /api/people/invite | leave         POST /api/people/remove {id}
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

from .. import __version__
from ..remote.host import Host, HostError, pc_name
from . import state
from .common import Refused, esc

PORT = 47330
BODY_MAX = 64 * 1024
CHUNK = 256 * 1024
INIT_MAX_AGE = 24 * 3600.0  # how old Telegram's signed sign-in may be
SESSION_LIFE = 24 * 3600.0
TUNNEL_PATIENCE = 45.0  # how long a starting tunnel is waited for before the bot is told there's no address
RATE = {"post": (40, 10.0), "signin": (30, 60.0)}  # (how many, in how many seconds): per person; sign-ins, in all
CSP = ("default-src 'none'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")


class AuthError(Exception):
    """Why a request isn't let in, in words for the page. `code` tells the page what to do about it: "signin"
    (open it again from the chat), "unlinked" (this person doesn't share the bot) or "expired" (the session ran
    out: sign in again with what Telegram handed the page)."""

    def __init__(self, message: str, code: str = "signin"):
        super().__init__(message)
        self.code = code


def verify_init_data(init_data: str, token: str, now: float, max_age: float = INIT_MAX_AGE) -> dict:
    """The Telegram user behind a Mini App's initData, if Telegram signed it with this bot's token and not
    too long ago. Raises AuthError. (core.telegram.org/bots/webapps: "Validating data received via the Mini
    App".)"""
    if not init_data or not token or len(init_data) > 8192:
        raise AuthError("Open this from the bot's chat in Telegram.")
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    given = fields.pop("hash", "")
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not given or not hmac.compare_digest(want.encode(), given.lower().encode()):  # bytes: any text compares
        raise AuthError("Telegram's sign-in didn't check out. Close this and open it again from the bot's chat.")
    try:
        at = float(fields.get("auth_date") or 0)
        user = json.loads(fields.get("user") or "{}")
        uid = int(user["id"])
    except (ValueError, TypeError, KeyError):
        raise AuthError("Telegram's sign-in had no user in it. Open this from the bot's chat.") from None
    if not (-300 <= now - at <= max_age):
        raise AuthError("That sign-in is too old. Close this and open it again from the bot's chat.")
    if user.get("is_bot"):
        raise AuthError("Bots can't use this.")
    return {"id": uid, "first_name": str(user.get("first_name") or ""), "last_name": str(user.get("last_name") or ""),
            "username": str(user.get("username") or "")}


class AppHost(Host):
    """The posting library and automation, as the Mac link sees them, for the Mini App."""

    def log(self, msg: str) -> None:
        try:
            self.runner.log(f"Telegram app: {msg}")
        except Exception:  # noqa: BLE001
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX: a restart can listen again at once. Windows: SO_REUSEADDR would let this take a port another program
    # is already listening on (and the other way round), so a port in use would never be noticed there.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 32


class WebApp:
    """The Mini App's server, run by automation next to the bot. It waits quietly until Settings → Telegram →
    Mini App is on and the bot has a token; settings are read again every few seconds."""

    def __init__(self, bot, host: Host | None = None, now: Callable[[], float] = time.time,
                 tunnel_factory=None, port: int | None = None):
        self.bot = bot
        self.now = now
        self.host = host or AppHost(bot.runner, now=now, posting=bot._posting)
        self.secret = secrets.token_bytes(32)
        self.httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self.fixed_port = port
        self.url = ""  # the public https address, once there is one
        self.error = ""
        self.tunnel = None
        self.tunnel_factory = tunnel_factory
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._told: tuple[str, str] | None = None  # what the bot was last told: (address, why there's none)
        self._wanted_at = 0.0
        self._hits: dict[str, list[float]] = {}  # "post:<uid>" / "signin" -> when its last requests came
        self._hits_lock = threading.Lock()
        self.clock = time.monotonic

    # --- running ---------------------------------------------------------------------------------------

    def start(self) -> None:
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._watch, name="telegram-app", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(5.0)
        self._close()

    def _watch(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.configure()
            except Exception as exc:  # noqa: BLE001 - try again on the next look
                self.error = f"{type(exc).__name__}: {exc}"
            self.stop_event.wait(3.0)

    def wanted(self, s) -> bool:
        return bool(getattr(s, "telegram_on", False) and getattr(s, "telegram_app", True)
                    and (getattr(s, "telegram_token", "") or "").strip())

    def configure(self) -> None:
        """Start, stop or re-point the server and its tunnel to match Settings → Telegram."""
        s = self.bot.settings()
        if not self.wanted(s):
            if self.httpd is not None:
                self._close()
                self.bot.log("The Mini App is off.")
            self._wanted_at = 0.0
            self._tell("", "")
            return
        self._wanted_at = self._wanted_at or self.now()
        port = self.fixed_port if self.fixed_port is not None else int(getattr(s, "telegram_app_port", PORT) or PORT)
        if self.httpd is not None and self.fixed_port is None and port != self.port:
            self._close()
        if self.httpd is None:
            try:
                self.serve(port)
            except OSError as exc:
                self.error = (f"Port {port} on this PC can't be used ({exc.strerror or exc}): another program may "
                              "have it. Pick another in Settings → Telegram → Mini App port.")
                self._tell("", self.error)
                return
        own = (getattr(s, "telegram_app_url", "") or "").strip().rstrip("/")
        if own:
            self._stop_tunnel()
            url = own if own.startswith("https://") else ""
            self.error = "" if url else "The Mini App address must start with https:// (Telegram only opens those)."
        else:
            if self.tunnel is None:
                from .tunnel import Tunnel

                self.tunnel = (self.tunnel_factory or Tunnel)(self.port, log=self.bot.log)
                self.tunnel.start()
            url, self.error = self.tunnel.url, self.tunnel.error
        self.url = url
        if url or self.error or self.now() - self._wanted_at > TUNNEL_PATIENCE:
            self._tell(url, self.error)  # (a tunnel still starting isn't "no address" yet)

    def _tell(self, url: str, error: str) -> None:
        if (url, error) != self._told:
            self._told = (url, error)
            self.bot.set_app_url(url, error)

    def serve(self, port: int) -> None:
        handler = type("Handler", (_Handler,), {"app": self})
        self.httpd = _Server(("127.0.0.1", port), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, name="telegram-app-http", daemon=True).start()
        self.bot.log(f"The Mini App is being served on this PC (port {self.port}).")

    def _stop_tunnel(self) -> None:
        if self.tunnel is not None:
            self.tunnel.stop()
            self.tunnel = None

    def _close(self) -> None:
        self._stop_tunnel()
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        self.url = ""

    # --- who is asking ---------------------------------------------------------------------------------

    def limit(self, kind: str, who: int | str = "") -> None:
        """Count a request against RATE[kind]; HostError 429 when there have been too many (a script, a
        stuck button): the PC isn't handed a queue of them."""
        most, span = RATE[kind]
        key, t = f"{kind}:{who}", self.clock()
        with self._hits_lock:
            seen = [x for x in self._hits.get(key, []) if t - x < span]
            seen.append(t)
            self._hits[key] = seen[-(most + 1):]
            if len(self._hits) > 500:  # (only linked people get this far, so it never grows: a belt and braces)
                self._hits = {key: self._hits[key]}
            over = len(seen) > most
        if over:
            raise HostError(429, "That's a lot at once. Give it a few seconds, then try again.")

    def _sign(self, uid: int, until: int, use: str = "api") -> str:
        return hmac.new(self.secret, f"{use}.{uid}.{until}".encode(), hashlib.sha256).hexdigest()[:40]

    def session_for(self, init_data: str) -> tuple[str, str, dict]:
        """A session for the linked person Telegram vouches for: (its token, the token that only opens videos
        and pictures, their chat). Raises AuthError."""
        self.limit("signin")
        user = verify_init_data(init_data, self.bot.token, self.now())
        chat = self.linked(user["id"])
        self.bot.seen(chat["id"], user)
        until = int(self.now() + SESSION_LIFE)
        uid = user["id"]
        return (f"{uid}.{until}.{self._sign(uid, until)}", f"{uid}.{until}.{self._sign(uid, until, 'media')}", chat)

    def linked(self, uid: int) -> dict:
        data = state.load(self.bot.state_file)
        chat = next((c for c in data["chats"] if c["id"] == uid), None)
        if chat is None:
            raise AuthError("This Telegram account isn't linked to ClipMonster. Ask for an invite, or link it "
                            "from the PC: Settings → Telegram → Link a phone.", "unlinked")
        return chat

    def who(self, token: str, use: str = "api") -> dict:
        """The linked chat a token belongs to (`use`: "api", or "media" for a video or picture's address).
        Raises AuthError."""
        try:
            uid, until, sig = (token or "").split(".")
            uid, until = int(uid), int(until)
        except ValueError:
            raise AuthError("Open this from the bot's chat in Telegram.") from None
        if not hmac.compare_digest(sig.encode(), self._sign(uid, until, use).encode()) or self.now() > until:
            raise AuthError("The session ran out.", "expired")
        return self.linked(uid)

    # --- what the page asks for ------------------------------------------------------------------------

    def me_json(self, chat: dict) -> dict:
        data = state.load(self.bot.state_file)
        return {"me": {"id": chat["id"], "name": state.first_name(data, chat["id"]),
                       "owner": state.is_owner(data, chat["id"])},
                "people": [{"id": c["id"], "name": state.chat_name(c), "first": state.first_name(data, c["id"]),
                            "owner": state.is_owner(data, c["id"]), "since": c.get("linked_at") or 0,
                            "you": c["id"] == chat["id"]} for c in state.people(data)],
                "most": state.MAX_PEOPLE, "pc": pc_name(), "version": __version__}

    def videos_json(self) -> dict:
        self.bot.sync_critiques()
        out = self.host.videos()
        data = state.load(self.bot.state_file)
        store = self.host.posting()[0]
        for v in out.get("items", []):
            self._dress(v, data, store)
        return out

    def _dress(self, v: dict, data: dict, store) -> None:
        """Add to a video what people said about it, and whether it can be made again from a critique."""
        said = state.critiques_of(data, v["id"])
        v["critiques"] = [self.bot.critique_json(c) for c in said]
        v["fix_of"] = next((c.get("item") or "" for c in said if c.get("fixed") == v["id"]), "")
        v["fixing"] = any(c.get("item") == v["id"] and c.get("status") in ("queued", "working") for c in said)
        it = store.get(v["id"])
        v["fixable"] = bool(v.get("exists")) and self.bot.fixable(it)
        # Which version it is, and the versions to choose between (approving one deletes the others)
        v["editable"] = self.bot.editable(it) if it is not None and v.get("exists") else ""
        v["version"] = self.bot.version_label(it, data) if it is not None else ""
        v["versions"] = self.bot.versions_json(it, data, store) if it is not None else []

    def source_file(self, game_id: str) -> Path:
        """The footage of a game on the Sports page, as it was downloaded to this PC: the editor plays a cut
        from it before the cut is made. Only a game the desk lists, and only its own download in the cache."""
        from ..sportsdesk import editor
        from ..sportsdesk.desk import DeskError

        try:
            g = self.bot.desk.game(game_id)
        except DeskError as exc:
            raise HostError(exc.status, exc.message) from exc
        path = editor.source_file(self.bot.settings(), g.get("video") or "")
        if path is None:
            raise HostError(404, "This game's footage isn't on the PC any more (it's downloaded again when an "
                                 "edit is made).")
        return path

    def api(self, method: str, path: str, body: dict, chat: dict) -> dict:
        me = chat["id"]
        bot, host = self.bot, self.host
        if method == "POST":
            self.limit("post", me)
        if path == "/api/me" and method == "GET":
            return self.me_json(chat)
        if path == "/api/videos" and method == "GET":
            return self.videos_json()
        if path == "/api/posted" and method == "GET":
            return host.posted(40)
        if path == "/api/status" and method == "GET":
            st = host.status()
            auto = st.get("automation") or {}
            job = st.get("job")
            try:  # what the Sports worker is on: the game, the step, how far (and how many wait)
                sport = bot.desk.doing()
                sport_waiting = sport["waiting"] if sport else bot.desk.waiting()
            except Exception:  # noqa: BLE001 - sports.db busy for a moment
                sport, sport_waiting = None, 0
            return {"paused": bool(auto.get("paused")), "doing": (auto.get("activity") or {}).get("text") or "",
                    "state": str(auto.get("state") or ""),
                    "sports": ({k: sport[k] for k in ("kind", "what", "title", "stage", "frac", "text", "emoji", "by",
                                                      "waiting")} if sport else None),
                    "sports_waiting": sport_waiting,
                    "job": {"title": job["title"], "making": job["making"], "stage": job["stage"]} if job else None,
                    "queue": st.get("queue") or {}, "posting": st.get("posting"),
                    "problems": list(st.get("problems") or [])[:5]}
        if path == "/api/pause" and method == "POST":
            paused = bool(body.get("paused"))
            out = host.pause(paused)
            bot.did(me, "pause" if paused else "resume")
            return out
        if path == "/api/categories" and method == "GET":
            return {"items": [{"id": c["id"], "name": c["name"], "paused": bool(c["paused"]), "account": c["account"]}
                              for c in host.categories()["items"] if c.get("enabled", True)]}
        if path == "/api/make" and method == "POST":
            return bot.app_make(me, str(body.get("idea") or ""), str(body.get("category") or ""),
                                body.get("seconds") or 0)
        if path == "/api/sports" and method == "GET":
            out = bot.sports_json()
            for o in out["orders"]:
                o["mine"] = o.get("by_id") == me
            return out
        if path == "/api/sports/played" and method == "GET":
            return bot.played_json(str(body.get("sport") or "any"), str(body.get("league") or ""))
        if path == "/api/sports/order" and method == "POST":
            if body.get("game"):  # a game picked from the games played, not typed
                r = bot.sports_game(me, str(body["game"])[:80], str(body.get("want") or "reel"), body.get("plays"),
                                    str(body.get("category") or ""), str(body.get("deliver") or "")[:12])
            else:
                r = bot.sports_order(me, body)
            return {k: r[k] for k in ("order", "message", "notes")}
        if path == "/api/sports/find" and method == "POST":
            r = bot.app_find(me, str(body.get("query") or ""), str(body.get("game") or ""))
            return {k: r[k] for k in ("order", "message")}
        if path == "/api/sports/moments" and method == "GET":
            return bot.moments_json(str(body.get("game") or "")[:80])
        if path == "/api/sports/moments" and method == "POST":
            ids = body.get("ids") if isinstance(body.get("ids"), list) else []
            r = bot.sports_moments(me, str(body.get("game") or "")[:80], [str(i)[:40] for i in ids[:8]],
                                   str(body.get("want") or "clips"),
                                   str(body["category"]) if body.get("category") else None,
                                   str(body.get("deliver") or "")[:12])
            return {k: r[k] for k in ("order", "message", "notes")}
        if path == "/api/sports/basket" and method == "POST":
            return bot.app_basket(str(body.get("game") or "")[:80], str(body.get("id") or "")[:40],
                                  body.get("add", True) is not False)
        m = re.fullmatch(r"/api/sports/orders/([a-z0-9]{1,24})/cancel", path)
        if m and method == "POST":
            return bot.sports_cancel(me, m.group(1))
        if path == "/api/activity" and method == "GET":
            data = state.load(bot.state_file)
            going = [bot.critique_json(c) for c in data["critiques"].values()
                     if c.get("status") in ("queued", "working")]
            return {"items": [dict(a, mine=a.get("by") == me) for a in reversed(data["activity"])][:60],
                    "going": sorted(going, key=lambda c: c["at"])}
        if path == "/api/people/invite" and method == "POST":
            return bot.invite(me)
        if path == "/api/people/leave" and method == "POST":
            return bot.leave(me)
        if path == "/api/people/remove" and method == "POST":
            try:
                other = int(body.get("id") or 0)
            except (TypeError, ValueError):
                raise HostError(400, "Say who to remove.") from None
            return bot.remove(me, other)
        m = re.fullmatch(r"/api/videos/([A-Za-z0-9_-]{1,64})/edit", path)
        if m and method == "GET":
            return bot.edit_doc(m.group(1))
        if m and method == "POST":
            changes = body.get("changes")
            r = bot.edit(me, m.group(1), changes if isinstance(changes, dict) else {})
            return {k: r[k] for k in ("critique", "edit", "version", "held", "message")}
        m = re.fullmatch(r"/api/videos/([A-Za-z0-9_-]{1,64})/(send|queue)", path)
        if m and method == "POST":
            if m.group(2) == "send":
                return bot.send_file(me, m.group(1))
            return bot.queue_download(me, m.group(1), str(body.get("category") or "")[:64])
        m = re.fullmatch(r"/api/videos/([A-Za-z0-9_-]{1,64})/(ok|post-now|retry|delete|critique|approve)", path)
        if m and method == "POST":
            item, what = m.groups()
            if what == "critique":
                return bot.fix(me, item, str(body.get("text") or "")[:4000])
            if what == "approve":
                return bot.approve_version(me, item)
            title = host.video(item)["headline"]
            out = {"ok": host.ok, "post-now": host.post_now, "retry": host.retry, "delete": host.delete}[what](item)
            # the cards of it in both chats follow, and the other person is told who did it
            bot.settle(item, gone=f"🗑 {esc(out.get('message') or 'Deleted.')}" if what == "delete" else "")
            bot.did(me, what.replace("-", "_"), title, item)
            return out
        raise HostError(404, "There's nothing at that address.")


MiniApp = WebApp  # the name the module goes by now; WebApp is what 3.10 and 3.11 called it


class _Handler(BaseHTTPRequestHandler):
    app: WebApp
    protocol_version = "HTTP/1.1"
    server_version = "ClipMonster"
    sys_version = ""
    timeout = 60  # a connection that says nothing is dropped

    def log_message(self, fmt, *args):  # quiet: automation's log isn't a web log
        return

    # --- answers ---------------------------------------------------------------------------------------

    def send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_page(self, which: str = "page") -> None:
        from . import mini_app_page as page

        text, kind = {"page": (page.PAGE, "text/html"), "css": (page.CSS, "text/css"),
                      "js": (page.JS, "text/javascript")}[which]
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_file(self, path: Path, save_as: str = "") -> None:
        """A file, with Range (so a video streams and seeks). `save_as` hands it over as a download under that
        name instead of something to play."""
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and size and (m.group(1) or m.group(2)):
            a, b = m.group(1), m.group(2)
            if a:
                start, end = int(a), min(size - 1, int(b)) if b else size - 1
            else:  # the last N bytes
                start = max(0, size - int(b))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1 if size else 0
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=300")
        self.send_header("X-Content-Type-Options", "nosniff")
        if save_as:
            plain = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", save_as).strip(" .") or "video.mp4"
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(save_as, safe='')}")
            # Telegram's own downloader (WebApp.downloadFile) fetches it from the web client's origin
            self.send_header("Access-Control-Allow-Origin", "https://web.telegram.org")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        at, left = start, length
        while left > 0:
            # The file is opened for each piece and let go before the piece is sent: a phone that has the video
            # paused holds this connection for minutes, and Windows won't delete a file that's open (Delete,
            # tapped right under the player, would fail).
            try:
                with open(path, "rb") as f:
                    f.seek(at)
                    chunk = f.read(min(CHUNK, left))
            except OSError:
                chunk = b""
            if not chunk:  # deleted (or cut short) meanwhile: the answer ends early, and the connection with it
                self.close_connection = True
                break
            self.wfile.write(chunk)
            at += len(chunk)
            left -= len(chunk)

    def read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > BODY_MAX:
            self.close_connection = True
            raise HostError(413, "That's too much to send.")
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HostError(400, "That wasn't JSON.") from None
        if not isinstance(data, dict):
            raise HostError(400, "Send an object.")
        return data

    # --- requests --------------------------------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - http.server's name
        self.handle_any("GET")

    def do_HEAD(self):  # noqa: N802
        self.handle_any("GET")

    def do_POST(self):  # noqa: N802
        self.handle_any("POST")

    def handle_any(self, method: str) -> None:
        app = self.app
        try:
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if method == "GET" and path in ("/", "/index.html", "/app.css", "/app.js"):
                self.send_page({"/app.css": "css", "/app.js": "js"}.get(path, "page"))
                return
            body = self.read_json() if method == "POST" else {k: v[0] for k, v in query.items() if v}
            if path == "/api/session" and method == "POST":
                token, media, chat = app.session_for(str(body.get("init_data") or ""))
                self.send_json(200, {"token": token, "media": media, **app.me_json(chat)})
                return
            m = re.fullmatch(r"/api/videos/([A-Za-z0-9_-]{1,64})/(file|thumb)", path)
            if m and method == "GET":
                app.who((query.get("m") or [""])[0], "media")
                item, what = m.groups()
                if what == "file" and (query.get("dl") or [""])[0] == "1":
                    self.send_file(app.host.video_file(item), save_as=app.host.download_name(item))
                else:
                    self.send_file(app.host.video_file(item) if what == "file" else app.host.thumb_file(item))
                return
            m = re.fullmatch(r"/api/sports/source/([A-Za-z0-9_.:-]{1,80})", path)
            if m and method == "GET":
                app.who((query.get("m") or [""])[0], "media")
                self.send_file(app.source_file(m.group(1)))
                return
            auth = self.headers.get("Authorization") or ""
            chat = app.who(auth[4:].strip() if auth.lower().startswith("tma ") else "")
            self.send_json(200, app.api(method, path, body, chat))
        except AuthError as exc:
            self.send_json(401, {"error": str(exc), "code": exc.code})
        except (HostError, Refused) as exc:
            self.send_json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - say so, rather than drop the connection
            app.bot.log(f"⚠ The Mini App couldn't answer {self.path.split('?')[0]}: {type(exc).__name__}: {exc}")
            try:
                why = (str(exc) or type(exc).__name__)[:200]
                self.send_json(500, {"error": "Something went wrong on the PC: " + why})
            except OSError:
                pass
