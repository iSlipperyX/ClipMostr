"""The weekly report: how each TikTok account did in the past week, sent on Monday morning.

For each account, from the stats ClipMonster reads (Performance tab):

* what was posted: clips, Creator videos and anything posted by hand;
* the views all of its videos got in the week, against the week before;
* how many of them were on videos over a minute (the only views Creator Rewards pays for), and where the
  account stands on the program's goals;
* followers gained;
* the week's best post, its weakest (of those a day old or more), any older video that took off, and
  whether clips or Creator videos did better.

Then what automation made in the week, and what's waiting for you.

A week's views come from a small daily record of every video's views and the followers (stats-days.json:
a reading a day, three weeks kept). Until that record reaches back a week, an older video's views come
from its stats history, and when even that doesn't reach back far enough they're estimated from the
history it has: the report then says "about".

Automation sends it on Mondays from 9 am, once a week (or the first time it runs after that, that week):
a Windows notification, a copy in the reports folder and, when the bot is set up, a Telegram message.
Performance → Weekly report shows it any time, and `ytclip report --week` prints it.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .settings import app_dir

DAY = 86400.0
WEEK = 7 * DAY
SEND_WEEKDAY = 0  # Monday
SEND_HOUR = 9
RECORD_EVERY = 20 * 3600.0  # a reading a day (a little under, so it doesn't creep later each day)
RECORD_KEEP_DAYS = 22  # this week and the one before, with room
NEAR = 1.5 * DAY  # a stats point this close before a moment stands for the numbers then
SETTLED = DAY  # posts are compared once they're a day old
EARNS_SECONDS = 60.0  # Creator Rewards pays for views on videos over a minute (posting/tracker.py)
TOOK_OFF_MIN = 500  # an older video's week counts as taking off from this many views
STALE = 12 * 3600.0  # stats read longer ago than this are mentioned
REPORTS_KEPT = 26

Log = Callable[[str], None]


def fmt(n: float) -> str:
    """12.1K, 3M, 840."""
    from .posting.live import fmt as live_fmt

    return live_fmt(int(round(n)))


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


# ---------------------------------------------------------------- the daily record

class Record:
    """The daily record of each account's followers and its videos' views (stats-days.json).

    Each reading holds every account's numbers as they were last read from TikTok (with that time,
    `read_at`), so the views between two moments are exact, whatever the stats history kept."""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "stats-days.json")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        days = data.get("days") if isinstance(data, dict) else None
        self.days: list[dict] = sorted((d for d in (days or []) if isinstance(d, dict)
                                        and isinstance(d.get("at"), (int, float))
                                        and isinstance(d.get("accounts"), dict)), key=lambda d: d["at"])

    def due(self, now: float) -> bool:
        return not self.days or now - self.days[-1]["at"] >= RECORD_EVERY

    def _entries(self, account_id: str) -> list[dict]:
        out = [d["accounts"][account_id] for d in self.days if isinstance(d["accounts"].get(account_id), dict)]
        return sorted((e for e in out if isinstance(e.get("read_at"), (int, float))), key=lambda e: e["read_at"])

    def take(self, store, account_ids: list[str], now: float) -> bool:
        """Today's reading, when one is due. Accounts not read again since the last one are left out
        (that reading still stands for them)."""
        if not self.due(now):
            return False
        accounts = {}
        for aid in account_ids:
            p = store.profile(aid)
            vids = store.videos(aid)
            read_at = last_read(p, vids)
            if not read_at:
                continue
            before = self._entries(aid)
            if before and abs(before[-1]["read_at"] - read_at) < 60:
                continue
            accounts[aid] = {"read_at": read_at, "followers": int(p.followers) if p.fetched_at else None,
                             "views": {vid: int(v.views) for vid, v in vids.items()}}
        self.days.append({"at": now, "accounts": accounts})
        self.days = [d for d in self.days if now - d["at"] < RECORD_KEEP_DAYS * DAY]
        return True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "days": self.days}), encoding="utf-8")
        tmp.replace(self.path)

    def value_at(self, account_id: str, t: float, get: Callable[[dict], float | None]) -> tuple[float | None, bool]:
        """A number at time `t` (a video's views, the followers): between the readings either side of `t`, or
        the nearest one when only one side is within a day. (value, exact): exact is False when the readings
        either side are more than two days apart, so the line between them is a guess."""
        before = after = None
        for e in self._entries(account_id):
            v = get(e)
            if not isinstance(v, (int, float)):
                continue
            if e["read_at"] <= t:
                before = (e["read_at"], float(v))
            elif after is None:
                after = (e["read_at"], float(v))
        if before and after:
            (t0, v0), (t1, v1) = before, after
            return v0 + (v1 - v0) * (t - t0) / (t1 - t0), t1 - t0 <= 2 * DAY
        if before and t - before[0] <= DAY:
            return before[1], True
        if after and after[0] - t <= DAY:
            return after[1], True
        return None, False


def last_read(profile, videos: dict) -> float:
    """When the account's stats were last read (0 = never)."""
    return max([profile.fetched_at or 0.0] + [v.history[-1][0] for v in videos.values() if v.history])


def views_at(v, t: float, record: Record | None, account_id: str) -> tuple[int | None, bool]:
    """A TikTok video's views at time `t`: (views, exact), or (None, False) when nothing reaches back."""
    if v.create_time and v.create_time >= t:
        return 0, True
    if record is not None:
        got, exact = record.value_at(account_id, t, lambda e: (e.get("views") or {}).get(v.id))
        if got is not None:
            return int(got), exact
    before = [p for p in v.history if len(p) >= 2 and p[0] <= t]
    if before and t - before[-1][0] <= NEAR:
        return int(before[-1][1]), True
    return None, False


def followers_at(profile, t: float, record: Record | None, account_id: str) -> int | None:
    if record is not None:
        got, _ = record.value_at(account_id, t, lambda e: e.get("followers"))
        if got is not None:
            return int(round(got))
    before = [p for p in profile.history if len(p) >= 2 and p[0] <= t]
    if before and t - before[-1][0] <= NEAR:
        return int(before[-1][1])
    return None


# ---------------------------------------------------------------- the report

@dataclass
class Post:
    title: str
    url: str = ""
    views: int = 0
    kind: str = "clip"  # clip | creator | own (posted by hand)
    posted_at: float = 0.0
    seconds: float = 0.0


@dataclass
class AccountWeek:
    id: str
    handle: str
    known: bool = False  # its stats have been read
    read_at: float = 0.0
    posts: int = 0
    clips: int = 0
    creator: int = 0
    own: int = 0  # posted by hand, not by ClipMonster
    short: int = 0  # posts a minute or shorter (they can't earn)
    views: int = 0  # all its videos' views in the week
    views_before: int | None = None  # the week before (None: the stats don't reach back that far)
    estimated: bool = False
    earning: int = 0  # of `views`, those on videos over a minute
    followers: int = 0
    gained: int | None = None  # followers gained in the week
    best: Post | None = None
    weakest: Post | None = None
    took_off: Post | None = None  # an older video that got more views this week than the best new post
    clip_avg: int | None = None  # the week's clips, a day old or more: average views
    clip_n: int = 0
    creator_avg: int | None = None
    creator_n: int = 0
    rewards: str = ""
    eligible: bool = False
    waiting: int = 0  # Creator videos waiting for your OK
    queued: int = 0

    @classmethod
    def from_json(cls, data: dict) -> "AccountWeek":
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        for k in ("best", "weakest", "took_off"):
            if isinstance(d.get(k), dict):
                d[k] = Post(**{x: y for x, y in d[k].items() if x in {f.name for f in fields(Post)}})
            else:
                d[k] = None
        return cls(**d)


@dataclass
class Week:
    since: float
    until: float
    accounts: list[AccountWeek] = field(default_factory=list)
    videos_clipped: int = 0  # YouTube videos automation clipped
    clips_made: int = 0
    creator_made: int = 0  # Creator videos made
    failed: int = 0

    @property
    def known(self) -> list[AccountWeek]:
        return [a for a in self.accounts if a.known]

    @property
    def empty(self) -> bool:
        return not self.known and not (self.clips_made or self.creator_made or self.failed)

    def to_json(self) -> dict:
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_json(cls, data: dict) -> "Week":
        return cls(since=float(data.get("since") or 0), until=float(data.get("until") or 0),
                   accounts=[AccountWeek.from_json(a) for a in data.get("accounts") or [] if isinstance(a, dict)],
                   **{k: int(data.get(k) or 0) for k in ("videos_clipped", "clips_made", "creator_made", "failed")})

    def span(self) -> str:
        """'3–9 Nov' (the days the week covers)."""
        a, b = dt.date.fromtimestamp(self.since), dt.date.fromtimestamp(self.until)
        if a.month == b.month:
            return f"{a.day}–{b.day} {b.strftime('%b')}"
        return f"{a.day} {a.strftime('%b')} – {b.day} {b.strftime('%b')}"

    def headline(self) -> str:
        known = self.known
        if not known:
            made = self._made()
            return f"Past week: {made}." if made else "Nothing to report for the past week."
        posts = sum(a.posts for a in known)
        views = sum(a.views for a in known)
        bits = [_plural(posts, "post")]
        text = f"{'about ' if any(a.estimated for a in known) else ''}{fmt(views)} views"
        before = [a.views_before for a in known]
        if all(b is not None for b in before) and sum(before):
            text += f" ({change(views, sum(before))})"
        bits.append(text)
        gained = [a.gained for a in known]
        if all(g is not None for g in gained):
            bits.append(signed(sum(gained), "follower"))
        return "Past week: " + ", ".join(bits) + "."

    def _made(self) -> str:
        bits = []
        if self.clips_made or self.videos_clipped:
            bits.append(f"{_plural(self.clips_made, 'clip')} from {_plural(self.videos_clipped, 'YouTube video')}")
        if self.creator_made:
            bits.append(_plural(self.creator_made, "Creator video"))
        if self.failed:
            bits.append(f"{self.failed} failed")
        return ("automation made " + ", ".join(bits)) if bits else ""

    def sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """[(heading, [(line, link)])]: each account, then automation. A line's link opens the video it names."""
        out = []
        for a in self.accounts:
            out.append((a.handle, account_lines(a, self.until)))
        made = self._made()
        if made:
            out.append(("Automation", [(made[0].upper() + made[1:] + ".", "")]))
        return out

    def lines(self) -> list[str]:
        """The report in plain lines, the headline first."""
        out = [self.headline()]
        for heading, rows in self.sections():
            out.append(heading)
            out += [f"  {text}" for text, _ in rows]
        return out

    def text(self) -> str:
        return f"ClipMonster weekly report, {self.span()}\n\n" + "\n".join(self.lines()) + "\n"


def change(now: float, before: float) -> str:
    """'up 31% on the week before', or '4.5 times the week before' for a big jump."""
    if before and now / before >= 2.5:
        ratio = now / before
        times = f"{ratio:.0f}" if ratio >= 10 else f"{ratio:.1f}".rstrip("0").rstrip(".")
        return f"{times} times the week before"
    pct = round((now - before) / before * 100) if before else 0
    if not pct:
        return "the same as the week before"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):,}% on the week before"


