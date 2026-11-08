"""The posting library: every clip waiting to post, each account's queue and stats, and the post history,
saved as one JSON file (posting/library.json) that the window and the background worker share.

Both processes keep it in memory. save() merges in what the other process saved since this one last
read the file (safefile.merge_entries: per entry, the fields this process changed win) and refresh()
takes in the other process's changes without saving, so the window shows the worker's posts as they
happen and the worker sees the queue order you set."""

from __future__ import annotations

import os
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import safefile
from .config import app_dir
from .media import VIDEO_EXTS

# Item status lifecycle: new -> queued -> uploading -> posted | failed
# last_error of a video whose upload was cut off by the app closing. It may be on TikTok already,
# so it is never retried by itself; the next stats refresh marks it posted if it finds it there.
INTERRUPTED = ("The app closed while this was uploading, so it may have been posted. "
               "Check TikTok, and only retry it if it isn't there.")


@dataclass
class VideoItem:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path: str = ""
    title: str = ""
    added_at: float = field(default_factory=time.time)
    # Where the clip came from (ClipMonster folder / source video), used to spread similar clips apart
    source_group: str = ""
    source_score: float | None = None
    source_reason: str = ""
    source_channel: str = ""
    source_url: str = ""
    source_permission: str = ""  # why ClipMonster's picking rules allowed this video
    source_rating: int | None = None  # ClipMonster's AI "does it stand alone" rating, 1-10
    hook: str = ""  # ClipMonster's AI hook line (also the starting caption)
    warnings: list[str] = field(default_factory=list)  # ClipMonster's quality check notes
    credit: str = ""  # a line the caption must carry (Creative Commons credit)
    imported_by: str = ""  # "auto" when automation's clip came in by itself, "" when added by hand
    # The category the clip was made for, and the account that category posts to (posting's account id)
    category_name: str = ""  # the category's name, e.g. "Gaming"
    category_id: str = ""  # stable across renames; shared back in results.json
    for_account: str = ""  # the account id the category posts to ("" = none)
    # ClipMonster's facts about the clip for its record: the source video, where in it, how it was made
    facts: dict = field(default_factory=dict)
    ai_generated: bool = False  # made by the Video Creator: TikTok's "AI-generated content" label goes on
    # The review hold (intake.needs_review): "waiting" = queued, but it doesn't post until you OK it (Queue tab,
    # Telegram); "ok" = you did; "" = it never needed it.
    review: str = ""
    # Part series: one moment cut into parts, each but the last saying "follow for part 2".
    # `series` is unique across the library: ClipMonster's id is only unique within one clips folder, and
    # the same video can be clipped into several folders (one per category, or a later run), so it's keyed on it.
    series: str = ""
    part: int | None = None  # 1-based
    parts: int | None = None  # how many parts ClipMonster kept
    # Analysis + score
    info: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    score: dict = field(default_factory=dict)
    analyzed_at: float = 0.0
    analyze_error: str = ""
    thumb: str = ""
    ai: dict = field(default_factory=dict)  # score, hook, verdict, caption, hashtags, ideas
    # Post settings
    caption: str = ""
    hashtags: str = ""
    privacy: str = ""  # "" = use the default from settings
    # Queue
    status: str = "new"
    account: str = ""  # id of the account it's queued for / was posted to ("" while it's just in the library)
    queue_pos: int | None = None  # position in that account's queue
    pinned_at: float | None = None  # a fixed post time picked by the user (epoch seconds)
    planned_at: float | None = None  # computed by the scheduler
    # Result
    attempts: int = 0
    last_error: str = ""
    upload_started_at: float | None = None  # when the last upload attempt began
    posted_at: float | None = None
    tiktok_id: str = ""
    tiktok_url: str = ""
    publish_id: str = ""
    # How it went up: "browser" (posted by the worker) or "seen" (an interrupted upload the stats found on TikTok)
    posted_via: str = ""
    # records.py: its record (.txt beside a .json) and when its files were deleted after posting
    cleaned_at: float | None = None
    record: str = ""
    record_at: float = 0.0  # when the record was last written
    snapshots: dict = field(default_factory=dict)  # "1h" / "24h" / "7d" -> [at, views, likes, comments, shares]
    keep_reason: str = ""  # why its files were kept after posting (e.g. not seen on the profile after 2 days)

    @property
    def total(self) -> float | None:
        return self.score.get("total") if self.score else None

    @property
    def duration(self) -> float:
        return float(self.info.get("duration") or 0.0)

    @property
    def name(self) -> str:
        return self.title or Path(self.path).stem

    @property
    def headline(self) -> str:
        """What to call the clip where you pick and post it: ClipMonster's hook when it wrote one
        (rather than a file name like "01 - 1-37 - clip"), else the title."""
        return " ".join(self.hook.split()) or self.name

    @property
    def part_label(self) -> str:
        """'Part 2 of 3' for a clip in a part series, else ""."""
        if not self.series or not self.part:
            return ""
        return f"Part {self.part} of {self.parts}" if self.parts else f"Part {self.part}"

    def exists(self) -> bool:
        return Path(self.path).is_file()


