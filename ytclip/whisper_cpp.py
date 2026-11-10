"""Whisper on the graphics card: whisper.cpp built with Vulkan, so it runs on AMD, NVIDIA and Intel
graphics alike (faster-whisper only uses NVIDIA cards). The release build ships it in the app
folder as whisper-cpp/whisper-cli.exe. Its models are a different format from faster-whisper's,
so each is downloaded once, on first use, to the models folder.

All the stretches to type out go through one run, joined with a little silence between them, so the
model loads once. A PC with no graphics card whisper.cpp can use is remembered, and Whisper goes
straight to the CPU there next time (transcribe.py).

When the card fails part-way (a driver timeout or "device lost", running out of graphics memory, a crash,
or whisper.cpp saying nothing for GPU_SILENCE_SECONDS), the same run is done again by whisper.cpp on the
CPU, with every thread Speed allows, so the video is still typed out and the run carries on; automation.log
gets the details (diagnostics.py). The card then rests for half an hour before it's tried again, and after
GPU_FAILS_TO_STOP failures in a row it isn't tried again until Settings → Speed says so. Running out of
graphics memory while the local AI's model is loaded stops the local AI first (it starts again by itself
when it's next asked something) and tries the card once more."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import diagnostics
from .ffmpeg import NO_WINDOW, Cancelled, tracked
from .settings import models_dir, work_folder
from .subtitles import Cue, Word

Cancel = Cancelled
RATE = 16000
JOIN = 0.6  # seconds of silence between joined stretches
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
# faster-whisper's model names -> whisper.cpp's files. The bigger ones are quantised: much smaller to
# download, quicker on the graphics card, and hardly any less accurate.
MODELS = {
    "tiny": "ggml-tiny.bin",
    "base": "ggml-base.bin",
    "small": "ggml-small.bin",
    "medium": "ggml-medium-q5_0.bin",
    "large-v3-turbo": "ggml-large-v3-turbo-q5_0.bin",
    "large-v3": "ggml-large-v3-q5_0.bin",
}
NOT_WORDS = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\)|\*[^*]*\*)\s*$")  # [BLANK_AUDIO], (music), *laughs*
SENTENCE_END = (".", "!", "?", "…")
GPU_API = "Metal" if sys.platform == "darwin" else "Vulkan"  # what whisper.cpp is built with here
# A Mac's card is "MTL0" to whisper.cpp ("using MTL0 backend"); older builds said "Metal".
GPU_FOUND = re.compile(r"ggml_vulkan: Found [1-9]|using (Vulkan|CUDA|Metal|MTL)\d* backend", re.I)
# The card's name. On a Mac the "found device" line has it ("Apple M2"); "GPU name" may only say "MTL0".
GPU_NAME = [re.compile(r"ggml_vulkan: \d+ = ([^|(\n]+)"), re.compile(r"ggml_metal\w*: found device:\s*([^\n]+)"),
            re.compile(r"ggml_metal\w*: GPU name:\s*([^\n]+)")]
NO_GPU = re.compile(r"no GPU found|ggml_vulkan: Found 0|ggml_vulkan: No devices", re.I)
PROGRESS = re.compile(r"progress\s*=\s*(\d+)%")
LANGUAGE = re.compile(r"auto-detected language:\s*([a-z]{2,3})")
GPU_REST_SECONDS = 1800  # after the card fails, Whisper uses the CPU this long before trying the card again
GPU_FAILS_TO_STOP = 3  # failures in a row before the card isn't tried again (Settings → Speed tries it afresh)
GPU_SILENCE_SECONDS = 600.0  # whisper-cli on the card saying nothing this long has hung (it prints every 5%)
GPU_SLOWEST = 0.4  # ...or longer for long audio: a slow card may take 1/0.4 times the audio's length per 5%
HUNG = -999  # run()'s exit code when whisper-cli was stopped for saying nothing

Progress = Callable[[str, "float | None"], None]
Log = Callable[[str], None]


def binary() -> Path | None:
    """whisper-cli: next to the app, or wherever CLIPMONSTER_WHISPER_CPP points (tests and CI)."""
    env = os.environ.get("CLIPMONSTER_WHISPER_CPP")
    if env:
        return Path(env) if Path(env).is_file() else None
    name = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"
    if getattr(sys, "frozen", False):
        from . import bundled

        return bundled.find("whisper-cpp", name)
    here = Path(__file__).resolve().parents[1] / "build" / "mac-tools" / "whisper-cpp" / name  # build_mac.py's
    if here.is_file():
        return here
    found = shutil.which("whisper-cli")
    return Path(found) if found else None


def _state_path() -> Path:
    return models_dir() / "whisper.cpp" / "gpu.json"


def _remembered(exe: Path) -> dict:
    """What the last run found out about the graphics card, while it's the same whisper-cli."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if data.get("binary") == f"{exe}|{exe.stat().st_mtime_ns}" else {}
    except (OSError, ValueError):
        return {}


