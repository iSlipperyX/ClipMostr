"""The Telegram Mini App and sharing the bot (ytclip/telegram/webapp.py, tunnel.py, and the bot's side of them):
two people at most, each told what the other did; a critique that has the AI make a video again; the Mini
App's server, signed in by what Telegram hands the page; and its public address. Telegram is telegram_fake,
the server is the real one on a local port, and cloudflared is a stand-in process."""

import hashlib
import hmac
import http.client
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from telegram_fake import JUSTYN, TOKEN, buttons
from test_telegram import STRANGER, clip_folder, make_video, world  # noqa: F401 - `world` is a fixture
from ytclip import ai
from ytclip.creator import critique, director, orchestrator
from ytclip.settings import Settings
from ytclip.telegram import bot as botmod
from ytclip.telegram import pair, state, tunnel, webapp, webapp_page

SAM = {"id": 5002, "type": "private", "first_name": "Sam", "last_name": "Lee", "username": "sam"}

BOARD = {
    "title": "The Stanford Prison Myth", "category": "Psychology", "target_duration": 62, "music_mood": "Suspense",
    "hook": "It was never an experiment", "caption": "What really happened in that basement.",
    "hashtags": ["psychology", "history"], "prompt": "the truth about the Stanford prison experiment",
    "written_by": "local-model", "sources": [{"title": "A paper", "url": "https://example.org/paper"}],
    "scenes": [
        {"scene_id": 1, "speech_text": "It was never an experiment.", "visual_prompt": "a dim basement corridor"},
        {"scene_id": 2, "speech_text": "The guards were coached on how to behave.", "visual_prompt": "a clipboard",
         "beat": "build", "stock_query": "clipboard office"},
        {"scene_id": 3, "speech_text": "And the famous breakdown was an act.", "visual_prompt": "a locked door"},
    ],
}


def creator_video(root: Path, cid: str = "c-1a2b", title: str = "The Stanford Prison Myth", revises: str = "",
                  studio: dict | None = None, board: dict | None = None) -> Path:
    """A Creator (or Studio) video's folder as it's delivered, with a real storyboard beside the video."""
    folder = root / "Psychology" / "Created" / f"2026-11-08 {title} [{cid}]"
    (folder / "thumbnails").mkdir(parents=True)
    name = f"01 - {title}.mp4"
    make_video(folder / name)
    (folder / "thumbnails" / "01.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 900)
    (folder / "storyboard.json").write_text(json.dumps(dict(board or BOARD, title=title)), encoding="utf-8")
    creator = {"id": cid, "prompt": BOARD["prompt"], "ai_generated": True, "style": "documentary"}
    if revises:
        creator.update(revises=revises, critique="the hook is flat")
    if studio:
        creator["studio"] = studio
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": cid, "title": title, "channel": "ClipMonster Video Creator"},
        "automation": {"origins": ["creator"], "how": "creator", "category": "Psychology", "category_id": "p1"},
        "creator": creator,
        "clips": [{"file": name, "title": title, "score": 100, "origin": "creator", "hook": title,
                   "ai_generated": True, "qc": {"ok": True}}],
    }), encoding="utf-8")
    return folder


def link(w, chat=SAM, later: float = 60.0) -> None:
    with state.edit(w.state_file) as data:
        state.link(data, chat, w.clock[0] + later)
    w.fake.clear()


def to(w, chat) -> list[dict]:
    """The messages sent to one chat, oldest first."""
    return [m for m in w.fake.sent("sendMessage") if m["chat_id"] == chat["id"]]


def writers(w) -> None:
    """An AI that can write scripts (a key for the cloud one; nothing is ever asked in these tests)."""
    w.s.creator_ai, w.s.ai_api_key = "any", "sk-ant-test"


# --- two people share the bot ----------------------------------------------------------------------------------


def test_two_people_at_most_and_a_third_never_uses_up_the_code(world):
    link(world)
    with state.edit(world.state_file) as data:
        code = state.new_code(data, world.clock[0])
    world.say(f"/start {code}", STRANGER)
    reply = to(world, STRANGER)[-1]["text"]
    assert "Two people already share" in reply
    data = state.load(world.state_file)
    assert state.chat_ids(data) == [JUSTYN["id"], SAM["id"]]
    assert state.live_code(data, world.clock[0]) == code and data["strikes"] == {}  # not used up, nothing counted
    world.fake.clear()
    world.say(f"/start {code}", STRANGER)  # told once, then left alone for a while
    assert to(world, STRANGER) == []
    # once someone leaves there's room again, and the same code still works
    world.bot.leave(SAM["id"])
    world.say(f"/start {code}", STRANGER)
    assert "Linked" in to(world, STRANGER)[-1]["text"]


def test_a_telegram_json_from_before_the_limit_keeps_the_two_linked_first(world):
    with state.edit(world.state_file) as data:
        state.link(data, STRANGER, world.clock[0] + 500)
        state.link(data, SAM, world.clock[0] + 50)
    world.bot.token = ""  # as when automation starts
    assert world.bot.configure()
    assert state.chat_ids(state.load(world.state_file)) == [JUSTYN["id"], SAM["id"]]
    assert any("Someone" in line and "unlinked" in line for line in world.logs)


def test_the_window_wont_link_a_third_phone(world):
    link(world)
    lk = pair.Linker(TOKEN, api_factory=world.bot.api_factory, state_file=world.state_file,
                     status_file=world.tmp / "telegram-status.json", now=lambda: world.clock[0])
    lk._link()
    assert lk.phase == "error" and "2 people share this bot already" in lk.error and not lk.code


def test_the_first_person_invites_the_second_and_each_is_told(world):
    world.say("/people")
    text, btns = world.fake.last()["text"], buttons(world.fake.last()["reply_markup"])
    assert "Justyn (@justyn) (you)" in text and ("➕ Invite someone", "ppl:invite") in btns
    world.press("ppl:invite")
    said = world.fake.last()["text"]
    code = state.load(world.state_file)["pairing"]["code"]
    assert f"https://t.me/ClipTestBot?start={code}" in said and f"<code>{code}</code>" in said
    assert state.load(world.state_file)["pairing"]["by"] == JUSTYN["id"]
    world.fake.clear()
    world.say(f"/start {code}", SAM)
    assert "Linked" in to(world, SAM)[0]["text"]
    told = to(world, JUSTYN)[-1]
    assert "<b>Sam</b> now shares this bot with you" in told["text"] and told["disable_notification"] is True
    data = state.load(world.state_file)
    assert state.is_owner(data, JUSTYN["id"]) and not state.is_owner(data, SAM["id"]) and state.full(data)
    assert [(a["what"], a["name"]) for a in data["activity"]] == [("join", "Sam")]
    # the second person can't invite, and with two there's no one left to invite anyway
    with pytest.raises(botmod.Refused) as refused:
        world.bot.invite(SAM["id"])
    assert "Only Justyn can invite" in str(refused.value) and refused.value.status == 403
    with pytest.raises(botmod.Refused, match="2 people already share"):
        world.bot.invite(JUSTYN["id"])
    world.say("/people", SAM)
    btns = buttons(world.fake.last()["reply_markup"])
    assert btns == [("🚪 Leave", "ppl:leave")]
    world.say("/people")
    assert ("✖ Remove Sam", f"ppl:remove:{SAM['id']}") in buttons(world.fake.last()["reply_markup"])


