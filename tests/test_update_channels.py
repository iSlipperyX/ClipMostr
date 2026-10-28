"""Update channels (Stable, Beta, Dev), downloading in the background, automation updating itself,
and going back to the previous version: against a fake GitHub serving real zips over local HTTP."""

import hashlib
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ytclip import pipeline, settings, swap, updater
from ytclip.settings import Settings


def app_zip(tag: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ClipMonster/ClipMonster.exe", f"exe {tag}")
        zf.writestr("ClipMonster/ytclip.exe", f"cli {tag}")
        zf.writestr("ClipMonster/_internal/a.dll", f"dll {tag}")
    return buf.getvalue()


class FakeGitHub:
    """/repos/o/r/releases(/latest) and the assets, like api.github.com and its download links."""

    def __init__(self):
        self.releases: list[dict] = []
        self.files: dict[str, bytes] = {}
        self.hits: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.hits.append(self.path)
                if self.path.startswith("/repos/o/r/releases/latest"):
                    stable = [r for r in fake.releases if not r["prerelease"]]
                    body = json.dumps(stable[0]).encode() if stable else None
                elif self.path.startswith("/repos/o/r/releases"):
                    body = json.dumps(fake.releases).encode()
                else:
                    body = fake.files.get(self.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, tag: str, name: str = "", prerelease: bool = False, commit: str = "", zip_asset: bool = True):
        data = app_zip(name or tag)
        path = f"/dl/{tag}/{updater.ASSET_NAME}"
        self.files[path] = data
        assets = [{"name": updater.ASSET_NAME, "browser_download_url": self.base + path, "size": len(data),
                   "digest": "sha256:" + hashlib.sha256(data).hexdigest()}] if zip_asset else []
        # newest first, as GitHub lists them
        self.releases.insert(0, {"tag_name": tag, "name": name or f"ClipMonster {tag.lstrip('v')}",
                                 "body": "notes", "html_url": f"https://github.com/o/r/releases/{tag}",
                                 "prerelease": prerelease, "draft": False, "assets": assets,
                                 "target_commitish": commit or "main"})


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(updater, "API_BASE", fake.base)
    yield fake
    fake.server.shutdown()


@pytest.fixture
def published(github):
    github.add("v2.7.0")
    github.add("v2.8.0-beta.1", prerelease=True)
    github.add("dev", name="ClipMonster 2.8.0-beta.1+dev.4", prerelease=True, commit="1a2b3c4d5e6f7a8b")
    return github


# ---------------------------------------------------------------- versions

def test_dev_builds_sort_after_the_release_they_build_on():
    order = ["2.7.0", "2.7.0+dev.1", "2.7.0+dev.12", "2.8.0-beta.1", "2.8.0-beta.1+dev.3", "2.8.0-beta.2",
             "2.8.0", "2.8.0+dev.1", "2.8.1"]
    assert sorted(order, key=updater.parse_version) == order
    assert updater.is_newer("2.7.0+dev.2", "2.7.0+dev.1") and not updater.is_newer("2.7.0", "2.7.0+dev.1")


def test_channel_of_a_version():
    assert [updater.channel_of(v) for v in ("2.8.0", "2.8.0-beta.1", "2.8.0+dev.3", "2.8.0-rc.1+dev.1")] == \
        ["stable", "beta", "dev", "dev"]


def test_build_label():
    assert updater.build_label("2.8.0", "abc") == "2.8.0"
    assert updater.build_label("2.8.0-beta.2") == "2.8.0-beta.2 (beta)"
    assert updater.build_label("2.8.0+dev.3", "1a2b3c4d5e") == "2.8.0+dev.3 (dev build 1a2b3c4)"


# ---------------------------------------------------------------- channels

def test_each_channel_offers_its_newest(published):
    assert updater.check("o/r", "stable", current="2.6.1").version == "2.7.0"
    assert updater.check("o/r", "beta", current="2.7.0").version == "2.8.0-beta.1"
    dev = updater.check("o/r", "dev", current="2.7.0")
    assert dev.version == "2.8.0-beta.1+dev.4" and dev.tag == "dev" and dev.commit == "1a2b3c4d5e6f7a8b"
    assert dev.label == "2.8.0-beta.1+dev.4 (dev build 1a2b3c4)"
    assert updater.check("o/r", "stable", current="2.7.0") is None
    assert updater.check("o/r", "dev", current="2.8.0-beta.1+dev.4") is None


def test_a_stable_release_newer_than_the_beta_is_offered_on_beta(published):
    published.add("v2.8.0")
    assert updater.check("o/r", "beta", current="2.8.0-beta.1").version == "2.8.0"
    assert updater.check("o/r", "dev", current="2.8.0-beta.1+dev.4").version == "2.8.0"


def test_picking_a_steadier_channel_offers_its_newest_as_a_switch(published):
    rel = updater.check("o/r", "stable", current="2.8.0-beta.1+dev.4")
    assert rel.version == "2.7.0" and rel.switch
    rel = updater.check("o/r", "beta", current="2.8.0-beta.1+dev.4")
    assert rel.version == "2.8.0-beta.1" and rel.switch
    assert not updater.check("o/r", "beta", current="2.8.0-beta.1")  # already the newest beta


def test_releases_without_the_app_zip_and_odd_tags_are_ignored(github):
    github.add("v2.7.0")
    github.add("v2.9.0", zip_asset=False)  # still building
    github.add("nightly", prerelease=True)
    github.add("dev", name="Something else", prerelease=True)  # no version in its title
    assert [r.version for r in updater.releases("o/r", "dev")] == ["2.7.0"]


# ---------------------------------------------------------------- download and unpack

def test_prepare_downloads_unpacks_and_deletes_the_zip(published, tmp_path):
    target = tmp_path / "Programs" / "ClipMonster"
    target.mkdir(parents=True)
    rel = updater.check("o/r", "beta", current="2.7.0")
    new_app = updater.prepare(rel, target)
    assert new_app == updater.staging_dir() / "ClipMonster"
    assert (new_app / "ClipMonster.exe").read_text() == "exe v2.8.0-beta.1"
    assert not list(updater.updates_dir().glob("*.zip")) and not list(updater.updates_dir().glob("*.part"))
    assert updater.load_state()["staged"] == "2.8.0-beta.1"
    assert updater.updates_dir().parent.name == "ClipMonster"  # %LOCALAPPDATA%\ClipMonster\updates
    hits = len(published.hits)
    assert updater.prepare(rel, target) == new_app and len(published.hits) == hits  # ready already: no download


def test_a_bad_checksum_is_refused(published, tmp_path):
    rel = updater.check("o/r", "stable", current="2.6.0")
    rel.sha256 = "0" * 64
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.prepare(rel, tmp_path)
    assert not updater.load_state().get("staged") and not list(updater.updates_dir().glob("*.zip"))


def test_tidy_keeps_a_downloaded_update_until_it_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "2.7.0")
    staged = updater.staging_dir() / "ClipMonster"
    staged.mkdir(parents=True)
    updater.save_state(staged="2.8.0")
    old_part = updater.updates_dir() / "ClipMonster-windows-2.8.0.part"
    old_part.write_bytes(b"x")
    import os
    os.utime(old_part, (1, 1))
    log = updater.update_log()
    log.write_bytes(b"line\n" * (updater.LOG_LIMIT // 4))
    updater.tidy(tmp_path / "app")
    assert staged.is_dir() and not old_part.exists()
    assert log.stat().st_size <= updater.LOG_LIMIT // 2 and log.read_bytes().startswith(b"line\n")
    monkeypatch.setattr(updater, "__version__", "2.8.0")  # installed now
    updater.tidy(tmp_path / "app")
    assert not updater.staging_dir().exists() and not updater.load_state()["staged"]


# ---------------------------------------------------------------- the install step keeps the previous version

def write_app(folder: Path, tag: str) -> None:
    (folder / "_internal").mkdir(parents=True, exist_ok=True)
    (folder / "ClipMonster.exe").write_text(f"exe {tag}")
    (folder / "ytclip.exe").write_text(f"cli {tag}")
    (folder / "_internal" / "a.dll").write_text(f"dll {tag}")


def test_the_replaced_version_is_kept_and_can_be_gone_back_to(tmp_path, monkeypatch):
    app = tmp_path / "Programs" / "ClipMonster"
    write_app(app, "2.8.0")
    new = updater.staging_dir() / "ClipMonster"
    write_app(new, "2.8.1")
    lines = []
    assert swap.apply(new, app, 0, "", lines.append, sleep=lambda s: None, previous=updater.previous_dir(),
                      old_version="2.8.0")
    assert (app / "ClipMonster.exe").read_text() == "exe 2.8.1"
    kept = updater.previous_dir() / "ClipMonster"
    assert (kept / "ClipMonster.exe").read_text() == "exe 2.8.0" and not swap.old_backups(app)
    assert lines[-1] == "Update finished" and any("Kept the previous version (2.8.0)" in x for x in lines)
    monkeypatch.setattr(updater, "__version__", "2.8.1")
    assert updater.previous_version() == "2.8.0"
    staged = updater.prepare_previous(app)
    assert staged == updater.staging_dir() / "ClipMonster" and (staged / "ClipMonster.exe").read_text() == "exe 2.8.0"
    assert (kept / "ClipMonster.exe").exists()  # copied, so the next install step can replace it
    # Going back is an install like any other: 2.8.1 is kept in its place
    assert swap.apply(staged, app, 0, "", lines.append, sleep=lambda s: None, previous=updater.previous_dir(),
                      old_version="2.8.1")
    assert (app / "ClipMonster.exe").read_text() == "exe 2.8.0" and (kept / "ClipMonster.exe").read_text() == "exe 2.8.1"


def test_no_previous_version_without_a_kept_copy(monkeypatch):
    assert updater.previous_version() == ""
    with pytest.raises(updater.UpdateError, match="no previous version"):
        updater.prepare_previous(Path("x"))
    write_app(updater.previous_dir() / "ClipMonster", "x")
    (updater.previous_dir() / "version.txt").write_text(updater.__version__)
    assert updater.previous_version() == ""  # the version that's installed now


def test_the_helper_command_line_for_relaunching_automation(tmp_path):
    cmd = swap.command(tmp_path / "new", tmp_path / "app", 9, tmp_path / "u.log", "ClipMonster.exe",
                       relaunch_args=("--automation",), keep_previous=tmp_path / "prev", old_version="2.8.0")
    assert "--relaunch-arg=--automation" in cmd
    assert cmd[cmd.index("--keep-previous") + 1] == str(tmp_path / "prev") and cmd[-1] == "2.8.0"
    started = []
    case = tmp_path / "case"
    write_app(case / "app", "old")
    write_app(case / "new", "new")
    import ytclip.swap as s

    orig = s.start_app
    s.start_app = lambda install, relaunch, log, args=(): started.append((relaunch, args))
    try:
        assert s.main(["--new-dir", str(case / "new"), "--install-dir", str(case / "app"), "--log-file",
                       str(tmp_path / "u.log"), "--relaunch", "ClipMonster.exe", "--relaunch-arg=--automation",
                       "--keep-previous", str(tmp_path / "prev"), "--old-version", "2.8.0"]) == 0
    finally:
        s.start_app = orig
    assert started == [("ClipMonster.exe", ("--automation",))]
    assert (tmp_path / "prev" / "version.txt").read_text() == "2.8.0"


# ---------------------------------------------------------------- settings

def test_include_betas_becomes_the_beta_channel_and_clean_up_turns_on(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"include_prereleases": True, "cleanup_auto": False}), encoding="utf-8")
    s = Settings.load(path)
    assert s.update_channel == "beta" and s.cleanup_auto
    path.write_text(json.dumps({"include_prereleases": False}), encoding="utf-8")
    assert Settings.load(path).update_channel == "stable"
    s.cleanup_auto = False
    s.save(path)  # saved by 2.8.0: your own choice from then on
    assert not Settings.load(path).cleanup_auto
    assert Settings().update_channel == "stable" and Settings().auto_update and Settings().cleanup_auto


