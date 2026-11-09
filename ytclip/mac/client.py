"""Talking to the PC: HTTPS with the PC's own certificate, checked against the fingerprint pinned when the
Mac was linked (remote/protocol.py), and the Mac's key on every request.

Python's own TLS checks a certificate against certificate authorities and a host name, neither of which a PC
on a home network has, so that check is off and the certificate is compared byte for byte (its SHA-256)
instead, before anything is sent. A PC showing any other certificate gets nothing, not even the key.
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import secrets
import socket
import ssl
import sys
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

from ..remote import protocol

TIMEOUT = 15.0


class ApiError(Exception):
    """The PC answered no: `status` is its HTTP status, `message` what it said."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Offline(ApiError):
    """The PC couldn't be reached at all."""

    def __init__(self, message: str):
        super().__init__(0, message)


class WrongPC(ApiError):
    """The PC at that address showed a different certificate than the one pinned."""

    def __init__(self, message: str):
        super().__init__(495, message)


def split_address(text: str, default_port: int = protocol.PORT) -> tuple[str, int]:
    """'192.168.1.20', '192.168.1.20:47321', 'my-pc.tail1234.ts.net', '[fe80::1]:47320' → (host, port)."""
    t = str(text or "").strip()
    for prefix in ("https://", "http://"):
        if t.lower().startswith(prefix):
            t = t[len(prefix):]
    t = t.split("/", 1)[0]
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif t.count(":") == 1:
        host, port = t.split(":")
    else:
        host, port = t, ""
    try:
        p = int(port) if port else default_port
    except ValueError:
        p = default_port
    return host.strip(), p if 0 < p < 65536 else default_port


def device_name() -> str:
    """'Justyn's MacBook Pro': what the PC lists this Mac as."""
    name = ""
    if sys.platform == "darwin":
        try:
            import subprocess

            name = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except (OSError, ValueError):
            name = ""
    return name or platform.node().split(".")[0] or "Mac"


