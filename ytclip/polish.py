"""AI polish: one AI call per video (Claude or Gemini, Settings → AI) that tidies the clips the finder chose.

The AI gets each clip's transcript and one frame from it, and sends back:

* a hook per clip - a few punchy words for the top banner and the upload caption,
* a bottom line per clip - a call to follow or comment written for that clip, which the bottom
  banner shows instead of "Follow for part 2" on a clip with no part 2,
* caption fixes - words the transcript misheard (names, slang, jargon), as exact
  "heard" -> "correct" swaps with the same number of words, so the word timings still fit,
* where the speaker is across the frame, so vertical crops keep them in shot,
* whether the video already has subtitles burned into the picture.

Anything the user edited by hand is left alone. A failed call never stops an export: the
clips just go out unpolished.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .analyze import Clip
from .ffmpeg import ffmpeg_exe, run
from .subtitles import Word, words_between

FRAME_WIDTH = 512
MAX_HOOK = 60  # characters
MAX_BOTTOM = 70  # room for an @handle; a longer line is dropped, not cut
MAX_FIXES = 12  # per clip

SCHEMA = {
    "type": "object",
    "properties": {
        "burned_in_subtitles": {
            "type": "boolean",
            "description": "True if the frames already show subtitles or captions drawn into the picture",
        },
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clip": {"type": "integer", "description": "The clip number from the list"},
                    "hook": {"type": "string", "description": "2 to 7 punchy words that make people stop scrolling"},
                    "bottom_text": {"type": "string",
                                    "description": "2 to 6 words for the banner under the clip: a call to follow, "
                                                   "comment or watch again, written for this clip, that never "
                                                   "promises a part 2"},
                    "rating": {"type": "integer",
                               "description": "1-10: how well this works as a standalone short for someone who "
                                              "hasn't seen the video"},
                    "rating_note": {"type": "string",
                                    "description": "Under 7: what holds it back, in a few words. Otherwise empty"},
                    "subject_x": {"type": "number",
                                  "description": "Where the main speaker's face is across the frame: 0 left edge, "
                                                 "0.5 centre, 1 right edge. 0.5 if nobody is on screen"},
                    "fixes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heard": {"type": "string", "description": "Words exactly as they appear in the "
                                                                           "transcript"},
                                "correct": {"type": "string", "description": "The same number of words, spelled "
                                                                             "correctly"},
                            },
                            "required": ["heard", "correct"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["clip", "hook", "bottom_text", "rating", "rating_note", "subject_x", "fixes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["burned_in_subtitles", "clips"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are the finishing editor for short vertical clips cut from a longer video. For each numbered clip "
    "you get its transcript (from automatic captions, so it can mishear names, slang and jargon) and one "
    "frame from the middle of it.\n"
    "- hook: 2 to 7 words for a banner above the clip. Make it specific to what happens in the clip, "
    "honest (never promise something the clip doesn't deliver), with no emojis, hashtags or quotation marks. "
    "Write it as the person posting the clip would, to tease what happens, even for a weak clip: never call "
    "the clip dull, pointless or empty in the hook. Criticism goes in rating_note.\n"
    "- bottom_text: 2 to 6 words for a banner under the clip that gets people to follow, comment or watch "
    "again, written for this clip: a question about what happens, or what following brings. Keep the spirit "
    "of the account's usual bottom line when one is given, and include any @handle you're told to. Never "
    "promise a part 2, a next part, a follow-up video or that the story continues. No emojis, hashtags or "
    "quotation marks. Vary it from clip to clip.\n"
    "- fixes: only words the captions clearly got wrong, judged from context and the video title. 'heard' "
    "must be copied exactly from that clip's transcript, and 'correct' must have the same number of words. "
    "Don't fix grammar, filler words or the speaker's own slang. Usually there are none.\n"
    "- rating: be a tough judge. 8+ means it hooks fast, makes sense with no context and pays off. 4 or less "
    "means it starts mid-thought, needs context from earlier, ends before the payoff or is just dull. "
    "Rate tragedy, violence or anything about children low unless it's clearly fine to repost.\n"
    "- subject_x: where the person talking is, across the frame.\n"
    "- burned_in_subtitles: whether any frame shows subtitles or captions that are part of the picture "
    "(not on-screen titles, logos or a news ticker)."
)


class PolishError(RuntimeError):
    """A plain-English reason the polish didn't happen."""