@dataclass
class TikTokVideo:
    """A video on the TikTok account, with a short history of its numbers."""

    id: str
    description: str = ""
    create_time: float = 0.0
    url: str = ""
    cover: str = ""
    duration: float = 0.0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    history: list[list[float]] = field(default_factory=list)  # [fetched_at, views, likes, comments, shares]

    @property
    def engagement(self) -> float:
        return (self.likes + self.comments + self.shares + self.saves) / self.views * 100 if self.views else 0.0

    def views_change(self) -> int | None:
        h = self.history
        if len(h) < 2:
            return None
        # The live view keeps updating the newest point until it's half an hour after the one before;
        # until then compare with the point before that, so this is never just a minute's change.
        base = h[-3] if len(h) >= 3 and h[-1][0] - h[-2][0] < LATELY else h[-2]
        return int(h[-1][1] - base[1])


LATELY = 30 * 60  # "views lately" covers at least this long when the history allows (live.HISTORY_SPACING)


@dataclass
class Profile:
    username: str = ""
    nickname: str = ""
    avatar: str = ""
    bio: str = ""
    followers: int = 0
    following: int = 0
    likes: int = 0
    video_count: int = 0
    fetched_at: float = 0.0
    history: list[list[float]] = field(default_factory=list)  # [fetched_at, followers, likes, videos]
    has_stats: bool = True  # False: the reading had no numbers (the old numbers are kept)


@dataclass
class AccountStats:
    """What was last read from one TikTok account: profile numbers and its videos."""

    profile: Profile = field(default_factory=Profile)
    tiktok_videos: dict[str, TikTokVideo] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"profile": asdict(self.profile), "tiktok_videos": [asdict(v) for v in self.tiktok_videos.values()]}

    @classmethod
    def from_json(cls, data: dict) -> "AccountStats":
        return cls(profile=_build(Profile, data.get("profile") or {}),
                   tiktok_videos={d["id"]: _build(TikTokVideo, d) for d in data.get("tiktok_videos", [])
                                  if d.get("id")})


def _parts(data) -> tuple[dict, dict, set]:
    """(items by id, stats by account id, ignored) from a library file's JSON, as dicts."""
    return _parts_json(data if isinstance(data, dict) else {})


def _parts_json(data: dict) -> tuple[dict, dict, set]:
    items = {d["id"]: _renamed(d) for d in data.get("items", []) if isinstance(d, dict) and d.get("id")}
    accounts = data.get("accounts")
    stats = {aid: d for aid, d in accounts.items() if isinstance(d, dict)} if isinstance(accounts, dict) else {}
    return items, stats, set(data.get("ignored") or [])


# Library entries saved before 3.2, when categories were called niches.
_OLD_KEYS = {"niche": "category_name", "niche_id": "category_id"}


def _renamed(d: dict) -> dict:
    if not any(k in d for k in _OLD_KEYS):
        return d
    d = dict(d)
    for old, new in _OLD_KEYS.items():
        if old in d:
            value = d.pop(old)
            d.setdefault(new, value)
    return d


