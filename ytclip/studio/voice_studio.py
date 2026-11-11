"""The Studio's voice: one way to get a narration and its word times, whichever engine speaks.

    track = narrate(board.scenes, settings, work / "voice.wav")
    track.path, track.duration, track.words        # a WAV, and every word's start and end
    export(track, folder / "assets")               # voice.wav + voice.words.json, kept beside the video

**Local voices** are the Creator's (creator/voice_engine.py): Kokoro-82M, then Piper, then Windows' own voices
or eSpeak NG, whichever is installed, in that order. Nothing leaves the computer.

**A cloud voice** is used only when you choose it by name (Settings → Creator → Voice engine, or `--tts`),
never as an automatic fallback, because it sends the script's text to that service and costs money a
character:

* ElevenLabs, with your own API key (Settings → Creator → ElevenLabs key, or the ELEVENLABS_API_KEY
  environment variable). The voice is one from your ElevenLabs account, by name or id (Settings → Creator →
  ElevenLabs voice); empty picks the first of your voices that suits the narrator chosen. Only use voices
  you have the right to use: your own, or ones ElevenLabs licenses to you. This app doesn't clone voices.

If the cloud voice fails (no credit, no connection), the local voices take over and the log says so.

**Length.** The Creator lands a narration on its target length by having the engine read it again a touch
faster or slower. A paid engine would charge twice for that, so a cloud narration is fitted here instead
(retime(): ffmpeg's atempo, which keeps the pitch), and its word times are scaled with it.

**Word times** come from whisper listening to the finished narration (voice_engine.time_words), lined up with
the script so captions show the script's spelling; without whisper they're estimated from the sound's own
pauses and syllables.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np

from ..creator import voice_engine
from ..creator.voice_engine import Engine, VoiceError, VoiceProfile, VoiceTrack
from ..ffmpeg import NO_WINDOW, ffmpeg_exe
from ..subtitles import Word

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]

ELEVEN_URL = "https://api.elevenlabs.io/v1"
ELEVEN_MODEL = "eleven_multilingual_v2"
ELEVEN_RATE = 24000
ELEVEN_SPEED = (0.7, 1.2)  # what its voice_settings.speed takes
ELEVEN_CHARS = 4500  # of text in one request (its limit is higher; a scene is far shorter)
Http = Callable[[str, dict, "bytes | None", float], bytes]


def _http(url: str, headers: dict, body: bytes | None = None, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def decode(data: bytes, rate: int = ELEVEN_RATE) -> np.ndarray:
    """Compressed audio (MP3) as mono float32 samples at `rate`."""
    done = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(rate), "-f",
                           "f32le", "pipe:1"], input=data, capture_output=True, creationflags=NO_WINDOW, timeout=120)
    if done.returncode != 0 or not done.stdout:
        raise VoiceError("The voice service's audio couldn't be read (" +
                         (done.stderr.decode("utf-8", "replace").strip()[-160:] or "it was empty") + ").")
    return np.frombuffer(done.stdout, np.float32).copy()


class ElevenLabsEngine(Engine):
    """ElevenLabs' text-to-speech API with the user's own key."""

    key = "elevenlabs"
    http: Http = staticmethod(_http)

    def api_key(self) -> str:
        return (getattr(self.s, "elevenlabs_api_key", "") or os.environ.get("ELEVENLABS_API_KEY", "")).strip()

    def missing(self) -> str:
        return "" if self.api_key() else "there's no ElevenLabs key (Settings → Creator → ElevenLabs key)"

    def _call(self, path: str, body: dict | None = None, accept: str = "application/json") -> bytes:
        headers = {"xi-api-key": self.api_key(), "Accept": accept}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            return self.http(ELEVEN_URL + path, headers, data, 120.0)
        except urllib.error.HTTPError as exc:
            raise VoiceError(self._why(exc)) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise VoiceError(f"ElevenLabs couldn't be reached ({getattr(exc, 'reason', None) or exc}).") from exc

    @staticmethod
    def _why(exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            data = json.loads(exc.read().decode("utf-8", "replace"))
            d = data.get("detail")
            detail = (d.get("message") or d.get("status") or "") if isinstance(d, dict) else str(d or "")
        except (ValueError, OSError, AttributeError):
            pass
        finally:
            exc.close()
        low = detail.lower()
        if exc.code == 401 and "quota" not in low:
            return "ElevenLabs didn't accept the key (Settings → Creator → ElevenLabs key)."
        if exc.code in (401, 402) or "quota" in low or "credit" in low:
            return "The ElevenLabs account is out of credit for this narration" + (f" ({detail})." if detail else ".")
        if exc.code == 429:
            return "ElevenLabs is busy or the plan's rate limit was reached. Try again in a minute."
        if exc.code in (400, 404, 422) and "voice" in low:
            return f"ElevenLabs doesn't have that voice on this account ({detail})."
        return f"ElevenLabs answered {exc.code}" + (f": {detail}" if detail else "") + "."

    def voices(self) -> list[dict]:
        """The account's voices: [{"voice_id", "name", "labels": {...}}, ...]."""
        if getattr(self, "_voices", None) is None:
            try:
                data = json.loads(self._call("/voices").decode("utf-8"))
            except ValueError as exc:
                raise VoiceError("ElevenLabs' list of voices couldn't be read.") from exc
            self._voices = [v for v in data.get("voices") or [] if isinstance(v, dict) and v.get("voice_id")]
        return self._voices

    def voice_id(self, voice: VoiceProfile) -> str:
        """The ElevenLabs voice to read with: Settings' (an id, or a voice's name), else the first of the
        account's voices of the narrator's gender."""
        want = str(getattr(self.s, "elevenlabs_voice", "") or "").strip()
        if len(want) >= 18 and want.isalnum():  # an id as it is: no need to list the account's voices
            return want
        voices = self.voices()
        if not voices:
            raise VoiceError("The ElevenLabs account has no voices. Add one at elevenlabs.io (Voices).")
        if want:
            for v in voices:
                if str(v.get("name", "")).strip().lower() == want.lower():
                    return str(v["voice_id"])
            for v in voices:
                if want.lower() in str(v.get("name", "")).lower():
                    return str(v["voice_id"])
            names = ", ".join(str(v.get("name")) for v in voices[:8])
            raise VoiceError(f"The ElevenLabs account has no voice called \"{want}\". It has: {names}.")
        for v in voices:
            if str((v.get("labels") or {}).get("gender", "")).lower() == voice.gender:
                return str(v["voice_id"])
        return str(voices[0]["voice_id"])

    def prepare(self, voice: VoiceProfile) -> None:
        self._voice_id = self.voice_id(voice)
        self.log("ElevenLabs reads this narration: the script's text is sent to elevenlabs.io with your key.")

    def speak(self, text: str, voice: VoiceProfile, speed: float) -> tuple[np.ndarray, int]:
        vid = getattr(self, "_voice_id", "") or self.voice_id(voice)
        body = {"text": text[:ELEVEN_CHARS],
                "model_id": str(getattr(self.s, "elevenlabs_model", "") or ELEVEN_MODEL),
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75,
                                   "speed": round(max(ELEVEN_SPEED[0], min(ELEVEN_SPEED[1], speed)), 2)}}
        data = self._call(f"/text-to-speech/{urllib.parse.quote(vid)}?output_format=mp3_44100_128", body, "audio/mpeg")
        return decode(data), ELEVEN_RATE


