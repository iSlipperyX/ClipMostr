"""The director: a prompt becomes a script and a storyboard (scenes, pictures, camera moves, sound cues).

The local AI (intelligence/llama_engine.py) writes it, its answer held to STORYBOARD_SCHEMA by a GGUF
grammar, so it always parses into the same shape:

    {"title": ..., "category": ..., "target_duration": 65, "music_mood": "Suspense",
     "hook": ..., "caption": ..., "hashtags": [...],
     "scenes": [{"scene_id": 1, "speech_text": ..., "visual_type": "generated_image",
                 "visual_prompt": ..., "camera_motion": "slow_push_in", "sfx_cue": "bass_drop",
                 "sfx_offset_ms": 0, "emphasis": [...], "keywords": [...]}, ...]}

What a grammar can't hold the model to is checked here and fixed or asked again (normalise, problems):

* the hook: scene 1 is one short line (at most HOOK_WORDS words, under 3.5 s at the target pace), split
  off the rest of the scene when the model ran on;
* the length: the words add up to the target length at the voice's pace (130-155 words a minute by
  default), which for Creator Rewards is 62-85 s; too short or too long and the model is asked again
  with the count;
* originality: a script too close to one made before (originality.py) is asked for again from a new angle.

Without an AI, your own script works too (from_script): it's split into scenes at sentence ends and each
scene's picture is described from its words and the style.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Callable

Log = Callable[[str], None]

MOTIONS = ("slow_push_in", "slow_pull_out", "pan_left", "pan_right", "pan_up", "pan_down", "pan_down_right",
           "pan_up_left", "push_in_left", "push_in_right")
SFX_CUES = ("none", "whoosh", "riser", "bass_drop", "sub_boom", "impact", "glass_hit", "vinyl_click", "shimmer",
            "tick", "heartbeat")
VISUAL_TYPES = ("generated_image", "broll", "texture")
MOODS = ("Suspense", "Dark", "Mysterious", "Epic", "Emotional", "Upbeat", "Chill")

HOOK_WORDS = 10  # scene 1 at most: about 3.5 s at 150-170 words a minute
SCENE_WORDS = (6, 30)  # a scene's words, fewest and most
MIN_SCENES, MAX_SCENES = 5, 16
DEFAULT_WPM = 150
WORD_SLACK = 0.12  # the script may miss its word target by this share before it's asked for again

# Creator Rewards pays for videos over one minute; 62-85 s leaves room either side.
REWARDS_RANGE = (62.0, 85.0)

STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "The video's title, at most 10 words, no hashtags"},
        "category": {"type": "string", "description": "The category it's for"},
        "target_duration": {"type": "integer", "description": "Seconds of narration it's written for"},
        "music_mood": {"type": "string", "enum": list(MOODS), "description": "The background music's mood"},
        "hook": {"type": "string", "description": "3-7 words shown big on screen at the start"},
        "caption": {"type": "string", "description": "The TikTok post's caption: one or two sentences, no hashtags"},
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "description": "3-6 relevant hashtags without the # sign"},
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_id": {"type": "integer", "description": "1, 2, 3, ..."},
                    "speech_text": {"type": "string", "description": "Exactly what the narrator says in this scene"},
                    "visual_type": {"type": "string", "enum": list(VISUAL_TYPES),
                                    "description": "generated_image (a picture is made for it), broll (stock footage "
                                                   "matching the keywords) or texture (an abstract moving background)"},
                    "visual_prompt": {"type": "string",
                                      "description": "The picture, for an image generator: subject, setting, lighting, "
                                                     "lens and mood, vertical 9:16, no text or logos in it"},
                    "camera_motion": {"type": "string", "enum": list(MOTIONS), "description": "How the camera moves"},
                    "sfx_cue": {"type": "string", "enum": list(SFX_CUES),
                                "description": "A sound effect for this scene, or none"},
                    "sfx_offset_ms": {"type": "integer", "description": "When the effect plays, ms after the scene starts"},
                    "emphasis": {"type": "array", "items": {"type": "string"},
                                 "description": "1-3 words from speech_text that the captions light up"},
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": "2-4 plain words describing what's on screen, for stock footage"},
                },
                "required": ["scene_id", "speech_text", "visual_type", "visual_prompt", "camera_motion", "sfx_cue",
                             "sfx_offset_ms", "emphasis", "keywords"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "category", "target_duration", "music_mood", "hook", "caption", "hashtags", "scenes"],
    "additionalProperties": False,
}

SYSTEM = """You are the head writer and director of a faceless short-form video channel. You write narrated \
vertical videos (TikTok, Reels, Shorts) that people watch to the end, and you storyboard every scene.

