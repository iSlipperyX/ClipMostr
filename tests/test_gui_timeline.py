"""The timeline view: what it draws for a timeline, and what it says under the mouse."""

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

from ytclip.gui import timeline_widget as W  # noqa: E402
from ytclip.studio import timeline_schema as T  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def timeline():
    tl = T.standard("Lighthouses", duration=20.0)
    tl.assets = {"p1": T.Asset("image", "scenes/scene_01.png", source="SDXL-Turbo"),
                 "c2": T.Asset("video", "scenes/scene_02_pexels_7.mp4", duration=12, source="Pexels",
                               credit="Video by A on Pexels"),
                 "v": T.Asset("audio", "assets/voice.wav", duration=20), "m": T.Asset("audio", "assets/bed.wav"),
                 "w": T.Asset("audio", "assets/sfx/whoosh.wav", duration=0.6)}
    tl.track(T.VIDEO_BASE).clips += [
        T.VideoClip("scene_01", "p1", 0, 8, keyframes=[T.Keyframe(0), T.Keyframe(8, scale=1.1)], label="One man kept"),
        T.VideoClip("scene_02", "c2", 8, 12, transition_in=T.Transition("dip_black", 0.3), label="Then the storm")]
    tl.track(T.AUDIO_VOICE).clips.append(T.AudioClip("voice", "v", 0, 20))
    tl.track(T.AUDIO_MUSIC).clips.append(T.AudioClip("music", "m", 0, 20, gain_db=-6, label="Epic"))
    tl.track(T.AUDIO_SFX).clips.append(T.AudioClip("auto_whoosh_2", "w", 7.7, 0.6, label="a change of picture"))
    tl.tracks = [t for t in tl.tracks if t.kind != "subtitle"] + [
        T.Track(T.SUBTITLES, "subtitle", style=T.SubtitleStyle(), events=[
            T.SubtitleEvent(0.3, 1.2, [T.WordEvent("ONE", 0.3, 0.6), T.WordEvent("MAN", 0.6, 1.2)]),
            T.SubtitleEvent(1.2, 2.0, [T.WordEvent("KEPT", 1.2, 2.0)])]),
        T.Track("titles", "subtitle", style=T.SubtitleStyle(preset="plain"), events=[
            T.SubtitleEvent(0.1, 2.2, [T.WordEvent("HE", 0.1, 1.0), T.WordEvent("STAYED", 1.0, 2.2)])])]
    tl.markers = [T.Marker(0, "hook", "He stayed"), T.Marker(8.2, "retention", "Then the storm"),
                  T.Marker(15, "chapter", "The rock")]
    return tl


def test_a_row_a_track_and_a_block_a_clip_or_caption():
    rows, blocks = W.rows_of(timeline())
    assert rows == [("Pictures", "video"), ("Overlays", "overlay"), ("Voice", "voice"), ("Music", "music"),
                    ("Effects", "sfx"), ("Captions", "subtitle"), ("Titles", "subtitle")]
    by = {(b.row, b.label): b for b in blocks}
    second = by[(0, "Then the storm")]
    assert second.transition == 0.3 and "arrives by dip black (0.30 s)" in second.tip
    assert "Video by A on Pexels" in second.tip and "from Pexels" in second.tip and "0:08.0 to 0:20.0" in second.tip
    assert "2 keyframes" in by[(0, "One man kept")].tip
    assert by[(4, "whoosh")].tip.startswith("whoosh (a change of picture)")
    assert "-6 dB" in by[(3, "Epic")].tip
    assert by[(5, "ONE MAN")].tip.startswith("“ONE MAN”") and (6, "HE STAYED") in by
    muted = timeline()
    muted.track(T.AUDIO_MUSIC).muted = True
    assert ("Music (muted)", "music") in W.rows_of(muted)[0]


def test_names_and_ticks():
    assert W._cue_name("auto_bass_drop_1") == "bass drop" and W._cue_name("cue_glass_hit_12") == "glass hit"
    assert W._cue_name("whoosh") == "whoosh"
    assert W.tick_step(13, 980) == 1 and W.tick_step(65, 700) == 10 and W.tick_step(600, 700) == 60
    assert W.tick_step(0, 700) == 1.0 and W.tick_step(30000, 100) == 1200.0
    assert W._clock(75.25) == "1:15.2" and W._clock(725) == "12:05"


