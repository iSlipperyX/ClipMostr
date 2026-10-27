import zipfile

import pytest

from ytclip import updater


@pytest.fixture
def updates(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(updater, "_state_path", lambda: tmp_path / "update_state.json")
    return updater.updates_dir()


def test_last_failure_reads_the_latest_attempt(updates):
    log = updater.update_log()
    log.write_text("2026-10-27 10:57:00 Update started: waiting for process 1 to exit\n"
                   "2026-10-27 10:57:20 Update failed: Could not move the old version aside\n"
                   "2026-10-27 11:01:28 Update started: waiting for process 2 to exit\n"
                   "2026-10-27 11:01:48 Update failed: Close these first: ytclip.exe (process 9)\n", encoding="utf-8")
    assert updater.last_failure() == "Close these first: ytclip.exe (process 9)"
    with log.open("a", encoding="utf-8") as f:
        f.write("2026-10-27 12:00:00 Update started: waiting for process 3 to exit\n")
    assert updater.last_failure() == ""  # the newest attempt hasn't failed


def test_a_failed_update_says_why(updates, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "2.2.0")
    updater.update_log().write_text("x Update started\nx Update failed: The new version is incomplete\n",
                                    encoding="utf-8")
    updater.save_state(pending="2.2.1")
    msg, ok = updater.finished_update_message()
    assert not ok and "still on 2.2.0. The new version is incomplete. Details:" in msg
    assert updater.finished_update_message() is None  # said once


def test_the_install_folder_itself_is_never_moved():
    script = updater.SWAP_SCRIPT
    # Renaming the whole folder failed whenever File Explorer, a terminal or any program had it open.
    assert "Directory]::Move($InstallDir" not in script
    assert "Move-Item " not in script and "Remove-Item" not in script
    # Only the app's own files move; anything else in the folder stays.
    assert "@('ClipMonster.exe', 'ytclip.exe', '_internal')" in script
    assert "Undo-Moves" in script


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return path


def test_path_length_check_allows_for_the_backup_folder(tmp_path, monkeypatch):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    room = updater.MAX_PATH - len(str(target / ".cm-old-20991231235959")) - 1
    deep = "_internal/" + "d" * (room - len("_internal/") + 1)
    with pytest.raises(updater.UpdateError, match="path length"):
        updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/" + deep]), target)
    ok = "_internal/" + "d" * (room - len("_internal/") - 1)
    new_app = updater.extract(_zip(tmp_path / "v.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/" + ok]), target)
    assert (new_app / "ClipMonster.exe").exists()