def test_each_is_told_what_the_other_did_and_their_cards_follow(world):
    link(world)
    creator_video(world.root)
    it = world.take()[0]
    assert it.review == "waiting"
    world.bot.show(JUSTYN["id"], it)
    world.bot.show(SAM["id"], it)
    sams_card = max(k[1] for k in world.fake.messages if k[0] == SAM["id"])
    world.fake.clear()
    world.press(f"ok:{it.id}", max(k[1] for k in world.fake.messages if k[0] == JUSTYN["id"]))
    assert world.store.get(it.id).review == "ok"
    told = to(world, SAM)
    assert [m["text"] for m in told] == ["✅ <b>Justyn</b> OK'd “The Stanford Prison Myth” to post."]
    assert told[0]["disable_notification"] is True and to(world, JUSTYN) == []
    assert "Waits for your OK" not in world.fake.messages[(SAM["id"], sams_card)]["caption"]  # Sam's card followed
    assert any("Justyn OK'd" in line for line in world.logs)
    world.fake.clear()
    world.say("/pause", SAM)
    assert "<b>Sam</b> paused automation" in to(world, JUSTYN)[-1]["text"]
    world.say("/make the moon landing", SAM)
    world.press(f"mk:{next(iter(world.bot.pending))}:-", chat=SAM)
    assert "<b>Sam</b> asked for a video about “the moon landing”" in to(world, JUSTYN)[-1]["text"]
    acts = state.load(world.state_file)["activity"]
    assert [(a["name"], a["what"]) for a in acts] == [("Justyn", "ok"), ("Sam", "pause"), ("Sam", "make")]
    assert acts[0]["item"] == it.id and acts[0]["by"] == JUSTYN["id"]


def test_what_one_asked_for_is_shown_to_both(world):
    link(world)
    world.bot.make(SAM["id"], "the truth about the Stanford prison experiment", None)
    job = next(j for j in world.runner.db.jobs() if j.is_creation)
    folder = creator_video(world.root)
    world.runner.db.update(job.id, world.clock[0], status="done", folder=str(folder), clips=1)
    world.take()
    world.fake.clear()
    world.bot.check(world.clock[0])
    videos = world.fake.sent("sendVideo")
    assert {int(v["chat_id"]) for v in videos} == {JUSTYN["id"], SAM["id"]}
    mine = next(v for v in videos if int(v["chat_id"]) == SAM["id"])["caption"]
    theirs = next(v for v in videos if int(v["chat_id"]) == JUSTYN["id"])["caption"]
    assert "Here's the video you asked for" in mine and "The video Sam asked for is made" in theirs


def test_remove_and_leave(world):
    link(world)
    world.bot.modes[SAM["id"]] = ("make", world.clock[0] + 60)
    with pytest.raises(botmod.Refused, match="Only the person linked first"):
        world.bot.remove(SAM["id"], JUSTYN["id"])
    world.say("/people")
    world.press(f"ppl:remove:{SAM['id']}")
    assert "Remove Sam Lee (@sam)?" in world.fake.last()["text"]
    assert state.is_linked(state.load(world.state_file), SAM["id"])  # asked first
    world.fake.clear()
    world.press(f"ppl:REMOVE:{SAM['id']}")
    assert not state.is_linked(state.load(world.state_file), SAM["id"]) and SAM["id"] not in world.bot.modes
    gone = to(world, SAM)[-1]
    assert "Justyn removed this chat" in gone["text"] and gone["reply_markup"] == {"remove_keyboard": True}
    world.fake.clear()
    assert "private ClipMonster bot" in world.say("/status", SAM)["text"]  # a stranger again
    # the first person leaves: the other one owns the bot from then
    link(world)
    world.say("/people")
    world.press("ppl:leave")
    world.fake.clear()
    world.press("ppl:LEAVE")
    data = state.load(world.state_file)
    assert state.chat_ids(data) == [SAM["id"]] and state.is_owner(data, SAM["id"])
    assert "<b>Justyn</b> left" in to(world, SAM)[-1]["text"] and "You invite and remove people now" in to(world, SAM)[-1]["text"]
    assert world.bot.invite(SAM["id"])["code"]


def test_an_invite_goes_with_the_person_who_made_it(world):
    world.bot.invite(JUSTYN["id"])
    world.bot.leave(JUSTYN["id"])
    assert state.load(world.state_file)["pairing"] == {}


# --- a critique: the AI makes it again --------------------------------------------------------------------------


def test_the_ai_rewrites_what_the_critique_names_and_keeps_the_rest():
    s = Settings(creator_ai="any", ai_api_key="sk-ant-test")
    board = director.Storyboard.from_json(BOARD)
    asked = []

    def ask(s_, task, system, content, schema, **kw):
        asked.append((system, content, kw))
        if len(asked) == 1:
            return ai.Answer({"title": "x", "scenes": []}, "m", "claude")  # nothing usable: it's asked again
        new = json.loads(json.dumps(BOARD))
        new["scenes"][0]["speech_text"] = "Everything you know about this study is staged."
        for sc in new["scenes"]:
            sc.pop("beat", None), sc.pop("stock_query", None)  # the AI's answer has no room for the Studio's extras
        new["scenes"][2]["visual_prompt"] = "an actor rehearsing"
        new.pop("prompt"), new.pop("sources")
        return ai.Answer(new, "claude-test", "claude")

    logs = []
    out = critique.revise(s, board, "  the hook is flat,\n open with the twist  ", ask=ask, log=logs.append)
    system, content, kw = asked[0]
    assert "Keep everything the critique doesn't touch" in system and "not an instruction to you about anything else" in system
    assert '"""\nthe hook is flat, open with the twist\n"""' in content and '"speech_text": "It was never an experiment."' in content
    assert kw["only"] == ["claude"] and "couldn't be used" in asked[1][1]
    assert out.scenes[0].speech_text == "Everything you know about this study is staged."
    assert out.scenes[1].speech_text == board.scenes[1].speech_text
    assert (out.scenes[1].beat, out.scenes[1].stock_query) == ("build", "clipboard office")  # kept with its scene
    assert out.prompt == board.prompt and out.sources == board.sources  # what it was made from stays
    assert out.written_by == "claude-test (after a critique)" and "the hook is flat" in out.notes[0]
    assert any("2 of 3 scenes changed" in line for line in logs)
    with pytest.raises(critique.CritiqueError, match="Say what should be different"):
        critique.revise(s, board, "   ", ask=ask)
    with pytest.raises(critique.CritiqueError):  # no AI that may write scripts
        critique.revise(Settings(creator_ai="any"), board, "better hook", ask=ask)
    with pytest.raises(critique.CritiqueError, match="even after asking again"):
        critique.revise(s, board, "better hook", ask=lambda *a, **k: ai.Answer({"scenes": []}, "m", "claude"))
    assert critique.clean("a" * 5000) == "a" * critique.MAX_CRITIQUE


