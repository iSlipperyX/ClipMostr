"""Categories: the topic of a TikTok account, with its own sources, look and output folder.

A category lists the channels and Creative Commons searches its videos come from (and, for videos
found on the charts, the YouTube categories and title words that fit it), how many videos a day it
gets, and the style its clips are made in. Every picked video belongs to one category, and its clips
go to Automation/<category>/ and straight into the posting queue of the category's TikTok account.

Every category posts to an account (account_id); one account can take several categories. A
category without one (from before 3.2, or whose account was removed) picks nothing and its waiting
videos wait, until it's given one: the Categories tab and `ytclip doctor` say so.

With no categories the app works as it always did: one channel list and one set of searches
(Settings → Picking), and every clip in the Automation folder.

Categories are saved in settings.json as a list of plain dicts (Settings.categories). A category's
id never changes, so renaming one keeps its queued videos, its picks and the account its clips post to.

Before 3.2 they were called niches: migrate_settings() reads the old settings key ("niches", and
each one's "categories" for its YouTube categories), and migrate_disk() rewrites settings.json once,
keeping a copy of the old file. The Automation folders keep their names, which come from the
category's name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, fields

from .organize import safe_name
from .sources import ChannelRef, parse_channels, parse_searches
from .trending import CATEGORIES as YT_CATEGORIES, History, TrendingVideo

DEFAULT_DAILY = 2
MAX_DAILY = 50
_RESERVED = re.compile(r"con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$", re.I)  # Windows device names
# Scripts written without spaces between words: a title word there is found anywhere in the title.
_NO_SPACES_RANGES = ("\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff"  # Thai, Lao, Burmese, Khmer
                     "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff")  # Japanese kana, Chinese
_NO_SPACES = re.compile(f"[{_NO_SPACES_RANGES}]")


MAX_CLIPS = 30
MAX_LEN = 600.0


def _number(value, kind, lo, hi):
    """A hand-edited number: the wrong type is the default (lo), out of range is clamped."""
    try:
        return max(lo, min(hi, kind(value)))
    except (TypeError, ValueError):
        return lo


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def folder_name(name: str) -> str:
    """The folder a category called `name` gets: safe on Windows, never a device name like CON or NUL."""
    folder = safe_name(" ".join(name.split()), 40)
    stem, dot, rest = folder.partition(".")
    if _RESERVED.fullmatch(stem.rstrip(" ")):
        return f"{stem}_{dot}{rest}"
    return folder


@dataclass
class Category:
    id: str = ""
    name: str = ""
    account: str = ""  # the TikTok account it's for, as shown: "@gamingclips" (kept up to date from account_id)
    account_id: str = ""  # the posting account (posting.accounts) its clips are queued for
    enabled: bool = True  # off: nothing new is picked for it
    daily: int = DEFAULT_DAILY  # videos picked per day
    channels: str = ""  # channel list lines, as Settings.pick_channels
    searches: str = ""  # Creative Commons searches, one per line
    finds: str = ""  # standing requests, one per line: "MW4 with good comedy" (lookup.py), run every few hours
    yt_categories: str = ""  # YouTube category ids, "20, 24": trending videos in them fit this category
    keywords: str = ""  # comma separated: trending videos with one in the title fit this category
    preset: str = ""  # "" = Automation's style
    view: str = ""  # "" = Automation's top/bottom view
    stack_folder: str = ""  # "" = the second-video folder in Settings → Layout
    instructions: str = ""  # special requests: what makes a good clip for this account, told to the AI
    clips_per_video: int = 0  # 0 = Automation's style decides
    min_len: float = 0.0  # clip length in seconds; 0 = Automation's style decides
    max_len: float = 0.0
    paused: bool = False  # paused in the Automation tab: nothing new is picked and its waiting videos wait

    @classmethod
    def from_json(cls, d) -> "Category | None":
        if not isinstance(d, dict):
            return None
        known = {f.name: f.type for f in fields(cls)}
        if "categories" in d and "yt_categories" not in d:  # a niche from before 3.2
            d = {**d, "yt_categories": d["categories"]}
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
        n.clips_per_video = _number(n.clips_per_video, int, 0, MAX_CLIPS)
        n.min_len = _number(n.min_len, float, 0.0, MAX_LEN)
        n.max_len = _number(n.max_len, float, 0.0, MAX_LEN)
        if n.min_len and n.max_len and n.max_len < n.min_len:
            n.min_len, n.max_len = n.max_len, n.min_len
        n.name = " ".join(n.name.split())
        # A hand-written category with no id gets one made from its name, so every read agrees on it.
        n.id = re.sub(r"[^\w-]", "", n.id) or hashlib.sha1(n.folder.lower().encode()).hexdigest()[:8]
        return n if n.name else None

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def folder(self) -> str:
        """The category's folder inside the Automation folder."""
        return folder_name(self.name)

    @property
    def needs_account(self) -> bool:
        """It has no TikTok account to post to, so it's held: nothing is picked or clipped for it."""
        return not self.account_id

    @property
    def held(self) -> bool:
        """Its waiting videos wait: paused in the Automation tab, or no account to post them to."""
        return self.paused or self.needs_account

    @property
    def on(self) -> bool:
        """Picking and clipping for it: switched on, not paused, and with an account to post to."""
        return self.enabled and not self.held

    @property
    def state(self) -> str:
        """Why it isn't picking, in a few words, or "" when it is."""
        if self.needs_account:
            return "Needs an account: nothing is picked or clipped until it has one"
        if self.paused:
            return "Paused in Automation: nothing is picked or clipped"
        if not self.enabled:
            return "Off: nothing new is picked"
        return ""

    def clip_overrides(self) -> dict:
        """The category's own clip count and length, for the settings its videos are clipped with."""
        out: dict = {}
        if self.clips_per_video:
            out["clip_count"] = self.clips_per_video
        if self.min_len:
            out["min_len"] = self.min_len
        if self.max_len:
            out["max_len"] = self.max_len
        return out

    @property
    def label(self) -> str:
        return f"{self.name} ({self.account})" if self.account else self.name

    def channel_refs(self) -> tuple[list[ChannelRef], list[str]]:
        return parse_channels(self.channels)

    def search_list(self) -> list[str]:
        return parse_searches(self.searches)

    def find_list(self) -> list[str]:
        return parse_finds(self.finds)

    def yt_category_ids(self) -> set[str]:
        return {c for c in re.split(r"[\s,;]+", self.yt_categories) if c and c in YT_CATEGORIES}

    def keyword_list(self) -> list[str]:
        return [" ".join(k.split()).lower() for k in self.keywords.split(",") if k.strip()]