def signed(n: int, word: str) -> str:
    return f"{'+' if n > 0 else '−' if n < 0 else ''}{abs(n):,} {word}{'s' if abs(n) != 1 else ''}"


def quoted(title: str, limit: int = 60) -> str:
    title = " ".join((title or "").split()) or "a video with no caption"
    return f"“{title[:limit - 1].rstrip()}…”" if len(title) > limit else f"“{title}”"


def account_lines(a: AccountWeek, now: float) -> list[tuple[str, str]]:
    if not a.known:
        return [("No stats yet: they're read once the account is signed in.", "")]
    out: list[tuple[str, str]] = []
    if a.posts:
        kinds = [x for x in (_plural(a.clips, "clip") if a.clips else "",
                             _plural(a.creator, "Creator video") if a.creator else "",
                             f"{a.own} posted by hand" if a.own else "") if x]
        out.append((f"{_plural(a.posts, 'post')}: {', '.join(kinds)}", ""))
    else:
        out.append(("Nothing posted this week.", ""))
    views = f"{'About ' if a.estimated else ''}{fmt(a.views)} views"
    if a.views_before is not None and a.views_before:
        views += f", {change(a.views, a.views_before)}"
    if a.views:
        views += f"; {fmt(a.earning)} of them on videos over a minute, the views Creator Rewards pays for"
    out.append((views, ""))
    if a.gained is not None:
        out.append((f"{signed(a.gained, 'follower')}, {a.followers:,} now", ""))
    else:
        out.append((f"{a.followers:,} followers", ""))
    if a.best:
        out.append((f"Best post: {quoted(a.best.title)}, {fmt(a.best.views)} views", a.best.url))
    if a.weakest:
        out.append((f"Weakest: {quoted(a.weakest.title)}, {fmt(a.weakest.views)} views", a.weakest.url))
    if a.took_off:
        out.append((f"An older video took off: {quoted(a.took_off.title)} got {fmt(a.took_off.views)} views this "
                    "week", a.took_off.url))
    if a.clip_avg is not None and a.creator_avg is not None:
        pair = [("Creator videos", a.creator_avg, a.creator_n), ("clips", a.clip_avg, a.clip_n)]
        pair.sort(key=lambda x: -x[1])
        (n1, v1, c1), (n2, v2, c2) = pair
        out.append((f"{n1[0].upper() + n1[1:]} averaged {fmt(v1)} views a post ({c1}), {n2} {fmt(v2)} ({c2})", ""))
    if a.short:
        out.append((f"{_plural(a.short, 'post')} {'was' if a.short == 1 else 'were'} a minute or shorter, so "
                    f"{'it' if a.short == 1 else 'they'} can't earn: Settings → Editing → Creator Rewards length "
                    "makes clips longer", ""))
    if a.rewards:
        out.append((f"Creator Rewards: {a.rewards}", ""))
    if a.waiting:
        out.append((f"{_plural(a.waiting, 'Creator video')} waiting for your OK (Queue tab, or Telegram)", ""))
    if a.queued:
        out.append((f"{a.queued} in the queue to post", ""))
    if a.read_at and now - a.read_at > STALE:
        from .posting.live import ago

        out.append((f"Stats last read {ago(now - a.read_at)} ago, so the latest views aren't in", ""))
    return out


