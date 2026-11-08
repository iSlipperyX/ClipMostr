"""The pro edit (Settings → Editing): dead-air and filler cuts, the camera, kinetic captions, cards, B-roll,
the sound (bleeps, ducking, mastering), Creator Rewards length, the offline signals, and one clip
rendered end to end."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ytclip import captions
from ytclip.analyze import Clip
from ytclip.audio import ducking, mastering, silence_remover
from ytclip.compliance import duration_guard
from ytclip.ffmpeg import ffmpeg_exe, probe
from ytclip.intelligence import sentiment, viral_heuristics
from ytclip.rendering import ass_builder, cards, filtergraph
from ytclip.rendering.filtergraph import AudioPlan
from ytclip.settings import PRESETS, PRO_EDIT, Settings
from ytclip.subtitles import Cue, Word
from ytclip.vision import broll_selector, camera


def words_from(text: str, start: float = 0.0, step: float = 0.4, length: float = 0.3,
               pauses: dict[int, float] | None = None) -> list[Word]:
    """Words `step` apart; `pauses` adds seconds of silence before word i."""
    out, t = [], start
    for i, w in enumerate(text.split()):
        t += (pauses or {}).get(i, 0.0)
        out.append(Word(round(t, 3), round(t + length, 3), w))
        t += step
    return out


# ---------------------------------------------------------------- dead air and fillers


def test_long_pauses_shrink_to_a_breath():
    words = words_from("one two three four", pauses={2: 1.0})  # a 1.1 s gap before "three"
    cuts = silence_remover.cuts_for(words, 0.0, 3.0, fillers=False, longest=0.40, keep=0.12)
    assert len(cuts) == 2  # the pause, and the air after the last word
    gap = cuts[0]
    assert gap.why == "pause"
    assert gap.start == pytest.approx(words[1].end + 0.06) and gap.end == pytest.approx(words[2].start - 0.06)
    spans = silence_remover.keep_spans(0.0, 3.0, cuts)
    # What's left between "two" and "three" is 120 ms.
    joined = silence_remover.to_output(words[2].start, spans) - silence_remover.to_output(words[1].end - 1e-6, spans)
    assert joined == pytest.approx(0.12, abs=0.01)


def test_fillers_go_but_meaningful_words_stay():
    words = words_from("so um I like it, it was, like, huge you know")
    words[6] = Word(words[6].start, words[6].end, "like,")
    found = silence_remover.find_fillers(words, silence_remover.filler_list(silence_remover.DEFAULT_FILLERS))
    picked = [" ".join(w.text for w in words[a:b + 1]) for a, b in found]
    assert "um" in picked and "like," in picked
    assert "like" not in picked  # "I like it" runs straight on
    assert "you know" not in picked  # not set apart: no pause or comma before it


def test_frame_snapped_spans_and_word_remapping():
    cuts = [silence_remover.Cut(1.013, 2.207, "pause")]
    spans = silence_remover.keep_spans(0.0, 4.0, cuts, fps=30)
    for a, b in spans:
        assert round(a * 30, 6) == round(a * 30) and round(b * 30, 6) == round(b * 30)
    assert silence_remover.length(spans) == pytest.approx(4.0 - (2.2 - 1.0), abs=1 / 30)
    words = [Word(0.2, 0.6, "keep"), Word(1.3, 1.6, "gone"), Word(2.5, 2.9, "later")]
    out = silence_remover.remap_words(words, spans)
    assert [w.text for w in out] == ["keep", "later"]
    assert out[1].start == pytest.approx(2.5 - 1.2, abs=0.04)
    assert silence_remover.to_source(silence_remover.to_output(3.0, spans), spans) == pytest.approx(3.0)


# ---------------------------------------------------------------- camera


def test_camera_eases_ignores_jitter_and_cuts_on_a_new_speaker():
    fps, n = 30, 300
    times = np.arange(n) / fps
    xs = np.full(n, 0.30) + np.random.default_rng(1).normal(0, 0.005, n)  # a little detector jitter
    xs[150:] = 0.70  # the other speaker
    ys = np.full(n, 0.4)
    jumps = np.zeros(n, bool)
    cx, _ = camera.follow(xs, ys, jumps, times, [], fps, alpha=0.08, deadband=0.04)
    assert np.ptp(cx[10:150]) < 1e-9  # jitter inside the deadband: the camera holds still
    assert cx[151] - cx[150] < 0.06 and cx[220] > 0.65  # eases across...
    assert np.all(np.diff(cx[150:]) >= -1e-9)
    jumps[150] = True
    cut_x, _ = camera.follow(xs, ys, jumps, times, [], fps)
    assert cut_x[150] == pytest.approx(0.70)  # ...or cuts, when the tracker flags a jump


def test_smoothing_is_the_same_speed_at_any_frame_rate():
    a30 = camera.alpha_for(0.08, 30)
    a60 = camera.alpha_for(0.08, 60)
    assert a30 == pytest.approx(0.08)
    assert (1 - a60) ** 2 == pytest.approx(1 - a30)


def test_punch_ins_alternate_on_sentences_and_push_ins_ease():
    words = words_from("This is one. And then two. Three!", step=0.5)
    starts = camera.sentence_starts(words)
    assert starts == [words[0].start, words[3].start, words[6].start]
    times = np.arange(0, 4, 0.1)
    z = camera.punch_path(times, starts, 1.08)
    assert z[0] == 1.0 and z[int(words[3].start * 10) + 1] == 1.08 and z[-1] == 1.0
    slow = camera.slow_path(np.array([0.0, 5.0, 10.0, 12.0]), [0.0], 1.15)
    assert slow[0] == 1.0 and 1.0 < slow[1] < 1.15 and slow[2] == pytest.approx(1.15) and slow[3] == pytest.approx(1.15)


def test_crop_window_stays_inside_the_frame():
    w = camera.window(0.98, 0.5, 1.08, 1920, 1080, 9 / 16)
    assert w.x + w.w <= 1920 + 1e-6 and w.h == pytest.approx(1080 / 1.08)
    w = camera.window(0.5, 0.0, 5.0, 1920, 1080, 9 / 16)  # zoom capped
    assert w.h == pytest.approx(1080 / camera.MAX_ZOOM) and w.y == 0


# ---------------------------------------------------------------- captions


def test_keyword_colours():
    assert [ass_builder.keyword_kind(w) for w in ("$500", "money", "50k", "dead", "why", "really?", "table")] == \
        ["money", "money", "money", "danger", "question", "question", ""]


def test_kinetic_captions_pop_highlight_and_sit_in_the_safe_zone():
    s = Settings(caption_style="kinetic", caption_words=5, burn_captions=True, caption_highlight="#FFD400")
    assert captions.words_per_group(s) == 3
    st = captions.caption_style(s, 1080, 1920)
    assert st.align == 2 and 1920 - st.margin_v == 1200  # the block ends at y = 1200
    words = words_from("I made a million dollars, why?")
    ass = captions.build_ass(words, 0.0, 4.0, 1080, 1920, s)
    assert "\\fscx80\\fscy80\\t(0,80,\\fscx110\\fscy110)" in ass  # 80% → 110% → settle
    green = "&H0066FF00"  # #00FF66 as ASS BGR
    assert green.upper() in ass.upper() and "&H0000E6FF" in ass.upper()  # money green, question yellow
    for line in ass.splitlines():
        if line.startswith("Dialogue:"):
            assert len(re.findall(r"\}[^{]+", line.split(",,", 1)[1])) <= 3


# ---------------------------------------------------------------- cards


def test_hook_card_and_lower_third():
    s = Settings(banner_font="TikTok Sans Black", intro_seconds=1.8, lower_third_text="{topic}")
    clip = Clip(0, 60, 80, "How I lost it all")
    clip.hook = "He lost a million in a day"
    ass = cards.build(1080, 1920, s, clip, intro=True, lower=True, channel="Some Podcast", topic="Money stories",
                      duration=60)
    assert "HE" in ass and "MILLION" in ass  # the headline, word by word
    assert "SOURCE · SOME PODCAST" in ass and "Money stories" in ass
    intro = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 3")]
    assert intro and all(",0:00:00.00,0:00:01.80," in ln for ln in intro)
    lower = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 6")]
    assert lower and all(",0:00:02.20," in ln for ln in lower)
    assert cards.build(1080, 1920, s, clip, intro=False, lower=False) == ""


# ---------------------------------------------------------------- B-roll and music


def test_broll_matches_the_words_and_keeps_clear_of_the_ends(tmp_path):
    for name in ("money/counting cash.mp4", "cars/fast car.mp4", "city night.mov", "notes.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    words = words_from(" ".join(["talk"] * 40 + ["the", "cash", "was", "gone"] + ["talk"] * 40), step=0.5)
    inserts = broll_selector.plan(words, 42.0, tmp_path, every=18, length=2.5, seed="a")
    assert inserts and all(broll_selector.EDGE <= i.start and i.end <= 42.0 - broll_selector.EDGE for i in inserts)
    assert inserts[0].path.name == "counting cash.mp4" and inserts[0].matched == "cash"
    assert all(b.start - a.start >= 18 * 0.6 for a, b in zip(inserts, inserts[1:]))
    assert broll_selector.plan(words, 42.0, tmp_path / "missing") == []


def test_music_follows_the_clips_mood(tmp_path):
    for name in ("Suspense/tense.mp3", "Upbeat/happy.m4a", "chill.wav", "cover.jpg"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"x")
    assert len(ducking.tracks(tmp_path)) == 3
    assert ducking.pick(tmp_path, "Story", "clip-1").name == "tense.mp3"
    assert ducking.pick(tmp_path, "Humor", "clip-1").name == "happy.m4a"
    assert ducking.pick(tmp_path / "nothing", "Story") is None
    assert "sidechaincompress=threshold=0.0316" in ducking.DUCK and "attack=50" in ducking.DUCK \
        and "release=300" in ducking.DUCK


# ---------------------------------------------------------------- sound, rendered by ffmpeg


def _tone(path: Path, seconds: float, freq: int = 300, volume: float = 0.1) -> None:
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency={freq}:duration={seconds}:sample_rate=48000", "-af", f"volume={volume}",
                    str(path)], check=True)


def _loudness(path: Path) -> float:
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null",
                           "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", done.stderr)[-1])


def _audio_seconds(path: Path) -> float:
    done = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path), "-map", "0:a", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    h, m, sec = re.findall(r"time=(\d+):(\d+):([\d.]+)", done.stderr)[-1]
    return int(h) * 3600 + int(m) * 60 + float(sec)


def test_cut_bleeped_ducked_and_mastered_to_minus_14(tmp_path):
    speech, music = tmp_path / "speech.wav", tmp_path / "music.wav"
    _tone(speech, 10, 300, 0.05)
    _tone(music, 4, 900, 0.3)
    plan = AudioPlan(duration=7.0, spans=[(0.0, 3.0), (4.0, 8.0)], bleeps=[(1.0, 1.4)], music=music,
                     music_level_db=-18, master=True)
    inputs = ["-i", str(speech), "-stream_loop", "-1", "-i", str(music)]
    plan.measured = filtergraph.measure(inputs, plan, "0:a", "1:a", None)
    assert plan.measured and float(plan.measured["input_i"]) < -14
    graph, out = filtergraph.chain(plan, "0:a", "1:a")
    assert "concat=n=2:v=0:a=1" in graph and "curve=qsin" in graph and "measured_I=" in graph
    dest = tmp_path / "out.wav"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", f"[{out}]",
                    "-t", "7", str(dest)], check=True)
    assert probe(dest).duration == pytest.approx(7.0, abs=0.05)
    assert _loudness(dest) == pytest.approx(-14.0, abs=1.0)


def test_loudnorm_reads_the_first_pass():
    stderr = 'x\n[Parsed_loudnorm_0 @ 0x1] \n{\n "input_i" : "-27.31",\n "input_tp" : "-9.02",\n ' \
             '"input_lra" : "3.10",\n "input_thresh" : "-37.61",\n "target_offset" : "0.52"\n}\n'
    m = mastering.parse_measurement(stderr)
    assert m["input_i"] == "-27.31"
    second = mastering.loudnorm(m)
    assert "I=-14:TP=-1:LRA=7" in second and "measured_I=-27.31" in second and "linear=true" in second
    assert mastering.parse_measurement(stderr.replace("-27.31", "-inf")) is None
    assert mastering.speech_chain(True) == ["highpass=f=80:poles=2", mastering.COMPRESSOR]


# ---------------------------------------------------------------- Creator Rewards


def test_creator_rewards_lengths_and_stretch():
    assert duration_guard.lengths(20, 60) == (62.0, 88.0)
    assert duration_guard.lengths(70, 120) == (70, 90.0)
    sents = [Cue(i * 10.0, i * 10.0 + 9.0, "x") for i in range(12)]
    clip = Clip(0.0, 59.0, 50)
    cut = 3.0  # the pause cuts take 3 s out
    assert duration_guard.stretch(clip, sents, lambda a, b: b - a - cut, limit=200)
    assert clip.end - clip.start - cut >= duration_guard.MIN_SECONDS
    short = Clip(0.0, 59.0, 50)
    assert not duration_guard.stretch(short, sents, lambda a, b: b - a - cut, limit=60)  # next clip in the way
    assert short.end == 59.0


def test_presets_turn_the_pro_edit_on():
    shorts = Settings().apply(PRESETS["Shorts / TikTok / Reels"])
    assert all(getattr(shorts, k) == v for k, v in PRO_EDIT.items())
    assert Settings().apply(PRESETS["Creator Rewards (over 1 min)"]).creator_rewards
    plain = Settings()
    assert not plain.cut_silences and not plain.track_speaker and not plain.creator_rewards


# ---------------------------------------------------------------- offline signals


def test_sentiment_and_categories():
    assert sentiment.score("I love this so much!") > 0.5
    assert sentiment.score("this is not good") < 0
    assert sentiment.score("the table is brown") == 0
    assert viral_heuristics.guess_category("so then one day I was at work and then we were told") == "Story"
    assert viral_heuristics.guess_category("why? how? what if the secret is real?") == "Curiosity"
    assert viral_heuristics.guess_category("hello there") == ""


def test_voice_and_emotion_signals_find_the_animated_part():
    n = 120
    hop = 0.5
    t = np.arange(int(n / hop))
    centroid = np.full(len(t), 900.0)
    centroid[160:200] = 900 + 600 * np.sin(t[160:200])  # seconds 80-100: pitch swinging about
    db = np.full(len(t), -30.0)
    db[160:200] = -18
    voice = viral_heuristics.voice_signal(centroid, db, n, hop)
    assert voice is not None and len(voice) == n and 80 <= int(np.argmax(voice)) <= 100
    cues = [Cue(i * 10, i * 10 + 9, "and then we went to the shop") for i in range(12)]
    cues[6] = Cue(60, 69, "This is the worst, most disgusting thing I have ever seen!")
    emotion = viral_heuristics.emotion_signal(cues, n)
    assert emotion is not None and 58 <= int(np.argmax(emotion)) <= 70
    assert viral_heuristics.voice_signal(np.zeros(0), np.zeros(0), n) is None


# ---------------------------------------------------------------- one clip, end to end


def test_a_pro_edited_clip_renders(tmp_path):
    pytest.importorskip("cv2")
    from ytclip.export import export_clip
    from ytclip.rendering import plan as edit_plan

    src = tmp_path / "talk.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=14",
                    "-f", "lavfi", "-i", "sine=frequency=220:duration=14:sample_rate=48000", "-af", "volume=0.05",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(src)], check=True)
    media = probe(src)
    words = words_from("So um this is the money story, of how I lost a fucking million dollars. Why? Because "
                       "nobody told me the secret.", start=0.3, step=0.42, length=0.35, pauses={8: 1.5})
    s = Settings(aspect="vertical_crop", burn_captions=True, encoder="libx264", **PRO_EDIT, bleep_audio=True,
                 caption_censor=True)
    clip = Clip(0.0, 13.5, 50, "How I lost a million", category="Story")
    clip.hook = "He lost a million in a day"
    edit = edit_plan.make(src, media, clip, words, s, tmp_path, channel="Test Channel", topic="Money stories")
    assert edit.duration < 13.5 - 1.0 and len(edit.spans) >= 2 and edit.audio.bleeps
    assert edit.video is not None and edit.video.is_file()
    dest = tmp_path / "out.mp4"
    export_clip(src, media, dest, clip, words, s, edit=edit)
    out = probe(dest)
    assert (out.width, out.height) == (1080, 1920)
    assert out.duration == pytest.approx(edit.duration, abs=0.15)
    assert _audio_seconds(dest) == pytest.approx(edit.duration, abs=0.15)  # the sound runs to the end too
    assert _loudness(dest) == pytest.approx(-14.0, abs=1.0)
