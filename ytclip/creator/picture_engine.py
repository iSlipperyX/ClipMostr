"""The picture engine: Stable Diffusion (SDXL-Turbo, or FLUX.1-schnell on a big NVIDIA card) in a Python of its
own, set up with one click, for the installed app.

ClipMonster.exe is a packaged app without PyTorch (several GB, and the right build depends on the graphics card),
so "Set up AI pictures" makes a separate environment in %LOCALAPPDATA%\\ClipMonster\\picture-engine:

* uv (Astral's installer: one file from its GitHub releases) fetches a private Python 3.11 and the packages;
* AMD and Intel cards get PyTorch with DirectML (torch-directml), NVIDIA cards PyTorch with CUDA, anything else
  the processor build (slow, but it works);
* diffusers, transformers and accelerate on top, at versions known to work together; the model itself downloads
  from Hugging Face with the first picture (about 7 GB for SDXL-Turbo) into the same models folder the app uses.

The Creator talks to a worker process in that environment (worker.py, written here) over its standard input and
output, one JSON line each way: it loads the model once, makes every scene's picture, and leaves when it's told
to or when ClipMonster ends. Removing the folder removes all of it (the model stays in the models folder).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]
Runner = Callable[[list, Log, "Callable[[], None] | None", dict, float], None]

VERSION = 1  # bump to have everyone's engine set up again
PYTHON = "3.11"
UV_BASE = "https://github.com/astral-sh/uv/releases/latest/download/"
UV_ASSETS = {"win32": "uv-x86_64-pc-windows-msvc.zip", "linux": "uv-x86_64-unknown-linux-gnu.tar.gz",
             "darwin": "uv-aarch64-apple-darwin.tar.gz"}
TORCH_INDEX = "https://download.pytorch.org/whl/"
# One set from late 2024 that works together (and with torch-directml's torch 2.4.1). Newer diffusers and
# transformers releases need newer PyTorch than DirectML has, so they're held here on purpose.
COMMON = ["diffusers==0.31.0", "transformers==4.46.3", "accelerate==1.1.1", "huggingface-hub>=0.26,<0.28",
          "tokenizers>=0.20,<0.21", "safetensors>=0.4.5", "numpy>=1.26,<2.2", "pillow>=10"]
NEED_GB = 12.0  # the environment (~2-4 GB) and SDXL-Turbo (~7 GB, downloaded with the first picture)
INSTALL_TIMEOUT = 60 * 60
PICTURE_TIMEOUT = 300.0  # without any sign of life from the worker
PICTURE_LIMIT = 2 * 60 * 60  # in all, for the first picture that also downloads the model
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class EngineError(RuntimeError):
    """The picture engine couldn't be set up, or didn't answer."""


def root() -> Path:
    from ..settings import local_dir

    return local_dir("picture-engine")


def env_dir() -> Path:
    return root() / "env"


def python_exe() -> Path:
    env = env_dir()
    return env / "Scripts" / "python.exe" if sys.platform == "win32" else env / "bin" / "python"


def marker() -> Path:
    return root() / "ready.json"


@dataclass
class Plan:
    device: str  # dml | cuda | mps | cpu
    packages: list[str]
    index_url: str = ""  # PyTorch's own package index ("" = PyPI)
    label: str = ""


def plan_for(vendor: str, gpu_name: str = "", platform: str = sys.platform) -> Plan:
    """What to install for this PC's graphics card."""
    if vendor == "nvidia":
        cuda = "cu128" if "RTX 50" in gpu_name.upper() else "cu126"  # the RTX 50 series needs CUDA 12.8
        return Plan("cuda", ["torch"], TORCH_INDEX + cuda, "PyTorch for NVIDIA (CUDA)")
    if vendor in ("amd", "intel") and platform == "win32":
        return Plan("dml", ["torch-directml"], "", f"PyTorch with DirectML for your {vendor.upper()} card")
    if vendor == "apple" or platform == "darwin":
        return Plan("mps", ["torch"], "", "PyTorch for Apple Silicon")
    return Plan("cpu", ["torch"], TORCH_INDEX + "cpu", "PyTorch for the processor (slow)")


