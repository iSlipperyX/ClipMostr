"""The timeline document: what it reads, what it writes back, and what it says when it's wrong."""

import json

import pytest

from ytclip.studio import timeline_schema as T


def full() -> T.Timeline:
    tl = T.standard("3 stoic habits", T.Canvas.named("vertical"), 8.0)
    tl.assets["pic1"] = T.Asset("image", "scenes/scene_01.png", width=1080, height=1920, source="pexels",
                                credit="Photo by A. Person on Pexels", url="https://www.pexels.com/photo/1/",
                                licence="Pexels licence")
    tl.assets["clip"] = T.Asset("video", "stock/waves.mp4", duration=12.0)
    tl.assets["black"] = T.Asset("color", color="#101010")
    tl.assets["voice"] = T.Asset("audio", "assets/voice.flac", duration=7.6)
    tl.assets["bed"] = T.Asset("audio", "assets/music.flac", duration=30.0)
    tl.assets["whoosh"] = T.Asset("audio", "assets/whoosh.wav", duration=0.5)
    base = tl.track(T.VIDEO_BASE)
    base.clips += [
        T.VideoClip("s1", "pic1", 0.0, 4.0, keyframes=[T.Keyframe(0, scale=1.15), T.Keyframe(4.0, scale=1.0)]),
        T.VideoClip("s2", "clip", 4.0, 3.0, source_in=2.0, crop=T.Crop(0.1, 0.0, 0.8, 1.0), audio="sfx",
                    gain_db=-12, transition_in=T.Transition("whip_left", 0.25),
                    keyframes=[T.Keyframe(0, x=0.4), T.Keyframe(3.0, x=0.6, ease="ease_in_out")]),
        T.VideoClip("s3", "black", 7.0, 1.0, transition_in=T.Transition("dip_black", 0.4))]
    tl.track(T.VIDEO_OVERLAY).clips.append(T.VideoClip(
        "logo", "pic1", 1.0, 2.0, fit="contain",
        keyframes=[T.Keyframe(0, 0.8, 0.15, 0.2, 0, 0), T.Keyframe(0.3, 0.8, 0.15, 0.2, 8, 1, "ease_out")]))
    tl.track(T.AUDIO_VOICE).clips.append(T.AudioClip("voice", "voice", 0.2, 7.6))
    tl.track(T.AUDIO_MUSIC).clips.append(T.AudioClip("bed", "bed", 0.0, 8.0, gain_db=-8, fade_out=1.5, loop=True))
    tl.track(T.AUDIO_SFX).clips += [T.AudioClip("w1", "whoosh", 3.8, 0.5), T.AudioClip("w2", "whoosh", 4.0, 0.5)]
    subs = tl.track(T.SUBTITLES)
    subs.style = T.SubtitleStyle(preset="karaoke", max_words=2)
    subs.events.append(T.SubtitleEvent(0.2, 1.1, [
        T.WordEvent("MONEY", 0.2, 0.6, color="#00FF66", emoji="💰", box=[402, 1090, 276, 96]),
        T.WordEvent("TALKS", 0.6, 1.1, emphasis=True)]))
    tl.markers += [T.Marker(0.0, "hook", "The hook"), T.Marker(4.0, "retention")]
    tl.audio = T.AudioMaster(duck_db=-12)
    tl.look = ["vignette"]
    tl.meta = {"sources": [{"url": "https://example.org/a", "title": "An article"}]}
    return tl


def test_round_trip_keeps_everything():
    tl = full()
    again = T.Timeline.loads(tl.dumps())
    assert again == tl
    assert T.Timeline.loads(again.dumps()).dumps() == again.dumps()  # and what's written reads back the same
    assert not [p for p in T.validate(tl) if p.level == "error"]


