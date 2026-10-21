"""User settings, built-in presets, and the schema the GUI builds its forms from."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

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
    # Export
    aspect: str = "original"
    resolution: int = 1080
    encoder: str = "auto"
    video_quality: int = 20
    audio_bitrate: int = 192
    normalize_audio: bool = False
    fades: bool = True
    fast_copy: bool = False
    thumbnails: bool = True
    write_srt: bool = True
    # Captions
    burn_captions: bool = False
    caption_font: str = "Arial Black"
    caption_size: int = 70
    caption_color: str = "#FFFFFF"
    caption_highlight: str = "#FFD400"
    caption_outline: int = 5
    caption_position: str = "lower"
    caption_words: int = 3
    caption_uppercase: bool = True
    title_overlay: bool = False
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

    # ------------------------------------------------------------ helpers
    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()

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
        return cls().apply(data)

    def save(self, path: Path | None = None) -> None:
        path = path or app_dir() / "settings.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ---------------------------------------------------------------- presets

PRESETS: dict[str, dict] = {
    "Balanced highlights": {
        "aspect": "original", "min_len": 20, "max_len": 60, "clip_count": 5, "burn_captions": False,
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
        "aspect": "original", "min_len": 45, "max_len": 120, "clip_count": 5, "w_reactions": 0.35,
        "w_scenes": 0.0, "burn_captions": False, "mode": "auto",
    },
    "Gaming / action": {
        "aspect": "original", "min_len": 15, "max_len": 45, "clip_count": 8, "w_audio": 0.5,
        "w_scenes": 0.3, "mode": "auto",
    },
    "Square posts": {
        "aspect": "square", "min_len": 15, "max_len": 60, "clip_count": 5, "burn_captions": True,
        "caption_position": "lower", "mode": "auto",
    },
    "Split by chapters": {"mode": "chapters", "aspect": "original", "burn_captions": False},
    "AI picks (Claude)": {"mode": "ai", "min_len": 20, "max_len": 75, "clip_count": 6},
}


def user_presets_path() -> Path:
    return app_dir() / "presets.json"


def load_user_presets() -> dict[str, dict]:
    try:
        return json.loads(user_presets_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_user_preset(name: str, settings: Settings) -> None:
    presets = load_user_presets()
    keys = {f.name for f in SCHEMA if f.group in ("Clip finding", "Export", "Captions")} | {"mode"}
    presets[name] = {k: v for k, v in asdict(settings).items() if k in keys}
    user_presets_path().write_text(json.dumps(presets, indent=2), encoding="utf-8")


def all_presets() -> dict[str, dict]:
    return PRESETS | load_user_presets()


# ---------------------------------------------------------------- schema for the GUI


@dataclass
class Field:
    name: str
    label: str
    group: str
    kind: str  # bool | int | float | str | choice | path | color | secret | text
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

    Field("aspect", "Format", "Export", "choice", choices=ASPECTS),
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

    Field("burn_captions", "Burn in animated captions", "Captions", "bool"),
    Field("caption_font", "Font", "Captions", "str"),
    Field("caption_size", "Size", "Captions", "int", lo=20, hi=200),
    Field("caption_color", "Text colour", "Captions", "color"),
    Field("caption_highlight", "Active word colour", "Captions", "color"),
    Field("caption_outline", "Outline width", "Captions", "int", lo=0, hi=20),
    Field("caption_position", "Position", "Captions", "choice",
          choices=(("lower", "Lower third"), ("middle", "Middle"), ("upper", "Top"))),
    Field("caption_words", "Words on screen", "Captions", "int", lo=1, hi=10),
    Field("caption_uppercase", "UPPERCASE", "Captions", "bool"),
    Field("title_overlay", "Show clip title at top", "Captions", "bool"),

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
]
