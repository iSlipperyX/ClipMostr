"""Editing a sports video after it's made: what can be changed, and the change checked.

A recap reel is kept with its plan (reel.json beside it: every stretch of the game's footage it uses, in
order, with its words). A single play's clip is kept with where it was cut from (its row in clips.json). So
either can be cut again differently without anything being guessed: this module says what there is to change
(`describe`), checks what the phone sends back (`reel_edit`, `clip_edit`), and lists the new version beside the
first one (`add_version`). The cutting itself is the Sports worker's (desk.DeskWorker.do_edit), with the same
engine and the same reel maker the first version came from.

    describe(folder, file, game, s)      what the editor shows: the plays, the words, the look, the limits
    reel_edit(plan, edit, game, length)  -> ReelEdit: the reel to make, how, and what changed, in words
    clip_edit(row, look, edit, length)   -> ClipEdit: the cut to make, how, and what changed

A change the footage can't give (a play starting before the video does, a reel of nothing) is refused with
EditError, in words for whoever asked. Nothing here touches the video files.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import montage

PLAY_MIN, PLAY_MAX = 2.0, 60.0  # seconds one play may run in an edited reel
PLAYS_MAX = 12
REEL_LONGEST = 180.0  # seconds: an edited reel's most (three minutes; the first cut aims at 72 s)
CLIP_MIN, CLIP_MAX = 3.0, 180.0  # seconds a single clip may run
REACH = 20.0  # seconds past a play's own ends the editor's sliders reach (typed times may go further)
HOOK_MAX, WORDS_MAX, CAPTION_MAX, LABEL_MAX, BADGE_MAX, FINAL_MAX = 60, 320, 300, 24, 60, 60
TAGS_MAX = 8
MUSIC = {"off": "Off", "low": "Quiet", "normal": "Normal", "loud": "Loud"}
GAME_SOUND = {"quiet": -6.0, "normal": 0.0, "loud": 4.0}  # dB on the game's own sound
FRAMING = {"follow": "Follow the action", "fit": "Whole picture"}
STYLES = {"mix": "Mixed", "dissolve": "Dissolve", "whip": "Whip pan", "zoom": "Zoom"}
ASPECTS = {"vertical_blur": "Whole picture", "vertical_crop": "Fill the screen"}


class EditError(Exception):
    """Why an edit can't be made, in words for whoever asked."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


@dataclass
class ReelEdit:
    reel: montage.Reel
    voice: str  # the narrator's voice, or "off"
    framing: str
    look: dict  # reel.make's `look`: {"music", "game", "bug"}
    changed: list[str] = field(default_factory=list)


@dataclass
class ClipEdit:
    start: float
    end: float
    headline: str  # the words over the clip ("" = none)
    caption: str
    hashtags: list[str]
    look: dict  # {"captions": bool, "aspect": ..., "crop_x": 0-1}
    changed: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- the folder's files


def plan_file(folder: Path, name: str) -> Path:
    """Where the plan of the reel `name` (a file in `folder`) is kept: an edited version has its own
    ("<name>.reel.json"), the first one is the folder's reel.json."""
    return Path(folder) / f"{Path(name).stem}.reel.json"


