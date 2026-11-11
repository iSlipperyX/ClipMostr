"""The Telegram bot (ytclip/telegram/): linking a phone, asking for videos from the chat, watching, OK'ing and
deleting what waits to post, and what it tells you by itself. Telegram is stood in for by telegram_fake, so
the bot's real Bot API code (JSON, multipart uploads, errors) runs against it."""

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_fake import JUSTYN, ME, TOKEN, FakeTelegram, buttons, parse_multipart
from ytclip.ffmpeg import ffmpeg_exe
from ytclip.jobs import Job, JobDB
from ytclip.pipeline import Runner
from ytclip.posting import intake
from ytclip.posting.accounts import Accounts
from ytclip.posting.store import Store
from ytclip.settings import SCHEMA, TELEGRAM_SENDS, Settings
from ytclip.telegram import api as tg
from ytclip.telegram import bot as botmod
from ytclip.telegram import pair, preview, state

STRANGER = {"id": 6666, "type": "private", "first_name": "Someone", "username": "someone"}


def make_video(path: Path, seconds: float = 2.0, size: str = "360x640", bitrate: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s={size}:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    *(["-b:v", bitrate] if bitrate else []), "-c:a", "aac", str(path)], check=True)
    return path


def clip_folder(root: Path, name: str = "Gaming/2026-11-08 Big Talk [abc]", clips: int = 2) -> Path:
    """A YouTube video's clip folder as automation writes it, with real little videos."""
    folder = root / name
    (folder / "thumbnails").mkdir(parents=True)
    rows = []
    for n in range(1, clips + 1):
        file = f"{n:02d} - moment {n}.mp4"
        make_video(folder / file)
        (folder / file).with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
        (folder / "thumbnails" / f"{n:02d}.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 500)
        rows.append({"file": file, "rank": n, "title": f"moment {n}", "score": 80 - n, "hook": f"Hook number {n}",
                     "start": 60.0 * n, "end": 60.0 * n + 40, "length": 40.0, "qc": {"ok": True}})
    (folder / "README.md").write_text("# Big Talk\n\n" + "".join(f"| {r['rank']} | [{r['file']}](<{r['file']}>) |\n"
                                                              for r in rows), encoding="utf-8")
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "abc", "title": "Big Talk", "channel": "Pod People", "duration": 3600},
        "automation": {"origins": ["channel"], "how": "auto", "category": "Gaming"},
        "clips": rows}), encoding="utf-8")
    return folder


def creator_folder(root: Path) -> Path:
    """A Video Creator video's folder as the Creator writes it (orchestrator._deliver), with scene pictures."""
    folder = root / "Psychology" / "Created" / "2026-11-08 The Stanford Prison Myth [c-1a2b]"
    (folder / "scenes").mkdir(parents=True)
    (folder / "thumbnails").mkdir()
    name = "01 - The Stanford Prison Myth.mp4"
    make_video(folder / name)
    (folder / "thumbnails" / "01.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 900)
    for i in range(1, 4):
        (folder / "scenes" / f"scene_{i:02d}.png").write_bytes(b"\x89PNG" + b"\x00" * 40_000)
    (folder / "storyboard.json").write_text(json.dumps({"title": "The Stanford Prison Myth", "scenes": []}),
                                            encoding="utf-8")
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "c-1a2b", "title": "The Stanford Prison Myth", "channel": "ClipMonster Video Creator"},
        "automation": {"origins": ["creator"], "how": "creator", "category": "Psychology", "category_id": "p1"},
        "creator": {"id": "c-1a2b", "prompt": "the truth about the Stanford prison experiment", "ai_generated": True},
        "clips": [{"file": name, "title": "The Stanford Prison Myth", "score": 100, "origin": "creator",
                   "hook": "It was never an experiment", "ai_generated": True, "qc": {"ok": True}}],
    }), encoding="utf-8")
    return folder


@pytest.fixture
def world(tmp_path):
    """A bot with a linked phone, on a stand-in Telegram, with automation's queue and posting's library."""
    w = SimpleNamespace(tmp=tmp_path, fake=FakeTelegram(), logs=[], clock=[time.time()])
    accs = Accounts.load()
    a = accs.add("")
    a.username, a.logged_in = "psych", True
    accs.save()
    w.accounts, w.account = accs, a
    w.root = tmp_path / "Automation"
    w.s = Settings(telegram_on=True, telegram_token=TOKEN, auto_output_dir=str(w.root), creator_review="always",
                   categories=[{"id": "p1", "name": "Psychology", "account_id": a.id},
                               {"id": "g1", "name": "Gaming", "account_id": a.id}])
    w.runner = Runner(db=JobDB(tmp_path / "jobs.db"), load=lambda: w.s, log=w.logs.append)
    w.store = Store(tmp_path / "lib.json")
    w.state_file = tmp_path / "telegram.json"
    w.bot = botmod.Bot(w.runner, api_factory=lambda t: tg.Api(t, transport=w.fake.transport, sleep=lambda s: None),
                       state_file=w.state_file, status_file=tmp_path / "telegram-status.json",
                       previews=tmp_path / "previews", now=lambda: w.clock[0], posting=(w.store, accs, None))
    assert w.bot.configure()
    with state.edit(w.state_file) as data:
        state.link(data, JUSTYN, w.clock[0])
    w.fake.clear()

    def say(text, chat=None):
        w.bot.handle(w.fake.message(text, chat))
        return w.fake.sent("sendMessage")[-1] if w.fake.sent("sendMessage") else None

    def press(data, message_id=None, chat=None):
        mid = message_id or max(k[1] for k in w.fake.messages)
        w.bot.handle(w.fake.press(data, mid, chat))
        return mid

    def take():
        items, _ = intake.take(w.store, w.accounts, w.s)
        w.store.save()
        return items

    w.say, w.press, w.take = say, press, take
    return w


