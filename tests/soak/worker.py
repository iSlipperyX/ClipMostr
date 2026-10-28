"""`ytclip auto`, with faults the soak test (soak.py) can switch on while it runs.

The soak test writes control.json next to the profile; each hook reads it when it's reached:

* "hang_export": the next export hangs in a real ffmpeg that makes no progress (the watchdog's job);
* "low_disk": the drives report 1 GB free (the disk guard's job);
* "block_downloads": the next download fails the way YouTube's sign-in request does.

What was injected goes to events.jsonl, so the soak test can match it with what automation did.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CONTROL = Path(os.environ["SOAK_CONTROL"])
EVENTS = CONTROL.with_name("events.jsonl")


def control() -> dict:
    try:
        return json.loads(CONTROL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def once(flag: str) -> bool:
    """True the first time a fault is switched on (each switch-on is numbered: hang_export: 2)."""
    want = control().get(flag) or 0
    if not want:
        return False
    done = [e for e in events() if e["fault"] == flag]
    if len(done) >= int(want):
        return False
    note(flag)
    return True


def events() -> list[dict]:
    try:
        return [json.loads(line) for line in EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def note(fault: str, **extra) -> None:
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"fault": fault, "at": time.time(), "pid": os.getpid(), **extra}) + "\n")


def install() -> None:
    from ytclip import engine, ffmpeg, pipeline

    real_export = engine.export_clip

    def export_clip(*args, **kwargs):
        if once("hang_export"):
            cmd = [ffmpeg.ffmpeg_exe(), "-v", "error", "-re", "-f", "lavfi", "-i", "anullsrc", "-t", "900",
                   "-f", "null", "-"]
            ffmpeg.run_with_progress(cmd, 0, None)  # no progress for 15 minutes, unless it's stopped
        return real_export(*args, **kwargs)

    engine.export_clip = export_clip

    real_free = pipeline.Runner.free_space

    def free_space(self, s):
        if control().get("low_disk"):
            return 1024 ** 3, "D:\\"
        return real_free(self, s)

    pipeline.Runner.free_space = free_space

    real_download = engine.download

    def download(url, *args, **kwargs):
        if once("block_downloads"):
            raise RuntimeError("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser")
        return real_download(url, *args, **kwargs)

    engine.download = download


if __name__ == "__main__":
    install()
    from ytclip.cli import main

    sys.exit(main(["auto", *sys.argv[1:]]))