CLOUD = {"elevenlabs": (ElevenLabsEngine, "ElevenLabs")}


def register() -> None:
    """Make the cloud engines known to the Creator's voice code (it then reads, re-reads and names them like
    its own), marked so "auto" never picks one."""
    for key, (cls, name) in CLOUD.items():
        voice_engine.ENGINE_CLASSES.setdefault(key, cls)
        voice_engine.ENGINE_NAMES.setdefault(key, name)
        voice_engine.CLOUD_ENGINES.add(key)


register()


def engines(s) -> list[dict]:
    """Every voice engine and whether it can speak here: [{"key", "name", "cloud", "ready", "why"}], local first."""
    have = voice_engine.available(s)
    return [{"key": k, "name": voice_engine.ENGINE_NAMES[k], "cloud": k in voice_engine.CLOUD_ENGINES,
             "ready": not have[k], "why": have[k]} for k in voice_engine.ENGINE_CLASSES]


def order(s, choice: str = "") -> list[str]:
    """The engines to try for a narration: `choice` (or Settings → Creator → Voice engine) first, then the
    local ones that work. A cloud engine is only ever there because it was chosen."""
    choice = (choice or "").strip().lower()
    if choice and choice != "auto" and choice not in voice_engine.ENGINE_CLASSES:
        raise VoiceError(f"There's no voice engine called \"{choice}\". Use one of: auto, "
                         + ", ".join(voice_engine.ENGINE_CLASSES) + ".")
    return voice_engine.pick_engines(replace(s, creator_tts=choice) if choice else s)


