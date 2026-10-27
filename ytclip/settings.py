"""User settings, built-in presets, and the schema the GUI builds its forms from."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from . import fonts
from .trending import CATEGORIES

APP_NAME = "ClipMonster"


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """Large downloaded models live in the local (non-roaming) profile."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".cache")
    path = Path(base) / APP_NAME / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    # General
    output_dir: str = str(Path.home() / "Videos" / APP_NAME)
    cache_dir: str = ""  # "" = <output_dir>/.downloads
    folder_template: str = "{channel}/{date} {title} [{id}]"
    file_template: str = "{rank:02d} - {time} - {label}"
    auto_export: bool = False
    # Updates
    check_updates: bool = True
    include_prereleases: bool = False
    update_repo: str = ""  # "" = the GitHub repo this build was published from
    # Download
    quality: int = 1080
    cookies_browser: str = ""
    keep_source: bool = True
    playlist_limit: int = 0
    # Clip finding
    mode: str = "auto"
    clip_count: int = 5
    min_len: float = 20.0
    max_len: float = 60.0
    min_gap: float = 5.0
    quality_floor: float = 0.45
    snap_to_pauses: bool = True
    pad_start: float = 0.0
    pad_end: float = 0.0
    trim_silence: bool = True  # start where the talking starts, end soon after it stops
    keywords: str = ""
    detect_scenes: bool = True
    w_heatmap: float = 0.55
    w_audio: float = 0.35
    w_reactions: float = 0.20
    w_scenes: float = 0.10
    w_keywords: float = 0.30
    # Transcription
    transcribe: str = "auto"
    whisper_model: str = "base"
    whisper_language: str = ""
    # AI
    ai_api_key: str = ""
    ai_model: str = "claude-opus-5-5"
    ai_instructions: str = (
        "Find self-contained moments that would work as standalone short-form clips: "
        "strong hooks, punchlines, surprising facts, hot takes, emotional peaks."
    )
    ai_polish: bool = False  # polish.py: one extra Claude call per video for hooks, caption fixes, crops
    ai_polish_model: str = "claude-sonnet-5-5"
    polish_burned_subs: str = "off"  # off = turn our captions off when the video already has subtitles | keep
    # Export
    aspect: str = "vertical_blur"
    resolution: int = 1080
    encoder: str = "auto"
    video_quality: int = 20
    audio_bitrate: int = 192
    normalize_audio: bool = False
    fades: bool = True
    fast_copy: bool = False
    thumbnails: bool = True
    write_srt: bool = True
    tiktok_ready: bool = True
    qc_enabled: bool = False  # qc.py: check every clip after export, moving bad ones to _rejected
    qc_min_rating: int = 5  # with AI polish: reject clips Claude rates under this (0 = ignore ratings)
    # Captions
    burn_captions: bool = False
    caption_font: str = "Montserrat Black"
    caption_size: int = 45  # letter (em) height in pixels on a 1080-wide frame
    caption_color: str = "#FFFFFF"
    caption_highlight: str = "#FFD400"
    caption_outline: int = 5
    caption_position: str = "lower"
    caption_words: int = 3
    caption_uppercase: bool = True
    title_overlay: bool = False
    caption_censor: bool = False
    # Layout (banners and stacked video)
    banner_top: bool = False
    banner_top_text: str = "{title}"
    banner_top_size: int = 10
    banner_bottom: bool = False
    banner_bottom_text: str = "Follow for part 2"
    banner_bottom_size: int = 8
    banner_mode: str = "bar"
    banner_bg: str = "#000000"
    banner_color: str = "#FFFFFF"
    banner_font: str = "TikTok Sans Black"
    banner_font_size: int = 70
    banner_uppercase: bool = True
    stack_enabled: bool = False
    stack_video: str = ""
    stack_folder: str = ""  # a folder of second videos: each clip gets a different one (stackpool.py)
    stack_position: str = "bottom"
    stack_split: float = 0.5
    stack_start: str = "random"
    stack_offset: float = 0.0
    stack_volume: int = 0
    layout_fit: str = "auto"
    # Research
    youtube_api_key: str = ""
    research_regions: str = "US, GB, CA, AU"
    research_category: str = ""
    research_per_region: int = 50
    research_min_minutes: float = 3.0
    research_deep_count: int = 12
    research_auto: bool = True
    research_interval: int = 30
    research_alert: int = 75
    # Picking rules (policy.py): which researched videos may be clipped without asking
    pick_from: str = "watchlist"
    pick_channels: str = ""
    pick_searches: str = ""
    pick_min_score: int = 65
    pick_min_minutes: float = 8.0
    pick_max_minutes: float = 180.0
    pick_max_age_days: float = 3.0
    pick_daily: int = 3
    pick_channel_weekly: int = 2
    pick_block_channels: str = ""
    pick_block_categories: str = "10, 1"  # Music, Film & Animation: reposts are usually claimed
    pick_block_words: str = "music video, lyric video, lyrics, official audio, trailer, full movie, full album"
    learn_from_results: bool = True  # learn.py: TikTok Manager's results rank channels and guide AI hooks
    # Automation (pipeline.py): the unattended runner
    auto_output_dir: str = ""  # "" = <output_dir>/Automation
    auto_preset: str = "Shorts / TikTok / Reels"
    auto_layout: str = ""  # "" = the layout in your settings
    auto_recipes: str = ""  # lines of "match = preset | view"
    auto_captions: bool = True
    auto_polish: bool = True
    auto_qc: bool = True
    auto_start_hour: int = 0  # the runner downloads and encodes between these hours (equal = any time)
    auto_end_hour: int = 0
    auto_gap_minutes: int = 10
    auto_ai_daily: int = 10
    auto_retries: int = 3
    auto_delete_sources: bool = True
    auto_keep_days: int = 14
    # 2 = text sizes are letter heights (1 = raw .ass sizes, which looked different in every font)
    text_units: int = 2

    # ------------------------------------------------------------ helpers
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()

    @property
    def auto_output_path(self) -> Path:
        return Path(self.auto_output_dir).expanduser() if self.auto_output_dir else self.output_path / "Automation"

    @property
    def cache_path(self) -> Path:
        return Path(self.cache_dir).expanduser() if self.cache_dir else self.output_path / ".downloads"

    @property
    def weights(self) -> dict[str, float]:
        return {"heatmap": self.w_heatmap, "audio": self.w_audio, "reactions": self.w_reactions,
                "scenes": self.w_scenes, "keywords": self.w_keywords}

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.keywords.split(",") if k.strip()]

    def copy(self, **changes) -> "Settings":
        return replace(self, **changes)

    def apply(self, overrides: dict) -> "Settings":
        known = {f.name for f in fields(self)}
        clean = {k: v for k, v in overrides.items() if k in known}
        for key in ("ai_api_key", "youtube_api_key"):
            if isinstance(clean.get(key), str):
                clean[key] = clean[key].strip()  # pasted keys often carry a stray space
        return replace(self, **clean)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_dir() / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls().apply(migrate(data))

    def save(self, path: Path | None = None) -> None:
        path = path or app_dir() / "settings.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ---------------------------------------------------------------- old files

