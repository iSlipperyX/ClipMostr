"""prefs.json: the Mac app's own choices (appearance, notifications, update check)."""

from __future__ import annotations

import json
import os

from .paths import support_dir

DEFAULTS = {"appearance": "system", "notify": True, "check_updates": True}


def load() -> dict:
    try:
        data = json.loads((support_dir() / "prefs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **(data if isinstance(data, dict) else {})}


def set(key: str, value) -> None:  # noqa: A001 - prefs.set reads well
    data = load()
    data[key] = value
    p = support_dir() / "prefs.json"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, p)