Retention rules:
1. Scene 1 is the hook: ONE short line, at most {hook} words, that stops the scroll. A pattern interrupt: a bold \
claim, a direct "you" challenge, a surprising fact or an open question. No greeting, no "in this video", no \
channel name.
2. Open a loop early (promise a payoff) and close it near the end. Give a small reward every few scenes.
3. Every scene moves the story on. Short, spoken sentences; plain words; second person where it fits.
4. Change the energy every 2-3 scenes (a question, a twist, a number, a short punchy line).
5. The last scene lands the payoff and ends on a line that makes people rewatch or comment. Never promise a \
part 2, a follow-up video or a link.

Honesty rules: say only things that are true. Don't invent statistics, studies, quotes, dates or names; when \
something is a theory or legend, say so. Today's date and any sources come with the request: they beat what you \
remember, which may be out of date. No medical, legal or financial instructions presented as advice.

Storyboard rules:
* Every scene has one picture. visual_prompt describes it for an image generator: subject, setting, lighting, \
camera and mood, vertical 9:16. No text, letters, logos, watermarks or real, identifiable people in pictures.
* Vary camera_motion from scene to scene. sfx_cue is a sound effect that fits: bass_drop or impact on the hook \
and on big reveals, whoosh on fast changes, riser before a reveal, glass_hit or shimmer on a twist, tick or \
heartbeat for tension, none for most scenes (at most one scene in three has an effect).
* emphasis: the 1-3 strongest words of the scene, copied exactly from speech_text."""

ASK = """Make a video about: {prompt}
Category: {category}{instructions}
Length: {seconds} seconds of narration, so {lo}-{hi} words of speech_text in total across all scenes \
(about {words}; the narrator reads {wpm} words a minute). Count them.
Scenes: {scenes_lo}-{scenes_hi} scenes. Scene 1 is the hook (at most {hook} words); the others have one or two \
sentences each ({scene_lo}-{scene_hi} words).
Picture style for every visual_prompt: {style}
Music: pick the mood that fits."""


class DirectorError(RuntimeError):
    """A plain-English reason no storyboard could be written."""


@dataclass
class Scene:
    scene_id: int
    speech_text: str
    visual_type: str = "generated_image"
    visual_prompt: str = ""
    camera_motion: str = "slow_push_in"
    sfx_cue: str = "none"
    sfx_offset_ms: int = 0
    emphasis: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    # What the Studio's scriptwriter adds (studio/scriptwriter.py); empty on the Creator's own storyboards.
    beat: str = ""  # the scene's job in the story: hook | setup | build | twist | payoff | cta
    mood: str = ""  # this scene's feeling, when it differs from the video's music mood
    stock_query: str = ""  # what to search stock footage for ("city skyline night")
    chapter: str = ""  # on the first scene of a chapter of a long video: the chapter's title

    @property
    def words(self) -> list[str]:
        return self.speech_text.split()

    def carry(self, other: "Scene") -> "Scene":
        """Take the Studio's extras from `other` (a scene made from it keeps them)."""
        self.beat, self.mood, self.stock_query = other.beat, other.mood, other.stock_query
        return self


