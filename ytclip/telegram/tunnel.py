"""A public address for the Mini App: a Cloudflare quick tunnel run by ClipMonster.

Telegram opens a Mini App only at an https:// address a phone can reach, and a PC at home has none. cloudflared
(Cloudflare's own free program) gives it one: it connects out from this PC to Cloudflare and gets an address
like https://four-random-words.trycloudflare.com, and what arrives there comes down that connection to the
Mini App's server on 127.0.0.1. Nothing is opened in the router or the firewall, and no account is needed.

What to know about it:
* the address is new each time ClipMonster starts, so the bot points each chat's ClipMonster button (left of
  the message box) at it again every time; a button in an old message stops working after a restart;
* Cloudflare gives quick tunnels no promise of staying up: if one drops, this starts another (and the address
  changes). For an address that stays, set your own in Settings → Telegram → Mini App address (a named
  Cloudflare tunnel, Tailscale Funnel, or a reverse proxy, pointed at this PC's port);
* the videos you watch in the Mini App travel through Cloudflare to your phone, encrypted on the way;
* the address by itself opens nothing: the Mini App's server only answers the people linked to the bot, as
  signed by Telegram (webapp.verify_init_data).

cloudflared is looked for beside ClipMonster's own files and on the PATH. When it isn't there it is downloaded
once (some tens of MB) from Cloudflare's releases on GitHub, unless Settings → Telegram → Mini App address is
your own.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Callable

RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
ASSETS = {("win32", "amd64"): "cloudflared-windows-amd64.exe", ("win32", "386"): "cloudflared-windows-386.exe",
          ("linux", "amd64"): "cloudflared-linux-amd64", ("linux", "arm64"): "cloudflared-linux-arm64",
          ("darwin", "amd64"): "cloudflared-darwin-amd64.tgz", ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz"}
# A quick tunnel's address is a few words with hyphens between (never www. or api., which its log also names)
ADDRESS = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)+\.trycloudflare\.com\b")
UP = re.compile(r"Registered tunnel connection|Connection [0-9a-f-]+ registered", re.I)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
UP_WAIT = 20.0  # an address is used this long after it's given even if "registered" was never read
START_LIMIT = 90.0  # no address after this long: start over
MIN_BYTES = 5_000_000


class TunnelError(RuntimeError):
    """A plain reason there's no tunnel."""


def machine() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86", "i386", "i686"):
        return "386"
    return m


def tools_dir() -> Path:
    from ..settings import local_dir

    return local_dir("tools")


def exe_name() -> str:
    return "cloudflared.exe" if sys.platform == "win32" else "cloudflared"


def find(folder: Path | None = None) -> Path | None:
    """cloudflared, if it's already on this PC: ClipMonster's own copy first, then one on the PATH."""
    own = (folder or tools_dir()) / exe_name()
    if own.is_file() and own.stat().st_size >= MIN_BYTES:
        return own
    found = shutil.which("cloudflared")
    return Path(found) if found else None


def download(folder: Path | None = None, fetch=None, log: Callable[[str], None] | None = None) -> Path:
    """Fetch cloudflared from Cloudflare's releases into ClipMonster's own tools folder. Raises TunnelError."""
    from ..creator import downloads

    asset = ASSETS.get((sys.platform, machine()))
    if not asset:
        raise TunnelError(f"cloudflared can't be downloaded automatically for this computer ({sys.platform} "
                          f"{machine()}). Install it yourself, or set your own Mini App address.")
    folder = folder or tools_dir()
    dest = folder / exe_name()
    if log:
        log("Downloading cloudflared from Cloudflare's releases on GitHub (once), to give the Mini App an "
            "address a phone can reach…")
    try:
        if asset.endswith(".tgz"):
            archive = folder / asset
            (fetch or downloads.fetch)(RELEASES + asset, archive, "Downloading cloudflared", None,
                                       min_bytes=MIN_BYTES)
            try:
                with tarfile.open(archive) as tf:
                    member = next(m for m in tf.getmembers() if m.name.rsplit("/", 1)[-1] == "cloudflared")
                    dest.write_bytes(tf.extractfile(member).read())  # type: ignore[union-attr]
            finally:
                archive.unlink(missing_ok=True)
        else:
            (fetch or downloads.fetch)(RELEASES + asset, dest, "Downloading cloudflared", None, min_bytes=MIN_BYTES)
        if sys.platform != "win32":
            dest.chmod(0o755)
    except (OSError, StopIteration, tarfile.TarError, AttributeError) as exc:
        dest.unlink(missing_ok=True)
        raise TunnelError(f"cloudflared couldn't be downloaded ({exc}). Check the internet connection, or set your "
                          "own Mini App address in Settings → Telegram.") from exc
    return dest


