"""Niches: one TikTok account's topic each, with its own sources, look and output folder.

A niche lists the channels and Creative Commons searches its videos come from (and, for videos
found on the charts, the categories and title words that fit it), how many videos a day it gets,
and the style its clips are made in. Every picked video belongs to one niche, and its clips go to
Automation/<niche>/, so each TikTok account can take its own folder.

With no niches the app works as it always did: one channel list and one set of searches
(Settings → Picking), and every clip in the Automation folder.

Niches are saved in settings.json as a list of plain dicts (Settings.niches). A niche's id never
changes, so renaming one keeps its queued videos, its picks and TikTok Manager's link to it.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import asdict, dataclass, fields

from .organize import safe_name
from .sources import ChannelRef, parse_channels, parse_searches
from .trending import CATEGORIES, History, TrendingVideo

DEFAULT_DAILY = 2
MAX_DAILY = 50
_RESERVED = re.compile(r"con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$", re.I)  # Windows device names
# Scripts written without spaces between words: a title word there is found anywhere in the title.
_NO_SPACES_RANGES = ("\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff"  # Thai, Lao, Burmese, Khmer
                     "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff")  # Japanese kana, Chinese
_NO_SPACES = re.compile(f"[{_NO_SPACES_RANGES}]")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def folder_name(name: str) -> str:
    """The folder a niche called `name` gets: safe on Windows, never a device name like CON or NUL."""
    folder = safe_name(" ".join(name.split()), 40)
    stem, dot, rest = folder.partition(".")
    if _RESERVED.fullmatch(stem.rstrip(" ")):
        return f"{stem}_{dot}{rest}"
    return folder


@dataclass
class Niche:
    id: str = ""
    name: str = ""
    account: str = ""  # the TikTok account it's for, e.g. @gamingclips (shown, and passed to TikTok Manager)
    enabled: bool = True  # off: nothing new is picked for it
    daily: int = DEFAULT_DAILY  # videos picked per day
    channels: str = ""  # channel list lines, as Settings.pick_channels
    searches: str = ""  # Creative Commons searches, one per line
    categories: str = ""  # YouTube category ids, "20, 24": trending videos in them fit this niche
    keywords: str = ""  # comma separated: trending videos with one in the title fit this niche
    preset: str = ""  # "" = Automation's style
    view: str = ""  # "" = Automation's top/bottom view
    stack_folder: str = ""  # "" = the second-video folder in Settings → Layout
    instructions: str = ""  # what makes a good clip for this account, added to the AI's instructions

    @classmethod
    def from_json(cls, d) -> "Niche | None":
        if not isinstance(d, dict):
            return None
        known = {f.name: f.type for f in fields(cls)}
        n = cls(**{k: v for k, v in d.items() if k in known})
        for f in fields(cls):  # hand-edited files: wrong types fall back to the default
            v = getattr(n, f.name)
            if f.type == "str" and not isinstance(v, str):
                setattr(n, f.name, "" if v is None else str(v))
            elif f.type == "bool" and not isinstance(v, bool):
                setattr(n, f.name, bool(v))
        try:
            n.daily = max(1, min(MAX_DAILY, int(n.daily)))
        except (TypeError, ValueError):
            n.daily = DEFAULT_DAILY
        n.name = " ".join(n.name.split())
        # A hand-written niche with no id gets one made from its name, so every read agrees on it.
        n.id = re.sub(r"[^\w-]", "", n.id) or hashlib.sha1(n.folder.lower().encode()).hexdigest()[:8]
        return n if n.name else None

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def folder(self) -> str:
        """The niche's folder inside the Automation folder."""
        return folder_name(self.name)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.account})" if self.account else self.name

    def channel_refs(self) -> tuple[list[ChannelRef], list[str]]:
        return parse_channels(self.channels)

    def search_list(self) -> list[str]:
        return parse_searches(self.searches)

    def category_ids(self) -> set[str]:
        return {c for c in re.split(r"[\s,;]+", self.categories) if c and c in CATEGORIES}

    def keyword_list(self) -> list[str]:
        return [" ".join(k.split()).lower() for k in self.keywords.split(",") if k.strip()]


# ---------------------------------------------------------------- reading them from settings


def load(s) -> list[Niche]:
    """Every niche in the settings, in order. Unreadable entries, and a second niche whose name
    (or folder) repeats an earlier one, are left out. A repeated id (a niche copied by hand) gets a
    suffix, the same one on every read."""
    raw = getattr(s, "niches", None)
    out: list[Niche] = []
    seen_ids: set[str] = set()
    seen_folders: set[str] = set()
    for d in raw if isinstance(raw, list) else []:
        n = Niche.from_json(d)
        if n is None or n.folder.lower() in seen_folders:
            continue
        base, i = n.id, 2
        while n.id in seen_ids:
            n.id, i = f"{base}-{i}", i + 1
        seen_ids.add(n.id)
        seen_folders.add(n.folder.lower())
        out.append(n)
    return out