@dataclass
class Storyboard:
    title: str
    category: str = ""
    target_duration: int = 65
    music_mood: str = "Suspense"
    scenes: list[Scene] = field(default_factory=list)
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    prompt: str = ""  # what it was made from
    written_by: str = ""  # the model that wrote it, or "your script"
    notes: list[str] = field(default_factory=list)  # what normalise() changed
    sources: list[dict] = field(default_factory=list)  # what was looked up online (web.Source.short())
    factcheck: dict = field(default_factory=dict)  # the fact check against them (factcheck.Report.to_json())
    cta: str = ""  # the closing call to action, when the Studio's scriptwriter wrote one

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.scenes)

    @property
    def script(self) -> str:
        return "\n".join(s.speech_text for s in self.scenes)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Storyboard":
        if not isinstance(data, dict):
            raise ValueError("not a storyboard")
        scenes = []
        for i, raw in enumerate(data.get("scenes") or [], 1):
            if not isinstance(raw, dict):
                continue
            known = {k: raw[k] for k in Scene.__dataclass_fields__ if k in raw}
            known.setdefault("scene_id", i)
            known["speech_text"] = str(known.get("speech_text") or "")
            scenes.append(Scene(**known))
        top = {k: data[k] for k in cls.__dataclass_fields__ if k in data and k != "scenes"}
        return cls(**top, scenes=scenes)

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- styles

@dataclass(frozen=True)
class Style:
    key: str
    label: str
    prompt: str  # added to every picture's description
    negative: str
    mood: str  # the music mood when the storyboard doesn't say
    palette: tuple[str, str, str]  # the built-in textures' colours (dark, mid, light)
    highlight: str  # the spoken word's caption colour
    emphasis: str = "#4FD8FF"  # the script's emphasis words' colour (never the same as highlight)


NEGATIVE = ("text, letters, words, caption, watermark, logo, signature, frame, border, blurry, lowres, jpeg "
            "artifacts, deformed, extra fingers, bad anatomy, duplicate, cropped")
STYLES: dict[str, Style] = {s.key: s for s in (
    Style("cinematic", "Cinematic dark", "cinematic film still, dramatic chiaroscuro lighting, moody, shallow depth "
          "of field, 35mm, rich contrast, highly detailed, vertical 9:16", NEGATIVE, "Suspense",
          ("#07090f", "#1b2a4a", "#4f7cff"), "#FFE600"),
    Style("documentary", "Documentary", "documentary photograph, natural light, realistic, detailed textures, "
          "muted film colours, 50mm, vertical 9:16", NEGATIVE, "Mysterious", ("#0e0b08", "#4a3b28", "#c9a46a"),
          "#FFD166", "#7FDBFF"),
    Style("history", "Old-world painting", "oil painting in the style of an old master, candle light, aged canvas "
          "texture, historical, rich detail, vertical 9:16", NEGATIVE, "Epic", ("#120c06", "#5b3a1a", "#d9a441"),
          "#FFC857", "#FFF3D6"),
    Style("horror", "Horror", "dark horror film still, fog, cold desaturated colours, deep shadows, unsettling, "
          "grainy, vertical 9:16", NEGATIVE + ", gore, blood", "Dark", ("#020303", "#16201c", "#6f8f7a"),
          "#FF3B30", "#E6FFF2"),
    Style("bright", "Bright and bold", "vivid editorial photo, bright saturated colours, clean studio light, crisp "
          "detail, energetic, vertical 9:16", NEGATIVE, "Upbeat", ("#0b1026", "#3a1c71", "#ff6a3d"), "#00FF66",
          "#FFE600"),
    Style("dreamy", "Calm and dreamy", "soft pastel photograph, golden hour haze, calm, minimal, gentle bokeh, "
          "vertical 9:16", NEGATIVE, "Chill", ("#0d1321", "#3e5c76", "#f0ebd8"), "#9BE7FF",
          "#FFD6A5"),
)}
DEFAULT_STYLE = "cinematic"

# A category's name or words in its instructions suggest a style (the Creator tab can pick another).
STYLE_WORDS = (
    ("horror", ("scary", "horror", "creepy", "paranormal", "haunted", "ghost", "nosleep", "crime", "unsolved")),
    ("history", ("history", "historical", "ancient", "empire", "medieval", "war", "myth", "legend")),
    ("documentary", ("science", "space", "nature", "documentary", "mystery", "mysteries", "facts", "explained")),
    ("bright", ("money", "finance", "business", "motivation", "hustle", "fitness", "tech", "gaming", "food")),
    ("dreamy", ("stoic", "stoicism", "mindful", "calm", "self-improvement", "self improvement", "relationship",
                "wellness", "sleep")),
    ("cinematic", ("psychology", "dark", "manipulation", "secrets", "truth")),
)