def _context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # checked by fingerprint instead (Client._connect)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Client:
    """One PC. Thread-safe: each thread keeps its own connection open between requests."""

    def __init__(self, address: str, port: int = protocol.PORT, fp: str = "", device_id: str = "", token: str = "",
                 timeout: float = TIMEOUT):
        self.address, self.port = address, int(port)
        self.fp = (fp or "").lower()
        self.device_id, self.token = device_id, token
        self.timeout = timeout
        self.seen_fp = ""  # the fingerprint of the last certificate seen
        self._local = threading.local()
        self._ctx = _context()

    # --- connections -------------------------------------------------------------------------------------

    def _connect(self, timeout: float | None = None) -> http.client.HTTPSConnection:
        conn = http.client.HTTPSConnection(self.address, self.port, timeout=timeout or self.timeout, context=self._ctx)
        try:
            conn.connect()
        except ssl.SSLError as exc:
            conn.close()
            raise Offline(f"The connection to {self.address} couldn't be secured ({exc.reason or exc}).") from exc
        except (OSError, socket.timeout) as exc:
            conn.close()
            raise Offline(self._why(exc)) from exc
        der = conn.sock.getpeercert(binary_form=True) or b""
        self.seen_fp = protocol.fingerprint(der)
        if self.fp and not protocol.same(self.seen_fp, self.fp):
            conn.close()
            raise WrongPC(f"The computer at {self.address} isn't the PC this Mac was linked to (its ID changed). If "
                          "you reinstalled ClipMonster or reset its settings, link this Mac again.")
        return conn

    def _why(self, exc: Exception) -> str:
        where = f"{self.address}:{self.port}"
        if isinstance(exc, socket.gaierror):
            return f"“{self.address}” couldn't be found on the network."
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return f"The PC at {where} didn't answer in time. Is it on, with automation running?"
        if isinstance(exc, ConnectionRefusedError):
            return (f"Nothing is answering at {where}. On the PC: Settings → Mac app → tick Let my Mac run "
                    "ClipMonster, Save, and keep automation running.")
        return f"The PC at {where} couldn't be reached ({exc})."

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _headers(self, extra: dict | None = None, auth: bool = True) -> dict:
        h = {"User-Agent": "ClipMonsterRemote", "Accept": "application/json"}
        if auth and self.device_id and self.token:
            h["Authorization"] = protocol.auth_header(self.device_id, self.token)
        h.update(extra or {})
        return h

    def _send(self, method: str, path: str, body: bytes | None, headers: dict, timeout: float | None):
        """Send on this thread's connection (a new one when it's gone stale); the response, unread."""
        for attempt in (0, 1):
            conn = getattr(self._local, "conn", None)
            fresh = conn is None or timeout is not None
            if fresh:
                conn = self._connect(timeout)
                if timeout is None:
                    self._local.conn = conn
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                if timeout is not None:
                    resp._one_off = conn  # closed with the response
                return resp
            except (http.client.HTTPException, OSError) as exc:
                conn.close()
                self._local.conn = None
                if fresh or attempt:  # a new connection failing is real; an old one may just have timed out
                    raise Offline(self._why(exc)) from exc
        raise Offline(f"The PC at {self.address}:{self.port} couldn't be reached.")

    # --- requests ------------------------------------------------------------------------------------------

    def request(self, method: str, path: str, data: dict | None = None, query: dict | None = None,
                auth: bool = True, timeout: float | None = None) -> dict:
        if query:
            path += "?" + urlencode({k: v for k, v in query.items() if v is not None})
        body = json.dumps(data).encode() if data is not None else (b"" if method == "POST" else None)
        headers = self._headers({"Content-Type": "application/json"} if body is not None else None, auth)
        resp = self._send(method, path, body, headers, timeout)
        try:
            raw = resp.read()
        except (http.client.HTTPException, OSError) as exc:
            self.close()
            raise Offline(self._why(exc)) from exc
        finally:
            one_off = getattr(resp, "_one_off", None)
            if one_off is not None:
                one_off.close()
        try:
            out = json.loads(raw) if raw else {}
        except ValueError:
            out = {"error": raw[:200].decode("utf-8", "replace")}
        if resp.status >= 400:
            raise ApiError(resp.status, str((out or {}).get("error") or f"The PC said {resp.status}."))
        return out if isinstance(out, dict) else {"value": out}

    def get(self, path: str, **query) -> dict:
        return self.request("GET", path, query=query or None)

    def post(self, path: str, data: dict | None = None, timeout: float | None = None) -> dict:
        return self.request("POST", path, data or {}, timeout=timeout)

    def raw(self, path: str, headers: dict | None = None, timeout: float | None = 60.0):
        """A response to read yourself (a video, a picture): its own connection, closed with it."""
        resp = self._send("GET", path, None, self._headers(headers), timeout)
        return resp

    def fetch(self, path: str, limit: int = 8 * 1024 * 1024) -> bytes:
        resp = self.raw(path)
        try:
            if resp.status >= 400:
                try:
                    msg = json.loads(resp.read(4096) or b"{}").get("error")
                except ValueError:
                    msg = ""
                raise ApiError(resp.status, msg or f"The PC said {resp.status}.")
            return resp.read(limit)
        finally:
            getattr(resp, "_one_off", None) and resp._one_off.close()

    def download(self, path: str, dest: Path, progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Path:
        resp = self.raw(path, timeout=120.0)
        try:
            if resp.status >= 400:
                raise ApiError(resp.status, f"The PC said {resp.status}.")
            total = int(resp.getheader("Content-Length") or 0)
            got = 0
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                while True:
                    if cancelled and cancelled():
                        raise ApiError(499, "Stopped.")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if progress:
                        progress(got, total)
            os.replace(tmp, dest)
            return dest
        finally:
            getattr(resp, "_one_off", None) and resp._one_off.close()

    # --- linking -------------------------------------------------------------------------------------------

    def hello(self) -> dict:
        """The PC's name and pairing state (no key needed). Sets seen_fp."""
        return self.request("GET", "/api/hello", auth=False, timeout=8.0)

    def pair(self, code: str, name: str | None = None) -> dict:
        """Link this Mac with the code the PC shows. Pins the certificate the code was checked against and
        returns {device_id, token, host_id, name, fp}. ApiError (403) for a wrong code."""
        if not protocol.looks_like_code(code):
            raise ApiError(400, f"The code is {protocol.CODE_LENGTH} letters and numbers, like ABCD-EFGH.")
        self.fp = ""
        info = self.hello()
        fp = self.seen_fp
        p = info.get("pairing") or {}
        if info.get("app") != protocol.APP:
            raise ApiError(400, "That isn't ClipMonster.")
        if int(info.get("api") or 0) != protocol.API_VERSION:
            raise ApiError(400, f"The PC runs ClipMonster {info.get('version', '?')}, which this app can't talk to: "
                                "update the older of the two.")
        if not p.get("open"):
            raise ApiError(403, "No code is up on the PC. Press Link a Mac in ClipMonster → Settings → Mac app.")
        try:
            salt = bytes.fromhex(str(p.get("salt") or ""))
        except ValueError as exc:
            raise ApiError(400, "The PC sent a broken pairing request.") from exc
        iterations = int(p.get("iterations") or protocol.ITERATIONS)
        if iterations < protocol.MIN_ITERATIONS:  # a PC asking for less isn't one of ours
            raise ApiError(400, "The PC asked for a weak pairing; not linked.")
        key = protocol.stretch(code, salt, iterations)
        nonce = secrets.token_hex(16)
        name = " ".join(str(name or device_name()).split())[:60] or "Mac"
        self.fp = fp  # the rest of pairing must be with the same certificate
        out = self.request("POST", "/api/pair", {"name": name, "nonce": nonce,
                                                  "proof": protocol.mac_proof(key, fp, nonce, name)},
                           auth=False, timeout=30.0)
        device_id, token = str(out.get("device_id") or ""), str(out.get("token") or "")
        if not device_id or not token or not protocol.same(
                out.get("proof"), protocol.host_proof(key, fp, nonce, device_id, token)):
            self.fp = ""
            raise ApiError(403, "The PC's answer didn't prove it knew the code, so this Mac didn't link to it.")
        self.device_id, self.token = device_id, token
        return {"device_id": device_id, "token": token, "host_id": str(out.get("host_id") or info.get("id") or ""),
                "name": str(out.get("name") or info.get("name") or self.address), "fp": fp}

    # --- shortcuts -----------------------------------------------------------------------------------------

    def video_path(self, item_id: str, what: str = "") -> str:
        return f"/api/videos/{quote(item_id, safe='')}" + (f"/{what}" if what else "")

    def delivery_path(self, item_id: str, what: str = "file") -> str:
        """A clip the PC sent to the Macs (the inbox): its file, thumb, or got/seen/dismiss."""
        return f"/api/deliveries/{quote(item_id, safe='')}/{what}"