def _remember(exe: Path, ok: bool, why: str = "", **more) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"binary": f"{exe}|{exe.stat().st_mtime_ns}", "ok": ok, "why": why,
                                "checked": time.time(), **more}), encoding="utf-8")


def unavailable() -> str:
    """Why Whisper can't use the graphics card, or "" when it can (or hasn't been tried yet)."""
    exe = binary()
    if exe is None:
        return "this copy of ClipMonster doesn't include it"
    known = _remembered(exe)
    if known and not known.get("ok"):
        return known.get("why") or "no usable graphics card was found last time"
    return ""


def resting(exe: Path | None = None, now: float | None = None) -> float:
    """When the graphics card failed lately: the time it's tried again (until then Whisper uses the CPU).
    0 when it isn't resting."""
    exe = exe or binary()
    if exe is None:
        return 0.0
    known = _remembered(exe)
    until = float(known.get("rest_until") or 0)
    return until if known.get("ok", True) and until > (now or time.time()) else 0.0


def rest_note(now: float | None = None) -> str:
    """'resting until 14:32, after it failed while typing out the clips', or "" when the card isn't
    resting."""
    exe = binary()
    until = resting(exe, now) if exe else 0.0
    if not until:
        return ""
    failed = str(_remembered(exe).get("failed") or "it failed")
    if failed.startswith("the graphics card "):
        failed = "it " + failed[len("the graphics card "):]
    return f"resting until {time.strftime('%H:%M', time.localtime(until))}, after {failed}"


def _card_failed(exe: Path, problem: str) -> bool:
    """The card failed where the CPU didn't: rest it. True when that's GPU_FAILS_TO_STOP in a row, and it
    isn't tried again until Settings → Speed says so."""
    known = _remembered(exe)
    fails = int(known.get("fails") or 0) + 1
    if fails >= GPU_FAILS_TO_STOP:
        _remember(exe, False, f"the graphics card failed {fails} times in a row ({problem})")
        return True
    _remember(exe, True, rest_until=time.time() + GPU_REST_SECONDS, fails=fails, failed=problem)
    return False


def forget_gpu_check() -> None:
    """Try the graphics card again next time (after a driver update, say)."""
    _state_path().unlink(missing_ok=True)


def model_path(name: str) -> Path:
    return models_dir() / "whisper.cpp" / MODELS.get(name, MODELS["base"])