def style_for(category: str, prompt: str = "", instructions: str = "") -> str:
    text = f" {category} {prompt} {instructions} ".lower()
    for key, words in STYLE_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return key
    return DEFAULT_STYLE


def get_style(key: str) -> Style:
    return STYLES.get(key or "", STYLES[DEFAULT_STYLE])


# ---------------------------------------------------------------- word targets

def word_target(seconds: float, wpm: float = DEFAULT_WPM) -> tuple[int, int, int]:
    """(fewest, aim, most) words for `seconds` of narration at `wpm` words a minute."""
    aim = round(seconds * wpm / 60)
    return round(aim * (1 - WORD_SLACK)), aim, round(aim * (1 + WORD_SLACK))


def scene_range(seconds: float, per: float = 0.0) -> tuple[int, int]:
    """How many scenes a video this long wants: a new picture about every 4-8 seconds, or about every `per`
    seconds (the pace your best videos had: intelligence/learner.py)."""
    if per > 0:
        lo = max(MIN_SCENES, min(MAX_SCENES - 1, round(seconds / (per * 1.2))))
        hi = min(MAX_SCENES, max(lo + 1, round(seconds / (per * 0.85))))
        return lo, hi
    lo = max(MIN_SCENES, round(seconds / 8))
    hi = min(MAX_SCENES, max(lo + 2, round(seconds / 4.5)))
    return lo, hi


# ---------------------------------------------------------------- cleaning up an answer

_STAGE = re.compile(r"\[[^\]]*\]|\([^)]*(?:music|sfx|sound|pause|beat|cut|scene)[^)]*\)|\*[^*]+\*", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_HASHTAG = re.compile(r"(?:^|\s)#\w+")
_SPACE = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?…])[\"”’)]*\s+(?=[\"“‘(]?[A-Z0-9])")
_LABEL = re.compile(r"^\s*(?:narrator|voice ?over|vo|scene \d+|hook)\s*:\s*", re.I)


def clean_speech(text: str) -> str:
    """What the narrator says, without stage directions, emoji, hashtags or speaker labels."""
    text = _LABEL.sub("", str(text or ""))
    text = _STAGE.sub(" ", text)
    text = _EMOJI.sub("", text)
    text = _HASHTAG.sub(" ", text)
    text = text.replace("—", ", ").replace("–", ", ").replace(" - ", ", ")
    text = _SPACE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    return text.strip(" ,")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(clean_speech(text)) if s.strip()]


STOPWORDS = set("""a an and are as at be been but by can could did do does doing don't for from had has have he her
him his how i if in into is it its it's just like make me more most my no not now of on one only or our out over
people really she so some than that that's the their them then there these they this those through to too up us
very was we were what when where which while who why will with would you your you're yourself ever never every
something someone everything anyone thing things way get got go going know think say said tell told look see""".split())


def keywords_of(text: str, n: int = 4) -> list[str]:
    """The plain content words of a line, most telling first (longer and rarer words first)."""
    words = [re.sub(r"[^\w'-]", "", w).lower().strip("'-") for w in text.split()]
    seen: list[str] = []
    for w in words:
        if len(w) > 2 and w not in STOPWORDS and not w.isdigit() and w not in seen:
            seen.append(w)
    seen.sort(key=lambda w: -len(w))
    return seen[:n]


def _choice(value, allowed: tuple[str, ...], fallback: str) -> str:
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    for a in allowed:
        if v == a.lower():
            return a
    return fallback


def mood_of(text: str) -> str:
    """One of MOODS from free text ("Suspense/Dark Ambient" -> Suspense)."""
    low = str(text or "").lower()
    for m in MOODS:
        if m.lower() in low:
            return m
    for word, m in (("ambient", "Chill"), ("lofi", "Chill"), ("lo-fi", "Chill"), ("horror", "Dark"),
                    ("tension", "Suspense"), ("cinematic", "Epic"), ("happy", "Upbeat"), ("sad", "Emotional"),
                    ("mystery", "Mysterious"), ("calm", "Chill")):
        if word in low:
            return m
    return ""


