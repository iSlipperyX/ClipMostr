"""Google Drive, for uploading finished clips (uploads.py): signing in once, and resumable uploads.

Plain HTTPS (no extra package), the way Google documents it for desktop apps:

* Signing in uses your own OAuth client (Settings → Upload: a "Desktop app" client from a Google Cloud
  project, free; docs/google-drive-setup.md says how). The browser opens Google's sign-in page, which
  hands a code back to a one-off server on 127.0.0.1, with PKCE. Only the drive.file scope is asked
  for: ClipMonster can see and change the files and folders it made itself, nothing else in your Drive.
* The refresh token is kept in %APPDATA%\\ClipMonster\\google-drive.json, encrypted for your Windows
  account (DPAPI), so it's no use copied to another PC or account.
* Clips go up in 8 MB pieces with a resumable upload session, so a dropped connection, or ClipMonster
  closing halfway, carries on where it stopped instead of starting again.

CLIPMONSTER_GOOGLE_BASE points every address at a fake Google, for the tests.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

from . import __version__

SCOPES = "https://www.googleapis.com/auth/drive.file openid email"
FOLDER_TYPE = "application/vnd.google-apps.folder"
CHUNK = 8 * 1024 * 1024  # a multiple of 256 KiB, as resumable uploads need
RETRY_WAITS = (2.0, 4.0, 8.0, 16.0)  # rate limits, busy servers and dropped connections are tried again
SIGN_IN_TIMEOUT = 300.0
SETUP_HELP = "https://github.com/iSlipperyX/ClipMostr/blob/main/docs/google-drive-setup.md"
CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"


def _base(name: str) -> str:
    fake = os.environ.get("CLIPMONSTER_GOOGLE_BASE", "").rstrip("/")
    if fake:
        return fake + {"auth": "/o/oauth2/v2/auth", "token": "/token", "revoke": "/revoke", "userinfo": "/userinfo",
                       "api": "/drive/v3", "upload": "/upload/drive/v3/files"}[name]
    return {"auth": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
            "revoke": "https://oauth2.googleapis.com/revoke",
            "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
            "api": "https://www.googleapis.com/drive/v3",
            "upload": "https://www.googleapis.com/upload/drive/v3/files"}[name]


class DriveError(RuntimeError):
    """A plain-English reason a Drive call failed. kind: "" (try again later) | "sign_in" (sign in
    again) | "full" (your Drive is full) | "setup" (the client ID or secret is wrong) | "gone" (the
    file or folder was deleted in Drive) | "cancelled"."""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- the saved sign-in

def token_path() -> Path:
    from .settings import app_dir

    return app_dir() / "google-drive.json"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows' per-account encryption (CryptProtectData), so the token only works for this account."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = (ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(Blob))
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):  # UI_FORBIDDEN
        raise OSError(ctypes.get_last_error(), "Windows couldn't decrypt the saved Google sign-in")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _seal(text: str) -> str:
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    return "plain:" + text  # elsewhere only for development: the file is the user's own


def _unseal(text: str) -> str:
    if text.startswith("dpapi:"):
        return _dpapi(base64.b64decode(text[6:]), False).decode("utf-8")
    if text.startswith("plain:"):
        return text[6:]
    return ""


@dataclass
class Account:
    email: str
    client_id: str
    refresh_token: str


def load_account(client_id: str = "") -> Account | None:
    """The saved sign-in, when there is one for this client ID."""
    try:
        data = json.loads(token_path().read_text(encoding="utf-8"))
        token = _unseal(str(data.get("refresh_token") or ""))
    except (OSError, ValueError, TypeError):
        return None
    if not token or (client_id and data.get("client_id") != client_id.strip()):
        return None
    return Account(str(data.get("email") or ""), str(data.get("client_id") or ""), token)


def save_account(account: Account) -> None:
    path = token_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"email": account.email, "client_id": account.client_id,
                               "refresh_token": _seal(account.refresh_token), "saved": time.time()}, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def sign_out(revoke: bool = True) -> None:
    """Forget the sign-in (and, when it can, tell Google to cancel it too)."""
    account = load_account()
    if account and revoke:
        try:
            _post(_base("revoke"), {"token": account.refresh_token}, timeout=10)
        except DriveError:
            pass
    token_path().unlink(missing_ok=True)
    _tokens.clear()


# ---------------------------------------------------------------- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A resumable upload answers 308 ("resume incomplete"), which isn't a redirect to follow."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _request(method: str, url: str, *, token: str = "", body: bytes | None = None,
             headers: dict | None = None, timeout: float = 60) -> tuple[int, dict, bytes]:
    """(status, headers, body); HTTP errors are answers too, network trouble is a DriveError."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and parts.hostname not in ("127.0.0.1", "localhost"):
        raise DriveError(f"Refusing to talk to Google over an insecure address: {url}")
    req = urllib.request.Request(url, data=body, method=method, headers={
        "User-Agent": f"ClipMonster/{__version__}", **({"Authorization": f"Bearer {token}"} if token else {}),
        **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except (OSError, http.client.HTTPException):
            data = b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise DriveError(f"Couldn't reach Google Drive ({getattr(exc, 'reason', exc)}). It's tried again "
                         "later.") from exc


def _json(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _reason(status: int, body: bytes) -> tuple[str, str]:
    """(Google's own reason code, its message) from an error answer."""
    data = _json(body)
    err = data.get("error")
    if isinstance(err, dict):
        reasons = [e.get("reason", "") for e in err.get("errors") or [] if isinstance(e, dict)]
        return (reasons[0] if reasons else str(err.get("status") or "")), str(err.get("message") or "")
    return str(err or ""), str(data.get("error_description") or "")


def _post(url: str, form: dict, timeout: float = 30) -> dict:
    status, _, body = _request("POST", url, body=urllib.parse.urlencode(form).encode(),
                               headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=timeout)
    if status != 200:
        code, message = _reason(status, body)
        if code in ("invalid_client", "unauthorized_client"):
            raise DriveError("Google doesn't accept the OAuth client ID and secret in Settings → Upload. Copy "
                             "them again from Google Auth Platform → Clients in your Google Cloud project (it must "
                             "be a Desktop app client).", "setup")
        if code == "invalid_grant":
            raise DriveError("Google has signed ClipMonster out of your Drive (the sign-in expired or was "
                             "removed). Sign in again in Settings → Upload.", "sign_in")
        raise DriveError(f"Google answered {status}{': ' + message if message else ''}.")
    return _json(body)


# ---------------------------------------------------------------- signing in

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class _Callback(BaseHTTPRequestHandler):
    result: dict = {}

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in query.items()}  # type: ignore[attr-defined]
        ok = "code" in query
        page = ("<h2>ClipMonster is signed in to Google Drive.</h2><p>You can close this tab.</p>" if ok else
                "<h2>Google Drive wasn't connected.</h2><p>You can close this tab and try again in ClipMonster.</p>")
        body = (f"<!doctype html><meta charset=utf-8><title>ClipMonster</title><body style='font-family:sans-serif;"
                f"background:#151221;color:#eee;padding:40px'>{page}</body>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def sign_in(client_id: str, client_secret: str, open_browser: Callable[[str], object] = webbrowser.open,
            cancelled: Callable[[], bool] | None = None, timeout: float = SIGN_IN_TIMEOUT) -> Account:
    """Open Google's sign-in page in the browser and wait for it to come back. Saves and returns
    the account."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id or not client_secret:
        raise DriveError("Enter your OAuth client ID and secret first (Settings → Upload says where to get "
                         "them).", "setup")
    verifier = _b64url(secrets.token_bytes(48))
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _Callback)
    server.result = {}  # type: ignore[attr-defined]
    server.timeout = 0.5
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = _base("auth") + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        "code_challenge_method": "S256", "state": state, "access_type": "offline", "prompt": "consent"})
    try:
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        end = time.monotonic() + timeout
        while not server.result:  # type: ignore[attr-defined]
            if cancelled and cancelled():
                raise DriveError("Signing in was cancelled.", "cancelled")
            if time.monotonic() > end:
                raise DriveError("Google's sign-in page didn't come back within 5 minutes. Try again.")
            server.handle_request()
        result = server.result  # type: ignore[attr-defined]
    finally:
        server.server_close()
    if result.get("state") != state:
        raise DriveError("The answer from Google's sign-in page didn't match. Try again.")
    if "error" in result:
        if result["error"] == "access_denied":
            raise DriveError("Access to Google Drive wasn't allowed on Google's page, so nothing was connected.")
        raise DriveError(f"Google's sign-in page said: {result['error']}.")
    tokens = _post(_base("token"), {"code": result.get("code", ""), "client_id": client_id,
                                    "client_secret": client_secret, "redirect_uri": redirect,
                                    "grant_type": "authorization_code", "code_verifier": verifier})
    refresh = tokens.get("refresh_token") or ""
    if not refresh:
        raise DriveError("Google didn't send a lasting sign-in. Remove ClipMonster's access at "
                         "myaccount.google.com/permissions and sign in again.")
    if "drive.file" not in str(tokens.get("scope") or "drive.file"):
        raise DriveError("Drive access was unticked on Google's page, so ClipMonster can't upload. Sign in "
                         "again and leave it ticked.")
    access = tokens.get("access_token") or ""
    email = ""
    if access:
        status, _, body = _request("GET", _base("userinfo"), token=access, timeout=20)
        email = str(_json(body).get("email") or "") if status == 200 else ""
        _tokens[client_id] = (access, time.time() + float(tokens.get("expires_in") or 3600) - 120)
    account = Account(email, client_id, refresh)
    save_account(account)
    return account


_tokens: dict[str, tuple[str, float]] = {}  # client ID -> (access token, good until)
_token_lock = threading.Lock()


def access_token(client_id: str, client_secret: str, force: bool = False) -> str:
    """A current access token (they last an hour), from the saved sign-in."""
    client_id = client_id.strip()
    with _token_lock:
        cached = _tokens.get(client_id)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        account = load_account(client_id)
        if account is None:
            raise DriveError("ClipMonster isn't signed in to Google Drive. Sign in in Settings → Upload.", "sign_in")
        tokens = _post(_base("token"), {"refresh_token": account.refresh_token, "client_id": client_id,
                                        "client_secret": client_secret.strip(), "grant_type": "refresh_token"})
        token = tokens.get("access_token") or ""
        if not token:
            raise DriveError("Google didn't send an access token. Try signing in again.", "sign_in")
        _tokens[client_id] = (token, time.time() + float(tokens.get("expires_in") or 3600) - 120)
        return token


# ---------------------------------------------------------------- Drive

class Drive:
    """Calls to Drive as the signed-in account, retrying what's worth retrying."""

    def __init__(self, client_id: str, client_secret: str, sleep: Callable[[float], None] = time.sleep):
        self.client_id, self.client_secret, self.sleep = client_id, client_secret, sleep

    def call(self, method: str, url: str, *, body: bytes | None = None, headers: dict | None = None,
             timeout: float = 60, ok: tuple[int, ...] = (200,)) -> tuple[int, dict, bytes]:
        waits = list(RETRY_WAITS)
        fresh = False
        while True:
            token = access_token(self.client_id, self.client_secret, force=fresh)
            try:
                status, head, data = _request(method, url, token=token, body=body, headers=headers, timeout=timeout)
            except DriveError:
                if not waits:
                    raise
                self.sleep(waits.pop(0))
                continue
            if status in ok:
                return status, head, data
            code, message = _reason(status, data)
            if status == 401 and not fresh:  # the access token ran out early: get a new one, once
                fresh = True
                continue
            if status == 401:
                raise DriveError("Google Drive stopped accepting ClipMonster's sign-in. Sign in again in "
                                 "Settings → Upload.", "sign_in")
            if code in ("storageQuotaExceeded", "quotaExceeded") or "storage quota" in message.lower():
                raise DriveError("Your Google Drive is full, so clips can't be uploaded. Free up space in Drive "
                                 "(or buy more), and uploading carries on by itself.", "full")
            if status == 404:
                raise DriveError(f"Drive says it's not there any more{': ' + message if message else ''}.", "gone")
            if status == 403 and code in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in message:
                raise DriveError("The Google Drive API isn't turned on in your Google Cloud project. Turn it on "
                                 "(APIs & Services → Library → Google Drive API → Enable) and try again.", "setup")
            if (status == 429 or status >= 500 or code in ("rateLimitExceeded", "userRateLimitExceeded")) and waits:
                self.sleep(waits.pop(0))
                continue
            raise DriveError(f"Google Drive answered {status}{': ' + message if message else ''}.")

    def get_json(self, method: str, url: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json; charset=UTF-8"} if body is not None else None
        return _json(self.call(method, url, body=body, headers=headers)[2])

    # ------------------------------------------------------------ folders
    def find_folder(self, name: str, parent: str) -> str:
        q = (f"name = '{_quote(name)}' and mimeType = '{FOLDER_TYPE}' and '{_quote(parent)}' in parents "
             "and trashed = false")
        data = self.get_json("GET", _base("api") + "/files?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id)", "spaces": "drive", "pageSize": "10"}))
        files = data.get("files") or []
        return str(files[0].get("id") or "") if files else ""

    def make_folder(self, name: str, parent: str) -> str:
        data = self.get_json("POST", _base("api") + "/files?fields=id",
                             {"name": name, "mimeType": FOLDER_TYPE, "parents": [parent],
                              "appProperties": {"clipmonster": "1"}})
        folder = str(data.get("id") or "")
        if not folder:
            raise DriveError(f"Drive didn't make the folder {name}.")
        return folder

    def exists(self, file_id: str) -> bool:
        """Whether a file or folder ClipMonster made is still there (not deleted or in the bin)."""
        try:
            data = self.get_json("GET", _base("api") + f"/files/{urllib.parse.quote(file_id)}?fields=id,trashed")
        except DriveError as exc:
            if exc.kind == "gone":
                return False
            raise
        return not data.get("trashed")

    # ------------------------------------------------------------ files
    def start_upload(self, name: str, parent: str, size: int, mime: str, description: str = "",
                     replace: str = "") -> str:
        """A resumable upload session's address; `replace` (a file id) uploads a new version of that file."""
        meta = {"name": name, "description": description[:4000]}
        if replace:
            url, method = _base("upload") + f"/{urllib.parse.quote(replace)}?uploadType=resumable", "PATCH"
        else:
            meta |= {"parents": [parent], "appProperties": {"clipmonster": "1"}}
            url, method = _base("upload") + "?uploadType=resumable", "POST"
        _, head, _ = self.call(method, url + "&fields=id,webViewLink", body=json.dumps(meta).encode("utf-8"),
                               headers={"Content-Type": "application/json; charset=UTF-8",
                                        "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        session = head.get("location", "")
        if not session:
            raise DriveError("Drive didn't start the upload. It's tried again later.")
        return session

    def uploaded_so_far(self, session: str, size: int) -> int | dict:
        """How much of a session's file Drive has (bytes), or the finished file's details."""
        status, head, data = _request("PUT", session, body=b"", headers={"Content-Range": f"bytes */{size}"},
                                      timeout=30)
        if status in (200, 201):
            return _json(data)
        if status == 308:
            got = head.get("range", "")  # "bytes=0-1234"
            return int(got.rsplit("-", 1)[1]) + 1 if got else 0
        if status in (404, 410):
            raise DriveError("The upload session expired: starting again.", "gone")
        raise DriveError(f"Google Drive answered {status} about an upload in progress.")

    def send(self, session: str, path: Path, size: int, offset: int,
             progress: Callable[[float], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
        """Send the file from `offset` in CHUNK pieces; returns the finished file's details."""
        waits = list(RETRY_WAITS)
        with open(path, "rb") as f:
            while True:
                if cancelled and cancelled():
                    raise DriveError("The upload was stopped; it carries on later.", "cancelled")
                f.seek(offset)
                piece = f.read(CHUNK)
                end = offset + len(piece) - 1
                rng = f"bytes {offset}-{end}/{size}" if piece else f"bytes */{size}"
                try:
                    status, head, data = _request("PUT", session, body=piece, headers={"Content-Range": rng},
                                                  timeout=120)
                except DriveError:
                    if not waits:
                        raise
                    self.sleep(waits.pop(0))
                    got = self.uploaded_so_far(session, size)  # the network dropped: ask where it got to
                    if isinstance(got, dict):
                        return got
                    offset = got
                    continue
                if status in (200, 201):
                    if progress:
                        progress(1.0)
                    return _json(data)
                if status == 308:
                    got = head.get("range", "")
                    offset = int(got.rsplit("-", 1)[1]) + 1 if got else 0
                    waits = list(RETRY_WAITS)
                    if progress and size:
                        progress(min(1.0, offset / size))
                    continue
                if status in (404, 410):
                    raise DriveError("The upload session expired: starting again.", "gone")
                code, message = _reason(status, data)
                if code in ("storageQuotaExceeded", "quotaExceeded"):
                    raise DriveError("Your Google Drive is full, so clips can't be uploaded. Free up space in "
                                     "Drive (or buy more), and uploading carries on by itself.", "full")
                if (status == 429 or status >= 500) and waits:
                    self.sleep(waits.pop(0))
                    got = self.uploaded_so_far(session, size)
                    if isinstance(got, dict):
                        return got
                    offset = got
                    continue
                raise DriveError(f"Google Drive answered {status}{': ' + message if message else ''}.")


def _quote(text: str) -> str:
    """A value inside a Drive search's single quotes."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


def link(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"
