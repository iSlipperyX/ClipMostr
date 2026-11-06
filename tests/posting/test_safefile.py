"""A damaged save file must never turn into an empty library, settings or account list."""

import json
import os
import time

import pytest

from ytclip.posting import safefile
from ytclip.posting.accounts import Accounts
from ytclip.posting.config import Settings
from ytclip.posting.store import Store, VideoItem


@pytest.fixture(autouse=True)
def fresh_notices(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    safefile.notices.clear()
    yield
    safefile.notices.clear()


def library_with(path, *names):
    st = Store(path)
    for n in names:
        it = VideoItem(path=f"C:/x/{n}.mp4", title=n)
        st.items[it.id] = it
    st.save()
    return st


def test_save_keeps_the_previous_file_as_bak(tmp_path):
    path = tmp_path / "library.json"
    st = library_with(path, "one")
    st.items.clear()
    st.save()
    assert len(json.loads((tmp_path / "library.json.bak").read_text(encoding="utf-8"))["items"]) == 1


def test_damaged_library_loads_the_backup_and_is_kept(tmp_path):
    path = tmp_path / "library.json"
    st = library_with(path, "one", "two")
    st.save()  # the first save made no .bak (nothing to keep yet); this one does
    path.write_text('{"version": 2, "items": [', encoding="utf-8")  # cut off mid-write
    again = Store(path)
    assert sorted(i.title for i in again.items.values()) == ["one", "two"]
    damaged = list(tmp_path.glob("library.json.damaged-*"))
    assert len(damaged) == 1 and damaged[0].read_text(encoding="utf-8").startswith('{"version": 2')
    assert safefile.notices and "posting library" in safefile.notices[0]
    again.save()  # saving after a recovery writes the recovered library, not an empty one
    assert len(json.loads(path.read_text(encoding="utf-8"))["items"]) == 2


def test_damaged_file_without_backup_starts_empty_but_is_not_overwritten(tmp_path):
    path = tmp_path / "library.json"
    path.write_text("\x00\x00garbage", encoding="utf-8")
    st = Store(path)
    assert st.items == {}
    st.save()
    (damaged,) = tmp_path.glob("library.json.damaged-*")
    assert damaged.read_text(encoding="utf-8") == "\x00\x00garbage"
    assert "no backup" in safefile.notices[0]


def test_damaged_bak_falls_back_to_the_daily_copy(tmp_path):
    path = tmp_path / "library.json"
    library_with(path, "one").save()
    (tmp_path / "library.json.bak").write_text("", encoding="utf-8")
    path.write_text("{", encoding="utf-8")
    assert [i.title for i in Store(path).items.values()] == ["one"]


def test_daily_copies_are_kept_for_a_week(tmp_path):
    path = tmp_path / "library.json"
    folder = tmp_path / "backups"
    folder.mkdir()
    for day in range(1, 11):
        (folder / f"library-2026-10-{day:02d}.json").write_text("{}", encoding="utf-8")
    library_with(path, "one").save()
    kept = sorted(p.name for p in folder.glob("library-*.json"))
    assert len(kept) == safefile.BACKUP_DAYS
    assert kept[-1] == f"library-{time.strftime('%Y-%m-%d')}.json"


def test_first_start_has_no_notice(tmp_path):
    assert safefile.load(tmp_path / "nothing.json", "your settings") is None
    assert safefile.notices == []


def test_damaged_settings_keep_their_values():
    s = Settings(posts_per_day=5, default_hashtags="#mine")
    s.save()
    s.save()
    Settings.path().write_text('{"posts_per_day": 5, "default_hasht', encoding="utf-8")
    again = Settings.load()
    assert (again.posts_per_day, again.default_hashtags) == (5, "#mine")


def test_damaged_account_list_keeps_the_accounts(tmp_path):
    path = tmp_path / "accounts.json"
    accs = Accounts.load(path)
    accs.add("Second")
    accs.save()
    accs.save()
    path.write_bytes(os.urandom(64))
    again = Accounts.load(path)
    assert [a.name for a in again][-1] == "Second"
