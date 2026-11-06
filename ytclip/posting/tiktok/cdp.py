"""A tiny DevTools (CDP) client for the plain login window, using only the standard library.

It talks to the browser itself, never to a page: nothing is attached to TikTok's tab and no
script runs in it, so the login page sees an ordinary browser. The login window uses it for
two things only: reading the cookie jar to notice that you've logged in, and closing the window.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
from pathlib import Path

PORT_FILE = "DevToolsActivePort"  # Chromium writes "<port>\n/devtools/browser/<id>" here


class CDPError(Exception):
    pass


def endpoint(profile_dir: Path) -> tuple[int, str] | None:
    """(port, path) of the browser's DevTools socket, once it has started; else None."""
    try:
        lines = (profile_dir / PORT_FILE).read_text(encoding="utf-8").split()
        port, path = int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None
    return (port, path) if 0 < port < 65536 and path.startswith("/devtools/browser/") else None


class CDP:
    """One WebSocket connection to the browser's DevTools endpoint."""

    def __init__(self, port: int, path: str, timeout: float = 5):
        self._id = 0
        self._buf = b""
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
            head = self._read_until(b"\r\n\r\n")
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise CDPError("DevTools refused the connection")
        except BaseException:
            self.sock.close()
            raise

    def __enter__(self) -> "CDP":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def call(self, method: str, params: dict | None = None) -> dict:
        """Send one command and return its result (events that arrive meanwhile are skipped)."""
        self._id += 1
        msg_id = self._id
        self._send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError(str(msg["error"].get("message") or msg["error"]))
                return msg.get("result") or {}

    def cookies(self) -> list[dict]:
        return self.call("Storage.getCookies").get("cookies") or []

    # --- WebSocket framing (RFC 6455): client frames are masked, server frames aren't ---

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("DevTools closed the connection")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("DevTools closed the connection")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise CDPError("DevTools sent an unexpected reply")
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _send(self, text: str, opcode: int = 1) -> None:
        payload = text.encode() if isinstance(text, str) else text
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv(self) -> str:
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                raise CDPError("DevTools closed the connection")
            if opcode == 9:  # ping
                self._send(data, opcode=10)
                continue
            if opcode in (0, 1, 2):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")
