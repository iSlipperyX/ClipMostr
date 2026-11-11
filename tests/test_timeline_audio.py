"""The timeline's sound: the three buses, the ducking's depth on real narration, the loudness it lands on, and
where the automatic effects go."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from ytclip.audio import mastering
from ytclip.editor import audio_mixer as A
from ytclip.editor import timeline_compiler as C
from ytclip.ffmpeg import ffmpeg_exe
from ytclip.studio import timeline_schema as T

NARRATION = Path(__file__).parent / "data" / "narration.flac"  # 6.6 s of the Kokoro narrator (made here)
SPEECH = [(0.2, 2.5), (3.3, 6.4)]  # when someone is speaking in it (clear of its edges and the pause between)
RATE = 48000
TONE = 6000  # Hz: the "music" is a steady tone the voice has nothing near, so its level can be read off the mix


def tone(path: Path, seconds: float, db: float = -14.0, hz: int = TONE) -> Path:
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f={hz}:r={RATE}:d={seconds}",
                    "-af", f"volume={db}dB", "-ac", "2", str(path)], check=True)
    return path


def samples(path: Path) -> np.ndarray:
    raw = subprocess.run([ffmpeg_exe(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE), "-f",
                          "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def level_of(x: np.ndarray, hz: int = TONE) -> tuple[np.ndarray, np.ndarray]:
    """(times, dB) of one frequency through the sound."""
    n, hop = 4096, 1024
    k = round(hz * n / RATE)
    win = np.hanning(n)
    lev = np.array([20 * np.log10(np.abs(np.fft.rfft(x[i:i + n] * win)[k - 1:k + 2]).max() + 1e-9)
                    for i in range(0, len(x) - n, hop)])
    return np.arange(len(lev)) * hop / RATE + n / 2 / RATE, lev


def loudness(path: Path) -> dict:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af",
                          mastering.loudnorm(None), "-f", "null", "-"], capture_output=True, text=True)
    return mastering.parse_measurement(out.stderr)


def timeline(tmp_path: Path, *, music: bool = True, voice_at: float = 1.0, total: float = 9.6, **master) -> T.Timeline:
    tl = T.standard("sound", T.Canvas(180, 320, 10), total)
    tl.assets["black"] = T.Asset("color", color="#101010")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("bg", "black", 0, total))
    tl.assets["voice"] = T.Asset("audio", str(NARRATION), duration=6.6)
    tl.track(T.AUDIO_VOICE).clips.append(T.AudioClip("voice", "voice", voice_at, 6.6))
    if music:
        tl.assets["bed"] = T.Asset("audio", str(tone(tmp_path / "tone.wav", total)), duration=total)
        tl.track(T.AUDIO_MUSIC).clips.append(T.AudioClip("bed", "bed", 0, total))
    tl.audio = T.AudioMaster(**master)
    return tl


def music_under_speech(mix: Path, voice_at: float = 1.0) -> tuple[float, float]:
    """(how far the tone drops while someone speaks, how far it is from its open level a second after)."""
    t, lev = level_of(samples(mix))
    before = np.median(lev[(t > 0.2) & (t < voice_at - 0.2)])
    speaking = np.zeros(len(t), bool)
    for a, b in SPEECH:
        speaking |= (t > voice_at + a + 0.15) & (t < voice_at + b)
    after = np.median(lev[t > voice_at + 6.6 + 1.2])
    return float(np.median(lev[speaking]) - before), float(after - before)


# ---------------------------------------------------------------- the graph

def test_the_graph_is_three_buses_and_a_master():
    src = [A.Source("0:a", "voice", 0.2, 5.0), A.Source("1:a", "voice", 5.4, 3.0),
           A.Source("2:a", "music", 0.0, 9.0, gain_db=-8, fade_out=1.5),
           A.Source("3:a", "sfx", 2.0, 0.5, gain_db=-9), A.Source("4:a", "sfx", 2.2, 0.5, skip=0.25)]
    g = A.graph(src, T.AudioMaster(), 9.0, voice_gain_db=3.5, measured=None)
    parts = g.split(";")
    assert parts[0].startswith("[0:a]atrim=duration=5.000,asetpts=PTS-STARTPTS,") and "adelay=200:all=1[voice0]" in parts[0]
    assert "[voice0][voice1]amix=inputs=2:normalize=0" in g
    # The voice: high-pass, soft-knee compression, then its gain; then the whole length of the video.
    assert f"{A.VOICE_HIGHPASS},{mastering.COMPRESSOR},volume=3.50dB,apad=whole_dur=9.000,atrim=duration=9.000[voicebus]" in g
    assert "knee=6" in mastering.COMPRESSOR
    assert "volume=-8.00dB,afade=t=out:st=7.500:d=1.500[music0]" in g
    assert "[4:a]atrim=start=0.250:duration=0.500" in g
    assert "[voicebus]asplit=2[voicemain][voicekey]" in g and "[musicbus][voicekey]sidechaincompress=" in g
    assert "attack=50:release=300" in g
    assert "[voicemain][ducked][sfxbus]amix=inputs=3:normalize=0:dropout_transition=0:duration=first," in g
    assert "loudnorm=I=-14:TP=-1" in parts[-1] and "alimiter=limit=0.871" in parts[-1] and parts[-1].endswith("[aout]")


def test_the_graph_without_a_voice_or_without_anything():
    music = [A.Source("0:a", "music", 0.0, 4.0)]
    assert "sidechaincompress" not in A.graph(music, T.AudioMaster(), 4.0)
    both = [*music, A.Source("1:a", "voice", 0.0, 4.0)]
    assert "sidechaincompress" not in A.graph(both, T.AudioMaster(duck_db=0), 4.0)  # ducking switched off
    assert "sidechaincompress" in A.graph(both, T.AudioMaster(duck_db=-6), 4.0)
    assert A.graph([], T.AudioMaster(), 4.0) == "anullsrc=r=48000:cl=stereo,atrim=duration=4.000[aout]"
    assert A.voice_graph(music, T.AudioMaster(), 4.0) is None
    assert "loudnorm=I=-18" in A.graph(music, T.AudioMaster(target_lufs=-18), 4.0)
    late = [A.Source("0:a", "music", 5.0, 4.0)]  # starts after the end: left out
    assert A.graph(late, T.AudioMaster(), 4.0).startswith("anullsrc")


def test_the_ducking_threshold_follows_the_depth_asked_for():
    def threshold(master):
        return float(A.duck_filter(master).split("threshold=")[1].split(":")[0])

    deep, usual, light = threshold(T.AudioMaster(duck_db=-24)), threshold(T.AudioMaster()), threshold(T.AudioMaster(duck_db=-6))
    assert deep < usual < light
    # 16 dB down at 20:1 means speech sits 16 / 0.95 dB over the threshold.
    want = 10 ** ((-14 + A.SPEECH_ABOVE_LUFS - 16 / 0.95) / 20)
    assert usual == pytest.approx(want, rel=1e-3)
    assert threshold(T.AudioMaster(voice_lufs=-20)) == pytest.approx(want * 10 ** (-6 / 20), rel=1e-3)


def test_voice_gain_from_its_measurement():
    assert A.voice_gain({"input_i": "-19.5"}, T.AudioMaster()) == pytest.approx(5.5)
    assert A.voice_gain({"input_i": "-70"}, T.AudioMaster()) == 30.0  # never more than 30 dB either way
    assert A.voice_gain(None, T.AudioMaster()) == 0.0 and A.voice_gain({"input_i": "quiet"}, T.AudioMaster()) == 0.0


# ---------------------------------------------------------------- what it sounds like

@pytest.mark.parametrize("asked", [-16.0, -8.0])
def test_music_ducks_as_far_as_asked_under_real_narration(tmp_path, asked):
    tl = timeline(tmp_path, duck_db=asked)
    report = C.render(tl, tmp_path / "out.mp4", base=tmp_path, work=tmp_path / "work")
    drop, after = music_under_speech(tmp_path / "out.mp4")
    assert abs(drop - asked) < 2.0, (drop, asked)
    assert abs(after) < 1.5  # and comes back up once the voice stops
    assert report.voice_gain_db != 0 and report.mix_lufs_before is not None


def test_the_mix_lands_on_its_loudness_and_under_its_peak(tmp_path):
    tl = timeline(tmp_path)
    C.render(tl, tmp_path / "out.mp4", base=tmp_path, work=tmp_path / "work")
    got = loudness(tmp_path / "out.mp4")
    assert abs(float(got["input_i"]) - (-14.0)) < 1.0
    assert float(got["input_tp"]) <= -0.5
    quiet = timeline(tmp_path, target_lufs=-23.0, true_peak=-3.0)
    C.render(quiet, tmp_path / "quiet.mp4", base=tmp_path, work=tmp_path / "work2")
    got = loudness(tmp_path / "quiet.mp4")
    assert abs(float(got["input_i"]) - (-23.0)) < 1.0 and float(got["input_tp"]) <= -2.5


def test_a_video_clips_own_sound_goes_to_its_bus(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run([ffmpeg_exe(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=180x320:r=10:d=4", "-f",
                    "lavfi", "-i", f"sine=f={TONE}:r={RATE}:d=4", "-shortest", "-pix_fmt", "yuv420p", str(clip)],
                   check=True)
    tl = T.standard("own sound", T.Canvas(180, 320, 10), 4.0)
    tl.assets["clip"] = T.Asset("video", "clip.mp4", duration=4.0)
    tl.track(T.VIDEO_BASE).clips += [T.VideoClip("a", "clip", 0, 2.0, audio="sfx"),
                                     T.VideoClip("b", "clip", 2.0, 2.0, source_in=2.0)]  # b is muted
    compiled = C.compile_timeline(tl, tmp_path)
    assert [s.role for s in compiled.sources] == ["sfx"] and len(compiled.audio_inputs) == 1
    C.render(tl, tmp_path / "out.mp4", base=tmp_path, work=tmp_path / "work")
    t, lev = level_of(samples(tmp_path / "out.mp4"))
    assert np.median(lev[(t > 0.3) & (t < 1.7)]) - np.median(lev[(t > 2.4) & (t < 3.6)]) > 40  # then silence


# ---------------------------------------------------------------- automatic effects

def marked() -> T.Timeline:
    tl = T.standard("fx", T.Canvas(180, 320, 10), 20.0)
    tl.assets["black"] = T.Asset("color", color="#101010")
    cut, fade = T.Transition(), T.Transition("crossfade", 0.3)
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("a", "black", 0, 4), T.VideoClip("b", "black", 4, 4, transition_in=fade),
        T.VideoClip("c", "black", 8, 4, transition_in=cut), T.VideoClip("d", "black", 12, 0.6, transition_in=fade),
        T.VideoClip("e", "black", 12.6, 7.4, transition_in=T.Transition("whip_left", 0.2))]
    tl.markers += [T.Marker(0.0, "hook"), T.Marker(8.0, "retention", "the twist"), T.Marker(16.0, "payoff"),
                   T.Marker(19.9, "retention"), T.Marker(10.0, "note")]
    return tl


def test_effects_are_planned_where_they_belong():
    plan = A.plan_sfx(marked(), {"whoosh": 0.5, "riser": 1.2})
    got = [(p.t, p.cue) for p in plan]
    assert got == [(0.0, "bass_drop"),  # the hook, once (the marker at 0 is the same moment)
                   (3.775, "whoosh"),  # peaking on the change of picture at 4 s
                   (8.0, "impact"),  # the retention beat (a plain cut gets no whoosh)
                   (11.775, "whoosh"),  # into d; the whoosh into e, 0.6 s later, would land on top of it
                   (14.8, "riser")]  # ends on the payoff; the marker at 19.9 s is too near the end
    assert plan[2].why == "the twist"
    taken = marked()
    taken.assets["mine"] = T.Asset("audio", "mine.wav")
    taken.track(T.AUDIO_SFX).clips.append(T.AudioClip("mine", "mine", 3.5, 1.0))
    assert (3.775, "whoosh") not in [(p.t, p.cue) for p in A.plan_sfx(taken, {"whoosh": 0.5})]  # yours stays alone


def test_effects_are_placed_on_the_timeline_and_render(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "local"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tl = marked()
    added = A.place_sfx(tl, seed="fx")
    assert [c.id for c in added] == ["auto_bass_drop_1", "auto_whoosh_2", "auto_impact_3", "auto_whoosh_4", "auto_riser_5"]
    assert len({c.asset for c in added}) == 4 and all(Path(tl.assets[c.asset].path).is_file() for c in added)
    assert all(tl.assets[c.asset].source == "made here" and tl.assets[c.asset].duration > 0.2 for c in added)
    assert added[0].gain_db < 0 and added[1].label == "a change of picture"
    assert A.place_sfx(tl, seed="fx") == []  # nothing lands on what's already there
    assert not [p for p in T.validate(tl, files=True) if p.level == "error"]
    C.render(tl, tmp_path / "out.mp4", base=tmp_path, work=tmp_path / "work")
    x = samples(tmp_path / "out.mp4")
    rms = lambda a, b: float(np.sqrt(np.mean(x[int(a * RATE):int(b * RATE)] ** 2)))  # noqa: E731
    assert rms(0.0, 0.5) > 20 * rms(5.5, 7.0)  # the bass drop, then nothing until the next effect
