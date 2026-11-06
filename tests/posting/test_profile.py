"""Profile setup: the photo, name and bio set up per account, and knowing what's been applied."""

import subprocess

import pytest

from ytclip.posting import profile
from ytclip.posting.accounts import Accounts
from ytclip.posting.config import Settings
from ytclip.posting.media import ffmpeg_exe, probe


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))


def picture(tmp_path, w, h, name="pic.png", colour="red"):
    path = tmp_path / name
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s={w}x{h}",
                    "-frames:v", "1", str(path)], check=True)
    return path


def test_photo_is_cropped_square_and_sized_for_tiktok(tmp_path):
    a = Accounts.load().add("")
    out = profile.prepare_photo(picture(tmp_path, 1600, 900), a.id)
    info = probe(out)
    assert (info.width, info.height) == (profile.PHOTO_SIZE, profile.PHOTO_SIZE)
    assert out.parent == profile.photos_dir() and out.name.startswith(a.id)
    assert profile.photo_path(a) is None  # prepared, not used yet
    profile.use_photo(a, out)
    assert profile.photo_path(a) == out


def test_new_photo_replaces_the_old_file_and_none_removes_it(tmp_path):
    a = Accounts.load().add("")
    first = profile.prepare_photo(picture(tmp_path, 400, 400), a.id)
    profile.use_photo(a, first)
    second = profile.prepare_photo(picture(tmp_path, 500, 300, "b.png", "blue"), a.id)
    assert second != first
    profile.use_photo(a, second)
    assert not first.exists() and profile.photo_path(a) == second
    profile.use_photo(a, None)
    assert not second.exists() and a.profile_photo == "" and profile.photo_path(a) is None


def test_not_a_picture_is_a_plain_error(tmp_path):
    bad = tmp_path / "notes.png"
    bad.write_text("hello", encoding="utf-8")
    with pytest.raises(RuntimeError, match="couldn't be read as a picture"):
        profile.prepare_photo(bad, "x")
    assert not list(profile.photos_dir().glob("*"))


def test_limits():
    assert profile.problems("a" * 30, "b" * 80) == []
    issues = profile.problems("a" * 31, "b" * 81)
    assert len(issues) == 2 and "30" in issues[0] and "80" in issues[1]


def test_pending_until_applied_and_again_after_an_edit(tmp_path):
    accs = Accounts.load()
    a = accs.add("")
    assert not profile.is_set(a) and not profile.pending(a)
    assert profile.status(a) == "No profile set up yet."
    a.profile_bio = "Daily podcast clips 🎙️"
    assert profile.pending(a) and "not applied" in profile.status(a)
    profile.mark_applied(a)
    assert not profile.pending(a) and profile.status(a).startswith("Profile applied to TikTok")
    profile.use_photo(a, profile.prepare_photo(picture(tmp_path, 300, 300), a.id))
    assert profile.pending(a)  # a new photo needs applying
    a.profile_error = "TikTok didn't save it"
    assert "Not fully applied" in profile.status(a)
    # It all survives a restart.
    accs.save()
    again = Accounts.load().items[0]
    assert (again.profile_bio, again.profile_photo, again.profile_applied) == \
           (a.profile_bio, a.profile_photo, a.profile_applied)
    assert profile.pending(again)
