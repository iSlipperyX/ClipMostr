"""ClipMonster as a Linux host (packaging/linux/: a Google Cloud VM or any Ubuntu/Debian server). The installer
runs `ytclip host-setup` as the app's own user after every install or update.

The first time, it sets what a computer with no screen needs: the Mac app's server on (that's how it's run
from a Mac), posting through Google Chrome (the browser the installer puts in, shown on the host's own
virtual screen, where TikTok treats it like any other window). After that it only changes the port when the
installer is given one, so whatever was changed in Settings stays as it was.
"""

from __future__ import annotations

import argparse
import json
import time

from . import __version__
from .settings import Settings, app_dir

MARKER = "linux-host.json"


def marker_path():
    return app_dir() / MARKER


def setup(port: int | None = None, now: float | None = None) -> list[str]:
    """Settings for a host. Returns what it changed, in words."""
    from .posting import config as posting_config

    now = time.time() if now is None else now
    done: list[str] = []
    path = marker_path()
    try:
        mark = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(mark, dict):
            mark = {}
    except (OSError, ValueError):
        mark = {}
    s = Settings.load()
    first = not mark.get("set_up_at")
    changes: dict = {}
    if first:
        if not s.remote_on:
            changes["remote_on"] = True
            done.append("turned on the Mac app's server")
    if port and port != s.remote_port:
        changes["remote_port"] = port
        done.append(f"set the Mac app's port to {port}")
    if changes:
        s.copy(**changes).save()
    if first:
        ps = posting_config.Settings.load()
        if ps.browser_channel != "chrome" or not ps.show_browser:
            ps.browser_channel, ps.show_browser = "chrome", True
            ps.save()
            done.append("set posting to Google Chrome on the host's own screen")
    mark = {**mark, "set_up_at": mark.get("set_up_at") or now, "version": __version__, "updated_at": now}
    path.write_text(json.dumps(mark, indent=2), encoding="utf-8")
    return done


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ytclip host-setup", description="Settings for a Linux host (the installer "
                                "runs this): the Mac app's server on, and posting through Google Chrome.")
    p.add_argument("--port", type=int, help="the Mac app's port")
    args = p.parse_args(argv)
    if args.port is not None and not 1024 <= args.port <= 65535:
        print("The port is a number from 1024 to 65535.")
        return 2
    done = setup(args.port)
    print(("Set up: " + "; ".join(done) + ".") if done else "Settings already set up for this host.")
    return 0