def msg_id(w) -> int:
    """The newest message the bot sent."""
    return max(k[1] for k in w.fake.messages)


def last_buttons(w, method="sendMessage"):
    return buttons(w.fake.last(method).get("reply_markup"))


# --- the Bot API side -------------------------------------------------------------------------------------


def test_calls_errors_and_the_token_never_shows():
    fake = FakeTelegram()
    waited = []
    api = tg.Api(TOKEN, transport=fake.transport, sleep=waited.append)
    assert api.get_me()["username"] == "ClipTestBot"
    fake.errors["sendMessage"] = [(429, "Too Many Requests: retry after 2", 2)]
    assert api.send(5001, "hi")["message_id"]  # waited out, then sent
    assert waited == [2.5]
    fake.errors["sendMessage"] = [(400, f"Bad Request: chat not found for bot{TOKEN}", 0)]
    with pytest.raises(tg.ApiError) as exc:
        api.send(5001, "hi")
    assert exc.value.code == 400 and TOKEN not in str(exc.value) and "<token>" in str(exc.value)

    def offline(url, body, headers, timeout):
        raise tg.NetworkError(f"can't reach {url}")

    with pytest.raises(tg.NetworkError) as net:
        tg.Api(TOKEN, transport=offline).get_me()
    assert TOKEN not in str(net.value) and TOKEN.split(":")[1] not in str(net.value)
    assert tg.masked(f"x{TOKEN}y", TOKEN) == "x<token>y"


def test_what_telegram_said_in_words():
    assert "doesn't know this token" in tg.why(tg.ApiError(401, "Unauthorized"))
    assert "webhook" in tg.why(tg.ApiError(409, "Conflict: can't use getUpdates method while webhook is active"))
    assert "Another program is using this bot" in tg.why(tg.ApiError(409, "Conflict: terminated by other getUpdates"))
    assert "couldn't be reached" in tg.why(tg.NetworkError("timed out"))
    assert tg.looks_like_token(TOKEN) and not tg.looks_like_token("123:short") and not tg.looks_like_token("abc")


def test_a_video_goes_up_as_a_multipart_form(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000)
    body, kind = tg.multipart({"chat_id": 5001, "caption": "Größe “quotes”", "supports_streaming": True,
                               "reply_markup": {"inline_keyboard": []}, "skip": None}, {"video": clip})
    got = parse_multipart(body, kind)
    assert got["chat_id"] == "5001" and got["caption"] == "Größe “quotes”" and got["supports_streaming"] == "true"
    assert got["reply_markup"] == {"inline_keyboard": []} and "skip" not in got
    assert got["video"]["size"] == clip.stat().st_size


def test_long_text_is_cut_at_a_line():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    cut = tg.clip_text(text, tg.TEXT_LIMIT)
    assert len(cut) <= tg.TEXT_LIMIT and cut.endswith("\n…") and cut.split("\n")[-2].startswith("line")


def test_a_long_line_is_cut_without_breaking_its_html():
    text = "<b>" + "word &amp; " * 600 + "</b>"
    cut = tg.clip_text(text, tg.CAPTION_LIMIT)
    assert len(cut) <= tg.CAPTION_LIMIT and cut.endswith("…</b>")
    assert cut.count("&") == cut.count("&amp;")  # no half entity
    link = "x" * 1000 + '<a href="https://www.tiktok.com/@psych/video/1">open</a>' + "y" * 100
    cut = tg.clip_text(link, tg.CAPTION_LIMIT)
    assert "<a" not in cut or "</a>" in cut


# --- linking a phone -----------------------------------------------------------------------------------------


def test_a_pairing_code_links_once(tmp_path):
    data = state.empty()
    code = state.new_code(data, 1000.0)
    assert len(code) == 6 and set(code) <= set(state.CODE_CHARS)
    assert state.looks_like_code(code.lower()) and state.looks_like_code(f"/start {code}")
    assert not state.looks_like_code("hello there") and not state.looks_like_code("/start")
    assert state.try_code(data, 1, f"  {code[:3].lower()} {code[3:]} ", 1001.0) == "linked"
    assert state.try_code(data, 1, code, 1002.0) == "none"  # used up


def test_wrong_codes_lock_a_chat_out_for_an_hour():
    data = state.empty()
    state.new_code(data, 0.0)
    said = [state.try_code(data, 9, "AAAAAA", float(i)) for i in range(5)]
    assert said == ["wrong"] * 4 + ["blocked"]
    code = state.new_code(data, 10.0)
    assert state.try_code(data, 9, code, 11.0) == "blocked"  # even the right one, for now
    later = 11.0 + state.STRIKE_PAUSE
    assert state.try_code(data, 9, state.new_code(data, later), later) == "linked"


def test_a_code_runs_out_and_a_new_bot_forgets_the_old_phones():
    data = state.empty()
    code = state.new_code(data, 0.0)
    assert state.try_code(data, 1, code, state.CODE_LIFE + 1) == "expired"
    state.set_bot(data, ME)
    state.link(data, JUSTYN, 5.0)
    assert not state.set_bot(data, ME) and state.chat_ids(data) == [5001]
    assert state.set_bot(data, {"id": 999, "username": "OtherBot"}) and data["chats"] == []


def test_strangers_are_only_told_how_to_link(world):
    reply = world.say("/pause", STRANGER)
    assert "private ClipMonster bot" in reply["text"] and reply["chat_id"] == STRANGER["id"]
    assert world.runner.db.meta("paused") != "1"  # nothing a stranger says does anything
    world.fake.clear()
    world.say("/status", STRANGER)
    assert world.fake.sent("sendMessage") == []  # told a moment ago: quiet
    world.say("/status", {"id": -100123, "type": "group", "title": "A group"})
    assert world.fake.sent("sendMessage") == []  # groups are never answered
    world.press("pause", 1, STRANGER)
    assert world.fake.last("answerCallbackQuery")["show_alert"] is True and world.runner.db.meta("paused") != "1"