def status() -> dict:
    """{} when it isn't set up, else what ready.json says (device, device_name, made, versions)."""
    try:
        data = json.loads(marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != VERSION or not python_exe().is_file():
        return {}
    return data


def ready() -> bool:
    return bool(status())


def describe() -> str:
    """One line for Settings, the Creator tab and `ytclip --check`."""
    st = status()
    if not st:
        return "not set up"
    where = {"dml": "DirectML", "cuda": "CUDA", "mps": "Apple GPU", "cpu": "the processor (slow)"}
    name = st.get("device_name") or ""
    return f"set up: {where.get(st.get('device', ''), st.get('device', ''))}" + (f" on {name}" if name and
                                                                                 st.get("device") != "cpu" else "")


# ---------------------------------------------------------------- setting it up

def _run(cmd: list, log: Log, check: Callable[[], None] | None, env: dict, timeout: float) -> None:
    """Run uv, logging its useful lines; EngineError with the last ones when it fails."""
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
    tail: list[str] = []
    end = time.monotonic() + timeout
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail = (tail + [line])[-12:]
                if any(w in line for w in ("Downloading", "Installed", "Prepared", "Resolved", "rror", "Using")):
                    log("  " + line.strip()[:200])
            if check:
                check()
            if time.monotonic() > end:
                raise EngineError(f"Setting up took more than {timeout / 60:.0f} minutes.")
        code = proc.wait()
    except BaseException:
        proc.kill()
        raise
    if code != 0:
        raise EngineError(f"uv {cmd[1]} failed (exit {code}): " + " | ".join(tail[-4:]))


def _uv(progress: Progress, fetch=None) -> Path:
    """uv's executable, downloaded once."""
    from . import downloads

    exe = root() / "uv" / ("uv.exe" if sys.platform == "win32" else "uv")
    if exe.is_file():
        return exe
    asset = UV_ASSETS.get(sys.platform)
    if not asset:
        raise EngineError(f"AI pictures can't be set up automatically on {sys.platform}.")
    archive = root() / asset
    (fetch or downloads.fetch)(UV_BASE + asset, archive, "Setting up AI pictures: downloading uv", progress,
                               min_bytes=100_000)
    exe.parent.mkdir(parents=True, exist_ok=True)
    try:
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                name = next(n for n in zf.namelist() if n.rsplit("/", 1)[-1] == exe.name)
                exe.write_bytes(zf.read(name))
        else:
            with tarfile.open(archive) as tf:
                member = next(m for m in tf.getmembers() if m.name.rsplit("/", 1)[-1] == exe.name)
                exe.write_bytes(tf.extractfile(member).read())  # type: ignore[union-attr]
            exe.chmod(0o755)
    except (StopIteration, zipfile.BadZipFile, tarfile.TarError, OSError, AttributeError) as exc:
        exe.unlink(missing_ok=True)
        raise EngineError(f"uv's download couldn't be unpacked ({exc}).") from exc
    finally:
        archive.unlink(missing_ok=True)
    return exe


def uv_env() -> dict:
    env = dict(os.environ)
    for k in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX"):
        env.pop(k, None)
    env.update({"UV_PYTHON_INSTALL_DIR": str(root() / "python"), "UV_CACHE_DIR": str(root() / "cache"),
                "UV_NO_PROGRESS": "1", "UV_LINK_MODE": "copy"})
    return env


def install(*, log: Log | None = None, progress: Progress | None = None, check: Callable[[], None] | None = None,
            vendor: str | None = None, gpu_name: str = "", fetch=None, runner: Runner | None = None,
            stub: bool = False, with_model: bool = True) -> dict:
    """Set the engine up (or again), then download SDXL-Turbo and draw a test picture (`with_model`).
    Returns status(). In tests, `fetch` and `runner` stand in for the download and for uv, and `stub` has the
    worker answer without PyTorch."""
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    run = runner or _run
    if vendor is None:
        from ..intelligence import hardware

        hw = hardware.detect()
        vendor, gpu_name = hw.gpu_vendor, hw.gpu_name
    plan = plan_for(vendor, gpu_name)
    root().mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root()).free / 2 ** 30
    if free < NEED_GB:
        raise EngineError(f"AI pictures need about {NEED_GB:.0f} GB free (for PyTorch and the model); "
                          f"there's {free:.1f} GB free on {root().anchor or root()}.")
    stop()
    marker().unlink(missing_ok=True)
    log(f"Setting up AI pictures in {root()}: {plan.label}, then diffusers. The picture model (about 7 GB) "
        "downloads with the first picture.")
    uv = _uv(progress, fetch)
    env = uv_env()
    progress("Setting up AI pictures: getting Python", None)
    if env_dir().exists():
        shutil.rmtree(env_dir(), ignore_errors=True)
    run([uv, "venv", "--python", PYTHON, "--python-preference", "only-managed", env_dir()], log, check, env,
        INSTALL_TIMEOUT)
    progress(f"Setting up AI pictures: installing {plan.label} (1-3 GB)", None)
    cmd = [uv, "pip", "install", "--python", python_exe(), *plan.packages]
    if plan.index_url:
        cmd += ["--index-url", plan.index_url]  # only PyTorch's index: it has torch's own dependencies too
    run(cmd, log, check, env, INSTALL_TIMEOUT)
    progress("Setting up AI pictures: installing diffusers", None)
    run([uv, "pip", "install", "--python", python_exe(), *COMMON], log, check, env, INSTALL_TIMEOUT)
    shutil.rmtree(root() / "cache", ignore_errors=True)  # the downloaded packages: several GB, not needed now
    write_worker()
    progress("Setting up AI pictures: checking it works", None)
    w = Worker(log=log, stub=stub)
    try:
        info = w.ping()
    finally:
        w.stop()
    if plan.device == "dml" and info.get("device") != "dml":
        log("DirectML didn't start, so pictures would be made on the processor (slow): "
            "updating the graphics driver usually fixes that; then set AI pictures up again.")
    data = {"version": VERSION, "device": info.get("device", plan.device),
            "device_name": info.get("device_name", ""), "torch": info.get("torch", ""),
            "diffusers": info.get("diffusers", ""), "made": time.strftime("%Y-%m-%d %H:%M")}
    if with_model:
        progress("Setting up AI pictures: drawing a test picture (the first time, the model downloads)", None)
        data |= test_picture(log=log, progress=progress, check=check, stub=stub)
        log(f"Test picture drawn in {data['test_seconds']:.0f} s (including loading the model).")
    marker().write_text(json.dumps(data), encoding="utf-8")
    log(f"AI pictures are {describe()}.")
    progress("Setting up AI pictures", 1.0)
    return status()


