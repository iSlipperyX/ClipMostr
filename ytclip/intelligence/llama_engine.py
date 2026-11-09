"""A language model on this PC: GGUF models run by llama.cpp, sized to the PC, answers held to a JSON schema.

**What runs it.** Two ways, tried in this order:

1. **llama-server**, the program llama.cpp ships. The Windows release includes one built for Vulkan
   (packaging/build_llama_cpp.py), which runs on AMD, NVIDIA and Intel graphics alike. It's started once,
   on a random local port with a random key (so nothing else on the PC can use it), and stopped after a
   few idle minutes so the graphics memory is free for other things.
2. **llama-cpp-python** (``pip install llama-cpp-python``), in this process: its CUDA build suits NVIDIA
   cards, its Metal build Macs, the plain build the CPU.

**How big.** models.py has four tiers, 3B to 32B. Before each start, `make_plan` reads the model's shape
from its GGUF header (gguf.py), how much graphics memory is free right now (hardware.py: NVML, DXGI, and
llama.cpp's own Vulkan check), and works out with planner.py how long the context can be (up to 32,768
tokens for Qwen2.5, Mistral Nemo and Command R: an hour-long transcript in one read) and how many layers
fit on the card, the rest staying in RAM. llama-server is then given the context, and llama.cpp's own
--fit places the layers, measuring its real buffers, with ClipMonster's margin kept free; with an older
llama-server, or llama-cpp-python, the planned layer count is passed as is.

**When it fails.** Never a crash and never a stopped run, in this order:

* the model doesn't load because the card ran out of memory: again with a quarter fewer layers on it;
* that fails too, or the card itself fails while loading: on the CPU alone (with no graphics device
  opened at all, so a crashed driver can't take it down too);
* the card fails part-way through an answer (Vulkan "device lost", a CUDA error, a driver timeout): the
  details go to automation.log (diagnostics.py), and the same question is asked again on the CPU. The card
  is left alone for half an hour, then tried again.

**The answer.** llama.cpp turns the JSON schema into a grammar, so the answer always parses and always has
every field: a small model can't wander off into prose or forget a bracket. The schema, with its field
descriptions, is also written into the instructions, because a grammar alone doesn't say what each field
is for. `ask_json` is what ai.py and transcript_parser.py call. Everything raises LocalAIError with a
plain-English reason.
"""

from __future__ import annotations

import atexit
import http.client
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import diagnostics
from . import gguf, hardware, models, planner

IDLE_SECONDS = 300  # the server stops after this long without a question, freeing the graphics memory
START_TIMEOUT = 420.0  # loading a 20 GB model from a slow disk takes a while
REPLY_TIMEOUT = 600.0  # longest wait for the next piece of an answer (reading progress comes every 2,048 tokens)
CHARS_PER_TOKEN = 3.6  # a rough, slightly cautious count for English transcripts
SEED = 7  # the same transcript gives the same picks
TEMPERATURE = 0.2
GPU_REST_SECONDS = 1800  # after the graphics card fails, the CPU is used this long before trying the card again
FEWER = 0.75  # after running out of graphics memory, this share of the layers is tried
LOG_LEVEL = "4"  # llama-server's -lv: 4 includes where the layers went and how big its buffers are
ANSWER_ROOM = 3500  # tokens kept back from the context for the instructions, the schema and the answer
GIB = 1024 ** 3

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]
Check = Callable[[], None]

# What a failed llama.cpp prints about the graphics card is in diagnostics.py (whisper.cpp prints the same);
# these are the model file's own problems, which no other card or the CPU would fix.
MODEL_SIGNS = ("unknown model architecture", "invalid magic", "not a gguf", "failed to read magic",
               "error loading model hyperparameters", "error loading model vocabulary", "tensor data is not within")


class LocalAIError(RuntimeError):
    """A plain-English reason the local AI couldn't answer."""


class LocalCutOff(LocalAIError):
    """The answer ran out of room before it finished."""


class TooLong(LocalAIError):
    """The question doesn't fit in the model's context."""


class _GPUFailed(LocalAIError):
    """The graphics card failed while answering: ask again on the CPU. `problem` is a plain description,
    `detail` what went wrong as the program saw it (for automation.log)."""

    def __init__(self, problem: str, detail: str = ""):
        super().__init__(problem[0].upper() + problem[1:] + ".")
        self.problem, self.detail = problem, detail