def active(s) -> list[Niche]:
    """The niches that pick videos (switched on)."""
    return [n for n in load(s) if n.enabled]


def find(s, niche_id: str) -> Niche | None:
    return next((n for n in load(s) if n.id == niche_id), None) if niche_id else None


def name_of(s, niche_id: str) -> str:
    n = find(s, niche_id)
    return n.name if n else ""


def to_settings(niches: list[Niche]) -> list[dict]:
    return [n.to_json() for n in niches]


def name_taken(niches: list[Niche], name: str, except_id: str = "") -> bool:
    folder = folder_name(name).lower()
    return any(n.folder.lower() == folder for n in niches if n.id != except_id)


def unique_name(niches: list[Niche], base: str = "New niche") -> str:
    name, i = base, 2
    while name_taken(niches, name):
        name, i = f"{base} {i}", i + 1
    return name


# ---------------------------------------------------------------- what a scan reads


def scan_sources(s) -> tuple[list[ChannelRef], list[str], list]:
    """(channels, searches, unreadable lines) a research scan should read. An unreadable line is
    the line itself, or (where, line) when it's in a niche.

    With niches: every switched-on niche's channels and searches, each once (a channel's note
    comes from the first niche that lists it). Without: the channel list and searches in
    Settings → Picking."""
    if not load(s):
        channels, bad = parse_channels(getattr(s, "pick_channels", ""))
        return channels, parse_searches(getattr(s, "pick_searches", "")), bad
    channels: dict[str, ChannelRef] = {}
    searches: dict[str, str] = {}
    bad: list = []
    for n in active(s):
        refs, unreadable = n.channel_refs()
        for ref in refs:
            channels.setdefault(ref.key, ref)
        bad += [(f"{n.name}'s channel list", x) for x in unreadable]
        for q in n.search_list():
            searches.setdefault(q.lower(), q)
    return list(channels.values()), list(searches.values()), bad


# ---------------------------------------------------------------- which niche a video is in


@dataclass
class Index:
    """Everything needed to sort videos into niches, built once per scan or rules change.

    Every niche, switched off or not, claims the channels and searches it lists, so a switched-off
    niche's videos are held back rather than handed to another account; a channel or search that a
    switched-on niche lists too is that niche's. Title words and categories only count for
    switched-on niches."""

    niches: list[Niche]
    by_channel: dict[str, str]  # YouTube channel id -> niche id
    by_search: dict[str, str]  # video id a niche's search found -> niche id

    @classmethod
    def build(cls, niches: list[Niche], history: History | None) -> "Index":
        by_channel: dict[str, str] = {}
        by_search: dict[str, str] = {}
        known_channels: dict = {}
        known_searches: dict = {}
        if history is not None:
            with history.lock:
                known_channels = {k: dict(v) for k, v in history.sources.get("channels", {}).items()}
                known_searches = {k: dict(v) for k, v in history.sources.get("searches", {}).items()}
        for n in sorted(niches, key=lambda n: not n.enabled):  # switched on first, otherwise in list order
            refs, _ = n.channel_refs()
            for ref in refs:
                cid = ref.value if ref.kind == "id" else (known_channels.get(ref.key) or {}).get("id", "")
                if cid:
                    by_channel.setdefault(cid, n.id)
            for q in n.search_list():
                for vid in (known_searches.get(q.lower()) or {}).get("videos") or []:
                    by_search.setdefault(vid, n.id)
        return cls(niches, by_channel, by_search)

    def niche_for(self, v: TrendingVideo) -> str:
        """The id of the niche a video belongs in, or "" when none fits. A niche listing the video's
        channel comes first, then one whose search found it, then title words, then categories;
        between niches on the same footing, the one higher up the list."""
        if not self.niches:
            return ""
        if v.channel_id in self.by_channel:
            return self.by_channel[v.channel_id]
        if v.id in self.by_search:
            return self.by_search[v.id]
        on = [n for n in self.niches if n.enabled]
        text = " ".join(f"{v.title} {v.channel}".lower().split())
        for n in on:
            if any(_has_words(text, k) for k in n.keyword_list()):
                return n.id
        for n in on:
            if v.category and v.category in n.category_ids():
                return n.id
        return ""


def _has_words(text: str, phrase: str) -> bool:
    if _NO_SPACES.search(phrase):
        return phrase in text
    # A word ends at a space, a mark or a letter of a language written without spaces ("Minecraftで建築").
    edge = rf"[^\W{_NO_SPACES_RANGES}]"
    return bool(re.search(rf"(?<!{edge}){re.escape(phrase)}(?!{edge})", text))


def assign(videos: list[TrendingVideo], niches: list[Niche], history: History | None) -> None:
    """Fill in each video's .niche (a niche id, or "" when none fits). Pass every niche, switched
    off ones included (see Index)."""
    index = Index.build(niches, history)
    for v in videos:
        v.niche = index.niche_for(v)


def assign_from_settings(videos: list[TrendingVideo], s, history: History | None) -> None:
    assign(videos, load(s), history)
