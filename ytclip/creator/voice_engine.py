"""The narrator: the script read out by a neural voice on this PC, with the time of every word.

Engines, best first (Settings → Creator → Voice engine, "auto" takes the first that works):

* Kokoro-82M (kokoro-onnx): natural, human-sounding voices, quick even on the CPU. The model (310 MB, or the
  88 MB compact one) and its voices (28 MB) download once from the kokoro-onnx releases on GitHub.
* Piper (piper-tts): very light, for slow PCs. Each voice downloads once (60-120 MB) from rhasspy's
  piper-voices.
* Windows' own voices (System.Speech): always there on Windows, nothing to install. Plainer.
* eSpeak NG: robotic, a last resort where nothing else is installed.

Each scene is read on its own and the scenes are joined with a short breath between, so every scene's
start and end time is exact (the pictures change on them). Then every word gets its own time, for the
captions:

1. whisper (the same one transcription uses) listens to the finished voice, and its words are lined up
   with the script's (difflib), so captions show the script's own spelling at whisper's times;
2. without whisper, or when it heard too little of the script, the times come from the sound itself:
   the pauses in it are found, and the words are spread over the speech between them by how long each
   takes to say (its syllables, plus a beat for punctuation).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..subtitles import Word
from . import downloads

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]

SCENE_GAP = 0.32  # seconds of breath between scenes
LEAD_IN = 0.12  # silence before the first word
TAIL = 0.6  # after the last word, for the music to close
SENTENCE_PAUSE = 0.32  # Kokoro's pause after a sentence inside a scene
CLAUSE_PAUSE = 0.12
MIN_SPEED, MAX_SPEED = 0.72, 1.25  # how far a voice's speed may be pushed to hit the length

KOKORO_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
KOKORO_MODELS = {"full": ("kokoro-v1.0.onnx", 300_000_000), "compact": ("kokoro-v1.0.int8.onnx", 80_000_000)}
KOKORO_VOICES = ("voices-v1.0.bin", 25_000_000)
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
ENGINES = ("auto", "kokoro", "piper", "windows", "espeak")
ENGINE_NAMES = {"kokoro": "Kokoro-82M", "piper": "Piper", "windows": "Windows voices", "espeak": "eSpeak NG"}


@dataclass(frozen=True)
class VoiceProfile:
    key: str
    label: str
    kokoro: str  # Kokoro voice id
    speed: float  # Kokoro speed for a natural narration pace
    wpm: float  # words a minute it reads at that speed (measured; the director writes to it)
    piper: str  # piper-voices name: en_US-ryan-high
    gender: str  # for Windows' voices: male | female
    espeak: str  # eSpeak voice
    blurb: str = ""


# Kokoro reads quickly at speed 1 (175-240 words a minute); each profile is slowed to a narrator's pace.
VOICES: dict[str, VoiceProfile] = {v.key: v for v in (
    VoiceProfile("en_male_deep", "Deep Male Narrator", "am_onyx", 0.9, 150, "en_US-ryan-high", "male", "en-us+m3",
                 "low and serious: psychology, dark facts, true crime"),
    VoiceProfile("en_storyteller", "Energetic Storyteller", "am_puck", 0.8, 175, "en_US-joe-medium", "male",
                 "en-us+m1", "quick and lively: stories, motivation, lists"),
    VoiceProfile("en_documentary", "Calm Documentary", "bm_george", 0.9, 160, "en_GB-alan-medium", "male",
                 "en-gb+m2", "British, measured: history, science, mysteries"),
    VoiceProfile("en_female_warm", "Warm Female Narrator", "af_heart", 0.9, 165, "en_US-amy-medium", "female",
                 "en-us+f3", "friendly and clear: self-improvement, facts"),
    VoiceProfile("en_female_bright", "Bright Female", "af_nova", 0.8, 175, "en_US-lessac-high", "female",
                 "en-us+f4", "upbeat: lifestyle, tech, food"),
    VoiceProfile("en_hushed", "Hushed Storyteller", "af_nicole", 1.1, 135, "en_US-kristin-medium", "female",
                 "en-us+f2", "soft, close whisper: scary stories, sleep stories"),
    VoiceProfile("en_british_male", "British Male", "bm_lewis", 0.92, 165, "en_GB-northern_english_male-medium",
                 "male", "en-gb+m3", "warm British: history, explainers"),
    VoiceProfile("en_male_calm", "Calm Male", "am_michael", 0.95, 165, "en_US-hfc_male-medium", "male", "en-us+m2",
                 "steady and plain: finance, explainers"),
)}
DEFAULT_VOICE = "en_male_deep"
ALIASES = {"deep_male": "en_male_deep", "male_deep": "en_male_deep", "deep": "en_male_deep", "narrator": "en_male_deep",
           "storyteller": "en_storyteller", "energetic": "en_storyteller", "documentary": "en_documentary",
           "calm_documentary": "en_documentary", "female": "en_female_warm", "warm_female": "en_female_warm",
           "bright_female": "en_female_bright", "whisper": "en_hushed", "hushed": "en_hushed", "scary": "en_hushed",
           "british": "en_british_male", "calm": "en_male_calm", "calm_male": "en_male_calm"}


# A category's name, or words in its special requests, suggest a voice (as director.STYLE_WORDS suggest a look).
VOICE_WORDS = (
    ("en_hushed", ("scary", "horror", "creepy", "paranormal", "haunted", "ghost", "nosleep", "sleep")),
    ("en_male_deep", ("crime", "true crime", "unsolved", "dark", "psychology", "manipulation", "conspiracy",
                      "secrets", "mystery", "mysteries")),
    ("en_documentary", ("history", "historical", "ancient", "empire", "medieval", "science", "space", "nature",
                        "documentary", "explained")),
    ("en_storyteller", ("gaming", "games", "gamer", "sports", "motivation", "hustle", "stories", "story", "funny",
                        "comedy", "memes", "facts", "anime")),
    ("en_male_calm", ("finance", "money", "business", "investing", "crypto", "tech", "technology", "ai")),
    ("en_female_warm", ("self-improvement", "self improvement", "wellness", "relationship", "relationships",
                        "health", "mindful", "stoic", "stoicism", "parenting")),
    ("en_female_bright", ("food", "cooking", "recipes", "beauty", "fashion", "travel", "lifestyle")),
)


def voice_for(category: str, instructions: str = "") -> str:
    """The voice that suits a category ("" when nothing in its name or special requests says: Settings' is
    used). Crime gets the deep narrator, gaming the energetic one, history the documentary voice."""
    import re

    text = f" {category} {instructions} ".lower()
    for key, words in VOICE_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return key
    return ""


def profile(key: str) -> VoiceProfile:
    k = str(key or "").strip().lower().replace("-", "_").replace(" ", "_")
    k = ALIASES.get(k, k)
    if k in VOICES:
        return VOICES[k]
    by_label = {v.label.lower().replace(" ", "_"): v for v in VOICES.values()}
    if k in by_label:
        return by_label[k]
    for v in VOICES.values():  # a Kokoro voice id ("am_onyx") picks the profile that uses it
        if v.kokoro == k:
            return v
    return VOICES[DEFAULT_VOICE]


class VoiceError(RuntimeError):
    """A plain-English reason the voice couldn't be made."""