def test_the_code_from_the_window_links_a_phone(world):
    with state.edit(world.state_file) as data:
        code = state.new_code(data, world.clock[0])
    assert "isn't right" in world.say("ZZZZZZ", STRANGER)["text"]
    world.say(f"/start {code}", STRANGER)
    reply = [m for m in world.fake.sent("sendMessage") if m["chat_id"] == STRANGER["id"]][-1]
    assert "Linked" in reply["text"] and reply["reply_markup"]["keyboard"][0][0]["text"] == botmod.B_STATUS
    assert set(state.chat_ids(state.load(world.state_file))) == {JUSTYN["id"], STRANGER["id"]}
    assert world.say("/status", STRANGER)["text"].startswith("<b>ClipMonster</b>")


# --- what you can ask for ---------------------------------------------------------------------------------------


def test_status_says_what_automation_is_doing(world):
    db = world.runner.db
    db.add(Job(id="w1", url="u", title="A long podcast", status="working"), 1.0)
    db.add(Job(id="q1", url="u", title="next"), 1.0)
    db.add(Job(id="q2", url="u", title="after"), 1.0)
    db.add(Job(id="create-x", url="creator:x", title="An idea", kind="creator"), 1.0)
    clip_folder(world.root)
    held = world.take()
    world.store.get(held[0].id).review = "waiting"
    reply = world.say("/status")
    text = reply["text"]
    assert "Clipping “A long podcast”" in text and "2 videos to clip · 1 Creator video to make" in text
    assert "2 videos waiting to post" in text and "1 video waits for your OK" in text and "GB free" in text
    assert ("⏸ Pause", "pause") in last_buttons(world) and ("📌 Keep this up to date", "pin") in last_buttons(world)
    world.press("pause")
    assert db.meta("paused") == "1"
    shown = world.fake.messages[(JUSTYN["id"], max(k[1] for k in world.fake.messages))]
    assert "Paused" in shown["text"] and ("▶ Resume", "resume") in buttons(shown["markup"])
    world.press("resume")
    assert db.meta("paused") == "0" and db.meta("nudge") == "1"


def test_pause_and_resume_from_the_menu(world):
    assert "Paused" in world.say(botmod.B_PAUSE)["text"] and world.runner.db.meta("paused") == "1"
    assert "Carrying on" in world.say(botmod.B_PAUSE)["text"] and world.runner.db.meta("paused") == "0"


def test_make_a_video_from_the_chat(world):
    reply = world.say("/make the real story of the Bermuda Triangle")
    assert "Which category" in reply["text"]
    choices = buttons(reply["reply_markup"])
    assert [c[0] for c in choices[:2]] == ["Psychology", "Gaming"] and ("No category", choices[2][1]) in choices
    world.press(choices[0][1])
    (job,) = world.runner.db.jobs()
    assert job.is_creation and job.origins == ["creator", "telegram"] and job.category_id == "p1"
    assert json.loads(job.request)["prompt"] == "the real story of the Bermuda Triangle"
    said = world.fake.last()["text"]
    assert "for Psychology" in said and "for your OK before it posts" in said and "It's next" in said
    assert state.load(world.state_file)["asked"][f"job:{job.id}"]["chat"] == JUSTYN["id"]
    assert world.fake.last("editMessageReplyMarkup")  # the question's buttons go once it's answered
    world.press(choices[1][1])  # the same question twice: it's gone
    assert len(world.runner.db.jobs()) == 1 and "expired" in world.fake.last("answerCallbackQuery")["text"]


def test_plain_words_or_the_menu_lead_to_a_video(world):
    reply = world.say("volcanoes that could end the world")
    assert ("✨ Make a video about it", buttons(reply["reply_markup"])[0][1]) in buttons(reply["reply_markup"])
    world.press(buttons(reply["reply_markup"])[0][1])
    assert "Which category" in world.fake.last()["text"]
    world.say(botmod.B_MAKE)
    assert "What should the video be about" in world.fake.last()["text"]
    reply = world.say("the moon landing tapes")
    assert "“the moon landing tapes”" in reply["text"] and "Which category" in reply["text"]


def test_send_a_link_to_clip_it(world):
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    reply = world.say(f"clip this please {url}")
    assert "Clip 1 video" in reply["text"]
    world.press(buttons(reply["reply_markup"])[1][1])  # Gaming
    job = world.runner.db.get("dQw4w9WgXcQ")
    assert job is not None and job.category_id == "g1" and job.how == "manual"
    assert "Queued 1 video to clip for Gaming" in world.fake.last()["text"]
    assert "job:dQw4w9WgXcQ" in state.load(world.state_file)["asked"]
    world.press(buttons(world.say(url)["reply_markup"])[1][1])
    assert "clipped before" in world.fake.last()["text"]