_SIZED = (("caption_size", "caption_font", "Montserrat Black"), ("banner_font_size", "banner_font", "TikTok Sans Black"))


def migrate(data: dict) -> dict:
    """Bring settings, presets and saved views from older versions up to date. Text sizes used to be
    raw .ass font sizes; they're now letter heights, so convert them and the text looks exactly as it did."""
    if not isinstance(data, dict) or data.get("text_units", 1) >= 2:
        return data
    data = dict(data)
    for size_key, font_key, default_font in _SIZED:
        if isinstance(data.get(size_key), (int, float)):
            font = data.get(font_key) or default_font
            data[size_key] = max(1, round(data[size_key] * fonts.metrics(str(font)).em_ratio))
    data["text_units"] = 2
    return data


# ---------------------------------------------------------------- presets

PRESETS: dict[str, dict] = {
    "Balanced highlights": {
        "aspect": "vertical_blur", "min_len": 20, "max_len": 60, "clip_count": 5, "burn_captions": False,
        "mode": "auto",
    },
    "Shorts / TikTok / Reels": {
        "aspect": "vertical_blur", "min_len": 15, "max_len": 58, "clip_count": 6, "burn_captions": True,
        "caption_position": "lower", "normalize_audio": True, "title_overlay": False, "mode": "auto",
    },
    "Vertical crop (talking head)": {
        "aspect": "vertical_crop", "min_len": 15, "max_len": 58, "clip_count": 6, "burn_captions": True,
        "normalize_audio": True, "mode": "auto",
    },
    "Podcast moments": {
        "aspect": "vertical_blur", "min_len": 45, "max_len": 120, "clip_count": 5, "w_reactions": 0.35,
        "w_scenes": 0.0, "burn_captions": False, "mode": "auto",
    },
    "Gaming / action": {
        "aspect": "vertical_blur", "min_len": 15, "max_len": 45, "clip_count": 8, "w_audio": 0.5,
        "w_scenes": 0.3, "mode": "auto",
    },
    "Square posts": {
        "aspect": "square", "min_len": 15, "max_len": 60, "clip_count": 5, "burn_captions": True,
        "caption_position": "lower", "mode": "auto", "tiktok_ready": False,  # 1:1 is for Instagram/X
    },
    "Stacked (gameplay below)": {
        "aspect": "vertical_crop", "min_len": 15, "max_len": 58, "clip_count": 6, "burn_captions": True,
        "caption_position": "middle", "normalize_audio": True, "stack_enabled": True, "stack_position": "bottom",
        "stack_split": 0.5, "banner_top": False, "banner_bottom": False, "mode": "auto",
    },
    "Banner top + bottom": {
        "aspect": "vertical_blur", "min_len": 15, "max_len": 58, "clip_count": 6, "burn_captions": True,
        "caption_position": "lower", "normalize_audio": True, "banner_top": True, "banner_bottom": True,
        "banner_mode": "bar", "stack_enabled": False, "title_overlay": False, "mode": "auto",
    },
    "Split by chapters": {"mode": "chapters", "aspect": "vertical_blur", "burn_captions": False},
    "AI picks (Claude)": {"mode": "ai", "min_len": 20, "max_len": 75, "clip_count": 6},
}
# Every built-in that picks a format also says whether it's for TikTok, so choosing "Square posts" and
# then another preset doesn't leave TikTok-ready output switched off.
for _values in PRESETS.values():
    if "aspect" in _values:
        _values.setdefault("tiktok_ready", True)