@dataclass
class ClipPolish:
    hook: str = ""
    subject_x: float | None = None
    fixes: list[tuple[str, str]] = field(default_factory=list)
    rating: int = 0
    rating_note: str = ""
    bottom_text: str = ""  # the bottom banner's line


@dataclass
class Polish:
    clips: dict[int, ClipPolish]  # keyed by the clip's index in the list that was sent
    burned_in_subtitles: bool = False
    model: str = ""


def grab_frames(src: Path, clips: list[Clip], folder: Path) -> list[Path | None]:
    """One small JPEG from the middle of each clip (None where ffmpeg couldn't read one)."""
    out: list[Path | None] = []
    for i, c in enumerate(clips):
        dest = folder / f"frame{i:02d}.jpg"
        try:
            run([ffmpeg_exe(), "-y", "-v", "error", "-ss", f"{c.start + c.length / 2:.3f}", "-i", str(src),
                 "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "5", str(dest)])
            out.append(dest if dest.is_file() and dest.stat().st_size else None)
        except Exception:
            out.append(None)
    return out


def build_content(title: str, clips: list[Clip], words: list[Word], frames: list[Path | None],
                  guide: str = "", bottom: str = "", handles: list[str] | None = None) -> list[dict]:
    """The user message. `guide` is learn.Lessons.prompt_text(): how earlier hooks did. `bottom` is the
    line the bottom banner shows on a clip with no part 2 (Settings → Layout), which the clips' bottom
    lines stand in for, and `handles` the @handles they have to keep (layout.bottom_handles)."""
    head = (f"{guide}\n\n" if guide else "") + f"Video title: {title}\n"
    if bottom.strip():
        head += f"The account's usual bottom line: {bottom.strip()}\n"
    if handles:
        head += f"Every bottom line must include {' and '.join(handles)}\n"
    blocks: list[dict] = [{"type": "text", "text": head + "\nClips:"}]
    for i, c in enumerate(clips, 1):
        said = " ".join(w.text for w in words_between(words, c.start, c.end)) or "(nobody speaks)"
        blocks.append({"type": "text", "text": f"\nClip {i} ({c.length:.0f}s)\nTranscript: {said}"})
        frame = frames[i - 1] if i - 1 < len(frames) else None
        if frame:
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.b64encode(frame.read_bytes()).decode()}})
    return blocks


def parse(text: str, count: int) -> Polish:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PolishError("The AI's answer couldn't be read.") from exc
    if not isinstance(data, dict):
        raise PolishError("The AI's answer couldn't be read.")
    return parse_data(data, count)