@dataclass
class SceneVoice:
    scene_id: int
    text: str
    start: float  # seconds into the voice track
    end: float


@dataclass
class VoiceTrack:
    path: Path  # mono WAV
    duration: float  # the whole track, tail included
    rate: int
    scenes: list[SceneVoice]
    words: list[Word] = field(default_factory=list)
    engine: str = ""
    voice: str = ""
    speed: float = 1.0  # the multiplier on the voice's own pace it was read at
    timing: str = ""  # "whisper" | "from the sound"
    engine_key: str = ""

    @property
    def speech_seconds(self) -> float:
        return self.scenes[-1].end if self.scenes else 0.0

    @property
    def wpm(self) -> float:
        n = sum(len(s.text.split()) for s in self.scenes)
        return n / self.speech_seconds * 60 if self.speech_seconds else 0.0


# ---------------------------------------------------------------- text for the voice

_NUM_SUFFIX = re.compile(r"\b(\d+)(k|m|bn)\b", re.I)


def speakable(text: str) -> str:
    """The script as a voice should read it: no emoji or markup; "$5k" read as "5 thousand dollars"."""
    from .director import clean_speech

    text = clean_speech(text)
    text = re.sub(r"\$(\d[\d,.]*)\s*(k|m|bn|million|billion|thousand)?\b",
                  lambda m: f"{m[1]} {({'k': 'thousand', 'm': 'million', 'bn': 'billion'}).get((m[2] or '').lower(), m[2] or '')} dollars".replace("  ", " "),
                  text, flags=re.I)
    text = _NUM_SUFFIX.sub(lambda m: f"{m[1]} {({'k': 'thousand', 'm': 'million', 'bn': 'billion'})[m[2].lower()]}", text)
    text = text.replace("%", " percent").replace("&", " and ").replace("…", "...")
    return " ".join(text.split())


