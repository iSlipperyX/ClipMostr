"""Automation's finished clips go straight into the posting queue.

After automation exports a video's clips (and every few minutes, in case the worker stopped in
between), the clips listed in the Automation folder's clips.json files that the quality check passed
come into the posting library, keeping the hook, the category, the part series and where they came from.
Each goes to the queue of the account its category posts to (Category.account_id). With one account,
everything goes there. A clip whose category has no account stays in the library, and the Queue tab
says so.
"""

from __future__ import annotations

import time
from pathlib import Path

from .accounts import Account, Accounts
from .store import Store, VideoItem


def roots(main) -> list[Path]:
    """The folders automation's clips land in (`main` is ClipMonster's Settings)."""
    return [main.auto_output_path]


def take(store: Store, accounts: Accounts | list[Account], main, now: float | None = None,
         folders: list[Path] | None = None) -> tuple[list[VideoItem], dict[str, int]]:
    """Bring in new finished clips and queue them. Returns (new items, how many each account got)."""
    now = time.time() if now is None else now
    found = [f for f in (folders or roots(main)) if f.is_dir()]
    if not found:
        return [], {}
    items = store.add_paths(found, watched=True, now=now)
    return items, assign(store, accounts, items, main)


def account_for(accounts: Accounts | list[Account], item: VideoItem, main=None) -> Account | None:
    """The account a clip posts to: the one its category names (at the time it was made, or now), else the
    only account there is."""
    accounts = list(accounts)
    by_id = {a.id: a for a in accounts}
    if item.for_account in by_id:
        return by_id[item.for_account]
    if main is not None and item.category_id:
        from .. import categories

        category = categories.find(main, item.category_id)
        if category is not None and category.account_id in by_id:
            return by_id[category.account_id]
        if category is not None and category.account and not category.account_id:  # a category from before 3.0: "@name"
            name = category.account.strip().lstrip("@").lower()
            match = next((a for a in accounts if a.username and a.username.lower() == name), None)
            if match is not None:
                return match
    return accounts[0] if len(accounts) == 1 else None


def series_order(items: list[VideoItem]) -> list[VideoItem]:
    """`items` in the same order, except that the parts of a series come together, part 1 first
    (where the first of them was), so they're queued to post one after another."""
    out: list[VideoItem] = []
    done: set[str] = set()
    for it in items:
        if it.id in done:
            continue
        group = sorted((i for i in items if i.series == it.series), key=lambda i: i.part or 0) if it.series else [it]
        out += group
        done.update(i.id for i in group)
    return out


def assign(store: Store, accounts: Accounts | list[Account], items: list[VideoItem], main=None) -> dict[str, int]:
    """Queue new clips for their accounts. The parts of a series all go to the account part 1 went to,
    also when they come in later. Returns how many each account got."""
    accounts = list(accounts)
    got: dict[str, int] = {}
    if not items or not accounts:
        return got
    ids = {a.id for a in accounts}
    new = {i.id for i in items}
    with store.lock:
        series_to: dict[str, str] = {i.series: i.account for i in store.items.values()
                                     if i.series and i.account in ids and i.id not in new}
    for it in series_order(items):
        owner = None
        if it.series and series_to.get(it.series) in ids:
            owner = next(a for a in accounts if a.id == series_to[it.series])
        owner = owner or account_for(accounts, it, main)
        if owner is None:
            continue
        if it.series:
            series_to.setdefault(it.series, owner.id)
        store.enqueue([it.id], owner.id)
        got[owner.id] = got.get(owner.id, 0) + 1
    return got


def unassigned(store: Store) -> list[VideoItem]:
    """Automation's clips still waiting for an account (their category has none): the Queue tab says so."""
    with store.lock:
        return [i for i in store.items.values()
                if i.imported_by == "auto" and i.status == "new" and not i.account and i.exists()]