def parse_data(data: dict, count: int) -> Polish:
    out = Polish({}, bool(data.get("burned_in_subtitles")))
    for item in data.get("clips") or []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item["clip"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= n < count or n in out.clips:
            continue
        hook = _line(item.get("hook"), MAX_HOOK)
        bottom = _line(item.get("bottom_text"), MAX_BOTTOM, cut=False)  # cutting could drop its @handle
        try:
            x = min(1.0, max(0.0, float(item.get("subject_x"))))
        except (TypeError, ValueError):
            x = None
        fixes = []
        for f in (item.get("fixes") or [])[:MAX_FIXES]:
            heard, correct = str(f.get("heard") or "").split(), str(f.get("correct") or "").split()
            if heard and len(heard) == len(correct) and heard != correct:
                fixes.append((" ".join(heard), " ".join(correct)))
        try:
            rating = min(10, max(1, int(item.get("rating"))))
        except (TypeError, ValueError):
            rating = 0
        note = re.sub(r"\s+", " ", str(item.get("rating_note") or "")).strip()[:120] if rating and rating < 7 else ""
        out.clips[n] = ClipPolish(hook, x, fixes, rating, note, bottom)
    return out


def _line(value, limit: int, cut: bool = True) -> str:
    """One tidy banner line: no quotes, hashtags or runs of spaces ("#1" isn't a hashtag). Over the
    limit it's cut at a word, or with cut False left out ("")."""
    text = re.sub(r"(?<!\w)#[^\W\d_]\w*", "", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip().strip('"“”').strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] if cut else ""
    return text


def ask(title: str, clips: list[Clip], words: list[Word], src: Path, s,
        log: Callable[[str], None] | None = None, check: Callable[[], None] | None = None,
        guide: str = "", bottom: str = "", handles: list[str] | None = None) -> Polish:
    """Send the clips to the AI Settings → AI chose (`s` is the Settings) and read back its polish.
    Raises PolishError."""
    from . import ai

    if not ai.has_key(s):
        raise PolishError(ai.missing_key(s))
    with tempfile.TemporaryDirectory(prefix="ytclip-polish-") as tmp:
        frames = grab_frames(src, clips, Path(tmp))
        content = build_content(title, clips, words, frames, guide, bottom, handles)
    try:
        answer = ai.ask_json(s, "polish", SYSTEM, content, SCHEMA, effort="low", max_tokens=8000, log=log,
                             check=check, what=f"polish {len(clips)} clip{'s' if len(clips) != 1 else ''}")
    except ai.AIRefused as exc:
        raise PolishError(f"{exc} The clips go out unpolished.") from exc
    except ai.AIError as exc:
        raise PolishError(str(exc)) from exc
    result = parse_data(answer.data, len(clips))
    result.model = answer.model
    return result


# ---------------------------------------------------------------- applying it

def _bare(token: str) -> str:
    return re.sub(r"[^\w']", "", token.lower())


def fix_words(words: list[Word], start: float, end: float, heard: str, correct: str) -> int:
    """Swap each run of `heard` inside [start, end) for `correct`, word for word, keeping each
    word's timing and its trailing punctuation. Returns how many runs were changed."""
    want = [_bare(t) for t in heard.split()]
    new = correct.split()
    idx = [i for i, w in enumerate(words) if w.end > start and w.start < end]
    changed, k = 0, 0
    while k + len(want) <= len(idx):
        run_ = idx[k:k + len(want)]
        if [_bare(words[i].text) for i in run_] == want:
            for i, token in zip(run_, new):
                tail = re.search(r"[^\w']*$", words[i].text).group(0)
                words[i].text = token if re.search(r"[^\w']$", token) else token + tail
            changed += 1
            k += len(want)
        else:
            k += 1
    return changed


def apply(polish: Polish, clips: list[Clip], words: list[Word], banner_template: str = "",
          banner_on: bool = False, bottom_on: bool = False) -> dict:
    """Write the polish into the clips and words. Returns a summary for clips.json. Each clip keeps the
    AI's bottom line (Clip.bottom_ai), which shows on a clip with no part 2 (layout.bottom_banner);
    bottom_on: your settings show those lines (layout.uses_ai_bottom), so they're counted."""
    from .layout import promises_more

    hooks = moved = bottoms = 0
    fixes: list[dict] = []
    for i, c in enumerate(clips):
        cp = polish.clips.get(i)
        if not cp:
            continue
        c.rating, c.rating_note = cp.rating, cp.rating_note
        if cp.hook:
            c.hook = cp.hook
            hooks += 1
            if banner_on and not c.edited and c.top_text is None and "{title}" in banner_template:
                c.top_text = banner_template.replace("{title}", cp.hook)
        if cp.bottom_text and not c.edited and not promises_more(cp.bottom_text):
            c.bottom_ai = cp.bottom_text
            bottoms += bottom_on
        if cp.subject_x is not None and not c.edited and abs(c.crop_x - 0.5) < 1e-6:
            c.crop_x = round(cp.subject_x, 3)
            moved += abs(cp.subject_x - 0.5) > 0.05
        if not c.edited:
            for heard, correct in cp.fixes:
                if fix_words(words, c.start, c.end, heard, correct):
                    fixes.append({"start": c.start, "end": c.end, "heard": heard, "correct": correct})
    return {"model": polish.model, "hooks": hooks, "bottom_lines": bottoms, "caption_fixes": len(fixes),
            "crops_moved": int(moved),
            "burned_in_subtitles": polish.burned_in_subtitles, "fixes": fixes}


def reapply_fixes(summary: dict, words: list[Word]) -> int:
    """Put saved caption fixes back after the transcript is loaded again (a restored session)."""
    return sum(bool(fix_words(words, f["start"], f["end"], f["heard"], f["correct"]))
               for f in (summary or {}).get("fixes", []) if isinstance(f, dict) and {"start", "end", "heard",
                                                                                  "correct"} <= f.keys())
