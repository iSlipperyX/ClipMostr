"""One research scan, shared by the Research tab and `ytclip research`:

charts per region + channel list + Creative Commons searches → merge → history (momentum)
→ quick score → most-replayed check on the best candidates → final score, best first.
"""

from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable

from . import clipability, niches
from .download import is_blocked
from .policy import language_codes
from .niches import Niche
from .settings import Settings, app_dir
from .sources import ChannelRef, Gathered, gather
from .trending import (CATEGORIES, ChartUnavailable, DeepInfo, History, ResearchError, TrendingVideo, deep_check,
                       fetch_chart, parse_regions)

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]


class ScanCancelled(Exception):
    pass


@dataclass
class ScanOptions:
    api_key: str
    regions: list[str]
    category: str = ""
    per_region: int = 50
    min_minutes: float = 0.0
    deep_count: int = 10
    cookies: str | None = None
    channels: list[ChannelRef] = field(default_factory=list)  # the channel list (Settings → Picking)
    searches: list[str] = field(default_factory=list)  # Creative Commons searches
    unreadable: list = field(default_factory=list)  # channel list lines that aren't a channel, or (where, line)
    language: str = ""  # Creative Commons searches favour this spoken language (the first of Picking's)
    deep_workers: int = 4  # most-replayed checks at once
    deep_pause: float = 0.0  # seconds between them (automation spaces them out)
    niches: list[Niche] = field(default_factory=list)  # every niche (switched off too), to sort the videos into

    @classmethod
    def from_settings(cls, s: Settings) -> "ScanOptions":
        channels, searches, bad = niches.scan_sources(s)
        return cls(api_key=youtube_key(s), regions=parse_regions(s.research_regions) or ["US"],
                   category=s.research_category, per_region=s.research_per_region,
                   min_minutes=s.research_min_minutes, deep_count=s.research_deep_count,
                   cookies=s.cookies_browser or None, channels=channels,
                   searches=searches, unreadable=bad,
                   language=next(iter(language_codes(s.pick_languages)), ""), niches=niches.load(s))

    @property
    def quota(self) -> int:
        """YouTube API units the charts use each scan (deep checks go through yt-dlp and cost none).
        The channel list and searches add their own, only when due; see sources.py."""
        return len(self.regions) * math.ceil(self.per_region / 50)


@dataclass
class ScanResult:
    videos: list[TrendingVideo]
    time: float
    regions: list[str]
    category: str = ""
    problems: list[str] = field(default_factory=list)
    quota: int = 0
    first: bool = False  # nothing to compare with yet, so nothing counts as "new"
    notes: dict[str, str] = field(default_factory=dict)  # channel id -> its note on the channel list
    blocked: str = ""  # YouTube asked this PC to sign in during the most-replayed checks

    @property
    def new(self) -> list[TrendingVideo]:
        return [v for v in self.videos if v.new]


def youtube_key(s: Settings) -> str:
    return (s.youtube_api_key or os.environ.get("YOUTUBE_API_KEY", "")).strip()


def history_path():
    return app_dir() / "research.json"