def split_hook(scenes: list[Scene], hook_words: int = HOOK_WORDS) -> bool:
    """Scene 1 must be one short line. When the model put more in it, the first sentence (or clause) stays
    as the hook and the rest becomes scene 2, with the same picture. True if it split."""
    if not scenes or len(scenes[0].words) <= hook_words:
        return False
    first = scenes[0]
    parts = sentences(first.speech_text)
    head = parts[0] if parts else first.speech_text
    if len(head.split()) > hook_words:  # one long sentence: try its first clause
        clause = re.split(r"(?<=[,;:])\s+", head, maxsplit=1)
        if len(clause) == 2 and 3 <= len(clause[0].split()) <= hook_words:
            head = clause[0].rstrip(",;:") + "."
            rest_text = clause[1] + " " + " ".join(parts[1:])
        else:
            return False
    else:
        rest_text = " ".join(parts[1:])
    rest_text = rest_text.strip()
    if not rest_text:
        return False
    first.speech_text = head
    second = Scene(0, rest_text[0].upper() + rest_text[1:], first.visual_type, first.visual_prompt,
                   "slow_pull_out" if first.camera_motion != "slow_pull_out" else "pan_right", "none", 0,
                   [w for w in first.emphasis if w.lower() in rest_text.lower()], list(first.keywords)).carry(first)
    if first.beat:
        first.beat, second.beat = "hook", "setup"
    first.emphasis = [w for w in first.emphasis if w.lower() in head.lower()]
    scenes.insert(1, second)
    return True


