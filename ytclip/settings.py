"""User settings, built-in presets, and the schema the GUI builds its forms from."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import fonts
from .trending import CATEGORIES

APP_NAME = "ClipMonster"


MAC = sys.platform == "darwin"
# A Mac keeps an app's files in ~/Library: settings and models in Application Support, scratch work in Caches.
MAC_SUPPORT = Path.home() / "Library" / "Application Support"
MAC_CACHES = Path.home() / "Library" / "Caches"


def app_dir() -> Path:
    """%APPDATA%\\ClipMonster on Windows, ~/Library/Application Support/ClipMonster on a Mac, ~/.config/ClipMonster
    on Linux: settings, presets, the session and automation's state."""
    base = os.environ.get("APPDATA") or str(MAC_SUPPORT if MAC else Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_dir(*parts: str) -> Path:
    """%LOCALAPPDATA%\\ClipMonster (or a folder in it): this PC's own big or short-lived files, never
    synced or roamed: models, updates, the previous version and temporary work. On a Mac they go beside the
    settings in Application Support (which nothing tidies away, unlike Caches: a model is gigabytes)."""
    base = os.environ.get("LOCALAPPDATA") or str(MAC_SUPPORT if MAC else Path.home() / ".cache")
    path = Path(base, APP_NAME, *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def models_dir() -> Path:
    """Large downloaded models live in the local (non-roaming) profile."""
    return local_dir("models")


def work_dir() -> Path:
    """Where every step's temporary files go (cleanup.sweep_work empties what a crash left behind). On a Mac,
    in ~/Library/Caches, where scratch files belong."""
    if MAC and not os.environ.get("LOCALAPPDATA"):
        path = MAC_CACHES / APP_NAME / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return local_dir("work")


# The keys in settings.json (Claude, Gemini, YouTube, Telegram). ClipMonster for Mac keeps them in the Mac's
# Keychain instead (keep_secrets_in): settings.json then holds them blank, and Settings.load fills them in.
SECRETS = ("ai_api_key", "gemini_api_key", "youtube_api_key", "telegram_token", "api_football_key",
           "elevenlabs_api_key", "pexels_api_key", "pixabay_api_key")
_secret_store = None


def keep_secrets_in(store) -> None:
    """`store` has get(name) -> str and set(name, value) (value "" forgets it); None: settings.json again."""
    global _secret_store
    _secret_store = store


def work_folder(prefix: str) -> "tempfile.TemporaryDirectory[str]":
    """A temporary folder in work_dir(), removed when the `with` ends. A file Windows still has open
    can't go then; cleanup.sweep_work clears it on the next start instead of letting it fail the step."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=work_dir(), ignore_cleanup_errors=True)


@dataclass
class Settings:
    # General
    output_dir: str = str(Path.home() / ("Movies" if MAC else "Videos") / APP_NAME)
    cache_dir: str = ""  # "" = <output_dir>/.downloads
    folder_template: str = "{channel}/{date} {title} [{id}]"
    file_template: str = "{rank:02d} - {time} - {label}"
    auto_export: bool = False
    setup_done: bool = False  # the first-run setup was finished (Settings → General → Set up again reopens it)
    # Updates
    check_updates: bool = True
    update_channel: str = "stable"  # stable | beta | dev (updater.CHANNELS)
    auto_update: bool = True  # download updates by themselves; automation installs them when it's idle
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
    series_parts: int = 3  # a great moment longer than one clip carries on in up to this many parts (1 = off)
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
    w_voice: float = 0.25  # how animated the voice is (intelligence/viral_heuristics.py)
    w_emotion: float = 0.20  # how strong and swinging the words' sentiment is
    sport: str = ""  # a sports category's sport (sports.py): its own signals and whole-play clips; "" = not sports
    # Transcription
    transcribe: str = "auto"  # auto | clips | always | never
    whisper_model: str = "base"
    whisper_language: str = ""
    # Speed (speed.py): how much of the PC clipping may use
    speed: str = "balanced"  # quiet | balanced | full
    gpu_whisper: bool = True  # Full power: Whisper on the graphics card (whisper_cpp.py), when there is one
    # AI (ai.py): Claude or Gemini, each with its own key and models
    ai_provider: str = "claude"  # claude | gemini
    ai_fallback: bool = True  # when the chosen AI fails or has no key, ask the other one (if it has a key)
    ai_api_key: str = ""  # Anthropic's, for Claude
    ai_model: str = "claude-opus-5-5"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    # Local AI (intelligence/llama_engine.py): a GGUF model on this PC, when "AI to use" is Local AI
    local_model: str = "auto"  # auto | a key of intelligence/models.MODELS | custom
    local_model_path: str = ""  # with custom: your own .gguf file
    local_gpu: bool = True  # put the model on the graphics card when there's one with room
    local_context: int = 0  # tokens the model reads at once; 0 = sized to the model and this PC (up to 32,768)
    local_gpu_layers: int = -1  # layers on the graphics card; -1 = as many as fit (intelligence/planner.py)
    gemini_polish_model: str = "gemini-3.8-flash"
    ai_instructions: str = (
        "Find self-contained moments that would work as standalone short-form clips: "
        "strong hooks, punchlines, surprising facts, hot takes, emotional peaks."
    )
    ai_polish: bool = False  # polish.py: one extra AI call per video for hooks, caption fixes, crops
    ai_polish_model: str = "claude-sonnet-5-5"
    polish_overpick: bool = True  # with polish: find extra clips, keep the best rated (engine.keep_best)
    polish_burned_subs: str = "off"  # off = turn our captions off when the video already has subtitles | keep
    polish_bottom_text: bool = True  # the polish writes a bottom line for each clip with no part 2
    # The AI router and its budget (intelligence/agent_router.py, usage.py)
    ai_budget_day: float = 0.0  # US dollars a day the paid AIs may cost; 0 = no limit
    ai_budget_month: float = 0.0  # and a calendar month
    ai_local_timeout: int = 240  # seconds the local AI gets before the router asks a cloud AI instead
    # Sports event search (sportsdesk/semantic_search.py)
    event_search_model: str = "auto"  # auto | minilm (all-MiniLM-L6-v2, ONNX) | words (no model: word matching)
    event_feed: str = "auto"  # play-by-play feed: auto | espn | api-football | off
    api_football_key: str = ""  # api-football.com (free tier: 100 requests a day): soccer cards, goals, VAR
    event_pad_before: float = 12.0  # seconds kept before the event (the build-up)
    event_pad_after: float = 25.0  # and after it (the card, the reaction, the first replay)
    reel_transitions: str = "mix"  # reel.STYLES: mix | dissolve | whip | zoom
    reel_transition_seconds: float = 0.3
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
    qc_min_rating: int = 5  # with AI polish: reject clips the AI rates under this (0 = ignore ratings)
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
    caption_style: str = "classic"  # classic | kinetic (rendering/ass_builder.py)
    caption_keyword_colours: bool = True  # kinetic: money green, danger red, questions yellow
    caption_money_colour: str = "#00FF66"
    caption_danger_colour: str = "#FF3B30"
    caption_question_colour: str = "#FFE600"
    # Editing (rendering/plan.py): camera, cuts, sound and context, done in the export
    track_speaker: bool = False  # follow the person talking (vision/face_tracker.py) instead of a fixed crop
    camera_smoothing: float = 0.08  # how quickly the camera follows: the EMA's alpha per frame at 30 fps
    camera_deadband: float = 0.04  # movements smaller than this share of the frame don't move the camera
    punch_zoom: bool = False  # alternate 1.00x / punch_scale at each new sentence
    punch_scale: float = 1.08
    slow_zoom: bool = False  # slow push-in for stories and curiosity clips
    slow_zoom_scale: float = 1.15
    cut_silences: bool = False  # pauses longer than silence_longest shrink to silence_keep
    silence_longest: float = 0.40
    silence_keep: float = 0.12
    cut_fillers: bool = False  # um, uh, and fillers set apart by pauses
    filler_words: str = "um, uh, erm, hmm, you know, i mean, like, literally, basically"
    bleep_audio: bool = False  # bleep strong swearing in the sound too
    bleep_tone: str = "tone"  # tone (an 800 Hz beep) | mute
    master_audio: bool = False  # 80 Hz high-pass, speech compressor, two-pass -14 LUFS / -1 dBTP
    music_folder: str = ""  # your royalty-free or licensed music, in mood folders (audio/ducking.py)
    music_level_db: int = -18
    hook_intro: bool = False  # an editor's headline over the first moments (rendering/cards.py)
    intro_seconds: float = 1.8
    lower_third: bool = False  # a context tag: the topic, and the source credited
    lower_third_text: str = "{topic}"
    broll_folder: str = ""  # your own licensed B-roll clips (vision/broll_selector.py)
    broll_every: float = 18.0
    broll_seconds: float = 2.5
    creator_rewards: bool = False  # clips 62-88 s (over a minute after cuts), for TikTok's Creator Rewards
    # Video Creator (creator/): original videos made from a prompt, on this PC
    creator_ai: str = "local"  # who writes scripts: local (this PC's AI only) | any (Settings → AI's order)
    creator_web: bool = True  # look the topic up online (news, Wikipedia) before the script is written
    creator_factcheck: str = "fix"  # check the script against what was found: fix | flag | off
    creator_review: str = "new"  # Creator videos wait for your OK before posting: new (first 2 weeks) | always | off
    creator_schedule_when: str = "night"  # the categories' daily Creator videos: night (from 1 am) | now
    creator_voice: str = "en_male_deep"  # creator/voice_engine.VOICES
    creator_tts: str = "auto"  # auto | kokoro | piper | windows | espeak
    creator_kokoro_model: str = "full"  # full (310 MB, best) | compact (88 MB)
    creator_word_timing: str = "whisper"  # whisper (listen to the voice) | sound (from its pauses and syllables)
    creator_duration: int = 65  # seconds of video it aims for
    creator_rewards_length: bool = True  # keep videos 62-85 s, over the minute Creator Rewards needs
    creator_style: str = ""  # creator/director.STYLES key; "" = from the category
    creator_visuals: str = "auto"  # auto | comfyui | sdxl_turbo | flux_schnell | local_broll | texture
    creator_image_model: str = "sdxl_turbo"  # the diffusers model auto tries: sdxl_turbo | flux_schnell
    creator_comfy_url: str = "http://127.0.0.1:8188"
    creator_comfy_checkpoint: str = ""  # "" = the fastest one ComfyUI has (turbo/schnell/lightning first)
    creator_comfy_workflow: str = ""  # your own API-format workflow JSON, with {{prompt}} etc. placeholders
    creator_cpu_pictures: bool = False  # let diffusers draw on the CPU (minutes a picture)
    creator_motion: float = 1.0  # camera movement strength (creator/motion.py)
    creator_transition: str = "mixed"  # mixed | fade | dissolve | fadeblack
    creator_music: str = "auto"  # auto (your music folder, else composed) | folder | composed | off
    creator_music_db: int = -20
    creator_sfx: bool = True
    creator_sfx_folder: str = ""  # your own sound effects, named for their cue (whoosh, riser...)
    creator_sfx_db: int = 0  # all effects up or down
    creator_auto_whoosh: bool = True  # a whoosh on scene changes that have no other effect
    creator_caption_font: str = "Montserrat Black"
    creator_caption_size: int = 84
    creator_uppercase: bool = True
    creator_hook_card: bool = True  # the hook in big letters over the first seconds
    creator_film_look: bool = True  # vignette and fine grain
    creator_downloads: bool = True  # download voice and picture models when they're first needed
    # The Studio (studio/, editor/): the Creator's timeline engine. Keys are yours, from each service's site.
    creator_engine: str = "creator"  # what the Creator tab makes videos with: creator (classic) | studio
    studio_captions: str = "pop"  # editor/kinetic_subtitles presets: pop | bounce | karaoke | plain
    studio_stock: bool = True  # look for a scene's footage on Pexels and Pixabay (needs a key below)
    studio_stock_video: bool = True  # prefer stock video clips to stock photos
    studio_keep_timeline: bool = True  # keep a Studio video's timeline.json and its files, to edit and render again
    pexels_api_key: str = ""  # pexels.com/api (free)
    pixabay_api_key: str = ""  # pixabay.com/api/docs (free)
    elevenlabs_api_key: str = ""  # elevenlabs.io: a cloud voice, used only when chosen as the voice engine
    elevenlabs_voice: str = ""  # one of your ElevenLabs voices, by name or id ("" = the first that suits)
    elevenlabs_model: str = "eleven_multilingual_v2"
    # Layout (banners and stacked video)
    banner_top: bool = False
    banner_top_text: str = "{title}"
    banner_top_size: int = 10
    banner_bottom: bool = False
    banner_bottom_text: str = "Follow for part 2"
    banner_bottom_alt: str = "Follow for more"  # instead, when the bottom text promises a part 2 that doesn't exist
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
    research_saturation_checks: int = 2  # research/velocity.py: Shorts searches per scan (100 quota units each)
    research_auto: bool = True
    research_interval: int = 30
    research_alert: int = 75
    research_category_charts: bool = True  # also check the trending chart of each YouTube category your categories use
    research_hide_used: bool = True  # Research hides videos already picked, queued, clipped or sent
    research_view: str = "matrix"  # Research shows: "matrix" (cards per cluster), "table" or "arbitrage"
    research_hidden_columns: str = ""  # Research table columns hidden from its header menu, comma separated
    # Picking rules (policy.py): which researched videos may be clipped without asking
    pick_from: str = "watchlist"
    pick_languages: str = "en"  # only pick videos in these spoken languages (when YouTube knows it); "" = any
    pick_channels: str = ""
    pick_searches: str = ""
    pick_finds: str = ""  # standing requests without categories, one per line (categories have their own)
    pick_find_hours: int = 6  # how often automation runs each standing request
    pick_min_score: int = 65
    pick_min_minutes: float = 8.0
    pick_max_minutes: float = 180.0
    pick_max_age_days: float = 3.0
    pick_max_age_allowed_days: float = 14.0
    pick_daily: int = 3
    pick_channel_weekly: int = 2
    pick_block_channels: str = ""
    pick_block_categories: str = "10, 1"  # Music, Film & Animation: reposts are usually claimed
    pick_block_words: str = "music video, lyric video, lyrics, official audio, trailer, full movie, full album"
    learn_from_results: bool = True  # learn.py: how posted clips did ranks channels and guides AI hooks
    # Categories (categories.py): one TikTok account's topic each, edited in the Categories tab. Empty = no categories
    categories: list = field(default_factory=list)
    # Automation (pipeline.py): the unattended runner
    auto_output_dir: str = ""  # "" = <output_dir>/Automation
    auto_preset: str = "Shorts / TikTok / Reels"
    auto_layout: str = ""  # "" = the layout in your settings
    auto_recipes: str = ""  # lines of "match = preset | view"
    auto_captions: bool = True
    auto_bleep: bool = True  # mask strong swearing in automation's captions
    auto_polish: bool = True
    auto_qc: bool = True
    auto_start_hour: int = 0  # the runner downloads and encodes between these hours (equal = any time)
    auto_end_hour: int = 0
    auto_gap_minutes: int = 10
    auto_ai_daily: int = 10
    auto_retries: int = 3
    auto_delete_sources: bool = True
    auto_keep_days: int = 14
    disk_min_free_gb: int = 15  # automation cleans up, then pauses, before free space falls under this (0 = off)
    watchdog_minutes: int = 30  # a download or export that stops moving this long is stopped (0 = off)
    auto_prefetch: bool = True  # download the next video while this one is clipped
    # Clean up (cleanup.py): clear out downloads, rejected clips and clips that won't be posted
    cleanup_auto: bool = True  # automation does it once a day
    cleanup_kinds: str = "partial, previews, rejected, posted, removed"
    cleanup_days: int = 3
    weekly_report: bool = True  # weekly.py: automation sends it on Monday mornings
    # Telegram (telegram/): automation's bot, so a phone can ask for videos, watch what's waiting to post and delete
    # bad ones. The phones it answers are in telegram.json, not here.
    telegram_on: bool = False
    telegram_token: str = ""  # from @BotFather
    telegram_send: str = "review, new, posted, news, problems, weekly"  # telegram/bot.SENDS
    # The Mini App (telegram/webapp.py): the bot as an app inside Telegram, to watch the videos and say what to fix.
    # Telegram only opens an https:// address a phone can reach: blank = a Cloudflare quick tunnel ClipMonster
    # runs itself (telegram/tunnel.py), or your own address pointed at this PC's telegram_app_port.
    telegram_app: bool = True
    telegram_app_url: str = ""
    telegram_app_port: int = 47330
    # The Mac app (remote/): automation's HTTPS server for ClipMonster for Mac. The Macs it answers are in
    # remote.json, not here.
    remote_on: bool = False
    remote_port: int = 47320  # remote/protocol.PORT
    # Look
    appearance: str = "dark"  # dark | light | system (gui/theme.py)
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
                "scenes": self.w_scenes, "keywords": self.w_keywords, "voice": self.w_voice,
                "emotion": self.w_emotion}

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.keywords.split(",") if k.strip()]

    def copy(self, **changes) -> "Settings":
        return replace(self, **changes)

    def apply(self, overrides: dict) -> "Settings":
        known = {f.name for f in fields(self)}
        clean = {k: v for k, v in overrides.items() if k in known}
        for key in SECRETS:
            if isinstance(clean.get(key), str):
                clean[key] = clean[key].strip()  # pasted keys often carry a stray space
        return replace(self, **clean)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path | None = None, tries: int = 3) -> "Settings":
        path = path or app_dir() / "settings.json"
        for attempt in range(tries):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                break
            except FileNotFoundError:
                return _with_secrets(cls()) if _secret_store is not None else cls()
            except (OSError, ValueError):  # held for a moment by another program (a virus scanner), or broken
                if attempt == tries - 1:
                    return _with_secrets(cls()) if _secret_store is not None else cls()
                time.sleep(0.1)
        if isinstance(data, dict) and data.get("transcribe") == "always" and "speed" not in data:
            # Saved before 2.5.0, when "Always" was the only way to get Whisper's captions on clips that
            # have YouTube captions: now only the clips are typed out (the captions still find them).
            data = data | {"transcribe": "clips"}
        if isinstance(data, dict) and data and "update_channel" not in data:
            # Saved before 2.8.0. "Include beta versions" is the Beta channel now, and Clean up runs by
            # itself every day: nothing ClipMonster is done with is left taking up space.
            data = data | {"update_channel": "beta" if data.get("include_prereleases") else "stable",
                           "cleanup_auto": True}
        from .categories import migrate_settings  # niches before 3.2

        out = cls().apply(migrate(migrate_settings(data)))
        return _with_secrets(out) if _secret_store is not None else out

    def save(self, path: Path | None = None) -> None:
        """Write settings.json whole or not at all: automation reads it while you type in the app."""
        path = path or app_dir() / "settings.json"
        data = asdict(self)
        if _secret_store is not None:  # in the Keychain, not the file
            for name in SECRETS:
                try:
                    if _secret_store.get(name) != data[name]:
                        _secret_store.set(name, data[name])
                    data[name] = ""
                except Exception:  # noqa: BLE001 - no Keychain: the file keeps it, as on a PC
                    pass
        text = json.dumps(data, indent=2)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # Windows: open in another program for a moment
                time.sleep(0.05)
        tmp.unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")


def _with_secrets(s: "Settings") -> "Settings":
    """The keys from the secret store; a key still in the file (from before) moves into it."""
    changes = {}
    for name in SECRETS:
        try:
            kept = _secret_store.get(name)
            in_file = getattr(s, name)
            if kept:
                changes[name] = kept
            elif in_file:
                _secret_store.set(name, in_file)
        except Exception:  # noqa: BLE001
            continue
    return s.copy(**changes) if changes else s


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

# The pro editing (rendering/plan.py) that the TikTok presets switch on: the speaker followed, punch-ins and
# push-ins, dead air and fillers cut, kinetic captions, mastered sound, a hook card and a credited context tag.
PRO_EDIT = {"track_speaker": True, "punch_zoom": True, "slow_zoom": True, "cut_silences": True, "cut_fillers": True,
            "caption_style": "kinetic", "caption_size": 62, "master_audio": True, "hook_intro": True,
            "lower_third": True}

# Presets leave clip length alone: it's yours (Min/Max clip length), so picking a look never makes
# the clips shorter or longer than you asked.
PRESETS: dict[str, dict] = {
    "Balanced highlights": {
        "aspect": "vertical_blur", "clip_count": 5, "burn_captions": False,
        "mode": "auto",
    },
    "Shorts / TikTok / Reels": {
        "aspect": "vertical_blur", "clip_count": 6, "burn_captions": True,
        "caption_position": "lower", "normalize_audio": True, "title_overlay": False, "mode": "auto",
        **PRO_EDIT,
    },
    "Creator Rewards (over 1 min)": {
        "aspect": "vertical_blur", "clip_count": 4, "burn_captions": True, "normalize_audio": True,
        "title_overlay": False, "mode": "auto", **PRO_EDIT, "creator_rewards": True,
    },
    "Vertical crop (talking head)": {
        "aspect": "vertical_crop", "clip_count": 6, "burn_captions": True,
        "normalize_audio": True, "mode": "auto",
    },
    "Podcast moments": {
        "aspect": "vertical_blur", "clip_count": 5, "w_reactions": 0.35,
        "w_scenes": 0.0, "burn_captions": False, "mode": "auto",
    },
    "Gaming / action": {
        "aspect": "vertical_blur", "clip_count": 8, "w_audio": 0.5,
        "w_scenes": 0.3, "mode": "auto",
    },
    "Square posts": {
        "aspect": "square", "clip_count": 5, "burn_captions": True,
        "caption_position": "lower", "mode": "auto", "tiktok_ready": False,  # 1:1 is for Instagram/X
    },
    "Stacked (gameplay below)": {
        "aspect": "vertical_crop", "clip_count": 6, "burn_captions": True,
        "caption_position": "middle", "normalize_audio": True, "stack_enabled": True, "stack_position": "bottom",
        "stack_split": 0.5, "banner_top": False, "banner_bottom": False, "mode": "auto",
    },
    "Banner top + bottom": {
        "aspect": "vertical_blur", "clip_count": 6, "burn_captions": True,
        "caption_position": "lower", "normalize_audio": True, "banner_top": True, "banner_bottom": True,
        "banner_mode": "bar", "stack_enabled": False, "title_overlay": False, "mode": "auto",
    },
    "Split by chapters": {"mode": "chapters", "aspect": "vertical_blur", "burn_captions": False},
    "AI picks (Claude)": {"mode": "ai", "clip_count": 6},
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
    keys = {f.name for f in SCHEMA if f.group in ("Clip finding", "Export", "Captions", "Editing", "Layout")} | {"mode"}
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


AI_PROVIDERS = (("claude", "Claude (Anthropic)"), ("gemini", "Gemini (Google)"),
                ("local", "Local AI on this PC (free, private)"))  # ai.PROVIDERS
# ClipMonster for Mac: its own local AI runs on the Mac, and a linked PC can lend its own (MAC_WORDING)
MAC_AI_PROVIDERS = (("claude", "Claude (Anthropic)"), ("gemini", "Gemini (Google)"),
                    ("local", "Local AI on this Mac (free, private)"),
                    ("pc", "Your PC's local AI (free, private; your PC linked)"))
EVENT_SEARCH_MODELS = (("auto", "The sentence model (a 90 MB download, once); words if it can't be had"),
                       ("minilm", "Sentence model (all-MiniLM-L6-v2, downloads once)"),
                       ("words", "Words only (no download)"))
EVENT_FEEDS = (("auto", "ESPN, or API-Football when it has a key"), ("espn", "ESPN's public scoreboard"),
               ("api-football", "API-Football (needs its key)"), ("off", "Off: the commentary only"))
REEL_TRANSITIONS = (("mix", "A mix of quick wipes, slides and zooms"), ("dissolve", "Cross-dissolves"),
                    ("whip", "Whip-pans"), ("zoom", "Zoom into the next play"))
CLAUDE_MODELS = (("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
                 ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)"))
GEMINI_MODELS = (("gemini-4-argon", "Gemini 4 Argon (best)"),  # gemini.MODELS
                 ("gemini-3.8-flash", "Gemini 3.8 Flash (fast, recommended)"),
                 ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite (cheapest)"))
SPEEDS = (("quiet", "Quiet (slowest, the PC stays free)"), ("balanced", "Balanced"),
          ("full", "Full power (every core and the graphics card)"))
MODES = (("auto", "Smart signals"), ("ai", "AI picks (Claude or Gemini)"), ("chapters", "YouTube chapters"))
ASPECTS = (("original", "Original"), ("vertical_blur", "9:16 vertical, blurred fill"),
           ("vertical_crop", "9:16 vertical, cropped"), ("square", "1:1 square"))
ENCODERS = (("auto", "Auto (fastest available)"), ("libx264", "CPU (x264)"), ("h264_amf", "AMD GPU (AMF)"),
            ("h264_nvenc", "NVIDIA GPU (NVENC)"), ("h264_qsv", "Intel GPU (Quick Sync)"))
MAC_ENCODERS = (("auto", "Auto (fastest available)"), ("libx264", "CPU (x264)"),
                ("h264_videotoolbox", "The Mac's media engine (VideoToolbox)"))

PICK_FROM = (
    ("watchlist", "My channel list and Creative Commons only"),
    ("cc", "Creative Commons only"),
    ("any", "Anything that passes the rules"),
)

# What Clean up can clear (cleanup.py), in the order it lists them
CLEANUP_KINDS = (
    ("partial", "Unfinished downloads"),
    ("previews", "Preview renders"),
    ("downloads", "Downloaded videos"),
    ("analysis", "Saved transcripts and analysis"),
    ("rejected", "Clips the quality check rejected"),
    ("posted", "Posted clips whose files were kept"),
    ("removed", "Clips you took out of the posting queue"),
)

LOCAL_MODELS = (('auto', 'Pick for this PC'),
                ('llama-3.2-3b', 'Llama 3.2 3B · Compact · 2.0 GB · any PC, quick on the CPU too'),
                ('qwen2.5-7b', 'Qwen2.5 7B · Standard · 4.7 GB · 8 GB+ graphics card'),
                ('mistral-nemo-12b', 'Mistral Nemo 12B · High-capacity · 7.5 GB · 12 GB+ graphics card'),
                ('qwen2.5-32b', 'Qwen2.5 32B · Ultra · 19.9 GB · 24 GB graphics card, or 16 GB + 32 GB RAM'),
                ('command-r-32b', 'Command R 32B · Ultra · 19.8 GB · non-commercial licence'),
                ('mistral-7b', "Mistral 7B v0.3 · 4.4 GB · ClipMonster 3.1's model"),
                ('custom', 'My own GGUF file'))  # intelligence/models.CHOICES (a test keeps them the same)
MAC_LOCAL_MODELS = (('auto', 'Pick for this Mac'),
                    ('llama-3.2-3b', 'Llama 3.2 3B · Compact · 2.0 GB · any Mac, Intel too'),
                    ('qwen2.5-7b', 'Qwen2.5 7B · Standard · 4.7 GB · 16 GB+ Mac'),
                    ('mistral-nemo-12b', 'Mistral Nemo 12B · High-capacity · 7.5 GB · 16 GB+ Mac'),
                    ('qwen2.5-32b', 'Qwen2.5 32B · Ultra · 19.9 GB · 36 GB+ Mac'),
                    ('command-r-32b', 'Command R 32B · Ultra · 19.8 GB · non-commercial licence'),
                    ('mistral-7b', "Mistral 7B v0.3 · 4.4 GB · ClipMonster 3.1's model"),
                    ('custom', 'My own GGUF file'))  # intelligence/models.MAC_CHOICES (kept the same too)
CAPTION_STYLES = (("classic", "Classic (word pop)"), ("kinetic", "Kinetic (pop, karaoke, keyword colours)"))
# The Video Creator's choices (a test keeps them the same as creator/'s own lists)
CREATOR_AI = (("local", "The local AI on this PC (free)"), ("any", "The AI chosen in Settings → AI"))
CREATOR_VOICES = (("en_male_deep", "Deep Male Narrator"), ("en_storyteller", "Energetic Storyteller"),
                  ("en_documentary", "Calm Documentary"), ("en_female_warm", "Warm Female Narrator"),
                  ("en_female_bright", "Bright Female"), ("en_hushed", "Hushed Storyteller"),
                  ("en_british_male", "British Male"), ("en_male_calm", "Calm Male"))
CREATOR_TTS = (("auto", "Best available on this PC"), ("kokoro", "Kokoro-82M (natural)"), ("piper", "Piper (light)"),
               ("windows", "Windows voices"), ("espeak", "eSpeak NG (robotic)"),
               ("elevenlabs", "ElevenLabs (online, your key)"))
CREATOR_ENGINES = (("creator", "Video Creator (classic)"),
                   ("studio", "Studio (an editable timeline)"))
STUDIO_CAPTIONS = (("pop", "Pop: words pop in, the spoken one lights up"), ("bounce", "Bounce: a springier pop"),
                   ("karaoke", "Karaoke: the colour sweeps through each word"), ("plain", "Plain: no movement"))
CREATOR_STYLES = (("", "Pick from the category"), ("cinematic", "Cinematic dark"), ("documentary", "Documentary"),
                  ("history", "Old-world painting"), ("horror", "Horror"), ("bright", "Bright and bold"),
                  ("dreamy", "Calm and dreamy"))
CREATOR_VISUALS = (("auto", "Best available"), ("comfyui", "ComfyUI on this PC"),
                   ("sdxl_turbo", "AI pictures: SDXL-Turbo"), ("flux_schnell", "AI pictures: FLUX.1-schnell (NVIDIA)"),
                   ("local_broll", "My B-roll folder"),
                   ("texture", "Built-in moving textures"))
CREATOR_WHEN = (("night", "Overnight, from 1 am"), ("now", "As soon as they're planned"))
CREATOR_REVIEW = (("new", "For an account's first two weeks"), ("always", "Always"), ("off", "Never"))
CREATOR_FACTCHECK = (("fix", "Fix what the sources contradict"), ("flag", "Mark it only"), ("off", "Off"))
# What the Telegram bot sends without being asked (telegram/bot.SENDS; a test keeps them the same)
TELEGRAM_SENDS = (("review", "Videos waiting for your OK, with the video"),
                  ("new", "New clips and videos queued to post"),
                  ("posted", "Each post as it goes up"),
                  ("news", "Milestones and videos taking off"),
                  ("problems", "Problems that need you: a sign-in running out, a post failing, low disk space"),
                  ("weekly", "The weekly report on Monday mornings"))
CREATOR_TRANSITIONS = (("mixed", "Varied (dissolves, dips, slides)"), ("fade", "Cross-dissolve"),
                       ("fadeblack", "Dip to black"), ("dissolve", "Grainy dissolve"))
CREATOR_MUSIC = (("auto", "My music folder, else composed"), ("folder", "My music folder only"),
                 ("composed", "Composed on this PC"), ("off", "No music"))
APPEARANCES = (("dark", "Dark"), ("light", "Light"), ("system", "Match Windows"))

UPDATE_CHANNELS = (("stable", "Stable"), ("beta", "Beta (test versions)"), ("dev", "Dev (every change)"))

BANNER_MODES = (("bar", "Solid bar (video sits between)"), ("overlay", "Text over the video"))
LAYOUT_FITS = (("auto", "Auto (from Format)"), ("crop", "Crop to fill"), ("blur", "Fit, blurred fill"),
               ("fit", "Fit, banner colour fill"))

SCHEMA: list[Field] = [
    Field("output_dir", "Output folder", "General", "path", help="Where finished clips are saved"),
    Field("appearance", "Appearance", "General", "choice", choices=APPEARANCES,
          help="Dark suits editing video; Light matches a light Windows theme. Takes effect when ClipMonster "
               "starts again"),
    Field("cache_dir", "Download cache", "General", "path", help="Where full videos are kept. Empty = <output>/.downloads"),
    Field("folder_template", "Folder naming", "General", "str",
          help="Fields: {channel} {date} {title} {id}"),
    Field("file_template", "Clip naming", "General", "str",
          help="Fields: {rank} {time} {label} {score} - e.g. {rank:02d} - {label}"),
    Field("auto_export", "Export automatically after analysis", "General", "bool",
          help="Hands-free batch mode: clips are exported as soon as a video is analysed"),

    Field("check_updates", "Check for updates automatically", "Updates", "bool",
          help="Looks for a new release on GitHub about once a day"),
    Field("update_channel", "Update channel", "Updates", "choice", choices=UPDATE_CHANNELS,
          help="Stable: finished versions only. Beta: also test versions of the next one. Dev: a new build "
               "every time ClipMonster's code changes, for trying things the moment they're made (least "
               "tested). Picking a steadier channel offers its newest version straight away"),
    Field("auto_update", "Get updates by themselves", "Updates", "bool",
          help="New versions download in the background, so updating is one click (Restart now). When the "
               "window is closed, automation installs them itself between videos and carries on. Off: you're "
               "told about new versions and download them yourself"),
    Field("update_repo", "Update source (GitHub owner/repo)", "Updates", "str",
          help="Leave blank to use the repository this build was published from"),

    Field("quality", "Max download resolution", "Download", "choice",
          choices=((2160, "2160p (4K)"), (1440, "1440p"), (1080, "1080p"), (720, "720p"), (480, "480p"), (360, "360p"))),
    Field("cookies_browser", "Use browser login", "Download", "choice",
          choices=(("", "None"), ("clipmonster", "ClipMonster's own sign-in (Firefox)"), ("chrome", "Chrome"),
                   ("edge", "Edge"), ("firefox", "Firefox"), ("brave", "Brave")),
          help="Signed-in downloads are what keep YouTube from asking this PC to confirm it's not a bot, and they "
               "also open age-restricted and members-only videos. Easiest: press Sign in to YouTube (Automation "
               "tab, or below), which opens Firefox just for ClipMonster. Use a spare Google account, not your "
               "main one"),
    Field("keep_source", "Keep full downloads", "Download", "bool"),
    Field("playlist_limit", "Max videos per playlist (0 = all)", "Download", "int", lo=0, hi=500),

    Field("mode", "Clip finder", "Clip finding", "choice", choices=MODES),
    Field("clip_count", "Clips per video", "Clip finding", "int", lo=1, hi=50),
    Field("min_len", "Min clip length (s)", "Clip finding", "float", lo=3, hi=600, step=1),
    Field("max_len", "Max clip length (s)", "Clip finding", "float", lo=5, hi=1800, step=1),
    Field("series_parts", "Part series: up to this many parts", "Clip finding", "int", lo=1, hi=5,
          help="When a great moment runs longer than one clip, it carries on in Part 2 (and 3...), each starting "
               "where the last stopped. Parts count towards Clips per video. 1 = off"),
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
    Field("w_voice", "Weight: animated voice", "Clip finding", "float", lo=0, hi=1, step=0.05,
          help="Where the speaker's voice gets lively: pitch swinging up and down with the energy. Works with "
               "no AI and no key"),
    Field("w_emotion", "Weight: emotional words", "Clip finding", "float", lo=0, hi=1, step=0.05,
          help="Where the words turn strongly positive or negative, or swing from one to the other. Read from "
               "the transcript on this PC, with no AI"),

    Field("transcribe", "Local transcription (Whisper)", "Transcription", "choice",
          choices=(("auto", "Only when YouTube has no captions"),
                   ("clips", "For the clips (captions find them, Whisper types them out)"),
                   ("always", "Always, the whole video (slow)"), ("never", "Never")),
          help="For the clips is much faster than Always: YouTube's captions find the clips, then Whisper "
               "types out just the clips, for captions and titles. Videos with no captions are typed out whole"),
    Field("whisper_model", "Whisper model", "Transcription", "choice",
          choices=(("tiny", "tiny (fastest)"), ("base", "base"), ("small", "small"), ("medium", "medium"),
                   ("large-v3-turbo", "large-v3-turbo (near-best, quicker than medium)"),
                   ("large-v3", "large-v3 (best, slow)")),
          help="Downloaded once on first use. ytclip whisper-test VIDEO times each one on this PC"),

    Field("speed", "How hard ClipMonster works", "Speed", "choice", choices=SPEEDS,
          help="Quiet keeps the PC free for other things and works slowest. Balanced uses half the CPU and "
               "steps aside for what you're doing. Full power uses every CPU core and the graphics card"),
    Field("gpu_whisper", "Full power: Whisper on the graphics card", "Speed", "bool",
          help="Types out speech on the graphics card (AMD, NVIDIA or Intel, through Vulkan), often several "
               "times faster than the CPU. Downloads the model for it once. Falls back to the CPU if it fails"),
    Field("whisper_language", "Language code (blank = detect)", "Transcription", "str"),

    Field("ai_provider", "AI to use", "AI", "choice", choices=AI_PROVIDERS,
          help="Which AI picks clips, polishes them and follows special requests. Claude and Gemini need an API key "
               "and are paid per use. Local AI runs a model on this PC: free and private, a little less sharp"),
    Field("ai_fallback", "If it fails, try another AI", "AI", "bool",
          help="When the AI above can't answer (no key, a rejected key, a used-up quota, busy servers, or it "
               "declines), the other AI is asked instead, if it has a key. Off: that video uses smart signals "
               "or goes out unpolished"),
    Field("ai_api_key", "Anthropic API key (Claude)", "AI", "secret", help="Needed to use Claude. From "
          "console.anthropic.com", placeholder="sk-ant-…"),
    Field("ai_model", "Claude model for finding clips", "AI", "choice", choices=CLAUDE_MODELS),
    Field("ai_polish_model", "Claude model for polishing", "AI", "choice", choices=CLAUDE_MODELS),
    Field("gemini_api_key", "Gemini API key", "AI", "secret", help="Needed to use Gemini. Free from "
          "aistudio.google.com/apikey", placeholder="AIza…"),
    Field("gemini_model", "Gemini model for finding clips", "AI", "choice", choices=GEMINI_MODELS),
    Field("gemini_polish_model", "Gemini model for polishing", "AI", "choice", choices=GEMINI_MODELS),
    Field("local_model", "Local AI model", "AI", "choice", choices=LOCAL_MODELS,
          help="Runs on this PC: free, private and offline once it's downloaded (from Hugging Face, once). 'Pick "
               "for this PC' takes the biggest model that fits entirely on the graphics card: Qwen2.5 7B for 8-10 GB, "
               "Mistral Nemo 12B for 11-20 GB, Qwen2.5 32B for 24 GB or more, else Llama 3.2 3B, which is also "
               "quick on a CPU. A bigger model than that splits between the card and RAM: better picks, but slower"),
    Field("local_model_path", "Local model file (with 'My own GGUF file')", "AI", "file",
          help="Any instruction-tuned GGUF model, such as Qwen or Gemma. Its own header says how big it is, so "
               "it's sized to the graphics card like the others"),
    Field("local_gpu", "Local AI on the graphics card", "AI", "bool",
          help="Much faster. Uses Vulkan, so AMD, NVIDIA and Intel cards all work. If the card runs out of memory "
               "or its driver fails, it carries on on the CPU by itself"),
    Field("local_context", "Local AI memory (tokens, 0 = sized to this PC)", "AI", "int", lo=0, hi=131072,
          step=1024, help="How much transcript it reads at once: up to 32,768 tokens (about an hour of talk) when "
                          "the graphics card has room. Longer videos are read in parts either way"),
    Field("local_gpu_layers", "Local AI layers on the card (-1 = all that fit)", "AI", "int", lo=-1,
          hi=256, help="Leave at -1: ClipMonster and llama.cpp measure the free graphics memory and fit as many "
                       "as they can. 0 runs it on the CPU only"),
    Field("ai_instructions", "What makes a good clip?", "AI", "text"),
    Field("ai_polish", "Polish clips with AI", "AI", "bool",
          help="One extra AI call per video, with a frame from each clip: writes a hook for the top banner and "
               "bottom lines for clips with no part 2, fixes misheard words in the captions, spots subtitles "
               "already in the video and centres vertical crops on the speaker"),
    Field("polish_bottom_text", "AI writes the bottom text when there's no part 2", "AI", "bool",
          help="When your bottom text promises a part 2 (like 'Follow for part 2'), each clip with no part 2 gets "
               "a line the AI wrote for it, never promising one, instead of the same 'no part 2' text every time. "
               "It keeps any @handle in your bottom texts. Clips with a part 2 keep your bottom text. Off: clips "
               "with no part 2 show the 'no part 2' text (Settings → Layout)"),
    Field("polish_overpick", "Find extra clips, keep the best the AI rates", "AI", "bool",
          help="With polish on, half as many clips again are found and polished, and only the best rated ones "
               "are exported. More of the clips you get pass the quality check, and none is made just to be "
               "rejected. A little more text goes to the AI"),
    Field("polish_burned_subs", "If the video already has subtitles", "AI", "choice",
          choices=(("off", "Turn my captions off for it"), ("keep", "Keep my captions anyway")),
          help="Two sets of words on screen at once is hard to read"),
    Field("ai_budget_day", "Spending limit a day (US $, 0 = none)", "AI", "float", lo=0, hi=1000, step=0.5,
          help="ClipMonster counts what Claude and Gemini cost as it goes (ai_usage.json). Once today's count "
               "reaches this, they aren't asked again until tomorrow; the local AI carries on, as it's free. "
               "The count uses list prices, so your provider's bill is the real figure"),
    Field("ai_budget_month", "Spending limit a month (US $, 0 = none)", "AI", "float", lo=0, hi=10000, step=1,
          help="The same, for the calendar month"),
    Field("ai_local_timeout", "Local AI gives up after (s)", "AI", "int", lo=30, hi=3600, step=30,
          help="When the local AI is asked first and takes longer than this, a cloud AI is asked instead "
               "(if one has a key)"),
    Field("event_search_model", "Event search reads with", "Sports", "choice", choices=EVENT_SEARCH_MODELS,
          help="Search a game for a moment (\"red card after a VAR check\"). The sentence model understands "
               "meaning, not just words, runs on this computer and downloads once (90 MB). Without it, the "
               "search matches words and what they usually mean in each sport"),
    Field("event_feed", "Play-by-play feed", "Sports", "choice", choices=EVENT_FEEDS,
          help="A game's official timeline (cards, goals, fouls, scores) finds moments the commentary never "
               "names, and puts the minute and the player on the clip. ESPN's public scoreboard needs no key; "
               "API-Football covers more soccer leagues with a free key"),
    Field("api_football_key", "API-Football key", "Sports", "secret",
          help="Free from api-football.com (100 requests a day): soccer cards, goals, substitutions and VAR "
               "decisions, minute by minute"),
    Field("event_pad_before", "Keep before an event (s)", "Sports", "float", lo=2, hi=60, step=1,
          help="The build-up: the foul before the card, the pass before the goal"),
    Field("event_pad_after", "Keep after an event (s)", "Sports", "float", lo=3, hi=90, step=1,
          help="The referee, the crowd, the protests and the first replay"),
    Field("reel_transitions", "Reel transitions", "Sports", "choice", choices=REEL_TRANSITIONS,
          help="How one play hands over to the next in a recap reel"),
    Field("reel_transition_seconds", "Transition length (s)", "Sports", "float", lo=0.1, hi=0.6, step=0.05),

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
          help="With AI polish on, the AI rates how well each clip stands alone, 1 to 10. 0 ignores the ratings"),

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
    Field("caption_style", "Caption style", "Captions", "choice", choices=CAPTION_STYLES,
          help="Kinetic: one to three words at a time, centred in TikTok's safe zone, popping in, with the spoken "
               "word lit up and money, danger and question words in their own colours"),
    Field("caption_keyword_colours", "Kinetic: colour keywords", "Captions", "bool",
          help="Money words green, danger and conflict red, questions and curiosity yellow"),
    Field("caption_money_colour", "Money words", "Captions", "color"),
    Field("caption_danger_colour", "Danger words", "Captions", "color"),
    Field("caption_question_colour", "Question words", "Captions", "color"),

    Field("track_speaker", "Follow the speaker", "Editing", "bool",
          help="Finds the person talking (whose mouth moves while there are words) and keeps them framed, "
               "gliding after them like a camera operator and cutting when someone else speaks. With a blurred "
               "fill format, clips with a speaker are cropped to them; clips without one keep the fill"),
    Field("camera_smoothing", "Camera speed (0.02 calm - 0.3 quick)", "Editing", "float", lo=0.01, hi=0.5,
          step=0.01),
    Field("camera_deadband", "Camera stays still within (share of frame)", "Editing", "float", lo=0.0, hi=0.2,
          step=0.01, help="Small movements of the speaker don't move the camera, so it never wobbles"),
    Field("punch_zoom", "Punch in on new sentences", "Editing", "bool",
          help="Each new sentence switches between the normal shot and a slightly closer one, like a two-camera edit"),
    Field("punch_scale", "Punch-in size", "Editing", "float", lo=1.02, hi=1.25, step=0.01),
    Field("slow_zoom", "Slow push-in on stories", "Editing", "bool",
          help="Stories and curiosity clips creep in over ten seconds, starting again at each change of topic"),
    Field("slow_zoom_scale", "Push-in size", "Editing", "float", lo=1.03, hi=1.3, step=0.01),
    Field("cut_silences", "Cut dead air", "Editing", "bool",
          help="Pauses inside the clip shrink to a breath, using Whisper's word timings. The picture jump-cuts and "
               "the sound joins without a click"),
    Field("silence_longest", "Pauses longer than (s)", "Editing", "float", lo=0.2, hi=2.0, step=0.05),
    Field("silence_keep", "...shrink to (s)", "Editing", "float", lo=0.04, hi=0.5, step=0.02),
    Field("cut_fillers", "Cut filler words", "Editing", "bool",
          help="'Um', 'uh' and the like always go. 'Like', 'you know' and 'literally' only when they stand apart "
               "(set off by commas or pauses), never in 'I like it'"),
    Field("filler_words", "Filler words", "Editing", "str"),
    Field("bleep_audio", "Bleep swearing in the sound", "Editing", "bool",
          help="The words masked in the captions (Captions → Bleep strong swearing) are bleeped in the sound too"),
    Field("bleep_tone", "Bleep with", "Editing", "choice", choices=(("tone", "A beep"), ("mute", "Silence"))),
    Field("master_audio", "Master the sound", "Editing", "bool",
          help="Takes out rumble (80 Hz high-pass), brings quiet words forward (speech compressor) and sets the "
               "loudness to TikTok's -14 LUFS with a -1 dB peak ceiling, measured in two passes"),
    Field("music_folder", "Music folder", "Editing", "path",
          help="Royalty-free or licensed tracks you have the right to use. Folders named Suspense, Upbeat, Chill "
               "or Dark are matched to each clip's mood. The music ducks under the speech by itself"),
    Field("music_level_db", "Music level (dB)", "Editing", "int", lo=-40, hi=-6),
    Field("hook_intro", "Hook card at the start", "Editing", "bool",
          help="The first moments dim behind a bold headline (the AI's hook, else the clip's title) that "
               "punches in word by word while the clip plays"),
    Field("intro_seconds", "Hook card length (s)", "Editing", "float", lo=0.8, hi=4.0, step=0.1),
    Field("lower_third", "Context tag with source credit", "Editing", "bool",
          help="A slim tag that slides in for a few seconds: the topic, and 'SOURCE · channel'"),
    Field("lower_third_text", "Context tag text", "Editing", "str",
          help="{topic} (the category, else the clip's title), {title} or {channel}"),
    Field("broll_folder", "B-roll folder", "Editing", "path",
          help="Your own or licensed clips only. File and folder names are matched to what's being said "
               "('money/counting cash.mp4' shows when money comes up)"),
    Field("broll_every", "B-roll about every (s)", "Editing", "float", lo=8, hi=60, step=1),
    Field("broll_seconds", "Each B-roll lasts (s)", "Editing", "float", lo=1, hi=6, step=0.5),
    Field("creator_rewards", "Creator Rewards length (over 1 minute)", "Editing", "bool",
          help="TikTok's Creator Rewards only pays for videos over one minute. Clips are found at 62 to 88 s, and "
               "one that would come out under 61 s after its pauses are cut runs on to the next sentence's end"),

    Field("creator_ai", "Script writer", "Creator", "choice", choices=CREATOR_AI,
          help="The local AI writes scripts for free on this PC (Settings → AI picks its model). Or let the AI "
               "chosen in Settings → AI write them (a cloud AI costs a little per script)"),
    Field("creator_web", "Look it up online first", "Creator", "bool",
          help="Before the script is written, the topic is searched on Google News, Bing News and Wikipedia, and "
               "the newest stories go to the AI with today's date. Without it, a local AI only knows what it "
               "learnt in training, which can be a year or more out of date"),
    Field("creator_factcheck", "Fact check", "Creator", "choice", choices=CREATOR_FACTCHECK,
          help="After the script is written, the AI checks each date, number, name and quote against what was "
               "found online. Fix rewrites what a source contradicts (and softens unconfirmed numbers and dates); "
               "Mark only lists them in the storyboard and the video's README. Needs Look it up online first"),
    Field("creator_review", "Check videos before they post", "Creator", "choice", choices=CREATOR_REVIEW,
          help="A Creator video waits in its account's queue until you OK it (Queue tab, or Telegram), so you see "
               "what the AI made before your followers do. \"First two weeks\" stops asking two weeks after an "
               "account's first Creator video posted"),
    Field("creator_schedule_when", "Make the daily videos", "Creator", "choice", choices=CREATOR_WHEN,
          help="Each category's \"Creator videos a day\" (Categories tab) are planned once a day. Overnight, they're "
               "made from 1 am (within Automation's working hours), so they're ready to post in the morning"),
    Field("creator_voice", "Voice", "Creator", "choice", choices=CREATOR_VOICES),
    Field("creator_tts", "Voice engine", "Creator", "choice", choices=CREATOR_TTS,
          help="Kokoro sounds most natural and downloads once (about 340 MB). Piper is lighter. Windows' own "
               "voices need nothing at all"),
    Field("creator_kokoro_model", "Kokoro model", "Creator", "choice",
          choices=(("full", "Full (310 MB, best)"), ("compact", "Compact (88 MB)"))),
    Field("creator_word_timing", "Caption timing", "Creator", "choice",
          choices=(("whisper", "Whisper listens to the voice (most exact)"), ("sound", "From the voice's pauses"))),
    Field("creator_duration", "Video length (s)", "Creator", "int", lo=20, hi=180,
          help="What the script is written for. The voice's speed is nudged to land on it"),
    Field("creator_rewards_length", "Keep it over a minute (Creator Rewards)", "Creator", "bool",
          help="TikTok's Creator Rewards only pays for videos over one minute: videos are kept between 62 and 85 s"),
    Field("creator_style", "Look", "Creator", "choice", choices=CREATOR_STYLES,
          help="The pictures' style, the textures' colours and the music's mood"),
    Field("creator_visuals", "Pictures from", "Creator", "choice", choices=CREATOR_VISUALS,
          help="Auto uses ComfyUI when it's running, then AI pictures (set up from the Creator tab), then your "
               "B-roll, then built-in moving textures. A scene that fails in one moves on to the next"),
    Field("creator_image_model", "AI pictures model", "Creator", "choice",
          choices=(("sdxl_turbo", "SDXL-Turbo (fast, 8 GB+ card, any brand)"),
                   ("flux_schnell", "FLUX.1-schnell (best, NVIDIA 16 GB+)"))),
    Field("creator_comfy_url", "ComfyUI address", "Creator", "str"),
    Field("creator_comfy_checkpoint", "ComfyUI checkpoint", "Creator", "str",
          help="Empty = the fastest one ComfyUI has (a turbo, schnell or lightning model first)",
          placeholder="sd_xl_turbo_1.0_fp16.safetensors"),
    Field("creator_comfy_workflow", "ComfyUI workflow (API format)", "Creator", "file",
          help="Optional. Your own workflow saved with 'Export (API)'; {{prompt}}, {{negative}}, {{seed}}, "
               "{{width}} and {{height}} in it are filled in for each scene"),
    Field("creator_cpu_pictures", "Let Stable Diffusion use the CPU", "Creator", "bool",
          help="Without a graphics card it can use: minutes per picture"),
    Field("creator_motion", "Camera movement", "Creator", "float", lo=0.0, hi=2.0, step=0.1,
          help="0 = still pictures, 1 = gentle push-ins and pans, 2 = twice as much"),
    Field("creator_transition", "Scene changes", "Creator", "choice", choices=CREATOR_TRANSITIONS),
    Field("creator_music", "Music", "Creator", "choice", choices=CREATOR_MUSIC,
          help="Your music folder (Settings → Editing) first, sorted into mood folders; without one, a calm bed "
               "is composed for the mood on this PC"),
    Field("creator_music_db", "Music level (dB)", "Creator", "int", lo=-40, hi=-6),
    Field("creator_sfx", "Sound effects", "Creator", "bool",
          help="Whooshes, risers, bass drops and hits on the script's cues, made on this PC"),
    Field("creator_sfx_folder", "Sound effects folder", "Creator", "path",
          help="Optional: your own licensed effects. Files or folders named whoosh, riser, bass drop, boom, impact, "
               "glass, vinyl, shimmer, tick or heartbeat are used for that cue"),
    Field("creator_sfx_db", "Sound effects level (dB)", "Creator", "int", lo=-20, hi=10),
    Field("creator_auto_whoosh", "Whoosh on scene changes", "Creator", "bool"),
    Field("creator_caption_font", "Caption font", "Creator", "font"),
    Field("creator_caption_size", "Caption size", "Creator", "int", lo=40, hi=120),
    Field("creator_uppercase", "Captions in capitals", "Creator", "bool"),
    Field("creator_hook_card", "Hook in big letters at the start", "Creator", "bool"),
    Field("creator_film_look", "Film look (vignette, fine grain)", "Creator", "bool"),
    Field("creator_downloads", "Download voice and picture models when needed", "Creator", "bool",
          help="Into %LOCALAPPDATA%\\ClipMonster\\models, once. Off: put them there yourself"),
    Field("creator_engine", "Make videos with", "Creator", "choice", choices=CREATOR_ENGINES,
          help="What the Creator tab starts on. The Studio lays every video out as a timeline you can edit and "
               "render again, takes a public article's address, makes wide and long videos and can use stock "
               "footage. The classic Video Creator is the one this app has always had"),
    Field("studio_captions", "Studio captions", "Creator", "choice", choices=STUDIO_CAPTIONS,
          help="How the Studio's captions move"),
    Field("studio_stock", "Use stock footage sites", "Creator", "bool",
          help="For a scene your B-roll folder has nothing for, the Studio searches Pexels and Pixabay (their "
               "official APIs, with your own free keys below) before it makes a picture. Each clip's author and "
               "address are kept in the video's README and timeline.json"),
    Field("studio_stock_video", "Prefer stock video to photos", "Creator", "bool"),
    Field("studio_keep_timeline", "Keep each Studio video's timeline", "Creator", "bool",
          help="timeline.json and the files it uses (voice, music, effects, each scene's picture or clip) stay in "
               "the video's folder, so you can edit the timeline and render it again. Clean up removes them with "
               "the video once it's posted"),
    Field("pexels_api_key", "Pexels API key", "Creator", "secret",
          help="Free from pexels.com/api. Pexels' licence lets you use its photos and videos in your own videos; "
               "its terms apply"),
    Field("pixabay_api_key", "Pixabay API key", "Creator", "secret",
          help="Free from pixabay.com/api/docs. Pixabay's content licence and terms apply"),
    Field("elevenlabs_api_key", "ElevenLabs key", "Creator", "secret",
          help="Only used when Voice engine is ElevenLabs: the script's text is then sent to elevenlabs.io to be "
               "read, and they charge your account by the character"),
    Field("elevenlabs_voice", "ElevenLabs voice", "Creator", "str", placeholder="a voice's name or id",
          help="One of the voices in your ElevenLabs account. Empty = the first that suits the narrator chosen. "
               "Only use voices you have the right to use"),
    Field("elevenlabs_model", "ElevenLabs model", "Creator", "str"),

    Field("banner_top", "Top banner", "Layout", "bool", help="A bar of text above the video"),
    Field("banner_top_text", "Top text", "Layout", "str",
          help="{title} is replaced with the clip title. Each clip can override this in the Layout tab"),
    Field("banner_top_size", "Top banner height (%)", "Layout", "int", lo=4, hi=35,
          help="Height of the text area. With TikTok-ready output the bar also covers TikTok's tabs above it"),
    Field("banner_bottom", "Bottom banner", "Layout", "bool", help="A bar of text below the video"),
    Field("banner_bottom_text", "Bottom text", "Layout", "str",
          help="{title} is replaced with the clip title. Text that promises a part 2 only shows on a clip that "
               "another clip carries straight on from; other clips show the AI's line for them (with AI polish) or "
               "the text below. Each clip can override this in the Layout tab"),
    Field("banner_bottom_alt", "Bottom text when there's no part 2", "Layout", "str",
          help="Shown instead of the bottom text on a clip with no part 2, when the bottom text promises one. "
               "With AI polish the AI writes a line for each such clip instead (Settings → AI). Leave it blank "
               "for no words on those clips"),
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
    Field("research_saturation_checks", "Saturation checks per scan", "Research", "int", lo=0, hi=10,
          help="The fastest risers get a YouTube Shorts search for clips of them already posted (100 quota units "
               "each, at most 20 a day, each video once per 12 hours). 0 turns it off"),
    Field("research_auto", "Keep checking while ClipMonster is open", "Research", "bool"),
    Field("research_interval", "Check every (minutes)", "Research", "int", lo=10, hi=720, step=5),
    Field("research_alert", "Highlight new videos scoring at least", "Research", "int", lo=0, hi=100,
          help="New arrivals above this score are counted on the Research tab"),
    Field("research_category_charts", "Also check my categories' YouTube charts", "Research", "bool",
          help="With All categories, each region's chart for the YouTube categories your categories use (up to 4) is checked "
               "too, 1 unit each: more videos for each category than the main chart shows"),

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
    Field("pick_finds", "Standing requests", "Picking", "text",
          help="Videos you want, one request per line, in your own words, e.g. 'Call of Duty MW4 with good "
               "comedy'. Automation searches YouTube for each one every few hours and picks the best new "
               "matches, within your limits (they count as permission to clip, like adding a link yourself). "
               "Each run of a request costs about 200 quota units. With categories, each category has its own",
          placeholder="Call of Duty MW4 with good comedy\nMinecraft hardcore funny moments"),
    Field("pick_find_hours", "Run standing requests every (hours)", "Picking", "int", lo=1, hi=72),
    Field("pick_languages", "Only pick videos in", "Picking", "str",
          help="Spoken language codes, comma separated (en, es, fr…). Empty = any language. Videos whose "
               "uploader didn't set a language are allowed. Creative Commons searches favour the first "
               "one",
          placeholder="en"),
    Field("pick_min_score", "Lowest clip-ability to pick", "Picking", "int", lo=0, hi=100),
    Field("pick_min_minutes", "Shortest video (min)", "Picking", "float", lo=0, hi=120, step=1),
    Field("pick_max_minutes", "Longest video (min, 0 = any)", "Picking", "float", lo=0, hi=1200, step=10,
          help="Very long streams take a long time to download and analyse"),
    Field("pick_max_age_days", "Oldest trending video (days, 0 = any)", "Picking", "float", lo=0, hi=60, step=1,
          help="For videos you don't have permission on record for"),
    Field("pick_max_age_allowed_days", "Oldest channel-list or CC video (days, 0 = any)", "Picking", "float",
          lo=0, hi=60, step=1,
          help="Videos from your channel list or under Creative Commons stay worth clipping for longer"),
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
          help="Posting reads the views your posted clips get. Channels whose clips beat your usual views rank "
               "higher, ones that flop rank lower, and the AI sees the hooks that worked (and flopped) closest "
               "to what it's writing. The Video Creator also aims at the pace of your best Creator videos"),

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
    Field("auto_bleep", "Bleep strong swearing", "Automation", "bool",
          help="Writes f*ck, sh*t and the like in captions and .srt files, which TikTok is less likely to "
               "hold back"),
    Field("auto_qc", "Check clips after export", "Automation", "bool",
          help="Clips that fail (silent, black, frozen, wrong length, or rated low by the AI polish) go to a "
               "_rejected folder instead of being ready to upload"),
    Field("auto_polish", "Polish clips with AI", "Automation", "bool",
          help="Hooks, caption fixes and speaker crops (see Settings → AI). Needs a Claude or Gemini key "
               "and counts toward the AI limit below"),
    Field("auto_start_hour", "Work from (hour)", "Automation", "int", lo=0, hi=23,
          help="Downloads and encodes only start between these hours. The same hour twice means any time"),
    Field("auto_end_hour", "Work until (hour)", "Automation", "int", lo=0, hi=23),
    Field("auto_gap_minutes", "Minutes between downloads", "Automation", "int", lo=0, hi=240, step=5,
          help="Spacing downloads out makes YouTube less likely to ask the PC to sign in. If it asks anyway, "
               "automation pauses for 6 hours, then spaces downloads out 2x (4x if it asks again) for a day"),
    Field("auto_ai_daily", "AI calls per day", "Automation", "int", lo=0, hi=500,
          help="Finding clips with AI and polishing each count as one. Over the limit, videos use the smart "
               "signals and skip polishing, so AI costs stay predictable"),
    Field("auto_retries", "Tries per video", "Automation", "int", lo=1, hi=10,
          help="A failed video is tried again after 15 minutes, then 1 hour, then 4 hours"),
    Field("auto_delete_sources", "Delete full downloads after clipping", "Automation", "bool"),
    Field("auto_keep_days", "Clear cached analysis after (days)", "Automation", "int", lo=1, hi=365,
          help="Transcripts and measurements of finished videos are removed from the download cache"),
    Field("disk_min_free_gb", "Keep this much disk free (GB, 0 = off)", "Automation", "int", lo=0, hi=2000,
          help="Before free space falls under this, automation runs a safe clean-up (unfinished and finished "
              "downloads, rejected clips, previews) and pauses if that isn't enough"),
    Field("watchdog_minutes", "Stop stuck work after (min, 0 = off)", "Automation", "int", lo=0, hi=600, step=5,
          help="An export, measurement or check that makes no progress for this long is stopped, tried once "
              "more, then skipped with the reason. Stalled downloads give up by themselves"),
    Field("auto_prefetch", "Download the next video while clipping", "Automation", "bool",
          help="The next video downloads while this one is being clipped, so it's ready to start straight "
               "away. Still waits the minutes between downloads"),
    Field("cleanup_auto", "Clean up by itself once a day", "Automation", "bool",
          help="Automation clears out what's ticked below, without asking. Clean up… in the Library or "
               "Automation tab shows what would go, and how much space it frees, before anything is deleted"),
    Field("cleanup_kinds", "What to clear", "Automation", "multi", choices=CLEANUP_KINDS,
          help="Clips waiting to post are never cleared, and nor is anything for a video that's queued, being "
               "clipped or in the Studio's list. Posted clips are cleared by posting itself once they're up"),
    Field("cleanup_days", "Only things older than (days)", "Automation", "int", lo=1, hi=365,
          help="Posted clips count from when you posted them; everything else from when it was made"),
    Field("weekly_report", "Weekly report on Monday mornings", "Automation", "bool",
          help="At 9 am on Mondays (or the first time automation runs after that) a notification sums up the "
               "week on each TikTok account: views against the week before, followers, the best and weakest "
               "post, clips against Creator videos, Creator Rewards progress. It goes to Telegram too once the "
               "bot is set up. Performance → Weekly report shows it any time"),
    Field("telegram_on", "Use the Telegram bot", "Telegram", "bool",
          help="Automation runs a Telegram bot of your own, so you can ask for videos, watch what's waiting to post "
               "and delete bad ones from your phone. Only the phones you link can use it. It runs while automation "
               "does"),
    Field("telegram_token", "Bot token", "Telegram", "secret", placeholder="123456789:AAH…",
          help="In Telegram, open @BotFather, send /newbot and pick a name and a username: it gives you the token. "
               "Use a bot just for ClipMonster: two programs can't share one"),
    Field("telegram_send", "Send me", "Telegram", "multi", choices=TELEGRAM_SENDS,
          help="What the bot tells you without being asked. Whatever you ask for in the chat is always answered "
               "there"),
    Field("telegram_app", "Mini App: the bot as an app inside Telegram", "Telegram", "bool",
          help="The ClipMonster button left of the message box opens an app in Telegram: watch the videos waiting "
               "to post, OK or delete them, say what's wrong with one so the AI makes it again, and see what each "
               "of the two people sharing the bot did. Off: the bot works by messages and buttons in the chat"),
    Field("telegram_app_url", "Mini App address", "Telegram", "str", placeholder="blank = automatic",
          help="Telegram only opens an https:// address your phone can reach. Blank: ClipMonster runs Cloudflare's "
               "free cloudflared (downloaded once, some tens of MB) for a temporary address, new each time automation "
               "starts; videos you watch pass through Cloudflare, encrypted, and only the people linked to the bot "
               "are answered. Or put an address of your own here that leads to this PC's Mini App port (a named "
               "Cloudflare tunnel, Tailscale Funnel, a reverse proxy)"),
    Field("telegram_app_port", "Mini App port on this PC", "Telegram", "int", lo=1024, hi=65535,
          help="Where the Mini App listens on this PC (only this PC can connect to it; the address above leads "
               "here). Change it only if another program uses 47330"),
    Field("remote_on", "Let my Mac run ClipMonster", "Mac app", "bool",
          help="Automation answers ClipMonster on your Mac: it shows what automation is doing, plays the videos "
               "waiting to post so you can OK or delete them, asks for videos, clips and requests, and lends the "
               "Mac this PC's local AI and graphics card to make clips with. Only Macs you link here can use it, "
               "over an encrypted connection. It runs while automation does"),
    Field("remote_port", "Port", "Mac app", "int", lo=1024, hi=65535,
          help="The network port the Mac connects to (47320 unless another program uses it). The Mac finds the "
               "PC by itself on the same network; type this port with the PC's address if it doesn't"),
]


# ClipMonster for Mac shows the same settings, in the Mac's own words: its graphics run Metal, not Vulkan, its
# videos are encoded by the Mac's media engine, and a linked PC can lend its local AI (ytclip/mac).
MAC_WORDING = {
    "appearance": dict(choices=(("dark", "Dark"), ("light", "Light"), ("system", "Match the Mac")),
                       help="Dark suits editing video; Light matches a light Mac. Takes effect when ClipMonster opens "
                            "again"),
    "mode": dict(choices=(("auto", "Smart signals"), ("ai", "AI picks (the AI chosen under AI)"),
                          ("chapters", "YouTube chapters"))),
    # (not Safari: macOS keeps its logins from other apps unless each is given Full Disk Access by hand, and
    # without it every download fails)
    "cookies_browser": dict(choices=(("", "None"), ("chrome", "Chrome"), ("firefox", "Firefox"), ("brave", "Brave"),
                                     ("edge", "Edge")),
                            help="Signed-in downloads keep YouTube from asking this Mac to confirm it's not a bot, "
                                 "and open age-restricted and members-only videos. Pick the browser you're signed in "
                                 "to YouTube in (for Chrome, Brave or Edge, macOS asks once to let ClipMonster use "
                                 "its key in the Keychain). Safari keeps its logins to itself. A spare Google "
                                 "account is safest"),
    "speed": dict(choices=(("quiet", "Quiet (slowest, the Mac stays free)"), ("balanced", "Balanced"),
                           ("full", "Full power (every core and the graphics)")),
                  help="Quiet keeps the Mac free for other things and works slowest. Balanced uses half the CPU and "
                       "the Mac's graphics for Whisper. Full power uses every core and the graphics"),
    "gpu_whisper": dict(label="Whisper on the Mac's graphics (Balanced and Full power)",
                        help="Types out speech on the Mac's graphics (Metal), several times faster than the CPU on "
                             "Apple silicon. Downloads the model for it once. Falls back to the CPU if it fails"),
    "ai_provider": dict(choices=MAC_AI_PROVIDERS,
                        help="Which AI picks clips, polishes them and follows special requests. Claude and Gemini "
                             "need an API key (kept in your Mac's Keychain) and are paid per use. Local AI runs a "
                             "model on this Mac. Your PC's local AI asks the model on your linked PC: free and "
                             "private, and the Mac stays cool"),
    "local_model": dict(choices=MAC_LOCAL_MODELS,
                        help="Runs on this Mac: free, private and offline once it's downloaded (from Hugging Face, "
                             "once). 'Pick for this Mac' takes the biggest model that fits in the memory the Mac's "
                             "graphics can use (about two thirds of its memory on Apple silicon): Llama 3.2 3B on "
                             "8 GB, Mistral Nemo 12B on 16 to 32 GB, Qwen2.5 32B on 36 GB or more. An Intel Mac runs "
                             "Llama 3.2 3B on its CPU. Or pick Your PC's local AI as the AI, and the PC's model answers"),
    "local_model_path": dict(help="Any instruction-tuned GGUF model, such as Qwen or Gemma. Its own header says how "
                                  "big it is, so it's sized to the Mac's memory like the others"),
    "local_gpu": dict(label="Local AI on the Mac's graphics",
                      help="Much faster: Metal on Apple silicon, where the graphics share the Mac's memory. If it "
                           "fails, it carries on on the CPU by itself"),
    "local_context": dict(label="Local AI memory (tokens, 0 = sized to this Mac)",
                          help="How much transcript it reads at once: up to 32,768 tokens (about an hour of talk) "
                               "when the Mac has the memory for it. Longer videos are read in parts either way"),
    "local_gpu_layers": dict(label="Local AI layers on the graphics (-1 = all that fit)",
                             help="Leave at -1: ClipMonster and llama.cpp measure the memory the Mac's graphics can "
                                  "use and fit as many as they can. 0 runs it on the CPU only"),
    "encoder": dict(choices=MAC_ENCODERS,
                    help="Auto uses the Mac's media engine (VideoToolbox), which is quick and keeps the Mac cool; "
                         "CPU (x264) is slower and a little sharper at the same size"),
}


def _for_the_mac(schema: list[Field]) -> None:
    for f in schema:
        own = MAC_WORDING.get(f.name, {})
        for key, value in own.items():
            setattr(f, key, value)
        if "label" not in own:
            f.label = f.label.replace("this PC", "this Mac")
        if "help" not in own:
            f.help = f.help.replace("this PC", "this Mac").replace("the PC ", "the Mac ")
        if "choices" not in own and f.choices:
            f.choices = tuple((k, v.replace("this PC", "this Mac") if isinstance(v, str) else v) for k, v in f.choices)


if MAC:
    _for_the_mac(SCHEMA)
