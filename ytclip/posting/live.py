"""Live activity: each logged-in account's TikTok profile page is read every few minutes.

A reading (`read`) is the account's profile numbers and its newest videos, taken from its profile
page in its own browser profile (the same Edge login it posts with), with the window hidden.
`Watcher.apply` compares it with the reading before and turns the difference into events for the
Performance tab's feed:

- a video's views, likes, comments and shares moving ("+120 views, +8 likes on …"),
- followers coming and going,
- a new video on the account (the clip the worker just posted, once TikTok lists it),
- a video taking off (far more views in the last 10 minutes than it usually gets),
- milestones (1K, 10K, 100K… views on a video; 100, 1K, 10K… followers).

The numbers also go into the library's stats, which link each posted clip to its TikTok video (so its
record is written and its files cleared, records.py) and keep the Performance tab and learning from
results current. Events are kept in live/<account id>.jsonl in the app's data
folder: the last 2,000, and all of today's for its totals.

Each reading opens the browser for a few seconds, so it waits its turn behind posting and other
browser work for the account, and isn't done more often than every 2 minutes. The profile page
shows public videos only, so "Only me" posts don't show.
Nothing here needs GUI code: the background worker runs it (service.py).
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .accounts import Account
from .config import Settings, app_dir
from .store import Profile, Store, TikTokVideo, VideoItem

# How often to read, as Settings.live_interval offers it: (seconds, words)
INTERVALS = [(120, "every 2 min"), (300, "every 5 min"), (600, "every 10 min"), (900, "every 15 min"),
             (1800, "every 30 min")]
MIN_INTERVAL = 120
RECENT_VIDEOS = 30  # newest videos read every time (the first screen of the profile's video grid)
FULL_EVERY = 2 * 3600  # all of them (up to ALL_VIDEOS, scrolling the grid) this often, so older ones stay current
ALL_VIDEOS = 120
HISTORY_SPACING = 30 * 60  # the library keeps one point per half hour of live readings (store._add_point)
MAX_BACKOFF = 15 * 60  # after errors, wait at most this long before trying again
AWAY_AFTER = 10 * 60  # a gap longer than this (or 3 readings' worth) is summed up as "while you were away"
FEED_VIDEOS = 6  # videos named in the feed per reading; the rest are summed up in one line
MAX_EVENTS = 2000
MAX_TODAY_EVENTS = 60_000  # today's are all kept beyond MAX_EVENTS, up to this (a day at 2 min is ~6,000)
VIEW_MILESTONES = (1_000, 10_000, 100_000, 1_000_000, 10_000_000)
FOLLOWER_MILESTONES = (100, 1_000, 10_000, 100_000, 1_000_000)
HOT_WINDOW = 10 * 60  # "taking off": views gained in the last 10 minutes…
HOT_MIN_VIEWS = 300  # …at least this many…
HOT_FACTOR = 3.0  # …and this many times the video's usual 10-minute gain
HOT_QUIET = 3600  # said at most once an hour per video
SERIES_SECONDS = 3 * 3600  # per-video readings kept in memory (for "taking off" and the trend lines)

# Error codes after which reading stops until the account is logged in (or set up) again
STOP_CODES = ("no_user", "no_browser", "logged_out")
CAPTCHA_WAIT = 90  # with the window shown, how long a reading waits for a captcha to be solved


class LiveError(Exception):
    """A reading that didn't work, with a code for Watcher.failed (STOP_CODES stop reading)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class Event:
    """One line of the Live page's activity feed."""

    at: float
    kind: str  # start | moved | more | followers | new | seen | hot | milestone | away | error | login_needed
    text: str
    video_id: str = ""
    views: int = 0  # what changed (for "today" totals); counted only from readings after `since`
    likes: int = 0
    comments: int = 0
    shares: int = 0
    followers: int = 0
    since: float = 0.0  # when the reading this was compared with was taken
    alert: bool = False  # worth a tray notification
    toasted: bool = False  # already shown as a Windows notification by the background process

    @classmethod
    def from_json(cls, d: dict) -> "Event | None":
        known = {f.name for f in fields(cls)}
        try:
            return cls(**{k: v for k, v in d.items() if k in known})
        except TypeError:
            return None


@dataclass
class Reading:
    """What one look at the account found."""

    at: float
    profile: Profile
    videos: list[TikTokVideo]
    full: bool = False  # all its videos were read, not only the newest
    logged_in: bool | None = None  # the browser profile still had its TikTok login (None: not known)


