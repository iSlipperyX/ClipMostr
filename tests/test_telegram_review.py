"""Critique / Regenerate (ytclip/telegram/review_pipeline.py and ytclip/studio/retouch.py): a critique is read
for what it asks, only that is made again (a caption or level change is one render of the timeline), the result
comes back as "v2 (Critique Applied)", both versions are held, and approving one deletes the other. Telegram is
telegram_fake; one test renders for real, the rest stand in for the render."""

import json
from pathlib import Path

import pytest

from telegram_fake import JUSTYN, buttons
from test_studio_orchestrator import URL, studio, voice  # noqa: F401 - `voice` is a fixture
from test_telegram import make_video, world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, creator_video, link, served, to, writers  # noqa: F401 - `served` is a fixture
from ytclip import ffmpeg
from ytclip.creator.orchestrator import CreateRequest, CreatorError
from ytclip.posting import records
from ytclip.studio import orchestrator as O
from ytclip.studio import retouch
from ytclip.studio import timeline_schema as T
from ytclip.studio.orchestrator import StudioRequest, read_request
from ytclip.telegram import bot as botmod
from ytclip.telegram import mini_app_page, review_pipeline, state


@pytest.mark.parametrize("text, how, edits, voice_key, rest", [
    ("make the captions yellow", "retouch", {"captions": {"color": "#FFE600"}}, "", ""),
    ("Make the captions yellow and bigger, and the music is too loud", "retouch",
     {"music": {"gain_db": -4.0}, "captions": {"color": "#FFE600", "size_scale": 1.2}}, "", ""),
    ("no music please", "retouch", {"music": {"off": True}}, "", ""),
    ("move the captions higher", "retouch", {"captions": {"move": -0.1}}, "", ""),
    ("louder voice", "retouch", {"voice": {"gain_db": 4.0}}, "", ""),
    ("use the british voice", "voice", {}, "en_british_male", ""),
    ("the hook is flat", "rewrite", {}, "", "the hook is flat"),
    # a change of each kind in one critique: the rewrite gets its part, and the caption change rides along
    ("captions yellow. Also the hook is flat, open with the twist", "rewrite", {"captions": {"color": "#FFE600"}}, "",
     "the hook is flat. open with the twist"),
])
def test_a_critique_is_read_for_what_it_asks(text, how, edits, voice_key, rest):
    plan = retouch.read(text)
    assert (plan.how, plan.edits, plan.voice, plan.rest) == (how, edits, voice_key, rest)
    assert bool(plan.said) == (how != "rewrite" or bool(edits))


def timeline() -> T.Timeline:
    """A two-second timeline with captions over a plain background."""
    tl = T.standard("Title", duration=2)
    tl.assets["c"] = T.Asset("color", color="#101010")
    tl.track(T.VIDEO_BASE).clips.append(T.VideoClip("a", "c", 0, 2))
    old = tl.track(T.SUBTITLES)
    words = [retouch.Word(i * 0.4, i * 0.4 + 0.35, w) for i, w in enumerate("It was never an experiment".split())]
    new = retouch.kinetic_subtitles.build_track(words, tl.canvas, old.style, duration=2, emphasis={"never"},
                                                track_id=T.SUBTITLES)
    old.events = new.events
    return tl


def test_the_edits_change_only_what_they_name():
    tl = timeline()
    style = tl.track(T.SUBTITLES).style
    size, said_words = style.size, [w.text for ev in tl.track(T.SUBTITLES).events for w in ev.words]
    said = retouch.apply(tl, {"captions": {"color": "#FFE600", "size_scale": 1.2, "preset": "plain"}})
    assert style.color == "#FFE600" and style.highlight.upper() != "#FFE600" and style.preset == "plain"
    assert style.size == min(retouch.SIZE_LIMITS[1], round(size * 1.2)) and len(said) == 3
    assert [w.text for ev in tl.track(T.SUBTITLES).events for w in ev.words] == said_words  # the same words
    assert len(tl.track(T.VIDEO_BASE).clips) == 1 and tl.duration == 2
    assert retouch.apply(tl, {}) == [] and retouch.apply(tl, {"captions": {}}) == []
    assert retouch.apply(tl, {"music": {"gain_db": -4.0}}) == []  # it has no music: nothing answers that
    for _ in range(12):  # "bigger" a dozen times stays on the screen
        retouch.apply(tl, {"captions": {"size_scale": 1.2}})
    assert style.size == retouch.SIZE_LIMITS[1]
    assert retouch.apply(tl, {"captions": {"off": True}}) == ["the captions are off"] and tl.track(T.SUBTITLES).muted
    assert retouch.apply(T.standard("Bare", duration=2), {"captions": {"color": "#FFE600"}}) == []