def fetch_model(name: str, progress: Progress, opener=urllib.request.urlopen) -> Path:
    """The model file, downloaded first if it isn't here yet (whole or not at all)."""
    path = model_path(name)
    if path.is_file() and path.stat().st_size > 1_000_000:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    label = f"Downloading Whisper '{name}' for the graphics card (once)"
    progress(label, None)
    with opener(MODEL_URL + path.name, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            progress(label, done / total if total else None)
    if total and part.stat().st_size != total:
        part.unlink(missing_ok=True)
        raise OSError("the Whisper model download was cut short")
    part.replace(path)
    return path


def join_spans(audio: np.ndarray, spans: list[tuple[float, float]]) -> tuple[np.ndarray, list[tuple[float, float, float]]]:
    """The stretches as one track with JOIN seconds of silence between, and where each one went:
    (start in the joined track, end in it, start in the video)."""
    gap = np.zeros(int(JOIN * RATE), np.float32)
    parts: list[np.ndarray] = []
    where: list[tuple[float, float, float]] = []
    at = 0.0
    for s, e in spans:
        piece = audio[int(s * RATE): int(e * RATE)]
        if parts:
            parts.append(gap)
            at += JOIN
        parts.append(piece)
        where.append((at, at + len(piece) / RATE, s))
        at += len(piece) / RATE
    return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), where


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def command(exe: Path, model: Path, wav: Path, out_base: Path, language: str, threads: int,
            gpu: bool = True) -> list[str]:
    # -ml 1 -sow: one segment per word, each with its own times. -pp: progress on stderr. -ng: the CPU only.
    return [str(exe), "-m", str(model), "-f", str(wav), "-l", language or "auto", "-t", str(max(1, threads)),
            "-oj", "-of", str(out_base), "-ml", "1", "-sow", "-pp"] + ([] if gpu else ["-ng"])


def cpu_env() -> dict[str, str]:
    """whisper-cli's environment for a run on the CPU: the graphics card hidden from it altogether, so a
    driver that just failed isn't even asked what cards there are."""
    return {**os.environ, "GGML_VK_VISIBLE_DEVICES": ""}


def parse(data: dict, where: list[tuple[float, float, float]] | None = None) -> list[Word]:
    """Words from whisper-cli's JSON, moved back to their place in the video."""
    words: list[Word] = []
    for seg in data.get("transcription") or []:
        text = str(seg.get("text") or "").strip()
        offsets = seg.get("offsets") or {}
        if not text or NOT_WORDS.match(text) or "from" not in offsets:
            continue
        start, end = offsets["from"] / 1000, offsets["to"] / 1000
        if where is not None:
            mid = (start + end) / 2
            spot = next(((a, b, real) for a, b, real in where if a - JOIN / 2 <= mid <= b + JOIN / 2), None)
            if spot is None:
                continue
            a, b, real = spot
            start, end = real + max(0.0, start - a), real + min(b, end) - a
            if end < start:
                end = start
        if words and not text[:1].isalnum() and text[:1] not in "\"'([":  # a lone "," or "." joins its word
            words[-1].text += text
            words[-1].end = max(words[-1].end, end)
            continue
        words.append(Word(start, end, text))
    return words


def cues_from(words: list[Word], longest: float = 8.0, pause: float = 0.8) -> list[Cue]:
    """Caption lines from words: a line ends at the end of a sentence, a pause, or after `longest` s."""
    cues: list[Cue] = []
    line: list[Word] = []
    for w in words:
        if line and (w.start - line[-1].end > pause or w.end - line[0].start > longest):
            cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
            line = []
        line.append(w)
        if w.text.endswith(SENTENCE_END):
            cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
            line = []
    if line:
        cues.append(Cue(line[0].start, line[-1].end, " ".join(x.text for x in line)))
    return cues


def run(cmd: list[str], progress: Progress, label: str, env: dict[str, str] | None = None,
        silence: float | None = None) -> tuple[int, list[str]]:
    """Run whisper-cli, passing on its progress. Returns its exit code and what it said. With `silence`, a
    run that says nothing for that many seconds is stopped, and the exit code is HUNG."""
    said: list[str] = []
    frac: float | None = None
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=env)
    heard = [time.monotonic()]
    hung, finished = threading.Event(), threading.Event()

    def watch() -> None:
        assert silence
        while not finished.wait(min(5.0, silence / 4)):
            if time.monotonic() - heard[0] > silence:
                hung.set()
                proc.kill()
                return

    if silence:
        threading.Thread(target=watch, name="whisper-cli watch", daemon=True).start()
    try:
        with tracked(proc):
            assert proc.stderr is not None
            for line in proc.stderr:
                heard[0] = time.monotonic()
                said.append(line.rstrip())
                del said[:-400]
                m = PROGRESS.search(line)
                frac = int(m[1]) / 100 if m else frac
                progress(label, frac)  # also where a cancel stops it
            proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        finished.set()
    if hung.is_set():
        said.append(f"ClipMonster: whisper-cli said nothing for {silence / 60:.0f} minutes, so it was stopped.")
        return HUNG, said
    return proc.returncode, said


