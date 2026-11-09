"""The Linux host (packaging/linux/): its settings the first time (ytclip host-setup), the Mac app's server
from a command line (ytclip remote), the addresses a Mac reaches a cloud VM at, Chrome on Linux, and the
installer's own consistency (its scripts, services and options). The install itself runs for real on
GitHub's Ubuntu machine (.github/workflows/tests.yml, linux-host)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ytclip import linux_host
from ytclip.posting import config as posting_config
from ytclip.remote import cli as remote_cli
from ytclip.remote import protocol, server, state
from ytclip.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
LINUX = ROOT / "packaging" / "linux"


@pytest.fixture(autouse=True)
def fresh_outside(monkeypatch):
    monkeypatch.setattr(server, "_outside", None)
    monkeypatch.setattr(server, "tailscale_ips", lambda timeout=2.0: [])
    monkeypatch.setattr(server, "on_google_cloud", lambda: False)


# --- ytclip host-setup -------------------------------------------------------------------------------------

def test_first_setup_turns_on_the_mac_server_and_chrome():
    done = linux_host.setup()
    s = Settings.load()
    assert s.remote_on and s.remote_port == protocol.PORT
    ps = posting_config.Settings.load()
    assert ps.browser_channel == "chrome" and ps.show_browser
    assert any("Mac" in d for d in done)  # (Chrome is already the default off Windows)
    assert json.loads(linux_host.marker_path().read_text())["set_up_at"]


def test_later_setups_keep_what_was_changed_in_settings():
    linux_host.setup()
    Settings.load().copy(remote_on=False).save()  # turned off in Settings → Mac app
    ps = posting_config.Settings.load()
    ps.show_browser = False
    ps.save()
    assert linux_host.setup() == []  # an update
    assert not Settings.load().remote_on and not posting_config.Settings.load().show_browser
    assert linux_host.setup(port=47999) == ["set the Mac app's port to 47999"]  # the installer's --port
    assert Settings.load().remote_port == 47999


def test_host_setup_command(capsys):
    from ytclip.cli import main

    assert main(["host-setup", "--port", "48000"]) == 0
    assert "Set up:" in capsys.readouterr().out and Settings.load().remote_port == 48000
    assert main(["host-setup", "--port", "80"]) == 2


# --- ytclip remote -----------------------------------------------------------------------------------------

def run(argv, capsys):
    from ytclip.cli import main

    code = main(["remote", *argv])
    return code, capsys.readouterr().out


def test_remote_on_off_and_status(capsys):
    code, out = run(["status"], capsys)
    assert code == 0 and "Off" in out and "none yet" in out
    code, out = run(["on", "--port", "47555"], capsys)
    assert code == 0 and Settings.load().remote_on and Settings.load().remote_port == 47555
    code, out = run(["status"], capsys)
    assert "automation isn't running" in out and "IP address" in out
    assert run(["on", "--port", "22"], capsys)[0] == 2
    run(["off"], capsys)
    assert not Settings.load().remote_on


def test_remote_link_shows_address_port_and_code(capsys, monkeypatch):
    assert run(["link", "--json"], capsys)[0] == 1  # off: says how to turn it on
    Settings.load().copy(remote_on=True, remote_port=47444).save()
    monkeypatch.setattr(server, "local_addresses", lambda: ["10.128.0.5"])
    code, out = run(["link", "--json"], capsys)
    got = json.loads(out)
    assert code == 0 and got["port"] == 47444 and got["addresses"] == ["10.128.0.5"]
    data = state.load()
    assert protocol.normalize_code(got["code"]) == data["pairing"]["code"] and state.pairing(data, got["until"] - 1)
    code, out = run(["link", "--no-wait"], capsys)
    assert code == 0 and "10.128.0.5" in out and "47444" in out
    assert re.search(r"Authorization code\s+[A-Z0-9]{4}-[A-Z0-9]{4}", out)


def test_remote_link_waits_for_the_mac(capsys, monkeypatch):
    Settings.load().copy(remote_on=True).save()
    monkeypatch.setattr(server, "local_addresses", lambda: [])
    real_sleep = remote_cli.time.sleep

    def mac_links(_s):  # the Mac types the code while the command waits
        with state.edit() as data:
            state.new_device(data, "Justyn's MacBook Pro", remote_cli.time.time())
        monkeypatch.setattr(remote_cli.time, "sleep", real_sleep)

    monkeypatch.setattr(remote_cli.time, "sleep", mac_links)
    code, out = run(["link", "--poll", "0"], capsys)
    assert code == 0 and "Linked Justyn's MacBook Pro" in out


def test_remote_unlink_by_name(capsys):
    with state.edit() as data:
        state.new_device(data, "Studio Mac", 1.0)
        state.new_device(data, "Laptop", 1.0)
    assert run(["unlink", "nobody"], capsys)[0] == 1
    code, out = run(["unlink", "studio mac"], capsys)
    assert code == 0 and "Unlinked Studio Mac" in out
    assert [d["name"] for d in state.load()["devices"]] == ["Laptop"]


# --- the addresses a Mac reaches the host at ---------------------------------------------------------------

def test_a_cloud_vm_gives_its_external_ip_after_tailscale(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "on_google_cloud", lambda: True)
    monkeypatch.setattr(server, "gce_external_ip", lambda timeout=0.6: calls.append(1) or "34.82.1.2")
    monkeypatch.setattr(server, "tailscale_ips", lambda timeout=2.0: ["100.101.7.42"])
    monkeypatch.setattr(server, "local_addresses", lambda: ["10.128.0.5"])
    assert server.mac_addresses() == ["100.101.7.42", "34.82.1.2", "10.128.0.5"]
    server.mac_addresses()
    assert len(calls) == 1  # remembered, not asked every heartbeat


def test_off_google_cloud_nothing_is_asked(monkeypatch):
    monkeypatch.setattr(server, "gce_external_ip", lambda timeout=0.6: pytest.fail("asked the metadata server"))
    monkeypatch.setattr(server, "local_addresses", lambda: ["192.168.1.20"])
    assert server.mac_addresses() == ["192.168.1.20"]


def test_gce_metadata_answer_is_checked(monkeypatch):
    class Answer:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return self.body

    import urllib.request

    class Opener:
        body = b"34.82.1.2\n"

        def open(self, req, timeout):
            assert req.get_header("Metadata-flavor") == "Google"
            return Answer(self.body)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a: Opener())
    assert server.gce_external_ip() == "34.82.1.2"
    Opener.body = b"<html>not an address</html>"
    assert server.gce_external_ip() == ""


# --- Chrome on Linux ---------------------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(shutil, "which") or __import__("os").name == "nt", reason="Linux and macOS")
def test_chrome_is_found_on_linux(monkeypatch, tmp_path):
    from ytclip.posting.tiktok import login

    chrome = tmp_path / "google-chrome-stable"
    monkeypatch.setattr(shutil, "which", lambda name: str(chrome) if name == "google-chrome-stable" else None)
    assert login.find_browser("chrome") == chrome
    assert login.find_browser("msedge") is None or "edge" in str(login.find_browser("msedge"))
    assert posting_config.Settings().browser_channel == "chrome"


# --- the installer -----------------------------------------------------------------------------------------

SCRIPTS = ["install.sh", "clipmonster", "clipmonster-display", "clipmonster-desktop"]


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_parse_and_are_executable(name):
    path = LINUX / name
    assert path.stat().st_mode & 0o111, f"{name} isn't executable"
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck isn't installed")
def test_shellcheck():
    got = subprocess.run(["shellcheck", "-S", "warning", *[str(LINUX / n) for n in SCRIPTS]],
                         capture_output=True, text=True)
    assert got.returncode == 0, got.stdout


def test_the_installer_installs_every_script_and_service_it_uses():
    install = (LINUX / "install.sh").read_text()
    units = sorted(p.name for p in (LINUX / "systemd").glob("*.service"))
    assert units == ["clipmonster-desktop.service", "clipmonster-display.service", "clipmonster.service"]
    for unit in units:
        assert unit in install
        text = (LINUX / "systemd" / unit).read_text()
        for exe in re.findall(r"^Exec\w*=-?(\S+)", text, re.M):
            name = Path(exe).name
            assert exe.startswith("/opt/clipmonster/"), exe
            assert name in ("ytclip",) or f'"$HERE/{name}"' in install, f"{unit} runs {exe}, which isn't installed"
        assert "User=clipmonster" in text and "EnvironmentFile=/etc/clipmonster/env" in text
    admin = (LINUX / "clipmonster").read_text()
    for unit in units:
        assert unit in admin


def test_the_installer_checks_its_options_before_anything():
    def go(*args):
        return subprocess.run(["bash", str(LINUX / "install.sh"), *args], capture_output=True, text=True)

    assert "--version X.Y.Z" in go("--help").stdout
    assert "Unknown option" in go("--nope").stderr
    assert "--port is a number" in go("--port", "80").stderr
    assert "--gpu is" in go("--gpu", "amd").stderr


def test_the_admin_command_explains_itself():
    got = subprocess.run(["bash", str(LINUX / "clipmonster"), "help"], capture_output=True, text=True)
    assert got.returncode == 0
    for cmd in ("status", "link-mac", "desktop", "firewall", "update", "uninstall"):
        assert f"clipmonster {cmd}" in got.stdout