@dataclass
class Reply:
    text: str
    model: str  # the model file's name
    backend: str  # "llama-server" | "llama-cpp-python"
    device: str  # what it ran on, as llama.cpp reported it ("" until known)
    context: int = 0
    gpu_layers: int = -1  # layers on the graphics card (-1 = not known)
    total_layers: int = 0


# ---------------------------------------------------------------- what's here


def server_binary() -> Path | None:
    """llama-server: CLIPMONSTER_LLAMA_SERVER, next to the app (llama-cpp/), or on PATH."""
    env = os.environ.get("CLIPMONSTER_LLAMA_SERVER")
    if env:
        return Path(env) if Path(env).is_file() else None
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    if getattr(sys, "frozen", False):
        from .. import bundled

        return bundled.find("llama-cpp", name)
    build = Path(__file__).resolve().parents[2] / "build"  # a source checkout's own build (build_mac.py's on a Mac)
    for here in (build / "llama-cpp" / name, build / "mac-tools" / "llama-cpp" / name):
        if here.is_file():
            return here
    found = shutil.which("llama-server")
    return Path(found) if found else None


def has_python_binding() -> bool:
    return importlib.util.find_spec("llama_cpp") is not None


def backend_name() -> str:
    """Which way llama.cpp would run here, or "" when neither is installed."""
    if server_binary() is not None:
        return "llama-server"
    if has_python_binding():
        return "llama-cpp-python"
    return ""


def missing_backend() -> str:
    return ("This copy of ClipMonster doesn't include the local AI. Update to the newest version, or "
            "install it with: pip install llama-cpp-python")


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def schema_prompt(schema: dict) -> str:
    """The schema written out for the model: a grammar decides the shape, this says what goes in it."""
    return ("Reply with one JSON object and nothing else. It must match this JSON schema, and each "
            "\"description\" says what that field is for:\n" + json.dumps(schema, separators=(",", ":")))