def test_a_request_is_read_from_plain_words(world):
    cats = [SimpleNamespace(name="Gaming", id="g1"), SimpleNamespace(name="True Crime", id="t1")]
    got = botmod.parse_request("10 clips 30-45s true crime, only the twists", cats)
    assert (got["count"], got["length"], got["category"].id, got["notes"]) == (10, (30.0, 45.0), "t1",
                                                                               "only the twists")
    got = botmod.parse_request("3 https://youtu.be/abc12345678 gaming", cats)
    assert got["links"] == ["https://youtu.be/abc12345678"] and got["count"] == 3 and got["category"].id == "g1"
    got = botmod.parse_request("funny reactions", cats)
    assert got["count"] == 5 and got["length"] is None and got["category"] is None and got["notes"] == "funny reactions"
    # without a YouTube key automation can't pick videos: it says so and asks for nothing
    assert "YouTube Data API key" in world.say("/request 10 30-45s Gaming funny reactions only")["text"]
    assert world.runner.db.orders() == []
    world.s = world.s.copy(youtube_api_key="AIza-test")
    world.say("/request 10 30-45s Gaming funny reactions only")
    (o,) = world.runner.db.orders()
    assert (o.count, o.min_len, o.max_len, o.category_id, o.notes) == (10, 30.0, 45.0, "g1", "funny reactions only")
    assert "Asked for 10 clips of 30–45 s for Gaming" in world.fake.last()["text"]
    assert f"order:{o.id}" in state.load(world.state_file)["asked"]


def test_requests_can_be_listed_and_cancelled(world):
    world.s = world.s.copy(youtube_api_key="AIza-test")
    world.say("/request 4 Gaming clutch plays")
    (o,) = world.runner.db.orders()
    reply = world.say("/requests")
    assert "clutch plays" in reply["text"]
    cancel = next(d for label, d in buttons(reply["reply_markup"]) if d.startswith("cancel:"))
    world.press(cancel)
    world.press(f"CANCEL:{o.id}")
    assert world.runner.db.get_order(o.id).status == "cancelled"
    assert f"order:{o.id}" not in state.load(world.state_file)["asked"]


# --- the videos ---------------------------------------------------------------------------------------------------


def test_videos_lists_what_waits_for_you_first(world):
    clip_folder(world.root)
    one, two = world.take()
    with world.store.lock:
        two.review = "waiting"
        one.planned_at = world.clock[0] + 3600
    reply = world.say(botmod.B_VIDEOS)
    assert "2 videos" in reply["text"] and "1 waits for your OK" in reply["text"]
    labels = [label for label, data in buttons(reply["reply_markup"]) if data.startswith("v:")]
    assert labels[0].startswith("⏸ Hook number 2") and "Hook number 1" in labels[1] and "@psych" in labels[1]


def test_watching_a_video_sends_the_video_itself(world):
    clip_folder(world.root)
    one, _two = world.take()
    world.press(f"v:{one.id}", 1)
    sent = world.fake.last("sendVideo")
    assert sent["video"]["size"] == Path(one.path).stat().st_size  # small enough: the file itself
    assert "<b>Hook number 1</b>" in sent["caption"] and "@psych · Gaming" in sent["caption"]
    assert "From “Big Talk” (Pod People)" in sent["caption"]
    assert [d for _l, d in buttons(sent["reply_markup"])] == [f"del:{one.id}", "list:0"]  # no posting worker here
    world.bot._posting = (world.store, world.accounts, SimpleNamespace(posting=True))
    world.press(f"v:{one.id}", 1)
    assert ("🚀 Post it now", f"now:{one.id}") in buttons(world.fake.last("sendVideo")["reply_markup"])


def test_a_video_goes_in_the_background_and_the_chat_keeps_answering(world, monkeypatch):
    clip_folder(world.root, clips=1)
    (it,) = world.take()
    gate = threading.Event()
    real = preview.for_telegram

    def slow(*args, **kwargs):  # a long encode or upload
        assert gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(preview, "for_telegram", slow)
    bot = world.bot
    bot._uploader = threading.Thread(target=bot._video_loop, daemon=True)
    bot._uploader.start()
    try:
        world.press(f"v:{it.id}", 1)
        assert world.fake.last("answerCallbackQuery")["text"] == "Sending the video…"
        world.press(f"v:{it.id}", 1)
        assert world.fake.last("answerCallbackQuery")["text"] == "It's on its way"  # not twice
        world.say("/status")
        assert world.fake.last()["text"].startswith("<b>ClipMonster</b>")  # answered meanwhile
        assert world.fake.sent("sendVideo") == []
        assert world.fake.last("sendChatAction")["action"] == "upload_video"
        gate.set()
        deadline = time.time() + 15
        while (not world.fake.sent("sendVideo") or bot.sending) and time.time() < deadline:
            time.sleep(0.02)
        assert len(world.fake.sent("sendVideo")) == 1 and bot.sending == set()
    finally:
        gate.set()
        bot.stop_event.set()
        bot._uploader.join(5)


