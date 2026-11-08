"""A language model on this PC: GGUF models run by llama.cpp, with answers held to a JSON schema.

Two ways to run llama.cpp, tried in this order:

1. **llama-server**, the program llama.cpp ships. The Windows release of ClipMonster includes one built
   for Vulkan (packaging/build_llama_cpp.py), which runs on AMD, NVIDIA and Intel graphics alike and
   falls back to every CPU thread when there's no usable card. It's started once, on a random local
   port, and stopped after a few idle minutes so the graphics memory is free for other things.
2. **llama-cpp-python** (``pip install llama-cpp-python``), in this process. Its CUDA build suits NVIDIA
   cards, its Metal build Macs; the plain build uses the CPU with OpenMP.

Either way the answer is constrained by a grammar made from the JSON schema (llama.cpp turns the schema
into a GBNF grammar), so it always parses and always has every required field: a small model can't
wander off into prose or forget a bracket. The schema, with its field descriptions, is also written
into the instructions, because a grammar alone doesn't tell the model what each field is for.

`ask_json` is what ai.py calls. Everything raises LocalAIError with a plain-English reason.
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
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

from . import hardware, models

IDLE_SECONDS = 300  # the server stops after this long without a question, freeing the graphics memory
START_TIMEOUT = 240.0  # loading a 7B model from a slow disk can take a while
REPLY_TIMEOUT = 600.0
CHARS_PER_TOKEN = 3.6  # a rough, slightly cautious count for English transcripts
SEED = 7  # the same transcript gives the same picks
TEMPERATURE = 0.2

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]
Check = Callable[[], None]


class LocalAIError(RuntimeError):
    """A plain-English reason the local AI couldn't answer."""


class LocalCutOff(LocalAIError):
    """The answer ran out of room before it finished."""


class TooLong(LocalAIError):
    """The question doesn't fit in the model's context."""


@dataclass
class Reply:
    text: str
    model: str  # the model file's name
    backend: str  # "llama-server" | "llama-cpp-python"
    device: str  # what it ran on, as llama.cpp reported it ("" until known)


# ---------------------------------------------------------------- what's here


def server_binary() -> Path | None:
    """llama-server: CLIPMONSTER_LLAMA_SERVER, next to the app (llama-cpp/), or on PATH."""
    env = os.environ.get("CLIPMONSTER_LLAMA_SERVER")
    if env:
        return Path(env) if Path(env).is_file() else None
    name = "llama-server.exe" if sys.platform == "win32" else "llama-server"
    if getattr(sys, "frozen", False):
        found = Path(sys.executable).resolve().parent / "llama-cpp" / name
        return found if found.is_file() else None
    here = Path(__file__).resolve().parents[2] / "build" / "llama-cpp" / name  # a source checkout's own build
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


