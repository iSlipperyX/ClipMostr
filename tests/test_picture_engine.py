"""The picture engine (creator/picture_engine.py): what it installs for each graphics card, setting it up (uv and
the downloads faked), the worker's JSON-lines protocol (the real worker, run by this Python in its stub mode,
which draws without PyTorch), and the Creator's pictures going through it when this Python has no PyTorch."""

import io
import json
import os
import struct
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from ytclip.creator import picture_engine as pe
from ytclip.creator import visual_generator as vg
from ytclip.settings import Settings


def png_size(path: Path) -> tuple[int, int]:
    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def stub_worker(monkeypatch):
    """The real worker script, run by this Python (no PyTorch needed in stub mode)."""
    w = pe.Worker(python=Path(sys.executable), stub=True)
    yield w
    w.stop()


def test_plan_for_each_card():
    amd = pe.plan_for("amd", "AMD Radeon RX 9060 XT", platform="win32")
    assert amd.device == "dml" and amd.packages == ["torch-directml"] and not amd.index_url
    assert pe.plan_for("intel", "Intel Arc A770", platform="win32").device == "dml"
    nv = pe.plan_for("nvidia", "NVIDIA GeForce RTX 4070", platform="win32")
    assert nv.device == "cuda" and nv.index_url.endswith("/cu126")
    assert pe.plan_for("nvidia", "NVIDIA GeForce RTX 5070 Ti", platform="win32").index_url.endswith("/cu128")
    assert pe.plan_for("amd", "", platform="linux").device == "cpu"  # DirectML is Windows only
    assert pe.plan_for("apple", "", platform="darwin").device == "mps"
    cpu = pe.plan_for("", "", platform="win32")
    assert cpu.device == "cpu" and cpu.index_url.endswith("/cpu")


def test_not_ready_until_set_up():
    assert pe.status() == {} and not pe.ready() and pe.describe() == "not set up"
    pe.marker().write_text(json.dumps({"version": pe.VERSION, "device": "dml"}))
    assert not pe.ready()  # no Python yet
    pe.python_exe().parent.mkdir(parents=True)
    pe.python_exe().write_text("")
    pe.marker().write_text(json.dumps({"version": pe.VERSION, "device": "dml",
                                       "device_name": "AMD Radeon RX 9060 XT"}))
    assert pe.ready() and pe.describe() == "set up: DirectML on AMD Radeon RX 9060 XT"
    pe.marker().write_text(json.dumps({"version": pe.VERSION - 1, "device": "dml"}))
    assert not pe.ready()  # an older engine is set up again
    pe.marker().write_text("not json")
    assert not pe.ready()


def test_worker_ping_and_pictures(stub_worker, tmp_path):
    assert stub_worker.ping()["device"] == "stub"
    out = stub_worker.generate("stabilityai/sdxl-turbo", "a lighthouse", "", 42, tmp_path / "pics" / "a.png",
                               width=576, height=1024, steps=4, guidance=0.0)
    assert png_size(out) == (72, 128)
    with pytest.raises(pe.EngineError, match="stub failure"):
        stub_worker.generate("stabilityai/sdxl-turbo", "please fail", "", 1, tmp_path / "b.png", width=64,
                             height=64, steps=1, guidance=0.0)
    again = stub_worker.generate("stabilityai/sdxl-turbo", "after a bad one", "", 2, tmp_path / "c.png", width=64,
                                 height=64, steps=1, guidance=0.0)
    assert again.is_file() and stub_worker.alive()  # one bad picture doesn't end the worker
    stub_worker.stop()
    assert not stub_worker.alive()


def test_worker_cancel_stops_it(stub_worker, tmp_path):
    from ytclip.ffmpeg import Cancelled

    stub_worker.ping()

    def check():
        raise Cancelled()

    with pytest.raises(Cancelled):
        stub_worker.generate("x", "y", "", 1, tmp_path / "d.png", width=64, height=64, steps=1, guidance=0.0,
                             check=check)
    assert not stub_worker.alive()


def test_worker_that_dies_says_why_and_starts_again(stub_worker, tmp_path):
    with pytest.raises(pe.EngineError, match="stopped .exit 3: stub crash"):
        stub_worker.generate("x", "crash now", "", 1, tmp_path / "e.png", width=64, height=64, steps=1,
                             guidance=0.0)
    assert stub_worker.ping()["ok"]  # the next picture starts a new one


def test_the_worker_script_is_written_fresh(stub_worker):
    script = pe.root() / "worker.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("old version")
    stub_worker.ping()
    assert script.read_text(encoding="utf-8") == pe.WORKER