def _exit(code: int) -> str:
    if code == HUNG:
        return "it stopped responding"
    if code < 0:
        return f"stopped by signal {-code}"
    return f"exit code {code}" if code < 256 else f"exit code {code & 0xFFFFFFFF:#010x}"  # a Windows crash code


def silence_for(seconds: float) -> float:
    """How long whisper-cli on the card may say nothing before it counts as hung: it reports every 5% of
    the audio, which on a slow card (GPU_SLOWEST times real time) takes a while for a long video."""
    return max(GPU_SILENCE_SECONDS, 0.05 * seconds / GPU_SLOWEST)


def _problem(code: int, said: list[str], what: str, silence: float = GPU_SILENCE_SECONDS) -> str:
    """What went wrong on the graphics card, in plain words."""
    if code == HUNG:
        return (f"the graphics card stopped responding while typing out {what} (nothing from whisper.cpp for "
                f"{max(1, round(silence / 60))} minutes)")
    kind = diagnostics.classify(said)
    if kind == "oom":
        return f"the graphics card ran out of memory while typing out {what}"
    if kind == "gpu":
        return f"the graphics card failed while typing out {what}"
    return f"whisper.cpp stopped on the graphics card ({_exit(code)})"


def _make_room() -> bool:
    """Stop the local AI if its model is holding graphics memory. True when it did."""
    try:
        from .intelligence import llama_engine

        return llama_engine.free_graphics_memory()
    except Exception:  # noqa: BLE001 - the local AI isn't there or isn't running: no room to make
        return False


def _attempt(exe: Path, model: Path, wav: Path, out: Path, language: str, threads: int, progress: Progress,
             label: str, gpu: bool, silence: float | None = None) -> tuple[dict | None, int, list[str]]:
    """One whisper-cli run: (its words as JSON, or None when it failed; its exit code; what it said). On the
    card, `silence` is how long it may say nothing."""
    result = out.with_suffix(".json")
    result.unlink(missing_ok=True)  # a failed run's leftovers never count as this run's words
    progress(label, 0.0)
    code, said = run(command(exe, model, wav, out, language, threads, gpu), progress, label,
                     env=None if gpu else cpu_env(), silence=(silence or GPU_SILENCE_SECONDS) if gpu else None)
    if code == 0 and result.is_file():
        try:
            return json.loads(result.read_text(encoding="utf-8", errors="replace")), code, said
        except ValueError:
            pass  # half written: as good as a failure
    return None, code, said


def _failed(exe: Path, code: int, said: list[str]) -> RuntimeError:
    """whisper.cpp itself doesn't work (on the CPU either): not again on every video, and faster-whisper
    takes over (transcribe.py). Speed's 'Try the graphics card again' tries it afresh."""
    tail = " ".join(line for line in said[-3:] if line) or _exit(code)
    why = f"whisper.cpp failed: {tail[:300]}"
    _remember(exe, False, why)
    return RuntimeError(why)


