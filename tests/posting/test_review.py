"""The review hold: the Video Creator's videos wait in their account's queue until you OK them (Settings → Creator
→ Check videos before they post), always, never, or for an account's first two weeks of Creator videos."""

import json

from posting_helpers import NOW, clipmonster_folder, mark_posted
from test_worker import Main, setup

from ytclip.posting import intake, jobs, schedule
from ytclip.posting.config import Settings
from ytclip.posting.store import Store

DAY = 86400.0


def make_creator(folder):
    """Turn a clip folder into one the Video Creator wrote (clips.json "creator" block)."""
    data = json.loads((folder / "clips.json").read_text())
    data["creator"] = {"ai_generated": True, "id": "c1"}
    (folder / "clips.json").write_text(json.dumps(data))


def take(tmp_path, review="new", creator=True, store=None, now=NOW):
    accs, (a,), main, root = setup(tmp_path)
    if creator:
        make_creator(root / "Pod" / "Talk [abc]")
    main.creator_review = review
    store = store or Store(tmp_path / "lib.json")
    items, _ = intake.take(store, accs, main, now=now)
    return store, accs, a, items, main


def test_creator_videos_wait_for_your_ok_on_a_new_account(tmp_path):
    store, accs, a, items, _ = take(tmp_path)
    assert items and all(i.ai_generated and i.review == "waiting" for i in items)
    assert [i.id for i in store.waiting_review(a.id)] == [i.id for i in sorted(items, key=lambda i: i.added_at)]
    jobs.replan(store, accs, Settings(), NOW)
    assert all(i.planned_at is None for i in items)  # no post time until you OK it
    assert schedule.due(store.queue(a.id), NOW + 30 * DAY) is None
    assert store.approve([items[0].id]) == 1 and items[0].review == "ok"
    assert store.approve([items[0].id]) == 0  # already OK
    jobs.replan(store, accs, Settings(), NOW)
    assert items[0].planned_at and items[1].planned_at is None


def test_clips_never_wait(tmp_path):
    _, _, _, items, _ = take(tmp_path, creator=False)
    assert items and all(i.review == "" for i in items)


def test_never(tmp_path):
    _, _, _, items, _ = take(tmp_path, review="off")
    assert items and all(i.review == "" for i in items)


def test_always(tmp_path):
    store, accs, a, items, main = take(tmp_path, review="always")
    mark_posted(store, items[0].id, now=NOW - 60 * DAY)  # even long after the first one
    assert intake.needs_review(store, main, a.id, items[1], now=NOW)


def test_two_weeks_after_the_first_creator_video_it_stops_asking(tmp_path):
    store, accs, a, items, main = take(tmp_path)
    first = items[0]
    store.approve([first.id])
    mark_posted(store, first.id, now=NOW - 15 * DAY)
    later = clipmonster_folder(tmp_path, name="Pod/Next [def]")
    make_creator(later)
    newer, _ = intake.take(store, accs, main, now=NOW)
    assert newer and all(i.review == "" for i in newer)
    # Only 3 days since the first one: still asking.
    assert intake.needs_review(store, main, a.id, newer[0], now=NOW - 12 * DAY)


def test_held_videos_survive_the_other_process_saving(tmp_path):
    store, accs, a, items, _ = take(tmp_path)
    store.save()
    other = Store(tmp_path / "lib.json")  # the window
    other.approve([items[0].id])
    other.save()
    store.refresh()
    assert items[0].review == "ok" and items[1].review == "waiting"


def test_needs_review_without_settings_defaults_to_new(tmp_path):
    store, accs, a, items, _ = take(tmp_path, creator=True)
    assert intake.needs_review(store, None, a.id, items[0], now=NOW)
    assert not intake.needs_review(store, Main(tmp_path), a.id, store.get(items[0].id).__class__(), now=NOW)
