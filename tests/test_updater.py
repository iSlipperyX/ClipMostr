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
    monkeypatch.setattr(updater, "prepare", lambda r, t, progress: tmp_path / "next" / "ClipMonster")
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
    # The deepest of the backup folder in the install folder and the unpacked copy (next-new) counts
    deepest = max(len(str(target / updater.BACKUP_NAME)), len(str(updater.staging_dir().with_name("next-new") / "ClipMonster")))
    room = updater.MAX_PATH - deepest - 1
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


# ---------------------------------------------------------------- one unpacker at a time
# The window's quiet download and automation's own update could unpack into the same folder at once,
# removing and recreating each other's files: "Cannot create a file when that file already exists".

HOLD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ytclip import updater
with updater.PrepareLock():
    Path(sys.argv[2]).write_text("locked")
    if sys.argv[3] == "stage":  # the other process gets the update meanwhile
        app = updater.staging_dir() / "ClipMonster"
        (app / "_internal").mkdir(parents=True)
        (app / "ClipMonster.exe").write_bytes(b"new")
        (app / "ytclip.exe").write_bytes(b"new")
        updater.save_state(staged="9.9.9")
    time.sleep(float(sys.argv[4]))
"""


def _hold_lock(tmp_path, stage=False, seconds=1.5):
    import subprocess
    import sys
    from pathlib import Path

    flag = tmp_path / "locked.flag"
    script = tmp_path / "hold.py"
    script.write_text(HOLD, encoding="utf-8")
    root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen([sys.executable, str(script), root, str(flag), "stage" if stage else "-", str(seconds)],
                            env=dict(os.environ))
    end = time.time() + 20
    while not flag.exists() and time.time() < end:
        time.sleep(0.05)
    assert flag.exists(), "the helper process didn't take the lock"
    return proc


def _rel():
    return updater.Release("9.9.9", "v9.9.9", "", "", "", "https://example.invalid/x.zip", 1, "", "", False)


def test_prepare_waits_for_another_process_and_reuses_its_copy(tmp_path, monkeypatch):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    monkeypatch.setattr(updater, "download", lambda *a, **k: pytest.fail("downloaded it a second time"))
    proc = _hold_lock(tmp_path, stage=True)
    try:
        assert updater.busy()
        assert updater.tidy(target) == 0  # leaves the other process's work alone
        started = time.monotonic()
        new_app = updater.prepare(_rel(), target)
        assert time.monotonic() - started > 0.3  # it waited its turn
        assert (new_app / "ClipMonster.exe").read_bytes() == b"new"
    finally:
        proc.wait(20)
    assert not updater.busy()


def test_waiting_for_the_lock_can_be_cancelled(tmp_path):
    proc = _hold_lock(tmp_path, seconds=5)
    try:
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(updater.UpdateCancelled):
            updater.PrepareLock(cancelled).acquire()
        with pytest.raises(updater.UpdateError, match="still downloading"):
            updater.PrepareLock(wait=0.2).acquire()
    finally:
        proc.kill()
        proc.wait(20)


def test_unpacking_replaces_a_leftover_next_folder(tmp_path):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    leftover = updater.staging_dir() / "ClipMonster" / "_internal"
    leftover.mkdir(parents=True)
    (leftover / "old.dll").write_bytes(b"old")
    new_app = updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe", "ClipMonster/_internal/x"]),
                              target)
    assert new_app == updater.staging_dir() / "ClipMonster"
    assert (new_app / "_internal" / "x").exists() and not (new_app / "_internal" / "old.dll").exists()
    assert not updater.staging_dir().with_name("next-new").exists()


def test_a_held_next_folder_is_moved_aside_and_tidied_later(tmp_path, monkeypatch):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    held = updater.staging_dir() / "ClipMonster"
    held.mkdir(parents=True)
    (held / "ytclip.exe").write_bytes(b"still running")
    real = updater.shutil.rmtree

    def stubborn(path, *a, **k):  # antivirus or a running helper keeps the folder from being removed
        if os.path.abspath(path) == os.path.abspath(updater.staging_dir()):
            return
        real(path, *a, **k)

    monkeypatch.setattr(updater.shutil, "rmtree", stubborn)
    monkeypatch.setattr(updater.time, "sleep", lambda s: None)
    new_app = updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe"]), target)
    assert (new_app / "ClipMonster.exe").exists()
    aside = [p for p in updater.updates_dir().iterdir() if p.name.startswith("next-old-")]
    assert len(aside) == 1 and (aside[0] / "ClipMonster" / "ytclip.exe").exists()
    monkeypatch.setattr(updater.shutil, "rmtree", real)
    updater.tidy(target)
    assert not aside[0].exists()


def test_a_damaged_archive_leaves_the_staged_copy_alone(tmp_path):
    target = tmp_path / "ClipMonster"
    target.mkdir()
    updater.extract(_zip(tmp_path / "u.zip", ["ClipMonster/ClipMonster.exe"]), target)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(updater.UpdateError, match="damaged"):
        updater.extract(bad, target)
    with pytest.raises(updater.UpdateError, match="doesn't contain"):
        updater.extract(_zip(tmp_path / "v.zip", ["ClipMonster/readme.txt"]), target)
    assert (updater.staging_dir() / "ClipMonster" / "ClipMonster.exe").exists()
    assert not updater.staging_dir().with_name("next-new").exists()