# ---------------------------------------------------------------- engines

class Engine:
    key = ""

    def __init__(self, s, log: Log, progress: Progress):
        self.s, self.log, self.progress = s, log, progress

    def missing(self) -> str:
        """"" when it can speak, else why not."""
        raise NotImplementedError

    def prepare(self, voice: VoiceProfile) -> None:
        """Download what's needed, load the model."""

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        raise NotImplementedError


_kokoro_lock = threading.Lock()
_kokoro: dict[tuple[str, str], object] = {}


class KokoroEngine(Engine):
    key = "kokoro"

    def missing(self) -> str:
        try:
            import kokoro_onnx  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - onnxruntime can fail with DLL errors, not only ImportError
            return f"kokoro-onnx isn't installed ({type(exc).__name__})"
        return ""

    def files(self) -> tuple[Path, Path]:
        size = getattr(self.s, "creator_kokoro_model", "full")
        name, _ = KOKORO_MODELS.get(size, KOKORO_MODELS["full"])
        root = downloads.folder("tts", "kokoro")
        return root / name, root / KOKORO_VOICES[0]

    def prepare(self, voice: VoiceProfile) -> None:
        model, voices = self.files()
        allowed = getattr(self.s, "creator_downloads", True)
        size = getattr(self.s, "creator_kokoro_model", "full")
        downloads.fetch(KOKORO_URL + model.name, model, "Downloading the Kokoro voice model (once)", self.progress,
                        allowed=allowed, min_bytes=KOKORO_MODELS.get(size, KOKORO_MODELS["full"])[1])
        downloads.fetch(KOKORO_URL + voices.name, voices, "Downloading the Kokoro voices (once)", self.progress,
                        allowed=allowed, min_bytes=KOKORO_VOICES[1])

    def _model(self):
        model, voices = self.files()
        key = (str(model), str(voices))
        with _kokoro_lock:
            if key not in _kokoro:
                from kokoro_onnx import Kokoro

                _kokoro.clear()
                _kokoro[key] = Kokoro(str(model), str(voices))
            return _kokoro[key]

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        k = self._model()
        lang = kokoro_lang(voice.kokoro)
        try:
            audio, rate = k.create(text, voice=voice.kokoro, speed=float(speed), lang=lang,
                                   sentence_pause=SENTENCE_PAUSE, clause_pause=CLAUSE_PAUSE)
        except TypeError:  # kokoro-onnx before 0.5 has no pause options
            audio, rate = k.create(text, voice=voice.kokoro, speed=float(speed), lang=lang)
        return np.asarray(audio, np.float32).ravel(), int(rate)


def kokoro_lang(voice_id: str) -> str:
    """The English a Kokoro voice speaks, from its id's first letter: a (American) or b (British). A British
    voice reading American phonemes keeps the voice but loses the accent ("stjuːdənts" becomes "stuːdənts")."""
    return "en-gb" if voice_id[:1].lower() == "b" else "en-us"


