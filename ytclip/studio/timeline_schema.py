"""The timeline: one JSON document that says everything about a video, made or cut.

A video is a canvas, some assets and tracks of clips laid out in time. The Studio writes one for every video
it makes (timeline.json, beside the video), editor/timeline_compiler.py turns one into a single ffmpeg run,
and the timeline view draws one. Edit the JSON and render it again, and you get the edited video.

    {
      "version": 1,
      "title": "3 stoic habits",
      "canvas": {"width": 1080, "height": 1920, "fps": 30, "background": "#000000"},
      "duration": 64.2,
      "assets": {"pic1": {"kind": "image", "path": "scenes/scene_01.png"},
                 "voice": {"kind": "audio", "path": "assets/voice.flac", "duration": 63.6}},
      "tracks": [
        {"id": "video_track_0", "kind": "video", "clips": [
            {"id": "s1", "asset": "pic1", "start": 0, "duration": 4.1,
             "keyframes": [{"t": 0, "scale": 1.0}, {"t": 4.1, "scale": 1.15}],
             "transition_in": {"type": "crossfade", "duration": 0.3}}]},
        {"id": "video_track_1", "kind": "video", "clips": []},
        {"id": "audio_track_voice", "kind": "audio", "role": "voice", "clips": [...]},
        {"id": "audio_track_music", "kind": "audio", "role": "music", "clips": [...]},
        {"id": "audio_track_sfx", "kind": "audio", "role": "sfx", "clips": [...]},
        {"id": "subtitle_track", "kind": "subtitle", "style": {...}, "events": [
            {"start": 0.12, "end": 1.0, "words": [
                {"text": "MONEY", "start": 0.12, "end": 0.5, "color": "#00FF66", "emoji": "💰",
                 "box": [402, 1090, 276, 96]}]}]}
      ],
      "markers": [{"t": 0.0, "kind": "hook", "label": "The hook"}],
      "audio": {"target_lufs": -14, "true_peak": -1, "duck_db": -16}
    }

**Tracks.** Video tracks are layers: `video_track_0` is the picture (backgrounds, B-roll, the source being cut),
`video_track_1` and up sit over it (overlays, picture-in-picture). Audio tracks carry a role, which decides
their bus in the mix: `voice` is levelled and compressed, `music` is ducked under the voice, `sfx` plays at its
own level. The subtitle track holds caption groups of words, each word with its own time.

**Time** is in seconds from the start of the video. A clip plays `duration` seconds from `start`; a video or
audio asset is read from `source_in`. Clips on a video track never overlap (sounds on an audio track may: they
are simply mixed). On `video_track_0` the picture
changes at each clip's `start`: with a `transition_in` both clips are stretched half a transition past the
cut, so the cut stays where it is (and the voice stays in time).

**Transforms.** A clip sits where its keyframes put it. `x` and `y` place the clip's centre on the canvas
(0-1 across and down, 0.5 = the middle); `scale` is its size against its fit (1 = just covering the canvas with
fit "cover", just inside it with "contain"); `rotation` is in degrees, clockwise; `opacity` 0-1. Between two
keyframes values move by the later one's `ease`. `t` counts from the clip's start. `crop` cuts the source
first (0-1 of its width and height).

Everything is checked by validate(): Timeline.from_json raises TimelineError listing every problem with where
it is ("tracks[0].clips[2].asset: there's no asset called pic9"), so a hand-edited file says what's wrong.
JSON_SCHEMA is the same shape as a JSON Schema document, for editors and other tools.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

VERSION = 1

VIDEO_BASE = "video_track_0"
VIDEO_OVERLAY = "video_track_1"
AUDIO_VOICE = "audio_track_voice"
AUDIO_MUSIC = "audio_track_music"
AUDIO_SFX = "audio_track_sfx"
SUBTITLES = "subtitle_track"
STANDARD_TRACKS = (VIDEO_BASE, VIDEO_OVERLAY, AUDIO_VOICE, AUDIO_MUSIC, AUDIO_SFX, SUBTITLES)

TRACK_KINDS = ("video", "audio", "subtitle")
ASSET_KINDS = ("image", "video", "audio", "color")
AUDIO_ROLES = ("voice", "music", "sfx")
FITS = ("cover", "contain")
EASES = ("linear", "ease_in", "ease_out", "ease_in_out", "hold")
# How one picture gives way to the next. "cut" is instant; the others take the transition's duration.
TRANSITIONS = ("cut", "crossfade", "dissolve", "dip_black", "dip_white", "whip_left", "whip_right", "whip_up",
               "whip_down", "zoom_in", "circle_open", "smooth_up")
CAPTION_PRESETS = ("pop", "karaoke", "bounce", "plain")
MARKER_KINDS = ("hook", "retention", "payoff", "cta", "chapter", "note")
LOOKS = ("vignette", "grain")

MAX_DURATION = 3 * 3600.0
MAX_SIDE = 4096
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class TimelineError(ValueError):
    """A timeline that can't be used. `problems` lists everything wrong with it."""

    def __init__(self, problems: list["Problem"]):
        self.problems = problems
        errors = [p for p in problems if p.level == "error"]
        head = f"The timeline has {len(errors)} problem{'s' if len(errors) != 1 else ''}"
        super().__init__(head + ": " + "; ".join(str(p) for p in errors[:8]) +
                         (f"; and {len(errors) - 8} more" if len(errors) > 8 else ""))


@dataclass(frozen=True)
class Problem:
    level: str  # error | warning
    where: str  # "tracks[0].clips[2].asset"
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}" if self.where else self.message


# ---------------------------------------------------------------- the parts