def test_something_going_wrong_is_said_not_swallowed(world, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk unplugged")

    monkeypatch.setattr(world.bot, "status_html", broken)
    world.say("/status")
    assert world.fake.last()["text"] == "⚠ Something went wrong on the PC: disk unplugged"
    world.press("st", 1)
    answer = world.fake.last("answerCallbackQuery")
    assert answer["text"] == "Something went wrong on the PC: disk unplugged" and answer["show_alert"] is True
    assert any("disk unplugged" in line for line in world.logs)


def test_pictures_and_shared_links(world):
    world.bot.handle({"update_id": 90, "message": {"message_id": 3, "chat": dict(JUSTYN), "photo": [{"file_id": "x"}]}})
    assert world.fake.last()["text"].startswith("I can only read words and links.")
    world.bot.handle({"update_id": 91, "message": {"message_id": 4, "chat": dict(JUSTYN), "photo": [{"file_id": "x"}],
                                                   "caption": "this one https://youtu.be/dQw4w9WgXcQ"}})
    assert "Which category are the clips for?" in world.fake.last()["text"]


def test_news_isnt_kept_while_the_bot_is_off(world):
    world.bot.api = None
    world.bot.on_event(world.account, "posted", "✔ Posted “Hook” to @psych.", True)
    assert world.bot.outbox.empty()


def test_a_big_video_goes_as_a_smaller_copy(world, monkeypatch):
    clip_folder(world.root, clips=1)
    (it,) = world.take()
    make_video(Path(it.path), bitrate="3M")  # a sharp one, bigger than its copy will be
    monkeypatch.setattr(preview, "LIMIT", Path(it.path).stat().st_size - 1)
    monkeypatch.setattr(preview, "plan", lambda duration, limit=0: (150_000, 320))
    world.press(f"v:{it.id}", 1)
    sent = world.fake.last("sendVideo")
    copy = world.tmp / "previews" / f"{it.id}.mp4"
    assert copy.is_file() and sent["video"]["size"] == copy.stat().st_size < Path(it.path).stat().st_size
    assert "A smaller copy" in sent["caption"]
    made = copy.stat().st_mtime
    world.press(f"v:{it.id}", 1)
    assert copy.stat().st_mtime == made  # made once
    assert preview.prune({it.id}, world.tmp / "previews") == 0 and preview.prune(set(), world.tmp / "previews") == 1


def test_ok_a_held_video_from_the_phone(world):
    clip_folder(world.root, clips=1)
    (it,) = world.take()
    it.review = "waiting"
    world.press(f"v:{it.id}", 1)
    sent = world.fake.last("sendVideo")
    assert buttons(sent["reply_markup"])[0] == ("✅ OK to post", f"ok:{it.id}")
    mid = max(k[1] for k in world.fake.messages)
    world.press(f"ok:{it.id}", mid)
    assert Store(world.store.path).get(it.id).review == "ok"  # saved
    card = world.fake.messages[(JUSTYN["id"], mid)]
    assert "Waits for your OK" not in card["caption"] and ("✅ OK to post", f"ok:{it.id}") not in buttons(card["markup"])
    assert "OK: it posts" in world.fake.last("answerCallbackQuery")["text"]


def test_delete_asks_first_then_clears_the_files(world):
    folder = clip_folder(world.root)
    one, two = world.take()
    other = world.store.copy_for(one.id, world.account.id)  # queued twice: both go
    world.store.save()
    world.press(f"v:{one.id}", 1)
    mid = max(k[1] for k in world.fake.messages)
    world.press(f"del:{one.id}", mid)
    assert buttons(world.fake.messages[(JUSTYN["id"], mid)]["markup"]) == [("🗑 Yes, delete it", f"DEL:{one.id}"),
                                                                          ("↩ Keep it", f"keep:{one.id}")]
    world.press(f"keep:{one.id}", mid)
    assert Path(one.path).is_file() and ("🗑 Delete", f"del:{one.id}") in buttons(
        world.fake.messages[(JUSTYN["id"], mid)]["markup"])
    world.press(f"del:{one.id}", mid)
    world.press(f"DEL:{one.id}", mid)
    assert not Path(one.path).exists() and not Path(one.path).with_suffix(".srt").exists()
    assert not (folder / "thumbnails" / "01.jpg").exists() and Path(two.path).is_file()
    rows = json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]
    assert [r["file"] for r in rows] == [Path(two.path).name]
    assert "01 - moment 1.mp4" not in (folder / "README.md").read_text(encoding="utf-8")
    disk = Store(world.store.path)
    assert disk.get(one.id) is None and disk.get(other.id) is None and disk.get(two.id) is not None
    assert "Deleted from the PC" in world.fake.messages[(JUSTYN["id"], mid)]["caption"]
    assert any("🗑 Justyn deleted “Hook number 1”" in line for line in world.logs)
    assert world.take() == []  # automation doesn't bring it back


def test_a_video_whose_file_is_gone_is_taken_off_the_list(world):
    clip_folder(world.root, clips=1)
    (it,) = world.take()
    Path(it.path).unlink()
    world.press(f"v:{it.id}", 1)
    msg = world.fake.last()
    assert msg["text"].startswith("⚠ The video file isn't on the PC any more.")
    assert buttons(msg["reply_markup"]) == [("🗑 Take it off the list", f"DEL:{it.id}")]
    world.press(f"DEL:{it.id}", msg_id(world))
    assert world.store.get(it.id) is None
    mid = msg_id(world)
    assert "Taken off the list" in world.fake.messages[(JUSTYN["id"], mid)]["text"]


def test_a_creator_card_says_what_it_was_made_from(world):
    creator_folder(world.root)
    (it,) = world.take()
    card = botmod.card_html(it, world.accounts, world.clock[0])
    assert "✨ Made from “the truth about the Stanford prison experiment”" in card
    assert "AI-generated, labelled as such" in card
    it.facts["creator"] = {"prompt": "your script", "written_by": "your script"}
    assert "✨ Made from your own script" in botmod.card_html(it, world.accounts, world.clock[0])


def test_deleting_the_last_creator_video_clears_its_folder(world):
    folder = creator_folder(world.root)
    (it,) = world.take()
    world.press(f"DEL:{it.id}", 1)
    assert not folder.exists() and not folder.parent.exists() and (world.root / "Psychology").is_dir()
    assert world.store.get(it.id) is None


def test_nothing_is_deleted_while_it_posts_or_once_posted(world):
    from ytclip.posting import records
    from ytclip.posting.config import Settings as PostingSettings

    clip_folder(world.root, clips=1)
    (it,) = world.take()
    it.status = "uploading"
    world.press(f"DEL:{it.id}", 1)
    assert Path(it.path).is_file() and "being posted right now" in world.fake.last()["text"]
    it.status = "posted"
    with pytest.raises(ValueError, match="posted already"):
        records.discard(world.store, it.id, PostingSettings(), [world.root])


