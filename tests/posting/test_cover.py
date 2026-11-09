"""The TikTok cover (posting/cover.py): the hook card's moment, else the best face, else the clearest frame;
the picture saved full size; and TikTok Studio's cover editor, with the post going up whatever it does."""

import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
import studio_page
from studio_page import clip

from ytclip.ffmpeg import ffmpeg_exe
from ytclip.posting import cover, jobs
from ytclip.posting.config import Settings
from ytclip.posting.store import Store, VideoItem
from ytclip.rendering import cards
from ytclip.vision.face_tracker import Face


def test_the_hook_card_is_fully_up():
    s = SimpleNamespace(intro_seconds=1.8, caption_uppercase=True)
    six = SimpleNamespace(hook="He quit live on air today", title="")
    assert cards.complete_at(six, s) == {"at": 0.61, "until": 1.8}  # the sixth word has punched in
    long = SimpleNamespace(hook=" ".join(["word"] * 30), title="")
    assert cards.complete_at(long, SimpleNamespace(intro_seconds=1.0))["at"] == 0.66  # before the fade, whatever
    assert cards.complete_at(SimpleNamespace(hook="", title=""), s) is None


def test_hook_card_from_the_record():
    assert cover.hook_card({"hook_card": {"at": 0.6, "until": 1.8}}) == (0.6, 1.8)
    assert cover.hook_card({"edit": {"hook_card": {"at": 0.7}}}) == (0.7, 1.7)
    for bad in (None, {}, {"hook_card": 3}, {"hook_card": {"at": -1}}, {"hook_card": {"at": "x"}}):
        assert cover.hook_card(bad) is None


