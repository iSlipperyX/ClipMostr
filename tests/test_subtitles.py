import pytest

from ytclip.captions import HOLD, build_ass, caption_events
from ytclip.download import _pick_caption_lang
from ytclip.settings import Settings
from ytclip.subtitles import Word, clean_text, parse_vtt, parse_vtt_words

# The shape of YouTube's rolling auto-captions, taken from a real download: ">>" speaker marks
# written as "&gt;&gt;", punctuation as its own timed token, a timed line that ends on a bare
# stamp (" me for" only appears in the next cue's untimed repeat), and a long gap between words.
ROLLING = """WEBVTT
Kind: captions
Language: en

00:00:02.460 --> 00:00:04.315 align:start position:0%
 above like that?
&gt;&gt; I<00:00:02.574><c> don't</c><00:00:02.918><c> know</c><00:00:03.148><c>,</c><00:00:03.262><c> man</c><00:00:03.434><c>.</c><00:00:03.592><c> Don't</c><00:00:03.833><c> blame</c><00:00:04.074>

00:00:04.315 --> 00:00:04.355 align:start position:0%
&gt;&gt; I don't know, man. Don't blame me for


00:00:04.355 --> 00:00:04.557 align:start position:0%
&gt;&gt; I don't know, man. Don't blame me for
 that<00:00:04.516><c>.</c>

00:00:04.557 --> 00:00:08.000 align:start position:0%
 that.
&gt;&gt; Too<00:00:04.823><c> showy</c><00:00:05.203><c>.</c><00:00:07.750><c> Immediately</c>
"""


def _write(tmp_path, text):
    path = tmp_path / "v.en.vtt"
    path.write_text(text, encoding="utf-8")
    return path


def test_clean_text_decodes_entities_and_drops_speaker_marks():
    assert clean_text("&gt;&gt; Yes") == "Yes"
    assert clean_text("&amp;gt;&amp;gt; Yes  &amp;amp; no") == "Yes & no"  # encoded twice
    assert clean_text("<c>Tom</c> &amp; Jerry") == "Tom & Jerry"
    assert clean_text("a >> b") == "a b"
    assert clean_text("x>>y 2 > 1") == "x>>y 2 > 1"  # only stand-alone marks go


def test_rolling_captions_words(tmp_path):
    words = parse_vtt_words(_write(tmp_path, ROLLING))
    texts = [w.text for w in words]
    assert texts == ["I", "don't", "know,", "man.", "Don't", "blame", "me", "for", "that.", "Too", "showy.",
                     "Immediately"]
    assert not any(">" in t or "&" in t for t in texts)
    starts = [w.start for w in words]
    assert starts == sorted(starts)
    me, for_ = words[6], words[7]
    assert 4.074 <= me.start < for_.start <= 4.355  # recovered from the repeat, after "blame"
    assert words[2].end >= 3.148  # "know," lasts until its comma
    assert all(w.end > w.start for w in words)


def test_cues_have_no_entities(tmp_path):
    cues = parse_vtt(_write(tmp_path, ROLLING))
    assert cues[0].text.startswith("above like that? I don't know")
    assert not any("&" in c.text or ">" in c.text for c in cues)


def test_words_under_one_stamp_are_spread(tmp_path):
    vtt = """WEBVTT

00:00:01.000 --> 00:00:03.000
Hello there my<00:00:02.000><c> friend</c>
"""
    words = parse_vtt_words(_write(tmp_path, vtt))
    assert [w.text for w in words] == ["Hello", "there", "my", "friend"]
    assert [round(w.start, 2) for w in words] == [1.0, 1.33, 1.67, 2.0]


def test_leading_punctuation_is_dropped(tmp_path):
    vtt = """WEBVTT

00:00:01.000 --> 00:00:02.000
- well, okay
"""
    assert [w.text for w in parse_vtt_words(_write(tmp_path, vtt))] == ["well,", "okay"]


def test_caption_holds_over_short_gaps():
    s = Settings()
    s.caption_words = 3
    words = [Word(0.0, 0.4, "Too"), Word(0.4, 0.8, "showy."), Word(5.0, 5.5, "Immediately")]
    events = caption_events(words, 0.0, 6.0, s)
    showy = next(e for e in events if e.words[e.active].lower() == "showy.")
    assert showy.end == 0.8 + HOLD  # stays up a while, not until the next word 4 s later
    words[2] = Word(1.5, 2.0, "Immediately")
    events = caption_events(words, 0.0, 6.0, s)
    showy = next(e for e in events if e.words[e.active].lower() == "showy.")
    assert showy.end == 1.5  # but never over the next caption


def test_caption_styles_let_libass_pick_the_text_direction():
    # Encoding -1: Arabic or Hebrew captions read right to left in the right order
    ass = build_ass([Word(0, 0.5, "مرحبا")], 0, 1, 1080, 1920, Settings(), title="x")
    styles = [ln for ln in ass.splitlines() if ln.startswith("Style:")]
    assert styles and all(ln.endswith(",-1") for ln in styles)


TRACKS = {"en": [], "ar": [], "fr": [], "id": []}


@pytest.mark.parametrize("info,choice", [
    # YouTube guessed Arabic for an English interview: use the English translation, not Arabic
    ({"language": "en", "automatic_captions": {"ar-orig": [], **TRACKS}}, ("en", True)),
    ({"language": "en", "automatic_captions": {"en-orig": [], **TRACKS}}, ("en-orig", True)),
    ({"language": "en", "subtitles": {"en-GB": [], "live_chat": []}, "automatic_captions": {"en-orig": []}},
     ("en-GB", False)),
    ({"language": "id", "subtitles": {"id": []}, "automatic_captions": {"en-orig": []}}, ("id", False)),
    ({"language": "id", "automatic_captions": {"en-orig": [], **TRACKS}}, ("en-orig", True)),
    ({"automatic_captions": {"fr-orig": [], **TRACKS}}, ("fr-orig", True)),  # language unknown
    ({"language": "de", "subtitles": {"es": []}}, ("es", False)),
    ({"language": "en", "automatic_captions": {"ar-orig": []}}, ("ar-orig", True)),  # nothing else
    ({"language": "en", "subtitles": {"live_chat": []}}, None),
])
def test_caption_track_choice(info, choice):
    assert _pick_caption_lang(info) == choice