def test_a_file_that_wont_go_keeps_its_entry(world):
    from ytclip.posting import records
    from ytclip.posting.config import Settings as PostingSettings

    clip_folder(world.root, clips=1)
    (it,) = world.take()

    def stuck(path):
        raise PermissionError("in use by another program")

    r = records.discard(world.store, it.id, PostingSettings(), [world.root], remove=stuck)
    assert r.removed == [] and r.failed and world.store.get(it.id) is not None and Path(it.path).is_file()


def test_post_now_from_the_phone(world):
    from ytclip.posting.actions import Actions

    clip_folder(world.root, clips=1)
    (it,) = world.take()
    it.review = "waiting"
    svc = SimpleNamespace(posting=True, actions=Actions(world.tmp / "actions.db"), wake=threading.Event())
    world.bot._posting = (world.store, world.accounts, svc)
    world.press(f"now:{it.id}", 1)
    (req,) = svc.actions.active()
    assert (req.kind, req.item, req.account) == ("post_now", it.id, world.account.id) and svc.wake.is_set()
    assert world.store.get(it.id).review == "ok"  # posting it now is your OK
    world.account.logged_in = False
    assert world.bot.press_now(JUSTYN["id"], 1, it.id, False).startswith("Sign the account in")


# --- what it tells you ----------------------------------------------------------------------------------------------


def test_held_videos_are_sent_once_each_a_few_at_a_time(world):
    world.bot.check(world.clock[0])  # the first look only marks where it starts
    folder = clip_folder(world.root, clips=4)
    items = world.take()
    for it in items:
        it.review = "waiting"
    assert folder.is_dir()
    sent = []
    for step in range(1, 5):
        world.bot.check(world.clock[0] + 20 * step)
        sent.append(len(world.fake.sent("sendVideo")))
    assert sent == [3, 4, 4, 4]
    assert "waits for your OK" in world.fake.last("sendVideo")["caption"]
    assert world.fake.sent("sendMessage") == []  # held ones aren't in the "new clips" message too


def test_videos_already_waiting_when_a_phone_links_are_one_message(world):
    clip_folder(world.root, clips=3)
    for it in world.take():
        it.review = "waiting"
    world.bot.check(world.clock[0])  # the first look since the phone was linked
    assert world.fake.sent("sendVideo") == []  # not three uploads in a row
    msg = world.fake.last()
    assert msg["text"].startswith("⏸ <b>3 videos wait for your OK</b>")
    assert [d for _l, d in buttons(msg["reply_markup"])] == ["list:0"]
    world.fake.clear()
    world.bot.check(world.clock[0] + 20)
    assert world.fake.calls == []  # told once: they're in /videos
    clip_folder(world.root, name="Gaming/2026-11-08 Other [def]", clips=1)
    (later,) = world.take()
    later.review = "waiting"
    world.bot.check(world.clock[0] + 40)
    assert len(world.fake.sent("sendVideo")) == 1  # one held later still comes with its video
    with state.edit(world.state_file) as data:
        state.unlink(data, JUSTYN["id"])
    world.bot.check(world.clock[0] + 60)
    data = state.load(world.state_file)
    assert data["seen_at"] == 0.0 and data["noted"] == {}  # no phone: nothing piles up for the next one


def test_held_videos_are_in_the_new_clips_message_when_review_isnt_ticked(world):
    world.s = world.s.copy(telegram_send="new")
    world.bot.check(world.clock[0])
    clip_folder(world.root)
    for it in world.take():
        it.review = "waiting"
    world.bot.check(world.clock[0] + 20)
    assert world.fake.sent("sendVideo") == []
    assert world.fake.last()["text"].startswith("<b>📥 2 new clips from “Big Talk”</b>")


def test_lots_of_new_videos_at_once_are_one_message(world):
    world.bot.check(world.clock[0])
    for n in range(4):
        clip_folder(world.root, name=f"Gaming/2026-11-08 Talk {n} [v{n}]", clips=1)
    world.take()
    world.bot.check(world.clock[0] + 20)
    (msg,) = world.fake.sent("sendMessage")
    assert msg["text"] == "<b>📥 4 new clips</b> from 4 videos. /videos lists them."


def test_new_clips_come_as_one_message_per_video(world):
    world.bot.check(world.clock[0])
    clip_folder(world.root)
    one, two = world.take()
    world.bot.check(world.clock[0] + 20)
    msg = world.fake.last()
    assert msg["text"].startswith("<b>📥 2 new clips from “Big Talk”</b>")
    assert [d for _l, d in buttons(msg["reply_markup"])] == [f"v:{one.id}", f"v:{two.id}", "list:0"]
    world.fake.clear()
    world.bot.check(world.clock[0] + 40)
    assert world.fake.calls == []
    world.s = world.s.copy(telegram_send="review, posted")
    clip_folder(world.root, name="Gaming/2026-11-08 Other [def]", clips=1)
    world.take()
    world.bot.check(world.clock[0] + 60)
    assert world.fake.sent("sendMessage") == []  # "new" isn't ticked


def test_posting_news_goes_out_as_settings_say(world):
    bot = world.bot
    bot.on_event(world.account, "posted", "✔ Posted “Hook” to @psych.", True)
    bot.on_event(world.account, "milestone", "🎉 @psych passed 1,000 followers", False)  # not an alert: no
    bot.on_event(world.account, "queued", "2 new clips queued for @psych.", False)  # not news
    bot.tell_once()
    assert [m["text"] for m in world.fake.sent("sendMessage")] == ["✔ Posted “Hook” to @psych."]
    world.s = world.s.copy(telegram_send="review, problems")
    world.fake.clear()
    bot.on_event(world.account, "posted", "✔ Posted “Hook 2” to @psych.", True)
    bot.on_event(world.account, "login_needed", "⚠ TikTok account @psych needs signing in again <now>", True)
    bot.tell_once()
    assert [m["text"] for m in world.fake.sent("sendMessage")] == [
        "⚠ TikTok account @psych needs signing in again &lt;now&gt;"]