def frame(kind):
    rng = np.random.default_rng(1)
    if kind == "dark":
        return (rng.random((640, 360, 3)) * 25).astype(np.uint8)
    if kind == "flat":
        return np.full((640, 360, 3), 128, np.uint8)
    board = (np.indices((640, 360)).sum(axis=0) // 20 % 2 * 200 + 25).astype(np.uint8)  # sharp, mid, contrasty
    return np.dstack([board] * 3)


class Faces:
    def __init__(self, at):
        self.at = at

    def detect(self, f):
        return [Face(0.3, 0.25, 0.4, 0.25, 0.95)] if f is self.at else []


def test_the_clearest_frame_without_a_face():
    frames = [(0.0, frame("dark")), (0.5, frame("sharp")), (1.0, frame("flat"))]
    pick = cover.best_frame(None, 10.0, frames=frames, detector=False)
    assert (pick.at, pick.why) == (cover.SKIP_START + 0.5, "the clearest frame")


def test_a_big_face_near_the_middle_wins():
    face = frame("flat")
    frames = [(0.0, frame("sharp")), (1.0, face), (2.0, frame("dark"))]
    pick = cover.best_frame(None, 10.0, frames=frames, detector=Faces(face))
    assert (pick.at, pick.why) == (cover.SKIP_START + 1.0, "a face")
    small = [Face(0.45, 0.4, 0.05, 0.04, 0.99)]  # a face in a crowd doesn't count
    assert cover.frame_score(face, small)[1] == 0.0


def item_with(tmp_path, **kw):
    return VideoItem(path=str(tmp_path / "clip.mp4"), info={"duration": 30.0, "width": 1080, "height": 1920}, **kw)


def test_choosing(tmp_path):
    it = item_with(tmp_path)
    card = {"hook_card": {"at": 0.61, "until": 1.8}}
    assert cover.choose(it, "auto", card) == cover.Pick(0.61, "the hook card", 1.0)
    assert cover.choose(it, "off", card) is None
    seen = {}

    def best(path, duration, size, skip_until=0.0, **kw):
        seen.update(duration=duration, size=size, skip=skip_until)
        return cover.Pick(4.0, "a face")

    real, cover.best_frame = cover.best_frame, best
    try:
        assert cover.choose(it, "face", card).why == "a face"  # never the card, which is skipped
        assert seen == {"duration": 30.0, "size": (1080, 1920), "skip": 1.8}
        assert cover.choose(it, "auto", {}).at == 4.0  # no card: the best face
    finally:
        cover.best_frame = real


@pytest.fixture
def real_clip(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run([ffmpeg_exe(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=10:duration=4",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_the_picture_is_saved_full_size(real_clip, tmp_path):
    out = cover.extract(real_clip, 1.5, tmp_path / "covers" / "x.jpg")
    head = out.read_bytes()[:3]
    assert head == b"\xff\xd8\xff" and out.stat().st_size > 1000
    assert not list((tmp_path / "covers").glob("*.part.jpg"))
    with pytest.raises(RuntimeError, match="couldn't take the frame"):
        cover.extract(tmp_path / "missing.mp4", 1.0, tmp_path / "covers" / "y.jpg")


def test_cover_item_sets_the_cover_and_the_queue_thumbnail(real_clip, tmp_path, monkeypatch):
    store = Store(tmp_path / "lib.json")
    it = VideoItem(path=str(real_clip), info={"duration": 4.0, "width": 270, "height": 480})
    store.items[it.id] = it
    monkeypatch.setattr(cover, "covers_dir", lambda: tmp_path / "covers")
    logs = []
    jobs.cover_item(store, it.id, Settings(), logs.append, detector=False)
    assert it.cover.endswith(f"{it.id}.jpg") and it.cover_why == "the clearest frame" and 0.3 <= it.cover_at <= 3.2
    assert it.thumb and logs[0].startswith(f"Cover for “{it.headline}”: the clearest frame (")
    assert not jobs.needs_cover(it, Settings())
    broken = VideoItem(path=str(tmp_path / "gone.mp4"), info={"duration": 4.0, "width": 270, "height": 480})
    store.items[broken.id] = broken
    jobs.cover_item(store, broken.id, Settings(), logs.append, detector=False)
    assert broken.cover == "" and broken.cover_why.startswith("none:")
    assert not jobs.needs_cover(broken, Settings())  # not tried again and again
    assert jobs.needs_cover(VideoItem(path="x.mp4"), Settings())
    assert not jobs.needs_cover(VideoItem(path="x.mp4"), Settings(cover="off"))


# --- TikTok Studio's cover editor (a stand-in page) -----------------------------------------------

@pytest.fixture
def studio(tmp_path, monkeypatch):
    with studio_page.opened(tmp_path, monkeypatch) as make:
        yield make


EDITOR = """<!doctype html><html><body>
<input type="file" id="f" accept="video/*">
<div id="form" hidden>
  <div class="public-DraftEditor-content" contenteditable="true"></div>
  <div class="cover-container"><img class="cover-image" src="data:,first"><div class="edit-container">Edit cover</div></div>
  <button data-e2e="post_video_button"
          onclick="document.body.insertAdjacentHTML('beforeend', '<p>Your video has been posted</p>')">Post</button>
</div>
<div class="cover-edit-container" id="editor" hidden>
  <div><div id="pick">Select cover</div><div id="up">Upload cover</div></div>
  <div id="panel" hidden><input type="file" id="img" accept="image/png, image/jpeg, image/jpg"></div>
  <div class="cover-edit-footer"><button class="TUXButton--secondary" id="cancel">Cancel</button>
    <button class="TUXButton TUXButton--primary" id="ok" disabled>Confirm</button></div>
</div>
<script>
const $ = id => document.getElementById(id);
$('f').onchange = () => $('form').hidden = false;
document.querySelector('.edit-container').onclick = () => $('editor').hidden = false;
$('up').onclick = () => $('panel').hidden = false;
$('img').onchange = () => { window.coverName = $('img').files[0].name; setTimeout(() => $('ok').disabled = false, 300); };
$('ok').onclick = () => { document.querySelector('.cover-image').src = 'data:,' + window.coverName;
                          $('editor').hidden = true; };
$('cancel').onclick = () => $('editor').hidden = true;
</script>
</body></html>"""


def picture(tmp_path):
    p = tmp_path / "cover.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"0" * 2000)
    return p


def test_the_cover_goes_in_before_post(studio, tmp_path):
    b = studio(html=EDITOR)
    said = []
    b.upload(clip(tmp_path), "A caption", "public", progress=said.append, cover=picture(tmp_path))
    assert b.cover_set is True and "Setting the cover…" in said
    assert b.page.evaluate("document.querySelector('.cover-image').src") == "data:,cover.jpg"
    assert "Your video has been posted" in b.page.inner_text("body")
    assert b.check_upload_page(clip(tmp_path))[-1][:2] == ("Cover editor", "ok")


def test_without_the_editor_it_posts_with_tiktoks_pick(studio, tmp_path):
    b = studio(html=EDITOR.replace('<div class="edit-container">Edit cover</div>', ""))  # no Edit cover
    said = []
    b.upload(clip(tmp_path), "A caption", "public", progress=said.append, cover=picture(tmp_path))
    assert b.cover_set is False
    assert any(m.startswith("Couldn't set the cover (TikTok Studio showed no Edit cover button)") for m in said)
    assert "Your video has been posted" in b.page.inner_text("body")


def test_an_editor_without_upload_is_closed_again(studio, tmp_path):
    b = studio(html=EDITOR.replace('<div id="up">Upload cover</div>', '<div id="up">Templates</div>'))
    b._open_upload_page()
    b._file_input().set_input_files(str(clip(tmp_path)))
    done, why = b.set_cover(picture(tmp_path))
    assert not done and why == "the cover editor has no Upload cover"
    assert b.page.evaluate("document.getElementById('editor').hidden")  # Cancel pressed: nothing left open