def test_local_folders_are_in_localappdata(tmp_path):
    assert settings.local_dir() == tmp_path / "local" / "ClipMonster"
    assert settings.work_dir() == tmp_path / "local" / "ClipMonster" / "work"
    assert updater.updates_dir() == tmp_path / "local" / "ClipMonster" / "updates"


# ---------------------------------------------------------------- automation updates itself

class Recorder:
    def __init__(self):
        self.lines = []

    def __call__(self, line):
        self.lines.append(line)


def test_automation_installs_an_update_while_idle(published, tmp_path, monkeypatch):
    from ytclip.jobs import JobDB

    target = tmp_path / "Programs" / "ClipMonster"
    write_app(target, "2.7.0")
    monkeypatch.setattr(updater, "install_dir", lambda: target)
    monkeypatch.setattr(updater, "other_instances", lambda t: [])
    launched = []
    monkeypatch.setattr(updater, "launch_swap", lambda *a, **kw: launched.append((a, kw)))
    monkeypatch.setattr(updater, "__version__", "2.7.0")
    s = Settings(update_repo="o/r", update_channel="beta")
    log = Recorder()
    runner = pipeline.Runner(db=JobDB(tmp_path / "jobs.db"), load=lambda: s, log=log, self_update=True)
    with pytest.raises(pipeline.Stopped):
        runner.self_update(s)
    (new_app, got_target, pid), kw = launched[0]
    assert got_target == target and pid == runner.pid and (new_app / "ClipMonster.exe").exists()
    assert kw == {"relaunch": "ClipMonster.exe", "relaunch_args": ("--automation",)}
    assert updater.load_state()["pending"] == "2.8.0-beta.1"
    assert any("starts again in the new version" in line for line in log.lines)
    runner.self_update(s)  # checked a moment ago: not again yet
    assert len(launched) == 1