MODEL_GB = {"stabilityai/sdxl-turbo": 6.9, "black-forest-labs/FLUX.1-schnell": 33.7}


def model_folder(cache: Path, repo: str) -> Path:
    """Where Hugging Face keeps `repo` under `cache`."""
    return Path(cache) / ("models--" + repo.replace("/", "--"))


def folder_gb(folder: Path) -> float:
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size  # blobs, not the links to them
            except OSError:
                pass
    return total / 2 ** 30


def download_watch(cache: Path, repo: str, label: str, progress: Progress, every: float = 3.0):
    """A `waiting` callback that shows the model's download growing (only while it is growing)."""
    folder = model_folder(cache, repo)
    expect = MODEL_GB.get(repo, 7.0)
    state = {"next": 0.0, "first": None, "last": None}

    def waiting() -> None:
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + every
        gb = folder_gb(folder)
        if state["first"] is None:
            state["first"] = gb
        if gb > state["first"] + 0.01 and gb != state["last"]:
            progress(f"{label}: downloading the model ({gb:.1f} of about {expect:.0f} GB)",
                     min(0.99, gb / expect))
        state["last"] = gb

    return waiting


def test_picture(*, log: Log, progress: Progress, check=None, stub: bool = False, local_only: bool = False) -> dict:
    """Load SDXL-Turbo (downloading it the first time) and draw one small picture: proves the card works
    and leaves the model ready for the first video."""
    from ..settings import models_dir

    repo = "stabilityai/sdxl-turbo"
    cache = models_dir() / "diffusers"
    dest = root() / "test-picture.png"
    w = Worker(log=log, stub=stub)
    started = time.monotonic()
    try:
        w.generate(repo, "a lighthouse on a cliff at sunset, dramatic sky, photo", "", 7, dest, width=512,
                   height=512, steps=1, guidance=0.0, cache=cache, local_only=local_only, check=check,
                   waiting=download_watch(cache, repo, "Setting up AI pictures", progress))
    finally:
        w.stop()
    return {"test_picture": str(dest), "test_seconds": round(time.monotonic() - started, 1)}