def _title(item, v) -> str:
    return " ".join(((item.headline if item else "") or (v.description if v else "") or "").split())


def account_week(store, a, since: float, now: float, record: Record | None) -> AccountWeek:
    from .posting import tracker

    line = AccountWeek(id=a.id, handle=a.handle)
    p = store.profile(a.id)
    vids = store.videos(a.id)
    line.read_at = last_read(p, vids)
    line.known = bool(line.read_at)
    posted = store.posted(a.id)
    by_video = {i.tiktok_id: i for i in posted if i.tiktok_id}
    mine = [i for i in posted if i.posted_at and since <= i.posted_at <= now]
    line.clips = sum(1 for i in mine if not i.ai_generated)
    line.creator = sum(1 for i in mine if i.ai_generated)
    unlinked = [v for v in vids.values() if v.create_time and since <= v.create_time <= now and v.id not in by_video]
    # A post this app made that the stats haven't matched yet is one of those: not posted by hand.
    line.own = max(0, len(unlinked) - sum(1 for i in mine if not i.tiktok_id))
    line.posts = line.clips + line.creator + line.own
    line.waiting = len(store.waiting_review(a.id))
    line.queued = sum(1 for i in store.queue(a.id) if i.status == "queued" and i.review != "waiting")
    if not line.known:
        return line

    gains: dict[str, int] = {}
    before_total, before_ok = 0, True
    for v in vids.values():
        lo, exact = views_at(v, since, record, a.id)
        if lo is None:
            n, est = tracker.views_in_window(v, now, 7)
            line.estimated = line.estimated or est
        else:
            n = max(0, int(v.views) - lo)
            line.estimated = line.estimated or not exact
        gains[v.id] = n
        line.views += n
        item = by_video.get(v.id)
        if (v.duration or (item.duration if item else 0.0)) > EARNS_SECONDS:
            line.earning += n
        if before_ok:
            older, _ = views_at(v, since - WEEK, record, a.id)
            if lo is None or older is None:
                before_ok = False
            else:
                before_total += max(0, lo - older)
    line.views_before = before_total if before_ok and vids else None

    if p.fetched_at:
        line.followers = int(p.followers)
        base = followers_at(p, since, record, a.id)
        line.gained = None if base is None else line.followers - base

    counted: list[Post] = []  # the week's posts with stats read
    short = 0
    for i in mine:
        v = store.video_for(i)
        seconds = (v.duration if v else 0.0) or i.duration
        short += 0 < seconds <= EARNS_SECONDS
        if v is not None:
            counted.append(Post(_title(i, v), v.url or i.tiktok_url, int(v.views),
                                "creator" if i.ai_generated else "clip", i.posted_at or v.create_time, seconds))
    pending = sum(1 for i in mine if not i.tiktok_id)  # posted from here, not matched to its TikTok video yet
    for v in unlinked:  # posted by hand (or, while some are pending, maybe one of those: kind unknown)
        short += not pending and 0 < v.duration <= EARNS_SECONDS
        counted.append(Post(_title(None, v), v.url, int(v.views), "" if pending else "own", v.create_time,
                            v.duration))
    line.short = short
    if counted:
        line.best = max(counted, key=lambda x: (x.views, x.posted_at))
    settled = [x for x in counted if now - x.posted_at >= SETTLED]
    if len(settled) >= 3:
        low = min(settled, key=lambda x: (x.views, -x.posted_at))
        if line.best is None or low.views < line.best.views:
            line.weakest = low
    old = [(gains.get(v.id, 0), v) for v in vids.values() if v.create_time and v.create_time < since]
    if old:
        g, v = max(old, key=lambda x: x[0])
        if g >= TOOK_OFF_MIN and g > (line.best.views if line.best else 0):
            item = by_video.get(v.id)
            line.took_off = Post(_title(item, v), v.url, g, ("creator" if item.ai_generated else "clip") if item
                                 else "own", v.create_time, v.duration)
    for kind in ("clip", "creator"):
        xs = [x.views for x in settled if x.kind == kind]
        if xs:
            setattr(line, f"{kind}_avg", round(sum(xs) / len(xs)))
            setattr(line, f"{kind}_n", len(xs))

    pr = tracker.progress(p, vids, now)
    line.eligible = pr.eligible
    line.rewards = ("meets both goals: apply in TikTok, Profile → menu → TikTok Studio → Creator Rewards Program"
                    if pr.eligible else
                    f"{pr.followers.value:,} of {pr.followers.target:,} followers, "
                    f"{'about ' if pr.views_estimated else ''}{fmt(pr.views.value)} of {fmt(pr.views.target)} views "
                    "in 30 days")
    return line


