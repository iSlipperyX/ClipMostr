"""A folder of second videos for stacked layouts, dealt out one per clip.

Each clip gets the next video from a shuffled deck; when the deck runs out it is reshuffled,
never starting with the video used last, so the same gameplay never appears twice in a row.
The deck is kept in stack_pool.json, so the rotation carries on across videos and restarts.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from .download import VIDEO_EXTS
from .settings import Settings, app_dir


def pool_files(folder: str | Path) -> list[Path]:
    try:
        root = Path(folder).expanduser()
        return sorted(f for f in root.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
    except OSError:
        return []


def preview_video(s: Settings) -> str:
    """The second video to show in previews. Exports deal from the folder when it has videos (see
    Engine.export), so previews show the folder's first one; otherwise the single chosen video."""
    files = pool_files(s.stack_folder) if s.stack_folder else []
    return str(files[0]) if files else s.stack_video


class StackPool:
    def __init__(self, path: Path | None = None, rng: random.Random | None = None):
        self.path = path if path is not None else app_dir() / "stack_pool.json"
        self.rng = rng or random.Random()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def next(self, folder: str | Path) -> Path | None:
        files = pool_files(folder)
        if not files:
            return None
        key = str(Path(folder).expanduser().resolve())
        data = self._load()
        state = data.get(key) or {}
        names = {f.name: f for f in files}
        deck = [n for n in state.get("deck", []) if n in names]
        last = state.get("last", "")
        if not deck:
            deck = list(names)
            self.rng.shuffle(deck)
            if len(deck) > 1 and deck[0] == last:
                deck.append(deck.pop(0))
        pick = deck.pop(0)
        data[key] = {"deck": deck, "last": pick}
        try:
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass
        return names[pick]