def remove() -> None:
    """Delete the engine (not the downloaded model, which lives in the models folder)."""
    stop()
    shutil.rmtree(root(), ignore_errors=True)


# ---------------------------------------------------------------- the worker

WORKER = r'''"""ClipMonster's picture worker: one JSON request a line on stdin, one JSON answer a line on stdout."""
import json, os, struct, sys, threading, time, traceback, zlib

if sys.platform == "win32":  # ClipMonster.exe's own DLL folder must not be searched for PyTorch's DLLs
    try:
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    except Exception:
        pass

OUT = sys.stdout
sys.stdout = sys.stderr  # libraries' prints must not mix with the answers
_lock = threading.Lock()
_busy = [False]


def send(obj):
    with _lock:
        OUT.write(json.dumps(obj) + "\n")
        OUT.flush()


def heartbeat():
    while True:
        time.sleep(15)
        if _busy[0]:
            send({"alive": True})


threading.Thread(target=heartbeat, daemon=True).start()


def device():
    import torch
    if torch.cuda.is_available():
        return "cuda", torch.cuda.get_device_name(0), "cuda"
    try:
        import torch_directml
        return "dml", torch_directml.device_name(torch_directml.default_device()), torch_directml.device()
    except Exception:
        pass
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps", "Apple GPU", "mps"
    return "cpu", "processor", "cpu"


def stub_png(path, w, h, seed):
    """A plain gradient picture without any model (tests)."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            rows += bytes(((x * 255 // max(1, w - 1) + seed) % 256, y * 255 // max(1, h - 1), 128))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


pipe = None
pipe_key = None


def load(repo, cache, local_only, full):
    """The pipeline for `repo`, kept between pictures. `full`: float32 (DirectML's half precision
    sometimes draws black pictures)."""
    global pipe, pipe_key
    key = (repo, full)
    if pipe is not None and pipe_key == key:
        return pipe
    import gc
    import torch
    pipe = None
    gc.collect()
    kind, _, dev = device()
    half = kind in ("cuda", "dml", "mps") and not full
    dtype = torch.float16 if half else torch.float32
    if "flux" in repo.lower():
        from diffusers import FluxPipeline
        p = FluxPipeline.from_pretrained(repo, torch_dtype=torch.bfloat16, cache_dir=cache,
                                         local_files_only=local_only)
        if kind == "cuda" and torch.cuda.get_device_properties(0).total_memory < 22 * 2 ** 30:
            p.enable_model_cpu_offload()  # 12B parameters: on a smaller card the parts take turns on it
        else:
            p = p.to(dev)
    else:
        from diffusers import AutoPipelineForText2Image
        try:  # the half-precision files, also for full precision (converted as they load: no second download)
            p = AutoPipelineForText2Image.from_pretrained(repo, torch_dtype=dtype, cache_dir=cache,
                                                          local_files_only=local_only, variant="fp16")
        except (OSError, ValueError):  # a model without them
            p = AutoPipelineForText2Image.from_pretrained(repo, torch_dtype=dtype, cache_dir=cache,
                                                          local_files_only=local_only)
        p = p.to(dev)
    try:
        p.set_progress_bar_config(disable=True)
    except Exception:
        pass
    pipe, pipe_key = p, key
    return p


def is_black(image):
    lo, hi = zip(*image.convert("RGB").getextrema())
    return max(hi) < 8


full_precision = False

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    cmd = req.get("cmd")
    _busy[0] = True
    try:
        if cmd == "quit":
            send({"ok": True})
            break
        if cmd == "ping":
            info = {"ok": True, "python": sys.version.split()[0]}
            if req.get("stub"):
                info.update(device="stub", device_name="stub")
            else:
                import diffusers, torch
                kind, name, _ = device()
                info.update(torch=torch.__version__, diffusers=diffusers.__version__, device=kind, device_name=name)
            send(info)
        elif cmd == "gen":
            if req.get("stub"):
                if "fail" in req.get("prompt", ""):
                    raise RuntimeError("stub failure")
                if "crash" in req.get("prompt", ""):
                    sys.stderr.write("stub crash\n")
                    sys.stderr.flush()
                    os._exit(3)
                stub_png(req["out"], max(8, int(req["width"]) // 8), max(8, int(req["height"]) // 8),
                         int(req["seed"]) % 256)
                send({"ok": True, "path": req["out"], "seconds": 0.0})
                continue
            import torch
            steps = int(req["steps"])
            started = time.time()
            for attempt in (0, 1):
                p = load(req["repo"], req.get("cache"), bool(req.get("local_only")), full_precision)
                send({"loaded": True, "seconds": round(time.time() - started, 1)})

                def step_end(pipeline, i, t, kw):
                    send({"progress": (i + 1) / steps})
                    return kw

                args = dict(prompt=req["prompt"], num_inference_steps=steps, guidance_scale=float(req["guidance"]),
                            width=int(req["width"]), height=int(req["height"]),
                            generator=torch.Generator("cpu").manual_seed(int(req["seed"])),
                            callback_on_step_end=step_end)
                if "flux" in req["repo"].lower():
                    args["max_sequence_length"] = 256
                elif float(req["guidance"]) > 0:
                    args["negative_prompt"] = req.get("negative", "")
                image = p(**args).images[0]
                if attempt == 0 and not full_precision and "flux" not in req["repo"].lower() and is_black(image):
                    full_precision = True  # half precision drew nothing: full precision from now on
                    send({"note": "The picture came out black in half precision; using full precision."})
                    continue
                break
            image.save(req["out"])
            send({"ok": True, "path": req["out"], "seconds": round(time.time() - started, 1),
                  "full_precision": full_precision})
        else:
            send({"error": "unknown command %r" % (cmd,)})
    except Exception as exc:
        send({"error": "%s: %s" % (type(exc).__name__, exc), "trace": traceback.format_exc()[-1500:]})
    finally:
        _busy[0] = False
'''