def test_the_file_is_the_documented_shape():
    data = full().to_json()
    assert data["version"] == 1 and data["canvas"] == {"width": 1080, "height": 1920, "fps": 30, "background": "#000000"}
    assert [t["id"] for t in data["tracks"]] == list(T.STANDARD_TRACKS)
    s1, s2, _ = data["tracks"][0]["clips"]
    assert s1 == {"id": "s1", "asset": "pic1", "start": 0.0, "duration": 4.0, "keyframes": [
        {"t": 0, "x": 0.5, "y": 0.5, "scale": 1.15, "rotation": 0.0, "opacity": 1.0, "ease": "linear"},
        {"t": 4.0, "x": 0.5, "y": 0.5, "scale": 1.0, "rotation": 0.0, "opacity": 1.0, "ease": "linear"}]}
    assert s2["transition_in"] == {"type": "whip_left", "duration": 0.25} and "transition_out" not in s2
    assert s2["crop"] == {"x": 0.1, "y": 0.0, "w": 0.8, "h": 1.0}
    word = data["tracks"][5]["events"][0]["words"][0]
    assert word == {"text": "MONEY", "start": 0.2, "end": 0.6, "color": "#00FF66", "emoji": "💰",
                    "box": [402, 1090, 276, 96]}
    assert data["tracks"][2]["role"] == "voice" and data["audio"] == {"target_lufs": -14.0, "duck_db": -12}
    assert "💰" in full().dumps()  # written as itself, not \\u escapes


def test_a_keyframe_left_short_carries_on_from_the_one_before():
    data = full().to_json()
    data["tracks"][0]["clips"][0]["keyframes"] = [{"t": 0, "scale": 1.3, "x": 0.4}, {"t": 2}, {"t": 4, "scale": 1.0}]
    data["tracks"][0]["clips"][1]["transition_in"] = "crossfade"  # the short way to write one
    keys = T.Timeline.from_json(data).track(T.VIDEO_BASE).clips[0].keyframes
    assert [(k.scale, k.x) for k in keys] == [(1.3, 0.4), (1.3, 0.4), (1.0, 0.4)]
    assert T.Timeline.from_json(data).track(T.VIDEO_BASE).clips[1].transition_in == T.Transition("crossfade", 0.3)


def problems(data) -> str:
    with pytest.raises(T.TimelineError) as caught:
        T.Timeline.from_json(data)
    return "\n".join(str(p) for p in caught.value.problems)


def test_every_problem_is_named_where_it_is():
    data = full().to_json()
    base = data["tracks"][0]["clips"]
    base[0]["asset"] = "pic9"
    base[1]["start"] = 3.0  # into s1
    base[1]["keyframes"][1]["ease"] = "bouncy"
    base[2]["transition_in"]["type"] = "star_wipe"
    data["tracks"][2]["role"] = "narrator"
    data["tracks"][5]["events"][0]["words"][1]["start"] = "soon"
    data["canvas"]["width"] = 1081
    data["audio"]["duck_db"] = 6
    data["markers"][0]["kind"] = "boom"
    said = problems(data)
    for part in ("tracks[0].clips[0].asset: there's no asset called 'pic9'",
                 "tracks[0].clips[1]: starts at 3.000 s, before clips[0] ends (4.000 s)",
                 "tracks[0].clips[1].keyframes[1].ease: is 'bouncy'",
                 "tracks[0].clips[2].transition_in.type: is 'star_wipe'",
                 "tracks[2].role: is 'narrator': an audio track's role is voice, music or sfx",
                 "tracks[5].events[0].words[1].start", "canvas.width: is 1081", "audio.duck_db: is 6",
                 "markers[0].kind: is 'boom'"):
        assert part in said, part
    with pytest.raises(T.TimelineError, match=r"The timeline has 11 problems: .*; and 3 more"):
        T.Timeline.from_json(data)  # the message itself names the first eight