class PiperEngine(Engine):
    key = "piper"

    def missing(self) -> str:
        try:
            import piper  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return f"piper-tts isn't installed ({type(exc).__name__})"
        return ""

    def files(self, voice: VoiceProfile) -> tuple[Path, Path]:
        root = downloads.folder("tts", "piper")
        return root / f"{voice.piper}.onnx", root / f"{voice.piper}.onnx.json"

    def prepare(self, voice: VoiceProfile) -> None:
        model, config = self.files(voice)
        lang, rest = voice.piper.split("-", 1)
        name, quality = rest.rsplit("-", 1)
        base = f"{PIPER_URL}{lang.split('_')[0]}/{lang}/{name}/{quality}/{voice.piper}"
        allowed = getattr(self.s, "creator_downloads", True)
        downloads.fetch(base + ".onnx", model, f"Downloading the Piper voice {voice.piper} (once)", self.progress,
                        allowed=allowed, min_bytes=1_000_000)
        downloads.fetch(base + ".onnx.json", config, "Downloading the Piper voice's settings", self.progress,
                        allowed=allowed, min_bytes=100)

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        from piper import PiperVoice

        model, config = self.files(voice)
        v = PiperVoice.load(str(model), config_path=str(config))
        rate = int(getattr(getattr(v, "config", None), "sample_rate", 22050))
        try:  # piper-tts 1.3+: chunks of float audio
            from piper import SynthesisConfig

            chunks = list(v.synthesize(text, syn_config=SynthesisConfig(length_scale=1.0 / max(0.5, speed))))
            audio = np.concatenate([np.asarray(c.audio_float_array, np.float32) for c in chunks]) if chunks else \
                np.zeros(0, np.float32)
            rate = int(getattr(chunks[0], "sample_rate", rate)) if chunks else rate
            return audio, rate
        except ImportError:  # piper-tts 1.2: writes a WAV
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "piper.wav"
                with wave.open(str(out), "wb") as w:
                    v.synthesize(text, w, length_scale=1.0 / max(0.5, speed))
                return read_wav(out)