def narrate(scenes, s, out: Path, *, engine: str = "", voice: str = "", speed: float | None = None,
            log: Log | None = None, progress: Progress | None = None, check: Callable[[], None] | None = None,
            words: bool = True) -> VoiceTrack:
    """Read `scenes` (anything with scene_id and speech_text) into one WAV at `out`, with word times.
    `engine`: a voice engine's key ("" = Settings'). Raises VoiceError."""
    log = log or (lambda m: None)
    want = order(s, engine)
    track = voice_engine.synthesize(scenes, s, Path(out), voice=voice, speed=speed, log=log, progress=progress,
                                    check=check, engines=want)
    if want and want[0] in voice_engine.CLOUD_ENGINES and track.engine_key != want[0]:
        why = voice_engine.available(s).get(want[0], "")
        log(f"{voice_engine.ENGINE_NAMES[want[0]]} didn't read it" + (f" ({why})" if why else "")
            + f", so {track.engine} on this computer did.")
    return voice_engine.time_words(track, s, log, progress) if words else track


def retime(track: VoiceTrack, speed: float, out: Path | None = None) -> VoiceTrack:
    """`track` played `speed` times as fast (0.5-2) at the same pitch, every scene and word time moved with
    it. For narrations that cost money to read again."""
    speed = max(0.5, min(2.0, float(speed)))
    if abs(speed - 1.0) < 0.005:
        return track
    src = Path(track.path)
    dest = Path(out) if out else src
    tmp = dest.with_name(dest.stem + ".retimed.wav")
    done = subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-i", str(src), "-af", f"atempo={speed:.4f}", "-ac", "1",
                           "-c:a", "pcm_s16le", str(tmp)], capture_output=True, text=True, creationflags=NO_WINDOW)
    if done.returncode != 0 or not tmp.is_file():
        raise VoiceError("The narration couldn't be fitted to its length (" + (done.stderr.strip()[-160:] or "ffmpeg") + ").")
    audio, rate = voice_engine.read_wav(tmp)
    os.replace(tmp, dest)
    k = 1.0 / speed
    return replace(track, path=dest, duration=len(audio) / rate, rate=rate,
                   scenes=[replace(sc, start=round(sc.start * k, 3), end=round(sc.end * k, 3)) for sc in track.scenes],
                   words=[Word(round(w.start * k, 3), round(w.end * k, 3), w.text) for w in track.words],
                   speed=track.speed * speed)


def export(track: VoiceTrack, folder: Path, name: str = "voice") -> tuple[Path, Path]:
    """Keep the narration with its video: `name`.wav and `name`.words.json (every word's start and end, each
    scene's span, the engine and voice). Returns the two paths."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    wav = folder / f"{name}.wav"
    if Path(track.path).resolve() != wav.resolve():
        wav.write_bytes(Path(track.path).read_bytes())
    data = {"audio": wav.name, "duration": round(track.duration, 3), "rate": track.rate, "engine": track.engine,
            "voice": track.voice, "speed": round(track.speed, 3), "timing": track.timing,
            "scenes": [{"scene_id": sc.scene_id, "start": sc.start, "end": sc.end, "text": sc.text}
                       for sc in track.scenes],
            "words": [{"text": w.text, "start": round(w.start, 3), "end": round(w.end, 3)} for w in track.words]}
    words = folder / f"{name}.words.json"
    words.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return wav, words


def load(words_file: Path) -> VoiceTrack:
    """A narration kept by export(), read back (to render its timeline again)."""
    words_file = Path(words_file)
    data = json.loads(words_file.read_text(encoding="utf-8"))
    return VoiceTrack(path=words_file.parent / str(data.get("audio") or "voice.wav"),
                      duration=float(data.get("duration") or 0.0), rate=int(data.get("rate") or 0),
                      scenes=[voice_engine.SceneVoice(int(d["scene_id"]), str(d.get("text", "")), float(d["start"]),
                                                      float(d["end"])) for d in data.get("scenes") or []],
                      words=[Word(float(d["start"]), float(d["end"]), str(d["text"])) for d in data.get("words") or []],
                      engine=str(data.get("engine", "")), voice=str(data.get("voice", "")),
                      speed=float(data.get("speed") or 1.0), timing=str(data.get("timing", "")))