def plan_of(folder: Path, name: str) -> dict | None:
    """The plan the reel `name` was made from, or None when it isn't beside it any more."""
    for path in (plan_file(folder, name), Path(folder) / "reel.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("segments"), list):
            return data
    return None


def index_of(folder: Path) -> dict:
    try:
        data = json.loads((Path(folder) / "clips.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def row_of(index: dict, name: str) -> dict | None:
    """The clips.json row of the file `name` (rows name files relative to the folder)."""
    for c in index.get("clips") or []:
        if isinstance(c, dict) and c.get("file") and Path(str(c["file"])).name == Path(name).name:
            return c
    return None


def kind_of(folder: Path, name: str) -> str:
    """'reel', 'clip' or '' (not a sports video this editor can cut again)."""
    index = index_of(folder)
    if not isinstance(index.get("sports"), dict):
        return ""
    row = row_of(index, name)
    if row is None:
        return ""
    if row.get("reel"):
        return "reel" if plan_of(folder, name) is not None else ""
    try:
        return "clip" if float(row["end"]) > float(row["start"]) else ""
    except (KeyError, TypeError, ValueError):
        return ""


def version_name(folder: Path, name: str, version: int) -> str:
    """'<name> (v2).mp4' for an edit of the file `name`: the next number nobody has used in `folder`."""
    stem = re.sub(r"\s*\(v\d+\)\s*$", "", Path(name).stem).strip() or "video"
    n = max(2, int(version or 2))
    while (Path(folder) / f"{stem} (v{n}).mp4").exists():
        n += 1
    return f"{stem} (v{n}).mp4"


def add_version(folder: Path, source: str, name: str, values: dict, note: str = "") -> bool:
    """List `name` in the folder's clips.json as an edit of `source` (a file there), with `values` over what
    the source's row says. Posting takes it in with the same category and the same way of being handed over
    (to download, or held for an OK beside the one it replaces). False when clips.json can't take it."""
    index_path = Path(folder) / "clips.json"
    data = index_of(folder)
    clips = data.get("clips")
    if not isinstance(clips, list):
        return False
    src = row_of(data, source)
    if src is None:
        return False
    if row_of(data, name) is not None:
        return True
    m = re.search(r"\(v(\d+)\)", name)
    base = re.sub(r"\s*\((?:v\d+|edit)\)\s*$", "", str(src.get("title") or Path(source).stem))
    row = {**src, **values, "rank": max((int(c.get("rank") or 0) for c in clips if isinstance(c, dict)), default=0) + 1,
           "file": name, "edit_of": src["file"]}
    row["title"] = str(values.get("title") or base) + (f" (v{m.group(1)})" if m else " (edit)")
    if note:
        row["edit_note"] = note
    clips.append(row)
    try:
        tmp = index_path.with_name(index_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, index_path)
    except OSError:
        return False
    return True


def source_file(s, video_id: str) -> Path | None:
    """The game's footage on the PC (the download the clips were cut from), when it's still in the cache and a
    phone can play it."""
    vid = re.sub(r"[^\w-]", "", str(video_id or ""))
    if not vid:
        return None
    path = Path(s.cache_path) / f"{vid}.mp4"
    return path if path.is_file() else None


# ---------------------------------------------------------------- what the editor shows


def _text(v, most: int) -> str:
    return " ".join(str(v or "").split())[:most].strip()


def _tags(v) -> list[str]:
    """Hashtags out of whatever was typed: "#nba lakers, #dunk" → ["nba", "lakers", "dunk"]."""
    words = v if isinstance(v, list) else re.split(r"[\s,]+", str(v or ""))
    out: list[str] = []
    for w in words:
        t = re.sub(r"[^\w]", "", str(w), flags=re.UNICODE)[:40]
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:TAGS_MAX]


def _num(v, default: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if x == x and abs(x) != float("inf") else default


def _voices() -> list[dict]:
    from ..creator.voice_engine import VOICES

    return [{"id": "off", "label": "No narrator"}] + [{"id": v.key, "label": v.label} for v in VOICES.values()]


def _game_sound(db: float) -> str:
    return min(GAME_SOUND, key=lambda k: abs(GAME_SOUND[k] - db))


def describe(folder: Path, name: str, game: dict | None, s=None, look: dict | None = None,
             duration: float = 0.0) -> dict:
    """What the editor opens for the sports video `name` in `folder`. `game` is its game on the Sports page
    (None when it's gone from there: the video can't be cut again then, and "why" says so). `look` is how a
    single clip was made (the category's recipe: captions, the picture's shape). `duration` is the length of
    the game's footage, when known. Raises EditError when it isn't a sports video with its plan beside it."""
    folder = Path(folder)
    index = index_of(folder)
    row = row_of(index, name)
    kind = kind_of(folder, name)
    if row is None or not kind:
        raise EditError("Only a sports clip or reel made by ClipMonster, with its plan still beside it on the "
                        "PC, can be edited here.", 409)
    sport = index.get("sports") or {}
    duration = float(duration or (game or {}).get("duration") or (index.get("video") or {}).get("duration") or 0)
    out = {"kind": kind, "file": Path(name).name, "title": _base_title(str(row.get("title") or Path(name).stem)),
           "length": round(_num(row.get("length"), 0.0), 2),
           "game": {"id": (game or {}).get("id") or "", "matchup": sport.get("matchup") or (game or {}).get("matchup")
                    or "", "duration": round(duration, 2), "source": False},
           "why": "" if game else "This game isn't on the PC's Sports page any more, so its footage can't be cut "
                                  "again. Ask for the game again to edit its videos.",
           "limits": {"reach": REACH, "caption": CAPTION_MAX, "tags": TAGS_MAX}}
    if kind == "reel":
        plan = plan_of(folder, name) or {}
        reel = montage.Reel.from_json(plan)
        made = plan.get("made") if isinstance(plan.get("made"), dict) else {}
        used = {seg.hid for seg in reel.plays}
        out["opening"] = {"on": bool(reel.segments and reel.segments[0].kind == "teaser"),
                          "hook": reel.story.hook, "words": reel.story.opening,
                          "voice": str(made.get("voice") or ("en_storyteller" if reel.story.opening else "off"))}
        out["plays"] = [{"id": seg.hid, "start": round(seg.start, 2), "end": round(seg.end, 2),
                         "moment": round(seg.moment, 2), "label": seg.label,
                         "badge": reel.story.badges.get(seg.hid) or seg.badge, "title": seg.title or seg.play,
                         "rank": seg.rank} for seg in reel.plays]
        out["more"] = [{"id": h["id"], "start": round(float(h["start"]), 2), "end": round(float(h["end"]), 2),
                        "title": _text(h.get("title") or h.get("play") or h.get("label") or "A play", 80),
                        "rank": h.get("rank") or 0, "found": h.get("origin") == "event"}
                       for h in sorted((game or {}).get("highlights") or [], key=lambda h: h.get("rank") or 99)
                       if h.get("id") not in used and _num(h.get("end"), 0) - _num(h.get("start"), 0) >= PLAY_MIN]
        out["words"] = {"caption": reel.story.caption, "hashtags": " ".join(f"#{t}" for t in reel.story.hashtags),
                        "final": reel.final}
        out["look"] = {"framing": str(made.get("framing") or "follow"), "style": reel.style,
                       "music": str(made.get("music") or "normal"),
                       "game": _game_sound(_num(made.get("game"), 0.0)), "bug": made.get("bug", True) is not False}
        out["choices"] = {"voices": _voices(), "framing": _options(FRAMING), "styles": _options(STYLES),
                          "music": _options(MUSIC), "game": _options({"quiet": "Quiet", "normal": "Normal",
                                                                      "loud": "Loud"})}
        out["limits"] |= {"play_min": PLAY_MIN, "play_max": PLAY_MAX, "plays": PLAYS_MAX, "longest": REEL_LONGEST,
                          "hook": HOOK_MAX, "words": WORDS_MAX, "label": LABEL_MAX, "badge": BADGE_MAX,
                          "final": FINAL_MAX, "transition": reel.transition, "teaser": montage.TEASER}
    else:
        look = dict(look or {}) | (row.get("edit") if isinstance(row.get("edit"), dict) else {})
        out["cut"] = {"start": round(_num(row.get("start"), 0.0), 2), "end": round(_num(row.get("end"), 0.0), 2)}
        out["words"] = {"headline": _text(look.get("headline") if "headline" in look else "", HOOK_MAX),
                        "caption": _text(row.get("caption") or row.get("hook") or "", CAPTION_MAX),
                        "hashtags": " ".join(f"#{str(h).lstrip('#')}" for h in row.get("hashtags") or [])}
        aspect = str(look.get("aspect") or "vertical_blur")
        out["look"] = {"captions": bool(look.get("captions")), "aspect": aspect if aspect in ASPECTS else
                       "vertical_blur", "crop_x": round(max(0.0, min(1.0, _num(look.get("crop_x"), 0.5))), 2)}
        out["choices"] = {"aspects": _options(ASPECTS)}
        out["limits"] |= {"clip_min": CLIP_MIN, "clip_max": CLIP_MAX, "headline": HOOK_MAX}
    return out


def _options(d: dict) -> list[dict]:
    return [{"id": k, "label": v} for k, v in d.items()]


def _base_title(title: str) -> str:
    return re.sub(r"\s*\((?:v\d+|edit)\)\s*$", "", title).strip()


# ---------------------------------------------------------------- a reel, cut again


def _span(start, end, was: tuple[float, float], duration: float, lo: float, hi: float, what: str) -> tuple[float, float]:
    a, b = round(_num(start, was[0]), 2), round(_num(end, was[1]), 2)
    if a < 0:
        a = 0.0
    if duration and b > duration:
        b = round(duration, 2)
    if b - a < lo - 0.005:
        raise EditError(f"{what} would be {max(0.0, b - a):.1f} s: the least is {lo:g} s.")
    if b - a > hi + 0.005:
        raise EditError(f"{what} would be {b - a:.0f} s: the most is {hi:g} s.")
    return a, b


def _secs(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") + " s"


def reel_edit(plan: dict, edit: dict, game: dict | None = None, duration: float = 0.0) -> ReelEdit:
    """The reel `plan` (a reel.json) with `edit` made on it. `edit` is what the editor sends:

        {"plays": [{"id", "start", "end", "label", "badge"}, ...]   the plays, in the order they should run
         "opening": {"on", "hook", "words", "voice"}                the teaser, its banner, the narrator
         "words": {"caption", "hashtags", "final"}
         "look": {"framing", "style", "music", "game", "bug"}}

    A part that's left out stays as it is. `game` gives the plays that can be added (its highlights and found
    moments); `duration` is the footage's length (0 = not known: only lengths are checked). Raises EditError."""
    if not isinstance(edit, dict):
        raise EditError("That edit couldn't be read.")
    was = montage.Reel.from_json(plan)
    reel = copy.deepcopy(was)
    made = plan.get("made") if isinstance(plan.get("made"), dict) else {}
    changed: list[str] = []
    old = {seg.hid: seg for seg in was.plays}
    spare = {str(h.get("id")): h for h in (game or {}).get("highlights") or [] if h.get("id")}

    # --- the plays, in their new order
    plays: list[montage.Segment] = list(reel.plays)
    if "plays" in edit:
        rows = edit["plays"]
        if not isinstance(rows, list) or not rows:
            raise EditError("A reel needs at least one play. Delete the reel instead if it isn't wanted.")
        if len(rows) > PLAYS_MAX:
            raise EditError(f"A reel takes {PLAYS_MAX} plays at most.")
        plays, seen, trimmed, renamed, added = [], set(), 0, 0, 0
        for n, r in enumerate(rows, 1):
            hid = str((r or {}).get("id") or "") if isinstance(r, dict) else ""
            if hid in seen:
                raise EditError("The same play is in the reel twice. Take one of them out.")
            seen.add(hid)
            if hid in old:
                seg = copy.deepcopy(old[hid])
            elif hid in spare:
                h = spare[hid]
                a, b = float(h["start"]), float(h["end"])
                if b - a > 24.0:  # a long found moment: its middle, as the first cut would take it
                    a, b = montage.cut_around(montage.moment_of(h, None), 24.0, a, b, [])
                seg = montage.Segment("play", hid, round(a, 3), round(b, 3), round(montage.moment_of(h, None), 3),
                                      int(h.get("rank") or 0), "", str(h.get("play") or h.get("label") or ""),
                                      str(h.get("title") or ""), [str(x) for x in h.get("players") or []][:3])
                added += 1
            else:
                raise EditError("One of those plays isn't in this game any more. Open the editor again.", 409)
            a, b = _span(r.get("start"), r.get("end"), (seg.start, seg.end), duration, PLAY_MIN, PLAY_MAX,
                         f"Play {n}")
            if hid in old and (abs(a - seg.start) > 0.04 or abs(b - seg.end) > 0.04):
                trimmed += 1
            seg.start, seg.end = a, b
            if not (a + 0.2 <= seg.moment <= b - 0.2):  # the moment the crowd lifts around stays inside the cut
                seg.moment = round(a + montage.SETUP_SHARE * (b - a), 3)
            if "label" in r:
                label = _text(r.get("label"), LABEL_MAX).upper()
                renamed += label != seg.label
                seg.label = label
            if "badge" in r:
                badge = _text(r.get("badge"), BADGE_MAX).upper()
                renamed += badge != (was.story.badges.get(hid) or seg.badge or "")
                seg.badge = badge
                reel.story.badges[hid] = badge
            plays.append(seg)
        gone = [h for h in old if h not in seen]
        for h in gone:
            reel.story.badges.pop(h, None)
        kept_old = [seg.hid for seg in plays if seg.hid in old]
        if gone:
            changed.append(f"took out {len(gone)} play{'s' if len(gone) != 1 else ''}")
        if added:
            changed.append(f"added {added} play{'s' if added != 1 else ''}")
        if kept_old != [h for h in old if h in seen]:
            changed.append("put the plays in a new order")
        if trimmed:
            changed.append(f"trimmed {trimmed} play{'s' if trimmed != 1 else ''}")
        if renamed:
            changed.append("changed the words on the plays")
        if gone or added or kept_old != [h for h in old if h in seen]:
            reel.order = "custom"

    # --- the opening: the teaser, its banner, the narrator
    opening = edit.get("opening") if isinstance(edit.get("opening"), dict) else {}
    had_teaser = bool(was.segments and was.segments[0].kind == "teaser")
    want_teaser = bool(opening["on"]) if "on" in opening else had_teaser
    voice_was = str(made.get("voice") or ("en_storyteller" if was.story.opening else "off"))
    voice = voice_was
    if "voice" in opening:
        from ..creator.voice_engine import VOICES

        voice = str(opening.get("voice") or "off")
        if voice != "off" and voice not in VOICES:
            raise EditError("That narrator's voice isn't one ClipMonster has.")
    if "hook" in opening:
        hook = _text(opening.get("hook"), HOOK_MAX)
        if hook != was.story.hook:
            changed.append("changed the opening's headline" if hook else "took the opening's headline off")
        reel.story.hook = hook
    if "words" in opening:
        words = _text(opening.get("words"), WORDS_MAX)
        if words != was.story.opening and voice != "off":
            changed.append("changed what the narrator says")
        reel.story.opening = words
    if voice != "off" and not reel.story.opening:
        voice = "off"  # a narrator with nothing to say
    if voice != voice_was:
        changed.append("turned the narrator off" if voice == "off" else "turned the narrator on" if
                       voice_was == "off" else "changed the narrator's voice")
    teaser = None
    if want_teaser and len(plays) > 1:
        first = was.segments[0] if had_teaser else None
        best = next((p for p in plays if first is not None and p.hid == first.hid), plays[-1])
        stop = round(best.moment - montage.TEASER_STOP, 3)
        start = round(max(0.0, stop - montage.TEASER), 3)
        if first is not None and best.hid == first.hid and abs(first.end - stop) < 0.5:
            start, stop = first.start, first.end  # the same opening as before
        if stop - start >= 1.5:
            teaser = montage.Segment("teaser", best.hid, start, stop, best.moment, best.rank, play=best.play,
                                     title=best.title)
    if bool(teaser) != had_teaser:
        changed.append("added an opening teaser" if teaser else "took the opening teaser off")
    reel.segments = ([teaser] if teaser else []) + plays

    # --- the words that go with it
    words = edit.get("words") if isinstance(edit.get("words"), dict) else {}
    if "caption" in words:
        caption = _text(words.get("caption"), CAPTION_MAX)
        if caption != was.story.caption:
            changed.append("changed the caption")
        reel.story.caption = caption
    if "hashtags" in words:
        tags = _tags(words.get("hashtags"))
        if tags != list(was.story.hashtags):
            changed.append("changed the hashtags")
        reel.story.hashtags = tags
    if "final" in words:
        final = _text(words.get("final"), FINAL_MAX)
        if final != was.final:
            changed.append("changed the final score line" if final else "took the final score off")
        reel.final = final

    # --- how it looks and sounds
    look_in = edit.get("look") if isinstance(edit.get("look"), dict) else {}
    framing_was = str(made.get("framing") or "follow")
    framing = str(look_in.get("framing") or framing_was)
    if framing not in FRAMING:
        raise EditError("Pick how the picture is framed: following the action, or the whole picture.")
    if framing != framing_was:
        changed.append("framed it as the whole picture" if framing == "fit" else "framed it to follow the action")
    style = str(look_in.get("style") or was.style)
    if style not in STYLES:
        raise EditError("That transition isn't one ClipMonster has.")
    if style != was.style:
        changed.append(f"set the transitions to {STYLES[style].lower()}")
    reel.style = style
    music_was = str(made.get("music") or "normal")
    music = str(look_in.get("music") or music_was)
    if music not in MUSIC:
        raise EditError("Pick the music's level: off, quiet, normal or loud.")
    if music != music_was:
        changed.append("turned the music off" if music == "off" else f"set the music to {MUSIC[music].lower()}")
    game_was = _game_sound(_num(made.get("game"), 0.0))
    game_sound = str(look_in.get("game") or game_was)
    if game_sound not in GAME_SOUND:
        raise EditError("Pick the game sound's level: quiet, normal or loud.")
    if game_sound != game_was:
        changed.append(f"set the game's sound to {game_sound}")
    bug_was = made.get("bug", True) is not False
    bug = bool(look_in["bug"]) if "bug" in look_in else bug_was
    if bug != bug_was:
        changed.append("turned the score bug on" if bug else "turned the score bug off")

    if not reel.plays:
        raise EditError("A reel needs at least one play.")
    total = reel.duration
    if total > REEL_LONGEST + 0.05:
        raise EditError(f"That comes to {total:.0f} s: an edited reel runs {REEL_LONGEST:g} s at most. Trim a "
                        "play or take one out.")
    if total < 5.0:
        raise EditError("That comes to under 5 seconds. Make a play longer, or add one.")
    if not changed:
        raise EditError("Nothing was changed, so there's no new version to make.")
    return ReelEdit(reel, voice, framing, {"music": music, "game": GAME_SOUND[game_sound], "bug": bug}, changed)


# ---------------------------------------------------------------- a single clip, cut again


def clip_edit(row: dict, look: dict | None, edit: dict, duration: float = 0.0) -> ClipEdit:
    """A single play's clip (`row`, its clips.json row; `look`, how it was made) with `edit` made on it:

        {"cut": {"start", "end"}, "words": {"headline", "caption", "hashtags"},
         "look": {"captions", "aspect", "crop_x"}}

    Raises EditError."""
    if not isinstance(edit, dict):
        raise EditError("That edit couldn't be read.")
    look = dict(look or {}) | (row.get("edit") if isinstance(row.get("edit"), dict) else {})
    changed: list[str] = []
    a0, b0 = _num(row.get("start"), 0.0), _num(row.get("end"), 0.0)
    cut = edit.get("cut") if isinstance(edit.get("cut"), dict) else {}
    a, b = _span(cut.get("start"), cut.get("end"), (a0, b0), duration, CLIP_MIN, CLIP_MAX, "The clip")
    if abs(a - a0) > 0.04 or abs(b - b0) > 0.04:
        changed.append(f"cut it from {_secs(b0 - a0)} to {_secs(b - a)}" if abs((b - a) - (b0 - a0)) > 0.04
                       else "moved where it's cut from")
    words = edit.get("words") if isinstance(edit.get("words"), dict) else {}
    head_was = _text(look.get("headline") if "headline" in look else "", HOOK_MAX)
    headline = _text(words.get("headline"), HOOK_MAX) if "headline" in words else head_was
    if headline != head_was:
        changed.append("changed the headline" if headline and head_was else "added a headline" if headline
                       else "took the headline off")
    cap_was = _text(row.get("caption") or row.get("hook") or "", CAPTION_MAX)
    caption = _text(words.get("caption"), CAPTION_MAX) if "caption" in words else cap_was
    if caption != cap_was:
        changed.append("changed the caption")
    tags_was = _tags(row.get("hashtags") or [])
    tags = _tags(words.get("hashtags")) if "hashtags" in words else tags_was
    if tags != tags_was:
        changed.append("changed the hashtags")
    lk = edit.get("look") if isinstance(edit.get("look"), dict) else {}
    caps_was = bool(look.get("captions"))
    captions = bool(lk["captions"]) if "captions" in lk else caps_was
    if captions != caps_was:
        changed.append("turned the captions on" if captions else "turned the captions off")
    aspect_was = str(look.get("aspect") or "vertical_blur")
    aspect_was = aspect_was if aspect_was in ASPECTS else "vertical_blur"
    aspect = str(lk.get("aspect") or aspect_was)
    if aspect not in ASPECTS:
        raise EditError("Pick the picture's shape: the whole picture, or filling the screen.")
    if aspect != aspect_was:
        changed.append("made it fill the screen" if aspect == "vertical_crop" else "showed the whole picture")
    crop_was = max(0.0, min(1.0, _num(look.get("crop_x"), 0.5)))
    crop = max(0.0, min(1.0, _num(lk.get("crop_x"), crop_was))) if "crop_x" in lk else crop_was
    if aspect == "vertical_crop" and abs(crop - crop_was) > 0.02 and aspect == aspect_was:
        changed.append("moved the crop")
    if not changed:
        raise EditError("Nothing was changed, so there's no new version to make.")
    return ClipEdit(a, b, headline, caption, tags,
                    {"captions": captions, "aspect": aspect, "crop_x": round(crop, 3), "headline": headline}, changed)


def said(changed: list[str]) -> str:
    """'Trimmed 2 plays; turned the music off' for the activity list and the version's note."""
    text = "; ".join(changed)
    return text[:1].upper() + text[1:]