# ---------------------------------------------------------------- llama-server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerBackend:
    """llama-server in the background, asked over HTTP on 127.0.0.1 only."""

    name = "llama-server"

    def __init__(self, exe: Path, model: Path, context: int, threads: int, gpu: bool, log: Log):
        self.exe, self.model, self.context, self.threads, self.gpu, self.log = exe, model, context, threads, gpu, log
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.said: deque[str] = deque(maxlen=400)
        self.device = ""
        self._lock = threading.Lock()
        self._idle: threading.Timer | None = None

    def command(self) -> list[str]:
        return [str(self.exe), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                "-c", str(self.context), "-t", str(max(1, self.threads)), "-ngl", "999" if self.gpu else "0",
                "-np", "1"]

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, check: Check | None = None) -> None:
        from ..ffmpeg import NO_WINDOW

        self.port = _free_port()
        self.said.clear()
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                     creationflags=NO_WINDOW)
        threading.Thread(target=self._read_log, daemon=True).start()
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                tail = " ".join(line for line in list(self.said)[-4:] if line) or f"exit code {self.proc.returncode}"
                self.proc = None
                raise LocalAIError(f"The local AI didn't start: {tail[:300]}")
            if check:
                try:
                    check()
                except BaseException:
                    self.stop()  # cancelled while the model loaded: don't leave it half started
                    raise
            try:
                with urllib.request.urlopen(self._url("/health"), timeout=2) as r:
                    if r.status == 200:
                        self.device = self.device or device_from_log(list(self.said), self.gpu)
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        self.stop()
        raise LocalAIError("The local AI took too long to load its model.")

    def _read_log(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
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

    def chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None) -> str:
        with self._lock:
            if not self.running():
                try:
                    self.start(check)
                except LocalAIError:
                    if not self.gpu:
                        raise
                    self.log("The local AI couldn't use the graphics card, so it runs on the CPU this time.")
                    self.gpu = False
                    self.start(check)
            if self._idle:
                self._idle.cancel()
            try:
                return self._chat(system, user, schema, max_tokens, check)
            finally:
                self._touch()

    def _chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None) -> str:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": TEMPERATURE, "seed": SEED, "max_tokens": max_tokens, "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema,
                                                                      "strict": True}},
        }
        req = urllib.request.Request(self._url("/v1/chat/completions"), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        text: list[str] = []
        finish = ""
        try:
            with urllib.request.urlopen(req, timeout=REPLY_TIMEOUT) as r:
                for n, raw in enumerate(r):
                    if check and n % 20 == 0:
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
                    for choice in chunk.get("choices") or []:
                        text.append(str((choice.get("delta") or {}).get("content") or ""))
                        finish = choice.get("finish_reason") or finish
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if "context" in detail.lower() and ("exceed" in detail.lower() or "too long" in detail.lower()):
                raise TooLong("The transcript is too long for the local AI's memory.") from exc
            raise LocalAIError(f"The local AI answered {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            self.stop()  # it crashed or hung: start it afresh next time
            raise LocalAIError(f"Lost the local AI part-way through ({exc}).") from exc
        if finish == "length":
            raise LocalCutOff("The local AI's answer was cut off before it finished.")
        return "".join(text)


def device_from_log(lines: list[str], gpu: bool) -> str:
    """What llama.cpp says it's running on: a graphics card's name, or the CPU."""
    import re

    text = "\n".join(lines)
    if gpu:
        for pattern in (r"ggml_vulkan: \d+ = ([^|(\n]+)", r"Device \d+: ([^,\n]+), compute capability",
                        r"ggml_metal_init: found device: ([^\n]+)", r"using device \w+ \(([^)]+)\)"):
            m = re.search(pattern, text)
            if m:
                return m[1].strip()
        m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", text)
        if m and int(m[1]) > 0:
            return "the graphics card"
    return "the CPU"


# ---------------------------------------------------------------- llama-cpp-python


class PythonBackend:
    """llama-cpp-python, loaded into this process."""

    name = "llama-cpp-python"

    def __init__(self, model: Path, context: int, threads: int, gpu: bool, log: Log):
        self.model, self.context, self.threads, self.gpu, self.log = model, context, threads, gpu, log
        self.llm = None
        self.device = ""
        self._lock = threading.Lock()

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
        try:
            self.llm = Llama(model_path=str(self.model), n_ctx=self.context, n_threads=max(1, self.threads),
                             n_gpu_layers=-1 if (self.gpu and offload) else 0, seed=SEED, verbose=False)
        except Exception as exc:
            raise LocalAIError(f"The local AI couldn't load {self.model.name}: {exc}") from exc
        self.device = "the graphics card" if self.gpu and offload else "the CPU"

    def stop(self) -> None:
        self.llm = None

    def chat(self, system: str, user: str, schema: dict, max_tokens: int, check: Check | None) -> str:
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
            str(getattr(s, "speed", "balanced")))


def context_for(s, model: models.Model | None) -> int:
    asked = int(getattr(s, "local_context", 0) or 0)
    return max(2048, asked or (model.context if model else 8192))


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
        log(f"Downloading the local AI model {model.label.split(' (')[0]} ({model.size_gb:.1f} GB, once)…")
        try:
            path = models.download(model, progress)
        except models.DownloadError as exc:
            raise LocalAIError(str(exc)) from exc
    key = _settings_key(s, path)
    with _engine_lock:
        if _engine is not None and _engine_key == key:
            return _engine
        if _engine is not None:
            _engine.stop()
        gpu = bool(getattr(s, "local_gpu", True)) and hw.has_gpu
        threads = threads_for(getattr(s, "speed", "balanced"))
        ctx = context_for(s, model)
        exe = server_binary()
        if exe is not None:
            _engine = ServerBackend(exe, path, ctx, threads, gpu, log)
        elif has_python_binding():
            _engine = PythonBackend(path, ctx, threads, gpu, log)
        else:
            raise LocalAIError(missing_backend())
        _engine_key = key
        return _engine


def shutdown() -> None:
    global _engine, _engine_key
    with _engine_lock:
        if _engine is not None:
            _engine.stop()
        _engine, _engine_key = None, None


atexit.register(shutdown)


def ask_json(s, system: str, content, schema: dict, *, max_tokens: int = 4000, log: Log | None = None,
             check: Check | None = None, progress: Progress | None = None) -> Reply:
    """Ask the local model for JSON matching `schema`. Raises LocalAIError (TooLong, LocalCutOff)."""
    log = log or (lambda m: None)
    eng = engine(s, log, progress, check)
    user = as_text(content)
    full_system = f"{system}\n\n{schema_prompt(schema)}"
    needed = estimate_tokens(full_system) + estimate_tokens(user) + max_tokens
    if needed > eng.context:
        raise TooLong(f"The transcript is too long for the local AI's memory ({needed:,} of {eng.context:,} "
                      "tokens).")
    first = not eng.running()
    started = time.monotonic()
    text = eng.chat(full_system, user, schema, max_tokens, check)
    if first and eng.device:
        log(f"The local AI is running on {eng.device} ({eng.name}, {time.monotonic() - started:.0f} s).")
    return Reply(text, eng.model.name, eng.name, eng.device)


def room(s) -> int:
    """Tokens of transcript one question can carry, after the instructions and the answer."""
    model, _ = models.resolve(s, hardware.detect())
    return max(1000, context_for(s, model) - 3500)


def status(s) -> str:
    """One line for Settings → AI and the health check: what would run, on what, and with which model."""
    hw = hardware.detect()
    model, path = models.resolve(s, hw)
    how = backend_name()
    if not how:
        return missing_backend()
    where = hw.gpu_name if (hw.has_gpu and getattr(s, "local_gpu", True)) else f"the CPU ({hw.cpu_threads} threads)"
    if model is None:
        have = "ready" if path.is_file() else "file not found"
        return f"{path.name or 'No model file chosen'} ({have}) on {where}, through {how}."
    have = "downloaded" if models.installed(path) else f"downloads once, {model.size_gb:.1f} GB"
    return f"{model.label.split(' (')[0]} ({have}) on {where}, through {how}."