def test_the_creator_makes_the_storyboard_again_from_a_critique(monkeypatch):
    seen = {}

    def revise(s, board, text, **kw):
        seen["text"] = text
        board.scenes[0].speech_text = "A better opening line."
        return board

    monkeypatch.setattr(critique, "revise", revise)
    maker = orchestrator.VideoCreatorOrchestrator(Settings())
    req = orchestrator.CreateRequest.from_json(orchestrator.CreateRequest(
        storyboard=BOARD, critique="the hook is flat", revises="The Stanford Prison Myth").to_json())
    board = maker.storyboard(req)
    assert seen["text"] == "the hook is flat" and board.scenes[0].speech_text == "A better opening line."
    assert maker.revision == {"revises": "The Stanford Prison Myth", "critique": "the hook is flat"}

    def cant(s, board, text, **kw):
        raise critique.CritiqueError("No AI is set up.")

    monkeypatch.setattr(critique, "revise", cant)
    with pytest.raises(orchestrator.CreatorError, match="No AI is set up"):
        orchestrator.VideoCreatorOrchestrator(Settings()).storyboard(req)
    # without a critique a handed-in storyboard is made as it is, as before
    monkeypatch.setattr(critique, "revise", lambda *a, **k: pytest.fail("not asked"))
    plain = orchestrator.VideoCreatorOrchestrator(Settings())
    assert plain.storyboard(orchestrator.CreateRequest(storyboard=BOARD)).scenes[0].speech_text.startswith("It was never")
    assert not hasattr(plain, "revision")


def test_a_critique_queues_the_video_again_holds_the_first_and_both_see_the_new_one(world):
    link(world)
    writers(world)
    creator_video(world.root)
    it = world.take()[0]
    world.store.approve([it.id])  # it was OK'd: it would post as it is
    world.store.save()
    world.fake.clear()
    r = world.bot.fix(SAM["id"], it.id, "The hook is flat.\nOpen with the twist.")
    assert r["held"] and "this one is held meanwhile" in r["message"] and "&" not in r["message"]
    assert world.store.get(it.id).review == "waiting"  # doesn't post as it is while it's made again
    job = world.runner.db.get(r["job"])
    req = orchestrator.CreateRequest.from_json(job.request)
    assert job.is_creation and job.category_id == "p1" and job.origins == ["creator", "telegram", "critique"]
    assert req.critique == "The hook is flat. Open with the twist." and req.revises == it.headline
    assert req.storyboard["scenes"][1]["speech_text"] == BOARD["scenes"][1]["speech_text"] and req.style == "documentary"
    told = to(world, JUSTYN)[-1]["text"]
    assert "<b>Sam</b> asked for “The Stanford Prison Myth” to be made again" in told and "Open with the twist" in told
    data = state.load(world.state_file)
    c = next(iter(data["critiques"].values()))
    assert (c["by"], c["name"], c["item"], c["status"], c["held"]) == (SAM["id"], "Sam", it.id, "queued", "ok")
    with pytest.raises(botmod.Refused, match="being made again already"):
        world.bot.fix(JUSTYN["id"], it.id, "also slower please")
    # automation makes it: the job is working, then done with the new video's folder
    world.runner.db.update(job.id, world.clock[0], status="working", stage="Drawing the pictures 40%")
    world.bot.sync_critiques()
    c = next(iter(state.load(world.state_file)["critiques"].values()))
    assert (c["status"], c["stage"]) == ("working", "Drawing the pictures 40%")
    new_folder = creator_video(world.root, cid="c-9z9z", title="The Staged Experiment", revises=it.headline)
    world.runner.db.update(job.id, world.clock[0], status="done", folder=str(new_folder), clips=1, stage="Done")
    world.s.creator_review = "off"  # even so: a video made again never posts on its own say-so
    new = world.take()[0]
    assert new.review == "waiting" and new.ai_generated
    world.fake.clear()
    world.bot.check(world.clock[0])
    c = next(iter(state.load(world.state_file)["critiques"].values()))
    assert (c["status"], c["fixed"]) == ("done", new.id)
    videos = {int(v["chat_id"]): v["caption"] for v in world.fake.sent("sendVideo")}
    assert "made again from your critique" in videos[SAM["id"]] and "Both versions wait for an OK" in videos[SAM["id"]]
    assert "was made again from Sam's critique" in videos[JUSTYN["id"]]
    assert "Made again from <b>Sam</b>'s critique of the first version" in videos[JUSTYN["id"]]
    assert state.load(world.state_file)["asked"] == {}
    # and the first version's card says what was said about it, and by whom
    assert "<b>Sam</b>: <i>“The hook is flat. Open with the twist.”</i> · made again" in world.bot.card(world.store.get(it.id))[0]


