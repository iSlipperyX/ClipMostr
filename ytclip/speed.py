"""How much of the PC clipping may use: Settings.speed, quiet | balanced | full.

* Quiet: a quarter of the CPU threads for Whisper, and automation runs at idle priority, so it only
  uses the PC when nothing else wants it.
* Balanced: half the threads, and automation at below-normal priority, so whatever you're doing
  comes first.
* Full power: every thread, Whisper on the graphics card when there is one (whisper_cpp.py), and
  automation at normal priority.

The ffmpeg runs automation starts inherit its priority. Clipping from the main window always runs
at normal priority: you're waiting for it.
"""

from __future__ import annotations

import os
import sys

LEVELS = ("quiet", "balanced", "full")
WORDS = {"quiet": "Quiet", "balanced": "Balanced", "full": "Full power"}

# Windows priority classes
IDLE = 0x40
BELOW_NORMAL = 0x4000
NORMAL = 0x20
PRIORITY = {"quiet": IDLE, "balanced": BELOW_NORMAL, "full": NORMAL}
NICE = {"quiet": 19, "balanced": 10, "full": 0}


def level(value: str) -> str:
    return value if value in LEVELS else "balanced"


def threads(value: str, logical: int | None = None) -> int:
    """CPU threads for Whisper. faster-whisper's own default is 4, whatever the PC."""
    n = logical or os.cpu_count() or 4
    return {"quiet": max(2, n // 4), "balanced": max(4, n // 2), "full": max(4, n)}[level(value)]


def use_gpu(s) -> bool:
    return level(s.speed) == "full" and bool(s.gpu_whisper)


def hwaccel(value: str) -> bool:
    """Decode video on the graphics card where it helps (scene detection reads every frame)."""
    return level(value) != "quiet"


def set_priority(value: str) -> str:
    """Set this process's priority for the speed (automation calls it as settings change).
    Returns what it did, for the log, or "" when nothing changed or it couldn't."""
    value = level(value)
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetPriorityClass.argtypes = (ctypes.c_void_p,)
        kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        me = kernel32.GetCurrentProcess()
        want = PRIORITY[value]
        if kernel32.GetPriorityClass(me) == want or not kernel32.SetPriorityClass(me, want):
            return ""
        return f"Speed: {WORDS[value]}"
    try:  # elsewhere only lowering is allowed without rights: go as low as asked, never back up
        now = os.getpriority(os.PRIO_PROCESS, 0)
        if NICE[value] > now:
            os.setpriority(os.PRIO_PROCESS, 0, NICE[value])
            return f"Speed: {WORDS[value]}"
    except (AttributeError, OSError):
        pass
    return ""