def _build(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


class Store:
    """Thread-safe: the worker's threads update items in the background. Shared with the other process
    through save() and refresh() (see the module docstring)."""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "library.json")
        self.lock = threading.RLock()
        self.items: dict[str, VideoItem] = {}
        self.stats: dict[str, AccountStats] = {}  # by account id
        self.ignored: set[str] = set()  # files removed from the library, which automation mustn't bring back
        self._base: tuple[dict, dict, set] = ({}, {}, set())  # the file as this process last read or wrote it
        self._sig = (0, 0)
        self.load()

    # --- persistence ------------------------------------------------------------

    def load(self) -> None:
        with safefile.file_lock(self.path):
            data = safefile.load(self.path, "your posting library")
            sig = safefile.signature(self.path)
        with self.lock:
            self.items, self.stats, self.ignored = {}, {}, set()
            self._take(*_parts(data))
            self._base = self._snapshot()
            self._sig = sig

    def save(self) -> None:
        """Write the library, first merging in what the other process saved since we last read it."""
        with self.lock, safefile.file_lock(self.path):
            if safefile.signature(self.path) != self._sig:
                disk = safefile.read_raw(self.path)
                if isinstance(disk, dict):
                    self._merge(_parts(disk))
            items, stats, ignored = self._snapshot()
            data = {
                "version": 3,
                "items": list(items.values()),
                "accounts": stats,
                "ignored": sorted(ignored),
            }
            safefile.save(self.path, data, indent=1)
            self._base = (items, stats, ignored)
            self._sig = safefile.signature(self.path)

    def refresh(self) -> bool:
        """Take in what the other process saved since we last read the file. True if anything came in."""
        if safefile.signature(self.path) == self._sig:
            return False
        with self.lock, safefile.file_lock(self.path):
            sig = safefile.signature(self.path)
            if sig == self._sig:
                return False
            disk = safefile.read_raw(self.path)
            if not isinstance(disk, dict):
                return False
            self._merge(_parts(disk))
            self._base = _parts(disk)
            self._sig = sig
            return True

    def _snapshot(self) -> tuple[dict, dict, set]:
        return ({i.id: asdict(i) for i in self.items.values()},
                {aid: st.to_json() for aid, st in self.stats.items()}, set(self.ignored))

    def _merge(self, disk: tuple[dict, dict, set]) -> None:
        """Lay the file's changes under ours (ours win field by field) into the objects in memory."""
        base_items, base_stats, base_ignored = self._base
        items, stats, ignored = self._snapshot()
        d_items, d_stats, d_ignored = disk
        merged_items = safefile.merge_entries(base_items, items, d_items)
        merged_stats = safefile.merge_entries(base_stats, stats, d_stats)
        merged_ignored = (d_ignored - (base_ignored - ignored)) | (ignored - base_ignored)
        self._take(merged_items, merged_stats, merged_ignored, keep_objects=True)

    def _take(self, items: dict[str, dict], stats: dict[str, dict], ignored: set, keep_objects: bool = False) -> None:
        known = {f.name for f in fields(VideoItem)}
        fresh: dict[str, VideoItem] = {}
        for item_id, d in items.items():
            old = self.items.get(item_id) if keep_objects else None
            if old is not None:  # the same object, so the window's references stay good
                for k, v in d.items():
                    if k in known:
                        setattr(old, k, v)
                fresh[item_id] = old
            else:
                fresh[item_id] = _build(VideoItem, d)
        self.items = fresh
        self.stats = {aid: AccountStats.from_json(d or {}) for aid, d in stats.items()}
        self.ignored = set(ignored)

    def interrupted(self) -> int:
        """Called by the one process that posts, when it starts: uploads a crash cut off may already be on
        TikTok, so they aren't queued again (that could post twice). They're marked failed, and the next
        stats reading marks one posted if it finds it there (link_posts)."""
        n = 0
        with self.lock:
            for it in self.items.values():
                if it.status == "uploading":
                    it.status, it.last_error = "failed", INTERRUPTED
                    n += 1
        return n

    def adopt(self, account_ids: list[str]) -> bool:
        """Queued or failed clips of an account that no longer exists go back to the library, and its
        stats go. Posted ones keep their account id so the history still says where they went.
        True if anything changed."""
        known = set(account_ids)
        changed = False
        with self.lock:
            for it in self.items.values():
                if it.account and it.account not in known and it.status != "posted":
                    self._unqueue(it)
                    changed = True
            for aid in [a for a in self.stats if a not in known]:
                del self.stats[aid]
                changed = True
            if changed:
                self._renumber()
        return changed

    def forget_account(self, account_id: str) -> None:
        """An account was removed: its queue goes back to the library, its stats are dropped."""
        with self.lock:
            for it in self.items.values():
                if it.account == account_id and it.status != "posted":
                    self._unqueue(it)
            self.stats.pop(account_id, None)
            self._renumber()

    # --- library ----------------------------------------------------------------

    def add_paths(self, paths: list[Path], watched: bool = False, now: float | None = None,
                  settle: float = 60.0) -> list[VideoItem]:
        """Add video files (folders are searched recursively). Already-known files are skipped, and so
        are clips the quality check rejected.

        `watched` is automation's own intake (intake.py): files removed from the library before stay
        out, and a file is only taken once it's finished, meaning it's listed in its folder's clips.json,
        or (for other files) nothing has written to it for `settle` seconds. Adding by hand brings a
        removed file back.
        """
        from .clipsjson import REJECTED_DIR, credit_line, needs_credit, read_folder_index

        now = time.time() if now is None else now
        with self.lock:
            known = {_key(i.path) for i in self.items.values()}
            added: list[VideoItem] = []
            files: list[Path] = []
            for p in paths:
                p = Path(p)
                if p.is_dir():
                    files += sorted(f for f in p.rglob("*") if f.suffix.lower() in VIDEO_EXTS
                                    and ".downloads" not in f.parts and REJECTED_DIR not in f.parts
                                    and not f.name.startswith("."))
                elif p.suffix.lower() in VIDEO_EXTS:
                    files.append(p)
            index_cache: dict[Path, dict] = {}
            for f in files:
                key = _key(f)
                if key in known or (watched and key in self.ignored):
                    continue
                if f.parent not in index_cache:
                    index_cache[f.parent] = read_folder_index(f.parent)
                meta = index_cache[f.parent].get(f.name)
                if meta and meta.get("rejected"):
                    continue
                if watched and not meta:
                    try:
                        if now - f.stat().st_mtime < settle:
                            continue  # still being written
                    except OSError:
                        continue
                known.add(key)
                self.ignored.discard(key)
                item = VideoItem(path=str(f.resolve()), title=f.stem, imported_by="auto" if watched else "")
                if meta:
                    item.title = meta.get("title") or item.title
                    item.source_group = meta.get("group", "")
                    item.source_score = meta.get("score")
                    item.source_reason = meta.get("reason", "")
                    item.source_channel = meta.get("channel", "")
                    item.source_url = meta.get("url", "")
                    item.source_permission = meta.get("permission", "")
                    item.source_rating = meta.get("rating")
                    item.hook = meta.get("hook", "")
                    item.caption = item.hook
                    item.warnings = list(meta.get("warnings") or [])
                    item.category_name = meta.get("category", "")
                    item.category_id = meta.get("category_id", "")
                    item.for_account = meta.get("account_id", "")
                    item.facts = dict(meta.get("facts") or {})
                    item.ai_generated = bool(meta.get("ai_generated"))
                    if meta.get("caption"):
                        item.caption = meta["caption"]
                    if meta.get("hashtags"):
                        item.hashtags = meta["hashtags"]
                    if meta.get("series"):
                        item.series = f"{f.parent.resolve()}#{meta['series']}"
                        item.part, item.parts = meta.get("part"), meta.get("parts")
                    if needs_credit(meta):
                        item.credit = credit_line(item.source_channel)
                else:
                    item.source_group = str(f.parent)
                self.items[item.id] = item
                added.append(item)
            return added

    def remove(self, ids: list[str]) -> None:
        with self.lock:
            for i in ids:
                it = self.items.pop(i, None)
                if it:
                    self.ignored.add(_key(it.path))
            self._renumber()

    def get(self, item_id: str) -> VideoItem | None:
        return self.items.get(item_id)

    def all(self) -> list[VideoItem]:
        with self.lock:
            return list(self.items.values())

    def copy_for(self, item_id: str, account: str) -> VideoItem | None:
        """Queue the same video for another account as a separate entry (its own status and stats)."""
        with self.lock:
            src = self.items.get(item_id)
            if src is None:
                return None
            fresh = VideoItem()
            copy = replace(src, id=fresh.id, added_at=fresh.added_at, info=deepcopy(src.info),
                           analysis=deepcopy(src.analysis), score=deepcopy(src.score), ai=deepcopy(src.ai),
                           status="new", account="", queue_pos=None, pinned_at=None, planned_at=None,
                           attempts=0, last_error="", posted_at=None, tiktok_id="", tiktok_url="", publish_id="",
                           posted_via="", warnings=list(src.warnings), cleaned_at=None, record="", record_at=0.0,
                           snapshots={}, keep_reason="", facts=deepcopy(src.facts))
            self.items[copy.id] = copy
            self.enqueue([copy.id], account)
            return copy

    # --- queue ------------------------------------------------------------------

    def queue(self, account: str | None = None) -> list[VideoItem]:
        """One account's queue in order (every account's when `account` is None)."""
        with self.lock:
            q = [i for i in self.items.values() if i.queue_pos is not None
                 and i.status in ("queued", "uploading", "failed") and (account is None or i.account == account)]
            return sorted(q, key=lambda i: i.queue_pos)

    def waiting_review(self, account: str | None = None) -> list[VideoItem]:
        """Queued videos held until you OK them, oldest first."""
        with self.lock:
            return sorted((i for i in self.items.values() if i.review == "waiting" and i.queue_pos is not None
                           and i.status == "queued" and (account is None or i.account == account)),
                          key=lambda i: i.added_at)

    def approve(self, ids: list[str]) -> int:
        """OK held videos to post when their time comes. Returns how many were waiting."""
        n = 0
        with self.lock:
            for item_id in ids:
                it = self.items.get(item_id)
                if it is not None and it.review == "waiting":
                    it.review = "ok"
                    n += 1
        return n

    def enqueue(self, ids: list[str], account: str) -> None:
        """Add videos to the end of `account`'s queue (videos in another queue are left where they are)."""
        with self.lock:
            nxt = max((i.queue_pos for i in self.items.values()
                       if i.queue_pos is not None and i.account == account), default=-1) + 1
            for item_id in ids:
                it = self.items.get(item_id)
                if not it or it.status == "posted" or it.queue_pos is not None:
                    continue
                it.queue_pos, it.status, it.last_error, it.account = nxt, "queued", "", account
                nxt += 1
            self._renumber()

    def move_to(self, ids: list[str], account: str) -> None:
        """Move queued videos to the end of another account's queue."""
        with self.lock:
            movable = [i for i in ids if (it := self.items.get(i)) and it.queue_pos is not None
                       and it.status != "uploading" and it.account != account]
            for item_id in movable:
                it = self.items[item_id]
                it.queue_pos, it.planned_at, it.pinned_at, it.status = None, None, None, "new"
            self.enqueue(movable, account)

    def dequeue(self, ids: list[str]) -> None:
        with self.lock:
            for item_id in ids:
                it = self.items.get(item_id)
                if it and it.status != "uploading":
                    self._unqueue(it)
            self._renumber()

    def _unqueue(self, it: VideoItem) -> None:
        it.queue_pos, it.planned_at, it.pinned_at = None, None, None
        if it.status in ("queued", "failed", "uploading"):
            it.status, it.account = "new", ""
        elif it.status == "new":
            it.account = ""

    def set_order(self, ids: list[str]) -> None:
        with self.lock:
            for pos, item_id in enumerate(ids):
                if item_id in self.items:
                    self.items[item_id].queue_pos = pos
            self._renumber()

    def _renumber(self) -> None:
        """Positions 0, 1, 2… within each account's queue."""
        q = sorted((i for i in self.items.values() if i.queue_pos is not None), key=lambda i: i.queue_pos)
        nxt: dict[str, int] = {}
        for it in q:
            it.queue_pos = nxt.get(it.account, 0)
            nxt[it.account] = it.queue_pos + 1

    def posted(self, account: str | None = None) -> list[VideoItem]:
        with self.lock:
            return sorted((i for i in self.items.values() if i.status == "posted"
                           and (account is None or i.account == account)),
                          key=lambda i: i.posted_at or 0, reverse=True)

    def last_post_time(self, account: str | None = None) -> float | None:
        times = [i.posted_at for i in self.items.values()
                 if i.posted_at and (account is None or i.account == account)]
        return max(times) if times else None

    def posts_since(self, since: float, account: str | None = None) -> int:
        return sum(1 for i in self.items.values()
                   if i.posted_at and i.posted_at >= since and (account is None or i.account == account))

    # --- account ----------------------------------------------------------------

    def stats_for(self, account: str) -> AccountStats:
        with self.lock:
            if account not in self.stats:
                self.stats[account] = AccountStats()
            return self.stats[account]

    def profile(self, account: str) -> Profile:
        return self.stats_for(account).profile

    def videos(self, account: str) -> dict[str, TikTokVideo]:
        return self.stats_for(account).tiktok_videos

    def update_tiktok_videos(self, account: str, videos: list[TikTokVideo], now: float | None = None,
                             spacing: float = 0.0) -> None:
        """Store what was read of the account's videos, each with a point added to its history.

        `spacing` is for the live view, which reads every minute or so: the newest point is then
        updated in place until it is `spacing` seconds after the one before it, so the 60 points
        kept still cover days rather than an hour.
        """
        now = time.time() if now is None else now
        with self.lock:
            known = self.videos(account)
            for v in videos:
                old = known.get(v.id)
                if old:
                    v.history = old.history
                    v.saves = v.saves or old.saves  # a reading without saves keeps the last ones
                    if not v.create_time and old.create_time:  # read off the grid: only views (rounded) and a link
                        v.create_time, v.likes, v.comments, v.shares = (old.create_time, old.likes, old.comments,
                                                                        old.shares)
                        v.cover, v.duration = v.cover or old.cover, v.duration or old.duration
                        v.description = old.description or v.description
                        v.views = max(v.views, old.views)  # "1.2K" isn't fewer views than 1,234
                v.history = _add_point(v.history, [now, v.views, v.likes, v.comments, v.shares], spacing)[-60:]
                known[v.id] = v
            self.link_posts(account)

    def update_profile(self, account: str, p: Profile, now: float | None = None, spacing: float = 0.0) -> None:
        now = time.time() if now is None else now
        with self.lock:
            st = self.stats_for(account)
            if not p.has_stats:  # keep the numbers the last full reading (or the browser) found
                old = st.profile
                p.followers, p.following, p.likes, p.video_count = old.followers, old.following, old.likes, \
                    old.video_count
                p.has_stats = bool(old.fetched_at) and old.has_stats
                p.history = list(old.history)
            else:
                p.history = _add_point(st.profile.history, [now, p.followers, p.likes, p.video_count],
                                       spacing)[-120:]
            p.username = p.username or st.profile.username
            p.bio = p.bio or st.profile.bio
            st.profile = p

    def link_posts(self, account: str) -> None:
        """Match items this app posted to the account's videos (by id, else by caption and time)."""
        with self.lock:
            mine = [i for i in self.items.values() if i.account == account]
            videos = self.videos(account)
            taken = {i.tiktok_id for i in mine if i.tiktok_id}
            for it in mine:
                interrupted = it.status == "failed" and it.last_error == INTERRUPTED
                if (it.status != "posted" and not interrupted) or it.tiktok_id:
                    continue
                # An empty caption posts as the video's name (tiktok.compose_caption).
                want = _norm(it.caption or (it.name if interrupted else ""))[:60]
                when = it.upload_started_at if interrupted else it.posted_at
                if interrupted and not (want and when):
                    continue
                best = None
                for v in videos.values():
                    if v.id in taken:
                        continue
                    close_in_time = when and abs(v.create_time - when) < 6 * 3600
                    same_text = want and _norm(v.description).startswith(want[:40])
                    if interrupted:  # only the same caption, close in time, proves that it went up
                        if same_text and close_in_time and v.create_time >= when - 600:
                            best = v
                            break
                        continue
                    if same_text and (close_in_time or not v.create_time):
                        best = v
                        break
                    if close_in_time and not want and best is None:
                        best = v
                if best:
                    it.tiktok_id, it.tiktok_url = best.id, best.url
                    taken.add(best.id)
                    if interrupted:
                        it.status, it.last_error, it.posted_at = "posted", "", best.create_time or when
                        it.queue_pos = it.pinned_at = it.planned_at = None
                        self._renumber()

    def video_for(self, item: VideoItem) -> TikTokVideo | None:
        if not item.tiktok_id or item.account not in self.stats:
            return None
        return self.stats[item.account].tiktok_videos.get(item.tiktok_id)


def _add_point(history: list[list[float]], point: list[float], spacing: float) -> list[list[float]]:
    """`history` with `point` added, or with its newest point replaced by it while that one is less
    than `spacing` seconds newer than the point before it."""
    if spacing and len(history) >= 2 and history[-1][0] - history[-2][0] < spacing:
        return history[:-1] + [point]
    return history + [point]


def _key(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())