def build(store, accounts, db=None, now: float | None = None, record: Record | None = None) -> Week:
    """The report for the 7 days to `now`. `db` (jobs.db) adds what automation made; without a `record`
    the saved one is read."""
    now = time.time() if now is None else now
    since = now - WEEK
    record = record if record is not None else Record()
    w = Week(since=since, until=now)
    for a in accounts:
        w.accounts.append(account_week(store, a, since, now, record))
    if db is not None:
        for j in db.jobs_since(since):
            if j.status == "done":
                if j.is_creation:
                    w.creator_made += 1
                else:
                    w.videos_clipped += 1
                    w.clips_made += j.clips
            elif j.status == "failed":
                w.failed += 1
    return w


# ---------------------------------------------------------------- saved reports

def folder() -> Path:
    return app_dir() / "reports"


def save(w: Week, where: Path | None = None) -> Path:
    """reports/week-<date>.txt (and .json, which the app shows again). Returns the .txt."""
    where = where or folder()
    where.mkdir(parents=True, exist_ok=True)
    stem = "week-" + dt.date.fromtimestamp(w.until).isoformat()
    (where / f"{stem}.json").write_text(json.dumps(w.to_json(), indent=1, ensure_ascii=False), encoding="utf-8")
    txt = where / f"{stem}.txt"
    txt.write_text(w.text(), encoding="utf-8")
    for old in sorted(where.glob("week-*.json"))[:-REPORTS_KEPT]:
        old.unlink(missing_ok=True)
        old.with_suffix(".txt").unlink(missing_ok=True)
    return txt


