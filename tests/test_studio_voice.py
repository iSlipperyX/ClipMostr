"""The Studio's voice: local engines first, a cloud voice only when chosen, ElevenLabs' API as it's called,
fitting a paid narration to its length without paying twice, and the narration kept with its word times."""

import io
import json
import subprocess
import urllib.error
from types import SimpleNamespace as NS

import numpy as np
import pytest

from ytclip.creator import voice_engine as V
from ytclip.ffmpeg import ffmpeg_exe
from ytclip.settings import Settings
from ytclip.studio import voice_studio as VS

SCENES = [NS(scene_id=1, speech_text="This habit rewires your morning."),
          NS(scene_id=2, speech_text="It takes two minutes, and nobody tells you about it."),
          NS(scene_id=3, speech_text="Try it tomorrow. Then tell me what changed.")]


def syllables(text: str, speed: float = 1.0, rate: int = 24000) -> np.ndarray:
    """Something a voice-shaped: a burst of tone for each word, a gap between."""
    out = []
    for i, word in enumerate(text.split()):
        n = int(rate * (0.12 + 0.03 * len(word)) / speed)
        t = np.arange(n) / rate
        out.append((0.5 * np.sin(2 * np.pi * (180 + 20 * (i % 5)) * t) * np.hanning(n)).astype(np.float32))
        out.append(np.zeros(int(rate * 0.06 / speed), np.float32))
    return np.concatenate(out)


class Local(V.Engine):
    """A local engine that always works."""

    key = "kokoro"
    spoken: list = []

    def missing(self) -> str:
        return ""

    def speak(self, text, voice, speed):
        Local.spoken.append((text, round(speed, 2)))
        return syllables(text, speed), 24000


@pytest.fixture
def local(monkeypatch):
    Local.spoken = []
    monkeypatch.setitem(V.ENGINE_CLASSES, "kokoro", Local)
    for key in ("piper", "windows", "espeak"):
        monkeypatch.setitem(V.ENGINE_CLASSES, key, type(key, (Local,), {"key": key, "missing": lambda self: "not here"}))
    return Local


def mp3(seconds: float = 0.8) -> bytes:
    return subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", f"sine=f=220:d={seconds}", "-f", "mp3",
                           "-"], capture_output=True, check=True).stdout


VOICES = {"voices": [{"voice_id": "idRachel0000000000001", "name": "Rachel", "labels": {"gender": "female"}},
                     {"voice_id": "idAdam000000000000002", "name": "Adam (deep)", "labels": {"gender": "male"}},
                     {"name": "broken"}]}


class Service:
    """ElevenLabs, as far as the engine can tell."""

    def __init__(self, fail=None):
        self.calls: list[NS] = []
        self.fail = fail
        self.audio = mp3()

    def __call__(self, url, headers, body, timeout):
        self.calls.append(NS(url=url, headers=headers, body=json.loads(body) if body else None))
        if self.fail is not None:
            raise self.fail
        return json.dumps(VOICES).encode() if url.endswith("/voices") else self.audio


def http_error(code: int, detail) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.elevenlabs.io", code, "x", {}, io.BytesIO(json.dumps({"detail": detail}).encode()))


def eleven(monkeypatch, service, **over) -> VS.ElevenLabsEngine:
    monkeypatch.setattr(VS.ElevenLabsEngine, "http", staticmethod(service))
    s = Settings().apply({"elevenlabs_api_key": " sk-test ", **over})
    return VS.ElevenLabsEngine(s, lambda m: None, lambda m, f: None)


# ---------------------------------------------------------------- which engine

