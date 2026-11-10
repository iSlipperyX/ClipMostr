"""The score at each play, read off the broadcast's own scoreboard, for the reel's score bug.

A broadcast shows the score in a corner (the "score bug": LAL 104 | 106 GSW · 4th 0:12). Reading it needs eyes
on the picture, so the frames go to the AI that can see pictures (Claude or Gemini, with your key): one
request with a frame from the start and the end of every play, small JPEGs. The local AI can't see pictures,
so with only the local AI (or none) the reel's bug shows the teams without a score, which is never wrong.

What comes back is checked before it's shown: scores are numbers, the teams match the game's, and a score that
goes *down* from one play to a later one is a misread and dropped (scores only go up within a game). The
score shown during a play is the one from its start; the one from its end replaces it once the moment is over.
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from typing import Callable

from ..ffmpeg import NO_WINDOW, ffmpeg_exe

SEES = ("claude", "gemini")  # the AIs that can read a picture
FRAME_W = 768  # wide enough to read a score bug's digits, small enough to send 14 of

SYSTEM = """You read the on-screen scoreboard ("score bug") of a sports broadcast. For each numbered frame, say \
whether a scoreboard is visible and, if it is, exactly what it shows: the two teams as written, their scores, \
and the period and clock if shown. Copy what's on screen; don't guess digits you can't read (set found to false \
for that frame). Replays and close-ups often hide the scoreboard: that's fine, say found false."""

SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"},
            "found": {"type": "boolean"},
            "left_team": {"type": "string"}, "left_score": {"type": "string"},
            "right_team": {"type": "string"}, "right_score": {"type": "string"},
            "period": {"type": "string"}, "clock": {"type": "string"}},
            "required": ["n", "found", "left_team", "left_score", "right_team", "right_score", "period", "clock"],
            "additionalProperties": False}},
    },
    "required": ["frames"],
    "additionalProperties": False,
}


@dataclass
class Score:
    left: str  # team as the broadcast writes it ("LAL")
    left_score: int
    right: str
    right_score: int
    period: str = ""
    clock: str = ""

    def text(self) -> str:
        tail = " · ".join(x for x in (self.period, self.clock) if x)
        return f"{self.left} {self.left_score} | {self.right_score} {self.right}" + (f"  {tail}" if tail else "")


def short_name(name: str) -> str:
    """A team's name for the bug when the broadcast's own isn't known: 'Golden State Warriors' → 'WARRIORS'."""
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    if not words:
        return ""
    full = " ".join(words).upper()
    if len(full) <= 12:
        return full
    return words[-1].upper()[:12]


AWAY_FIRST = ("basketball", "football", "baseball", "hockey")  # US sports say "away at home"


def teams_for(game: dict) -> tuple[str, str]:
    """The two teams for a bug without a score, in the order the sport's broadcasts put them."""
    home, away = short_name(game.get("home") or ""), short_name(game.get("away") or "")
    return (away, home) if game.get("sport") in AWAY_FIRST else (home, away)


def frame_jpeg(src, at: float) -> bytes:
    cmd = [ffmpeg_exe(), "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-frames:v", "1",
           "-vf", f"scale={FRAME_W}:-2", "-q:v", "4", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    done = subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    return done.stdout if done.returncode == 0 else b""


def can_read(s) -> bool:
    from .. import ai

    return any(p in SEES for p in ai.order(s))


def read(src, times: list[float], s, *, log: Callable[[str], None] | None = None,
         check: Callable[[], None] | None = None) -> tuple[list[Score | None], str]:
    """The score shown at each of `times` (source seconds), None where it couldn't be read; and the model."""
    from .. import ai

    if not times or not can_read(s):
        return [None] * len(times), ""
    blocks: list[dict] = []
    for i, t in enumerate(times, 1):
        jpg = frame_jpeg(src, t)
        if not jpg:
            continue
        blocks.append({"type": "text", "text": f"Frame {i}:"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.b64encode(jpg).decode()}})
    if not blocks:
        return [None] * len(times), ""
    blocks.append({"type": "text", "text": f"Read the scoreboard in each of the {len(times)} frames."})
    try:
        ans = ai.ask_json(s, "polish", SYSTEM, blocks, SCHEMA, effort="low", max_tokens=3000, log=log, check=check,
                          what="read the scoreboard", only=list(SEES))
    except ai.AIError as exc:
        if log:
            log(f"The scoreboard couldn't be read ({exc}); the reel's score bug shows the teams only.")
        return [None] * len(times), ""
    out: list[Score | None] = [None] * len(times)
    for f in ans.data.get("frames") or []:
        if not isinstance(f, dict) or not f.get("found"):
            continue
        n = int(f.get("n") or 0)
        if not 1 <= n <= len(times):
            continue
        sc = _score(f)
        if sc is not None:
            out[n - 1] = sc
    return consistent(out, times), ans.model


def _score(f: dict) -> Score | None:
    def num(v) -> int | None:
        m = re.fullmatch(r"\s*(\d{1,3})\s*", str(v or ""))
        return int(m[1]) if m else None

    a, b = num(f.get("left_score")), num(f.get("right_score"))
    lt, rt = " ".join(str(f.get("left_team") or "").split())[:14], " ".join(str(f.get("right_team") or "").split())[:14]
    if a is None or b is None or not lt or not rt:
        return None
    return Score(lt.upper(), a, rt.upper(), b, " ".join(str(f.get("period") or "").split())[:10],
                 " ".join(str(f.get("clock") or "").split())[:8])


def consistent(scores: list[Score | None], times: list[float]) -> list[Score | None]:
    """Drop reads that can't be right: another pair of teams than most frames show, or a score lower than one
    read earlier in the game."""
    teams: dict[tuple[str, str], int] = {}
    for sc in scores:
        if sc:
            teams[(sc.left, sc.right)] = teams.get((sc.left, sc.right), 0) + 1
    if not teams:
        return scores
    pair = max(teams, key=teams.get)
    out: list[Score | None] = [sc if sc and (sc.left, sc.right) == pair else None for sc in scores]
    by_time = sorted(range(len(times)), key=lambda i: times[i])
    best = (-1, -1)
    for i in by_time:
        sc = out[i]
        if sc is None:
            continue
        if sc.left_score < best[0] or sc.right_score < best[1]:
            out[i] = None
            continue
        best = (sc.left_score, sc.right_score)
    return out
