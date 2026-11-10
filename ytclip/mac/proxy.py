"""Playing a video straight from the PC: a tiny web server on this Mac only (127.0.0.1) that the video player
asks for the file, passing each request on to the PC over the pinned, keyed connection (client.py).

The player can't check the PC's certificate by fingerprint or send the key itself, so it asks this server
instead. Each address carries a random secret made when the app starts, so no other program on the Mac can
use it to reach the PC. Range requests pass through, so the video starts at once and seeking works.
"""

from __future__ import annotations

import re
import secrets
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import quote

CHUNK = 128 * 1024
PASS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_Server"

    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_HEAD(self):  # noqa: N802
        self._pass(head=True)

    def do_GET(self):  # noqa: N802
        self._pass()

    def _pass(self, head: bool = False) -> None:
        m = re.fullmatch(r"/([0-9a-f]{32})/([vd])/([A-Za-z0-9_\-~.]{1,80})/(file|thumb)", self.path.split("?", 1)[0])
        if not m or not secrets.compare_digest(m.group(1), self.server.secret):
            self.send_error(404)
            return
        client = self.server.client()
        if client is None:
            self.send_error(503, "Not connected to the PC")
            return
        headers = {}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            where = client.video_path if m.group(2) == "v" else client.delivery_path  # a video, or a sent clip
            resp = client.raw(where(m.group(3), m.group(4)), headers, timeout=60.0)
        except Exception:  # noqa: BLE001 - the player shows it can't play
            self.send_error(502, "The PC couldn't be reached")
            return
        try:
            self.send_response(resp.status)
            for h in PASS:
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if head:
                return
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError:  # the player stopped reading (seeked, or closed): fine
            self.close_connection = True
        finally:
            one_off = getattr(resp, "_one_off", None)
            if one_off is not None:
                one_off.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, client: Callable[[], object]):
        self.client = client
        self.secret = secrets.token_hex(16)
        super().__init__(("127.0.0.1", 0), _Handler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # (without HTTPServer's name lookup, which nothing here uses)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]

    def handle_error(self, request, client_address):
        pass


class MediaProxy:
    def __init__(self, client: Callable[[], object]):
        self._server = _Server(client)
        self._thread = threading.Thread(target=self._server.serve_forever, name="media-proxy", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def url(self, item_id: str, what: str = "file", kind: str = "v") -> str:
        """kind: v for a video in posting's library, d for a clip sent to the Macs (the inbox)."""
        return f"http://127.0.0.1:{self.port}/{self._server.secret}/{kind}/{quote(item_id, safe='')}/{what}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