def _uv_archive(dest: Path) -> None:
    name = pe.UV_ASSETS[sys.platform]
    if name.endswith(".zip"):
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("uv.exe", b"MZ" + b"\0" * 200_000)
    else:
        data = b"#!/bin/sh\n" + b"\0" * 200_000
        with tarfile.open(dest, "w:gz") as tf:
            info = tarfile.TarInfo(name.removesuffix(".tar.gz") + "/uv")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def test_install_runs_uv_for_the_card(monkeypatch):
    fetched, ran = [], []

    def fetch(url, dest, label, progress, min_bytes=0, **kw):
        fetched.append(url)
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        _uv_archive(Path(dest))
        return Path(dest)

    def runner(cmd, log, check, env, timeout):
        cmd = [str(c) for c in cmd]
        ran.append(cmd)
        assert env["UV_PYTHON_INSTALL_DIR"].startswith(str(pe.root()))
        assert "VIRTUAL_ENV" not in env
        if cmd[1] == "venv":  # a "Python" for the stub worker: this one
            exe = pe.python_exe()
            exe.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(sys.executable, exe)

    logs, steps = [], []
    st = pe.install(log=logs.append, progress=lambda m, f: steps.append(m), vendor="nvidia",
                    gpu_name="NVIDIA GeForce RTX 4070", fetch=fetch, runner=runner, stub=True)
    assert fetched == [pe.UV_BASE + pe.UV_ASSETS[sys.platform]]
    assert (pe.root() / "uv").is_dir() and not (pe.root() / pe.UV_ASSETS[sys.platform]).exists()
    venv, torch, common = ran
    assert venv[1:4] == ["venv", "--python", pe.PYTHON]
    assert torch[1:3] == ["pip", "install"] and "torch" in torch and torch[-2:] == ["--index-url",
                                                                                  pe.TORCH_INDEX + "cu126"]
    assert "diffusers==0.31.0" in common and "transformers==4.46.3" in common
    assert st["device"] == "stub" and st["version"] == pe.VERSION and pe.ready()
    assert png_size(st["test_picture"]) == (64, 64)  # the test picture, drawn by the worker
    assert any("test picture" in m for m in steps) and any("Test picture drawn" in m for m in logs)
    pe.stop()


def test_install_needs_room(monkeypatch):
    class Usage:
        free = 3 * 2 ** 30

    monkeypatch.setattr(pe.shutil, "disk_usage", lambda p: Usage)
    with pytest.raises(pe.EngineError, match="12 GB free"):
        pe.install(vendor="amd", fetch=lambda *a, **k: None, runner=lambda *a: None)


def test_failed_uv_step_leaves_it_not_ready(monkeypatch):
    def fetch(url, dest, label, progress, min_bytes=0, **kw):
        _uv_archive(Path(dest))

    def runner(cmd, log, check, env, timeout):
        if str(cmd[1]) == "pip":
            raise pe.EngineError("uv pip failed (exit 1): no matching distribution")

    with pytest.raises(pe.EngineError, match="no matching"):
        pe.install(vendor="amd", fetch=fetch, runner=runner, stub=True)
    assert not pe.ready()


def test_download_progress_follows_the_model_folder(tmp_path):
    seen = []
    watch = pe.download_watch(tmp_path, "stabilityai/sdxl-turbo", "Making pictures",
                              lambda m, f: seen.append((m, f)), every=0)
    watch()
    assert not seen  # nothing downloading yet
    blobs = pe.model_folder(tmp_path, "stabilityai/sdxl-turbo") / "blobs"
    blobs.mkdir(parents=True)
    (blobs / "unet").write_bytes(b"\0" * (40 * 2 ** 20))
    watch()
    assert seen and "downloading the model (0.0 of about 7 GB)" in seen[-1][0] and 0 < seen[-1][1] < 0.01
    n = len(seen)
    watch()
    assert len(seen) == n  # it stopped growing: no repeat


def _engine_ready(device="dml"):
    pe.python_exe().parent.mkdir(parents=True, exist_ok=True)
    pe.python_exe().write_text("")
    pe.marker().write_text(json.dumps({"version": pe.VERSION, "device": device, "device_name": "Radeon"}))


def test_pictures_use_the_engine_without_pytorch_here(monkeypatch, tmp_path):
    monkeypatch.setattr(vg, "torch_device", lambda: ("", "PyTorch isn't installed (ModuleNotFoundError)"))
    s = Settings()
    assert "aren't set up" in vg.available(s)["sdxl_turbo"]
    _engine_ready("dml")
    ready = vg.available(s)
    assert ready["sdxl_turbo"] == "" and "NVIDIA" in ready["flux_schnell"]
    assert vg.order_for("auto", ready)[0] in ("comfyui", "sdxl_turbo")

    asked = []

    class FakeWorker:
        def alive(self):
            return bool(asked)

        def generate(self, repo, prompt, negative, seed, dest, **kw):
            asked.append((repo, prompt, kw["width"], kw["height"], kw["steps"], kw["cache"]))
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"png")
            return Path(dest)

    monkeypatch.setattr(pe, "worker", lambda log=None: FakeWorker())
    d = vg.Diffusers(s, "sdxl_turbo", lambda m: None, lambda m, f: None)
    assert d.via_engine()
    out = d.generate("a red fox in snow", "blurry", 5, tmp_path / "scene_01.png")
    assert out.is_file()
    assert asked[0][:5] == ("stabilityai/sdxl-turbo", "a red fox in snow", 576, 1024, 4)
    assert asked[0][5].name == "diffusers"  # the same models folder as everything else


def test_engine_on_the_processor_needs_the_cpu_setting(monkeypatch):
    monkeypatch.setattr(vg, "torch_device", lambda: ("", "PyTorch isn't installed (ModuleNotFoundError)"))
    _engine_ready("cpu")
    assert "processor" in vg.available(Settings())["sdxl_turbo"]
    assert vg.available(Settings().apply({"creator_cpu_pictures": True}))["sdxl_turbo"] == ""


def test_unload_stops_the_engine(monkeypatch):
    stopped = []
    monkeypatch.setattr(pe, "stop", lambda: stopped.append(1))
    vg.unload()
    assert stopped