def read(s: Settings, account: Account, full: bool = False, log: Callable[[str], None] = lambda _m: None,
         cancelled: Callable[[], bool] = lambda: False, now: Callable[[], float] = time.time) -> Reading:
    """Read the account's profile page in its browser profile, out of sight (worker thread)."""
    from .tiktok import refresh_account
    from .tiktok.browser import BrowserError, Cancelled

    if not account.username and not account.logged_in:
        raise LiveError(f"{account.label} isn't logged in, so there's no profile to read. Log in on the "
                        "Accounts tab.", "no_user")
    session: dict = {}
    try:
        profile, videos = refresh_account(s, account, log, cancelled, max_videos=ALL_VIDEOS if full else RECENT_VIDEOS,
                                          headless=not s.live_show_browser, captcha_wait=CAPTCHA_WAIT, session=session)
    except Cancelled:
        raise LiveError("Cancelled.", "cancelled") from None
    except BrowserError as exc:
        text = str(exc)
        if "already open" in text:
            raise LiveError("The account's browser window is open, so it can't be read until it's closed.",
                            "busy") from None
        if "Couldn't find" in text:
            raise LiveError(text, "no_browser") from None
        if text.startswith("Not logged in"):
            raise LiveError(f"{account.label}'s TikTok login has run out. Log in again on the Accounts tab.",
                            "logged_out") from None
        if "captcha" in text.lower():
            if s.live_show_browser:
                raise LiveError("TikTok asked for a captcha and it wasn't solved. The next reading shows it again.",
                                "captcha") from None
            raise LiveError("TikTok is asking for a captcha. Turn on 'Show the browser while reading stats' in "
                            "Settings → Posting and solve it in the window that opens at the next reading.", "captcha") from None
        if "Couldn't tell which account" in text:
            raise LiveError(text, "no_user") from None
        raise LiveError(text, "no_profile") from None
    except Exception as exc:  # noqa: BLE001 - Playwright's timeouts and page errors
        name = type(exc).__name__
        raise LiveError("TikTok's page didn't load in time." if "Timeout" in name else
                        f"The browser couldn't read TikTok ({name}).", "page") from None
    if videos and not any(v.create_time for v in videos):
        # Only the grid's links and rounded view counts, without likes, comments or dates: taking it in
        # would read as every number dropping, then jumping back at the next full reading.
        videos = []
    if not videos and profile.video_count:
        raise LiveError("TikTok sent the profile but not its videos (it may want a captcha). Turn on "
                        "'Show the browser while reading stats' in Settings → Posting if this keeps happening.", "no_videos")
    return Reading(at=now(), profile=profile, videos=videos, full=full, logged_in=session.get("logged_in"))


def fmt(n: int) -> str:
    n = int(n)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{suffix}".replace(".0", "") if abs(v) < 100 else f"{v:.0f}{suffix}"
    return str(n)


def changes(views: int = 0, likes: int = 0, comments: int = 0, shares: int = 0, followers: int = 0) -> str:
    """'+120 views, +8 likes, −1 follower'."""
    bits = []
    for n, word in ((views, "view"), (likes, "like"), (comments, "comment"), (shares, "share"),
                    (followers, "follower")):
        if n:
            bits.append(f"{'+' if n > 0 else '−'}{fmt(abs(n))} {word}{'s' if abs(n) != 1 else ''}")
    return ", ".join(bits)


def video_name(v: TikTokVideo, item: VideoItem | None = None, limit: int = 48) -> str:
    """What to call a TikTok video in the feed: the clip's hook when it came from this app, else its caption."""
    text = " ".join(((item.headline if item else "") or v.description or "").split()) or "a video with no caption"
    return f"“{text[:limit - 1].rstrip()}…”" if len(text) > limit else f"“{text}”"


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 2 * 86400:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def _kept(events: list[Event], now: float) -> list[Event]:
    """The newest MAX_EVENTS events, and every one from today however many (today() adds them up)."""
    i = max(0, len(events) - MAX_EVENTS)
    start = day_start(now)
    while i > 0 and events[i - 1].at >= start and len(events) - i < MAX_TODAY_EVENTS:
        i -= 1
    return events[i:]