def as_text(content) -> str:
    """The text of a message. Local models here read text only, so images are left out."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def threads_for(speed: str) -> int:
    from .. import speed as speeds

    return speeds.threads(speed)


def classify(lines) -> str:
    """'oom', 'gpu', 'model' or '' for what a failed llama.cpp printed."""
    lines = list(lines)
    text = "\n".join(lines).lower()
    if any(sign in text for sign in MODEL_SIGNS):
        return "model"
    return diagnostics.classify(lines)


# ---------------------------------------------------------------- the graphics card's health


_gpu_failed_at = 0.0
_gpu_lock = threading.Lock()


def gpu_failed() -> None:
    """The graphics card failed: use the CPU for a while."""
    global _gpu_failed_at
    with _gpu_lock:
        _gpu_failed_at = time.time()


def gpu_resting(now: float | None = None) -> bool:
    """Whether the graphics card failed in the last half hour (then the CPU is used)."""
    return bool(_gpu_failed_at) and (now or time.time()) - _gpu_failed_at < GPU_REST_SECONDS


def gpu_back_at() -> float:
    return _gpu_failed_at + GPU_REST_SECONDS if _gpu_failed_at else 0.0


# ---------------------------------------------------------------- what llama-server can do


FLAGS = ("--fit", "--fit-target", "-ctk", "-ctv", "-dev", "--cache-ram", "-lv", "--no-webui", "--list-devices",
         "--api-key")
_flags: dict[tuple[str, int, int], frozenset[str]] = {}


def server_flags(exe: Path) -> frozenset[str]:
    """The options this llama-server knows, from its --help (read once per build): an older or custom
    llama-server is only given the options it has."""
    try:
        st = exe.stat()
    except OSError:
        return frozenset()
    key = (str(exe), st.st_size, int(st.st_mtime))
    if key not in _flags:
        from ..ffmpeg import NO_WINDOW

        try:
            run = subprocess.run([str(exe), "--help"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30, creationflags=NO_WINDOW)
            text = run.stdout + run.stderr
        except (OSError, subprocess.SubprocessError):
            text = ""
        _flags[key] = frozenset(f for f in FLAGS if re.search(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", text))
    return _flags[key]


# ---------------------------------------------------------------- the plan for one run


@dataclass
class RunPlan:
    path: Path
    name: str  # "Mistral Nemo 12B Instruct", or the file's name
    context: int
    gpu_layers: int  # planned layers on the card (llama.cpp's count); 0 = the CPU alone
    total_layers: int
    kv_type: str  # f16 | q8_0
    threads: int
    card: str = ""  # "AMD Radeon RX 9060 XT", or "" for the CPU
    device: str = ""  # llama.cpp's name for the card (Vulkan0), when known
    margin_mib: int = 1024  # graphics memory left free
    fixed_layers: bool = False  # Settings fixes the layer count
    fits: bool = True
    note: str = ""
    vram_free_gb: float | None = None
    vram_source: str = ""
    ram_free_gb: float = 0.0
    gpu_gb: float = 0.0  # expected on the card
    ram_gb: float = 0.0  # expected in RAM

    @property
    def cpu_only(self) -> bool:
        return self.gpu_layers <= 0

    def describe(self) -> str:
        p = planner.Plan(self.context, self.gpu_layers, self.total_layers, self.kv_type, 0, 0, self.fits)
        return p.describe(self.card or "the graphics card")


def context_for(s, model: models.Model | None, shape: gguf.Shape | None = None) -> int:
    """The context to aim for: Settings → AI's own number, else the model's (a custom file: its trained
    context, up to 16,384)."""
    asked = int(getattr(s, "local_context", 0) or 0)
    if asked:
        return max(2048, asked)
    if model is not None:
        return model.context
    trained = shape.n_ctx_train if shape else 0
    return min(16384, trained) if trained >= 2048 else 8192


def make_plan(s, backend: str = "", exe: Path | None = None, *, probe: bool = True, cpu: bool = False) -> RunPlan:
    """How the model in Settings → AI would run here right now. `probe` adds llama.cpp's own Vulkan check
    of free graphics memory (a second or so); Settings' status line leaves it out. `cpu` plans for the CPU
    alone (after the graphics card failed). Raises LocalAIError."""
    backend = backend or backend_name()
    hw = hardware.detect()
    model, path = models.resolve(s, hw)
    try:
        shape = models.shape_of(model, path)
    except gguf.GGUFError as exc:
        raise LocalAIError(str(exc)) from exc
    want_gpu = bool(getattr(s, "local_gpu", True)) and hw.has_gpu and not gpu_resting() and not cpu
    exe = (exe or server_binary()) if backend == "llama-server" else None
    flags = server_flags(exe) if exe is not None else frozenset()
    mem = hardware.memory_now(hw, exe if (probe and want_gpu and "--list-devices" in flags) else None)
    layers = int(getattr(s, "local_gpu_layers", -1))
    quantize = backend == "llama-server" and {"-ctk", "-ctv"} <= flags
    p = planner.plan(shape, vram_total=int(mem.vram_total_gb * GIB) if want_gpu else 0,
                     vram_free=None if mem.vram_free_gb is None else int(mem.vram_free_gb * GIB),
                     ram_free=int(mem.ram_free_gb * GIB), want_context=context_for(s, model, shape), gpu=want_gpu,
                     layers=layers if layers >= 0 else None, quantize_kv=quantize, flash=backend == "llama-server")
    name = model.name if model else path.name
    return RunPlan(path, name, p.context, p.gpu_layers, p.total_layers, p.kv_type,
                   threads_for(getattr(s, "speed", "balanced")), hw.gpu_name if p.on_gpu else "", mem.device,
                   max(1024, planner.margin_bytes(int(mem.vram_total_gb * GIB)) // planner.MIB),
                   layers >= 0 and p.on_gpu, p.fits, p.note, mem.vram_free_gb, mem.source, mem.ram_free_gb,
                   p.gpu_bytes / GIB, p.ram_bytes / GIB)


def _too_big(plan: RunPlan) -> LocalAIError:
    return LocalAIError(f"{plan.name} needs more memory than this PC has free (about {plan.gpu_gb + plan.ram_gb:.0f} "
                        f"GB between the graphics card and RAM, with {plan.ram_free_gb:.0f} GB of RAM free). Close "
                        "other programs, or choose a smaller model in Settings → AI.")


# ---------------------------------------------------------------- llama-server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerBackend:
    """llama-server in the background, asked over HTTP on 127.0.0.1 only, with a key of its own."""

    name = "llama-server"

    def __init__(self, exe: Path, plan: RunPlan | Callable[..., RunPlan], log: Log):
        self.exe, self.log = exe, log
        self._replan = plan if callable(plan) else (lambda cpu=False: plan)  # cpu=True: the plan for the CPU alone
        self.plan: RunPlan = self._replan()
        self.flags = server_flags(exe)
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.said: deque[str] = deque(maxlen=800)
        self.device = ""
        self.mode = "planned"  # planned | fewer | cpu
        self.layers_on_gpu = -1  # what llama.cpp reported
        self.fewer_layers = 0
        self.key = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        self._idle: threading.Timer | None = None

    # what the rest of the engine reads
    @property
    def model(self) -> Path:
        return self.plan.path

    @property
    def context(self) -> int:
        return self.plan.context

    @property
    def gpu(self) -> bool:
        return self.mode != "cpu" and not self.plan.cpu_only

    def command(self) -> list[str]:
        p, f = self.plan, self.flags
        cmd = [str(self.exe), "-m", str(p.path), "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(p.context), "-t", str(max(1, p.threads)), "-np", "1"]
        if "-lv" in f:
            cmd += ["-lv", LOG_LEVEL]
        if "--cache-ram" in f:
            cmd += ["--cache-ram", "0"]  # each transcript is new: no point keeping old ones (it'd hold up to 8 GB)
        if "--no-webui" in f:
            cmd += ["--no-webui"]
        if p.kv_type != "f16" and {"-ctk", "-ctv"} <= f:
            cmd += ["-ctk", p.kv_type, "-ctv", p.kv_type]
        fit_off = ["--fit", "off"] if "--fit" in f else []
        if not self.gpu:
            cmd += ["-ngl", "0"] + (["-dev", "none"] if "-dev" in f else []) + fit_off
        elif self.mode == "fewer":
            cmd += ["-ngl", str(self.fewer_layers)] + fit_off
        elif p.fixed_layers or "--fit" not in f:
            cmd += ["-ngl", str(p.gpu_layers)] + fit_off
        else:  # llama.cpp measures its own buffers and places the layers, leaving our margin free
            cmd += ["--fit", "on"] + (["--fit-target", str(p.margin_mib)] if "--fit-target" in f else [])
        return cmd

    def environment(self) -> dict:
        env = dict(os.environ)
        env["LLAMA_API_KEY"] = self.key
        if not self.gpu:  # the CPU alone: no graphics device is even opened
            env["GGML_VK_VISIBLE_DEVICES"] = ""
            env["CUDA_VISIBLE_DEVICES"] = ""
        return env

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, check: Check | None = None) -> None:
        """Start llama-server, stepping down (fewer layers, then the CPU) until the model loads."""
        self.plan = self._replan()  # free memory changes: size it again each time it starts
        self.mode = "cpu" if self.plan.cpu_only else "planned"
        while True:
            if not self.plan.fits:
                raise _too_big(self.plan)
            try:
                self._start(check)
                return
            except LocalAIError as exc:
                kind = classify(self.said)
                if not self.gpu or kind == "model" or isinstance(exc, _Cancelled):
                    raise
                said = list(self.said)
                if kind == "oom" and self.mode == "planned":
                    known = self.layers_on_gpu if self.layers_on_gpu > 0 else self.plan.gpu_layers
                    self.fewer_layers = max(1, int(known * FEWER))
                    if self.fewer_layers >= max(2, self.plan.total_layers // 8):
                        self.mode = "fewer"
                        self._diagnose("the graphics card ran out of memory loading the model", said,
                                       f"Trying again with {self.fewer_layers} layers on it.")
                        continue
                elif kind == "gpu":
                    gpu_failed()  # the card or its driver failed: leave it alone for a while
                self.mode = "cpu"
                self.plan = self._replan(cpu=True)
                problem = ("the graphics card ran out of memory loading the model" if kind == "oom" else
                           "the model didn't load on the graphics card" + (f" ({exc})" if kind == "" else ""))
                self._diagnose(problem, said, "Running it on the CPU instead.")

    def _start(self, check: Check | None) -> None:
        from ..ffmpeg import NO_WINDOW

        self.port = _free_port()
        self.said.clear()
        self.layers_on_gpu = -1
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                     env=self.environment(), creationflags=NO_WINDOW)
        threading.Thread(target=self._read_log, args=(self.proc,), daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                time.sleep(0.2)  # let the log reader catch the last lines
                tail = " ".join(line for line in list(self.said)[-4:] if line) or f"exit code {self.proc.returncode}"
                self.proc = None
                raise LocalAIError(f"The local AI didn't start: {tail[:300]}")
            if check:
                try:
                    check()
                except BaseException as exc:
                    self.stop()  # cancelled while the model loaded: don't leave it half started
                    raise _Cancelled(str(exc)) from exc
            try:
                with urllib.request.urlopen(self._url("/health"), timeout=2) as r:
                    if r.status == 200:
                        self._loaded()
                        return
            except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(0.5)
        self.stop()
        raise LocalAIError("The local AI took too long to load its model.")

    def _loaded(self) -> None:
        lines = list(self.said)
        self.layers_on_gpu, _ = offloaded(lines)
        if not self.gpu:
            self.layers_on_gpu = 0
        name = self.plan.card or device_from_log(lines, True)
        self.device = (name if self.layers_on_gpu != 0 and self.gpu else "the CPU")
        if self.gpu and 0 < self.layers_on_gpu < self.plan.total_layers:
            self.device = f"{name} ({self.layers_on_gpu} of {self.plan.total_layers} layers) and the CPU"

    def _read_log(self, proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            self.said.append(line.rstrip())

    def stop(self) -> None:
        if self._idle:
            self._idle.cancel()
            self._idle = None
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _touch(self) -> None:
        if self._idle:
            self._idle.cancel()
        self._idle = threading.Timer(IDLE_SECONDS, self.stop)
        self._idle.daemon = True
        self._idle.start()

    def _diagnose(self, problem: str, said: list[str], action: str) -> None:
        what = f"{self.plan.name} on {self.plan.card or 'the graphics card'}"
        diagnostics.gpu_fallback("Local AI", f"{problem} ({what})", said, action, self.log)

    def chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None,
             progress: Progress | None = None) -> str:
        with self._lock:
            if self._idle:
                self._idle.cancel()
            try:
                for attempt in (1, 2):
                    if not self.running():
                        self.start(check)
                    try:
                        return self._chat(system, user, schema, max_tokens, check, progress)
                    except _GPUFailed as exc:
                        if attempt == 2 or not self.gpu:
                            raise LocalAIError(str(exc)) from exc
                        said = list(self.said) + ([f"ClipMonster: {exc.detail}"] if exc.detail else [])
                        self.stop()
                        gpu_failed()
                        self.mode = "cpu"
                        self._diagnose(exc.problem, said,
                                       "Asking again on the CPU; the card is tried again in half an hour.")
                raise LocalAIError("The local AI didn't answer.")
            finally:
                self._touch()

    def _chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None,
              progress: Progress | None) -> str:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": TEMPERATURE, "seed": SEED, "max_tokens": max_tokens, "stream": True,
            "return_progress": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema,
                                                                      "strict": True}},
        }
        req = urllib.request.Request(self._url("/v1/chat/completions"), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        text: list[str] = []
        finish = ""
        timings: dict = {}
        try:
            with urllib.request.urlopen(req, timeout=REPLY_TIMEOUT) as r:
                for n, raw in enumerate(r):
                    if check and n % 10 == 0:
                        check()
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if "error" in chunk:
                        raise self._failed(str(chunk["error"]))
                    read = chunk.get("prompt_progress")
                    if read and progress and read.get("total"):
                        progress("The local AI is reading the transcript",
                                 min(1.0, (read.get("processed", 0) or 0) / read["total"]))
                    timings = chunk.get("timings") or timings
                    for choice in chunk.get("choices") or []:
                        text.append(str((choice.get("delta") or {}).get("content") or ""))
                        finish = choice.get("finish_reason") or finish
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            low = detail.lower()
            if "context" in low and ("exceed" in low or "too long" in low or "larger than" in low):
                raise TooLong("The transcript is too long for the local AI's memory.") from exc
            raise self._failed(f"answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise self._failed(f"stopped part-way ({exc})") from exc
        if timings:
            self._speed(timings)
        if finish == "length":
            raise LocalCutOff("The local AI's answer was cut off before it finished.")
        return "".join(text)

    def _failed(self, what: str) -> LocalAIError:
        """Why an answer failed: a graphics card failure (asked again on the CPU) or anything else."""
        time.sleep(0.3)  # the log reader catches the server's last words
        crashed = self.proc is not None and self.proc.poll() is not None
        kind = classify(list(self.said)[-60:])
        if not crashed and self.proc is not None and "stopped part-way" in what:
            self.stop()  # it hung: start it afresh next time
            crashed = True
        elif crashed:
            self.proc = None
        if self.gpu and (kind in ("gpu", "oom") or crashed):
            problem = {"gpu": "the graphics card failed while answering",
                       "oom": "the graphics card ran out of memory while answering"}.get(
                kind, "llama-server stopped while answering on the graphics card")
            return _GPUFailed(problem, f"the answer {what[:300]}")
        return LocalAIError(f"The local AI {what[:300]}")

    def _speed(self, t: dict) -> None:
        try:
            read, wrote = int(t.get("prompt_n") or 0), int(t.get("predicted_n") or 0)
            line = (f"The local AI read {read:,} tokens at {float(t.get('prompt_per_second') or 0):,.0f} a second "
                    f"and wrote {wrote:,} at {float(t.get('predicted_per_second') or 0):,.1f} a second.")
        except (TypeError, ValueError):
            return
        self.log(line)


class _Cancelled(LocalAIError):
    """The run was stopped while the model loaded."""


def offloaded(lines: list[str]) -> tuple[int, int]:
    """(layers on the graphics card, all layers) from llama.cpp's 'offloaded 29/29 layers to GPU', or (-1, 0)."""
    m = None
    for m in re.finditer(r"offloaded (\d+)/(\d+) layers to GPU", "\n".join(lines)):
        pass
    return (int(m[1]), int(m[2])) if m else (-1, 0)


