"""Where ClipMonster for Mac keeps what it knows about the PC it's linked to: the PCs, its own choices and the
sports clips inbox in a "pc" folder beside the app's settings (settings.app_dir(): ~/Library/Application
Support/ClipMonster on a Mac), and pictures in ~/Library/Caches/ClipMonster/pc. CLIPMONSTER_REMOTE_HOME puts
them all somewhere else (the tests do)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import APP_NAME, OLD_NAME

ENV = "CLIPMONSTER_REMOTE_HOME"
OLD_FILES = ("hosts.json", "prefs.json", "keys.json", "inbox.json")


def support_dir() -> Path:
    if os.environ.get(ENV):
        base = Path(os.environ[ENV])
    else:
        from ..settings import app_dir

        base = app_dir() / "pc"
        if not base.exists():
            _bring_over(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir() -> Path:
    if os.environ.get(ENV):
        base = Path(os.environ[ENV]) / "cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / APP_NAME / "pc"
    else:
        from ..settings import local_dir

        base = local_dir("pc-cache")
    base.mkdir(parents=True, exist_ok=True)
    return base


def old_dir() -> Path:
    """ClipMonster Remote's folder (the first builds)."""
    return Path.home() / "Library" / "Application Support" / OLD_NAME


def _bring_over(new: Path) -> None:
    """The first builds were called ClipMonster Remote and kept their own folder: bring its linked PCs, choices
    and inbox over, once (copied: that app may still be on the Mac)."""
    old = old_dir()
    if sys.platform != "darwin" or not old.is_dir():
        return
    try:
        new.mkdir(parents=True, exist_ok=True)
        for name in OLD_FILES:
            if (old / name).is_file() and not (new / name).exists():
                shutil.copy2(old / name, new / name)
    except OSError:
        pass