SAPI_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try { $s.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::{gender}, [System.Speech.Synthesis.VoiceAge]::Adult, 0, [System.Globalization.CultureInfo]::GetCultureInfo('en-US')) } catch {}
$s.Rate = {rate}
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile('{wav}', $fmt)
$s.Speak([System.IO.File]::ReadAllText('{txt}', [System.Text.Encoding]::UTF8))
$s.Dispose()
"""


class WindowsEngine(Engine):
    key = "windows"

    def missing(self) -> str:
        if sys.platform != "win32":
            return "Windows' voices are only on Windows"
        return "" if shutil.which("powershell") or shutil.which("powershell.exe") else "PowerShell isn't available"

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        from ..ffmpeg import NO_WINDOW

        rate = max(-10, min(10, round((speed * voice.wpm / 165 - 1) * 10)))
        with tempfile.TemporaryDirectory() as tmp:
            txt, wav = Path(tmp) / "say.txt", Path(tmp) / "say.wav"
            txt.write_text(text, encoding="utf-8")
            script = (SAPI_SCRIPT.replace("{gender}", "Female" if voice.gender == "female" else "Male")
                      .replace("{rate}", str(rate)).replace("{wav}", str(wav).replace("'", "''"))
                      .replace("{txt}", str(txt).replace("'", "''")))
            done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                  capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
            if done.returncode != 0 or not wav.is_file():
                raise VoiceError("Windows' voice didn't speak: " + (done.stderr.strip().splitlines() or ["no reason given"])[-1])
            return read_wav(wav)


class EspeakEngine(Engine):
    key = "espeak"

    def exe(self) -> str | None:
        return shutil.which("espeak-ng") or shutil.which("espeak")

    def missing(self) -> str:
        return "" if self.exe() else "eSpeak NG isn't installed"

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        from ..ffmpeg import NO_WINDOW

        with tempfile.TemporaryDirectory() as tmp:
            txt, wav = Path(tmp) / "say.txt", Path(tmp) / "say.wav"
            txt.write_text(text, encoding="utf-8")
            wpm = max(80, min(400, round(voice.wpm * speed)))
            done = subprocess.run([self.exe(), "-v", voice.espeak, "-s", str(wpm), "-g", "2", "-w", str(wav), "-f",
                                   str(txt)], capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW)
            if done.returncode != 0 or not wav.is_file():
                raise VoiceError("eSpeak didn't speak: " + (done.stderr.strip() or "no reason given"))
            return read_wav(wav)


ENGINE_CLASSES = {"kokoro": KokoroEngine, "piper": PiperEngine, "windows": WindowsEngine, "espeak": EspeakEngine}
# Engines that send the script to a service (studio/voice_studio.py adds them). Never picked by "auto": only
# when Settings → Creator → Voice engine names one, so nothing leaves this PC unasked.
CLOUD_ENGINES: set[str] = set()


def _cloud() -> None:
    """Bring in the cloud engines (they add themselves to ENGINE_CLASSES)."""
    try:
        from ..studio import voice_studio  # noqa: F401
    except ImportError:  # a build without the Studio
        pass


def available(s) -> dict[str, str]:
    """Each engine: "" when it can be used here, else why not."""
    _cloud()
    return {key: cls(s, lambda m: None, lambda m, f: None).missing() for key, cls in ENGINE_CLASSES.items()}


def pick_engines(s) -> list[str]:
    """The engines to try, in turn: the chosen one, then (on auto, or when it fails) the others that work."""
    choice = str(getattr(s, "creator_tts", "auto") or "auto")
    have = available(s)
    order = [k for k in ENGINE_CLASSES if not have[k] and k not in CLOUD_ENGINES]
    if choice in ENGINE_CLASSES:
        order = [choice] + [k for k in order if k != choice]
    return order


# ---------------------------------------------------------------- audio helpers

def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float32 samples and the rate of a PCM WAV."""
    with wave.open(str(path), "rb") as w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise VoiceError(f"Unsupported WAV sample width: {width}")
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def resample(audio: np.ndarray, rate: int, to: int) -> np.ndarray:
    """Linear resampling, for joining engines' different rates (the mix resamples properly later)."""
    if rate == to or not len(audio):
        return audio
    n = int(round(len(audio) * to / rate))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)


FRAME = 0.01  # seconds per loudness frame


def envelope(audio: np.ndarray, rate: int) -> np.ndarray:
    """dBFS per 10 ms."""
    hop = max(1, int(rate * FRAME))
    n = len(audio) // hop
    if n == 0:
        return np.zeros(0)
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9)


def trim(audio: np.ndarray, rate: int, keep: float = 0.04) -> np.ndarray:
    """Leading and trailing silence off (engines pad differently), `keep` seconds left at each end."""
    env = envelope(audio, rate)
    if not len(env):
        return audio
    loud = np.nonzero(env > max(env.max() - 45, -60))[0]
    if not len(loud):
        return audio
    hop = int(rate * FRAME)
    a = max(0, loud[0] * hop - int(keep * rate))
    b = min(len(audio), (loud[-1] + 1) * hop + int(keep * rate))
    return audio[a:b]


# ---------------------------------------------------------------- word timing

def syllables(word: str) -> int:
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 1 if re.search(r"\d", word) else 0
    n = len(re.findall(r"[aeiouy]+", w))
    if w.endswith("e") and not w.endswith(("le", "ee")) and n > 1:
        n -= 1
    return max(1, n)


def word_weight(word: str) -> float:
    """How long a word takes to say, in rough syllable units; digits take longer than letters suggest."""
    digits = len(re.findall(r"\d", word))
    return syllables(word) + digits * 0.8 + 0.35


