"""Posting settings and where posting keeps its files.

Posting keeps its own small settings file next to ClipMonster's (posting/settings.json), because each
TikTok account lays its own schedule and posting defaults over it (Account.apply). The AI keys and
models are ClipMonster's own (Settings → AI); nothing here holds a key.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .. import settings as main_settings
from . import safefile


def app_dir() -> Path:
    """%APPDATA%\\ClipMonster\\posting: the posting library, accounts, settings and the Live feeds."""
    path = main_settings.app_dir() / "posting"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    """%LOCALAPPDATA%\\ClipMonster\\posting: each account's browser profile (its TikTok login),
    thumbnails and the browser's error screenshots. Never roamed or synced."""
    return main_settings.local_dir("posting")


def thumbs_dir() -> Path:
    path = cache_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Weights:
    """How much each part of a clip's posting score counts. Only relative sizes matter."""

    hook: float = 0.25
    format: float = 0.15
    duration: float = 0.15
    pacing: float = 0.15
    audio: float = 0.15
    visual: float = 0.10
    source: float = 0.20  # ClipMonster's own clip score
    ai: float = 0.35  # the AI's rating, when there is one


@dataclass
class Settings:
    # Scoring (scoring.py): only orders the queue ("Smart order"); every clip that passed the quality check posts
    weights: Weights = field(default_factory=Weights)
    ideal_min_len: float = 61.0  # TikTok's Creator Rewards pay for videos over a minute
    ideal_max_len: float = 90.0
    target_lufs: float = -14.0
    # The AI (ytclip.ai, with ClipMonster's keys): the niche it writes hashtags for, and how many
    ai_niche: str = ""
    ai_hashtag_count: int = 4
    ai_hashtags: bool = True  # each new clip gets hashtags from the AI (one quick text-only call)
    # Posting. default_privacy, default_hashtags, ai_niche, the schedule and autopilot are per account
    # (accounts.Account); Account.apply() lays them over a copy of these.
    browser_channel: str = "msedge"  # msedge | chrome
    show_browser: bool = True  # TikTok is less likely to block a browser you can see
    default_privacy: str = "public"  # public | friends | private
    allow_comments: bool = True
    allow_duet: bool = True
    allow_stitch: bool = True
    ai_generated_label: bool = False
    caption_template: str = "{caption} {hashtags}"
    default_hashtags: str = "#fyp"
    # Schedule (per account; these only seed new accounts)
    posts_per_day: int = 2
    first_post_time: str = "11:00"
    last_post_time: str = "21:00"
    min_gap_minutes: int = 180
    vary_minutes: int = 15
    warm_up_start: float = 0.0
    schedule_seed: str = ""
    autopilot: bool = True
    browser_logged_in: bool = False
    # Live view (live.py): logged-in accounts' profile pages are read this often (seconds), in the background
    live_interval: int = 300
    live_show_browser: bool = False  # show the window while reading (if TikTok keeps asking for a captcha)
    live_alerts: bool = True  # notifications: a video taking off, milestones, a post going up
    # Records and clearing up (records.py): a record of every posted clip, then its files go
    records_folder: str = ""  # "" = Documents\ClipMonster\Posted videos
    delete_after_post: bool = True  # delete a clip's files once TikTok shows the post on the profile
    recycle: bool = False  # to the Recycle Bin rather than deleted for good

    @classmethod
    def path(cls) -> Path:
        return app_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        data = safefile.load(cls.path(), "your posting settings")
        if not isinstance(data, dict):
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in data.items() if k in known and k != "weights"})
        w = data.get("weights") or {}
        wknown = {f.name for f in fields(Weights)}
        try:
            s.weights = Weights(**{k: float(v) for k, v in w.items() if k in wknown})
        except (TypeError, ValueError):
            s.weights = Weights()
        return s

    def save(self) -> None:
        safefile.save(self.path(), asdict(self))


SCHEMA = [
    main_settings.Field("delete_after_post", "Delete a clip once it's posted", "Posting", "bool",
                        help="A record of the clip and how it did is kept in the records folder; the video, its "
                             "captions file and thumbnail are deleted once TikTok shows the post on the account"),
    main_settings.Field("recycle", "Send deleted clips to the Recycle Bin", "Posting", "bool",
                        help="Lets you change your mind; the space is only freed when you empty it"),
    main_settings.Field("records_folder", "Records folder", "Posting", "path",
                        help="Where each posted clip's record goes. Empty = Documents\\ClipMonster\\Posted videos"),
    main_settings.Field("ai_hashtags", "AI hashtags for each new clip", "Posting", "bool",
                        help="One quick text-only call per clip to the AI in Settings → AI, fitted to the account's "
                             "niche and the hashtags that got its posts the most views"),
    main_settings.Field("ai_hashtag_count", "Hashtags per clip", "Posting", "int", lo=0, hi=10),
    main_settings.Field("caption_template", "Caption", "Posting", "str",
                        help="{caption} is the clip's hook (or what you wrote), {hashtags} the account's and the "
                             "clip's hashtags, {title} the clip's title"),
    main_settings.Field("allow_comments", "Allow comments", "Posting", "bool"),
    main_settings.Field("allow_duet", "Allow duets", "Posting", "bool"),
    main_settings.Field("allow_stitch", "Allow stitches", "Posting", "bool"),
    main_settings.Field("ai_generated_label", "Label posts as AI-generated", "Posting", "bool"),
    main_settings.Field("browser_channel", "Browser to post with", "Posting", "choice",
                        choices=(("msedge", "Microsoft Edge"), ("chrome", "Google Chrome"))),
    main_settings.Field("show_browser", "Show the browser while posting", "Posting", "bool",
                        help="TikTok is less likely to block a browser you can see, and you can solve a captcha "
                             "if one appears. It opens for about a minute per post"),
    main_settings.Field("live_interval", "Read each account's stats", "Posting", "choice",
                        choices=((120, "Every 2 min"), (300, "Every 5 min"), (600, "Every 10 min"),
                                 (900, "Every 15 min"), (1800, "Every 30 min"))),
    main_settings.Field("live_show_browser", "Show the browser while reading stats", "Posting", "bool",
                        help="Turn on if TikTok keeps asking for a captcha, and solve it once in the window"),
    main_settings.Field("live_alerts", "Notifications", "Posting", "bool",
                        help="A post going up, a video taking off, milestones (only while the window or tray icon "
                             "is open)"),
    main_settings.Field("ideal_min_len", "Ideal length from (s)", "Posting", "float", lo=1, hi=600),
    main_settings.Field("ideal_max_len", "Ideal length to (s)", "Posting", "float", lo=1, hi=600),
]
