"""AI polish: one Claude call per video that tidies the clips the finder chose.

Claude gets each clip's transcript and one frame from it, and sends back:

* a hook per clip - a few punchy words for the top banner and the upload caption,
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
import os
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
                "required": ["clip", "hook", "rating", "rating_note", "subject_x", "fixes"],
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
    "honest (never promise something the clip doesn't deliver), with no emojis, hashtags or quotation marks.\n"
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
                  guide: str = "") -> list[dict]:
    """The user message. `guide` is learn.Lessons.prompt_text(): how earlier hooks did."""
    blocks: list[dict] = [{"type": "text", "text": (f"{guide}\n\n" if guide else "") + f"Video title: {title}\n\nClips:"}]
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
        raise PolishError("Claude's answer couldn't be read.") from exc
    out = Polish({}, bool(data.get("burned_in_subtitles")))
    for item in data.get("clips") or []:
        try:
            n = int(item["clip"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= n < count or n in out.clips:
            continue
        hook = re.sub(r"\s+", " ", str(item.get("hook") or "")).strip().strip('"“”').strip()
        if len(hook) > MAX_HOOK:
            hook = hook[:MAX_HOOK].rsplit(" ", 1)[0]
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
        out.clips[n] = ClipPolish(hook, x, fixes, rating, note)
    return out


def ask(title: str, clips: list[Clip], words: list[Word], src: Path, api_key: str, model: str,
        log: Callable[[str], None] | None = None, check: Callable[[], None] | None = None,
        guide: str = "") -> Polish:
    """Send the clips to Claude and read back its polish. Raises PolishError."""
    if not ((api_key or "").strip() or os.environ.get("ANTHROPIC_API_KEY")):
        raise PolishError("No Anthropic API key set (Settings → AI).")
    import anthropic

    from .ai import _client, _friendly

    with tempfile.TemporaryDirectory(prefix="ytclip-polish-") as tmp:
        frames = grab_frames(src, clips, Path(tmp))
        content = build_content(title, clips, words, frames, guide)
    if log:
        log(f"Asking {model} to polish {len(clips)} clip{'s' if len(clips) != 1 else ''}…")
    try:
        with _client(api_key, max_retries=3).beta.messages.stream(
            model=model,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default"},
        ) as stream:
            for i, _event in enumerate(stream):
                if check and i % 25 == 0:
                    check()
            response = stream.get_final_message()
    except (anthropic.APIError, TypeError) as exc:
        raise PolishError(str(_friendly(exc, model))) from exc
    if response.stop_reason == "refusal":
        raise PolishError("Claude declined to polish these clips.")
    if response.stop_reason == "max_tokens":
        raise PolishError("Claude's answer was cut off.")
    result = parse("".join(b.text for b in response.content if b.type == "text"), len(clips))
    result.model = getattr(response, "model", None) or model
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
          banner_on: bool = False) -> dict:
    """Write the polish into the clips and words. Returns a summary for clips.json."""
    hooks = moved = 0
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
        if cp.subject_x is not None and not c.edited and abs(c.crop_x - 0.5) < 1e-6:
            c.crop_x = round(cp.subject_x, 3)
            moved += abs(cp.subject_x - 0.5) > 0.05
        if not c.edited:
            for heard, correct in cp.fixes:
                if fix_words(words, c.start, c.end, heard, correct):
                    fixes.append({"start": c.start, "end": c.end, "heard": heard, "correct": correct})
    return {"model": polish.model, "hooks": hooks, "caption_fixes": len(fixes), "crops_moved": int(moved),
            "burned_in_subtitles": polish.burned_in_subtitles, "fixes": fixes}


def reapply_fixes(summary: dict, words: list[Word]) -> int:
    """Put saved caption fixes back after the transcript is loaded again (a restored session)."""
    return sum(bool(fix_words(words, f["start"], f["end"], f["heard"], f["correct"]))
               for f in (summary or {}).get("fixes", []) if isinstance(f, dict) and {"start", "end", "heard",
                                                                                  "correct"} <= f.keys())
