"""Posting's work: scoring a clip, the AI's hashtags, posting, reading an account and picking what's due.
Free of any GUI code, so the background worker runs it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from . import schedule, scoring
from .accounts import Account, Accounts
from .config import Settings, thumbs_dir
from .media import analyze, probe, thumbnail
from .store import Store, VideoItem

Log = Callable[[str], None]


def ai_part(ai: dict) -> float | None:
    if not ai or "appeal" not in ai:
        return None
    return round(ai["appeal"] * 0.75 + ai.get("hook", ai["appeal"]) * 0.25, 1)


def analyze_item(store: Store, item_id: str, s: Settings) -> VideoItem:
    item = store.get(item_id)
    if item is None:
        raise KeyError(item_id)
    path = Path(item.path)
    try:
        info = probe(path)
        a = analyze(path, info)
        sc = scoring.score(a, s, item.source_score, ai_part(item.ai))
        thumb = thumbs_dir() / f"{item.id}.jpg"
        try:
            thumbnail(path, thumb, min(1.0, info.duration / 3))
        except Exception:
            thumb = None
    except Exception as exc:
        with store.lock:
            item.analyze_error = str(exc) or exc.__class__.__name__
            item.analyzed_at = time.time()
        raise
    with store.lock:
        item.info = a.to_json()["info"]
        item.analysis = a.to_json()
        item.score = sc.to_json()
        item.analyze_error = ""
        item.analyzed_at = time.time()
        if thumb:
            item.thumb = str(thumb)
    return item


def cover_item(store: Store, item_id: str, s: Settings, log: Log | None = None, **kw) -> VideoItem:
    """Choose and save the clip's TikTok cover (cover.py); the Queue tab's thumbnail becomes that frame. A clip
    that can't have one gets cover_why "none: <why>" and posts with TikTok's own pick."""
    from . import cover

    item = store.get(item_id)
    if item is None:
        raise KeyError(item_id)
    try:
        made = cover.make(item, s.cover, **kw)
    except Exception as exc:  # noqa: BLE001 - an unreadable frame: TikTok picks
        with store.lock:
            item.cover, item.cover_at = "", None
            item.cover_why = f"none: {exc}"[:200]
        if log:
            log(f"⚠ No cover for “{item.headline}” ({exc}): TikTok picks one.")
        return item
    if made is None:
        return item
    pick, picture = made
    with store.lock:
        item.cover, item.cover_at, item.cover_why = str(picture), pick.at, pick.why
    thumb = thumbs_dir() / f"{item.id}.jpg"
    try:
        thumbnail(Path(item.path), thumb, pick.at)
        with store.lock:
            item.thumb = str(thumb)
    except Exception:  # noqa: BLE001 - the old thumbnail stays
        pass
    if log:
        log(f"Cover for “{item.headline}”: {cover.describe(item)}.")
    return item


def needs_cover(item: VideoItem, s: Settings) -> bool:
    """The clip should have a cover made before it posts (none made or tried yet, or its picture is gone)."""
    if getattr(s, "cover", "auto") == "off" or item.cover_why.startswith("none:"):
        return False
    return not (item.cover and Path(item.cover).is_file())


def proven_tags(store: Store, account_id: str, limit: int = 8, min_posts: int = 3) -> list[tuple[str, int]]:
    """The account's own hashtags whose posts got the most views (median per tag), best first.

    Empty until at least `min_posts` of its posts have view counts, so a single lucky post
    doesn't steer every suggestion.
    """
    import statistics

    from .tiktok import normalize_hashtags

    views: dict[str, list[int]] = {}
    names: dict[str, str] = {}
    counted = 0
    for it in store.posted(account_id):
        v = store.video_for(it)
        if v is None:
            continue
        counted += 1
        for tag in normalize_hashtags(it.hashtags).split():
            key = tag.lstrip("#").lower()
            names.setdefault(key, tag.lstrip("#"))
            views.setdefault(key, []).append(v.views)
    if counted < min_posts:
        return []
    ranked = sorted(views.items(), key=lambda kv: statistics.median(kv[1]), reverse=True)
    return [(names[k], int(statistics.median(v))) for k, v in ranked[:limit]]


def clip_context(item: VideoItem) -> dict:
    """ClipMonster's facts about the clip (the source video's title, channel, why it was picked)."""
    from .clipsjson import read_folder_index

    path = Path(item.path)
    try:
        return read_folder_index(path.parent).get(path.name) or {}
    except OSError:
        return {}


def ai_hashtags(store: Store, item_id: str, s: Settings, account: Account | None = None, log: Log | None = None,
                only_if_empty: bool = False) -> VideoItem:
    """The AI writes the clip's hashtags (text only). With `only_if_empty`, hashtags you or an AI
    rating already gave it are kept (used for clips automation brings in)."""
    from .ai import suggest_hashtags

    item = store.get(item_id)
    if item is None:
        raise KeyError(item_id)
    if only_if_empty and item.hashtags.strip():
        return item
    a = account
    tags = suggest_hashtags(a.apply(s) if a else s, item, audience=a.audience if a else s.ai_audience,
                            always=a.default_hashtags if a else s.default_hashtags,
                            proven=proven_tags(store, a.id) if a else None, context=clip_context(item))
    if not tags:
        raise RuntimeError("The AI didn't come up with hashtags that fit; add them yourself.")
    with store.lock:
        if only_if_empty and item.hashtags.strip():  # typed in while the AI was thinking
            return item
        item.hashtags = " ".join("#" + t for t in tags)
    if log:
        log(f"AI hashtags for “{item.name}”: {item.hashtags}")
    return item


def recombine(item: VideoItem, s: Settings) -> None:
    """Recompute the total after weights change or an AI rating arrives (no re-analysis)."""
    if not item.score:
        return
    parts = dict(item.score.get("parts", {}))
    ai = ai_part(item.ai)
    if ai is not None:
        parts["ai"] = ai
        item.score.setdefault("notes", {})["ai"] = "The AI's prediction of viewer appeal"
    if item.source_score is not None:
        parts["source"] = round(max(0.0, min(100.0, item.source_score)), 1)
    item.score["parts"] = parts
    item.score["total"] = scoring.combine(parts, s)


def post_item(store: Store, item_id: str, s: Settings, account: Account, log: Log, cancelled: Callable[[], bool],
              progress: Callable[[str], None] | None = None) -> VideoItem:
    """Post a queued video to the account whose queue it's in. `s` is the app-wide settings."""
    from . import tiktok

    item = store.get(item_id)
    if item is None:
        raise KeyError(item_id)
    if item.account != account.id:
        raise RuntimeError(f"“{item.name}” is queued for a different account.")
    if needs_cover(item, s) and item.exists():  # not made ahead (posted straight away): a few seconds now
        cover_item(store, item.id, s, log)
    with store.lock:
        item.status = "uploading"
        item.attempts += 1
        item.last_error = ""
        item.upload_started_at = time.time()
    store.save()
    try:
        result = tiktok.post(item, account.apply(s), account, log, cancelled, progress)
    except Exception as exc:
        with store.lock:
            item.status = "failed"
            item.last_error = str(exc) or exc.__class__.__name__
        store.save()
        raise
    with store.lock:
        item.status = "posted"
        item.posted_at = time.time()
        item.queue_pos = None
        item.pinned_at = None
        item.planned_at = None
        item.publish_id = result.get("publish_id", "")
        item.posted_via = result.get("via", "")
        item.cover_set = result.get("cover_set")
        if result.get("tiktok_id"):
            item.tiktok_id = result["tiktok_id"]
            if account.username:
                item.tiktok_url = f"https://www.tiktok.com/@{account.username}/video/{item.tiktok_id}"
        store._renumber()
    store.save()
    return item