def device_from_log(lines: list[str], gpu: bool) -> str:
    """What llama.cpp says it's running on: a graphics card's name, or the CPU."""
    text = "\n".join(lines)
    if gpu:
        for pattern in (r"ggml_vulkan: \d+ = ([^|(\n]+)", r"Device \d+: ([^,\n]+), compute capability",
                        r"ggml_metal_init: found device: ([^\n]+)", r"using device \w+ \(([^)]+)\)"):
            m = re.search(pattern, text)
            if m:
                return m[1].strip()
        layers, _ = offloaded(lines)
        if layers > 0:
            return "the graphics card"
    return "the CPU"


# ---------------------------------------------------------------- llama-cpp-python


class PythonBackend:
    """llama-cpp-python, loaded into this process. Sized by the plan; a load that runs out of graphics
    memory is tried again with fewer layers, then on the CPU. (A card that fails part-way through an
    answer takes this process with it, which is why llama-server comes first.)"""

    name = "llama-cpp-python"

    def __init__(self, plan: RunPlan | Callable[..., RunPlan], log: Log):
        self._replan = plan if callable(plan) else (lambda cpu=False: plan)
        self.plan: RunPlan = self._replan()
        self.log = log
        self.llm = None
        self.device = ""
        self._lock = threading.Lock()

    @property
    def model(self) -> Path:
        return self.plan.path

    @property
    def context(self) -> int:
        return self.plan.context

    def running(self) -> bool:
        return self.llm is not None

    def start(self, check: Check | None = None) -> None:
        try:
            from llama_cpp import Llama, llama_supports_gpu_offload
        except ImportError as exc:
            raise LocalAIError(missing_backend()) from exc
        try:
            offload = bool(llama_supports_gpu_offload())
        except Exception:
            offload = False
        self.plan = self._replan()
        if not self.plan.fits:
            raise _too_big(self.plan)
        layers = self.plan.gpu_layers if offload else 0
        tries = [layers] + ([max(1, int(layers * FEWER)), 0] if layers > 0 else [])
        last: Exception | None = None
        for n in dict.fromkeys(tries):
            try:
                self.llm = Llama(model_path=str(self.plan.path), n_ctx=self.plan.context,
                                 n_threads=max(1, self.plan.threads), n_gpu_layers=n, seed=SEED, verbose=False)
            except Exception as exc:
                last = exc
                if n:
                    diagnostics.gpu_fallback("Local AI", f"{self.plan.name} didn't load with {n} layers on the "
                                             "graphics card", [str(exc)], "Trying with fewer.", self.log)
                continue
            self.device = (f"{self.plan.card or 'the graphics card'}" if n >= self.plan.total_layers else
                           f"{self.plan.card or 'the graphics card'} ({n} of {self.plan.total_layers} layers) and "
                           "the CPU") if n else "the CPU"
            return
        raise LocalAIError(f"The local AI couldn't load {self.plan.path.name}: {last}")

    def stop(self) -> None:
        self.llm = None

    def chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None,
             progress: Progress | None = None) -> str:
        from llama_cpp import LlamaGrammar

        with self._lock:
            if self.llm is None:
                self.start(check)
            grammar = LlamaGrammar.from_json_schema(json.dumps(schema), verbose=False)
            try:
                stream = self.llm.create_chat_completion(
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    grammar=grammar, temperature=TEMPERATURE, max_tokens=max_tokens, stream=True)
                text, finish = [], ""
                for n, chunk in enumerate(stream):
                    if check and n % 20 == 0:
                        check()
                    for choice in chunk.get("choices") or []:
                        text.append(str((choice.get("delta") or {}).get("content") or ""))
                        finish = choice.get("finish_reason") or finish
            except ValueError as exc:  # llama-cpp-python's "Requested tokens exceed context window"
                if "context" in str(exc).lower():
                    raise TooLong("The transcript is too long for the local AI's memory.") from exc
                raise LocalAIError(str(exc)) from exc
            if finish == "length":
                raise LocalCutOff("The local AI's answer was cut off before it finished.")
            return "".join(text)