def speech_runs(audio: np.ndarray, rate: int, min_gap: float = 0.09) -> list[tuple[float, float]]:
    """The stretches of speech (seconds), split at pauses of at least `min_gap`."""
    env = envelope(audio, rate)
    if not len(env):
        return []
    floor = max(env.max() - 38, -55)
    on = env > floor
    runs: list[tuple[float, float]] = []
    i, n = 0, len(on)
    gap_frames = max(1, int(min_gap / FRAME))
    while i < n:
        if not on[i]:
            i += 1
            continue
        j = i
        while j < n:
            if on[j]:
                j += 1
                continue
            k = j
            while k < n and not on[k]:
                k += 1
            if k - j >= gap_frames or k >= n:
                break
            j = k
        runs.append((i * FRAME, j * FRAME))
        i = j
    return [r for r in runs if r[1] - r[0] >= 0.04]


def estimate_words(text: str, audio: np.ndarray, rate: int, offset: float = 0.0) -> list[Word]:
    """Times for `text`'s words from the sound: spread over the speech, skipping its pauses, by how long
    each word takes to say. Pauses go after punctuation first."""
    words = text.split()
    if not words:
        return []
    runs = speech_runs(audio, rate) or [(0.0, len(audio) / rate)]
    weights = np.array([word_weight(w) for w in words], float)
    # A pause in the sound matches a punctuation mark in the text: words after a comma or full stop start
    # in a new run when there are as many runs as there are marks.
    breaks = [i + 1 for i, w in enumerate(words[:-1]) if w[-1:] in ",.;:!?…" or w.endswith(("—", "-"))]
    groups: list[list[int]]
    if len(runs) > 1 and len(breaks) >= len(runs) - 1:
        # Keep the strongest marks (full stops before commas) as the run boundaries.
        strength = {b: (2 if words[b - 1][-1:] in ".!?…" else 1) for b in breaks}
        chosen = sorted(sorted(breaks, key=lambda b: (-strength[b], b))[: len(runs) - 1])
        edges = [0, *chosen, len(words)]
        groups = [list(range(edges[g], edges[g + 1])) for g in range(len(edges) - 1)]
    else:
        groups = []
    out: list[Word] = []
    if groups and all(groups):
        for (a, b), idx in zip(runs, groups):
            w = weights[idx]
            cum = np.concatenate([[0], np.cumsum(w)]) / w.sum()
            for j, i in enumerate(idx):
                out.append(Word(round(float(offset + a + cum[j] * (b - a)), 3), round(float(offset + a + cum[j + 1] * (b - a)), 3),
                                words[i]))
        return out
    # Otherwise: one timeline of speech only, the words spread along it, then put back in real time.
    total = sum(b - a for a, b in runs)
    cum = np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * total

    def real(t: float) -> float:
        for a, b in runs:
            if t <= b - a + 1e-9:
                return a + t
            t -= b - a
        return runs[-1][1]

    for i, w in enumerate(words):
        out.append(Word(round(float(offset + real(cum[i])), 3), round(float(offset + real(cum[i + 1])), 3), w))
    return out


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


def align(script: list[Word], heard: list[Word]) -> tuple[list[Word], float]:
    """The script's words at the times whisper heard them. Words whisper missed or heard differently get
    times between their matched neighbours. Returns (words, share of the script whisper matched)."""
    if not script:
        return [], 0.0
    a = [_norm(w.text) for w in script]
    b = [_norm(w.text) for w in heard]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    times: list[tuple[float, float] | None] = [None] * len(script)
    matched = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (heard[j1 + k].start, heard[j1 + k].end)
                matched += 1
        elif tag == "replace" and i2 - i1 == j2 - j1:  # misspelt, one for one ("Voynich" / "voinich")
            for k in range(i2 - i1):
                times[i1 + k] = (heard[j1 + k].start, heard[j1 + k].end)
        elif tag == "replace" and j2 > j1:  # different splits ("twenty five" / "25"): share the stretch
            t0, t1 = heard[j1].start, heard[j2 - 1].end
            ws = [word_weight(script[i].text) for i in range(i1, i2)]
            acc = t0
            for k, i in enumerate(range(i1, i2)):
                d = (t1 - t0) * ws[k] / sum(ws)
                times[i] = (acc, acc + d)
                acc += d
    # Gaps: between the known neighbours, else the estimate's own times.
    i = 0
    n = len(script)
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        lo = times[i - 1][1] if i > 0 else script[i].start
        hi = times[j][0] if j < n else script[j - 1].end
        if hi <= lo:
            hi = lo + 0.05 * (j - i)
        ws = [word_weight(script[k].text) for k in range(i, j)]
        acc = lo
        for k in range(i, j):
            d = (hi - lo) * ws[k - i] / sum(ws)
            times[k] = (acc, acc + d)
            acc += d
        i = j
    out = []
    prev_end = 0.0
    for w, (t0, t1) in zip(script, times):  # type: ignore[misc]
        t0 = max(t0, prev_end - 0.02)
        t1 = max(t1, t0 + 0.05)
        out.append(Word(round(t0, 3), round(t1, 3), w.text))
        prev_end = t1
    return out, matched / len(script)