# ---------------------------------------------------------------- reading them from settings


def load(s) -> list[Category]:
    """Every category in the settings, in order. Unreadable entries, and a second category whose name
    (or folder) repeats an earlier one, are left out. A repeated id (a category copied by hand) gets a
    suffix, the same one on every read."""
    raw = getattr(s, "categories", None)
    out: list[Category] = []
    seen_ids: set[str] = set()
    seen_folders: set[str] = set()
    for d in raw if isinstance(raw, list) else []:
        n = Category.from_json(d)
        if n is None or n.folder.lower() in seen_folders:
            continue
        base, i = n.id, 2
        while n.id in seen_ids:
            n.id, i = f"{base}-{i}", i + 1
        seen_ids.add(n.id)
        seen_folders.add(n.folder.lower())
        out.append(n)
    return out


def active(s) -> list[Category]:
    """The categories that pick videos (switched on, not paused, and with an account)."""
    return [n for n in load(s) if n.on]


def paused_ids(s) -> set[str]:
    """Categories whose waiting videos wait: paused in the Automation tab, or without an account."""
    return {n.id for n in load(s) if n.held}


def without_account(s) -> list[Category]:
    """Switched-on categories held because they have no account to post to."""
    return [n for n in load(s) if n.enabled and n.needs_account]


def find(s, category_id: str) -> Category | None:
    return next((n for n in load(s) if n.id == category_id), None) if category_id else None


def name_of(s, category_id: str) -> str:
    n = find(s, category_id)
    return n.name if n else ""


def for_account(s, account_id: str) -> list[Category]:
    """The categories whose clips post to this account."""
    return [n for n in load(s) if account_id and n.account_id == account_id]