def studio_folder(folder: Path, title: str = "The Stanford Prison Myth", cid: str = "c-1a2b") -> Path:
    """A Studio video's folder with a timeline that has captions to change (nothing in it is rendered)."""
    made = creator_video(folder.parent, cid=cid, title=title, studio={"canvas": "1080x1920", "timeline": True})
    tl = timeline()
    tl.dump(made / "timeline.json")
    return made


def test_a_version_gets_its_own_name_and_is_listed_with_its_video(tmp_path):
    folder = studio_folder(tmp_path / "v")
    source = "01 - The Stanford Prison Myth.mp4"
    assert retouch.can_retouch(folder) and not retouch.can_retouch(tmp_path)
    name = retouch.version_name(folder, "The Stanford Prison Myth", 2)
    assert name == "The Stanford Prison Myth (v2).mp4"
    make_video(folder / name)
    assert retouch.version_name(folder, "The Stanford Prison Myth (v2)", 2) == "The Stanford Prison Myth (v3).mp4"
    assert retouch.list_version(folder, source, name, "captions in yellow") and retouch.list_version(folder, source, name)
    clips = json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [c["file"] for c in clips] == [source, name] and clips[1]["edit_of"] == source
    assert clips[1]["title"].endswith("(v2)") and clips[1]["ai_generated"] is True
    # the next change starts from the version's own timeline, when it has one
    assert retouch.timeline_of(folder, name).name == "timeline.json"
    (folder / "The Stanford Prison Myth (v2).timeline.json").write_text("{}", encoding="utf-8")
    assert retouch.timeline_of(folder, name).name == "The Stanford Prison Myth (v2).timeline.json"
    with pytest.raises(CreatorError, match="no timeline beside it"):
        retouch.render(tmp_path, "a.mp4", "a (v2).mp4", {"captions": {"color": "#FFE600"}})