def test_a_cloud_voice_is_never_picked_unless_chosen(local, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    keyed = Settings().apply({"elevenlabs_api_key": "sk-test"})
    assert "elevenlabs" in V.ENGINE_CLASSES and V.CLOUD_ENGINES == {"elevenlabs"}
    assert V.pick_engines(keyed) == ["kokoro"]  # auto: this PC only, key or no key
    assert VS.order(keyed) == ["kokoro"] and VS.order(keyed, "auto") == ["kokoro"]
    assert VS.order(keyed, "elevenlabs") == ["elevenlabs", "kokoro"]  # chosen: first, local voices behind it
    assert V.pick_engines(keyed.apply({"creator_tts": "elevenlabs"})) == ["elevenlabs", "kokoro"]
    with pytest.raises(V.VoiceError, match="no voice engine called \"siri\""):
        VS.order(keyed, "siri")
    listed = {e["key"]: e for e in VS.engines(Settings())}
    assert listed["elevenlabs"]["cloud"] and not listed["elevenlabs"]["ready"] and "no ElevenLabs key" in listed["elevenlabs"]["why"]
    assert listed["kokoro"]["ready"] and not listed["kokoro"]["cloud"] and listed["piper"]["why"] == "not here"
    assert list(listed)[0] == "kokoro"


def test_the_key_comes_from_settings_or_the_environment(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    bare = VS.ElevenLabsEngine(Settings(), lambda m: None, lambda m, f: None)
    assert "no ElevenLabs key" in bare.missing()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "from-env")
    assert bare.missing() == "" and bare.api_key() == "from-env"
    assert "elevenlabs_api_key" in __import__("ytclip.settings", fromlist=["SECRETS"]).SECRETS


# ---------------------------------------------------------------- ElevenLabs

def test_elevenlabs_is_asked_for_one_scene(monkeypatch):
    service = Service()
    eng = eleven(monkeypatch, service, elevenlabs_voice="idAdam000000000000002")
    said = []
    eng.log = said.append
    prof = V.profile("en_male_deep")
    eng.prepare(prof)
    audio, rate = eng.speak("Hello there, world.", prof, 1.5)
    assert rate == 24000 and 0.7 * 24000 < len(audio) < 0.95 * 24000 and float(np.abs(audio).max()) > 0.1
    call = service.calls[-1]
    assert len(service.calls) == 1  # an id needs no list of voices
    assert call.url == "https://api.elevenlabs.io/v1/text-to-speech/idAdam000000000000002?output_format=mp3_44100_128"
    assert call.headers["xi-api-key"] == "sk-test" and call.headers["Content-Type"] == "application/json"
    assert call.body == {"text": "Hello there, world.", "model_id": "eleven_multilingual_v2",
                         "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.2}}  # its fastest
    assert "sent to elevenlabs.io" in said[0]
    eng.speak("Slowly.", prof, 0.5)
    assert service.calls[-1].body["voice_settings"]["speed"] == 0.7


@pytest.mark.parametrize("setting, profile, want", [
    ("", "en_male_deep", "idAdam000000000000002"),  # the first voice of the narrator's gender
    ("", "en_female_warm", "idRachel0000000000001"),
    ("rachel", "en_male_deep", "idRachel0000000000001"),  # by name, however it's typed
    ("adam", "en_female_warm", "idAdam000000000000002"),  # part of a name
])
def test_the_voice_is_one_of_the_accounts(monkeypatch, setting, profile, want):
    service = Service()
    eng = eleven(monkeypatch, service, elevenlabs_voice=setting)
    assert eng.voice_id(V.profile(profile)) == want
    assert eng.voice_id(V.profile(profile)) == want and len(service.calls) == 1  # the list is asked for once
    assert service.calls[0].url.endswith("/v1/voices") and service.calls[0].body is None


def test_a_voice_the_account_doesnt_have_is_named(monkeypatch):
    eng = eleven(monkeypatch, Service(), elevenlabs_voice="Morgan")
    with pytest.raises(V.VoiceError, match='no voice called "Morgan". It has: Rachel, Adam'):
        eng.voice_id(V.profile("en_male_deep"))


@pytest.mark.parametrize("fail, said", [
    (http_error(401, {"status": "invalid_api_key", "message": "Invalid API key"}), "didn't accept the key"),
    (http_error(401, {"status": "quota_exceeded", "message": "You have 12 credits left, this needs 400 (quota)"}),
     "out of credit"),
    (http_error(402, "payment required"), "out of credit"),
    (http_error(429, {"message": "too many"}), "rate limit"),
    (http_error(422, {"message": "voice not found"}), "doesn't have that voice"),
    (http_error(500, "boom"), "answered 500: boom"),
    (urllib.error.URLError("no route"), "couldn't be reached \\(no route\\)"),
])
def test_what_goes_wrong_is_said_plainly(monkeypatch, fail, said):
    eng = eleven(monkeypatch, Service(fail), elevenlabs_voice="idAdam000000000000002")
    with pytest.raises(V.VoiceError, match=said):
        eng.speak("Hello.", V.profile("en_male_deep"), 1.0)


def test_audio_that_isnt_audio_is_refused():
    with pytest.raises(V.VoiceError, match="couldn't be read"):
        VS.decode(b"<html>not an mp3</html>")


# ---------------------------------------------------------------- a narration

def test_a_narration_with_its_word_times(local, tmp_path):
    s = Settings().apply({"creator_word_timing": "sound"})
    track = VS.narrate(SCENES, s, tmp_path / "voice.wav", voice="en_female_warm")
    assert track.path == tmp_path / "voice.wav" and track.path.is_file() and track.engine == "Kokoro-82M"
    audio, rate = V.read_wav(track.path)
    assert rate == 24000 and track.duration == pytest.approx(len(audio) / rate)
    assert [sc.scene_id for sc in track.scenes] == [1, 2, 3] and track.scenes[0].start == pytest.approx(V.LEAD_IN)
    assert [w.text for w in track.words] == " ".join(sc.speech_text for sc in SCENES).split()
    assert all(a.start <= b.start and a.end > a.start for a, b in zip(track.words, track.words[1:]))
    for sc in track.scenes:  # every word inside its scene
        mine = [w for w in track.words if sc.start - 0.06 <= w.start < sc.end]
        assert len(mine) == len(sc.text.split())
    assert track.timing == "from the sound"


def test_a_failed_cloud_voice_falls_back_to_this_pc_and_says_so(local, tmp_path, monkeypatch):
    monkeypatch.setattr(VS.ElevenLabsEngine, "http", staticmethod(Service(http_error(402, "no credit"))))
    s = Settings().apply({"creator_word_timing": "sound", "elevenlabs_api_key": "sk-test",
                          "elevenlabs_voice": "idAdam000000000000002"})
    said = []
    track = VS.narrate(SCENES, s, tmp_path / "voice.wav", engine="elevenlabs", log=said.append)
    assert track.engine_key == "kokoro"
    assert any("ElevenLabs voice didn't work" in m and "out of credit" in m for m in said)
    assert any("so Kokoro-82M on this computer did" in m for m in said)
    nokey = []
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    VS.narrate(SCENES, Settings().apply({"creator_word_timing": "sound"}), tmp_path / "v2.wav", engine="elevenlabs",
               log=nokey.append)
    assert any("ElevenLabs didn't read it (there's no ElevenLabs key" in m for m in nokey)


def test_a_cloud_narration_reads_every_scene_once(local, tmp_path, monkeypatch):
    service = Service()
    monkeypatch.setattr(VS.ElevenLabsEngine, "http", staticmethod(service))
    s = Settings().apply({"creator_word_timing": "sound", "elevenlabs_api_key": "sk-test",
                          "elevenlabs_voice": "idAdam000000000000002"})
    track = VS.narrate(SCENES, s, tmp_path / "voice.wav", engine="elevenlabs")
    assert track.engine == "ElevenLabs" and track.engine_key == "elevenlabs"
    assert [c.body["text"] for c in service.calls] == [sc.speech_text for sc in SCENES]
    assert not local.spoken


def test_retime_fits_a_narration_without_reading_it_again(local, tmp_path):
    s = Settings().apply({"creator_word_timing": "sound"})
    track = VS.narrate(SCENES, s, tmp_path / "voice.wav")
    spoken = len(local.spoken)
    fast = VS.retime(track, 1.25)
    assert len(local.spoken) == spoken  # nothing was read again
    assert fast.path == track.path and fast.duration == pytest.approx(track.duration / 1.25, abs=0.03)
    audio, rate = V.read_wav(fast.path)
    assert len(audio) / rate == pytest.approx(fast.duration)
    assert fast.scenes[2].start == pytest.approx(track.scenes[2].start / 1.25, abs=0.002)
    assert fast.words[-1].end == pytest.approx(track.words[-1].end / 1.25, abs=0.002)
    assert fast.speed == pytest.approx(1.25) and [w.text for w in fast.words] == [w.text for w in track.words]
    assert VS.retime(fast, 1.0) is fast
    # The words are still where the sound is: the last word's burst ends where the times say.
    env = np.abs(audio)
    last = np.nonzero(env > 0.05)[0][-1] / rate
    assert abs(last - fast.scenes[-1].end) < 0.08


def test_the_narration_is_kept_with_its_word_times(local, tmp_path):
    s = Settings().apply({"creator_word_timing": "sound"})
    track = VS.narrate(SCENES, s, tmp_path / "work" / "voice.wav")
    wav, words = VS.export(track, tmp_path / "assets")
    assert wav == tmp_path / "assets" / "voice.wav" and wav.read_bytes() == track.path.read_bytes()
    data = json.loads(words.read_text(encoding="utf-8"))
    assert data["audio"] == "voice.wav" and data["engine"] == "Kokoro-82M" and data["timing"] == "from the sound"
    assert data["words"][0] == {"text": "This", "start": track.words[0].start, "end": round(track.words[0].end, 3)}
    assert len(data["words"]) == len(track.words) and data["scenes"][1]["text"] == SCENES[1].speech_text
    back = VS.load(words)
    assert back.path == wav and back.duration == pytest.approx(track.duration, abs=0.001)
    assert [(w.text, w.start) for w in back.words] == [(w.text, round(w.start, 3)) for w in track.words]
    assert [(sc.scene_id, sc.start, sc.end) for sc in back.scenes] == [(sc.scene_id, sc.start, sc.end) for sc in track.scenes]