# ---------------------------------------------------------------- one engine per run


_engine = None
_engine_key: tuple | None = None
_engine_lock = threading.Lock()


def _settings_key(s, path: Path) -> tuple:
    return (str(path), int(getattr(s, "local_context", 0) or 0), bool(getattr(s, "local_gpu", True)),
            str(getattr(s, "speed", "balanced")), int(getattr(s, "local_gpu_layers", -1)))


def engine(s, log: Log | None = None, progress: Progress | None = None, check: Check | None = None):
    """The running local model for these settings, downloading the model first when needed."""
    global _engine, _engine_key
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    hw = hardware.detect()
    model, path = models.resolve(s, hw)
    if model is None:
        if not path.name or not path.is_file():
            raise LocalAIError("Choose your GGUF model file in Settings → AI (Local model file).")
        if not models.is_gguf(path):
            raise LocalAIError(f"{path.name} isn't a GGUF model file.")
    elif not models.installed(path):
        if not models.free_space_ok(model, path):
            raise LocalAIError(f"Not enough free space for the local AI model ({model.size_gb:.1f} GB).")
        log(f"Downloading the local AI model {model.name} ({model.size_gb:.1f} GB, once)…")
        try:
            path = models.download(model, progress)
        except models.DownloadError as exc:
            raise LocalAIError(str(exc)) from exc
    key = _settings_key(s, path)
    with _engine_lock:
        if _engine is not None and _engine_key == key:
            _engine.log = log
            return _engine
        if _engine is not None:
            _engine.stop()
        exe = server_binary()
        if exe is not None:
            _engine = ServerBackend(exe, lambda cpu=False: make_plan(s, "llama-server", exe, cpu=cpu), log)
        elif has_python_binding():
            _engine = PythonBackend(lambda cpu=False: make_plan(s, "llama-cpp-python", probe=False, cpu=cpu), log)
        else:
            raise LocalAIError(missing_backend())
        _engine_key = key
        return _engine