def whisper_words(path: Path, s, log: Log, progress: Progress) -> list[Word]:
    """What whisper hears in the voice track ([] when whisper isn't installed here)."""
    from .. import speed
    from .. import transcribe as whisper

    gpu = speed.use_gpu(s)
    if not whisper.available() and not gpu:
        return []
    try:
        _, words = whisper.transcribe(path, getattr(s, "whisper_model", "base"), "en", 0.0, progress,
                                      threads=speed.threads(s.speed), gpu=gpu, log=log)
    except Exception as exc:  # noqa: BLE001 - timing falls back to the sound itself
        log(f"Whisper couldn't time the words ({exc}); timing them from the sound instead.")
        return []
    return words


# ---------------------------------------------------------------- the whole narration

def synthesize(scenes, s, out: Path, *, voice: str = "", speed: float | None = None, log: Log | None = None,
               progress: Progress | None = None, check: Callable[[], None] | None = None,
               engines: list[str] | None = None) -> VoiceTrack:
    """Read every scene (director.Scene) into one WAV at `out`. `speed` multiplies the voice's own (1 =
    its natural narration pace). Raises VoiceError."""
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    prof = profile(voice or getattr(s, "creator_voice", DEFAULT_VOICE))
    order = engines if engines is not None else pick_engines(s)
    if not order:
        raise VoiceError("No voice engine is installed. Install Kokoro (pip install kokoro-onnx soundfile), or on "
                         "Windows the built-in voices are used.")
    failures: list[str] = []
    for key in order:
        eng = ENGINE_CLASSES[key](s, log, progress)
        why = eng.missing()
        if why:
            failures.append(f"{ENGINE_NAMES[key]}: {why}")
            continue
        try:
            eng.prepare(prof)
            track = _read_scenes(eng, scenes, prof, speed or 1.0, out, progress, check)
        except (downloads.MissingModel, OSError, VoiceError, RuntimeError, ValueError) as exc:
            if type(exc).__name__ in ("Cancelled", "Stopped"):
                raise
            failures.append(f"{ENGINE_NAMES[key]}: {exc}")
            log(f"The {ENGINE_NAMES[key]} voice didn't work ({exc}); trying the next one.")
            continue
        track.engine, track.voice = ENGINE_NAMES[key], prof.label
        return track
    raise VoiceError("No voice could read the script. " + " · ".join(failures))


