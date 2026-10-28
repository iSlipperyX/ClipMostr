import os
import time
import zipfile

import pytest

from ytclip import cli, updater


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


def test_an_older_failure_isnt_blamed_for_a_new_attempt(updates):
    updater.update_log().write_text("x Update started\nx Update failed: Close these first: ytclip.exe\n",
                                    encoding="utf-8")
    updater._log_line("Update requested: helper output goes to x")  # the helper then never logged
    assert updater.last_attempt() == ("requested", "") and updater.last_failure() == ""


def test_opened_again_while_installing(updates, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "2.2.0")
    updater.update_log().write_text("x Update requested\nx Update started: waiting\n", encoding="utf-8")
    updater.save_state(pending="2.2.1")
    msg, ok = updater.finished_update_message()
    assert not ok and "still being installed" in msg
    assert updater.load_state()["pending"] == "2.2.1"  # asked again next time
    old = time.time() - updater.INSTALL_WINDOW - 5
    os.utime(updater.update_log(), (old, old))  # the helper died without a final line
    msg, ok = updater.finished_update_message()
    assert not ok and "didn't complete" in msg and not updater.load_state()["pending"]


def test_helper_that_never_started_points_at_its_output(updates, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "2.2.0")
    updater._log_line("Update requested: x")
    old = time.time() - updater.INSTALL_WINDOW - 5
    os.utime(updater.update_log(), (old, old))
    updater.save_state(pending="2.2.1")
    msg, _ = updater.finished_update_message()
    assert "installer didn't start" in msg and "apply-update.out" in msg


def test_cli_updates_from_inside_the_app_folder(tmp_path, monkeypatch):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    monkeypatch.chdir(target)  # used to be refused; the swap no longer needs the folder free
    rel = updater.Release("9.9.9", "v9.9.9", "", "", "", "", 1, "", "", False)
    launched = []
    monkeypatch.setattr(updater, "check", lambda *a: rel)
    monkeypatch.setattr(updater, "install_dir", lambda: target)
    monkeypatch.setattr(updater, "other_instances", lambda t: [])
    monkeypatch.setattr(updater, "download", lambda r, progress: tmp_path / "u.zip")
    monkeypatch.setattr(updater, "extract", lambda z, t: tmp_path / ".cm-up" / "ClipMonster")
    monkeypatch.setattr(updater, "launch_swap", lambda *a, **kw: launched.append(a))
    assert cli.run_update(True) == 0 and launched


def test_update_waits_for_automation_and_gives_up_clearly(monkeypatch):
    from ytclip.jobs import JobDB

    db = JobDB()
    db.set_meta("runner_pid", str(os.getpid()))  # a process that is running
    db.set_meta("heartbeat", time.time())
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    assert updater.stop_automation(timeout=0) is False  # still running
    assert db.meta("stop") == "1" and db.meta("resume_after_update") == "1"
    db.set_meta("runner_pid", "")
    assert updater.stop_automation(timeout=0) is True


def test_a_failed_update_says_why(updates, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "2.2.0")
    updater.update_log().write_text("x Update started\nx Update failed: The new version is incomplete\n",
                                    encoding="utf-8")
    updater.save_state(pending="2.2.1")
    msg, ok = updater.finished_update_message()
    assert not ok and "still on 2.2.0. The new version is incomplete. Details:" in msg
    assert updater.finished_update_message() is None  # said once


def _zip(path, names):
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, b"x")
    return path


def test_path_length_check_allows_for_the_backup_folder(tmp_path, monkeypatch):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    room = updater.MAX_PATH - len(str(target / updater.BACKUP_NAME)) - 1
    deep = "_internal/" + "d" * (room - len("_internal/") + 1)
    with pytest.raises(updater.UpdateError, match="path length"):
        updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/" + deep]), target)
    ok = "_internal/" + "d" * (room - len("_internal/") - 1)
    new_app = updater.extract(_zip(tmp_path / "v.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/" + ok]), target)
    assert (new_app / "ClipMonster.exe").exists()


def test_path_length_check_counts_folders_and_the_old_files(tmp_path):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    room = updater.MAX_PATH - len(str(target / updater.BACKUP_NAME)) - 1
    # A folder needs 12 characters spare even when its files' names are short
    deep_dir = "_internal/" + "d" * (room - len("_internal/") - 12 + 1)
    with pytest.raises(updater.UpdateError, match="path length"):
        updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe", f"ClipMonster/{deep_dir}/a"]), target)
    # The installed version's files move into the backup too
    old = target / "_internal" / ("o" * (room - len("_internal/") + 1))
    old.parent.mkdir()
    old.write_bytes(b"")
    with pytest.raises(updater.UpdateError, match="path length"):
        updater.extract(_zip(tmp_path / "v.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/_internal/x"]), target)
