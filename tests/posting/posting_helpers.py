"""Shared bits for the posting tests: a ClipMonster Automation folder, two accounts, fixed times."""

import datetime as dt
import json
import time

from ytclip.posting.accounts import Accounts


def ts(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi).timestamp()


NOW = ts(2026, 10, 27, 10)


def clipmonster_folder(tmp_path, name="Pod/Talk [abc]", permission="Creative Commons licence: credit the creator",
                       license="", automation: dict | None = None):
    """A video's clip folder as automation writes it: two clips that passed the quality check, one rejected."""
    folder = tmp_path / "Automation" / name
    (folder / "_rejected").mkdir(parents=True)
    for f in ("01 - good.mp4", "02 - fine.mp4", "_rejected/03 - dud.mp4"):
        (folder / f).write_bytes(b"x")
    auto = dict(automation or {})
    if permission:
        auto["permission"] = permission
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "abc", "title": "Big Talk", "channel": "Pod People", "license": license, "duration": 3600},
        "source": "https://youtu.be/abc",
        **({"automation": auto} if auto else {}),
        "clips": [
            {"file": "01 - good.mp4", "title": "good", "score": 81, "reason": "laughs", "hook": "He quit on air",
             "rating": 8, "watch": "https://youtu.be/abc?t=60", "start": 60, "end": 125, "qc": {"ok": True}},
            {"file": "02 - fine.mp4", "title": "fine", "score": 70, "start": 300, "end": 370,
             "qc": {"ok": True, "warnings": ["Ends mid-sentence"]}},
            {"file": "_rejected/03 - dud.mp4", "title": "dud", "score": 60,
             "qc": {"ok": False, "problems": ["Silent"]}},
        ]}), encoding="utf-8")
    return folder


def two_accounts(path=None):
    accs = Accounts.load(path)
    a = accs.add("")
    a.username = "cats"
    b = accs.add("Second", copy_schedule_from=a)
    b.username = "dogs"
    return accs, a, b


def mark_posted(store, item_id, now=None):
    """What a post going up does to its entry (jobs.post_item without TikTok)."""
    now = time.time() if now is None else now
    it = store.get(item_id)
    with store.lock:
        it.status = "posted"
        it.posted_at = now
        it.queue_pos = None
        it.planned_at = None
        it.posted_via = "browser"
        store._renumber()
    return it