def _on_card(exe: Path, model: Path, wav: Path, out: Path, language: str, threads: int, progress: Progress,
             what: str, log: Log, seconds: float) -> dict:
    label = f"Transcribing {what} on the graphics card"
    silence = silence_for(seconds)
    data, code, said = _attempt(exe, model, wav, out, language, threads, progress, label, True, silence)
    if data is None and code != HUNG and diagnostics.classify(said) == "oom" and _make_room():
        diagnostics.gpu_fallback("Whisper", f"the graphics card ran out of memory while typing out {what}, with the "
                                 "local AI's model loaded", said, "Stopped the local AI to make room (it starts again "
                                 "by itself when it's next needed) and trying the card again.", log)
        data, code, said = _attempt(exe, model, wav, out, language, threads, progress, label, True, silence)
    if data is not None:
        text = "\n".join(said)
        if not GPU_FOUND.search(text) or NO_GPU.search(text):
            _remember(exe, False, f"no graphics card that supports {GPU_API} was found")
            log("Whisper found no graphics card it can use, so it uses the CPU from now on. (Settings → Speed)")
        else:
            known = _remembered(exe)
            if not known.get("ok") or known.get("fails"):  # the first time, or back after failing
                _remember(exe, True)
                found = next((m for m in (name.search(text) for name in GPU_NAME) if m), None)
                card = found[1].strip() if found else ""
                log("Whisper is using the graphics card" + (f" ({card})" if card else "") + ".")
        return data

    # It failed on the card: the same run again on the CPU, so the video is still typed out.
    problem = _problem(code, said, what, silence)
    diagnostics.gpu_fallback("Whisper", problem, said, f"Typing out {what} again on the CPU "
                             f"({max(1, threads)} threads).", log)
    data, cpu_code, cpu_said = _attempt(exe, model, wav, out, language, threads, progress,
                                        f"Transcribing {what} on the CPU", False)
    if data is None:
        diagnostics.write([f"[GPU] Whisper: whisper.cpp failed on the CPU too ({_exit(cpu_code)}), so it isn't the "
                           "graphics card; faster-whisper takes over."]
                          + [f"[GPU]   {line[:300]}" for line in cpu_said if line.strip()][-6:])
        raise _failed(exe, cpu_code, cpu_said)
    if _card_failed(exe, problem):
        log(f"Whisper: the graphics card has failed {GPU_FAILS_TO_STOP} times in a row, so Whisper uses the CPU "
            "from now on. (Settings → Speed → Try the graphics card again)")
    else:
        log(f"Whisper: typed out on the CPU; the graphics card is tried again after "
            f"{time.strftime('%H:%M', time.localtime(resting(exe)))}.")
    return data


def transcribe(audio: np.ndarray, model_name: str, language: str, spans: list[tuple[float, float]] | None,
               threads: int, progress: Progress, log: Log, opener=urllib.request.urlopen,
               cpu: bool = False) -> tuple[list[Cue], list[Word]]:
    """Type out `audio` (or its `spans`) on the graphics card, else (`cpu`, or the card resting) the CPU.
    The Mac app has no faster-whisper: there, whisper.cpp is Whisper on the CPU too."""
    from .transcribe import merge_spans

    exe = binary()
    if exe is None:
        raise RuntimeError(unavailable())
    model = fetch_model(model_name, progress, opener)
    duration = len(audio) / RATE
    where = None
    if spans is not None:
        spans = merge_spans(spans, duration)
        if not spans:
            return [], []
        audio, where = join_spans(audio, spans)
    what = "the speech" if spans is None else "the clips"
    with work_folder("whisper-") as tmp:
        wav, out = Path(tmp) / "audio.wav", Path(tmp) / "words"
        write_wav(wav, audio)
        until = resting(exe)
        if cpu:
            data, code, said = _attempt(exe, model, wav, out, language, threads, progress,
                                        f"Transcribing {what}", False)
            if data is None:
                raise _failed(exe, code, said)
        elif until:  # the card failed lately: the CPU until it has rested
            log(f"Whisper is using the CPU until {time.strftime('%H:%M', time.localtime(until))}: the graphics card "
                "failed a little while ago (details in automation.log).")
            data, code, said = _attempt(exe, model, wav, out, language, threads, progress,
                                        f"Transcribing {what} on the CPU", False)
            if data is None:
                raise _failed(exe, code, said)
        else:
            data = _on_card(exe, model, wav, out, language, threads, progress, what, log, len(audio) / RATE)
    words = parse(data, where)
    return cues_from(words), words
