"""Problems ClipMonster worked around by itself, written down in automation.log with the details.

When the graphics card fails part-way (a driver timeout, "device lost", running out of graphics memory)
whisper.cpp and the local AI carry on on the CPU instead of stopping the run. The run's own log gets one
plain line; automation.log gets the full story (what failed, the last thing the program said, what was
done about it), so it can be looked into later, whether the run was automation or the window.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable

MAX_BYTES = 2_000_000  # the same roll-over as pipeline.file_logger

# What whisper.cpp and llama.cpp print when the graphics card runs out of memory, or the card or its driver
# fails (ggml's own messages: ggml-vulkan.cpp, ggml-cuda.cu, ggml-backend.cpp). Each is specific to a
# failure: the normal start-up lines name the driver ("ggml_vulkan: 0 = AMD Radeon RX 9060 XT (AMD proprietary
# driver)"), so a word like "driver" alone would blame the card for anything.
OOM_SIGNS = ("out of memory", "failed to allocate", "unable to allocate", "outofdevicememory",
             "cudamalloc failed", "not enough memory", "insufficient memory", "failed to create context",
             "failed to fit params")
GPU_SIGNS = ("device lost", "devicelost", "device fault", "ggml_vulkan: error", "vk::", "vk_error",
             "cuda error", "launch timed out", "hiperror", "hip error", "metal error", "gpu hang",
             "illegal memory access", "unspecified launch failure", "driver timeout", "timeout detection")


def classify(lines: Iterable[str]) -> str:
    """'oom' (the graphics card ran out of memory), 'gpu' (the card or its driver failed) or '' (neither)
    for what a failed whisper.cpp or llama.cpp printed."""
    text = "\n".join(lines).lower()
    if any(sign in text for sign in OOM_SIGNS):
        return "oom"
    if any(sign in text for sign in GPU_SIGNS):
        return "gpu"
    return ""


def log_path() -> Path:
    from .settings import app_dir

    return app_dir() / "automation.log"


def write(lines: Iterable[str], path: Path | None = None) -> None:
    """Append lines to automation.log, each stamped like the runner's own lines. Never raises."""
    path = path or log_path()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{stamp}  {line}\n")
    except OSError:
        pass


def gpu_fallback(component: str, problem: str, said: Iterable[str], action: str,
                 log: Callable[[str], None] | None = None, path: Path | None = None) -> None:
    """A graphics card failure that `component` worked around with `action`. `said` is the tail of what
    the program printed (its error lines go into automation.log)."""
    tail = [line.strip() for line in said if line and line.strip()][-12:]
    write([f"[GPU] {component}: {problem}. {action}"] + [f"[GPU]   {line[:300]}" for line in tail], path)
    if log:
        log(f"{component}: {problem}. {action} (details in automation.log)")