def test_automation_problems_reach_the_phone(world):
    world.runner.telegram = world.bot
    world.runner.block("Sign in to confirm you're not a bot", world.clock[0])
    world.bot.tell_once()
    assert "YouTube asked this PC to sign in" in world.fake.last()["text"]


def test_the_weekly_report_goes_to_telegram(world):
    from ytclip import weekly

    w = weekly.Week(since=world.clock[0] - 7 * 86400, until=world.clock[0], clips_made=12, videos_clipped=3,
                    creator_made=2)
    world.bot.send_week(w)
    world.bot.tell_once()
    text = world.fake.last()["text"]
    assert text.startswith("<b>📅 Your week,") and "automation made 12 clips from 3 YouTube videos" in text
    world.s = world.s.copy(telegram_send="review")
    world.fake.clear()
    world.bot.send_week(w)
    world.bot.tell_once()
    assert world.fake.sent("sendMessage") == []


def test_what_you_asked_for_comes_back_to_you(world):
    world.bot.check(world.clock[0])
    world.say("/make the truth about the Stanford prison experiment")
    world.press(buttons(world.fake.last()["reply_markup"])[0][1])
    (job,) = world.runner.db.jobs()
    folder = creator_folder(world.root)
    world.runner.db.update(job.id, world.clock[0], status="done", folder=str(folder), clips=1)
    (it,) = world.take()
    assert it.review == "waiting"
    world.fake.clear()
    world.bot.check(world.clock[0] + 20)
    (sent,) = world.fake.sent("sendVideo")
    assert sent["chat_id"] == str(JUSTYN["id"]) and "Here's the video you asked for" in sent["caption"]
    assert "waits for your OK" in sent["caption"] and state.load(world.state_file)["asked"] == {}
    world.bot.check(world.clock[0] + 40)
    assert len(world.fake.sent("sendVideo")) == 1  # not again as a held video, nor as a new one


def test_clips_you_asked_for_come_back_as_a_list(world):
    world.bot.check(world.clock[0])
    world.say("https://www.youtube.com/watch?v=abc12345678")
    world.press(buttons(world.fake.last()["reply_markup"])[1][1])
    folder = clip_folder(world.root)
    world.runner.db.update("abc12345678", world.clock[0], status="done", folder=str(folder), clips=2)
    world.take()
    world.fake.clear()
    world.bot.check(world.clock[0] + 20)
    (msg,) = world.fake.sent("sendMessage")
    assert msg["text"].startswith("<b>✂️ Your 2 clips from “Big Talk”</b>")


def test_a_failed_ask_says_why(world):
    world.say("/make the moon")
    world.press(buttons(world.fake.last()["reply_markup"])[0][1])
    (job,) = world.runner.db.jobs()
    world.runner.db.update(job.id, world.clock[0], status="failed", error="The local AI isn't set up")
    world.bot.check(world.clock[0])
    assert "Couldn't make “the moon”: The local AI isn't set up" in world.fake.last()["text"]


def test_cards_follow_what_happens_elsewhere(world):
    world.bot.check(world.clock[0])
    clip_folder(world.root, clips=1)
    (it,) = world.take()
    it.review = "waiting"
    world.bot.check(world.clock[0] + 20)
    mid = max(k[1] for k in world.fake.messages)
    assert "Waits for your OK" in world.fake.messages[(JUSTYN["id"], mid)]["caption"]
    world.store.approve([it.id])  # OK'd in the window's Queue tab
    world.bot.check(world.clock[0] + 40)
    card = world.fake.messages[(JUSTYN["id"], mid)]
    assert "Waits for your OK" not in card["caption"] and ("🗑 Delete", f"del:{it.id}") in buttons(card["markup"])
    world.store.remove([it.id])  # "Don't post" in the window
    world.bot.check(world.clock[0] + 60)
    assert world.fake.messages[(JUSTYN["id"], mid)]["caption"] == "🗑 Gone from the library."
    assert state.load(world.state_file)["shown"] == {}


def test_a_chat_that_blocked_the_bot_is_skipped_until_it_writes(world):
    world.fake.errors["sendMessage"] = [(403, "Forbidden: bot was blocked by the user", 0)]
    assert world.bot.broadcast("hello") == 0
    assert state.load(world.state_file)["chats"][0]["blocked"] is True
    assert world.bot.broadcast("hello again") == 0 and world.fake.sent("sendMessage")[-1]["text"] == "hello"
    world.say("/help")
    assert not state.load(world.state_file)["chats"][0].get("blocked")
    assert world.bot.broadcast("back") == 1


def test_the_dashboard_keeps_itself_up_to_date(world):
    world.say("/dashboard")
    mid = max(k[1] for k in world.fake.messages)
    assert world.fake.last("pinChatMessage")["message_id"] == mid
    world.runner.db.set_meta("paused", "1")
    world.bot.update_dashboards()
    assert "Paused" in world.fake.messages[(JUSTYN["id"], mid)]["text"]
    world.say("/dashboard off")
    assert not state.load(world.state_file)["chats"][0].get("dashboard")


# --- running with automation ------------------------------------------------------------------------------------------


def test_the_bot_listens_answers_and_remembers_where_it_was(world):
    bot = world.bot
    world.fake.updates.append(world.fake.message("/status"))
    bot.start()
    try:
        deadline = time.time() + 10
        while not world.fake.sent("sendMessage") and time.time() < deadline:
            time.sleep(0.02)
    finally:
        bot.stop()
    assert world.fake.sent("sendMessage")[0]["text"].startswith("<b>ClipMonster</b>")
    assert state.load(world.state_file)["offset"] == world.fake._update + 1
    assert not bot.running and json.loads((world.tmp / "telegram-status.json").read_text())["polling"] is False