def user_presets_path() -> Path:
    return app_dir() / "presets.json"


def load_user_presets() -> dict[str, dict]:
    try:
        data = json.loads(user_presets_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: migrate(v) for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save_user_preset(name: str, settings: Settings) -> None:
    presets = load_user_presets()
    keys = {f.name for f in SCHEMA if f.group in ("Clip finding", "Export", "Captions", "Layout")} | {"mode"}
    presets[name] = {k: v for k, v in asdict(settings).items() if k in keys | {"text_units"}}
    user_presets_path().write_text(json.dumps(presets, indent=2), encoding="utf-8")


def all_presets() -> dict[str, dict]:
    return PRESETS | load_user_presets()


# ---------------------------------------------------------------- layout ("view") presets
# Top/bottom views only: banners, the stacked video and their look. Picking one replaces the layout and
# leaves clip finding, format and captions alone. Built-ins keep your chosen second video.

LAYOUT_PRESETS: dict[str, dict] = {
    "Clip only": {"banner_top": False, "banner_bottom": False, "stack_enabled": False},
    "Title on top": {
        "banner_top": True, "banner_top_text": "{title}", "banner_bottom": False, "stack_enabled": False,
        "banner_mode": "bar",
    },
    "Title top + follow bottom": {
        "banner_top": True, "banner_top_text": "{title}", "banner_bottom": True,
        "banner_bottom_text": "Follow for part 2", "stack_enabled": False, "banner_mode": "bar",
    },
    "Text over video": {
        "banner_top": True, "banner_top_text": "{title}", "banner_bottom": True,
        "banner_bottom_text": "Follow for part 2", "stack_enabled": False, "banner_mode": "overlay",
    },
    "Meme (white bar)": {
        "banner_top": True, "banner_top_text": "{title}", "banner_bottom": False, "stack_enabled": False,
        "banner_mode": "bar", "banner_bg": "#FFFFFF", "banner_color": "#000000", "banner_uppercase": False,
        "banner_font": "Archivo Black", "banner_top_size": 14,
    },
    "Gameplay below": {
        "stack_enabled": True, "stack_position": "bottom", "stack_split": 0.5, "banner_top": False,
        "banner_bottom": False,
    },
    "Gameplay below + title": {
        "stack_enabled": True, "stack_position": "bottom", "stack_split": 0.5, "banner_top": True,
        "banner_top_text": "{title}", "banner_bottom": False, "banner_mode": "bar",
    },
    "Gameplay above": {
        "stack_enabled": True, "stack_position": "top", "stack_split": 0.5, "banner_top": False,
        "banner_bottom": False,
    },
    "Big clip, small gameplay": {
        "stack_enabled": True, "stack_position": "bottom", "stack_split": 0.65, "banner_top": False,
        "banner_bottom": False,
    },
}


def layout_keys() -> set[str]:
    return {f.name for f in SCHEMA if f.group == "Layout"}


def user_layouts_path() -> Path:
    return app_dir() / "layout_presets.json"


def load_user_layouts() -> dict[str, dict]:
    try:
        data = json.loads(user_layouts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: migrate(v) for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write_user_layouts(layouts: dict[str, dict]) -> None:
    user_layouts_path().write_text(json.dumps(layouts, indent=2), encoding="utf-8")


def save_user_layout(name: str, settings: Settings) -> None:
    """Save the whole current layout (including the second video and fonts) under `name`."""
    layouts = load_user_layouts()
    keys = layout_keys()
    layouts[name] = {k: v for k, v in asdict(settings).items() if k in keys | {"text_units"}}
    _write_user_layouts(layouts)


def delete_user_layout(name: str) -> None:
    layouts = load_user_layouts()
    if layouts.pop(name, None) is not None:
        _write_user_layouts(layouts)


def all_layouts() -> dict[str, dict]:
    return LAYOUT_PRESETS | load_user_layouts()


def find_layout(name: str) -> str | None:
    """Exact name first, then the first name starting with it (case-insensitive), for the CLI."""
    layouts = all_layouts()
    if name in layouts:
        return name
    low = name.lower()
    return next((k for k in layouts if k.lower() == low), None) or \
        next((k for k in layouts if k.lower().startswith(low)), None)


# ---------------------------------------------------------------- schema for the GUI


@dataclass
class Field:
    name: str
    label: str
    group: str
    kind: str  # bool | int | float | str | choice | multi | path | file | font | color | secret | text
    choices: tuple = ()
    lo: float = 0
    hi: float = 100
    step: float = 1
    help: str = ""
    placeholder: str = ""


MODES = (("auto", "Smart signals"), ("ai", "AI picks (Claude)"), ("chapters", "YouTube chapters"))
ASPECTS = (("original", "Original"), ("vertical_blur", "9:16 vertical, blurred fill"),
           ("vertical_crop", "9:16 vertical, cropped"), ("square", "1:1 square"))
ENCODERS = (("auto", "Auto (fastest available)"), ("libx264", "CPU (x264)"), ("h264_amf", "AMD GPU (AMF)"),
            ("h264_nvenc", "NVIDIA GPU (NVENC)"), ("h264_qsv", "Intel GPU (Quick Sync)"))

PICK_FROM = (
    ("watchlist", "My channel list and Creative Commons only"),
    ("cc", "Creative Commons only"),
    ("any", "Anything that passes the rules"),
)

BANNER_MODES = (("bar", "Solid bar (video sits between)"), ("overlay", "Text over the video"))
LAYOUT_FITS = (("auto", "Auto (from Format)"), ("crop", "Crop to fill"), ("blur", "Fit, blurred fill"),
               ("fit", "Fit, banner colour fill"))

SCHEMA: list[Field] = [
    Field("output_dir", "Output folder", "General", "path", help="Where finished clips are saved"),
    Field("cache_dir", "Download cache", "General", "path", help="Where full videos are kept. Empty = <output>/.downloads"),
    Field("folder_template", "Folder naming", "General", "str",
          help="Fields: {channel} {date} {title} {id}"),
    Field("file_template", "Clip naming", "General", "str",
          help="Fields: {rank} {time} {label} {score} - e.g. {rank:02d} - {label}"),
    Field("auto_export", "Export automatically after analysis", "General", "bool",
          help="Hands-free batch mode: clips are exported as soon as a video is analysed"),

    Field("check_updates", "Check for updates automatically", "Updates", "bool",
          help="Looks for a new release on GitHub about once a day"),
    Field("include_prereleases", "Include beta versions", "Updates", "bool"),
    Field("update_repo", "Update source (GitHub owner/repo)", "Updates", "str",
          help="Leave blank to use the repository this build was published from"),

    Field("quality", "Max download resolution", "Download", "choice",
          choices=((2160, "2160p (4K)"), (1440, "1440p"), (1080, "1080p"), (720, "720p"), (480, "480p"), (360, "360p"))),
    Field("cookies_browser", "Use browser login", "Download", "choice",
          choices=(("", "None"), ("chrome", "Chrome"), ("edge", "Edge"), ("firefox", "Firefox"), ("brave", "Brave")),
          help="Needed for age-restricted or members-only videos"),
    Field("keep_source", "Keep full downloads", "Download", "bool"),
    Field("playlist_limit", "Max videos per playlist (0 = all)", "Download", "int", lo=0, hi=500),

    Field("mode", "Clip finder", "Clip finding", "choice", choices=MODES),
    Field("clip_count", "Clips per video", "Clip finding", "int", lo=1, hi=50),
    Field("min_len", "Min clip length (s)", "Clip finding", "float", lo=3, hi=600, step=1),
    Field("max_len", "Max clip length (s)", "Clip finding", "float", lo=5, hi=1800, step=1),
    Field("min_gap", "Min gap between clips (s)", "Clip finding", "float", lo=0, hi=120, step=1),
    Field("quality_floor", "Drop clips weaker than (x best)", "Clip finding", "float", lo=0, hi=1, step=0.05,
          help="0 keeps everything; 0.5 drops clips scoring under half the best clip"),
    Field("snap_to_pauses", "Snap edges to pauses", "Clip finding", "bool"),
    Field("pad_start", "Extra lead-in (s)", "Clip finding", "float", lo=0, hi=10, step=0.5),
    Field("pad_end", "Extra tail (s)", "Clip finding", "float", lo=0, hi=10, step=0.5),
    Field("trim_silence", "Trim dead air", "Clip finding", "bool",
          help="Clips start just before the first word and end soon after the last, instead of on silence"),
    Field("keywords", "Boost keywords", "Clip finding", "str",
          help="Comma separated words or phrases; moments mentioning them score higher"),
    Field("detect_scenes", "Detect scene cuts", "Clip finding", "bool",
          help="Finds fast-cut, high-energy sections. Adds some analysis time"),
    Field("w_heatmap", "Weight: most replayed", "Clip finding", "float", lo=0, hi=1, step=0.05),
    Field("w_audio", "Weight: audio excitement", "Clip finding", "float", lo=0, hi=1, step=0.05),
    Field("w_reactions", "Weight: laughter/applause", "Clip finding", "float", lo=0, hi=1, step=0.05),
    Field("w_scenes", "Weight: scene cuts", "Clip finding", "float", lo=0, hi=1, step=0.05),
    Field("w_keywords", "Weight: keywords", "Clip finding", "float", lo=0, hi=1, step=0.05),

    Field("transcribe", "Local transcription (Whisper)", "Transcription", "choice",
          choices=(("auto", "Only when YouTube has no captions"), ("always", "Always"), ("never", "Never"))),
    Field("whisper_model", "Whisper model", "Transcription", "choice",
          choices=(("tiny", "tiny (fastest)"), ("base", "base"), ("small", "small"), ("medium", "medium"),
                   ("large-v3", "large-v3 (best, slow)")),
          help="Downloaded once on first use"),
    Field("whisper_language", "Language code (blank = detect)", "Transcription", "str"),

    Field("ai_api_key", "Anthropic API key", "AI", "secret", help="Needed for the AI clip finder",
          placeholder="sk-ant-…"),
    Field("ai_model", "Claude model", "AI", "choice",
          choices=(("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                   ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)"))),
    Field("ai_instructions", "What makes a good clip?", "AI", "text"),
    Field("ai_polish", "Polish clips with AI", "AI", "bool",
          help="One extra Claude call per video, with a frame from each clip: writes a hook for the top banner, "
               "fixes misheard words in the captions, spots subtitles already in the video and centres "
               "vertical crops on the speaker"),
    Field("ai_polish_model", "Model for polishing", "AI", "choice",
          choices=(("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                   ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)"))),
    Field("polish_burned_subs", "If the video already has subtitles", "AI", "choice",
          choices=(("off", "Turn my captions off for it"), ("keep", "Keep my captions anyway")),
          help="Two sets of words on screen at once is hard to read"),

    Field("aspect", "Format", "Export", "choice", choices=ASPECTS,
          help="With TikTok-ready output on, Original and 1:1 are fitted into a 9:16 frame with a blurred fill"),
    Field("resolution", "Resolution (short side)", "Export", "choice",
          choices=((2160, "2160"), (1440, "1440"), (1080, "1080"), (720, "720"), (480, "480"))),
    Field("encoder", "Encoder", "Export", "choice", choices=ENCODERS),
    Field("video_quality", "Quality (lower = better, 14-30)", "Export", "int", lo=10, hi=40),
    Field("audio_bitrate", "Audio bitrate (kbps)", "Export", "int", lo=64, hi=320, step=32),
    Field("normalize_audio", "Normalise loudness (-14 LUFS)", "Export", "bool"),
    Field("fades", "Audio fade in/out", "Export", "bool"),
    Field("fast_copy", "Fast copy (no re-encode, original format only)", "Export", "bool"),
    Field("thumbnails", "Save thumbnails", "Export", "bool"),
    Field("write_srt", "Save .srt subtitles per clip", "Export", "bool"),
    Field("tiktok_ready", "TikTok-ready output (1080×1920)", "Export", "bool",
          help="Every clip comes out 9:16 at TikTok's size (1080×1920 at the 1080 setting, never above it, never "
               "under 540×960), keeps text out of TikTok's buttons and captions, caps frame rate at 60 and bitrate "
               "at 20 Mbps, uses 44.1 kHz audio, and warns about clips over 10 minutes or 287 MB"),
    Field("qc_enabled", "Check clips after export", "Export", "bool",
          help="Watches each clip once for silence, black or frozen picture and the wrong length or size, and "
               "notes clips that start or end mid-sentence. Failed clips move to a _rejected folder"),
    Field("qc_min_rating", "Reject AI ratings under", "Export", "int", lo=0, hi=10,
          help="With AI polish on, Claude rates how well each clip stands alone, 1 to 10. 0 ignores the ratings"),

    Field("burn_captions", "Burn in animated captions", "Captions", "bool"),
    Field("caption_font", "Font", "Captions", "font"),
    Field("caption_size", "Size", "Captions", "int", lo=12, hi=160,
          help="Letter height in pixels on a 1080-wide video. The same size looks the same in every font"),
    Field("caption_color", "Text colour", "Captions", "color"),
    Field("caption_highlight", "Active word colour", "Captions", "color"),
    Field("caption_outline", "Outline width", "Captions", "int", lo=0, hi=20),
    Field("caption_position", "Position", "Captions", "choice",
          choices=(("lower", "Lower third"), ("middle", "Middle"), ("upper", "Top"))),
    Field("caption_words", "Words on screen", "Captions", "int", lo=1, hi=10),
    Field("caption_uppercase", "UPPERCASE", "Captions", "bool"),
    Field("title_overlay", "Show clip title at top", "Captions", "bool"),
    Field("caption_censor", "Bleep strong swearing", "Captions", "bool",
          help="Writes f*ck, sh*t and the like in captions, which TikTok is less likely to hold back"),

    Field("banner_top", "Top banner", "Layout", "bool", help="A bar of text above the video"),
    Field("banner_top_text", "Top text", "Layout", "str",
          help="{title} is replaced with the clip title. Each clip can override this in the Layout tab"),
    Field("banner_top_size", "Top banner height (%)", "Layout", "int", lo=4, hi=35,
          help="Height of the text area. With TikTok-ready output the bar also covers TikTok's tabs above it"),
    Field("banner_bottom", "Bottom banner", "Layout", "bool", help="A bar of text below the video"),
    Field("banner_bottom_text", "Bottom text", "Layout", "str", help="{title} is replaced with the clip title"),
    Field("banner_bottom_size", "Bottom banner height (%)", "Layout", "int", lo=4, hi=35,
          help="Height of the text area. With TikTok-ready output the bar sits above TikTok's caption and "
               "fills the strip behind it, so no video is hidden. Pick 'Text over the video' to keep that video"),
    Field("banner_mode", "Banner style", "Layout", "choice", choices=BANNER_MODES),
    Field("banner_bg", "Banner colour", "Layout", "color"),
    Field("banner_color", "Banner text colour", "Layout", "color"),
    Field("banner_font", "Banner font", "Layout", "font"),
    Field("banner_font_size", "Banner text size", "Layout", "int", lo=12, hi=160,
          help="Letter height in pixels on a 1080-wide video. Long text shrinks to fit the banner"),
    Field("banner_uppercase", "Banner UPPERCASE", "Layout", "bool"),
    Field("stack_enabled", "Stack a second video", "Layout", "bool",
          help="Splits the frame: your clip on one half, a looping background video on the other"),
    Field("stack_video", "Second video", "Layout", "file",
          help="Gameplay, satisfying clips, b-roll... It loops if it's shorter than the clip"),
    Field("stack_folder", "Or a folder of them", "Layout", "path",
          help="Each clip gets a different video from this folder, never the same one twice in a row. "
               "Used instead of the single second video when it's set"),
    Field("stack_position", "Second video goes", "Layout", "choice",
          choices=(("bottom", "Below the clip"), ("top", "Above the clip"))),
    Field("stack_split", "Clip share of the frame", "Layout", "float", lo=0, hi=1, step=0.05,
          help="0.50 = equal halves; 0.60 gives the clip more room"),
    Field("stack_start", "Second video starts", "Layout", "choice",
          choices=(("random", "At a random spot per clip"), ("fixed", "At a fixed time"))),
    Field("stack_offset", "Fixed start (s)", "Layout", "float", lo=0, hi=36000, step=1),
    Field("stack_volume", "Second video volume (%)", "Layout", "int", lo=0, hi=100, step=5,
          help="0 keeps it silent so only the clip is heard"),
    Field("layout_fit", "Fit the clip", "Layout", "choice", choices=LAYOUT_FITS,
          help="How the clip fills its part of the frame"),
    Field("youtube_api_key", "YouTube Data API key", "Research", "secret", placeholder="AIza…",
          help="Free from the Google Cloud console. Each region checked uses 1 of 10,000 daily units"),
    Field("research_regions", "Regions", "Research", "str",
          help="Two-letter country codes, comma separated, e.g. US, GB, CA"),
    Field("research_category", "Category", "Research", "choice", choices=tuple(CATEGORIES.items())),
    Field("research_per_region", "Videos per region", "Research", "int", lo=10, hi=200, step=10,
          help="YouTube's charts go up to 200; every 50 costs 1 quota unit"),
    Field("research_min_minutes", "Skip videos shorter than (min)", "Research", "float", lo=0, hi=120, step=1,
          help="Short videos have little to cut. 0 keeps everything"),
    Field("research_deep_count", "Most-replayed checks per scan", "Research", "int", lo=0, hi=50,
          help="The best candidates get their Most replayed graph read (a few seconds each, no quota)"),
    Field("research_auto", "Keep checking while ClipMonster is open", "Research", "bool"),
    Field("research_interval", "Check every (minutes)", "Research", "int", lo=10, hi=720, step=5),
    Field("research_alert", "Highlight new videos scoring at least", "Research", "int", lo=0, hi=100,
          help="New arrivals above this score are counted on the Research tab"),

    Field("pick_from", "Pick videos from", "Picking", "choice", choices=PICK_FROM,
          help="Creators on your channel list, or Creative Commons videos, have said clips are fine. "
               "'Anything' also picks trending videos nobody gave permission for, which risks copyright strikes"),
    Field("pick_channels", "Channel list", "Picking", "text",
          help="One channel per line: @handle, channel link or channel id. Add a note after #, e.g. "
               "'@somepodcast  # clipping programme, credit in caption'. Each channel costs about 1 quota "
               "unit an hour",
          placeholder="@somepodcast  # clipping programme, credit in caption\nhttps://www.youtube.com/@streamer"),
    Field("pick_searches", "Creative Commons searches", "Picking", "text",
          help="One search per line, e.g. 'gaming highlights'. Each search costs 100 quota units and runs "
               "at most every 6 hours",
          placeholder="gaming highlights\nscience lecture"),
    Field("pick_min_score", "Lowest clip-ability to pick", "Picking", "int", lo=0, hi=100),
    Field("pick_min_minutes", "Shortest video (min)", "Picking", "float", lo=0, hi=120, step=1),
    Field("pick_max_minutes", "Longest video (min, 0 = any)", "Picking", "float", lo=0, hi=1200, step=10,
          help="Very long streams take a long time to download and analyse"),
    Field("pick_max_age_days", "Oldest video (days, 0 = any)", "Picking", "float", lo=0, hi=60, step=1),
    Field("pick_daily", "Videos picked per day", "Picking", "int", lo=0, hi=50,
          help="Counted over the last 24 hours. Each video makes several clips"),
    Field("pick_channel_weekly", "Most picks per channel per week (0 = any)", "Picking", "int", lo=0, hi=20,
          help="Keeps your feed from being one channel's content"),
    Field("pick_block_channels", "Never pick these channels", "Picking", "text",
          help="One per line: channel name, @handle, link or id", placeholder="Some Channel\n@another"),
    Field("pick_block_categories", "Never pick these categories", "Picking", "multi",
          choices=tuple((cid, name) for cid, name in CATEGORIES.items() if cid)),
    Field("pick_block_words", "Skip titles containing", "Picking", "str",
          help="Comma separated words or phrases"),
    Field("learn_from_results", "Learn from how my clips do", "Picking", "bool",
          help="TikTok Manager shares the views your posted clips get. Channels whose clips beat your usual "
               "views rank higher, ones that flop rank lower, and the AI polish sees which hooks worked"),

    Field("auto_output_dir", "Clips go to", "Automation", "path",
          help="Empty = an Automation folder inside your output folder"),
    Field("auto_preset", "Style (preset)", "Automation", "choice",
          choices=(("", "My current settings"),) + tuple((k, k) for k in PRESETS),
          help="How clips are found and exported, unless a recipe below says otherwise"),
    Field("auto_layout", "Top/bottom view", "Automation", "choice",
          choices=(("", "As in my settings"),) + tuple((k, k) for k in LAYOUT_PRESETS)),
    Field("auto_recipes", "Recipes", "Automation", "text",
          help="Per category or channel: 'what = preset | view'. 'what' is a category name, a channel name or "
               "@handle, or * for everything else. The first matching line wins",
          placeholder="Gaming = Gaming / action | Gameplay below\n@somepodcast = Podcast moments | Title on top"),
    Field("auto_captions", "Always burn in captions", "Automation", "bool"),
    Field("auto_qc", "Check clips after export", "Automation", "bool",
          help="Clips that fail (silent, black, frozen, wrong length, or rated low by the AI polish) go to a "
               "_rejected folder instead of being ready to upload"),
    Field("auto_polish", "Polish clips with AI", "Automation", "bool",
          help="Hooks, caption fixes and speaker crops (see Settings → AI). Needs an Anthropic key and "
               "counts toward the AI limit below"),
    Field("auto_start_hour", "Work from (hour)", "Automation", "int", lo=0, hi=23,
          help="Downloads and encodes only start between these hours. The same hour twice means any time"),
    Field("auto_end_hour", "Work until (hour)", "Automation", "int", lo=0, hi=23),
    Field("auto_gap_minutes", "Minutes between downloads", "Automation", "int", lo=0, hi=240, step=5,
          help="Spacing downloads out makes YouTube less likely to ask the PC to sign in"),
    Field("auto_ai_daily", "AI calls per day", "Automation", "int", lo=0, hi=500,
          help="Finding clips with AI and polishing each count as one. Over the limit, videos use the smart "
               "signals and skip polishing, so Claude costs stay predictable"),
    Field("auto_retries", "Tries per video", "Automation", "int", lo=1, hi=10,
          help="A failed video is tried again after 15 minutes, then 1 hour, then 4 hours"),
    Field("auto_delete_sources", "Delete full downloads after clipping", "Automation", "bool"),
    Field("auto_keep_days", "Clear cached analysis after (days)", "Automation", "int", lo=1, hi=365,
          help="Transcripts and measurements of finished videos are removed from the download cache"),
]
