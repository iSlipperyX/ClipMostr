"""Changing a finished Studio video without making it again from scratch.

Someone watches a video and says "make the captions yellow" or "the music is too loud". None of that needs a
new script, a new voice or new pictures: the video's timeline.json has every caption and every level in it, so
the change is made there and the timeline is rendered again (editor/timeline_compiler.py). The first version
stays as it was; the new one lands beside it as "<title> (v2).mp4", listed in the folder's clips.json as an
edit of it, which is how posting knows to label it AI-generated and hold it for an OK.

read() takes what was said apart, clause by clause:

* what the timeline alone can answer (caption colour, size, style and place; the music's and the voice's
  level) becomes `edits`;
* a new voice ("change the voice to British Male") becomes `voice`: the same script is voiced again, which
  moves every caption and cut, so the video is made again from its storyboard, but nothing is rewritten;
* everything else ("the hook is too slow, start with the referee arguing") is `rest`, for the AI to rewrite
  the storyboard from (creator/critique.py).

apply() makes the edits on a Timeline; render() does the whole of a retouch for a video's folder. Only what
changed is made again: the captions are laid out anew when their look changed, the picture and the voice are
the files already there, and one ffmpeg run puts them together.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..creator import voice_engine
from ..creator.orchestrator import CreatorError
from ..editor import kinetic_subtitles, timeline_compiler
from ..subtitles import Word
from .timeline_schema import AUDIO_MUSIC, AUDIO_VOICE, SUBTITLES, Timeline, TimelineError

COLOURS = {"yellow": "#FFE600", "white": "#FFFFFF", "red": "#FF3B30", "green": "#00FF66", "blue": "#4F9BFF",
           "cyan": "#4FD8FF", "orange": "#FF9500", "pink": "#FF5FA2", "purple": "#B57BFF", "gold": "#FFC857",
           "lime": "#B6FF3B", "teal": "#2FD6C3", "black": "#101010"}
COLOUR_WORDS = COLOURS | {"golden": COLOURS["gold"], "aqua": COLOURS["cyan"], "turquoise": COLOURS["teal"],
                          "violet": COLOURS["purple"], "magenta": COLOURS["pink"]}
PRESETS = ("karaoke", "bounce", "pop", "plain")
SIZE_STEP = 1.2  # "bigger" and "smaller" move the captions' size by this much
SIZE_LIMITS = (48, 140)
PLACE_STEP = 0.1  # "higher" and "lower" move them this far up or down the picture
GAIN_STEP = 4.0  # dB a level moves for "louder" or "quieter"
GAIN_LIMITS = (-18.0, 12.0)

_CAPTION = r"(?:captions?|subtitles?|subs|caption text|on[- ]screen text|the text|the words on screen|lettering)"
_MUSIC = r"(?:music|soundtrack|backing track|background (?:music|track)|the beat|the song)"
_VOICE = r"(?:voice ?over|voiceover|narrator|narration|voice)"
_LOUDER = r"(?:louder|turn(?:ed)? up|turn it up|raise|boost|more volume|too quiet|too soft|can'?t hear|hard to hear)"
_QUIETER = (r"(?:quieter|softer|lower|turn(?:ed)? down|turn it down|less volume|too loud|drown(?:s|ing)?(?: out)?|"
            r"overpower(?:s|ing)?|distracting)")
_FILLER = re.compile(r"\b(?:please|pls|can you|could you|i want|i'd like|make|change|set|put|turn|use|have|give|"
                     r"the|a|an|it|its|them|this|that|video|to|be|is|are|in|into|with|just|also|and|then|too|bit|"
                     r"little|lot|much|more|way|of|on|for|so|very|really|should|needs?|instead|colou?r|colou?red|"
                     r"style|size|font|level|volume|only|all|every|whole)\b", re.I)


@dataclass
class Plan:
    """What a critique asks for, taken apart. `said` is each change in words ("captions in yellow")."""
    edits: dict = field(default_factory=dict)  # for apply(): {"captions": {...}, "music": {...}, "voice": {...}}
    voice: str = ""  # a voice_engine profile key, when another voice was asked for
    rest: str = ""  # what's left for the AI to rewrite the storyboard from
    said: list[str] = field(default_factory=list)

    @property
    def how(self) -> str:
        """rewrite (the AI writes it again) | voice (voiced again, nothing rewritten) | retouch (the timeline
        changed and rendered again)."""
        return "rewrite" if self.rest else "voice" if self.voice else "retouch" if self.edits else "rewrite"

    def to_json(self) -> dict:
        return {"edits": self.edits, "voice": self.voice, "rest": self.rest, "said": list(self.said)}


def _colour(text: str) -> tuple[str, str]:
    """(name, #hex) of the first colour named in `text`, or ("", "")."""
    m = re.search(r"#([0-9a-f]{6})\b", text, re.I)
    if m:
        return "#" + m.group(1).upper(), "#" + m.group(1).upper()
    for name in sorted(COLOUR_WORDS, key=len, reverse=True):
        if re.search(rf"\b{name}\b", text, re.I):
            return name, COLOUR_WORDS[name]
    return "", ""


def voice_named(text: str) -> str:
    """The voice profile a sentence asks for ("change voice to British Male", "a deeper voice"), or ""."""
    low = " ".join(str(text or "").lower().replace("-", " ").split())
    if not re.search(rf"\b{_VOICE}\b", low) and not re.search(r"\b(?:narrated|read|voiced) by\b", low):
        return ""
    for v in sorted(voice_engine.VOICES.values(), key=lambda v: -len(v.label)):
        if v.label.lower() in low:
            return v.key
    hints = (("british", "en_british_male"), ("whisper", "en_hushed"), ("hushed", "en_hushed"),
             ("documentary", "en_documentary"), ("energetic", "en_storyteller"), ("lively", "en_storyteller"),
             ("storyteller", "en_storyteller"), ("deeper", "en_male_deep"), ("deep", "en_male_deep"),
             ("bright", "en_female_bright"), ("upbeat", "en_female_bright"), ("warm", "en_female_warm"),
             ("female", "en_female_warm"), ("woman", "en_female_warm"), ("girl", "en_female_warm"),
             ("calm", "en_male_calm"), ("male", "en_male_deep"), ("man", "en_male_deep"))
    for word, key in hints:
        if re.search(rf"\b{word}\b", low):
            if word == "british" and re.search(r"\b(?:female|woman)\b", low):
                continue  # there's no British woman among the voices: the next hint decides
            return key
    return ""


def _clauses(text: str) -> list[str]:
    parts = re.split(r"[.;!?\n]+|,\s*(?:and\s+)?|\s+and\s+(?=(?:also\s+)?(?:make|change|turn|set|put|use|the|"
                     r"give|add|remove|drop|cut|fix|start|open|end|slow|speed)\b)|\s+(?:also|plus)\s+",
                     str(text or ""), flags=re.I)
    return [" ".join(p.split()) for p in parts if p and p.strip()]


def _leftover(clause: str, used: list[str]) -> bool:
    """Is there anything in `clause` the matched words (`used`: patterns) don't cover?"""
    rest = clause
    for pattern in used:
        rest = re.sub(pattern, " ", rest, flags=re.I)
    rest = _FILLER.sub(" ", rest)
    return len(re.findall(r"[A-Za-z]{3,}", rest)) >= 2


def read(text: str) -> Plan:
    """Take a critique apart (see the top of this file). Nothing here asks an AI: a clause either names a
    change the timeline can make, or it's left for the rewrite."""
    plan = Plan()
    rest: list[str] = []
    cap: dict = {}
    topic = ""  # what the clause before was about: "move them higher" after "make the captions smaller"
    for clause in _clauses(text):
        low = clause.lower()
        named = next((w for w, pat in (("captions", _CAPTION), ("music", _MUSIC), ("voice", _VOICE))
                      if re.search(rf"\b{pat}\b", low)), "")
        if not named and topic and re.search(r"\b(?:them|they|it|those)\b", low):
            low = f"{topic} {low}"
        topic = named or topic
        used: list[str] = []
        took = False
        about_captions = bool(re.search(rf"\b{_CAPTION}\b", low))
        if about_captions:
            used.append(_CAPTION)
            name, code = _colour(low)
            spoken = bool(re.search(r"\b(?:highlight(?:ed)?|spoken word|current word|active word|the word being "
                                    r"(?:said|spoken))\b", low))
            if code:
                cap["highlight" if spoken else "color"] = code
                plan.said.append(f"{'the spoken word' if spoken else 'captions'} in {name}")
                used += [re.escape(name), r"highlight(?:ed)?|spoken|current|active|word being (?:said|spoken)"]
                took = True
            if re.search(r"\b(?:bigger|larger|big|large|huge|too small|hard to read|can'?t read|tiny)\b", low):
                cap["size_scale"] = round(cap.get("size_scale", 1.0) * SIZE_STEP, 3)
                plan.said.append("bigger captions")
                used.append(r"bigger|larger|big|large|huge|too small|hard to read|can'?t read|tiny|read")
                took = True
            elif re.search(r"\b(?:smaller|small|too big|too large|less in the way)\b", low):
                cap["size_scale"] = round(cap.get("size_scale", 1.0) / SIZE_STEP, 3)
                plan.said.append("smaller captions")
                used.append(r"smaller|small|too big|too large|less in the way")
                took = True
            for preset in PRESETS:
                if re.search(rf"\b{preset}\b", low):
                    cap["preset"] = preset
                    plan.said.append(f"{preset} captions")
                    used.append(preset)
                    took = True
                    break
            if re.search(r"\b(?:higher|move(?:d)? up|further up|raise(?:d)?|too low|covers? (?:the )?(?:bottom|"
                         r"buttons?))\b", low):
                cap["move"] = round(cap.get("move", 0.0) - PLACE_STEP, 3)
                plan.said.append("captions higher up")
                used.append(r"higher|moved? up|further up|raised?|too low|covers?|bottom|buttons?")
                took = True
            elif re.search(r"\b(?:lower|move(?:d)? down|further down|too high|covers? (?:the )?(?:face|faces|"
                           r"action))\b", low):
                cap["move"] = round(cap.get("move", 0.0) + PLACE_STEP, 3)
                plan.said.append("captions lower down")
                used.append(r"lower|moved? down|further down|too high|covers?|faces?|action")
                took = True
            if re.search(r"\b(?:no|remove|drop|without|turn off|get rid of|lose)\b", low) and not took:
                cap["off"] = True
                plan.said.append("no captions")
                used.append(r"no|remove|drop|without|turn off|get rid|lose|off")
                took = True
        elif re.search(rf"\b{_MUSIC}\b", low):
            used.append(_MUSIC)
            music = plan.edits.setdefault("music", {})
            if re.search(r"\b(?:no|remove|drop|without|turn off|get rid of|lose|mute|cut)\b", low):
                music["off"] = True
                plan.said.append("no music")
                used.append(r"no|remove|drop|without|turn off|get rid|lose|mute|cut|off")
                took = True
            elif re.search(rf"\b{_QUIETER}\b", low):
                music["gain_db"] = music.get("gain_db", 0.0) - GAIN_STEP
                plan.said.append("quieter music")
                used.append(_QUIETER + r"|voice|narrator|narration|out|hear")
                took = True
            elif re.search(rf"\b{_LOUDER}\b", low):
                music["gain_db"] = music.get("gain_db", 0.0) + GAIN_STEP
                plan.said.append("louder music")
                used.append(_LOUDER)
                took = True
            if not music:
                plan.edits.pop("music", None)
        elif re.search(rf"\b{_VOICE}\b", low):
            asks = re.search(r"\b(?:change|switch|swap|use|different|another|new|make|give|try|want|get|with|to)\b"
                             r"|\b(?:an?|in an?)\s+[\w ]{0,30}voice\b", low)
            key = voice_named(low) if asks else ""
            level = re.search(rf"\b{_LOUDER}\b", low), re.search(rf"\b{_QUIETER}\b", low)
            if level[0] or level[1]:
                voice = plan.edits.setdefault("voice", {})
                voice["gain_db"] = voice.get("gain_db", 0.0) + (GAIN_STEP if level[0] else -GAIN_STEP)
                plan.said.append("a louder voice" if level[0] else "a quieter voice")
                used += [_VOICE, _LOUDER, _QUIETER]
                took = True
            elif key:
                plan.voice = key
                label = voice_engine.VOICES[key].label
                plan.said.append(f"the {label} voice")
                used += [_VOICE, r"british|male|female|woman|man|girl|deep(?:er)?|calm|warm|bright|upbeat|energetic|"
                                 r"lively|storyteller|documentary|whisper(?:ed|ing)?|hushed|narrator|different|"
                                 r"another|new|switch|swap|try|want|get"]
                took = True
        if not took or _leftover(clause, used):
            rest.append(clause)
    if cap:
        plan.edits["captions"] = cap
    plan.rest = ". ".join(rest)
    return plan


# ---------------------------------------------------------------- the edits, on a timeline


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def apply(tl: Timeline, edits: dict) -> list[str]:
    """Make `edits` (Plan.edits) on `tl`. Returns what changed, in words; an empty list means nothing in the
    timeline answered them (it has no captions to recolour, say)."""
    done: list[str] = []
    cap = edits.get("captions") if isinstance(edits.get("captions"), dict) else {}
    track = tl.track(SUBTITLES)
    if cap and track is not None and track.style is not None and track.events:
        style = track.style
        if cap.get("off"):
            track.muted = True
            done.append("the captions are off")
        else:
            relay = False
            if cap.get("color"):
                style.color = str(cap["color"])
                if style.highlight.upper() == style.color.upper():  # the spoken word still has to stand out
                    style.highlight = "#FFFFFF" if style.color.upper() != "#FFFFFF" else "#FFE600"
                done.append(f"the captions are {style.color}")
            if cap.get("highlight"):
                style.highlight = str(cap["highlight"])
                done.append(f"the spoken word is {style.highlight}")
            if cap.get("size_scale"):
                size = int(round(_clamp(style.size * float(cap["size_scale"]), *SIZE_LIMITS)))
                if size != style.size:
                    done.append(f"the captions are {'bigger' if size > style.size else 'smaller'} ({size} px)")
                    style.size, relay = size, True
            if cap.get("preset") in PRESETS and cap["preset"] != style.preset:
                style.preset, relay = cap["preset"], True
                done.append(f"the captions are {style.preset}")
            if cap.get("move"):
                at = style.position or 0.78  # 0 = the safe zone's own place, which is about there
                new = round(_clamp(at + float(cap["move"]), 0.2, 0.9), 3)
                if new != style.position:
                    style.position, relay = new, True
                    done.append("the captions sit " + ("higher" if float(cap["move"]) < 0 else "lower"))
            if relay:  # sizes and places are in the words' boxes: lay the same words out again
                marked = {w.text for ev in track.events for w in ev.words if w.emphasis}
                words = [Word(w.start, w.end, w.text) for ev in track.events for w in ev.words]
                new = kinetic_subtitles.build_track(words, tl.canvas, style, duration=tl.duration or tl.end,
                                                    emphasis=marked, track_id=SUBTITLES)
                track.events = new.events
            else:  # a colour: the words that had the old style's colour of their own keep theirs
                kinetic_subtitles.layout(track, tl.canvas)
    for role, track_id, name in (("music", AUDIO_MUSIC, "the music"), ("voice", AUDIO_VOICE, "the voice")):
        change = edits.get(role) if isinstance(edits.get(role), dict) else {}
        audio = tl.track(track_id)
        if not change or audio is None or not audio.clips:
            continue
        if change.get("off") and role == "music":
            audio.muted = True
            done.append("the music is off")
            continue
        by = float(change.get("gain_db") or 0.0)
        if by:
            audio.gain_db = round(_clamp(audio.gain_db + by, *GAIN_LIMITS), 2)
            done.append(f"{name} is {abs(by):g} dB {'louder' if by > 0 else 'quieter'}")
    return done


def can_retouch(folder: Path) -> bool:
    """Does the video in `folder` have a timeline to change (the Studio made it, and kept it)?"""
    return (Path(folder) / "timeline.json").is_file()


def timeline_of(folder: Path, video: str = "") -> Path:
    """The timeline `video` (a file name in `folder`) was rendered from: its own ("<name>.timeline.json", a
    version made by a retouch) when there is one, else the folder's timeline.json."""
    own = Path(folder) / f"{Path(video).stem}.timeline.json" if video else None
    return own if own is not None and own.is_file() else Path(folder) / "timeline.json"


def version_name(folder: Path, title: str, version: int) -> str:
    """'<title> (v2).mp4', or the next number nobody has used in `folder` yet."""
    from ..organize import safe_name

    stem = re.sub(r"\s*\((?:v\d+|edit)\)\s*$", "", safe_name(title or "video", 60)).strip() or "video"
    n = max(2, int(version or 2))
    while (Path(folder) / f"{stem} (v{n}).mp4").exists():
        n += 1
    return f"{stem} (v{n}).mp4"


def list_version(folder: Path, source: str, name: str, note: str = "") -> bool:
    """Add `name` to the folder's clips.json as an edit of `source` (a file name there): it carries the same
    caption, hashtags, category and AI-generated label when posting takes it in, and waits for an OK."""
    index = Path(folder) / "clips.json"
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    clips = data.get("clips") if isinstance(data, dict) else None
    if not isinstance(clips, list):
        return False
    rows = [c for c in clips if isinstance(c, dict) and c.get("file")]
    src = next((c for c in rows if c["file"] == source), None) or next(iter(rows), None)
    if src is None:
        return False
    if any(c["file"] == name for c in rows):
        return True
    m = re.search(r"\(v(\d+)\)", name)
    label = f"v{m.group(1)}" if m else "edit"
    base = re.sub(r"\s*\((?:v\d+|edit)\)\s*$", "", str(src.get("title") or Path(source).stem))
    row = {**src, "rank": max((int(c.get("rank") or 0) for c in rows), default=0) + 1, "file": name,
           "title": f"{base} ({label})", "edit_of": src["file"]}
    if note:
        row["edit_note"] = note
    clips.append(row)
    try:
        tmp = index.with_name(index.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, index)
    except OSError:
        return False
    return True


@dataclass
class Retouched:
    video: Path
    folder: Path
    timeline: Path
    changed: list[str]
    duration: float
    report: object = None


def render(folder: Path, source: str, name: str, edits: dict, *, settings=None, log=None, progress=None,
           check_cancel=None) -> Retouched:
    """Make `name` in `folder`: the video `source` there with `edits` made on its timeline. Raises
    CreatorError (in words for whoever asked) when there's no timeline, nothing in it answers the edits, or
    the render fails. Nothing is left behind by a render that doesn't finish."""
    from .orchestrator import _render_folder

    log = log or (lambda m: None)
    folder = Path(folder)
    path = timeline_of(folder, source)
    if not path.is_file():
        raise CreatorError("This video has no timeline beside it, so it can't be changed without making it again.")
    try:
        tl = Timeline.load(path)
    except (TimelineError, ValueError, OSError) as exc:
        raise CreatorError(f"{path.name} can't be used: {exc}") from exc
    changed = apply(tl, edits or {})
    if not changed:
        raise CreatorError("Nothing in this video's timeline answers that: it has no captions or music to change.")
    dest = folder / name
    own = folder / f"{dest.stem}.timeline.json"
    log("Changing the timeline: " + "; ".join(changed) + ".")
    with _render_folder(folder) as tmp:
        work = Path(tmp)
        part = work / ".edit.mp4"  # a name posting's intake passes over
        try:
            report = timeline_compiler.render(tl, part, base=folder, work=work, settings=settings,
                                              progress=progress, check_cancel=check_cancel, log=log)
        except (timeline_compiler.CompileError, TimelineError) as exc:
            raise CreatorError(str(exc)) from exc
        try:
            try:
                os.replace(part, dest)
            except OSError:  # another drive
                shutil.copyfile(part, dest)
            tl.dump(own)  # this version's own timeline: the next change starts from it
        except OSError as exc:
            dest.unlink(missing_ok=True)
            own.unlink(missing_ok=True)
            raise CreatorError(f"The new version was rendered but couldn't be saved as {dest.name} "
                               f"({exc.strerror or exc}).") from exc
        if not list_version(folder, source, name, "; ".join(changed)):
            dest.unlink(missing_ok=True)
            own.unlink(missing_ok=True)
            raise CreatorError("The new version was rendered, but the folder's clips.json couldn't take it, so "
                               "posting wouldn't label it. Nothing was changed.")
    report.path = dest
    return Retouched(dest, folder, own, changed, float(tl.duration or 0.0), report)
