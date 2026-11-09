"""The HTTPS server the Mac app talks to, run by automation next to the Telegram bot.

It waits quietly until Settings → Mac app turns it on, then listens on the port set there (47320 unless
changed), on every network the PC is on, and answers a "who's there?" broadcast on the local network so the
Mac finds the PC by itself. Every request but /api/hello and /api/pair needs a linked Mac's key; see
protocol.py for how a Mac is linked. Settings are read again every few seconds, so turning it off, changing
the port or unlinking a Mac takes effect without restarting automation.

Requests (JSON in and out unless said):
  GET  /api/hello                         name, version, whether a code is up (no key needed)
  POST /api/pair                          link a Mac with the code (no key needed)
  GET  /api/status                        what automation and posting are doing
  GET  /api/events?after=N&wait=S         news since event N (posts, problems), waiting up to S seconds
  GET  /api/videos?rev=R                  the videos waiting to post (or {"same": true})
  GET  /api/videos/<id>                   one of them
  GET  /api/videos/<id>/thumb             its picture (JPEG)
  GET  /api/videos/<id>/file              the video (MP4, with Range so it streams)
  POST /api/videos/<id>/ok | post-now | retry | delete | edit {caption, hashtags}
  GET  /api/posted?limit=N                the latest posts and their numbers
  POST /api/automation {paused}
  GET  /api/categories                    POST /api/categories/<id> {paused}    POST /api/scan {category}
  POST /api/make {idea, category}         POST /api/clip {links, category}
  GET  /api/requests                      POST /api/requests {...}              POST /api/requests/<id>/cancel
  GET  /api/week                          GET  /api/log?lines=N                 POST /api/unlink (this Mac)

What a Mac may hand this PC (studio.py):
  GET  /api/ai                            whether this PC's local AI can answer, and its model
  POST /api/ai/tasks {kind: ask|find, …}  ask it something; GET /api/ai/tasks/<id>?wait=S for the answer
  POST /api/ai/tasks/<id>/cancel
  PUT  /api/studio/uploads?name=N         a video from the Mac (the body is the file)
  GET  /api/studio/jobs                   this Mac's videos made here    POST /api/studio/jobs {source|upload, settings}
  GET  /api/studio/jobs/<id>              GET  /api/studio/jobs/<id>/file?path=P (one of its clips' files)
  POST /api/studio/jobs/<id>/cancel | done (the Mac has the clips: they go)
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from . import certs, protocol, state
from .host import Host, HostError

HEARTBEAT = 5.0
BODY_MAX = 256 * 1024
AI_BODY_MAX = 8 * 1024 * 1024  # a question for the AI carries a whole transcript
DRAIN_MAX = 256 * 1024 * 1024  # a video the PC says no to is read to its end (and dropped) up to this big, so the
# Mac can hear the no; a bigger one's connection is closed on it instead
CHUNK = 256 * 1024
HANDSHAKE_TIMEOUT = 15.0
IDLE_TIMEOUT = 60.0
EVENTS_WAIT_MAX = 25.0
FAILS_PER_MINUTE = 12  # wrong keys from one address before it's made to wait a minute
ID = r"([A-Za-z0-9_\-~.]{1,80})"


def local_addresses() -> list[str]:
    """This PC's addresses on its networks (not 127.0.0.1), the likely one first."""
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent: it only picks the route
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.") and not ip.startswith("169.254.")]


GCE_PRODUCT = Path("/sys/class/dmi/id/product_name")
GCE_EXTERNAL_IP = ("http://169.254.169.254/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/"
                   "external-ip")
_outside: tuple[float, list[str]] | None = None
OUTSIDE_FOR = 600.0  # s an outside address is remembered


