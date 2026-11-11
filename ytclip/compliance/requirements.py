"""The minimums a video made for a Telegram request must meet before it's handed over as ready.

Someone asking from a phone doesn't watch the video being made, so what comes back is checked for them:

* **length**: longer than a minute (61 s, Settings → Telegram → Shortest video). TikTok's Creator Rewards only
  pays for videos over a minute, and a 19-second single play isn't what "make me the highlights" means;
* **shape**: upright 9:16, at least 720 x 1280;
* **sound**: it has a sound track, and it isn't silent;
* **it plays**: a length and a picture can be read from the file, at 24 frames a second or more.

A video that meets them all is handed over as any other. One that doesn't still comes into the library, held
for an OK with the reason beside it (posting's review hold), so nothing short of the minimums posts by itself
and nothing made is thrown away. The same rules are written into the video's clips.json ("requires") when it's
made, so posting's intake holds it even if it comes in before the check here has run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .duration_guard import MIN_SECONDS

MAX_SECONDS = 600.0  # TikTok's own app stops at ten minutes
MIN_WIDTH, MIN_HEIGHT = 720, 1280
SHAPE = 9 / 16
SHAPE_SLACK = 0.03
MIN_FPS = 23.5
SILENT_LUFS = -50.0  # quieter than this all the way through is no sound at all


@dataclass
class Check:
    id: str  # length | shape | sound | plays
    ok: bool
    found: str  # "26 s"
    need: str  # "over 60 s"

    def to_json(self) -> dict:
        return {"id": self.id, "ok": self.ok, "found": self.found, "need": self.need}


@dataclass
class Review:
    checks: list[Check] = field(default_factory=list)
    name: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def why(self) -> str:
        """'26 s (needs over 60 s); 1280 x 720 (needs upright 9:16)' for what it doesn't meet, '' when it does."""
        return "; ".join(f"{c.found} (needs {c.need})" for c in self.failed)

    def passed(self) -> str:
        """'72 s, 1080 x 1920, with sound' for a video that meets them."""
        return ", ".join(c.found for c in self.checks if c.ok and c.id != "plays")

    def to_json(self) -> dict:
        return {"ok": self.ok, "why": self.why(), "passed": self.passed(), "checks": [c.to_json() for c in self.checks]}


def rules(s=None) -> dict:
    """The minimums in force, from Settings → Telegram ({} = none: the check is off)."""
    try:
        least = float(getattr(s, "telegram_min_seconds", MIN_SECONDS) if s is not None else MIN_SECONDS)
    except (TypeError, ValueError):
        least = MIN_SECONDS
    if least <= 0:
        return {}
    return {"min_seconds": round(max(1.0, min(MAX_SECONDS - 1, least)), 1), "max_seconds": MAX_SECONDS,
            "min_width": MIN_WIDTH, "min_height": MIN_HEIGHT, "by": "telegram"}


def _seconds(v: float, fine: bool = False) -> str:
    return f"{v:.1f} s" if fine and abs(v - round(v)) >= 0.05 else f"{v:.0f} s"


def over(least: float) -> str:
    """'over 60 s' for a 61-second minimum (a minute, with a second's room for players that round down)."""
    whole = abs(least - round(least)) < 0.05
    return f"over {_seconds(least - 1)}" if least >= 2 and whole else f"{_seconds(least, fine=True)} or more"


def review(need: dict, *, length: float | None, width: int | None = None, height: int | None = None,
           sound: bool | None = None, lufs: float | None = None, fps: float | None = None,
           name: str = "") -> Review:
    """Check a video's measurements against `need` (rules()). What wasn't measured (None) isn't judged."""
    r = Review(name=name)
    if not need:
        return r
    least, most = float(need.get("min_seconds") or 0), float(need.get("max_seconds") or MAX_SECONDS)
    if not length or length <= 0:
        r.checks.append(Check("plays", False, "it can't be read", "a video that plays"))
        return r
    if length < least:  # (a near miss is said to the tenth: "60.4 s" isn't "over 60 s" by the rule's own margin)
        r.checks.append(Check("length", False, _seconds(length, fine=least - length < 1.5), over(least)))
    elif length > most:
        r.checks.append(Check("length", False, _seconds(length), f"{most / 60:.0f} minutes at most"))
    else:
        r.checks.append(Check("length", True, _seconds(length), over(least)))
    if width and height:
        upright = abs(width / height - SHAPE) <= SHAPE_SLACK
        big = width >= int(need.get("min_width") or MIN_WIDTH) and height >= int(need.get("min_height") or MIN_HEIGHT)
        r.checks.append(Check("shape", upright and big, f"{width} x {height}",
                              "upright 9:16" if not upright else f"at least {MIN_WIDTH} x {MIN_HEIGHT}"))
    if sound is not None:
        silent = sound and lufs is not None and lufs < SILENT_LUFS
        r.checks.append(Check("sound", bool(sound) and not silent,
                              "with sound" if sound and not silent else "silent" if sound else "no sound track",
                              "sound"))
    if fps is not None and fps < MIN_FPS:
        r.checks.append(Check("plays", False, f"{fps:g} frames a second", "24 or more"))
    return r


def review_file(path: Path, need: dict, *, probe=None, loudness=None) -> Review:
    """Measure the file at `path` and check it. `probe` and `loudness` stand in for ffmpeg's in tests."""
    path = Path(path)
    if not need:
        return Review(name=path.name)
    if probe is None:
        from ..ffmpeg import probe
    try:
        info = probe(path)
    except Exception:  # noqa: BLE001 - a file ffmpeg can't open doesn't play
        return review(need, length=None, name=path.name)
    lufs = None
    if loudness is not None and info.has_audio:
        try:
            lufs = loudness(path)
        except Exception:  # noqa: BLE001 - not measured: not judged
            lufs = None
    return review(need, length=info.duration, width=info.width, height=info.height, sound=info.has_audio, lufs=lufs,
                  fps=info.fps, name=path.name)


def from_record(need: dict, clip: dict) -> Review:
    """Check a clip by what its clips.json row says of it (its length, and its quality check's measurements
    when a reel wrote them): what posting's intake has to go on."""
    qc = clip.get("qc") if isinstance(clip.get("qc"), dict) else {}

    def number(*values):
        for v in values:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
        return None

    length = number(qc.get("length"), clip.get("length"))
    if length is None:  # an older record: its span of the source, or nothing to judge it by
        start, end = number(clip.get("start")), number(clip.get("end"))
        if start is None or end is None or end <= start:
            return Review(name=str(clip.get("file") or ""))
        length = end - start
    return review(need, length=length, width=number(qc.get("width")),
                  height=number(qc.get("height")), sound=qc.get("sound") if isinstance(qc.get("sound"), bool) else None,
                  lufs=number(qc.get("lufs")), name=str(clip.get("file") or ""))


def hold_reason(need, clip: dict) -> str:
    """Why a clip made for a Telegram request waits for an OK ('' when it meets the minimums)."""
    if not isinstance(need, dict) or not need:
        return ""
    made = clip.get("requirements") if isinstance(clip.get("requirements"), dict) else None
    if made is not None and isinstance(made.get("ok"), bool):
        why = "" if made["ok"] else str(made.get("why") or "it doesn't meet the minimums")
    else:
        why = from_record(need, clip).why()
    return f"Under the minimums for a Telegram request: {why}" if why else ""