def normalise(board: Storyboard, style: Style | None = None, hook_words: int = HOOK_WORDS,
              max_scenes: int = MAX_SCENES + 4) -> Storyboard:
    """Fix what a grammar can't: clean speech, a short hook, no empty or tiny scenes, valid choices, varied
    camera moves, pictures for every scene. Notes what it changed in board.notes. `max_scenes`: the most
    scenes kept (a long video's scriptwriter asks for more)."""
    style = style or get_style(DEFAULT_STYLE)
    scenes: list[Scene] = []
    for raw in board.scenes:
        text = clean_speech(raw.speech_text)
        if not text:
            continue
        sc = Scene(0, text,
                   _choice(raw.visual_type, VISUAL_TYPES, "generated_image"),
                   " ".join(str(raw.visual_prompt or "").split()),
                   _choice(raw.camera_motion, MOTIONS, ""),
                   _choice(raw.sfx_cue, SFX_CUES, "none"),
                   max(0, min(2000, int(raw.sfx_offset_ms or 0) if str(raw.sfx_offset_ms).lstrip("-").isdigit() else 0)),
                   [str(w).strip() for w in (raw.emphasis or []) if str(w).strip()][:3],
                   [str(w).strip().lower() for w in (raw.keywords or []) if str(w).strip()][:4])
        sc.beat = str(getattr(raw, "beat", "") or "").strip().lower()
        sc.mood = mood_of(getattr(raw, "mood", "")) if getattr(raw, "mood", "") else ""
        sc.stock_query = " ".join(str(getattr(raw, "stock_query", "") or "").split())[:80]
        sc.chapter = " ".join(str(getattr(raw, "chapter", "") or "").split())[:80]
        # A scene of two or three words after the hook joins the one before it (never across a chapter's start).
        if scenes and len(scenes) > 1 and len(sc.words) < 4 and not sc.chapter:
            scenes[-1].speech_text += " " + sc.speech_text
            board.notes.append(f"joined a {len(sc.words)}-word scene to the one before")
            continue
        scenes.append(sc)
    if not scenes:
        raise DirectorError("The storyboard has no narration in it.")
    if split_hook(scenes, hook_words):
        board.notes.append("split the hook off scene 1 so it's one short line")
    # Long scenes are split at a sentence end, so pictures keep changing.
    out: list[Scene] = []
    for sc in scenes:
        parts = sentences(sc.speech_text)
        if len(sc.words) > SCENE_WORDS[1] and len(parts) > 1:
            half, acc, first = len(sc.words) / 2, 0, []
            for p in parts:
                if acc >= half and first:
                    break
                first.append(p)
                acc += len(p.split())
            rest = parts[len(first):]
            out.append(Scene(0, " ".join(first), sc.visual_type, sc.visual_prompt, sc.camera_motion, sc.sfx_cue,
                             sc.sfx_offset_ms, sc.emphasis, sc.keywords, chapter=sc.chapter).carry(sc))
            out.append(Scene(0, " ".join(rest), sc.visual_type, sc.visual_prompt, "", "none", 0, [],
                             sc.keywords).carry(sc))
            board.notes.append("split a long scene in two")
        else:
            out.append(sc)
    scenes = out[:max(1, max_scenes)]
    if len(out) > len(scenes):  # never silently: the rest of the story is gone
        board.notes.append(f"kept the first {len(scenes)} of {len(out)} scenes (the most one video takes)")
    last_motion = ""
    for i, sc in enumerate(scenes, 1):
        sc.scene_id = i
        if not sc.keywords:
            sc.keywords = keywords_of(sc.speech_text)
        if not sc.visual_prompt:
            sc.visual_prompt = ", ".join(sc.keywords) or board.title
        if style.prompt and style.prompt.split(",")[0].lower() not in sc.visual_prompt.lower():
            sc.visual_prompt = f"{sc.visual_prompt.rstrip(' ,.')}, {style.prompt}"
        if not sc.camera_motion or sc.camera_motion == last_motion:
            sc.camera_motion = MOTIONS[(MOTIONS.index(last_motion) + 3) % len(MOTIONS)] if last_motion else MOTIONS[0]
        last_motion = sc.camera_motion
        low = sc.speech_text.lower()
        sc.emphasis = [w for w in sc.emphasis if w.lower() in low]
    if scenes[0].sfx_cue == "none":
        scenes[0].sfx_cue, scenes[0].sfx_offset_ms = "bass_drop", 0
    board.scenes = scenes
    board.music_mood = mood_of(board.music_mood) or style.mood
    board.title = " ".join(clean_speech(board.title).split()[:12]).rstrip(".") or " ".join(scenes[0].words[:8])
    board.hook = clean_speech(board.hook) or board.title
    board.caption = clean_speech(board.caption) or board.title
    board.cta = clean_speech(board.cta)
    tags: list[str] = []
    for t in board.hashtags or []:
        t = re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if t and t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    board.hashtags = tags[:6]
    return board


def problems(board: Storyboard, seconds: float, wpm: float, hook_words: int = HOOK_WORDS) -> list[str]:
    """What's wrong with a storyboard for this length, in words a model can act on ([] = fine)."""
    lo, aim, hi = word_target(seconds, wpm)
    out = []
    n = board.word_count
    if n < lo:
        out.append(f"The script has {n} words but needs {lo}-{hi} (about {aim}) to fill {seconds:.0f} seconds. "
                   f"Add about {aim - n} words: more detail, an example or another reveal.")
    elif n > hi:
        out.append(f"The script has {n} words but must have {lo}-{hi} (about {aim}) for {seconds:.0f} seconds. "
                   f"Cut about {n - aim} words.")
    if board.scenes and len(board.scenes[0].words) > hook_words + 4:
        out.append(f"Scene 1 must be one hook line of at most {hook_words} words.")
    scenes_lo, _ = scene_range(seconds)
    if len(board.scenes) < max(3, scenes_lo - 2):
        out.append(f"Use at least {scenes_lo} scenes.")
    return out


# ---------------------------------------------------------------- writing one