def on_google_cloud() -> bool:
    """A Google Compute Engine VM (its firmware says so; nothing is asked over the network)."""
    try:
        return "Google" in GCE_PRODUCT.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def gce_external_ip(timeout: float = 0.6) -> str:
    """This VM's external IP address, from Google's metadata server ('' when it has none)."""
    import urllib.request

    req = urllib.request.Request(GCE_EXTERNAL_IP, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:
            ip = r.read(64).decode("ascii", "replace").strip()
    except (OSError, ValueError):
        return ""
    return ip if re.fullmatch(r"[0-9.]{7,15}", ip) else ""


def tailscale_ips(timeout: float = 2.0) -> list[str]:
    """This computer's Tailscale addresses (100.x.y.z), when Tailscale is installed and up."""
    import shutil
    import subprocess

    exe = shutil.which("tailscale")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ip for ip in out.split() if re.fullmatch(r"100\.[0-9.]+", ip)]


def outside_addresses(now: float | None = None) -> list[str]:
    """Addresses a Mac elsewhere reaches this computer at: Tailscale first (private and encrypted), then a
    Google Cloud VM's external IP. Looked up at most every ten minutes."""
    global _outside
    now = time.monotonic() if now is None else now
    if _outside is not None and now - _outside[0] < OUTSIDE_FOR:
        return list(_outside[1])
    found = tailscale_ips()
    if on_google_cloud():
        ip = gce_external_ip()
        if ip and ip not in found:
            found.append(ip)
    _outside = (now, found)
    return list(found)


def mac_addresses() -> list[str]:
    """Every address to type in the Mac app, the one most likely to work first: Tailscale, a cloud VM's
    external IP, then this computer's own network addresses."""
    out = outside_addresses()
    return out + [ip for ip in local_addresses() if ip not in out]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX: a restart can listen again at once (old connections in TIME_WAIT). Windows: SO_REUSEADDR would let a
    # second program take the same port, and TIME_WAIT doesn't stop a new listener there anyway.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 32

    def __init__(self, addr, handler, ctx: ssl.SSLContext, remote: "RemoteServer"):
        self.ctx = ctx
        self.remote = remote
        self.open_sockets: set = set()  # every connection a Mac has open, closed when the server stops
        self._sockets_lock = threading.Lock()
        super().__init__(addr, handler)

    def close_connections(self) -> None:
        """End every open connection, so a Mac kept connected (HTTP keep-alive) notices at once that the
        server stopped, instead of being answered until its connection goes idle."""
        with self._sockets_lock:
            socks, self.open_sockets = list(self.open_sockets), set()
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def finish_request(self, request, client_address):
        """The TLS handshake on the request's own thread, so a slow one never holds up the others."""
        self._track(request, True)
        try:
            request.settimeout(HANDSHAKE_TIMEOUT)
            tls = self.ctx.wrap_socket(request, server_side=True)  # (request is now tls's)
            tls.settimeout(IDLE_TIMEOUT)
        except (OSError, ssl.SSLError):
            try:
                request.close()
            except OSError:
                pass
            return
        finally:
            self._track(request, False)
        self._track(tls, True)
        try:
            self.RequestHandlerClass(tls, client_address, self)
        finally:
            self._track(tls, False)
            try:
                tls.close()
            except OSError:
                pass

    def _track(self, sock, open_: bool) -> None:
        with self._sockets_lock:
            if open_:
                self.open_sockets.add(sock)
            else:
                self.open_sockets.discard(sock)

    def handle_error(self, request, client_address):  # noqa: D401 - quiet: a dropped connection isn't news
        pass


class Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    server_version = "ClipMonster"
    sys_version = ""
    body_read = False

    def log_message(self, format, *args):  # noqa: A002 - no console lines
        pass

    @property
    def remote(self) -> "RemoteServer":
        return self.server.remote

    # --- answering ---------------------------------------------------------------------------------------

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def fail(self, status: int, message: str) -> None:
        self.send_json(status, {"error": message})

    def body(self, limit: int = BODY_MAX) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise HostError(413, "That's too much to send.")
        raw = self.rfile.read(n) if n else b""
        self.body_read = True
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise HostError(400, "That wasn't JSON.") from exc
        if not isinstance(data, dict):
            raise HostError(400, "Send an object.")
        return data

    def send_file(self, path: Path, kind: str = "") -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range") or ""
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and size:
            a, b = m.group(1), m.group(2)
            if a:
                start = int(a)
                end = min(size - 1, int(b)) if b else size - 1
            elif b:  # the last N bytes
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
        self.send_header("Content-Type", kind or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=300")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD" or not length:
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(CHUNK, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # --- requests ------------------------------------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        self.route("GET")

    def do_HEAD(self):  # noqa: N802
        self.route("GET")

    def do_POST(self):  # noqa: N802
        self.route("POST")

    def do_PUT(self):  # noqa: N802
        self.route("PUT")

    def route(self, method: str) -> None:
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        ip = self.client_address[0]
        self.body_read = False  # (one handler answers every request on a kept-alive connection)
        try:
            if path == "/api/hello" and method == "GET":
                return self.send_json(200, self.remote.hello())
            if path == "/api/pair" and method == "POST":
                return self.send_json(200, self.remote.pair(self.body(), ip))
            if not path.startswith("/api/"):
                return self.fail(404, "Nothing here.")
            device = self.remote.authorize(self.headers.get("Authorization") or "", ip)
            self.device = device
            self.dispatch(method, path, q)
        except HostError as exc:
            if exc.status == 401 or self._unread(method):
                self.close_connection = True
            self.fail(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, TimeoutError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - one bad request never stops the server
            self.remote.log(f"⚠ A request went wrong ({method} {path}): {type(exc).__name__}: {exc}")
            if self._unread(method):
                self.close_connection = True
            try:
                self.fail(500, f"Something went wrong on the PC: {type(exc).__name__}: {exc}")
            except OSError:
                self.close_connection = True

    def _unread(self, method: str) -> bool:
        """A request whose body may still be (partly) on the connection, after a no: the connection can't carry
        another request, so it closes after the answer."""
        if method == "PUT":
            return not self.body_read
        try:
            return method == "POST" and not self.body_read and int(self.headers.get("Content-Length") or 0) > 0
        except ValueError:
            return True

    def _drain(self, size: int) -> None:
        """Read a body the PC won't use to its end, so the answer to it can be heard (the Mac is still sending
        it); too big a body is left, and the connection closes on it."""
        if not 0 < size <= DRAIN_MAX:
            return
        left = size
        try:
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    return
                left -= len(chunk)
        except (OSError, ssl.SSLError):
            return
        self.body_read = True

    def dispatch(self, method: str, path: str, q: dict) -> None:
        host = self.remote.host
        get = method == "GET"
        if get and path == "/api/status":
            return self.send_json(200, host.status())
        if get and path == "/api/events":
            return self.send_json(200, self.remote.wait_events(int(q.get("after") or 0),
                                                               min(EVENTS_WAIT_MAX, float(q.get("wait") or 0))))
        if get and path == "/api/videos":
            return self.send_json(200, host.videos(q.get("rev", "")))
        if get and path == "/api/posted":
            return self.send_json(200, host.posted(int(q.get("limit") or 60)))
        if get and path == "/api/categories":
            return self.send_json(200, host.categories())
        if get and path == "/api/requests":
            return self.send_json(200, host.requests())
        if get and path == "/api/week":
            return self.send_json(200, host.week())
        if get and path == "/api/log":
            return self.send_json(200, host.log_tail(int(q.get("lines") or 300)))
        if get and path == "/api/whoami":
            return self.send_json(200, {"id": self.device["id"], "name": self.device["name"]})
        if get and path == "/api/deliveries":
            return self.send_json(200, host.deliveries(self.device["id"], q.get("rev", "")))
        if path.startswith(("/api/ai", "/api/studio")):
            return self.lend(method, path, q)
        m = re.fullmatch(r"/api/deliveries/" + ID + r"/([a-z]+)", path)
        if m:
            did, action = m.group(1), m.group(2)
            if get and action == "file":
                return self.send_file(host.delivery_file(did))
            if get and action == "thumb":
                return self.send_file(host.delivery_thumb(did), "image/jpeg")
            if not get and action in ("got", "seen", "dismiss"):
                self.body()
                return self.send_json(200, host.delivery_mark(did, self.device["id"],
                                                              "dismissed" if action == "dismiss" else action))
            raise HostError(404, "No such request.")
        m = re.fullmatch(r"/api/videos/" + ID + r"(?:/([a-z\-]+))?", path)
        if m:
            vid, action = m.group(1), m.group(2) or ""
            if get and not action:
                return self.send_json(200, host.video(vid))
            if get and action == "thumb":
                return self.send_file(host.thumb_file(vid), "image/jpeg")
            if get and action == "file":
                return self.send_file(host.video_file(vid))
            if not get:
                body = self.body()
                if action == "ok":
                    return self.send_json(200, host.ok(vid))
                if action == "post-now":
                    return self.send_json(200, host.post_now(vid))
                if action == "retry":
                    return self.send_json(200, host.retry(vid))
                if action == "delete":
                    return self.send_json(200, host.delete(vid))
                if action == "edit":
                    return self.send_json(200, host.edit(vid, body.get("caption"), body.get("hashtags")))
            raise HostError(404, "No such request.")
        if get:
            raise HostError(404, "No such request.")
        body = self.body()
        if path == "/api/automation":
            return self.send_json(200, host.pause(bool(body.get("paused"))))
        m = re.fullmatch(r"/api/categories/" + ID, path)
        if m:
            return self.send_json(200, host.pause_category(m.group(1), bool(body.get("paused"))))
        if path == "/api/scan":
            return self.send_json(200, host.scan(str(body.get("category") or "")))
        if path == "/api/make":
            return self.send_json(200, host.make(str(body.get("idea") or ""), str(body.get("category") or "")))
        if path == "/api/clip":
            links = body.get("links")
            if isinstance(links, str):
                links = links.split()
            return self.send_json(200, host.clip(links if isinstance(links, list) else [], str(body.get("category") or "")))
        if path == "/api/requests":
            links = body.get("links")
            if isinstance(links, str):
                links = links.split()
            return self.send_json(200, host.new_request(body.get("count") or 5, body.get("min_len"), body.get("max_len"),
                                                        str(body.get("category") or ""),
                                                        links if isinstance(links, list) else [],
                                                        str(body.get("notes") or "")))
        m = re.fullmatch(r"/api/requests/" + ID + r"/cancel", path)
        if m:
            return self.send_json(200, host.cancel_request(m.group(1)))
        if path == "/api/unlink":
            self.remote.unlink(self.device["id"])
            return self.send_json(200, {"message": "This Mac is unlinked."})
        raise HostError(404, "No such request.")


    def lend(self, method: str, path: str, q: dict) -> None:
        """What a Mac hands this PC: questions for its local AI, and videos to make (studio.py)."""
        host, me = self.remote.host, self.device["id"]
        get = method == "GET"
        if get and path == "/api/ai":
            return self.send_json(200, host.ai_tasks.status(fresh=True))
        if method == "POST" and path == "/api/ai/tasks":
            return self.send_json(200, host.ai_tasks.start(me, self.body(AI_BODY_MAX)))
        m = re.fullmatch(r"/api/ai/tasks/" + ID + r"(?:/(cancel))?", path)
        if m:
            if get and not m.group(2):
                rev = q.get("rev", "")
                return self.send_json(200, host.ai_tasks.get(m.group(1), me, float(q.get("wait") or 0),
                                                             int(rev) if rev.isdigit() else None))
            if method == "POST" and m.group(2):
                self.body()
                return self.send_json(200, host.ai_tasks.cancel(m.group(1), me))
            raise HostError(404, "No such request.")
        if method == "PUT" and path == "/api/studio/uploads":
            size = int(self.headers.get("Content-Length") or 0)
            try:
                host.studio.check_upload(q.get("name", ""), size)
            except HostError:
                self._drain(size)  # so the Mac, still sending, hears why
                raise
            got = host.studio.receive(q.get("name", ""), size, self.rfile.read)
            self.body_read = True
            return self.send_json(200, got)
        if path == "/api/studio/jobs":
            if get:
                return self.send_json(200, host.studio.jobs_for(me))
            if method == "POST":
                return self.send_json(200, host.studio.add(me, self.device.get("name", ""), self.body()))
        m = re.fullmatch(r"/api/studio/jobs/" + ID + r"(?:/([a-z]+))?", path)
        if m:
            jid, action = m.group(1), m.group(2) or ""
            if get and not action:
                return self.send_json(200, host.studio.job(jid, me))
            if get and action == "file":
                return self.send_file(host.studio.file(jid, me, q.get("path", "")))
            if method == "POST" and action in ("cancel", "done"):
                self.body()
                return self.send_json(200, host.studio.cancel(jid, me) if action == "cancel" else host.studio.done(jid, me))
        raise HostError(404, "No such request.")


class RemoteServer:
    """Runs (and stops) the HTTPS server and the discovery answerer as Settings → Mac app says."""

    def __init__(self, runner, host: Host | None = None, state_file: Path | None = None,
                 status_file: Path | None = None, cert_dir: Path | None = None,
                 now: Callable[[], float] = time.time, bind: str = "0.0.0.0", discovery: bool = True):
        self.runner = runner
        self.host = host or Host(runner, now=now)
        self.state_file = state_file
        self.status_file = status_file
        self.cert_dir = cert_dir
        self.now = now
        self.bind = bind
        self.discovery = discovery
        self.port = 0
        self.fp = ""
        self.error = ""
        self.httpd: _Server | None = None
        self._serve: threading.Thread | None = None
        self._udp: socket.socket | None = None
        self._udp_thread: threading.Thread | None = None
        self._loop: threading.Thread | None = None
        self.stop_event = threading.Event()
        self._fails: dict[str, list[float]] = {}
        self._fail_lock = threading.Lock()
        self._seen_at: dict[str, float] = {}
        self._pair_lock = threading.Lock()
        self._key_cache: tuple[str, str, bytes] | None = None  # (code, salt, stretched key)
        self.host_id = ""
        self.name = ""

    # --- running ------------------------------------------------------------------------------------------

    def log(self, msg: str) -> None:
        try:
            self.runner.log(f"Mac app: {msg}")
        except Exception:  # noqa: BLE001
            pass

    def settings(self):
        return self.runner.load()

    def start(self) -> None:
        self.stop_event.clear()
        self._loop = threading.Thread(target=self._run, name="remote", daemon=True)
        self._loop.start()
        self.host.studio.start()  # videos a Mac sent: it makes them whether or not a Mac is connected now

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.host.ai_tasks.stop_all()
        self.host.studio.stop(timeout)
        if self._loop is not None:
            self._loop.join(timeout)
        self._close()
        self._write_status()

    @property
    def listening(self) -> bool:
        return self.httpd is not None

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.configure()
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
            self._write_status()
            self.stop_event.wait(HEARTBEAT)

    def configure(self) -> bool:
        """Listen when Settings → Mac app is on (on its port), stop when it's off. True when listening."""
        s = self.settings()
        on = bool(getattr(s, "remote_on", False))
        port = int(getattr(s, "remote_port", protocol.PORT) or protocol.PORT)
        if not on:
            if self.httpd is not None:
                self._close()
                self.log("Off (Settings → Mac app).")
            self.error = ""
            return False
        if self.httpd is not None and port == self.port:
            return True
        self._close()
        return self.open(port)

    def open(self, port: int) -> bool:
        with state.edit(self.state_file) as data:
            self.host_id = state.host_id(data)
        self.name = socket.gethostname() or "ClipMonster"
        key, cert = certs.ensure(self.cert_dir, self.name)
        self.fp = certs.fingerprint_of(cert)
        ctx = certs.server_context(key, cert)
        try:
            httpd = _Server((self.bind, port), Handler, ctx, self)
        except OSError as exc:
            said = (f"Port {port} is in use by another program: pick another in Settings → Mac app."
                    if getattr(exc, "errno", 0) in (98, 48, 10048, 10013) else f"Couldn't listen on port {port}: {exc}")
            if said != self.error:
                self.log(f"⚠ {said}")
            self.error = said
            return False
        self.httpd, self.port, self.error = httpd, httpd.server_address[1], ""
        self._serve = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                       name="remote-https", daemon=True)
        self._serve.start()
        if self.discovery:
            self._open_discovery()
        addrs = local_addresses()
        self.log(f"Listening on port {self.port}" + (f" ({', '.join(addrs[:3])})" if addrs else "") + ".")
        return True

    def _close(self) -> None:
        httpd, self.httpd = self.httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
                httpd.close_connections()
            except OSError:
                pass
        udp, self._udp = self._udp, None
        if udp is not None:
            try:
                udp.close()
            except OSError:
                pass
        self.port = 0

    def _open_discovery(self) -> None:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind((self.bind, protocol.DISCOVERY_PORT))
            udp.settimeout(1.0)
            protocol.udp_no_resets(udp)
        except OSError as exc:
            self.log(f"Macs can't find this PC by themselves (UDP {protocol.DISCOVERY_PORT}: {exc}); type its "
                     "address in the Mac app instead.")
            return
        self._udp = udp
        self._udp_thread = threading.Thread(target=self._answer_discovery, args=(udp,), name="remote-discovery",
                                            daemon=True)
        self._udp_thread.start()

    def _answer_discovery(self, udp: socket.socket) -> None:
        last: dict[str, float] = {}
        while self._udp is udp and not self.stop_event.is_set():
            try:
                data, addr = udp.recvfrom(512)
            except (socket.timeout, ConnectionResetError):  # (Windows: an earlier answer's Mac had gone)
                continue
            except OSError:
                return
            if not data.startswith(protocol.DISCOVER_HELLO):
                continue
            now = time.monotonic()
            if now - last.get(addr[0], 0) < 0.2:  # one answer per moment per Mac
                continue
            last[addr[0]] = now
            nonce = data[len(protocol.DISCOVER_HELLO):][:64].decode("ascii", "replace")
            try:
                pairing = bool(state.pairing(state.load(self.state_file), self.now()))
            except Exception:  # noqa: BLE001 - an unreadable remote.json still gets an answer
                pairing = False
            reply = {"app": protocol.APP, "api": protocol.API_VERSION, "name": self.name, "id": self.host_id,
                     "port": self.port, "fp": self.fp, "nonce": nonce, "version": __version__, "pairing": pairing}
            try:
                udp.sendto(json.dumps(reply).encode(), addr)
            except OSError:
                pass

    def _write_status(self) -> None:
        devices = []
        try:
            devices = state.load(self.state_file)["devices"]
        except Exception:  # noqa: BLE001
            pass
        state.write_status({"at": self.now(), "pid": os.getpid(), "listening": self.listening, "port": self.port,
                            "fingerprint": self.fp, "error": self.error, "addresses": mac_addresses()
                            if self.listening else [], "devices": len(devices)}, self.status_file)

    # --- keys and pairing ---------------------------------------------------------------------------------

    def hello(self) -> dict:
        data = state.load(self.state_file)
        p = state.pairing(data, self.now())
        return {**self.host.hello(), "id": data.get("host_id") or self.host_id,
                "pairing": {"open": bool(p), "salt": p.get("salt", ""),
                            "iterations": int(p.get("iterations") or protocol.ITERATIONS),
                            "until": p.get("until", 0)}}

    def _too_many(self, ip: str) -> bool:
        now = time.monotonic()
        with self._fail_lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < 60]
            self._fails[ip] = recent
            return len(recent) >= FAILS_PER_MINUTE

    def _failed(self, ip: str) -> None:
        with self._fail_lock:
            self._fails.setdefault(ip, []).append(time.monotonic())

    def authorize(self, header: str, ip: str) -> dict:
        if self._too_many(ip):
            raise HostError(429, "Too many wrong keys: wait a minute.")
        device_id, token = protocol.parse_auth(header)
        if not device_id:
            self._failed(ip)
            raise HostError(401, "This Mac isn't linked to the PC. Link it with a code from Settings → Mac app.")
        d = state.find_device(state.load(self.state_file), device_id, token)
        if d is None:
            self._failed(ip)
            raise HostError(401, "This Mac isn't linked to the PC any more. Link it again with a code from "
                                 "Settings → Mac app.")
        now = self.now()
        if now - self._seen_at.get(d["id"], 0) > state.SEEN_EVERY or d.get("address") != ip:
            self._seen_at[d["id"]] = now
            try:
                with state.edit(self.state_file) as data:
                    for x in data["devices"]:
                        if x.get("id") == d["id"]:
                            x["seen_at"], x["address"] = now, ip
            except (OSError, TimeoutError):
                pass
        return d

    def pair(self, body: dict, ip: str) -> dict:
        name = " ".join(str(body.get("name") or "Mac").split())[:60] or "Mac"
        nonce = str(body.get("nonce") or "")
        proof = str(body.get("proof") or "")
        if not re.fullmatch(r"[0-9a-f]{32,128}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", proof):
            raise HostError(400, "That isn't a pairing request.")
        if self._too_many(ip):
            raise HostError(429, "Too many tries: wait a minute.")
        with self._pair_lock:  # one try at a time, so five wrong ones really are five
            now = self.now()
            with state.edit(self.state_file) as data:
                p = state.pairing(data, now)
                if not p:
                    raise HostError(403, "No code is up on the PC. Press Link a Mac in Settings → Mac app, then type "
                                         "the code it shows.")
                code, salt = p["code"], p["salt"]
                rounds = int(p.get("iterations") or protocol.ITERATIONS)
            key = self._stretched(code, salt, rounds)
            if not protocol.same(protocol.mac_proof(key, self.fp, nonce, name), proof):
                with state.edit(self.state_file) as data:
                    left = 0
                    if (data.get("pairing") or {}).get("code") == code:
                        data["pairing"]["wrong"] = int(data["pairing"].get("wrong") or 0) + 1
                        left = protocol.MAX_WRONG - data["pairing"]["wrong"]
                        if left <= 0:
                            data["pairing"] = {}
                self._failed(ip)
                self.log(f"⚠ A wrong code from {ip}" + ("; the code is thrown away." if left <= 0 else "."))
                if left <= 0:
                    raise HostError(403, "Too many wrong codes, so that code stopped working. Press Link a Mac on the "
                                         "PC for a new one.")
                raise HostError(403, "That code isn't right (or this isn't the PC showing it). Check it and try again.")
            with state.edit(self.state_file) as data:
                entry, token = state.new_device(data, name, now, ip)
                state.stop_pairing(data)
            self._key_cache = None
            self.log(f"Linked {name} ({ip}).")
            self.host.add_event("linked", f"{name} is linked to this PC.")
            return {"device_id": entry["id"], "token": token, "host_id": self.host_id, "name": self.name,
                    "proof": protocol.host_proof(key, self.fp, nonce, entry["id"], token)}

    def _stretched(self, code: str, salt: str, rounds: int) -> bytes:
        c = self._key_cache
        if c and c[0] == code and c[1] == salt:
            return c[2]
        key = protocol.stretch(code, bytes.fromhex(salt), rounds)
        self._key_cache = (code, salt, key)
        return key

    def unlink(self, device_id: str) -> None:
        with state.edit(self.state_file) as data:
            state.unlink(data, device_id)
        self.log("A Mac unlinked itself.")

    def wait_events(self, after: int, wait: float) -> dict:
        until = time.monotonic() + max(0.0, wait)
        while True:
            got = self.host.events_after(after)
            if got["events"] or time.monotonic() >= until or self.stop_event.is_set() or after > got["seq"]:
                return got
            time.sleep(0.4)