def saved(where: Path | None = None) -> list[Path]:
    """The saved reports (.json), newest first."""
    where = where or folder()
    return sorted(where.glob("week-*.json"), reverse=True) if where.is_dir() else []


def load(path: Path) -> Week | None:
    try:
        return Week.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------- sending it

def week_key(now: float) -> str:
    y, wk, _ = dt.date.fromtimestamp(now).isocalendar()
    return f"{y}-W{wk:02d}"


def send_time(now: float) -> bool:
    """Monday from 9 am, or later that week."""
    t = time.localtime(now)
    return t.tm_wday > SEND_WEEKDAY or (t.tm_wday == SEND_WEEKDAY and t.tm_hour >= SEND_HOUR)


def toast(w: Week) -> bool:
    from . import notify

    best = max((a.best for a in w.known if a.best), key=lambda p: p.views, default=None)
    text = w.headline()
    if best:
        text += f" Best: {quoted(best.title, 50)}, {fmt(best.views)} views."
    return notify.toast("ClipMonster: your week", text, button="Open the report", link=notify.report_link())


def _posting():
    from .posting.accounts import Accounts
    from .posting.store import Store

    return Store(), Accounts.load()


def tick(s, db, now: float | None = None, log: Log | None = None, *, store=None, accounts=None,
         record: Record | None = None, notify_fn: Callable[[Week], bool] | None = None) -> Week | None:
    """Automation's part, every few minutes: today's reading for the record, and on Monday morning the
    report. Returns the report when it was made this time."""
    now = time.time() if now is None else now
    log = log or (lambda m: None)
    record = record if record is not None else Record()
    key = week_key(now)
    last = db.meta("weekly_sent")
    if not last:  # the first report is the coming Monday's, not today's few days
        db.set_meta("weekly_sent", key)
    report_due = bool(last) and last != key and send_time(now)
    if not (record.due(now) or report_due):
        return None
    if store is None or accounts is None:
        store, accounts = _posting()
    ids = accounts.ids() if hasattr(accounts, "ids") else [a.id for a in accounts]
    if record.take(store, ids, now):
        record.save()
    if not report_due:
        return None
    db.set_meta("weekly_sent", key)
    if not getattr(s, "weekly_report", True):
        return None
    w = build(store, accounts, db, now, record)
    if w.empty:
        return None
    path = save(w)
    log(f"Weekly report: {w.headline()} (saved as {path})")
    try:
        (notify_fn or toast)(w)
    except Exception as exc:  # noqa: BLE001 - the report is saved either way
        log(f"⚠ Couldn't show the weekly report's notification: {exc}")
    return w