def running_engine(s):
    """The engine already made for these settings, or None (nothing is started or downloaded)."""
    with _engine_lock:
        if _engine is None or _engine_key is None:
            return None
        model, path = models.resolve(s, hardware.detect())
        return _engine if _engine_key == _settings_key(s, path) else None


def shutdown() -> None:
    global _engine, _engine_key
    with _engine_lock:
        if _engine is not None:
            _engine.stop()
        _engine, _engine_key = None, None


atexit.register(shutdown)


def _holds_gpu(eng) -> bool:
    if not eng.running():
        return False
    if isinstance(eng, ServerBackend):
        return eng.gpu and eng.layers_on_gpu != 0
    return bool(eng.device) and eng.device != "the CPU"


def free_graphics_memory() -> bool:
    """Stop the local AI when its model is holding graphics memory that something else needs (Whisper on
    the same card ran out of it). True when it was stopped; it starts again by itself the next time it's
    asked something. An answer in progress is never cut short."""
    with _engine_lock:
        eng = _engine
        if eng is None or not _holds_gpu(eng):
            return False
        if not eng._lock.acquire(blocking=False):  # it's answering right now
            return False
        try:
            eng.stop()
        finally:
            eng._lock.release()
        return True


def ask_json(s, system: str, content, schema: dict, *, max_tokens: int = 4000, min_tokens: int | None = None,
             log: Log | None = None, check: Check | None = None, progress: Progress | None = None,
             prompt_schema: dict | None = None) -> Reply:
    """Ask the local model for JSON matching `schema` (its grammar). `max_tokens` is the longest answer;
    when the question leaves less room than that in the model's memory, the answer gets what's left as
    long as that's at least `min_tokens` (default: all of max_tokens), else it raises TooLong.
    `prompt_schema`, when given, is the copy written into the instructions (the same shape, put in words a
    model reads more easily). Raises LocalAIError (TooLong, LocalCutOff)."""
    log = log or (lambda m: None)
    eng = engine(s, log, progress, check)
    user = as_text(content)
    full_system = f"{system}\n\n{schema_prompt(prompt_schema or schema)}"
    first = not eng.running()
    started = time.monotonic()
    if first:
        eng.start(check)  # sizes the context to what's free now
        plan = eng.plan
        layers = getattr(eng, "layers_on_gpu", -1)
        where = eng.device or plan.describe()
        log(f"The local AI is running on {where}: {plan.name}, {plan.context:,}-token memory "
            f"({eng.name}, loaded in {time.monotonic() - started:.0f} s).")
        if plan.note:
            log(f"The local AI: {plan.note}.")
        if layers >= 0 and plan.gpu_layers > 0 and layers != plan.gpu_layers and getattr(eng, "gpu", False):
            log(f"llama.cpp put {layers} of {plan.total_layers} layers on the graphics card "
                f"(ClipMonster's estimate was {plan.gpu_layers}).")
    question = estimate_tokens(full_system) + estimate_tokens(user)
    left = eng.context - question
    if left < min(max_tokens, max_tokens if min_tokens is None else min_tokens):
        raise TooLong(f"The transcript is too long for the local AI's memory ({question + max_tokens:,} of "
                      f"{eng.context:,} tokens).")
    text = eng.chat(full_system, user, schema, min(max_tokens, left), check, progress)
    return Reply(text, eng.model.name, eng.name, eng.device, eng.context,
                 getattr(eng, "layers_on_gpu", -1), eng.plan.total_layers)