@pytest.mark.parametrize("change, said", [
    (lambda d: d.update(duration=0), "duration: is 0 s"),
    (lambda d: d.update(version=99), "version: is 99"),
    (lambda d: d.update(tracks={}), "tracks"),
    (lambda d: d["tracks"][0]["clips"][0].update(duration=0.01), "less than one frame"),
    (lambda d: d["tracks"][0]["clips"][1].update(crop={"x": 0.5, "y": 0, "w": 0.8, "h": 1}), "must stay inside the source"),
    (lambda d: d["tracks"][0]["clips"][0].update(audio="voice"), "only a video has sound of its own"),
    (lambda d: d["tracks"][0]["clips"][0]["keyframes"][1].update(t=0), "not after the keyframe before it"),
    (lambda d: d["tracks"][0]["clips"][0]["keyframes"][0].update(opacity=1.5), "opacity runs from 0 to 1"),
    (lambda d: d["tracks"][3]["clips"][0].update(asset="pic1"), "pic1 is an image, which an audio track can't play"),
    (lambda d: d["tracks"][5]["events"][0].update(words=[]), "a caption needs at least one word"),
    (lambda d: d["tracks"][5]["events"][0]["words"][0].update(color="green"), "a colour is written #RRGGBB"),
    (lambda d: d["tracks"][5]["style"].update(preset="disco"), "preset: is 'disco'"),
    (lambda d: d["tracks"][5]["events"][0]["words"][0].update(box=[1, 2, 3]), "should be [x, y, width, height]"),
    (lambda d: d["tracks"][1].update(id="video_track_0"), "two tracks are called video_track_0"),
    (lambda d: d["tracks"][1]["clips"][0].update(id="s1"), "two clips are called s1"),
    (lambda d: d["assets"]["black"].update(color="black"), "assets.black.color"),
    (lambda d: d.update(look=["sepia"]), "look[0]: is 'sepia'"),
])
def test_one_thing_wrong(change, said):
    data = full().to_json()
    change(data)
    assert said in problems(data)


def test_not_json_and_not_an_object():
    with pytest.raises(T.TimelineError, match="isn't valid JSON"):
        T.Timeline.loads("{nope")
    with pytest.raises(T.TimelineError):
        T.Timeline.from_json([1, 2])


def test_warnings_dont_stop_a_timeline():
    data = full().to_json()
    data["tracks"][0]["clips"][2]["start"] = 7.5  # a gap, and it now runs past the end
    data["tracks"][0]["clips"][1]["duration"] = 3.4
    data["tracks"][0]["clips"][1]["source_in"] = 10.0  # past the 12 s of stock
    data["tracks"][0]["clips"][1]["mood"] = "calm"  # a key this version doesn't know
    tl = T.Timeline.from_json(data)
    said = "\n".join(str(p) for p in T.check(tl))
    assert "past the end of the video" in said and "its last frame holds" in said
    assert "nothing plays from 7.40 to 7.50 s" in said
    loose: list[T.Problem] = []
    T._Reader(loose).timeline(data)
    assert [p.level for p in loose] == ["warning"] and "mood" in str(loose[0])


def test_loose_reading_keeps_what_it_can():
    data = full().to_json()
    data["tracks"][0]["clips"][0]["asset"] = "gone"
    tl = T.Timeline.from_json(data, strict=False)
    assert len(tl.track(T.VIDEO_BASE).clips) == 3
    assert any("gone" in str(p) for p in T.validate(tl))


def test_files_are_looked_for_beside_the_timeline(tmp_path):
    tl = T.standard("files", T.Canvas.named("landscape"), 2.0)
    tl.assets["pic"] = T.Asset("image", "scenes/a.png")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("a", "pic", 0, 2.0))
    assert tl.resolve("pic", tmp_path) == tmp_path / "scenes" / "a.png"
    with pytest.raises(T.TimelineError, match="the file isn't there"):
        T.check(tl, tmp_path, files=True)
    (tmp_path / "scenes").mkdir()
    (tmp_path / "scenes" / "a.png").write_bytes(b"x")
    assert T.check(tl, tmp_path, files=True) == []
    saved = tl.dump(tmp_path / "timeline.json")
    assert T.Timeline.load(saved) == tl


