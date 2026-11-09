"""Where the Mac app keeps its files: the PCs it's linked to, and a cache of pictures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def support_dir() -> Path:
    """~/Library/Application Support/ClipMonster Remote on a Mac (CLIPMONSTER_REMOTE_HOME overrides it)."""
    if os.environ.get("CLIPMONSTER_REMOTE_HOME"):
        base = Path(os.environ["CLIPMONSTER_REMOTE_HOME"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir() -> Path:
    if os.environ.get("CLIPMONSTER_REMOTE_HOME"):
        base = Path(os.environ["CLIPMONSTER_REMOTE_HOME"]) / "cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / APP_NAME
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME / "cache"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base
