"""Clean up after a Video Creator video is posted: its scene pictures and storyboard go with the video's folder,
and its record keeps how it was made (the prompt, the script, the voice, the sources, the fact check)."""

import json
import time
from pathlib import Path

from ytclip.posting import intake, records
from ytclip.posting.accounts import Accounts
from ytclip.posting.clipsjson import creator_story
from ytclip.posting.config import Settings
from ytclip.posting.store import Store, TikTokVideo

HOUR = 3600.0
DAY = 86400.0
SCRIPT = ["In 1971, twenty-four students went into a basement at Stanford.",
          "What happened next is still argued about.", "Here's what the tapes show."]
PICTURE = b"\x89PNG" + b"\x00" * 400_000


class Main:
    def __init__(self, root: Path):
        self.auto_output_path = root
        self.categories = []
        self.creator_review = "off"


def creator_folder(root: Path) -> Path:
    """A Creator video's folder as orchestrator._deliver writes it."""
    folder = root / "Psychology" / "Created" / "2026-11-08 The Stanford Prison Myth [c-1a2b]"
    (folder / "thumbnails").mkdir(parents=True)
    (folder / "scenes").mkdir()
    name = "01 - The Stanford Prison Myth.mp4"
    (folder / name).write_bytes(b"\x00" * 50_000)
    (folder / "thumbnails" / "01.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 900)
    for i in range(1, 4):
        (folder / "scenes" / f"scene_{i:02d}.png").write_bytes(PICTURE)
    (folder / "README.md").write_text("# The Stanford Prison Myth\n", encoding="utf-8")
    (folder / "storyboard.json").write_text(json.dumps({
        "title": "The Stanford Prison Myth", "prompt": "the truth about the Stanford prison experiment",
        "scenes": [{"scene_id": i, "speech_text": line} for i, line in enumerate(SCRIPT, 1)]}), encoding="utf-8")
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "c-1a2b", "title": "The Stanford Prison Myth", "channel": "ClipMonster Video Creator"},
        "automation": {"origins": ["creator"], "how": "creator", "recipe": "Video Creator", "category": "Psychology"},
        "creator": {"id": "c-1a2b", "prompt": "the truth about the Stanford prison experiment",
                    "written_by": "Mistral Nemo 12B", "style": "documentary", "voice": "Calm Documentary",
                    "voice_engine": "Kokoro-82M", "music_mood": "Suspense", "pictures": ["ComfyUI", "Built-in textures"],
                    "ai_generated": True,
                    "sources": [{"title": "Stanford prison experiment", "url": "https://en.wikipedia.org/wiki/SPE",
                                 "publisher": "Wikipedia", "kind": "wiki"}],
                    "factcheck": {"mode": "check", "checked": 3, "model": "Mistral Nemo 12B", "fixed": 1, "to_check": 0}},
        "clips": [{"file": name, "title": "The Stanford Prison Myth", "score": 100, "origin": "creator",
                   "hook": "It was never an experiment", "ai_generated": True, "qc": {"ok": True}}],
    }), encoding="utf-8")
    return folder


def posted_creator_video(tmp_path):
    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = "psych", True
    accs.save()
    root = tmp_path / "Automation"
    folder = creator_folder(root)
    store = Store(tmp_path / "lib.json")
    items, _ = intake.take(store, accs, Main(root))
    (it,) = items
    now = time.time()
    it.status, it.posted_at, it.posted_via = "posted", now - 2 * HOUR, "browser"
    it.queue_pos = it.planned_at = None
    store.update_tiktok_videos(a.id, [TikTokVideo(id="7009", description="It was never an experiment",
                                                  create_time=now - 2 * HOUR + 30, views=8000,
                                                  url="https://www.tiktok.com/@psych/video/7009")])
    it.tiktok_id, it.tiktok_url = "7009", "https://www.tiktok.com/@psych/video/7009"
    return accs, root, folder, store, it, now