def test_another_program_on_the_same_bot_is_said_once(world):
    world.fake.errors["getUpdates"] = [(409, "Conflict: terminated by other getUpdates request", 0)] * 3
    t = threading.Thread(target=world.bot._listen, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not world.bot.error and time.time() < deadline:
        time.sleep(0.02)
    world.bot.stop_event.set()
    t.join(5)
    assert "Another program is using this bot" in world.bot.error
    assert sum("Another program" in line for line in world.logs) == 1


def test_automation_starts_and_stops_the_bot_with_it(world, monkeypatch):
    world.s = world.s.copy(telegram_on=False)  # it waits quietly, without calling Telegram
    world.runner.poster = SimpleNamespace(service=SimpleNamespace(listeners=[]))
    monkeypatch.setattr(state, "status_path", lambda: world.tmp / "status-real.json")
    monkeypatch.setattr(state, "path", lambda: world.tmp / "telegram-real.json")
    bot = world.runner.start_telegram()
    try:
        assert bot.running and world.runner.poster.service.listeners == [bot.on_event]
    finally:
        bot.stop()
    assert not bot.running


def test_posting_tells_its_listeners(tmp_path):
    from ytclip.posting.service import Service

    heard = []
    svc = Service(store=Store(tmp_path / "lib.json"), accounts=Accounts.load(), status_file=tmp_path / "s.json",
                  live_folder=tmp_path / "live")
    svc.listeners.append(lambda *a: heard.append(a))
    svc.listeners.append(lambda *a: 1 / 0)  # a broken listener changes nothing
    svc.event(None, "posted", "✔ Posted", alert=True, item="the item")
    assert heard == [(None, "posted", "✔ Posted", True, "the item")]


# --- the window's side -----------------------------------------------------------------------------------------------


def test_the_window_links_a_phone_while_automation_is_off(tmp_path):
    fake = FakeTelegram()
    lk = pair.Linker(TOKEN, api_factory=lambda t: tg.Api(t, transport=fake.transport), state_file=tmp_path / "t.json",
                     status_file=tmp_path / "s.json")
    lk.start()
    deadline = time.time() + 5
    while lk.phase != "waiting" and time.time() < deadline:
        time.sleep(0.01)
    assert lk.phase == "waiting" and lk.link == f"https://t.me/ClipTestBot?start={lk.code}"
    fake.updates.append(fake.message("hi"))
    fake.updates.append(fake.message(f"/start {lk.code}"))
    lk.join(5)
    assert lk.phase == "linked" and lk.linked == "Justyn (@justyn)"
    assert "private ClipMonster bot" in fake.sent("sendMessage")[0]["text"]
    assert "Linked" in fake.sent("sendMessage")[1]["text"]
    assert state.chat_ids(state.load(tmp_path / "t.json")) == [JUSTYN["id"]]


def test_the_window_leaves_the_listening_to_automations_bot(tmp_path):
    fake = FakeTelegram()
    state.write_status({"pid": os.getpid(), "at": time.time(), "polling": True, "bot_id": ME["id"]},
                       tmp_path / "s.json")
    lk = pair.Linker(TOKEN, api_factory=lambda t: tg.Api(t, transport=fake.transport), state_file=tmp_path / "t.json",
                     status_file=tmp_path / "s.json")
    lk.start()
    deadline = time.time() + 5
    while not lk.by_automation and time.time() < deadline:
        time.sleep(0.01)
    with state.edit(tmp_path / "t.json") as data:  # automation's bot links the chat
        state.link(data, JUSTYN, time.time())
    lk.join(5)
    assert lk.phase == "linked" and fake.sent("getUpdates") == []


def test_checking_the_token_and_sending_a_test():
    fake = FakeTelegram()
    factory = lambda t: tg.Api(t, transport=fake.transport)  # noqa: E731
    assert pair.check_token(TOKEN, factory) == ("@ClipTestBot", "")
    assert "doesn't look like a bot token" in pair.check_token("hello", factory)[1]
    assert "doesn't know this token" in pair.check_token("123456789:" + "B" * 35, factory)[1]
    assert pair.send_test(TOKEN, [JUSTYN["id"]], factory) == (1, "")


def test_settings_and_the_bot_agree():
    assert [k for k, _ in TELEGRAM_SENDS] == list(botmod.SENDS)
    fields = {f.name: f for f in SCHEMA if f.group == "Telegram"}
    assert set(fields) == {"telegram_on", "telegram_token", "telegram_send", "telegram_app", "telegram_app_url",
                           "telegram_app_port"}
    assert fields["telegram_token"].kind == "secret"
    assert {k.strip() for k in Settings().telegram_send.split(",")} == set(botmod.SENDS)
    assert Settings().apply({"telegram_token": f"  {TOKEN} "}).telegram_token == TOKEN


def test_settings_telegram_page_shows_the_linked_phones(app):
    from ytclip.gui.settings_dialog import GROUPS, SettingsDialog

    with state.edit() as data:
        state.set_bot(data, ME)
        state.link(data, JUSTYN, time.time())
    dlg = SettingsDialog(Settings(telegram_token=TOKEN), tab="Telegram")
    page = dlg._telegram
    assert "Telegram" in GROUPS and page is not None
    assert "Tick Use the Telegram bot" in page.bot_line.text() and "@ClipTestBot" in page.bot_line.text()
    labels = [w.text() for w in page.findChildren(type(page.bot_line).__mro__[1]) if "Justyn" in w.text()]
    assert labels and "Justyn (@justyn)" in labels[0]
    page._unlink(JUSTYN["id"])
    assert state.load()["chats"] == []
    dlg.reject()
    assert not page.timer.isActive()