def scan(opts: ScanOptions, history: History, log: Log | None = None, progress: Progress | None = None,
         cancelled: Callable[[], bool] | None = None,
         fetch: Callable[..., list[TrendingVideo]] = fetch_chart,
         deep: Callable[..., DeepInfo] = deep_check, now: float | None = None,
         sources: Callable[..., Gathered] = gather) -> ScanResult:
    log = log or (lambda m: None)
    cancelled = cancelled or (lambda: False)
    now = now or time.time()

    def step(msg: str, frac: float | None) -> None:
        if cancelled():
            raise ScanCancelled()
        if progress:
            progress(msg, frac)

    if not opts.regions:
        raise ResearchError("Pick at least one region to check.")
    merged: dict[str, TrendingVideo] = {}
    problems: list[str] = []
    category = CATEGORIES.get(opts.category, "")
    quota = 0
    for i, region in enumerate(opts.regions):
        step(f"Checking what's popular in {region}", i / (len(opts.regions) + 1))
        try:
            chart = fetch(opts.api_key, region, opts.category, opts.per_region)
        except ChartUnavailable:
            problems.append(f"{region} has no {category} chart." if opts.category
                            else f"{region} has no chart (is it a YouTube region?).")
            quota += 1
            continue
        except ResearchError as exc:
            if not merged:  # a bad key or used-up quota fails every region the same way
                raise
            problems.append(f"{region}: {exc}")
            continue
        quota += math.ceil(max(1, len(chart)) / 50)
        for v in chart:
            if v.id in merged:
                merged[v.id].regions.update(v.regions)
                merged[v.id].views = max(merged[v.id].views, v.views)
            else:
                merged[v.id] = v

    extra = Gathered()
    if opts.channels or opts.searches:
        step("Checking your channel list" if opts.channels else "Searching Creative Commons videos",
             len(opts.regions) / (len(opts.regions) + 1))
        extra = sources(opts.api_key, opts.channels, opts.searches, history, now, log, language=opts.language)
        quota += extra.quota
        problems += extra.problems
    problems += [f"{x[0] if isinstance(x, tuple) else 'Channel list'}: \"{x[1] if isinstance(x, tuple) else x}\" "
                 "isn't a channel link, @handle or id." for x in opts.unreadable]
    for v in extra.videos:
        if v.id in merged:
            merged[v.id].origins = sorted(set(merged[v.id].origins) | set(v.origins))
        else:
            merged[v.id] = v
    for v in merged.values():
        if v.channel_id in extra.watch_ids and "watchlist" not in v.origins:
            v.origins = sorted(v.origins + ["watchlist"])

    videos = [v for v in merged.values()
              if v.live != "upcoming" and (not opts.min_minutes or v.duration >= opts.min_minutes * 60
                                           or v.live == "live")]
    if merged and len(videos) < len(merged):
        log(f"Skipped {len(merged) - len(videos)} video(s) shorter than {opts.min_minutes:g} min or not yet live.")
    niches.assign(videos, opts.niches, history)
    first = not history.videos
    for v in videos:
        history.record(v, now)
        v.new = v.new and not first
        v.deep = history.cached_deep(v.id, now)
    velocities = [v.velocity for v in videos]
    for v in videos:
        clipability.score(v, velocities, now)

    # Most-replayed checks cost a few seconds each, so only the best candidates get one.
    blocked = ""
    todo = [v for v in sorted(videos, key=lambda v: v.score, reverse=True)[:max(0, opts.deep_count)]
            if v.deep is None and v.live == "none" and v.duration > 60]
    if todo:
        step(f"Checking most-replayed moments (0/{len(todo)})", len(opts.regions) / (len(opts.regions) + 1))
        pool = ThreadPoolExecutor(max_workers=max(1, opts.deep_workers))
        stop = threading.Event()

        def check(v: TrendingVideo) -> DeepInfo | None:
            if stop.is_set() or (opts.deep_pause and stop.wait(opts.deep_pause)):
                return None  # skipped: YouTube is pushing back, or the scan was cancelled
            return deep(v.url, opts.cookies, log)

        jobs = {pool.submit(check, v): v for v in todo}
        finished = False
        done = 0
        try:
            pending = set(jobs)
            while pending:
                # Wake up regularly so Cancel works even while every check is still running.
                ready, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for job in ready:
                    v = jobs[job]
                    try:
                        info = job.result()
                    except Exception as exc:  # a failed check only means less to go on
                        info = DeepInfo(checked=now, error=str(exc) or exc.__class__.__name__)
                    done += 1
                    if info is None:
                        continue
                    if is_blocked(info.error):
                        # More checks would only make it worse: skip the rest, and don't remember
                        # this as the video's result.
                        if not blocked:
                            blocked = info.error
                            stop.set()
                            log("YouTube asked this PC to sign in, so the remaining most-replayed checks "
                                "are skipped.")
                        continue
                    v.deep = info
                    history.store_deep(v.id, v.deep)
                step(f"Checking most-replayed moments ({done}/{len(todo)})",
                     (len(opts.regions) + done / len(todo)) / (len(opts.regions) + 1))
            finished = True
        finally:
            # On cancel, don't sit waiting for checks still in flight; their results are dropped.
            stop.set()
            pool.shutdown(wait=finished, cancel_futures=True)
        for v in videos:
            clipability.score(v, velocities, now)

    videos.sort(key=lambda v: (v.score, v.velocity), reverse=True)
    result = ScanResult(videos, now, list(opts.regions), opts.category, problems, quota, first, extra.notes,
                        blocked=blocked)
    history.remember(videos, {"regions": result.regions, "category": opts.category, "problems": problems,
                              "notes": extra.notes}, now)
    history.save()
    return result


def last_result(history: History) -> ScanResult | None:
    """The previous scan, so the Research tab opens with results instead of an empty table."""
    if not history.last_scan.get("videos"):
        return None
    ls = history.last_scan
    videos = history.last_videos()
    for v in videos:
        v.new = False
    return ScanResult(videos, ls.get("time", 0.0), ls.get("regions", []), ls.get("category", ""),
                      ls.get("problems", []), notes=ls.get("notes") or {})