def test_both_people_critiquing_one_video_at_the_same_moment_makes_it_again_once(world):
    import threading

    link(world)
    writers(world)
    creator_video(world.root)
    it = world.take()[0]
    go, got = threading.Barrier(2), []

    def send(who, text):
        go.wait(5)
        try:
            got.append(("ok", world.bot.fix(who["id"], it.id, text)["job"]))
        except botmod.Refused as exc:
            got.append(("no", str(exc)))

    threads = [threading.Thread(target=send, args=(JUSTYN, "The hook is flat.")),
               threading.Thread(target=send, args=(SAM, "The ending is rushed."))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(kind for kind, _ in got) == ["no", "ok"]
    assert "being made again already" in next(said for kind, said in got if kind == "no")
    assert len(state.load(world.state_file)["critiques"]) == 1
    assert len([j for j in world.runner.db.jobs() if j.is_creation]) == 1


def test_a_critique_that_cant_be_answered_gives_the_first_version_back(world):
    writers(world)
    creator_video(world.root)
    it = world.take()[0]
    world.store.approve([it.id])
    r = world.bot.fix(JUSTYN["id"], it.id, "make the ending clearer")
    world.runner.db.update(r["job"], world.clock[0], status="failed", error="The local AI ran out of memory.")
    world.fake.clear()
    world.bot.check(world.clock[0])
    c = next(iter(state.load(world.state_file)["critiques"].values()))
    assert (c["status"], c["error"]) == ("failed", "The local AI ran out of memory.")
    assert world.store.get(it.id).review == "ok"  # as it was before the critique
    said = to(world, JUSTYN)[-1]["text"]
    assert "Couldn't make “The Stanford Prison Myth” again: The local AI ran out of memory." in said
    assert "The first version is as it was" in said
    world.bot.fix(JUSTYN["id"], it.id, "try once more: clearer ending")  # and it can be asked again


def test_what_cant_be_made_again_says_why(world):
    creator_video(world.root)
    it = world.take()[0]
    with pytest.raises(botmod.Refused):  # no AI that may write scripts on this PC
        world.bot.fix(JUSTYN["id"], it.id, "better hook")
    writers(world)
    with pytest.raises(botmod.Refused, match="Say what should be different") as short:
        world.bot.fix(JUSTYN["id"], it.id, " . ")
    assert short.value.status == 400
    with pytest.raises(botmod.Refused, match="isn't in the library") as missing:
        world.bot.fix(JUSTYN["id"], "nope", "better hook")
    assert missing.value.status == 404
    (Path(it.path).parent / "storyboard.json").unlink()
    assert not world.bot.fixable(it)
    with pytest.raises(botmod.Refused, match="storyboard isn't beside it"):
        world.bot.fix(JUSTYN["id"], it.id, "better hook")
    clip_folder(world.root, clips=1)
    clip = world.take()[0]
    assert not world.bot.fixable(clip)
    with pytest.raises(botmod.Refused, match="Only a video the Creator or the Studio made"):
        world.bot.fix(JUSTYN["id"], clip.id, "better hook")
    assert world.runner.db.jobs() == [] and state.load(world.state_file)["critiques"] == {}


def test_a_studio_video_is_made_again_in_its_own_shape(world):
    from ytclip.studio.orchestrator import StudioRequest, read_request

    writers(world)
    creator_video(world.root, studio={"canvas": "1920x1080", "timeline_file": "timeline.json"})
    it = world.take()[0]
    r = world.bot.fix(JUSTYN["id"], it.id, "slower at the end")
    req = read_request(world.runner.db.get(r["job"]).request)
    assert isinstance(req, StudioRequest) and req.orientation == "landscape" and req.critique == "slower at the end"
    assert botmod._shape("1080x1920") == "vertical" and botmod._shape("1080x1080") == "square" and botmod._shape("") == ""


def test_saying_what_to_fix_from_the_chat(world):
    writers(world)
    creator_video(world.root)
    it = world.take()[0]
    world.bot.show(JUSTYN["id"], it)
    assert ("🛠 Say what to fix", f"fix:{it.id}") in buttons(world.fake.last("sendVideo")["reply_markup"])
    world.press(f"fix:{it.id}")
    assert "What should be different about “The Stanford Prison Myth”?" in world.fake.last()["text"]
    reply = world.say("the hook is flat")
    assert reply["text"].startswith("🛠 Got it: the AI makes it again with that fixed.")
    assert [j.is_creation for j in world.runner.db.jobs()] == [True]
    assert "Open a video the AI made" in world.say("/fix whatever")["text"]  # no video picked
    clip_folder(world.root, clips=1)
    clip = world.take()[0]
    world.bot.show(JUSTYN["id"], clip)
    assert not any(d.startswith("fix:") for _, d in buttons(world.fake.last("sendVideo")["reply_markup"]))


# --- the Mini App's sign-in -------------------------------------------------------------------------------------


def init_data(user: dict, at: float, token: str = TOKEN, **extra) -> str:
    """What Telegram hands a Mini App for this user, signed with the bot's token."""
    fields = {"auth_date": str(int(at)), "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
              "user": json.dumps({k: v for k, v in user.items() if k != "type"}, separators=(",", ":")), **extra}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_only_what_telegram_signed_for_this_bot_is_believed():
    now = 1_800_000_000.0
    good = init_data(SAM, now - 60)
    assert webapp.verify_init_data(good, TOKEN, now) == {"id": SAM["id"], "first_name": "Sam", "last_name": "Lee",
                                                        "username": "sam"}
    for bad, why in [
        (good.replace("Sam", "Sue"), "didn't check out"),  # changed after signing
        (init_data(SAM, now - 60, token="999:someoneElsesBot"), "didn't check out"),  # another bot's signature
        (good.replace("hash=", "hush="), "didn't check out"),
        (init_data(SAM, now - 2 * 86400), "too old"),
        (init_data(SAM, now + 3600), "too old"),  # from the future
        ("", "Open this from the bot's chat"),
        ("x" * 9000, "Open this from the bot's chat"),
        (good.split("hash=")[0] + "hash=" + "é" * 64, "didn't check out"),  # not even hex
    ]:
        with pytest.raises(webapp.AuthError, match=why):
            webapp.verify_init_data(bad, TOKEN, now)
    with pytest.raises(webapp.AuthError):
        webapp.verify_init_data(good, "", now)
    nobody = init_data({"first_name": "No id"}, now)
    with pytest.raises(webapp.AuthError, match="no user"):
        webapp.verify_init_data(nobody, TOKEN, now)


# --- the Mini App's server ----------------------------------------------------------------------------------------


@pytest.fixture
def served(world):
    """The world with its Mini App served on a local port, and the second person linked."""
    link(world)
    writers(world)
    app = webapp.WebApp(world.bot, now=lambda: world.clock[0], port=0)
    app.serve(0)
    world.app = app

    def call(method, path, body=None, token="", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        head = dict(headers or {})
        if token:
            head["Authorization"] = f"tma {token}"
        raw = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        if raw is not None:
            head.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=raw, headers=head)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        kind = r.getheader("Content-Type") or ""
        return r.status, (json.loads(data) if kind.startswith("application/json") and data else data), r

    def session(user):
        status, d, _ = call("POST", "/api/session", {"init_data": init_data(user, world.clock[0] - 5)})
        assert status == 200, d
        return d

    world.call, world.session = call, session
    yield world
    app._close()


def test_the_page_is_public_and_holds_nothing_private(served):
    status, page, r = served.call("GET", "/")
    assert status == 200 and b"<title>ClipMonster</title>" in page
    csp = r.getheader("Content-Security-Policy")
    assert "script-src 'self' https://telegram.org;" in csp and "frame-ancestors https://web.telegram.org" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert b"<script>" not in page and b'src="app.js"' in page and b'href="app.css"' in page  # found beside the page
    status, js, r = served.call("GET", "/app.js")
    assert status == 200 and r.getheader("Content-Type").startswith("text/javascript") and b"api/session" in js
    assert served.call("GET", "/app.css")[2].getheader("Content-Type").startswith("text/css")
    for text in (webapp_page.PAGE, webapp_page.JS, webapp_page.CSS):
        assert TOKEN not in text and "innerHTML" not in text and "eval(" not in text
    assert served.call("GET", "/nothing-here")[0] == 401  # everything else needs a session


def test_only_the_linked_people_get_in(served):
    w = served
    d = w.session(SAM)
    assert d["me"] == {"id": SAM["id"], "name": "Sam", "owner": False} and d["pc"] and d["most"] == 2
    assert [(p["first"], p["owner"], p["you"]) for p in d["people"]] == [("Justyn", True, False), ("Sam", False, True)]
    status, err, _ = w.call("POST", "/api/session", {"init_data": init_data(STRANGER, w.clock[0])})
    assert status == 401 and "isn't linked to ClipMonster" in err["error"] and err["code"] == "unlinked"
    status, err, _ = w.call("POST", "/api/session", {"init_data": init_data(SAM, w.clock[0], token="1:other")})
    assert status == 401 and "didn't check out" in err["error"] and err["code"] == "signin"
    for path in ("/api/videos", "/api/status", "/api/activity", "/api/me", "/api/posted", "/api/categories"):
        assert w.call("GET", path)[0] == 401 and w.call("GET", path, token="1.2.abc")[0] == 401
        assert w.call("GET", path, token=d["token"])[0] == 200, path
    # a session made up by someone who knows a linked person's id isn't one
    forged = f"{JUSTYN['id']}.{int(w.clock[0] + 999)}.{'0' * 40}"
    assert w.call("GET", "/api/videos", token=forged)[0] == 401
    with pytest.raises(webapp.AuthError):  # nor is one whose signature isn't plain text
        w.app.who(f"{JUSTYN['id']}.{int(w.clock[0] + 999)}.{'é' * 40}")
    # it lasts a day, then the page signs in again
    w.clock[0] += webapp.SESSION_LIFE + 5
    status, err, _ = w.call("GET", "/api/videos", token=d["token"])
    assert (status, err["code"]) == (401, "expired")
    w.clock[0] -= webapp.SESSION_LIFE + 5
    # and someone removed from the bot is out at once, session or not
    w.bot.remove(JUSTYN["id"], SAM["id"])
    status, err, _ = w.call("GET", "/api/videos", token=d["token"])
    assert status == 401 and "isn't linked" in err["error"]


def test_the_name_follows_telegram(served):
    w = served
    w.session(dict(SAM, first_name="Samantha", last_name="", username="samantha"))
    c = next(c for c in state.load(w.state_file)["chats"] if c["id"] == SAM["id"])
    assert (c["name"], c["username"]) == ("Samantha", "samantha")


def test_watching_a_video_in_the_app(served):
    w = served
    folder = creator_video(w.root)
    it = w.take()[0]
    d = w.session(SAM)
    status, vids, _ = w.call("GET", "/api/videos", token=d["token"])
    v = vids["items"][0]
    assert (v["id"], v["state"], v["ai_generated"], v["fixable"], v["fixing"], v["critiques"]) == (
        it.id, "review", True, True, False, [])
    whole = (folder / "01 - The Stanford Prison Myth.mp4").read_bytes()
    url = f"/api/videos/{it.id}/file?m={d['media']}"
    status, data, r = w.call("GET", url)
    assert status == 200 and data == whole and r.getheader("Accept-Ranges") == "bytes" and r.getheader("Content-Type") == "video/mp4"
    status, data, r = w.call("GET", url, headers={"Range": "bytes=100-299"})  # a phone's player asks in pieces
    assert status == 206 and data == whole[100:300] and r.getheader("Content-Range") == f"bytes 100-299/{len(whole)}"
    status, data, r = w.call("GET", url, headers={"Range": "bytes=-50"})
    assert status == 206 and data == whole[-50:]
    status, data, r = w.call("GET", url, headers={"Range": f"bytes={len(whole) - 10}-"})
    assert status == 206 and data == whole[-10:]
    assert w.call("GET", url, headers={"Range": f"bytes={len(whole) + 5}-"})[0] == 416
    status, data, r = w.call("GET", f"/api/videos/{it.id}/thumb?m={d['media']}")
    assert status == 200 and data[:3] == b"\xff\xd8\xff"
    # the address of a video opens videos and nothing else; the session doesn't open videos by address
    assert w.call("GET", f"/api/videos/{it.id}/file?m={d['token']}")[0] == 401
    assert w.call("GET", f"/api/videos/{it.id}/file")[0] == 401
    assert w.call("GET", "/api/videos", token=d["media"])[0] == 401
    assert w.call("GET", f"/api/videos/nope/file?m={d['media']}")[0] == 404
    assert w.call("GET", f"/api/videos/..%2F..%2Fetc/file?m={d['media']}")[0] == 401  # not a video's address at all


def test_a_video_is_sent_in_pieces_without_holding_its_file(served, monkeypatch):
    """Windows won't delete a file that's open, and a paused player holds its connection: the file is only
    open while a piece is read."""
    w = served
    folder = creator_video(w.root)
    it = w.take()[0]
    d = w.session(SAM)
    path = folder / "01 - The Stanford Prison Myth.mp4"
    whole = path.read_bytes()
    monkeypatch.setattr(webapp, "CHUNK", 97)
    assert len(whole) > 5 * 97
    opened, real_open = [], open

    def counting(file, *a, **k):
        if Path(str(file)) == path:
            opened.append(1)
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", counting)
    url = f"/api/videos/{it.id}/file?m={d['media']}"
    status, data, _ = w.call("GET", url)
    assert status == 200 and data == whole and len(opened) == -(-len(whole) // 97)
    status, data, _ = w.call("GET", url, headers={"Range": "bytes=50-449"})
    assert status == 206 and data == whole[50:450]
    monkeypatch.undo()
    # gone from the PC between two pieces: the answer just ends, and the next ask says it's gone
    path.unlink()
    assert w.call("GET", url)[0] in (404, 410)


def test_ok_and_delete_from_the_app_carry_the_name(served):
    w = served
    creator_video(w.root)
    creator_video(w.root, cid="c-2222", title="Another One")
    a, b = sorted(w.take(), key=lambda i: i.headline != "The Stanford Prison Myth")
    w.bot.show(JUSTYN["id"], a)
    card = max(k[1] for k in w.fake.messages if k[0] == JUSTYN["id"])
    sam = w.session(SAM)
    w.fake.clear()
    status, out, _ = w.call("POST", f"/api/videos/{a.id}/ok", {}, token=sam["token"])
    assert status == 200 and out["video"]["state"] == "queued" and w.store.get(a.id).review == "ok"
    assert [m["text"] for m in to(w, JUSTYN)] == ["✅ <b>Sam</b> OK'd “The Stanford Prison Myth” to post."]
    assert "Waits for your OK" not in w.fake.messages[(JUSTYN["id"], card)]["caption"]  # the chat's card followed
    assert w.call("POST", f"/api/videos/{a.id}/ok", {}, token=sam["token"])[0] == 409  # didn't need one
    status, out, _ = w.call("POST", f"/api/videos/{b.id}/delete", {}, token=sam["token"])
    assert status == 200 and w.store.get(b.id) is None and not Path(b.path).exists()
    assert "<b>Sam</b> deleted “Another One”" in to(w, JUSTYN)[-1]["text"]
    status, acts, _ = w.call("GET", "/api/activity", token=w.session(JUSTYN)["token"])
    assert [(x["name"], x["what"], x["title"], x["mine"]) for x in acts["items"]] == [
        ("Sam", "delete", "Another One", False), ("Sam", "ok", "The Stanford Prison Myth", False)]
    assert w.call("POST", f"/api/videos/{b.id}/ok", {}, token=sam["token"])[0] == 404
    status, out, _ = w.call("POST", "/api/pause", {"paused": True}, token=sam["token"])
    assert status == 200 and w.runner.db.meta("paused") == "1"
    assert w.call("GET", "/api/status", token=sam["token"])[1]["paused"] is True
    assert "<b>Sam</b> paused automation" in to(w, JUSTYN)[-1]["text"]


def test_a_critique_from_the_app(served):
    w = served
    creator_video(w.root)
    it = w.take()[0]
    sam, justyn = w.session(SAM), w.session(JUSTYN)
    w.fake.clear()
    status, out, _ = w.call("POST", f"/api/videos/{it.id}/critique", {"text": "The hook is flat."}, token=sam["token"])
    assert status == 200 and out["critique"]["name"] == "Sam" and out["critique"]["status"] == "queued"
    assert "Got it: the AI makes it again" in out["message"]
    status, vids, _ = w.call("GET", "/api/videos", token=justyn["token"])
    v = vids["items"][0]
    assert v["fixing"] is True and [(c["name"], c["text"], c["status"]) for c in v["critiques"]] == [
        ("Sam", "The hook is flat.", "queued")]
    status, acts, _ = w.call("GET", "/api/activity", token=justyn["token"])
    assert acts["items"][0]["what"] == "critique" and acts["items"][0]["text"] == "The hook is flat."
    assert [(c["name"], c["title"]) for c in acts["going"]] == [("Sam", "The Stanford Prison Myth")]
    assert "<b>Sam</b> asked for “The Stanford Prison Myth” to be made again" in to(w, JUSTYN)[-1]["text"]
    status, err, _ = w.call("POST", f"/api/videos/{it.id}/critique", {"text": "and slower"}, token=justyn["token"])
    assert status == 409 and "being made again already" in err["error"]
    status, err, _ = w.call("POST", f"/api/videos/{it.id}/critique", {"text": ""}, token=justyn["token"])
    assert status == 400
    # made: the new version shows up under both, tied to what was said
    new_folder = creator_video(w.root, cid="c-9z9z", title="The Staged Experiment", revises=it.headline)
    w.runner.db.update(out["job"], w.clock[0], status="done", folder=str(new_folder), clips=1)
    new = w.take()[0]
    status, vids, _ = w.call("GET", "/api/videos", token=justyn["token"])
    by_id = {v["id"]: v for v in vids["items"]}
    assert by_id[new.id]["fix_of"] == it.id and by_id[new.id]["state"] == "review"
    assert by_id[it.id]["critiques"][0]["fixed"] == new.id and by_id[it.id]["fixing"] is False


def test_asking_for_a_video_and_sharing_from_the_app(served):
    w = served
    sam, justyn = w.session(SAM), w.session(JUSTYN)
    status, cats, _ = w.call("GET", "/api/categories", token=sam["token"])
    assert [c["name"] for c in cats["items"]] == ["Psychology", "Gaming"]
    w.fake.clear()
    status, out, _ = w.call("POST", "/api/make", {"idea": "  the moon   landing ", "category": "p1"}, token=sam["token"])
    assert status == 200 and "a video about “the moon landing” for Psychology" in out["message"] and "<" not in out["message"]
    assert "It shows up under Videos when it's made, and waits for an OK" in out["message"]
    job = w.runner.db.get(out["job"])
    assert job.is_creation and job.category_id == "p1"
    assert state.load(w.state_file)["asked"][f"job:{job.id}"]["chat"] == SAM["id"]  # it comes back to Sam
    assert to(w, SAM) == [] and "<b>Sam</b> asked for a video about “the moon landing”" in to(w, JUSTYN)[-1]["text"]
    assert w.call("POST", "/api/make", {"idea": ""}, token=sam["token"])[0] == 400
    assert w.call("POST", "/api/make", {"idea": "x", "category": "gone"}, token=sam["token"])[0] == 400
    # people
    assert w.call("POST", "/api/people/invite", {}, token=sam["token"])[0] == 403
    assert w.call("POST", "/api/people/invite", {}, token=justyn["token"])[0] == 409  # two already
    assert w.call("POST", "/api/people/remove", {"id": JUSTYN["id"]}, token=sam["token"])[0] == 403
    assert w.call("POST", "/api/people/remove", {"id": "everyone"}, token=justyn["token"])[0] == 400
    status, out, _ = w.call("POST", "/api/people/remove", {"id": SAM["id"]}, token=justyn["token"])
    assert status == 200 and "Sam Lee (@sam) can't use this bot any more" in out["message"]
    assert w.call("GET", "/api/me", token=sam["token"])[0] == 401
    status, inv, _ = w.call("POST", "/api/people/invite", {}, token=justyn["token"])
    assert status == 200 and inv["link"] == f"https://t.me/ClipTestBot?start={inv['code']}" and inv["minutes"] == 10
    status, out, _ = w.call("POST", "/api/people/leave", {}, token=justyn["token"])
    assert status == 200 and state.load(w.state_file)["chats"] == []


def test_the_server_keeps_its_guard_up(served):
    w = served
    sam = w.session(SAM)
    status, err, _ = w.call("POST", "/api/make", b"x" * (webapp.BODY_MAX + 1), token=sam["token"])
    assert status == 413
    assert w.call("POST", "/api/make", b"not json", token=sam["token"])[0] == 400
    assert w.call("POST", "/api/make", b"[1]", token=sam["token"])[0] == 400
    assert w.call("GET", "/api/nothing", token=sam["token"])[0] == 404
    assert w.call("POST", "/api/videos/x/explode", {}, token=sam["token"])[0] == 404
    assert w.app.httpd.server_address[0] == "127.0.0.1"  # only this PC connects; the tunnel carries it out
    # something breaking inside is an answer, not a dropped connection, and never the token
    w.app.host.videos = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}"))
    status, err, _ = w.call("GET", "/api/videos", token=sam["token"])
    assert status == 500 and "Something went wrong on the PC" in err["error"]
    assert not any(TOKEN in line for line in w.logs)


# --- the Mini App's address, and the chats' button into it --------------------------------------------------------


class FakeTunnel:
    made: list = []

    def __init__(self, port, log=None):
        self.port, self.url, self.error, self.stopped = port, "", "", False
        FakeTunnel.made.append(self)

    def start(self):
        pass

    def stop(self):
        self.stopped = True


def test_the_app_gets_an_address_and_each_chat_a_button_into_it(world):
    link(world)
    FakeTunnel.made.clear()
    app = webapp.WebApp(world.bot, now=lambda: world.clock[0], port=0, tunnel_factory=FakeTunnel)
    world.bot.webapp = app
    try:
        app.configure()
        tun = FakeTunnel.made[0]
        assert app.httpd is not None and tun.port == app.port and not world.bot.app_known  # still starting
        world.bot.sync_menus()
        assert world.fake.menus == {}  # nothing is touched until there's something to say
        tun.url = "https://four-random-words.trycloudflare.com"
        app.configure()
        assert world.bot.app_url == tun.url
        world.bot.tell_once()
        want = {"type": "web_app", "text": "ClipMonster", "web_app": {"url": tun.url + "/"}}
        assert world.fake.menus == {JUSTYN["id"]: want, SAM["id"]: want}
        hello = to(world, JUSTYN)
        assert "is an app inside Telegram now" in hello[0]["text"] and hello[0]["reply_markup"] == {"remove_keyboard": True}
        assert buttons(hello[1]["reply_markup"]) == [(botmod.B_APP, tun.url + "/")]
        assert hello[1]["reply_markup"]["inline_keyboard"][0][0]["web_app"] == {"url": tun.url + "/"}
        # the tunnel comes back with a new address (as it does each start): the buttons follow, without a word
        world.fake.clear()
        tun.url = "https://another-four-words.trycloudflare.com"
        app.configure()
        world.bot.tell_once()
        assert world.fake.menus[SAM["id"]]["web_app"]["url"] == tun.url + "/" and world.fake.sent("sendMessage") == []
        # someone removed gets the plain menu back
        world.bot.remove(JUSTYN["id"], SAM["id"])
        world.bot.tell_once()
        assert world.fake.menus[SAM["id"]] == {"type": "commands"}
        assert str(SAM["id"]) not in state.load(world.state_file)["menus"]
        # with the app up, the chat points into it instead of listing and uploading
        creator_video(world.root)
        it = world.take()[0]
        world.fake.clear()
        world.bot.show(JUSTYN["id"], it)
        assert world.fake.sent("sendVideo") == []
        assert buttons(world.fake.last()["reply_markup"]) == [("▶ Watch it in the app", f"{tun.url}/?v={it.id}")]
        assert "in the app" in world.say("/videos")["reply_markup"]["inline_keyboard"][0][0]["text"]
        assert "Everything is in the app" in world.say("/help")["text"]
        assert "opens inside Telegram" in world.say("/app")["text"]
        # the tunnel drops: said once, and the chat works by itself again
        tun.url, tun.error = "", "Cloudflare isn't giving out a quick tunnel right now."
        app.configure()
        world.bot.tell_once()
        assert world.fake.menus[JUSTYN["id"]] == {"type": "commands"} and world.bot.app_url == ""
        assert "Cloudflare isn't giving out" in world.say("/app")["text"]
        world.fake.clear()
        world.bot.show(JUSTYN["id"], it)
        assert len(world.fake.sent("sendVideo")) == 1
        # an address of your own: no tunnel is run
        world.s.telegram_app_url = "https://clips.example.com/app/"
        app.configure()
        assert tun.stopped and app.tunnel is None and world.bot.app_url == "https://clips.example.com/app"
        world.bot.tell_once()
        assert world.fake.menus[JUSTYN["id"]]["web_app"]["url"] == "https://clips.example.com/app/"
        world.s.telegram_app_url = "http://192.168.1.20:47330"
        app.configure()
        assert world.bot.app_url == "" and "must start with https://" in world.bot.app_error
        # turned off in Settings: the server stops too
        world.s.telegram_app = False
        app.configure()
        assert app.httpd is None and world.bot.app_url == "" and world.bot.app_error == ""
        assert "turned off" in world.say("/app")["text"]
    finally:
        app._close()


def test_a_port_someone_else_has_is_said_in_words(world):
    import socket

    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    world.s.telegram_app_port = taken.getsockname()[1]
    app = webapp.WebApp(world.bot, now=lambda: world.clock[0], tunnel_factory=FakeTunnel)
    try:
        app.configure()
        assert app.httpd is None and "can't be used" in world.bot.app_error and "Mini App port" in world.bot.app_error
        taken.close()
        app.configure()  # freed: it's tried again on the next look
        assert app.httpd is not None and app.port == world.s.telegram_app_port
    finally:
        taken.close()
        app._close()


def test_someone_who_joins_while_the_app_is_up_is_welcomed_into_it_once(world):
    world.bot.set_app_url("https://four-random-words.trycloudflare.com")
    world.bot.tell_once()
    code = world.bot.invite(JUSTYN["id"])["code"]
    world.fake.clear()
    world.say(f"/start {code}", SAM)
    world.bot.tell_once()
    mine = to(world, SAM)
    assert len(mine) == 1 and "Everything is in the app" in mine[0]["text"]  # not told about the app twice
    assert buttons(mine[0]["reply_markup"]) == [(botmod.B_APP, "https://four-random-words.trycloudflare.com/")]
    assert world.fake.menus[SAM["id"]]["type"] == "web_app"


def test_stopping_the_bot_takes_the_button_back(world):
    FakeTunnel.made.clear()
    app = webapp.WebApp(world.bot, now=lambda: world.clock[0], port=0, tunnel_factory=FakeTunnel)
    world.bot.webapp = app
    app.configure()
    FakeTunnel.made[0].url = "https://four-random-words.trycloudflare.com"
    app.configure()
    world.bot.tell_once()
    assert world.fake.menus[JUSTYN["id"]]["type"] == "web_app"
    world.bot.stop()
    assert world.fake.menus[JUSTYN["id"]] == {"type": "commands"} and app.httpd is None and FakeTunnel.made[0].stopped


# --- cloudflared ----------------------------------------------------------------------------------------------------

LOG = """2026-11-11T01:00:00Z INF Thank you for trying Cloudflare Tunnel.
2026-11-11T01:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-11-11T01:00:01Z INF +--------------------------------------------------------------------------------------------+
2026-11-11T01:00:01Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-11-11T01:00:01Z INF |  https://four-random-words.trycloudflare.com                                               |
2026-11-11T01:00:01Z INF +--------------------------------------------------------------------------------------------+
2026-11-11T01:00:02Z INF Registered tunnel connection connIndex=0 connection=abc event=0 ip=1.2.3.4 location=ewr01 protocol=quic
"""


class FakeProcess:
    """cloudflared, as far as the tunnel can tell: it writes its log, then runs until it's told to stop."""

    def __init__(self, lines: str, hold: bool = True):
        self.done = threading.Event()
        self.lines, self.hold = lines.splitlines(keepends=True), hold
        self.stdout = self._read()
        self.args = None

    def _read(self):
        yield from self.lines
        if self.hold:
            self.done.wait(5)

    def poll(self):
        return 0 if self.done.is_set() else None

    def terminate(self):
        self.done.set()

    kill = terminate

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return 0


def test_the_tunnel_reads_its_address_from_cloudflared(tmp_path):
    procs, logs = [], []

    def popen(args, **kw):
        procs.append(FakeProcess(LOG))
        procs[-1].args = args
        return procs[-1]

    exe = tmp_path / "cloudflared"
    t = tunnel.Tunnel(47330, logs.append, popen=popen, finder=lambda: exe)
    t.start()
    for _ in range(200):
        if t.url:
            break
        time.sleep(0.01)
    assert t.url == "https://four-random-words.trycloudflare.com" and t.error == ""
    assert procs[0].args == [str(exe), "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:47330"]
    t.stop()
    assert t.url == "" and procs[0].done.is_set()


def test_a_tunnel_that_cant_start_says_why(tmp_path):
    logs = []
    limited = "2026-11-11T01:00:00Z ERR failed to request quick Tunnel: 429 Too Many Requests\n"
    t = tunnel.Tunnel(1, logs.append, popen=lambda args, **kw: FakeProcess(limited, hold=False), finder=lambda: tmp_path / "c")
    t.run_once(tmp_path / "c")
    assert t.url == "" and "isn't giving out a quick tunnel right now" in t.error
    t = tunnel.Tunnel(1, logs.append, popen=lambda args, **kw: FakeProcess("some trouble\n", hold=False))
    t.run_once(tmp_path / "c")
    assert "stopped without giving an address (some trouble)" in t.error
    # not on this PC and it can't be downloaded: said once, in words
    def no_download(log=None):
        raise tunnel.TunnelError("cloudflared couldn't be downloaded (no internet).")

    t = tunnel.Tunnel(1, logs.append, finder=lambda: None, fetcher=no_download)
    with pytest.raises(tunnel.TunnelError):
        t._exe()
    t._problem("cloudflared couldn't be downloaded (no internet).")
    t._problem("cloudflared couldn't be downloaded (no internet).")
    assert sum("couldn't be downloaded" in line for line in logs) == 1
    with pytest.raises(tunnel.TunnelError, match="downloads are off"):
        tunnel.Tunnel(1, finder=lambda: None, allow_download=False)._exe()


def test_cloudflared_is_found_or_downloaded_once(tmp_path, monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    assert tunnel.find(tmp_path) is None
    fetched = []

    def fetch(url, dest, label, progress=None, min_bytes=0):
        fetched.append(url)
        Path(dest).write_bytes(b"\x7fELF" + b"0" * tunnel.MIN_BYTES)
        return dest

    monkeypatch.setattr(tunnel.sys, "platform", "win32")
    monkeypatch.setattr(tunnel, "machine", lambda: "amd64")
    exe = tunnel.download(tmp_path, fetch=fetch)
    assert exe == tmp_path / "cloudflared.exe" and exe.stat().st_size > tunnel.MIN_BYTES
    assert fetched == ["https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"]
    assert tunnel.find(tmp_path) == exe  # there next time: nothing is downloaded again
    monkeypatch.setattr(tunnel, "machine", lambda: "riscv64")
    with pytest.raises(tunnel.TunnelError, match="can't be downloaded automatically"):
        tunnel.download(tmp_path / "other", fetch=fetch)

    def broken(url, dest, label, progress=None, min_bytes=0):
        raise OSError("Couldn't download cloudflared.exe: timed out")

    monkeypatch.setattr(tunnel, "machine", lambda: "amd64")
    (tmp_path / "x").mkdir()
    with pytest.raises(tunnel.TunnelError, match="couldn't be downloaded"):
        tunnel.download(tmp_path / "x", fetch=broken)
    assert not (tmp_path / "x" / "cloudflared.exe").exists()
    for line in LOG.splitlines():
        m = tunnel.ADDRESS.search(line)
        assert m is None or m.group(0) == "https://four-random-words.trycloudflare.com"
    assert tunnel.ADDRESS.search("see https://developers.cloudflare.com/ and https://www.trycloudflare.com/terms") is None


def test_the_window_says_how_the_app_is_doing():
    from ytclip.gui.telegram_page import app_line

    assert app_line({"running": False, "app": "https://x.trycloudflare.com"}) == ""
    assert "The Mini App is up" in app_line({"running": True, "app": "https://x.trycloudflare.com", "app_on": True})
    assert "has no address: no internet" in app_line({"running": True, "app": "", "app_error": "no internet"})
    assert "getting its address" in app_line({"running": True, "app": "", "app_error": "", "app_on": True})
    assert app_line({"running": True}) == ""  # a heartbeat from before the Mini App