def _read_scenes(eng: Engine, scenes, prof: VoiceProfile, speed: float, out: Path, progress: Progress,
                 check) -> VoiceTrack:
    real_speed = max(MIN_SPEED, min(MAX_SPEED, prof.speed * speed)) if eng.key == "kokoro" else max(0.6, min(1.6, speed))
    rate = 0
    pieces: list[np.ndarray] = []
    spans: list[SceneVoice] = []
    words: list[Word] = []
    at = LEAD_IN
    n = len(scenes)
    for i, sc in enumerate(scenes):
        if check:
            check()
        progress(f"Recording the voice: scene {i + 1} of {n}", i / max(1, n))
        text = speakable(sc.speech_text)
        if not text:
            continue
        audio, r = eng.speak(text, prof, real_speed)
        if not len(audio):
            raise VoiceError(f"The voice made no sound for scene {i + 1}.")
        rate = rate or r
        audio = trim(resample(audio, r, rate), rate)
        peak = float(np.max(np.abs(audio))) or 1.0
        audio = audio * min(4.0, 0.89 / peak)  # every scene at the same level
        if pieces:
            pieces.append(np.zeros(int(SCENE_GAP * rate), np.float32))
            at += SCENE_GAP
        start = at
        pieces.append(audio.astype(np.float32))
        at += len(audio) / rate
        spans.append(SceneVoice(sc.scene_id, sc.speech_text, round(start, 3), round(at, 3)))
        # The scene's displayed words (the script's spelling), timed from its own sound.
        words += estimate_words(sc.speech_text, audio, rate, start)
    if not pieces:
        raise VoiceError("The script has nothing to read.")
    lead = np.zeros(int(LEAD_IN * rate), np.float32)
    tail = np.zeros(int(TAIL * rate), np.float32)
    full = np.concatenate([lead, *pieces, tail])
    out.parent.mkdir(parents=True, exist_ok=True)
    write_wav(out, full, rate)
    progress("Recording the voice", 1.0)
    return VoiceTrack(out, len(full) / rate, rate, spans, words, speed=speed, timing="from the sound",
                      engine_key=eng.key)


def time_words(track: VoiceTrack, s, log: Log | None = None, progress: Progress | None = None) -> VoiceTrack:
    """Better word times from whisper, when it's installed and hears the script (Settings → Creator →
    Word timing). The pauses-and-syllables estimate stays otherwise."""
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    if getattr(s, "creator_word_timing", "whisper") != "whisper":
        return track
    progress("Lining up the words with whisper", None)
    heard = whisper_words(track.path, s, log, progress)
    if not heard:
        return track
    aligned, share = align(track.words, heard)
    if share < 0.55:
        log(f"Whisper heard only {share:.0%} of the script, so the word times come from the sound.")
        return track
    # Keep every word inside its own scene (a scene's picture and captions change together).
    fixed: list[Word] = []
    k = 0
    for sc in track.scenes:
        n = len(sc.text.split())
        for w in aligned[k:k + n]:
            fixed.append(Word(max(sc.start - 0.05, min(w.start, sc.end)), max(sc.start, min(w.end, sc.end + 0.05)),
                              w.text))
        k += n
    track.words = fixed
    track.timing = "whisper"
    log(f"Word times from whisper ({share:.0%} of the script heard exactly).")
    return track


# ---------------------------------------------------------------- learning each voice's pace

def rates_path() -> Path:
    from ..settings import app_dir

    return app_dir() / "creator_voice_rates.json"


def learned_wpm(voice: str, engine: str = "kokoro") -> float:
    """The voice's measured words a minute with that engine (the last videos' average), else its profile's."""
    prof = profile(voice)
    try:
        data = json.loads(rates_path().read_text(encoding="utf-8"))
        value = float(data.get(f"{engine}:{prof.key}", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        value = 0.0
    return value if 90 <= value <= 260 else prof.wpm


def remember_wpm(voice: str, wpm: float, speed: float = 1.0, engine: str = "kokoro") -> None:
    """Note what the voice read at (scaled back to speed 1), so the next script is written to its pace."""
    if not 60 <= wpm <= 300 or speed <= 0:
        return
    prof = profile(voice)
    path = rates_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    natural = wpm / speed
    key = f"{engine}:{prof.key}"
    old = float(data.get(key, 0) or 0)
    data[key] = round(natural if not old else old * 0.6 + natural * 0.4, 1)
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def preview(s, voice: str, out: Path, text: str = "", log: Log | None = None,
            progress: Progress | None = None) -> VoiceTrack:
    """A short sample of a voice, for the Creator tab's ▶."""
    from .director import Scene

    prof = profile(voice)
    text = text or f"This is the {prof.label}. Here's how your next video will sound."
    return synthesize([Scene(1, text)], s, out, voice=prof.key, log=log, progress=progress)