class EventLog:
    """The account's feed, kept as one JSON line per event."""

    def __init__(self, account_id: str, folder: Path | None = None):
        self.path = self.path_for(account_id, folder)
        self.events: list[Event] = []
        self._lines = 0  # lines in the file, so it's trimmed while the app runs too
        self.load()

    @staticmethod
    def path_for(account_id: str, folder: Path | None = None) -> Path:
        return (folder or app_dir() / "live") / f"{account_id}.jsonl"

    def load(self, now: float | None = None) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            raw = ""
        lines = raw.splitlines()
        out, torn = [], bool(raw) and not raw.endswith("\n")
        for line in lines:
            try:
                e = Event.from_json(json.loads(line))
            except ValueError:  # a line cut short by a crash
                torn = True
                continue
            if e:
                out.append(e)
        self.events = _kept(out, time.time() if now is None else now)
        self._lines = len(lines)
        if torn or self._lines > len(self.events) + MAX_EVENTS // 2:
            self._rewrite()  # without the torn line, so the next event isn't glued onto it

    def add(self, events: list[Event], now: float | None = None) -> None:
        if not events:
            return
        self.events = _kept(self.events + events, time.time() if now is None else now)
        if self._lines + len(events) > len(self.events) + MAX_EVENTS // 2 and self._rewrite():
            return  # otherwise (a locked temp file, say) append as usual and trim another time
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
            self._lines += len(events)
        except OSError:
            pass  # the feed still shows them; only the copy on disk is behind

    def _rewrite(self) -> bool:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(asdict(e), ensure_ascii=False) + "\n" for e in self.events),
                           encoding="utf-8")
            tmp.replace(self.path)
            self._lines = len(self.events)
            return True
        except OSError:
            return False

    def clear(self) -> None:
        self.events = []
        self.remove(self.path)

    @staticmethod
    def remove(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass


def day_start(now: float) -> float:
    d = dt.datetime.fromtimestamp(now)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def today(events: list[Event], now: float | None = None) -> dict[str, int]:
    """What the feed saw change since local midnight: views, likes, comments, shares, followers.

    A reading compared with one from before midnight (after the app was closed overnight, say)
    isn't counted, because part of that change happened yesterday.
    """
    now = time.time() if now is None else now
    start = day_start(now)
    out = {"views": 0, "likes": 0, "comments": 0, "shares": 0, "followers": 0}
    for e in events:
        if e.at >= start and e.since >= start:
            for k in out:
                out[k] += getattr(e, k)
    return out


@dataclass
class VideoLive:
    """The live view's own record of one video: recent readings, for trends and "taking off"."""

    series: deque = field(default_factory=lambda: deque())  # (at, views)
    hot_at: float = 0.0

    def add(self, at: float, views: int) -> None:
        self.series.append((at, views))
        while self.series and at - self.series[0][0] > SERIES_SECONDS:
            self.series.popleft()

    def gain(self, seconds: float, at: float | None = None) -> int | None:
        """Views gained in the last `seconds` (None until there are readings that far back)."""
        if len(self.series) < 2:
            return None
        at = self.series[-1][0] if at is None else at
        older = [v for t, v in self.series if at - t >= seconds * 0.8]
        if not older:
            return None
        return int(self.series[-1][1] - older[-1])

    def usual_gain(self, seconds: float) -> float | None:
        """The median gain per `seconds` over the readings kept, leaving out the last window."""
        pts = list(self.series)
        if len(pts) < 4 or pts[-1][0] - pts[0][0] < seconds * 3:
            return None
        gains = []
        j = 0
        for i, (t, v) in enumerate(pts):
            while j < i and t - pts[j][0] > seconds:
                j += 1
            if t - pts[j][0] >= seconds * 0.8 and pts[-1][0] - t >= seconds:
                gains.append(v - pts[j][1])
        return float(statistics.median(gains)) if gains else None


class Watcher:
    """Turns successive readings of one account into feed events and keeps its stats current."""

    def __init__(self, account_id: str, log_folder: Path | None = None):
        self.account_id = account_id
        self.log = EventLog(account_id, log_folder)
        self.prev_at = 0.0
        self.prev: dict[str, tuple[int, int, int, int]] = {}  # video id -> views, likes, comments, shares
        self.prev_followers: int | None = None
        self.best_followers = 0  # the most seen, so a count going back and forth over 1K is announced once
        self.best_views: dict[str, int] = {}
        self.videos: dict[str, VideoLive] = {}
        self.last_full = 0.0
        self.last_ok = 0.0
        self.failures = 0
        self.stopped = ""  # why reading stopped until the account is logged in again (or watching turned back on)
        self.error = ""
        self.next_at = 0.0

    # --- scheduling ---------------------------------------------------------------

    def want_full(self, now: float) -> bool:
        return now - self.last_full >= FULL_EVERY

    def schedule(self, s: Settings, now: float) -> None:
        self.next_at = now + max(MIN_INTERVAL, int(s.live_interval or 300))

    def failed(self, s: Settings, message: str, code: str, now: float) -> list[Event]:
        """A reading didn't work: wait longer each time, or stop when there's nothing it could read."""
        self.failures += 1
        self.error = message
        events = []
        if code in STOP_CODES:
            self.stopped = message
            events.append(Event(at=now, kind="error", text=f"Stopped watching: {message}", alert=True))
        else:
            base = max(MIN_INTERVAL, int(s.live_interval or 300))
            wait = min(MAX_BACKOFF, base * 2 ** min(self.failures, 6))
            if code == "busy":  # just waiting for a window to close: look again soon, it's not TikTok's fault
                wait = MIN_INTERVAL
            self.next_at = now + wait
            if self.failures == 1 or self.failures % 10 == 0:  # don't fill the feed while TikTok is down
                events.append(Event(at=now, kind="error",
                                    text=f"Couldn't read TikTok: {message} Trying again in {ago(wait)}."))
        self.log.add(events)
        return events

    def resume(self) -> None:
        """The account was logged in again (or watching was turned back on)."""
        self.stopped = self.error = ""
        self.failures = 0
        self.next_at = 0.0

    # --- a reading ------------------------------------------------------------------

    def _seed(self, store: Store) -> None:
        """Before the first reading of this run: compare with the stats the library already has."""
        with store.lock:
            vids = store.videos(self.account_id)
            p = store.profile(self.account_id)
            self.prev = {v.id: (v.views, v.likes, v.comments, v.shares) for v in vids.values()}
            last = max((v.history[-1][0] for v in vids.values() if v.history), default=0.0)
            self.prev_at = max(last, p.fetched_at)
            self.prev_followers = p.followers if p.fetched_at and p.has_stats else None
            self.best_followers = max(self.best_followers, self.prev_followers or 0)
            for vid, numbers in self.prev.items():
                self.best_views[vid] = max(self.best_views.get(vid, 0), numbers[0])

    def apply(self, store: Store, account: Account, s: Settings, r: Reading) -> list[Event]:
        """Take a reading in: events for the feed (already added to its log), stats saved to the store."""
        theirs = r.profile.username.lstrip("@").lower()
        if account.username and theirs and theirs != account.username.lstrip("@").lower():
            # A different TikTok account's page: its numbers would land in this one's stats.
            self.stopped = self.error = (f"TikTok showed @{r.profile.username}'s profile, not "
                                         f"{account.handle}'s. Check the username on the Accounts tab.")
            events = [Event(at=r.at, kind="error", alert=True, text=f"Stopped watching: {self.stopped}")]
            self.log.add(events)
            return events

        first_of_run = not self.last_ok
        if first_of_run:
            self._seed(store)
        never_read = not self.prev_at  # nothing in the library or from an earlier reading to compare with
        since = self.prev_at
        gap = r.at - since if since else 0.0
        away = bool(since) and gap > max(AWAY_AFTER, 3 * max(MIN_INTERVAL, int(s.live_interval or 300)))
        self.failures, self.error, self.stopped = 0, "", ""
        self.last_ok = r.at
        if r.full:
            self.last_full = r.at
        self.schedule(s, r.at)

        with store.lock:
            items = {i.tiktok_id: i for i in store.items.values() if i.account == account.id and i.tiktok_id}
        events: list[Event] = []
        if never_read:
            followers = f"{fmt(r.profile.followers)} followers, " if r.profile.has_stats else ""
            events.append(Event(at=r.at, kind="start", text=(
                f"Watching {account.handle} live: {followers}{len(r.videos)} videos read. Changes show up "
                "here from the next reading.")))

        # Videos that weren't on the account before: just posted (by the worker, or by hand on TikTok)
        new_ids = [] if never_read else [v.id for v in r.videos if v.id not in self.prev and self._is_new(v, since, r)]
        for v in r.videos:
            if v.id not in new_ids:
                continue
            when = f", posted {ago(r.at - v.create_time)} ago" if v.create_time else ""
            events.append(Event(at=r.at, kind="new", video_id=v.id, since=since,
                                views=v.views if not away else 0,
                                text=f"New video on {account.handle}: {video_name(v, items.get(v.id))}{when}."))

        # How each known video moved
        moved: list[tuple[int, TikTokVideo, tuple[int, int, int, int]]] = []
        for v in r.videos:
            live = self.videos.setdefault(v.id, VideoLive())
            live.add(r.at, v.views)
            before = self.prev.get(v.id)
            if before is None:
                continue
            d = (v.views - before[0], v.likes - before[1], v.comments - before[2], v.shares - before[3])
            if any(d):
                moved.append((d[0], v, d))
            best = max(before[0], self.best_views.get(v.id, 0))
            for m in VIEW_MILESTONES:
                if best < m <= v.views:
                    events.append(Event(at=r.at, kind="milestone", video_id=v.id, alert=True, since=since,
                                        text=f"🏆 {video_name(v, items.get(v.id))} passed {fmt(m)} views."))
            self.best_views[v.id] = max(best, v.views)
            hot = self._taking_off(v, live, r.at)
            if hot:
                events.append(Event(at=r.at, kind="hot", video_id=v.id, alert=True, since=since, text=hot))

        moved.sort(key=lambda m: (abs(m[2][0]), sum(abs(x) for x in m[2][1:])), reverse=True)
        if away and moved:
            total = [sum(m[2][k] for m in moved) for k in range(4)]
            events.append(Event(at=r.at, kind="away", since=since, views=total[0], likes=total[1],
                                comments=total[2], shares=total[3], text=(
                                    f"Since {ago(gap)} ago: {changes(*total)} across {len(moved)} "
                                    f"video{'s' if len(moved) != 1 else ''}.")))
        elif moved:
            for _, v, d in moved[:FEED_VIDEOS]:
                events.append(Event(at=r.at, kind="moved", video_id=v.id, since=since, views=d[0], likes=d[1],
                                    comments=d[2], shares=d[3],
                                    text=f"{changes(*d)} on {video_name(v, items.get(v.id))}"))
            rest = moved[FEED_VIDEOS:]
            if rest:
                total = [sum(m[2][k] for m in rest) for k in range(4)]
                events.append(Event(at=r.at, kind="more", since=since, views=total[0], likes=total[1],
                                    comments=total[2], shares=total[3],
                                    text=f"{changes(*total)} across {len(rest)} more videos"))

        # Followers (not when the page didn't show them)
        f_now = r.profile.followers if r.profile.has_stats else self.prev_followers
        if f_now is not None and self.prev_followers is not None and f_now != self.prev_followers:
            d = f_now - self.prev_followers
            events.append(Event(at=r.at, kind="followers", followers=d, since=since,
                                text=f"{changes(followers=d)} ({fmt(f_now)} now)"
                                + (f" since {ago(gap)} ago" if away else "")))
            for m in FOLLOWER_MILESTONES:
                if max(self.prev_followers, self.best_followers) < m <= f_now:
                    events.append(Event(at=r.at, kind="milestone", alert=True, since=since,
                                        text=f"🏆 {account.handle} reached {fmt(m)} followers."))
        if f_now is not None:
            self.best_followers = max(self.best_followers, f_now)

        # Into the library, and remember this reading for the next one
        store.update_profile(account.id, r.profile, now=r.at, spacing=HISTORY_SPACING)
        store.update_tiktok_videos(account.id, r.videos, now=r.at, spacing=HISTORY_SPACING)
        if r.profile.username and not account.username:
            account.username = r.profile.username
        for v in r.videos:
            self.prev[v.id] = (v.views, v.likes, v.comments, v.shares)
        self.prev_followers = f_now
        self.prev_at = r.at
        self.log.add(events, now=r.at)
        return events

    @staticmethod
    def _is_new(v: TikTokVideo, since: float, r: Reading) -> bool:
        """A video the last reading didn't have that really is new, not just one it didn't reach
        (an older video first seen in a full reading)."""
        if not v.create_time:
            return not r.full
        return v.create_time >= (since or r.at) - 3600

    def _taking_off(self, v: TikTokVideo, live: VideoLive, now: float) -> str:
        gain = live.gain(HOT_WINDOW, now)
        if gain is None or gain < HOT_MIN_VIEWS or now - live.hot_at < HOT_QUIET:
            return ""
        usual = live.usual_gain(HOT_WINDOW)
        if usual is None or gain < HOT_FACTOR * max(usual, 1.0):
            return ""
        live.hot_at = now
        return (f"🔥 {video_name(v)} is taking off: +{fmt(gain)} views in the last 10 minutes, "
                f"about {gain / max(usual, 1.0):.0f}× its usual pace.")

    # --- for the Live page --------------------------------------------------------------

    def trend(self, video_id: str) -> list[tuple[float, int]]:
        live = self.videos.get(video_id)
        return list(live.series) if live else []

    def last_hour(self, video_id: str) -> int | None:
        live = self.videos.get(video_id)
        return live.gain(3600) if live else None