@dataclass
class Canvas:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    background: str = "#000000"

    @classmethod
    def named(cls, name: str, fps: int = 30) -> "Canvas":
        """vertical (9:16, 1080x1920), landscape (16:9, 1920x1080) or square (1080x1080)."""
        key = str(name or "vertical").strip().lower()
        sizes = {"vertical": (1080, 1920), "9:16": (1080, 1920), "portrait": (1080, 1920), "short": (1080, 1920),
                 "landscape": (1920, 1080), "16:9": (1920, 1080), "wide": (1920, 1080), "long": (1920, 1080),
                 "square": (1080, 1080), "1:1": (1080, 1080)}
        if key not in sizes:
            raise ValueError(f"There's no format called {name!r}: use vertical, landscape or square.")
        return cls(*sizes[key], fps)

    @property
    def vertical(self) -> bool:
        return self.height > self.width

    @property
    def aspect(self) -> str:
        g = math.gcd(self.width, self.height) or 1
        return f"{self.width // g}:{self.height // g}"


@dataclass
class Asset:
    """A file (or a flat colour) the timeline uses. `path` may be relative to the timeline's own folder."""

    kind: str  # image | video | audio | color
    path: str = ""
    duration: float = 0.0  # of a video or audio file, when known (0 = not measured)
    width: int = 0
    height: int = 0
    color: str = ""  # kind "color": #RRGGBB
    # Where it came from, kept for the credits and for you: "pexels", "pixabay", "your B-roll", "SDXL-Turbo"...
    source: str = ""
    credit: str = ""  # who to credit ("Video by Jane Doe on Pexels")
    url: str = ""  # its page, when it came from the web
    licence: str = ""
    prompt: str = ""  # what it was made or searched from


@dataclass
class Keyframe:
    t: float = 0.0  # seconds from the clip's start
    x: float = 0.5  # the clip's centre on the canvas, 0-1 across
    y: float = 0.5
    scale: float = 1.0
    rotation: float = 0.0  # degrees, clockwise
    opacity: float = 1.0
    ease: str = "linear"  # how the values arrive at this keyframe from the one before


@dataclass
class Crop:
    x: float = 0.0  # 0-1 of the source's width
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0

    @property
    def whole(self) -> bool:
        return self.x <= 0 and self.y <= 0 and self.w >= 1 and self.h >= 1


@dataclass
class Transition:
    type: str = "cut"
    duration: float = 0.0

    @property
    def cut(self) -> bool:
        return self.type == "cut" or self.duration <= 0


@dataclass
class VideoClip:
    id: str
    asset: str
    start: float
    duration: float
    source_in: float = 0.0  # where in a video asset it starts
    fit: str = "cover"
    crop: Crop | None = None
    keyframes: list[Keyframe] = field(default_factory=list)
    transition_in: Transition = field(default_factory=Transition)
    transition_out: Transition = field(default_factory=Transition)  # the last clip's fade, or an overlay's
    loop: bool = False  # a video shorter than the clip plays again (else its last frame holds)
    audio: str = ""  # a video's own sound goes to this bus: "" (muted) | voice | music | sfx
    gain_db: float = 0.0
    label: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class AudioClip:
    id: str
    asset: str
    start: float
    duration: float
    source_in: float = 0.0
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    loop: bool = False
    label: str = ""

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class WordEvent:
    text: str
    start: float
    end: float
    color: str = ""  # overrides the style's colours for this word
    emoji: str = ""  # an emoji or icon shown with the word
    emphasis: bool = False
    box: list[float] | None = None  # [x, y, width, height] on the canvas, as it's drawn (filled in by layout)


@dataclass
class SubtitleEvent:
    """A caption group: the few words on screen together."""

    start: float
    end: float
    words: list[WordEvent] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class SubtitleStyle:
    preset: str = "pop"  # pop | karaoke | bounce | plain
    font: str = "Montserrat Black"
    size: int = 84  # letter height in pixels on a 1080-wide canvas
    color: str = "#FFFFFF"
    highlight: str = "#FFE600"  # the word being spoken
    emphasis: str = "#4FD8FF"  # words the script marked
    outline_color: str = "#000000"
    outline: int = 7
    uppercase: bool = True
    max_words: int = 3  # words on screen together (1-3 for kinetic captions)
    position: float = 0.0  # where the block's bottom sits, 0-1 down the canvas (0 = the safe zone's default)
    keyword_colors: bool = True
    money: str = "#00FF66"
    danger: str = "#FF3B30"
    question: str = "#FFE600"
    emoji: bool = True  # draw the words' emoji over the captions
    censor: bool = True  # mask strong swearing (f*ck)


@dataclass
class Marker:
    t: float
    kind: str = "note"
    label: str = ""


@dataclass
class AudioMaster:
    target_lufs: float = -14.0
    true_peak: float = -1.0
    duck_db: float = -16.0  # how far music drops under speech (0 = no ducking)
    duck_attack_ms: float = 50.0
    duck_release_ms: float = 300.0
    voice_compress: bool = True  # high-pass and soft-knee compression on the voice bus
    voice_lufs: float = -14.0  # the voice bus's own level before the mix


@dataclass
class Track:
    id: str
    kind: str
    clips: list = field(default_factory=list)  # VideoClip or AudioClip
    events: list[SubtitleEvent] = field(default_factory=list)  # subtitle tracks
    role: str = ""  # audio tracks: voice | music | sfx
    gain_db: float = 0.0
    muted: bool = False
    style: SubtitleStyle | None = None
    label: str = ""

    @property
    def items(self) -> list:
        return self.events if self.kind == "subtitle" else self.clips

    @property
    def end(self) -> float:
        return max((i.end for i in self.items), default=0.0)