def test_it_paints_and_answers_the_mouse(app):
    w = W.TimelineWidget()
    assert w.sizeHint().height() > 0
    w.resize(900, 200)
    assert not w.grab().isNull()  # empty: the hint line, no crash
    tl = timeline()
    w.set_timeline(tl)
    w.resize(900, w.sizeHint().height())
    image = w.grab().toImage()
    assert image.width() == 900 and image.height() == W.RULER + W.PAD * 2 + 7 * (W.ROW + W.GAP)
    y_pictures = W.RULER + W.PAD + W.ROW / 2
    assert image.pixelColor(int(w.x_of(4.0)), int(y_pictures)).name().lower() != \
        image.pixelColor(10, int(y_pictures)).name().lower()  # a block is drawn where the clip is
    assert abs(w.time_at(w.x_of(12.5)) - 12.5) < 0.01 and w.time_at(0) == 0 and w.time_at(5000) == 20.0
    assert w.block_at(w.x_of(4.0), y_pictures).label == "One man kept"
    assert w.block_at(w.x_of(12.0), y_pictures).label == "Then the storm"
    captions_y = W.RULER + W.PAD + 5 * (W.ROW + W.GAP) + W.ROW / 2
    assert w.block_at(w.x_of(1.5), captions_y).label == "KEPT"
    assert w.block_at(w.x_of(19.0), captions_y) is None
    assert w.tip_at(w.x_of(8.2), 10).startswith("Retention at 0:08.2: Then the storm")
    assert w.tip_at(w.x_of(15), 12) == "Chapter at 0:15.0: The rock"
    assert w.tip_at(w.x_of(11.0), 10) == "" and w.tip_at(w.x_of(4.0), y_pictures).startswith("One man kept")
    w.set_timeline(None)
    assert w.blocks == [] and not w.grab().isNull()


# ---------------------------------------------------------------- the Creator tab's Studio side

def creator_tab(tmp_path, **over):
    from ytclip.gui.creator_view import CreatorView
    from ytclip.settings import Settings

    s = Settings().apply({"output_dir": str(tmp_path / "Out"), "categories": [{"id": "h", "name": "History"}]} | over)
    return CreatorView(lambda: s)


def test_the_creator_tab_stays_the_classic_creator_until_the_studio_is_picked(app, tmp_path):
    from ytclip.creator.orchestrator import CreateRequest
    from ytclip.studio.orchestrator import StudioRequest

    view = creator_tab(tmp_path)
    assert view.engine.currentData() == "creator" and not view.studio()
    assert view.shape.isHidden() and view.captions.isHidden() and view.seconds.maximum() == 180
    view.prompt.setPlainText("Lighthouses")
    assert type(view.request()) is CreateRequest
    view.engine.setCurrentIndex(view.engine.findData("studio"))
    assert view.studio() and not view.shape.isHidden() and not view.captions.isHidden()
    assert view.seconds.maximum() == 1200 and "Stock footage" in view.ready.text()
    view.seconds.setValue(480)
    view.shape.setCurrentIndex(view.shape.findData("landscape"))
    view.shape.currentIndexChanged.emit(view.shape.currentIndex())
    view.captions.setCurrentIndex(view.captions.findData("karaoke"))
    req = view.request()
    assert isinstance(req, StudioRequest)
    assert (req.prompt, req.orientation, req.captions, req.seconds, req.url, req.category) == \
        ("Lighthouses", "landscape", "karaoke", 480.0, "", "h")
    assert req.rewards is False  # Creator Rewards is for upright videos
    view.engine.setCurrentIndex(view.engine.findData("creator"))
    assert view.seconds.value() == 180 and view.shape.isHidden()  # back in the classic Creator's range
    view.deleteLater()


def test_an_articles_address_makes_it_a_studio_video(app, tmp_path):
    from ytclip.studio.orchestrator import StudioRequest

    view = creator_tab(tmp_path)
    assert view._check_input() == "Say what the video is about first, or give an article's address."
    view.url.setText("  https://example.org/a  ")
    assert view.studio() and view._check_input() == "" and not view.shape.isHidden()
    req = view.request()
    assert isinstance(req, StudioRequest) and req.url == "https://example.org/a" and req.prompt == ""
    view.mode_script.setChecked(True)  # your own script: the address isn't used
    assert view.url.isHidden() and view.article() == "" and not view.studio()
    view.deleteLater()


def test_the_studio_can_be_the_tabs_default_from_settings(app, tmp_path):
    view = creator_tab(tmp_path, creator_engine="studio", pexels_api_key="k")
    assert view.studio() and "Stock footage: <b>Pexels</b>" in view.ready.text()
    view.deleteLater()


def test_a_made_videos_timeline_is_drawn_and_can_be_rendered_again(app, tmp_path):
    view = creator_tab(tmp_path)
    assert view.timeline_scroll.isHidden() and view.rerender_btn.isHidden()
    path = tmp_path / "timeline.json"
    timeline().dump(path)
    view.show_timeline(path)
    assert not view.timeline_scroll.isHidden() and not view.rerender_btn.isHidden()
    assert len(view.timeline_view.rows) == 7
    path.write_text("{broken", encoding="utf-8")  # half-edited: the tab says so and carries on
    view.show_timeline(path)
    assert view.timeline_scroll.isHidden() and not view.rerender_btn.isHidden()
    assert "can't be shown" in view.log_box.toPlainText()
    view.show_timeline(tmp_path / "none.json")
    assert view.rerender_btn.isHidden()
    view.deleteLater()