def test_the_story_is_read_from_the_folder():
    folder = creator_folder(Path(__import__("tempfile").mkdtemp()) / "Automation")
    story = creator_story(folder)
    assert story["prompt"] == "the truth about the Stanford prison experiment"
    assert story["script"] == "\n".join(SCRIPT) and story["scenes"] == 3
    assert story["voice"] == "Calm Documentary" and story["pictures"] == ["ComfyUI", "Built-in textures"]
    assert story["sources"] == [{"title": "Stanford prison experiment", "url": "https://en.wikipedia.org/wiki/SPE",
                                 "publisher": "Wikipedia"}]
    assert story["factcheck"] == {"checked": 3, "model": "Mistral Nemo 12B", "fixed": 1, "to_check": 0}
    assert creator_story(folder.parent) == {}  # not a Creator folder


def test_posted_creator_video_goes_with_its_scene_pictures(tmp_path):
    accs, root, folder, store, it, now = posted_creator_video(tmp_path)
    assert it.ai_generated and it.facts["creator"]["script"].startswith("In 1971")  # kept from intake
    s = Settings(records_folder=str(tmp_path / "Records"))
    r = records.finish(store, accs, s, [root], now=now)
    assert r.cleaned == 1 and not r.failed
    assert not folder.exists() and not folder.parent.exists()  # the video's folder, then the empty "Created"
    assert (root / "Psychology").is_dir()  # the category's folder stays
    assert r.freed >= 3 * len(PICTURE) + 50_000  # the pictures count towards the space freed
    text = Path(it.record).read_text(encoding="utf-8")
    for want in ("Made by the Video Creator:", "the truth about the Stanford prison experiment",
                 "Calm Documentary (Kokoro-82M)", "ComfyUI, Built-in textures", "3 sentences checked by Mistral Nemo "
                 "12B, 1 fixed", "Labelled:      AI-generated", "Stanford prison experiment · Wikipedia "
                 "<https://en.wikipedia.org/wiki/SPE>", "Script (3 scenes):", "    What happened next is still argued about."):
        assert want in text, want
    assert "Where it came from:" not in text  # it came from the prompt
    data = json.loads(Path(it.record).with_suffix(".json").read_text(encoding="utf-8"))
    assert data["ai_generated"] is True and data["creator"]["script"] == "\n".join(SCRIPT)
    assert "creator" not in data["source"]
    # a day later the record is rewritten with new numbers, long after the folder went: the story stays
    store.update_tiktok_videos(it.account, [TikTokVideo(id="7009", description="It was never an experiment",
                                                        create_time=now - 2 * HOUR + 30, views=91000)])
    assert records.update_records(store, accs, s, now=now + DAY) == 1
    text = Path(it.record).read_text(encoding="utf-8")
    assert "91,000" in text and "Here's what the tapes show." in text


def test_a_video_queued_before_35_still_keeps_its_script(tmp_path):
    accs, root, folder, store, it, now = posted_creator_video(tmp_path)
    del it.facts["creator"]  # queued by 3.4, which didn't keep it
    s = Settings(records_folder=str(tmp_path / "Records"))
    records.finish(store, accs, s, [root], now=now)
    assert not folder.exists()
    assert it.facts["creator"]["script"].endswith("Here's what the tapes show.")  # read before the folder went
    assert "Script (3 scenes):" in Path(it.record).read_text(encoding="utf-8")


def test_scene_pictures_stay_while_the_video_waits(tmp_path):
    accs, root, folder, store, it, now = posted_creator_video(tmp_path)
    it.tiktok_id = ""  # not seen on the profile yet: nothing is deleted
    s = Settings(records_folder=str(tmp_path / "Records"))
    records.finish(store, accs, s, [root], now=now)
    assert len(list((folder / "scenes").glob("scene_*.png"))) == 3 and (folder / "storyboard.json").is_file()
    s.delete_after_post = False  # and with Clean up off, a posted video's pictures stay too
    it.tiktok_id = "7009"
    records.finish(store, accs, s, [root], now=now)
    assert len(list((folder / "scenes").glob("scene_*.png"))) == 3


def test_your_own_files_in_the_scenes_folder_are_left_alone(tmp_path):
    accs, root, folder, store, it, now = posted_creator_video(tmp_path)
    (folder / "scenes" / "my edit.psd").write_bytes(b"mine")
    records.finish(store, accs, Settings(records_folder=str(tmp_path / "Records")), [root], now=now)
    assert (folder / "scenes" / "my edit.psd").is_file()
    assert not list((folder / "scenes").glob("scene_*")) and not (folder / "storyboard.json").exists()