def link_accounts(s, accounts) -> bool:
    """Keep each category's account in step with the posting accounts (`accounts`: posting.accounts.Account
    objects): the shown name follows the account's @username, a category whose account was removed loses
    it, and a category from before 3.0 that names an @username an account now has is linked to it.
    Changes `s.categories`; True if anything changed (the caller saves)."""
    accounts = list(accounts)
    by_id = {a.id: a for a in accounts}
    by_name = {a.username.lower(): a for a in accounts if a.username}
    out, changed = [], False
    for n in load(s):
        a = by_id.get(n.account_id)
        if a is None and n.account_id:
            n.account_id, changed = "", True
        if a is None and n.account:
            a = by_name.get(n.account.strip().lstrip("@").lower())
            if a is not None:
                n.account_id, changed = a.id, True
        if a is not None and n.account != a.handle:
            n.account, changed = a.handle, True
        out.append(n)
    if changed:
        s.categories = to_settings(out)
    return changed


def to_settings(categories: list[Category]) -> list[dict]:
    return [n.to_json() for n in categories]


def name_taken(categories: list[Category], name: str, except_id: str = "") -> bool:
    folder = folder_name(name).lower()
    return any(n.folder.lower() == folder for n in categories if n.id != except_id)


def unique_name(categories: list[Category], base: str = "New category") -> str:
    name, i = base, 2
    while name_taken(categories, name):
        name, i = f"{base} {i}", i + 1
    return name


# ---------------------------------------------------------------- what a scan reads


MAX_FINDS = 10  # standing requests per category (each costs about 200 quota units a run)


def parse_finds(text: str) -> list[str]:
    """Standing requests, one per line; # starts a comment line. Each request once."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if line and not line.startswith("#"):
            out.setdefault(line.lower(), line[:300])
    return list(out.values())[:MAX_FINDS]


def standing_requests(s) -> list[tuple[str, str]]:
    """(category id, request) for every standing request automation runs: each switched-on category's,
    or without categories the ones in Settings → Picking (category id "")."""
    if not load(s):
        return [("", r) for r in parse_finds(getattr(s, "pick_finds", ""))]
    return [(n.id, r) for n in active(s) for r in n.find_list()]


def scan_sources(s) -> tuple[list[ChannelRef], list[str], list]:
    """(channels, searches, unreadable lines) a research scan should read. An unreadable line is
    the line itself, or (where, line) when it's in a category.

    With categories: every switched-on category's channels and searches, each once (a channel's note
    comes from the first category that lists it). Without: the channel list and searches in
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


# ---------------------------------------------------------------- which category a video is in


@dataclass
class Index:
    """Everything needed to sort videos into categories, built once per scan or rules change.

    Every category, switched off or not, claims the channels and searches it lists, so a switched-off
    category's videos are held back rather than handed to another account; a channel or search that a
    switched-on category lists too is that category's. Title words and categories only count for
    switched-on categories."""

    categories: list[Category]
    by_channel: dict[str, str]  # YouTube channel id -> category id
    by_search: dict[str, str]  # video id a category's search found -> category id

    @classmethod
    def build(cls, categories: list[Category], history: History | None) -> "Index":
        by_channel: dict[str, str] = {}
        by_search: dict[str, str] = {}
        known_channels: dict = {}
        known_searches: dict = {}
        if history is not None:
            with history.lock:
                known_channels = {k: dict(v) for k, v in history.sources.get("channels", {}).items()}
                known_searches = {k: dict(v) for k, v in history.sources.get("searches", {}).items()}
        for n in sorted(categories, key=lambda n: not n.on):  # switched on first, otherwise in list order
            refs, _ = n.channel_refs()
            for ref in refs:
                cid = ref.value if ref.kind == "id" else (known_channels.get(ref.key) or {}).get("id", "")
                if cid:
                    by_channel.setdefault(cid, n.id)
            for q in n.search_list():
                for vid in (known_searches.get(q.lower()) or {}).get("videos") or []:
                    by_search.setdefault(vid, n.id)
        return cls(categories, by_channel, by_search)

    def category_for(self, v: TrendingVideo) -> str:
        """The id of the category a video belongs in, or "" when none fits. A category listing the video's
        channel comes first, then one whose search found it, then title words, then categories;
        between categories on the same footing, the one higher up the list."""
        if not self.categories:
            return ""
        if v.channel_id in self.by_channel:
            return self.by_channel[v.channel_id]
        if v.id in self.by_search:
            return self.by_search[v.id]
        on = [n for n in self.categories if n.on]
        text = " ".join(f"{v.title} {v.channel}".lower().split())
        for n in on:
            if any(_has_words(text, k) for k in n.keyword_list()):
                return n.id
        for n in on:
            if v.category and v.category in n.yt_category_ids():
                return n.id
        return ""