def write_worker() -> Path:
    path = root() / "worker.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKER, encoding="utf-8")
    return path


class Worker:
    """The worker process: started on first use, kept for the rest of the video."""

    def __init__(self, python: Path | None = None, log: Log | None = None, stub: bool = False):
        self.python = python or python_exe()
        self.log = log or (lambda m: None)
        self.stub = stub
        self.proc: subprocess.Popen | None = None
        self._lines: list[str] = []
        self._err: list[str] = []
        self._err_reader: threading.Thread | None = None
        self._cv = threading.Condition()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.alive():
            return
        script = root() / "worker.py"
        try:
            current = script.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != WORKER:
            write_worker()
        env = uv_env()
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "HF_HUB_DISABLE_TELEMETRY": "1",
                    "HF_HUB_DISABLE_PROGRESS_BARS": "1", "TOKENIZERS_PARALLELISM": "false"})
        self._lines, self._err = [], []
        self.proc = subprocess.Popen([str(self.python), "-I", str(script)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", env=env, creationflags=NO_WINDOW, cwd=str(root()))
        threading.Thread(target=self._read, args=(self.proc.stdout, False), daemon=True).start()
        self._err_reader = threading.Thread(target=self._read, args=(self.proc.stderr, True), daemon=True)
        self._err_reader.start()

    def _read(self, stream, err: bool) -> None:
        for line in stream:
            with self._cv:
                if err:
                    if line.strip():
                        self._err = (self._err + [line.rstrip()])[-30:]
                else:
                    self._lines.append(line)
                    self._cv.notify_all()
        with self._cv:
            self._cv.notify_all()

    def ask(self, req: dict, quiet: float = PICTURE_TIMEOUT, limit: float = PICTURE_LIMIT,
            progress: Callable[[float], None] | None = None, check: Callable[[], None] | None = None,
            waiting: Callable[[], None] | None = None) -> dict:
        """Send one request and wait for its answer. Gives up after `quiet` seconds without any sign of life
        (a step, a heartbeat) or `limit` seconds in all. `waiting` is called about twice a second meanwhile."""
        self.start()
        assert self.proc is not None and self.proc.stdin is not None
        if self.stub:
            req = req | {"stub": True}
        try:
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise EngineError(f"The picture engine stopped ({self._why()}).") from exc
        start = time.monotonic()
        heard = start
        while True:
            if check:
                try:
                    check()
                except BaseException:
                    self.stop(kill=True)
                    raise
            line = None
            with self._cv:
                if not self._lines:
                    if not self.alive():
                        raise EngineError(f"The picture engine stopped ({self._why()}).")
                    self._cv.wait(0.5)
                if self._lines:
                    line = self._lines.pop(0)
            now = time.monotonic()
            if waiting:
                waiting()
            if line is None:
                if now - heard > quiet or now - start > limit:
                    self.stop(kill=True)
                    raise EngineError(f"The picture engine didn't answer for {now - heard:.0f} s.")
                continue
            heard = now
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "progress" in msg:
                if progress:
                    progress(float(msg["progress"]))
                continue
            if msg.get("alive") or msg.get("loaded"):
                continue
            if msg.get("note"):
                self.log(str(msg["note"]))
                continue
            if msg.get("error"):
                raise EngineError(str(msg["error"]))
            return msg

    def _why(self) -> str:
        code = self.proc.poll() if self.proc else None
        if code is not None and self._err_reader is not None:
            self._err_reader.join(2)  # its last words
        tail = " ".join(self._err[-3:])[-300:]
        return (f"exit {code}" if code is not None else "no answer") + (f": {tail}" if tail else "")

    def ping(self) -> dict:
        return self.ask({"cmd": "ping"}, quiet=300, limit=300)

    def generate(self, repo: str, prompt: str, negative: str, seed: int, dest: Path, *, width: int, height: int,
                 steps: int, guidance: float, cache: Path | None = None, local_only: bool = False,
                 progress: Callable[[float], None] | None = None, check=None,
                 waiting: Callable[[], None] | None = None) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.ask({"cmd": "gen", "repo": repo, "prompt": prompt, "negative": negative, "seed": int(seed),
                  "out": str(dest), "width": int(width), "height": int(height), "steps": int(steps),
                  "guidance": float(guidance), "cache": str(cache) if cache else None,
                  "local_only": bool(local_only)}, progress=progress, check=check, waiting=waiting)
        if not dest.is_file():
            raise EngineError("The picture engine said it made the picture, but there's no file.")
        return dest

    def stop(self, kill: bool = False) -> None:
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            if kill:
                raise OSError
            assert proc.stdin is not None
            proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            proc.stdin.flush()
            proc.wait(15)
        except (OSError, ValueError, subprocess.TimeoutExpired, AssertionError):
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass


_worker: Worker | None = None
_worker_lock = threading.Lock()


def worker(log: Log | None = None) -> Worker:
    """The shared worker (one model in the graphics card's memory, however many videos ask)."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = Worker(log=log)
        elif log is not None:
            _worker.log = log
        return _worker


def stop() -> None:
    """Close the worker, which frees the graphics memory it holds."""
    global _worker
    with _worker_lock:
        w, _worker = _worker, None
    if w is not None:
        w.stop()