def refresh_account(store: Store, s: Settings, account: Account, log: Log, cancelled: Callable[[], bool]) -> None:
    from . import tiktok

    profile, videos = tiktok.refresh_account(account.apply(s), account, log, cancelled)
    if profile.username and not account.username:
        account.username = profile.username
    store.update_profile(account.id, profile)
    store.update_tiktok_videos(account.id, videos)
    store.save()
    log(f"{account.handle} refreshed: {profile.followers:,} followers, {len(videos)} videos read.")
    from . import results

    try:
        results.share(store)
    except OSError as exc:
        log(f"Couldn't save how the clips did for picking: {exc}")


# --- scheduling across accounts ------------------------------------------------------


def replan(store: Store, accounts: Accounts | list[Account], s: Settings, now: float | None = None) -> None:
    """Work out every queued clip's post time, from its own account's schedule and last post."""
    now = time.time() if now is None else now
    for a in list(accounts):
        q = store.queue(a.id)
        go = [i for i in q if i.status == "queued" and i.review != "waiting"]  # held ones wait for your OK
        planned = schedule.plan(go, a.apply(s), now, store.last_post_time(a.id))
        with store.lock:
            for it in q:
                it.planned_at = planned.get(it.id)


def can_autopost(account: Account, s: Settings) -> bool:
    """Autopilot is on for the account and it has a login to post with."""
    return account.autopilot and account.logged_in


def autopilot_pick(store: Store, accounts: Accounts | list[Account], s: Settings,
                   now: float | None = None, log: Log | None = None) -> tuple[Account, VideoItem] | None:
    """The video autopilot should post now: the most overdue one among accounts with autopilot on.

    Due videos whose file has gone missing are marked failed (so they don't hold up the rest of
    that account's queue) instead of being returned.
    """
    now = time.time() if now is None else now
    replan(store, accounts, s, now)
    best: tuple[Account, VideoItem] | None = None
    for a in accounts:
        if not can_autopost(a, s):
            continue
        while (item := schedule.due(store.queue(a.id), now)) is not None and not item.exists():
            with store.lock:
                item.status, item.last_error = "failed", f"The video file is missing: {item.path}"
            if log:
                log(f"✖ {a.handle}: skipped “{item.name}”, its file is missing.")
        if item and (best is None or (item.planned_at or 0) < (best[1].planned_at or 0)):
            best = (a, item)
    return best


UPLOAD_CHECK_EVERY = 20 * 3600  # the quick upload-page check runs about once a day per autopilot account
UPLOAD_CHECK_QUIET = 30 * 60  # and never this close before a post is due


def upload_check_due(store: Store, accounts: Accounts | list[Account], s: Settings,
                     now: float | None = None) -> Account | None:
    """An autopilot account whose TikTok upload page is due its daily check."""
    now = time.time() if now is None else now
    for a in accounts:
        if not can_autopost(a, s) or now - a.upload_check_at < UPLOAD_CHECK_EVERY:
            continue
        nxt = next_post(store, a.id)
        if nxt and nxt.planned_at and nxt.planned_at - now < UPLOAD_CHECK_QUIET:
            continue
        return a
    return None


def next_post(store: Store, account_id: str | None = None) -> VideoItem | None:
    """The next queued video by planned time (for one account, or any)."""
    q = [i for i in store.queue(account_id) if i.status == "queued" and i.planned_at]
    return min(q, key=lambda i: i.planned_at) if q else None