class Tunnel:
    """Keeps one quick tunnel to http://127.0.0.1:<port> going. `url` is its public address once it's up ("" while
    it isn't), `error` why there's none. `popen`, `finder` and `fetcher` stand in for the real ones in tests."""

    def __init__(self, port: int, log: Callable[[str], None] | None = None, *, popen=subprocess.Popen,
                 finder: Callable[[], Path | None] = find, fetcher: Callable[..., Path] = download,
                 now: Callable[[], float] = time.time, allow_download: bool = True):
        self.port = int(port)
        self.log = log or (lambda m: None)
        self.popen, self.finder, self.fetcher, self.now = popen, finder, fetcher, now
        self.allow_download = allow_download
        self.url = ""
        self.error = ""
        self.started_at = now()
        self.proc = None
        self._found, self._found_at = "", 0.0
        self.stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._kill()
        if self._thread is not None:
            self._thread.join(5.0)
        self.url = ""

    def _kill(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass

    def _exe(self) -> Path:
        exe = self.finder()
        if exe is None:
            if not self.allow_download:
                raise TunnelError("cloudflared isn't on this PC and downloads are off. Install it, or set your own "
                                  "Mini App address in Settings → Telegram.")
            exe = self.fetcher(log=self.log)
        return exe

    def _run(self) -> None:
        wait = 5.0
        while not self.stop_event.is_set():
            began = self.now()
            try:
                self.run_once(self._exe())
            except TunnelError as exc:
                self._problem(str(exc))
                wait = max(wait, 60.0)  # a missing program or no internet: not every few seconds
            except OSError as exc:
                self._problem(f"cloudflared couldn't be started ({exc}).")
                wait = max(wait, 60.0)
            self.url = ""
            if self.stop_event.is_set():
                break
            if self.now() - began > 600:
                wait = 5.0  # it had been up a good while: straight back
            self.stop_event.wait(wait)
            wait = min(300.0, wait * 2)

    def _problem(self, text: str) -> None:
        if text != self.error:
            self.log(f"⚠ The Mini App has no public address: {text}")
        self.error = text

    def run_once(self, exe: Path) -> None:
        """Run cloudflared until it ends, publishing its address once the tunnel is up."""
        self.proc = self.popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        began = self.now()
        self._found, self._found_at = "", 0.0
        last = ""
        watchdog = threading.Thread(target=self._watch_start, args=(self.proc, began), daemon=True)
        watchdog.start()
        try:
            for line in self.proc.stdout:  # cloudflared writes its log here, a line at a time
                line = line.strip()
                if not line:
                    continue
                last = line
                if self.stop_event.is_set():
                    break
                m = ADDRESS.search(line)
                if m and not self._found:
                    self._found, self._found_at = m.group(0), self.now()
                if self._found and not self.url and UP.search(line):
                    self.url, self.error = self._found, ""
                if "failed to request quick Tunnel" in line or "Too Many Requests" in line:
                    self._problem("Cloudflare isn't giving out a quick tunnel right now (it limits how many are "
                                  "made). It's tried again in a few minutes.")
        finally:
            self._kill()
            self.proc = None
        if not self.stop_event.is_set() and not self._found and not self.error:
            self._problem("cloudflared stopped without giving an address" + (f" ({last[-160:]})." if last else "."))

    def _watch_start(self, proc, began: float) -> None:
        """A tunnel with an address but no "registered" line still counts after a while; one with no address at
        all after START_LIMIT is started over."""
        while proc.poll() is None and not self.stop_event.wait(1.0):
            if self.url:
                return
            if self._found and self.now() - self._found_at > UP_WAIT:
                self.url, self.error = self._found, ""
                return
            if not self._found and self.now() - began > START_LIMIT:
                self._problem("cloudflared didn't get an address in time. Trying again.")
                try:
                    proc.terminate()
                except OSError:
                    pass
                return