def test_helpers():
    assert (T.Canvas.named("landscape").width, T.Canvas.named("landscape").height) == (1920, 1080)
    assert T.Canvas.named("vertical").vertical and T.Canvas.named("vertical").aspect == "9:16"
    assert T.Canvas.named("square").aspect == "1:1" and T.Canvas.named("landscape", 60).fps == 60
    with pytest.raises(ValueError):
        T.Canvas.named("imax")
    tl = T.standard()
    one = tl.add_asset(T.Asset("image", "a.png"), "scene 1!")
    assert one == "scene_1" and tl.add_asset(T.Asset("image", "a.png"), "other") == one
    assert tl.add_asset(T.Asset("image", "b.png"), "scene 1!") == "scene_1_2"
    assert [t.id for t in tl.audio_tracks("music")] == [T.AUDIO_MUSIC] and len(tl.video_tracks()) == 2
    extra = tl.ensure_track("video_track_2")
    assert extra.kind == "video" and tl.ensure_track("video_track_2") is extra
    assert tl.ensure_track("audio_track_voice_2", "audio", "voice").role == "voice"


def test_json_schema_agrees_with_the_reader():
    schema = T.JSON_SCHEMA
    json.dumps(schema)  # plain JSON all the way down
    clip = schema["properties"]["tracks"]["items"]["properties"]["clips"]["items"]["anyOf"][0]
    assert set(clip["properties"]) == {f for f in T.VideoClip.__dataclass_fields__}
    assert clip["properties"]["transition_in"]["oneOf"][0]["enum"] == list(T.TRANSITIONS)
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(full().to_json(), schema)
    bad = full().to_json()
    bad["tracks"][0]["clips"][0]["fit"] = "stretch"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# ---------------------------------------------------------------- what the review of the Studio found

def test_a_misspelt_key_is_said_with_the_nearest_real_name():
    data = full().to_json()
    data["tracks"][0]["clips"][1]["transiton_in"] = data["tracks"][0]["clips"][1].pop("transition_in")
    data["audio"]["duck"] = -30
    tl = T.Timeline.from_json(data)
    assert tl.notes == [
        "tracks[0].clips[1].transiton_in: isn't a field the timeline knows (ignored: did you mean transition_in?)",
        "audio.duck: isn't a field the timeline knows (ignored: did you mean duck_db?)"]
    assert "notes" not in tl.to_json()  # what was noticed while reading isn't part of the document
    assert T.Timeline.from_json(full().to_json()).notes == []
    # With something really wrong as well, the error names both.
    data["duration"] = -1
    with pytest.raises(T.TimelineError, match=r"Also: .*did you mean transition_in"):
        T.Timeline.from_json(data)


def test_a_file_saved_with_a_byte_order_mark_is_read(tmp_path):
    """Notepad's "UTF-8 with BOM" and PowerShell 5 put three bytes in front that JSON itself refuses."""
    path = tmp_path / "timeline.json"
    path.write_text(full().dumps(), encoding="utf-8-sig")
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"
    assert T.Timeline.load(path).title == "3 stoic habits"


def test_values_the_renderer_cannot_use_are_refused():
    def problems(**audio):
        tl = full()
        for k, v in audio.items():
            setattr(tl.audio, k, v)
        return [str(p) for p in T.validate(tl) if p.level == "error"]

    assert problems() == []
    assert any("audio.voice_lufs: is 3" in p for p in problems(voice_lufs=3))
    assert any("audio.duck_attack_ms: is 5000" in p for p in problems(duck_attack_ms=5000))
    assert any("audio.duck_release_ms: is 20000" in p for p in problems(duck_release_ms=20000))
    assert problems(duck_attack_ms=2000, duck_release_ms=9000, voice_lufs=-20) == []
    short = T.standard("blink", T.Canvas.named("vertical"), 0.01)  # under half a frame: ffmpeg would never stop
    assert any("less than one frame" in str(p) for p in T.validate(short) if p.level == "error")