def room(s) -> int:
    """Tokens of transcript one question can carry, after the instructions and the answer."""
    eng = running_engine(s)
    if eng is not None and eng.running():
        return max(1000, eng.context - ANSWER_ROOM)
    try:
        plan = make_plan(s, probe=False)
    except LocalAIError:
        model, _ = models.resolve(s, hardware.detect())
        return max(1000, context_for(s, model) - ANSWER_ROOM)
    return max(1000, plan.context - ANSWER_ROOM)


def status(s) -> str:
    """One line for Settings → AI and the health check: which model, on what, and how it fits ("Mistral
    Nemo 12B Instruct (downloaded): all 41 layers on AMD Radeon RX 9060 XT, 32,768-token memory, through
    llama-server.")."""
    hw = hardware.detect()
    model, path = models.resolve(s, hw)
    how = backend_name()
    if not how:
        return missing_backend()
    if model is None:
        have = "ready" if path.is_file() else "file not found"
        head = f"{path.name or 'No model file chosen'} ({have})"
    else:
        have = "downloaded" if models.installed(path) else f"downloads once, {model.size_gb:.1f} GB"
        head = f"{model.name} ({have})"
    try:
        plan = make_plan(s, how, server_binary() if how == "llama-server" else None, probe=False)
    except LocalAIError as exc:
        return f"{head}: {exc}"
    desc = plan.describe()
    if plan.gpu_layers <= 0:
        desc = desc.replace("on the CPU", f"on the CPU ({hw.cpu_threads} threads)", 1)
    line = f"{head}: {desc}, through {how}."
    if not plan.fits:
        line += f" It needs more memory than is free now ({plan.note})."
    elif plan.note:
        line += f" ({plan.note[0].upper() + plan.note[1:]}.)"
    if gpu_resting():
        line += (" The graphics card failed earlier, so the CPU is used until "
                 f"{time.strftime('%H:%M', time.localtime(gpu_back_at()))}.")
    better = models.better_choice(s, hw)
    if better is not None:
        line += (f" {better.name} suits this PC better: run `ytclip local-ai download`, or choose it in "
                 "Settings → AI.")
    if model is not None and not model.commercial:
        line += f" {model.name}'s licence ({model.licence}) doesn't allow earning money from what it makes."
    return line