@dataclass
class Brief:
    """What to make."""

    prompt: str
    category: str = ""
    instructions: str = ""  # the category's special requests
    seconds: float = 65.0
    wpm: float = DEFAULT_WPM
    style: str = ""  # STYLES key; "" = from the category
    avoid: list[str] = field(default_factory=list)  # titles of earlier videos on this, to take a new angle
    facts: str = ""  # today's date and what was found online (web.brief); "" = just today's date
    lessons: str = ""  # how earlier videos did (intelligence/learner.py, else learn.Lessons.creator_text); "" = none yet
    scene_seconds: float = 0.0  # a new picture about this often (learner.Pacing); 0 = the usual 4-8 s


def write(s, brief: Brief, *, log: Log | None = None, check: Callable[[], None] | None = None,
          progress=None, similar: Callable[[Storyboard], tuple[float, str]] | None = None,
          ask=None, tries: int = 3) -> Storyboard:
    """Have the AI write the storyboard. `s` is the Settings (which AI, Settings → Creator → Script writer).
    `similar(board)` gives (0-1 similarity, title) to the closest earlier video, for the originality check.
    `ask` stands in for ai.ask_json (tests). Raises DirectorError."""
    from .. import ai

    log = log or (lambda m: None)
    style = get_style(brief.style or style_for(brief.category, brief.prompt, brief.instructions))
    lo, aim, hi = word_target(brief.seconds, brief.wpm)
    scenes_lo, scenes_hi = scene_range(brief.seconds, brief.scene_seconds)
    system = SYSTEM.format(hook=HOOK_WORDS)
    base = ASK.format(prompt=brief.prompt.strip(), category=brief.category or "general",
                      instructions=f"\nWhat this account wants: {brief.instructions.strip()}" if brief.instructions.strip() else "",
                      seconds=round(brief.seconds), lo=lo, hi=hi, words=aim, wpm=round(brief.wpm),
                      scenes_lo=scenes_lo, scenes_hi=scenes_hi, hook=HOOK_WORDS, scene_lo=SCENE_WORDS[0],
                      scene_hi=SCENE_WORDS[1] - 6, style=style.prompt)
    from . import web

    base += "\n\n" + (brief.facts or web.brief(None))
    if brief.lessons.strip():
        base += "\n\n" + brief.lessons.strip()
    if brief.avoid:
        base += "\nEarlier videos on this already exist; take a clearly different angle from: " + \
                "; ".join(f'"{t}"' for t in brief.avoid[:5])
    only = script_writers(s)
    if not only:
        raise DirectorError(no_writer_text(s))
    ask = ask or ai.ask_json
    feedback: list[str] = []
    best: Storyboard | None = None
    best_gap = 1e9
    for attempt in range(1, tries + 1):
        prompt = base
        if feedback:
            prompt += "\n\nYour last storyboard needs fixing:\n- " + "\n- ".join(feedback) + \
                      "\nWrite the whole storyboard again with these fixed."
        try:
            answer = ask(s, "find", system, prompt, STORYBOARD_SCHEMA, effort="medium", max_tokens=8000, log=log,
                         check=check, what="write the script and storyboard" if attempt == 1 else
                         "write the storyboard again", only=only, progress=progress)
        except ai.AIError as exc:
            if best is not None:
                log(f"Asking again didn't work ({exc}); keeping the storyboard it wrote before.")
                break
            raise DirectorError(str(exc)) from exc
        try:
            board = Storyboard.from_json(answer.data)
            board = normalise(board, style)
        except (ValueError, TypeError, DirectorError) as exc:
            feedback = [f"The answer couldn't be used ({exc}). Follow the schema exactly."]
            continue
        board.prompt, board.written_by = brief.prompt, getattr(answer, "model", "") or "AI"
        board.category = brief.category or board.category
        board.target_duration = int(round(brief.seconds))
        feedback = problems(board, brief.seconds, brief.wpm)
        if similar is not None:
            score, like = similar(board)
            if score >= ORIGINALITY_LIMIT:
                feedback.append(f"It's too close to an earlier video (\"{like}\"). Take a different angle, with "
                                "different facts, examples and wording.")
        gap = abs(board.word_count - aim) + (1000 if similar and similar(board)[0] >= ORIGINALITY_LIMIT else 0)
        if gap < best_gap:
            best, best_gap = board, gap
        if not feedback:
            log(f"Script written: \"{board.title}\", {len(board.scenes)} scenes, {board.word_count} words.")
            return board
        log("The script needs another go: " + " ".join(feedback))
    if best is None:
        raise DirectorError("The AI's storyboard couldn't be used, even after asking again.")
    if similar is not None and similar(best)[0] >= ORIGINALITY_LIMIT:
        raise DirectorError("Every script the AI wrote was too close to a video you've made before. Try a "
                            "different prompt or angle.")
    best.notes.append("kept the closest of the AI's tries to the word target")
    log(f"Script written: \"{best.title}\", {len(best.scenes)} scenes, {best.word_count} words "
        f"(aimed for {aim}; the voice's speed makes up the difference).")
    return best


