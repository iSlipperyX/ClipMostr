"""Creator Rewards progress: how close an account is to TikTok's Creator Rewards Program.

TikTok pays creators in the program for qualified views on original videos longer than one minute. To
join, an account needs (TikTok's published rules; age, region and good standing are checked by TikTok):

* at least 10,000 followers, and
* at least 100,000 video views in the last 30 days.

This works out where an account stands from the stats ClipMonster already reads (Performance page):
followers from the profile, views in the last 30 days from each video's view history, and how many of
the last 30 days' posts are over a minute (the only ones that earn). From the recent trend it estimates
when each goal will be reached. Nothing here posts or changes anything; it only reads the store.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import Profile, TikTokVideo

FOLLOWERS_GOAL = 10_000
VIEWS_GOAL = 100_000
WINDOW_DAYS = 30
QUALIFYING_SECONDS = 60.0  # strictly longer than this


@dataclass
class Goal:
    label: str
    value: int
    target: int
    days_left: float | None = None  # estimated days until reached at the recent rate; None: not growing

    @property
    def done(self) -> bool:
        return self.value >= self.target

    @property
    def fraction(self) -> float:
        return max(0.0, min(1.0, self.value / self.target)) if self.target else 1.0


@dataclass
class Progress:
    followers: Goal
    views: Goal
    known: bool  # False: no stats read yet, so the numbers are zeros, not the account's real state
    views_estimated: bool = False  # True: some older videos' 30-day views were estimated
    posts: int = 0  # posted in the last 30 days
    qualifying: int = 0  # of them, over a minute
    notes: list[str] = field(default_factory=list)

    @property
    def eligible(self) -> bool:
        return self.known and self.followers.done and self.views.done


def views_in_window(video: TikTokVideo, now: float, days: int = WINDOW_DAYS) -> tuple[int, bool]:
    """Views `video` got in the last `days` days, and whether that's an estimate.

    A video posted inside the window counts all its views. An older one counts what it gained since the
    last history point at or before the window's start; when its history doesn't reach back that far,
    its gain over the history it has is scaled up to the window (an estimate)."""
    start = now - days * 86400
    if video.create_time and video.create_time >= start:
        return int(video.views), False
    h = [p for p in video.history if len(p) >= 2]
    if not h:
        return 0, bool(video.views)
    before = [p for p in h if p[0] <= start]
    if before:
        return max(0, int(video.views - before[-1][1])), False
    first = h[0]
    span = max(1.0, now - first[0])
    gained = max(0.0, video.views - first[1])
    return int(gained * min(days * 86400 / span, 30.0)) if span >= 3600 else int(gained), True


def _days_to(value: float, target: float, per_day: float) -> float | None:
    if value >= target:
        return 0.0
    if per_day <= 0:
        return None
    return (target - value) / per_day


def follower_rate(profile: Profile, now: float, days: int = 14) -> float:
    """New followers a day over the last `days` days of profile history (0 without enough of it)."""
    h = [p for p in profile.history if len(p) >= 2 and p[0] >= now - days * 86400]
    if len(h) < 2 or h[-1][0] - h[0][0] < 12 * 3600:
        return 0.0
    return max(0.0, (h[-1][1] - h[0][1]) / ((h[-1][0] - h[0][0]) / 86400))


def progress(profile: Profile, videos: dict[str, TikTokVideo] | list[TikTokVideo], now: float) -> Progress:
    vids = list(videos.values()) if isinstance(videos, dict) else list(videos)
    known = bool(profile.fetched_at)
    followers = Goal("Followers", int(profile.followers), FOLLOWERS_GOAL)
    followers.days_left = _days_to(followers.value, FOLLOWERS_GOAL, follower_rate(profile, now))

    total, estimated = 0, False
    for v in vids:
        n, est = views_in_window(v, now)
        total += n
        estimated = estimated or est
    views = Goal("Views (30 days)", total, VIEWS_GOAL)
    # The 30-day total keeps its pace while posting continues; take the last week's pace, else the month's.
    week = sum(views_in_window(v, now, 7)[0] for v in vids)
    per_day = week / 7 if week else total / WINDOW_DAYS
    # A rolling total stops at the daily pace times 30: it can only reach the goal if the pace will.
    views.days_left = (0.0 if views.done else
                       (_days_to(total, VIEWS_GOAL, per_day) if per_day * WINDOW_DAYS >= VIEWS_GOAL else None))

    start = now - WINDOW_DAYS * 86400
    recent = [v for v in vids if v.create_time and v.create_time >= start]
    qualifying = [v for v in recent if v.duration > QUALIFYING_SECONDS]
    p = Progress(followers, views, known, estimated, len(recent), len(qualifying))
    p.notes = _notes(p, per_day)
    return p


def _notes(p: Progress, views_per_day: float) -> list[str]:
    if not p.known:
        return ["No stats yet: they're read every few minutes once the account is logged in."]
    out = []
    if p.eligible:
        out.append("This account meets the follower and view goals. Apply in TikTok: Profile → menu → "
                   "TikTok Studio → Creator Rewards Program.")
    else:
        for g in (p.followers, p.views):
            if g.done:
                continue
            if g.days_left is None:
                out.append(f"{g.label}: {g.target - g.value:,} to go, not growing fast enough yet to estimate.")
            else:
                out.append(f"{g.label}: {g.target - g.value:,} to go, about {_days(g.days_left)} at the recent pace.")
        if not p.views.done and views_per_day and views_per_day * WINDOW_DAYS < VIEWS_GOAL:
            need = VIEWS_GOAL / WINDOW_DAYS
            out.append(f"The 30-day total needs about {need:,.0f} views a day; it's getting {views_per_day:,.0f}.")
    if p.posts:
        short = p.posts - p.qualifying
        if short:
            what = (f"{short} of the last {p.posts} posts are" if p.posts > 1 else "The last post is")
            out.append(f"{what} a minute or shorter and won't earn: turn on Settings → Editing → "
                       "Creator Rewards length.")
        else:
            out.append("The post from the last 30 days is over a minute." if p.posts == 1 else
                       f"All {p.posts} posts from the last 30 days are over a minute.")
    return out


def _days(d: float) -> str:
    if d < 1:
        return "a day"
    if d < 14:
        return f"{round(d)} day{'s' if round(d) != 1 else ''}"
    if d < 60:
        return f"{round(d / 7)} weeks"
    return f"{round(d / 30)} months"