def _has_words(text: str, phrase: str) -> bool:
    if _NO_SPACES.search(phrase):
        return phrase in text
    # A word ends at a space, a mark or a letter of a language written without spaces ("Minecraftで建築").
    edge = rf"[^\W{_NO_SPACES_RANGES}]"
    return bool(re.search(rf"(?<!{edge}){re.escape(phrase)}(?!{edge})", text))


def assign(videos: list[TrendingVideo], categories: list[Category], history: History | None) -> None:
    """Fill in each video's .category_id (a category id, or "" when none fits). Pass every category, switched
    off ones included (see Index)."""
    index = Index.build(categories, history)
    for v in videos:
        v.category_id = index.category_for(v)


def assign_from_settings(videos: list[TrendingVideo], s, history: History | None) -> None:
    assign(videos, load(s), history)


# ---------------------------------------------------------------- from before 3.2 (niches)

LEGACY_FILE = "niches.json"  # a hand-made list some early setups kept beside settings.json
BACKUP_FILE = "settings.before-3.2.json"


def migrate_settings(data):
    """settings.json as read from disk, with niches (before 3.2) as categories. Leaves anything else
    alone, and leaves `data` itself unchanged."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if "niches" in out:
        old = out.pop("niches")
        if not out.get("categories") and isinstance(old, list):
            out["categories"] = old
    if isinstance(out.get("categories"), list):
        out["categories"] = [({k: v for k, v in d.items() if k != "categories"}
                              | {"yt_categories": d.get("yt_categories", d["categories"])})
                             if isinstance(d, dict) and "categories" in d else d for d in out["categories"]]
    if "research_niche_charts" in out:
        value = out.pop("research_niche_charts")
        out.setdefault("research_category_charts", value)
    return out


def _legacy_list(path) -> list | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        data = data.get("categories") or data.get("niches")
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else None


def migrate_disk(folder=None) -> list[str]:
    """Bring the files in ClipMonster's settings folder up to 3.2, once: settings.json's niches become
    categories (the old file is kept as settings.before-3.2.json), and a niches.json beside it is
    taken in when settings.json has none, then renamed niches.json.migrated. Safe to run from the app
    and the automation worker at once, and every start: once done, it finds nothing to do.
    Returns what it did, for the log."""
    from .settings import app_dir

    folder = folder or app_dir()
    done: list[str] = []
    path = folder / "settings.json"
    legacy = folder / LEGACY_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return done  # unreadable or busy: Settings.load copes, and the next start tries again
    if not isinstance(raw, dict):
        return done
    new = migrate_settings(raw)
    extra = _legacy_list(legacy) if legacy.exists() else None
    if extra and not new.get("categories"):
        new["categories"] = migrate_settings({"categories": extra})["categories"]
        done.append(f"took in {len(extra)} categor{'y' if len(extra) == 1 else 'ies'} from {LEGACY_FILE}")
    if new != raw:
        backup = folder / BACKUP_FILE
        try:
            if path.exists() and not backup.exists():
                backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            tmp = path.with_name(f"settings.json.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(new, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return done  # held by another program for a moment: the next start tries again
        if raw.get("niches"):
            done.append(f"renamed {len(raw['niches'])} niche{'s' if len(raw['niches']) != 1 else ''} to categories "
                        f"(the old settings are kept as {BACKUP_FILE})")
    if legacy.exists() and extra is not None:
        try:
            os.replace(legacy, legacy.with_name(LEGACY_FILE + ".migrated"))
        except OSError:
            pass
    return done