ORIGINALITY_LIMIT = 0.45  # share of 5-word runs shared with an earlier script (originality.py)


def script_writers(s) -> list[str]:
    """The AIs that may write scripts: only this PC's (Settings → Creator → Script writer: local), or the
    ones Settings → AI asks in turn."""
    from .. import ai

    order = ai.order(s)
    if getattr(s, "creator_ai", "local") == "local":
        if ai.key_for(s, "local"):
            return ["local"]
        return []
    return order


def no_writer_text(s) -> str:
    from .. import ai

    if getattr(s, "creator_ai", "local") == "local":
        from ..intelligence import llama_engine

        why = llama_engine.missing_backend().rstrip(". ")
        return (f"Scripts are written by the local AI, which isn't ready here ({why}). Paste your own script "
                "instead, or let the AI chosen in Settings → AI write them (Settings → Creator → Script writer).")
    return ai.missing_key(s)


# ---------------------------------------------------------------- your own script

def from_script(text: str, *, title: str = "", category: str = "", style: str = "", seconds: float = 0.0,
                prompt: str = "", max_scenes: int = 0) -> Storyboard:
    """A storyboard from a script you wrote: the first sentence is the hook, then a scene every one or two
    sentences (up to about 24 words), each picture described from its words and the style."""
    st = get_style(style or style_for(category, prompt or text[:200]))
    # Paragraphs are kept apart: a blank line always starts a new scene.
    paragraphs = [p for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]
    if not paragraphs:
        raise DirectorError("The script is empty.")
    scenes: list[Scene] = []
    first = True
    for para in paragraphs:
        parts = sentences(para)
        cur: list[str] = []
        for p in parts:
            if first:
                scenes.append(Scene(0, p))
                first = False
                continue
            if cur and len(" ".join(cur + [p]).split()) > 24:
                scenes.append(Scene(0, " ".join(cur)))
                cur = []
            cur.append(p)
        if cur:
            scenes.append(Scene(0, " ".join(cur)))
    sfx = {1: "bass_drop"}
    for i, sc in enumerate(scenes, 1):
        sc.keywords = keywords_of(sc.speech_text)
        sc.visual_prompt = ", ".join(sc.keywords) or "abstract"
        sc.camera_motion = MOTIONS[(i * 3) % len(MOTIONS)] if i > 1 else "slow_push_in"
        sc.sfx_cue = sfx.get(i, "whoosh" if i % 4 == 0 else "none")
        if "?" in sc.speech_text and i > 1:
            sc.sfx_cue = "riser" if sc.sfx_cue == "none" else sc.sfx_cue
    words = sum(len(sc.words) for sc in scenes)
    head = scenes[0].speech_text
    board = Storyboard(title=title or " ".join(head.split()[:10]).rstrip(".!?,"), category=category,
                       target_duration=int(round(seconds or words / DEFAULT_WPM * 60)), music_mood=st.mood,
                       scenes=scenes, hook=" ".join(head.split()[:7]).rstrip(".!?,"), caption=title or head,
                       hashtags=[re.sub(r"\W", "", w) for w in (category.split() + keywords_of(text, 3)) if
                                 re.sub(r"\W", "", w)][:5],
                       prompt=prompt or "your script", written_by="your script")
    # Your hook is kept as you wrote it.
    return normalise(board, st, hook_words=40, max_scenes=max_scenes or MAX_SCENES + 4)