def test_automation_leaves_updates_to_an_open_window_and_skipped_versions(published, tmp_path, monkeypatch):
    from ytclip.jobs import JobDB

    monkeypatch.setattr(updater, "install_dir", lambda: tmp_path)
    launched = []
    monkeypatch.setattr(updater, "launch_swap", lambda *a, **kw: launched.append(a))
    s = Settings(update_repo="o/r", update_channel="stable")
    runner = pipeline.Runner(db=JobDB(tmp_path / "jobs.db"), load=lambda: s, self_update=True)
    monkeypatch.setattr(updater, "__version__", "2.6.0")
    monkeypatch.setattr(updater, "other_instances", lambda t: ["ClipMonster.exe (process 5)"])
    runner.self_update(s)  # the window is open: it offers the update
    updater.save_state(last_check=0, skipped="2.7.0")
    monkeypatch.setattr(updater, "other_instances", lambda t: [])
    runner.self_update(s)  # skipped in the window
    updater.save_state(last_check=0, skipped="")
    runner.self_update(s.copy(auto_update=False))
    monkeypatch.setattr(updater, "__version__", "2.8.0+dev.3")  # a dev build, with Stable picked: asked in the window
    updater.save_state(last_check=0)
    runner.self_update(s)
    assert launched == [] and updater.due_for_check() is False  # it did look
    monkeypatch.setattr(updater, "__version__", "2.6.0")
    updater.save_state(last_check=0)
    with pytest.raises(pipeline.Stopped):
        runner.self_update(s)  # and with nothing in the way, it updates
    assert len(launched) == 1