def test_a_caption_change_is_one_real_render_of_the_same_video(tmp_path, voice):  # noqa: F811
    st = studio(tmp_path, creator_music="composed")
    first = st.create(StudioRequest(url=URL, seconds=20, post=False))
    before = T.Timeline.load(first.folder / "timeline.json")
    name = retouch.version_name(first.folder, first.storyboard.title, 2)
    log = []
    again = O.StudioOrchestrator(st.s, log=log.append, register=False).create(StudioRequest(
        prompt="x", retouch={"folder": str(first.folder), "source": first.video.name, "name": name,
                             "edits": retouch.read("make the captions yellow and the music quieter").edits}))
    assert again.video == first.folder / name and again.video.is_file() and first.video.is_file()
    a, b = ffmpeg.probe(first.video), ffmpeg.probe(again.video)
    assert (b.width, b.height, b.has_audio) == (1080, 1920, True) and abs(a.duration - b.duration) < 0.2
    after = T.Timeline.load(first.folder / f"{Path(name).stem}.timeline.json")
    assert after.track(T.SUBTITLES).style.color == "#FFE600" != before.track(T.SUBTITLES).style.color
    # nothing else was made again: the same voice file, the same pictures, the same clips at the same times
    assert sorted(after.assets) == sorted(before.assets)
    assert [(c.asset, c.start, c.duration) for c in after.track(T.VIDEO_BASE).clips] == [
        (c.asset, c.start, c.duration) for c in before.track(T.VIDEO_BASE).clips]
    assert T.Timeline.load(first.folder / "timeline.json").track(T.SUBTITLES).style.color == before.track(T.SUBTITLES).style.color
    listed = json.loads((first.folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [c["file"] for c in listed] == [first.video.name, name]
    assert any("Changing the timeline" in line for line in log) and not list(first.folder.glob(".render-*"))


@pytest.fixture
def reviewing(world):  # noqa: F811
    w = world
    link(w)
    writers(w)
    w.folder = studio_folder(w.root / "x")
    w.it = w.take()[0]
    w.fake.clear()
    return w


def finish(w, r) -> object:
    """Automation made what the critique `r` queued (a retouch: the new file beside the first). The new video."""
    job = w.runner.db.get(r["job"])
    req = read_request(job.request)
    make_video(w.folder / req.retouch["name"])
    assert retouch.list_version(w.folder, req.retouch["source"], req.retouch["name"], "captions in yellow")
    w.runner.db.update(job.id, w.clock[0], status="done", folder=str(w.folder), clips=1, stage="Done")
    new = [i for i in w.take() if Path(i.path).name == req.retouch["name"]]
    assert len(new) == 1
    return new[0]


def test_every_generated_video_carries_the_critique_button(reviewing):
    w = reviewing
    w.bot.show(JUSTYN["id"], w.it)
    card = w.fake.last("sendVideo")
    assert ("🛠 Critique / Regenerate", f"fix:{w.it.id}") in buttons(card["reply_markup"])
    assert "🧬" not in card["caption"] and review_pipeline.B_FIX == "🛠 Critique / Regenerate"
    w.press(f"fix:{w.it.id}")
    assert "What should be different" in w.fake.last()["text"] or "what" in w.fake.last()["text"].lower()


def test_a_caption_critique_changes_only_the_timeline_and_comes_back_as_v2(reviewing):
    w = reviewing
    w.store.approve([w.it.id])
    w.store.save()
    r = w.bot.fix(SAM["id"], w.it.id, "Make the captions yellow")
    assert (r["how"], r["version"], r["held"]) == ("retouch", 2, True)
    assert r["message"].startswith("Got it: only that is changed on its timeline and rendered again (captions in yellow): "
                                   "the script, the voice and the pictures stay.")
    assert "v2 then waits beside this one: approve the one to post and the other is deleted, and this one is held" in r["message"]
    assert w.store.get(w.it.id).review == "waiting"  # v1 doesn't post while v2 is made
    job = w.runner.db.get(r["job"])
    req = read_request(job.request)
    assert isinstance(req, StudioRequest) and job.origins == ["creator", "telegram", "critique"]
    assert req.retouch == {"folder": str(w.folder), "source": Path(w.it.path).name,
                           "name": "The Stanford Prison Myth (v2).mp4", "edits": {"captions": {"color": "#FFE600"}}}
    assert req.storyboard is None  # nothing is written or drawn again
    new = finish(w, r)
    assert new.review == "waiting" and new.id != w.it.id
    w.fake.clear()
    w.bot.check(w.clock[0])
    c = next(iter(state.load(w.state_file)["critiques"].values()))
    assert (c["status"], c["fixed"], c["how"], c["version"]) == ("done", new.id, "retouch", 2)
    videos = {int(v["chat_id"]): v for v in w.fake.sent("sendVideo")}
    mine, theirs = videos[SAM["id"]], videos[JUSTYN["id"]]
    assert mine["caption"].startswith("🛠 <b>v2 (Critique Applied)</b>: here's “The Stanford Prison Myth” made again from "
                                      "your critique. Both versions wait for an OK: approve the one to post, and the "
                                      "other is deleted.")
    assert "🧬 <b>v2 (Critique Applied)</b>" in mine["caption"] and "was made again from Sam's critique" in theirs["caption"]
    rows = buttons(mine["reply_markup"])
    assert rows[:2] == [("✅ Approve v1", f"apv:{w.it.id}"), ("✅ Approve v2", f"apv:{new.id}")]
    assert ("🛠 Critique / Regenerate", f"fix:{new.id}") in rows and not any(d.startswith("ok:") for _, d in rows)
    # v1's card says which it is, and offers the same choice
    text, markup = w.bot.card(w.store.get(w.it.id))
    assert text.startswith("🧬 <b>v1</b>") and ("✅ Approve v2", f"apv:{new.id}") in buttons(markup)
    assert [(v["version"], v["label"], v["critique"], v["by"]) for v in w.bot.versions_json(new)] == [
        (1, "v1", "", ""), (2, "v2 (Critique Applied)", "Make the captions yellow", "Sam")]
    w.new = new


def test_approving_one_version_queues_it_and_deletes_the_other(reviewing):
    w = reviewing
    test_a_caption_critique_changes_only_the_timeline_and_comes_back_as_v2(w)
    new, old_file = w.new, Path(w.it.path)
    mid = max(k[1] for k in w.fake.messages if k[0] == JUSTYN["id"])
    w.bot.show(SAM["id"], w.store.get(w.it.id))  # v1's card is in Sam's chat too
    w.fake.clear()
    w.press(f"apv:{new.id}", mid)
    assert w.store.get(w.it.id) is None and not old_file.exists()  # v1 is gone from the library and the PC
    kept = w.store.get(new.id)
    assert (kept.status, kept.review) == ("queued", "ok") and Path(kept.path).is_file()
    assert (w.folder / "timeline.json").is_file() and (w.folder / "storyboard.json").is_file()  # v2's makings stay
    assert "<b>Justyn</b> picked v2 (Critique Applied) of “The Stanford Prison Myth” to post" in to(w, SAM)[-1]["text"]
    captions = [p.get("caption") or "" for m, p in w.fake.calls if m == "editMessageCaption"]
    assert any(c.startswith("🗑 <b>Deleted</b>: v2 (Critique Applied) was picked to post instead.") for c in captions)
    assert any("🕓" in c and "🧬 <b>v2 (Critique Applied)</b>" in c for c in captions)
    acts = state.load(w.state_file)["activity"]
    assert (acts[-1]["what"], acts[-1]["name"]) == ("approve", "Justyn")
    assert any("picked v2 (Critique Applied)" in line and "v1 deleted" in line for line in w.logs)
    # there's nothing to pick between any more
    assert w.bot.versions(kept) == [] and not any(d.startswith("apv:") for _, d in buttons(w.bot.card(kept)[1]))
    with pytest.raises(botmod.Refused, match="isn't in the library"):
        w.bot.approve_version(JUSTYN["id"], w.it.id)


def test_approving_the_first_version_deletes_the_new_one_and_a_third_can_follow(reviewing):
    w = reviewing
    r = w.bot.fix(JUSTYN["id"], w.it.id, "bigger captions")
    with pytest.raises(botmod.Refused, match="still being made"):
        w.bot.approve_version(JUSTYN["id"], w.it.id)  # not while v2 is on its way
    new = finish(w, r)
    w.bot.check(w.clock[0])
    out = w.bot.approve_version(SAM["id"], w.it.id)
    assert out == {"item": w.it.id, "deleted": [2], "kept": [], "message": "v1 posts when its time comes. v2 was deleted."}
    assert w.store.get(new.id) is None and not Path(new.path).exists() and Path(w.it.path).is_file()
    assert w.store.get(w.it.id).review == "ok"
    # a critique of what's left makes the next version, numbered on
    r = w.bot.fix(JUSTYN["id"], w.it.id, "captions in red")
    assert r["version"] == 3 and read_request(w.runner.db.get(r["job"]).request).retouch["name"].endswith("(v3).mp4")


def test_a_voice_change_keeps_the_script_and_a_rewrite_asks_the_ai(reviewing):
    w = reviewing
    r = w.bot.fix(JUSTYN["id"], w.it.id, "use the british voice")
    req = read_request(w.runner.db.get(r["job"]).request)
    assert r["how"] == "voice" and "the same script is made again with that changed (the British Male voice); nothing is rewritten." in r["message"]
    assert isinstance(req, StudioRequest) and req.keep_script and req.voice == "en_british_male" and req.retouch is None
    assert [s["speech_text"] for s in req.storyboard["scenes"]][0] == "It was never an experiment."
    w.runner.db.update(r["job"], w.clock[0], status="failed", error="no voice engine")
    w.bot.check(w.clock[0])
    r = w.bot.fix(JUSTYN["id"], w.it.id, "The hook is flat, and make the captions yellow")
    req = read_request(w.runner.db.get(r["job"]).request)
    assert r["how"] == "rewrite" and "the AI makes it again with that fixed (and captions in yellow)." in r["message"]
    assert not req.keep_script and req.look == {"captions": {"color": "#FFE600"}} and "hook is flat" in req.critique
    assert "yellow" not in req.critique  # the AI is only asked for what needs rewriting


def test_a_classic_creator_video_has_no_timeline_so_the_same_script_is_made_again(world):  # noqa: F811
    w = world
    writers(w)
    creator_video(w.root)  # the classic Creator's: a storyboard, no timeline
    it = w.take()[0]
    r = w.bot.fix(JUSTYN["id"], it.id, "make the captions yellow")
    req = read_request(w.runner.db.get(r["job"]).request)
    assert r["how"] == "voice" and "This one has no timeline to change" in r["message"]
    assert isinstance(req, StudioRequest) and req.keep_script and req.look == {"captions": {"color": "#FFE600"}}
    r2 = None
    w.runner.db.update(r["job"], w.clock[0], status="failed", error="x")
    w.bot.check(w.clock[0])
    r2 = w.bot.fix(JUSTYN["id"], it.id, "use a deep voice")
    assert isinstance(read_request(w.runner.db.get(r2["job"]).request), CreateRequest)


def test_a_version_that_cant_be_deleted_is_said_and_the_pick_still_stands(reviewing, monkeypatch):
    w = reviewing
    new = finish(w, w.bot.fix(JUSTYN["id"], w.it.id, "bigger captions"))
    w.bot.check(w.clock[0])

    def stuck(*a, **k):
        raise ValueError("it's open in another program")

    monkeypatch.setattr(records, "discard", stuck)
    out = w.bot.approve_version(JUSTYN["id"], new.id)
    assert out["deleted"] == [] and out["kept"] == [1] and "v1 couldn't be deleted: it's open in another program" in out["message"]
    assert w.store.get(new.id).review == "ok" and w.store.get(w.it.id) is not None


def test_cleaning_up_a_version_takes_its_own_timeline_with_it(reviewing):
    w = reviewing
    new = finish(w, w.bot.fix(JUSTYN["id"], w.it.id, "bigger captions"))
    own = w.folder / f"{Path(new.path).stem}.timeline.json"
    own.write_text("{}", encoding="utf-8")
    w.bot.check(w.clock[0])
    w.bot.approve_version(JUSTYN["id"], w.it.id)
    assert not own.exists() and (w.folder / "timeline.json").is_file()


def test_versions_in_the_app(served):  # noqa: F811
    w = served
    w.folder = studio_folder(w.root / "x")
    it = w.take()[0]
    sam, justyn = w.session(SAM)["token"], w.session(JUSTYN)["token"]
    status, out, _ = w.call("POST", f"/api/videos/{it.id}/critique", {"text": "captions in yellow"}, token=sam)
    assert status == 200 and (out["how"], out["version"]) == ("retouch", 2) and out["critique"]["how"] == "retouch"
    assert w.call("POST", f"/api/videos/{it.id}/approve", token=justyn)[0] == 409  # v2 is still being made
    new = finish(w, out)
    w.bot.check(w.clock[0])
    _, vids, _ = w.call("GET", "/api/videos", token=justyn)
    by_id = {v["id"]: v for v in vids["items"]}
    assert by_id[it.id]["version"] == "v1" and by_id[new.id]["version"] == "v2 (Critique Applied)"
    assert [(x["id"], x["version"]) for x in by_id[new.id]["versions"]] == [(it.id, 1), (new.id, 2)]
    assert by_id[new.id]["versions"][1]["critique"] == "captions in yellow" and by_id[new.id]["versions"][1]["by"] == "Sam"
    assert w.call("POST", f"/api/videos/{new.id}/approve")[0] == 401
    w.fake.clear()
    status, r, _ = w.call("POST", f"/api/videos/{new.id}/approve", token=justyn)
    assert status == 200 and r["message"] == "v2 (Critique Applied) posts when its time comes. v1 was deleted."
    _, vids, _ = w.call("GET", "/api/videos", token=sam)
    assert [v["id"] for v in vids["items"]] == [new.id] and vids["items"][0]["versions"] == []
    assert "<b>Justyn</b> picked v2 (Critique Applied)" in to(w, SAM)[-1]["text"]
    _, acts, _ = w.call("GET", "/api/activity", token=sam)
    assert acts["items"][0]["what"] == "approve"
    assert w.call("POST", f"/api/videos/{it.id}/approve", token=justyn)[0] == 404
    for part in ("Pick the version to post", "Approve v", "/approve", "Critique / Regenerate"):
        assert part in mini_app_page.JS, part