@dataclass
class Timeline:
    title: str = ""
    canvas: Canvas = field(default_factory=Canvas)
    duration: float = 0.0
    assets: dict[str, Asset] = field(default_factory=dict)
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    audio: AudioMaster = field(default_factory=AudioMaster)
    look: list[str] = field(default_factory=list)  # finishing touches over the whole picture: vignette, grain
    meta: dict = field(default_factory=dict)  # anything else worth keeping (the prompt, the source page...)
    version: int = VERSION

    # ------------------------------------------------------------ finding things
    def track(self, track_id: str) -> Track | None:
        return next((t for t in self.tracks if t.id == track_id), None)

    def ensure_track(self, track_id: str, kind: str = "", role: str = "") -> Track:
        """The track called `track_id`, added (as its standard kind) when it isn't there yet."""
        found = self.track(track_id)
        if found is not None:
            return found
        kind = kind or ("video" if track_id.startswith("video") else "subtitle" if track_id.startswith("subtitle")
                        else "audio")
        if kind == "audio" and not role:
            role = next((r for r in AUDIO_ROLES if track_id.endswith(r)), "sfx")
        track = Track(track_id, kind, role=role, style=SubtitleStyle() if kind == "subtitle" else None)
        self.tracks.append(track)
        return track

    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "video" and not t.muted]

    def audio_tracks(self, role: str = "") -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio" and not t.muted and (not role or t.role == role)]

    def subtitle_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "subtitle" and not t.muted]

    @property
    def end(self) -> float:
        """Where the last clip or caption ends."""
        return max((t.end for t in self.tracks), default=0.0)

    def add_asset(self, asset: Asset, hint: str = "asset") -> str:
        """Keep `asset` and return its id. The same file or colour gets the same id."""
        for key, have in self.assets.items():
            if have.kind == asset.kind and have.path == asset.path and have.color == asset.color:
                return key
        stem = re.sub(r"[^A-Za-z0-9_.\-]+", "_", hint).strip("_.")[:40] or "asset"
        key, n = stem, 2
        while key in self.assets:
            key, n = f"{stem}_{n}", n + 1
        self.assets[key] = asset
        return key

    # ------------------------------------------------------------ JSON
    def to_json(self) -> dict:
        return _plain(self)

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False)

    def dump(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(self.dumps() + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_json(cls, data, *, strict: bool = True) -> "Timeline":
        """A Timeline from parsed JSON. With `strict`, anything wrong raises TimelineError (every problem at
        once); without, what can be read is kept and validate() tells you the rest."""
        problems: list[Problem] = []
        timeline = _Reader(problems).timeline(data)
        if strict:
            problems += [p for p in validate(timeline) if p.level == "error"]
            if any(p.level == "error" for p in problems):
                raise TimelineError(problems)
        return timeline

    @classmethod
    def loads(cls, text: str, *, strict: bool = True) -> "Timeline":
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise TimelineError([Problem("error", "", f"it isn't valid JSON ({exc})")]) from exc
        return cls.from_json(data, strict=strict)

    @classmethod
    def load(cls, path: str | Path, *, strict: bool = True) -> "Timeline":
        return cls.loads(Path(path).read_text(encoding="utf-8"), strict=strict)

    def resolve(self, asset: Asset | str, base: str | Path | None = None) -> Path:
        """An asset's file. A relative path is looked for in `base` (the timeline file's folder)."""
        a = self.assets[asset] if isinstance(asset, str) else asset
        path = Path(a.path).expanduser()
        return path if path.is_absolute() or base is None else Path(base) / path


def _plain(obj):
    """Dataclasses to JSON, leaving out what's at its default (so a timeline file stays readable)."""
    if isinstance(obj, (Timeline, Canvas, Keyframe)):  # always whole (a keyframe's values never default)
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    if hasattr(obj, "__dataclass_fields__"):
        out = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            if value == _default(f) and f.name not in _ALWAYS.get(type(obj).__name__, ()):
                continue
            out[f.name] = _plain(value)
        return out
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 4)
    return obj


_ALWAYS = {"Track": ("id", "kind"), "Transition": ("type", "duration"), "Asset": ("kind",),
           "WordEvent": ("text", "start", "end"), "SubtitleEvent": ("start", "end", "words"), "Marker": ("t", "kind"),
           "Crop": ("x", "y", "w", "h"), "SubtitleStyle": ("preset",), "AudioMaster": ("target_lufs",)}
_MISSING = object()


def _default(f):
    from dataclasses import MISSING

    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:  # type: ignore[misc]
        return f.default_factory()  # type: ignore[misc]
    return _MISSING


# ---------------------------------------------------------------- reading JSON

class _Reader:
    """Builds the dataclasses from parsed JSON, noting every wrong type or unknown key with its place."""

    def __init__(self, problems: list[Problem]):
        self.problems = problems

    def bad(self, where: str, message: str, level: str = "error") -> None:
        self.problems.append(Problem(level, where, message))

    def obj(self, data, where: str, known: tuple[str, ...]) -> dict:
        if not isinstance(data, dict):
            self.bad(where, f"should be an object, not {_kind(data)}")
            return {}
        for key in data:
            if key not in known:
                self.bad(f"{where}.{key}" if where else str(key), "isn't a field the timeline knows (ignored)",
                         "warning")
        return data

    def num(self, d: dict, key: str, where: str, default: float, required: bool = False) -> float:
        if key not in d:
            if required:
                self.bad(f"{where}.{key}", "is missing")
            return default
        v = d[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            self.bad(f"{where}.{key}", f"should be a number, not {_kind(v)}")
            return default
        return float(v)

    def integer(self, d: dict, key: str, where: str, default: int) -> int:
        v = self.num(d, key, where, default)
        if v != int(v):
            self.bad(f"{where}.{key}", f"should be a whole number, not {v:g}")
        return int(v)

    def text(self, d: dict, key: str, where: str, default: str = "", required: bool = False) -> str:
        if key not in d:
            if required:
                self.bad(f"{where}.{key}", "is missing")
            return default
        v = d[key]
        if not isinstance(v, str):
            self.bad(f"{where}.{key}", f"should be text, not {_kind(v)}")
            return default
        return v

    def flag(self, d: dict, key: str, where: str, default: bool) -> bool:
        if key not in d:
            return default
        if not isinstance(d[key], bool):
            self.bad(f"{where}.{key}", f"should be true or false, not {_kind(d[key])}")
            return default
        return d[key]

    def items(self, d: dict, key: str, where: str) -> list:
        v = d.get(key, [])
        if not isinstance(v, list):
            self.bad(f"{where}.{key}", f"should be a list, not {_kind(v)}")
            return []
        return v

    # ------------------------------------------------------------ each part
    def timeline(self, data) -> Timeline:
        d = self.obj(data, "", tuple(f.name for f in fields(Timeline)))
        version = self.integer(d, "version", "", VERSION) if "version" in d else VERSION
        if version > VERSION:
            self.bad("version", f"is {version}: this ClipMonster reads timelines up to version {VERSION}")
        canvas = self.canvas(d.get("canvas", {}))
        assets: dict[str, Asset] = {}
        raw_assets = d.get("assets", {})
        if not isinstance(raw_assets, dict):
            self.bad("assets", f"should be an object of assets by id, not {_kind(raw_assets)}")
            raw_assets = {}
        for key, raw in raw_assets.items():
            assets[str(key)] = self.asset(raw, f"assets.{key}")
        tracks = [self.track(raw, f"tracks[{i}]") for i, raw in enumerate(self.items(d, "tracks", ""))]
        markers = []
        for i, raw in enumerate(self.items(d, "markers", "")):
            where = f"markers[{i}]"
            m = self.obj(raw, where, ("t", "kind", "label"))
            markers.append(Marker(self.num(m, "t", where, 0.0, True), self.text(m, "kind", where, "note"),
                                  self.text(m, "label", where)))
        look = [str(x) for x in self.items(d, "look", "")]
        meta = d.get("meta", {})
        if not isinstance(meta, dict):
            self.bad("meta", f"should be an object, not {_kind(meta)}")
            meta = {}
        return Timeline(title=self.text(d, "title", ""), canvas=canvas, duration=self.num(d, "duration", "", 0.0),
                        assets=assets, tracks=tracks, markers=markers, audio=self.master(d.get("audio", {})),
                        look=look, meta=meta, version=version)

    def canvas(self, raw) -> Canvas:
        d = self.obj(raw, "canvas", ("width", "height", "fps", "background"))
        return Canvas(self.integer(d, "width", "canvas", 1080), self.integer(d, "height", "canvas", 1920),
                      self.integer(d, "fps", "canvas", 30), self.text(d, "background", "canvas", "#000000"))

    def asset(self, raw, where: str) -> Asset:
        d = self.obj(raw, where, tuple(f.name for f in fields(Asset)))
        return Asset(kind=self.text(d, "kind", where, "", True), path=self.text(d, "path", where),
                     duration=self.num(d, "duration", where, 0.0), width=self.integer(d, "width", where, 0),
                     height=self.integer(d, "height", where, 0), color=self.text(d, "color", where),
                     source=self.text(d, "source", where), credit=self.text(d, "credit", where),
                     url=self.text(d, "url", where), licence=self.text(d, "licence", where),
                     prompt=self.text(d, "prompt", where))

    def master(self, raw) -> AudioMaster:
        d = self.obj(raw, "audio", tuple(f.name for f in fields(AudioMaster)))
        base = AudioMaster()
        return AudioMaster(**{f.name: (self.flag(d, f.name, "audio", getattr(base, f.name))
                                       if isinstance(getattr(base, f.name), bool)
                                       else self.num(d, f.name, "audio", getattr(base, f.name)))
                              for f in fields(AudioMaster)})

    def track(self, raw, where: str) -> Track:
        d = self.obj(raw, where, tuple(f.name for f in fields(Track)))
        kind = self.text(d, "kind", where, "", True)
        track = Track(id=self.text(d, "id", where, "", True), kind=kind, role=self.text(d, "role", where),
                      gain_db=self.num(d, "gain_db", where, 0.0), muted=self.flag(d, "muted", where, False),
                      label=self.text(d, "label", where))
        clips = self.items(d, "clips", where)
        events = self.items(d, "events", where)
        if kind == "video":
            track.clips = [self.video_clip(c, f"{where}.clips[{i}]") for i, c in enumerate(clips)]
        elif kind == "audio":
            track.clips = [self.audio_clip(c, f"{where}.clips[{i}]") for i, c in enumerate(clips)]
        elif kind == "subtitle":
            track.events = [self.event(e, f"{where}.events[{i}]") for i, e in enumerate(events)]
            track.style = self.style(d.get("style", {}), f"{where}.style")
        if kind != "subtitle" and events:
            self.bad(f"{where}.events", f"only subtitle tracks have events (this is {kind or 'no kind'})")
        if kind == "subtitle" and clips:
            self.bad(f"{where}.clips", "a subtitle track has events, not clips")
        return track

    def transition(self, raw, where: str) -> Transition:
        if raw is None:
            return Transition()
        if isinstance(raw, str):  # "crossfade" alone: the usual length
            return Transition(raw, 0.0 if raw == "cut" else 0.3)
        d = self.obj(raw, where, ("type", "duration"))
        kind = self.text(d, "type", where, "cut")
        return Transition(kind, self.num(d, "duration", where, 0.0 if kind == "cut" else 0.3))

    def keyframes(self, d: dict, where: str) -> list[Keyframe]:
        out = []
        base = Keyframe()
        for i, raw in enumerate(self.items(d, "keyframes", where)):
            w = f"{where}.keyframes[{i}]"
            k = self.obj(raw, w, tuple(f.name for f in fields(Keyframe)))
            prev = out[-1] if out else base  # a value left out carries on from the keyframe before
            out.append(Keyframe(t=self.num(k, "t", w, 0.0, True), x=self.num(k, "x", w, prev.x),
                                y=self.num(k, "y", w, prev.y), scale=self.num(k, "scale", w, prev.scale),
                                rotation=self.num(k, "rotation", w, prev.rotation),
                                opacity=self.num(k, "opacity", w, prev.opacity), ease=self.text(k, "ease", w, "linear")))
        return out

    def video_clip(self, raw, where: str) -> VideoClip:
        d = self.obj(raw, where, tuple(f.name for f in fields(VideoClip)))
        crop = None
        if d.get("crop") is not None:
            c = self.obj(d["crop"], f"{where}.crop", ("x", "y", "w", "h"))
            crop = Crop(self.num(c, "x", f"{where}.crop", 0.0), self.num(c, "y", f"{where}.crop", 0.0),
                        self.num(c, "w", f"{where}.crop", 1.0), self.num(c, "h", f"{where}.crop", 1.0))
        return VideoClip(id=self.text(d, "id", where, "", True), asset=self.text(d, "asset", where, "", True),
                         start=self.num(d, "start", where, 0.0, True), duration=self.num(d, "duration", where, 0.0, True),
                         source_in=self.num(d, "source_in", where, 0.0), fit=self.text(d, "fit", where, "cover"),
                         crop=crop, keyframes=self.keyframes(d, where),
                         transition_in=self.transition(d.get("transition_in"), f"{where}.transition_in"),
                         transition_out=self.transition(d.get("transition_out"), f"{where}.transition_out"),
                         loop=self.flag(d, "loop", where, False), audio=self.text(d, "audio", where),
                         gain_db=self.num(d, "gain_db", where, 0.0), label=self.text(d, "label", where))

    def audio_clip(self, raw, where: str) -> AudioClip:
        d = self.obj(raw, where, tuple(f.name for f in fields(AudioClip)))
        return AudioClip(id=self.text(d, "id", where, "", True), asset=self.text(d, "asset", where, "", True),
                         start=self.num(d, "start", where, 0.0, True), duration=self.num(d, "duration", where, 0.0, True),
                         source_in=self.num(d, "source_in", where, 0.0), gain_db=self.num(d, "gain_db", where, 0.0),
                         fade_in=self.num(d, "fade_in", where, 0.0), fade_out=self.num(d, "fade_out", where, 0.0),
                         loop=self.flag(d, "loop", where, False), label=self.text(d, "label", where))

    def event(self, raw, where: str) -> SubtitleEvent:
        d = self.obj(raw, where, ("start", "end", "words"))
        words = []
        for i, w in enumerate(self.items(d, "words", where)):
            ww = f"{where}.words[{i}]"
            k = self.obj(w, ww, tuple(f.name for f in fields(WordEvent)))
            box = k.get("box")
            if box is not None and not (isinstance(box, list) and len(box) == 4 and
                                        all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
                self.bad(f"{ww}.box", "should be [x, y, width, height]")
                box = None
            words.append(WordEvent(text=self.text(k, "text", ww, "", True), start=self.num(k, "start", ww, 0.0, True),
                                   end=self.num(k, "end", ww, 0.0, True), color=self.text(k, "color", ww),
                                   emoji=self.text(k, "emoji", ww), emphasis=self.flag(k, "emphasis", ww, False),
                                   box=[float(v) for v in box] if box else None))
        return SubtitleEvent(self.num(d, "start", where, 0.0, True), self.num(d, "end", where, 0.0, True), words)

    def style(self, raw, where: str) -> SubtitleStyle:
        d = self.obj(raw, where, tuple(f.name for f in fields(SubtitleStyle)))
        base = SubtitleStyle()
        values = {}
        for f in fields(SubtitleStyle):
            have = getattr(base, f.name)
            if isinstance(have, bool):
                values[f.name] = self.flag(d, f.name, where, have)
            elif isinstance(have, int):
                values[f.name] = self.integer(d, f.name, where, have)
            elif isinstance(have, float):
                values[f.name] = self.num(d, f.name, where, have)
            else:
                values[f.name] = self.text(d, f.name, where, have)
        return SubtitleStyle(**values)


def _kind(v) -> str:
    return {bool: "true/false", int: "a number", float: "a number", str: "text", list: "a list", dict: "an object",
            type(None): "nothing (null)"}.get(type(v), type(v).__name__)


# ---------------------------------------------------------------- checking one

def validate(tl: Timeline, base: str | Path | None = None, files: bool = False) -> list[Problem]:
    """Everything wrong with a timeline, errors first. Errors would break the render; warnings are worth
    knowing (a clip that runs past its source, a gap in the picture). With `files`, each asset's file must
    exist (looked for in `base` when its path is relative)."""
    out: list[Problem] = []

    def err(where: str, message: str) -> None:
        out.append(Problem("error", where, message))

    def warn(where: str, message: str) -> None:
        out.append(Problem("warning", where, message))

    c = tl.canvas
    for name, value in (("width", c.width), ("height", c.height)):
        if not 16 <= value <= MAX_SIDE:
            err(f"canvas.{name}", f"is {value}: it must be 16 to {MAX_SIDE} pixels")
        elif value % 2:
            err(f"canvas.{name}", f"is {value}: H.264 needs an even number of pixels")
    if not 1 <= c.fps <= 120:
        err("canvas.fps", f"is {c.fps}: it must be 1 to 120")
    if not _COLOUR.match(c.background or ""):
        err("canvas.background", f"is {c.background!r}: a colour is written #RRGGBB")
    if not 0 < tl.duration <= MAX_DURATION:
        err("duration", f"is {tl.duration:g} s: it must be more than 0 and at most {MAX_DURATION / 3600:g} hours")

    for key, a in tl.assets.items():
        where = f"assets.{key}"
        if not _ID.match(key):
            err(where, "an asset's id is letters, digits, _ . and - (at most 64)")
        if a.kind not in ASSET_KINDS:
            err(f"{where}.kind", f"is {a.kind!r}: use {_or(ASSET_KINDS)}")
        elif a.kind == "color":
            if not _COLOUR.match(a.color or ""):
                err(f"{where}.color", f"is {a.color!r}: a colour is written #RRGGBB")
        elif not a.path.strip():
            err(f"{where}.path", "is empty")
        elif files and not tl.resolve(a, base).is_file():
            err(f"{where}.path", f"the file isn't there ({tl.resolve(a, base)})")
        if a.duration < 0:
            err(f"{where}.duration", "can't be negative")

    seen_tracks: set[str] = set()
    seen_clips: set[str] = set()
    for ti, tr in enumerate(tl.tracks):
        where = f"tracks[{ti}]"
        if not _ID.match(tr.id or ""):
            err(f"{where}.id", f"is {tr.id!r}: a track's id is letters, digits, _ . and - (at most 64)")
        elif tr.id in seen_tracks:
            err(f"{where}.id", f"two tracks are called {tr.id}")
        seen_tracks.add(tr.id)
        if tr.kind not in TRACK_KINDS:
            err(f"{where}.kind", f"is {tr.kind!r}: use {_or(TRACK_KINDS)}")
            continue
        if tr.kind == "audio" and tr.role not in AUDIO_ROLES:
            err(f"{where}.role", f"is {tr.role!r}: an audio track's role is {_or(AUDIO_ROLES)}")
        if tr.kind == "subtitle":
            _check_subtitles(tr, where, tl, err, warn)
            continue
        want = ("image", "video", "color") if tr.kind == "video" else ("audio", "video")
        last_end, last_where = None, ""
        for ci, clip in sorted(enumerate(tr.clips), key=lambda p: p[1].start):
            w = f"{where}.clips[{ci}]"
            if not _ID.match(clip.id or ""):
                err(f"{w}.id", f"is {clip.id!r}: a clip's id is letters, digits, _ . and - (at most 64)")
            elif clip.id in seen_clips:
                err(f"{w}.id", f"two clips are called {clip.id}")
            seen_clips.add(clip.id)
            asset = tl.assets.get(clip.asset)
            if asset is None:
                err(f"{w}.asset", f"there's no asset called {clip.asset!r}")
            elif asset.kind not in want:
                err(f"{w}.asset", f"{clip.asset} is {_a(asset.kind)}, which {_a(tr.kind)} track can't play")
            if clip.start < 0:
                err(f"{w}.start", f"is {clip.start:g}: a clip can't start before the video does")
            if clip.duration <= 0:
                err(f"{w}.duration", f"is {clip.duration:g}: it must be more than 0")
            elif clip.duration < 1.0 / max(1, c.fps) and tr.kind == "video":
                err(f"{w}.duration", f"is {clip.duration:g} s, less than one frame")
            if clip.source_in < 0:
                err(f"{w}.source_in", "can't be negative")
            if clip.end > tl.duration + 0.02 and tl.duration > 0:
                warn(w, f"runs to {clip.end:.2f} s, past the end of the video ({tl.duration:.2f} s): the rest is cut off")
            if tr.kind == "video" and last_end is not None and clip.start < last_end - 0.001:
                err(w, f"starts at {clip.start:.3f} s, before {last_where} ends ({last_end:.3f} s): clips on a video "
                       "track can't overlap (put it on another track)")
            last_end, last_where = clip.end, f"clips[{ci}]"
            if asset is not None and asset.duration > 0 and asset.kind in ("video", "audio") and not clip.loop and \
                    clip.source_in + clip.duration > asset.duration + 0.05:
                warn(w, f"plays {clip.duration:.2f} s from {clip.source_in:.2f} s, but {clip.asset} is only "
                        f"{asset.duration:.2f} s long: " + ("its last frame holds" if tr.kind == "video" else
                                                            "the rest is silence"))
            if tr.kind == "video":
                _check_video_clip(clip, w, asset, err, warn)
            else:
                if clip.fade_in < 0 or clip.fade_out < 0:
                    err(w, "a fade can't be negative")
                elif clip.fade_in + clip.fade_out > clip.duration + 0.001 > 0.001:
                    warn(w, "its fades are longer than the clip")
        if tr.kind == "video" and tr.id == VIDEO_BASE and tr.clips:
            _check_gaps(tr, where, tl, warn)

    for mi, m in enumerate(tl.markers):
        if m.kind not in MARKER_KINDS:
            err(f"markers[{mi}].kind", f"is {m.kind!r}: use {_or(MARKER_KINDS)}")
        if m.t < 0 or (tl.duration > 0 and m.t > tl.duration + 0.02):
            warn(f"markers[{mi}].t", f"is {m.t:g} s, outside the video")
    for li, name in enumerate(tl.look):
        if name not in LOOKS:
            err(f"look[{li}]", f"is {name!r}: use {_or(LOOKS)}")
    a = tl.audio
    if not -40 <= a.target_lufs <= -5:
        err("audio.target_lufs", f"is {a.target_lufs:g}: loudness targets run from -40 to -5 LUFS")
    if not -9 <= a.true_peak <= 0:
        err("audio.true_peak", f"is {a.true_peak:g}: the peak ceiling runs from -9 to 0 dBTP")
    if not -40 <= a.duck_db <= 0:
        err("audio.duck_db", f"is {a.duck_db:g}: ducking runs from -40 dB to 0 (off)")
    if a.duck_attack_ms <= 0 or a.duck_release_ms <= 0:
        err("audio", "the ducking's attack and release must be more than 0 ms")
    if not tl.video_tracks() or not any(t.clips for t in tl.video_tracks()):
        warn("tracks", "there's no picture: the video will be the background colour")
    return sorted(out, key=lambda p: p.level != "error")


def _check_video_clip(clip: VideoClip, w: str, asset: Asset | None, err, warn) -> None:
    if clip.fit not in FITS:
        err(f"{w}.fit", f"is {clip.fit!r}: use {_or(FITS)}")
    if clip.audio and clip.audio not in AUDIO_ROLES:
        err(f"{w}.audio", f"is {clip.audio!r}: use {_or(AUDIO_ROLES)}, or leave it out to mute the clip")
    elif clip.audio and asset is not None and asset.kind != "video":
        err(f"{w}.audio", f"only a video has sound of its own ({clip.asset} is {_a(asset.kind)})")
    if clip.crop is not None:
        k = clip.crop
        if not (0 <= k.x < 1 and 0 <= k.y < 1 and 0 < k.w <= 1 and 0 < k.h <= 1 and k.x + k.w <= 1.0001 and
                k.y + k.h <= 1.0001):
            err(f"{w}.crop", "must stay inside the source: x, y, w and h are shares of it from 0 to 1")
    for name, tr in (("transition_in", clip.transition_in), ("transition_out", clip.transition_out)):
        if tr.type not in TRANSITIONS:
            err(f"{w}.{name}.type", f"is {tr.type!r}: use {_or(TRANSITIONS)}")
        if tr.duration < 0:
            err(f"{w}.{name}.duration", "can't be negative")
        elif not tr.cut and tr.duration > clip.duration:
            warn(f"{w}.{name}", f"lasts {tr.duration:g} s, longer than its clip: it's shortened to fit")
    last = None
    for ki, k in enumerate(clip.keyframes):
        kw = f"{w}.keyframes[{ki}]"
        if k.ease not in EASES:
            err(f"{kw}.ease", f"is {k.ease!r}: use {_or(EASES)}")
        if k.t < 0:
            err(f"{kw}.t", "a keyframe's time counts from its clip's start, so it can't be negative")
        if last is not None and k.t <= last:
            err(f"{kw}.t", f"is {k.t:g} s, not after the keyframe before it ({last:g} s)")
        last = k.t
        if not 0.01 <= k.scale <= 20:
            err(f"{kw}.scale", f"is {k.scale:g}: scale runs from 0.01 to 20")
        if not 0 <= k.opacity <= 1:
            err(f"{kw}.opacity", f"is {k.opacity:g}: opacity runs from 0 to 1")
        if not -3600 <= k.rotation <= 3600:
            err(f"{kw}.rotation", f"is {k.rotation:g}: at most ten turns either way")
        if not (-5 <= k.x <= 6 and -5 <= k.y <= 6):
            err(kw, "x and y place the clip's centre on the canvas: 0 to 1 is on screen")
        if k.t > clip.duration + 0.02:
            warn(f"{kw}.t", f"is {k.t:g} s, after its clip ends ({clip.duration:g} s)")


def _check_gaps(tr: Track, where: str, tl: Timeline, warn) -> None:
    at = 0.0
    for clip in sorted(tr.clips, key=lambda k: k.start):
        if clip.start - at > 0.05:
            warn(where, f"nothing plays from {at:.2f} to {clip.start:.2f} s: the background colour shows")
        at = max(at, clip.end)
    if tl.duration - at > 0.05:
        warn(where, f"nothing plays from {at:.2f} s to the end ({tl.duration:.2f} s): the background colour shows")


def _check_subtitles(tr: Track, where: str, tl: Timeline, err, warn) -> None:
    st = tr.style or SubtitleStyle()
    sw = f"{where}.style"
    if st.preset not in CAPTION_PRESETS:
        err(f"{sw}.preset", f"is {st.preset!r}: use {_or(CAPTION_PRESETS)}")
    for name in ("color", "highlight", "emphasis", "outline_color", "money", "danger", "question"):
        if not _COLOUR.match(getattr(st, name) or ""):
            err(f"{sw}.{name}", f"is {getattr(st, name)!r}: a colour is written #RRGGBB")
    if not 8 <= st.size <= 400:
        err(f"{sw}.size", f"is {st.size}: letters are 8 to 400 pixels tall")
    if not 1 <= st.max_words <= 12:
        err(f"{sw}.max_words", f"is {st.max_words}: 1 to 12 words at a time")
    if not 0 <= st.position <= 1:
        err(f"{sw}.position", "runs from 0 to 1 down the canvas (0 = the usual place)")
    if not 0 <= st.outline <= 40:
        err(f"{sw}.outline", "runs from 0 to 40 pixels")
    last = None
    for ei, ev in enumerate(tr.events):
        w = f"{where}.events[{ei}]"
        if ev.end <= ev.start:
            err(w, f"ends ({ev.end:g} s) before it starts ({ev.start:g} s)")
        if ev.start < 0:
            err(f"{w}.start", "can't be negative")
        if not ev.words:
            err(f"{w}.words", "a caption needs at least one word")
        if last is not None and ev.start < last - 0.001:
            err(w, f"starts at {ev.start:.3f} s, before the caption before it ends ({last:.3f} s)")
        last = ev.end
        prev = None
        for wi, word in enumerate(ev.words):
            ww = f"{w}.words[{wi}]"
            if not word.text.strip():
                err(f"{ww}.text", "is empty")
            if word.end < word.start:
                err(ww, f"ends ({word.end:g} s) before it starts ({word.start:g} s)")
            if word.start < ev.start - 0.05 or word.end > ev.end + 0.05:
                err(ww, f"is spoken from {word.start:g} to {word.end:g} s, outside its caption "
                        f"({ev.start:g} to {ev.end:g} s)")
            if prev is not None and word.start < prev - 0.001:
                err(ww, "starts before the word before it")
            prev = word.start
            if word.color and not _COLOUR.match(word.color):
                err(f"{ww}.color", f"is {word.color!r}: a colour is written #RRGGBB")
        if ev.end > tl.duration + 0.05 and tl.duration > 0:
            warn(w, f"runs to {ev.end:.2f} s, past the end of the video")


def _or(names) -> str:
    names = list(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def _a(kind: str) -> str:
    return ("an " if kind[:1] in "aeiou" else "a ") + kind


def check(tl: Timeline, base: str | Path | None = None, files: bool = False) -> list[Problem]:
    """validate(), raising TimelineError when anything would break the render. Returns the warnings."""
    problems = validate(tl, base, files)
    if any(p.level == "error" for p in problems):
        raise TimelineError(problems)
    return problems


# ---------------------------------------------------------------- the same shape as JSON Schema

def _schema_of(cls, required: tuple[str, ...], extra: dict | None = None) -> dict:
    props: dict = {}
    for f in fields(cls):
        default = _default(f)
        if isinstance(default, bool):
            props[f.name] = {"type": "boolean"}
        elif isinstance(default, int):
            props[f.name] = {"type": "integer"}
        elif isinstance(default, float):
            props[f.name] = {"type": "number"}
        elif isinstance(default, str) or default is _MISSING and f.type in ("str", str):
            props[f.name] = {"type": "string"}
        elif default is _MISSING and f.type in ("float", float):
            props[f.name] = {"type": "number"}
    props.update(extra or {})
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _json_schema() -> dict:
    colour = {"type": "string", "pattern": _COLOUR.pattern}
    transition = {"oneOf": [{"type": "string", "enum": list(TRANSITIONS)},
                            _schema_of(Transition, ("type",), {"type": {"type": "string", "enum": list(TRANSITIONS)},
                                                                "duration": {"type": "number", "minimum": 0}})]}
    keyframe = _schema_of(Keyframe, ("t",), {"ease": {"type": "string", "enum": list(EASES)},
                                             "opacity": {"type": "number", "minimum": 0, "maximum": 1}})
    crop = _schema_of(Crop, ())
    video_clip = _schema_of(VideoClip, ("id", "asset", "start", "duration"), {
        "fit": {"type": "string", "enum": list(FITS)}, "crop": {"oneOf": [crop, {"type": "null"}]},
        "keyframes": {"type": "array", "items": keyframe}, "transition_in": transition, "transition_out": transition,
        "audio": {"type": "string", "enum": ["", *AUDIO_ROLES]}})
    audio_clip = _schema_of(AudioClip, ("id", "asset", "start", "duration"))
    word = _schema_of(WordEvent, ("text", "start", "end"), {
        "color": {"oneOf": [colour, {"const": ""}]},
        "box": {"oneOf": [{"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                          {"type": "null"}]}})
    event = _schema_of(SubtitleEvent, ("start", "end", "words"), {"words": {"type": "array", "items": word,
                                                                            "minItems": 1}})
    style = _schema_of(SubtitleStyle, (), {"preset": {"type": "string", "enum": list(CAPTION_PRESETS)},
                                           **{k: colour for k in ("color", "highlight", "emphasis", "outline_color",
                                                                  "money", "danger", "question")}})
    track = _schema_of(Track, ("id", "kind"), {
        "kind": {"type": "string", "enum": list(TRACK_KINDS)}, "role": {"type": "string", "enum": ["", *AUDIO_ROLES]},
        "clips": {"type": "array", "items": {"anyOf": [video_clip, audio_clip]}},
        "events": {"type": "array", "items": event}, "style": {"oneOf": [style, {"type": "null"}]}})
    asset = _schema_of(Asset, ("kind",), {"kind": {"type": "string", "enum": list(ASSET_KINDS)}})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ClipMonster timeline",
        "type": "object",
        "properties": {
            "version": {"type": "integer", "minimum": 1, "maximum": VERSION},
            "title": {"type": "string"},
            "canvas": _schema_of(Canvas, ("width", "height"), {"background": colour}),
            "duration": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_DURATION},
            "assets": {"type": "object", "additionalProperties": asset},
            "tracks": {"type": "array", "items": track},
            "markers": {"type": "array", "items": _schema_of(Marker, ("t",), {
                "kind": {"type": "string", "enum": list(MARKER_KINDS)}})},
            "audio": _schema_of(AudioMaster, ()),
            "look": {"type": "array", "items": {"type": "string", "enum": list(LOOKS)}},
            "meta": {"type": "object"},
        },
        "required": ["canvas", "duration", "tracks"],
        "additionalProperties": False,
    }


JSON_SCHEMA = _json_schema()


def standard(title: str = "", canvas: Canvas | None = None, duration: float = 0.0) -> Timeline:
    """An empty timeline with the six standard tracks."""
    tl = Timeline(title=title, canvas=canvas or Canvas(), duration=duration)
    for track_id in STANDARD_TRACKS:
        tl.ensure_track(track_id)
    return tl
