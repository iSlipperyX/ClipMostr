"""The bot's guard rails (ytclip/telegram/api.py, streamer.py, state.py, bot.py, mini_app.py): Telegram's rate
limits kept and waited out, calls tried again only when that can't send anything twice, uploads sent in pieces,
only the linked people themselves heard, a flood of taps not queued, and one progress message per thing being
made, edited in place. Telegram is telegram_fake; time is a clock the test moves."""

import json

import pytest

from telegram_fake import JUSTYN, TOKEN, FakeTelegram
from test_telegram import clip_folder, make_video, world  # noqa: F401 - `world` is a fixture
from test_telegram_app import SAM, init_data, link, served, to, writers  # noqa: F401 - `served` is a fixture
from ytclip.steps import Status
from ytclip.telegram import api as tg
from ytclip.telegram import bot as botmod
from ytclip.telegram import mini_app, state, streamer


class Clock:
    def __init__(self, t=1000.0):
        self.t, self.slept = t, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.t += seconds


# --- Telegram's limits -----------------------------------------------------------------------------------------


def test_each_chat_gets_a_message_a_second_and_all_of_them_thirty():
    clock = Clock()
    th = tg.Throttle(clock=clock, sleep=clock.sleep)
    assert th.turn(1) == 0 and th.turn(1) == pytest.approx(1.0) and th.turn(1) == pytest.approx(2.0)
    assert th.turn(2) == pytest.approx(3 * tg.OVERALL)  # another chat doesn't wait for the first one's second
    assert th.turn(None) == pytest.approx(4 * tg.OVERALL)  # a call for no chat only keeps the overall pace
    clock.t += 10
    assert th.turn(1) == 0
    # what Telegram asks for pushes the next turn back: one chat's, or everyone's
    th.hold(1, 7)
    assert th.turn(1) == pytest.approx(7.0) and th.turn(2) < 1
    th.hold(None, 30)
    assert th.turn(3) == pytest.approx(30.0, abs=0.2)
    th.wait(4)
    assert clock.slept and clock.slept[-1] > 29


def test_the_api_keeps_the_pace_and_waits_out_a_429():
    fake, clock = FakeTelegram(), Clock()
    api = tg.Api(TOKEN, transport=fake.transport, sleep=clock.sleep, throttle=tg.Throttle(clock=clock, sleep=clock.sleep))
    for _ in range(3):
        api.send(5001, "hi")
    assert clock.slept == [1.0, 1.0]  # the second and third wait their second
    api.get_me()  # nothing that isn't a message is paced
    assert clock.slept == [1.0, 1.0]
    fake.errors["sendMessage"] = [(429, "Too Many Requests: retry after 4", 4)]
    assert api.send(5001, "again")["message_id"]
    assert 4.5 in clock.slept and len(fake.sent("sendMessage")) == 5  # waited, then sent once more
    # a 429 that asks for longer than anyone should wait is passed on, and the chat stays held that long
    fake.errors["sendMessage"] = [(429, "Too Many Requests: retry after 300", 300)]
    with pytest.raises(tg.ApiError) as exc:
        api.send(5001, "later")
    assert exc.value.code == 429 and exc.value.retry_after == 300
    assert api.throttle.turn(5001) == pytest.approx(300, abs=2)
    # three 429s in a row: it gives up instead of looping
    fake.errors["sendMessage"] = [(429, "Too Many Requests", 1)] * 3
    with pytest.raises(tg.ApiError):
        api.send(5002, "x")


def test_a_call_is_tried_again_only_when_that_cant_send_twice():
    fake, waited = FakeTelegram(), []
    api = tg.Api(TOKEN, transport=fake.transport, sleep=waited.append)
    mid = api.send(5001, "hi")["message_id"]
    fake.errors["editMessageText"] = [(502, "Bad Gateway", 0), (500, "Internal Server Error", 0)]
    assert api.edit(5001, mid, "changed") and waited == [1.0, 2.0]  # an edit is safe to repeat: twice, backing off
    fake.errors["sendMessage"] = [(502, "Bad Gateway", 0)]
    with pytest.raises(tg.ApiError):
        api.send(5001, "once")  # Telegram may have sent it before it failed: never a second copy
    assert len(fake.sent("sendMessage")) == 2
    # the same for a connection that drops
    calls = []

    def flaky(url, body, headers, timeout):
        calls.append(url.rsplit("/", 1)[1])
        if len(calls) == 1:
            raise tg.NetworkError(f"reset talking to {url}")
        return fake.transport(url, body, headers, timeout)

    api = tg.Api(TOKEN, transport=flaky, sleep=waited.append)
    assert api.get_me()["username"] == "ClipTestBot" and calls == ["getMe", "getMe"]
    calls.clear()
    with pytest.raises(tg.NetworkError) as net:
        api.send(5001, "hi")
    assert calls == ["sendMessage"] and TOKEN not in str(net.value)
    # an answer that isn't JSON (a proxy's error page)
    api = tg.Api(TOKEN, transport=lambda *a: (502, b"<html>Bad Gateway</html>"), sleep=waited.append)
    with pytest.raises(tg.ApiError, match="isn't JSON"):
        api.get_me()


def test_a_video_is_uploaded_in_pieces_and_says_how_far(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(bytes(range(256)) * 4000)  # about a megabyte
    heard = []
    up = tg.Upload({"chat_id": 5001, "caption": "héllo"}, {"video": video}, lambda sent, total: heard.append((sent, total)))
    assert len(up) == up.total > video.stat().st_size
    pieces = []
    while True:
        got = up.read(64 * 1024)
        if not got:
            break
        assert len(got) <= 64 * 1024
        pieces.append(got)
    body = b"".join(pieces)
    assert len(body) == up.total and video.read_bytes() in body and "héllo".encode() in body
    assert heard[-1] == (up.total, up.total) and len(heard) <= 52 and [s for s, _ in heard] == sorted(s for s, _ in heard)
    assert up._file is None and bytes(up) == body  # the file is closed; a transport that needs bytes gets the same
    # the bot's own transport streams it; a progress call that breaks never stops an upload
    assert getattr(tg._urllib, "streams", False)
    broken = tg.Upload({}, {"video": video}, lambda s, t: 1 / 0)
    assert len(bytes(broken)) == broken.total


# --- the video as Telegram takes it -------------------------------------------------------------------------------


def test_a_master_over_the_limit_goes_as_a_mobile_preview_and_stays_where_it_is(tmp_path, monkeypatch):
    master = make_video(tmp_path / "posting" / "master.mp4", size="1080x1920", bitrate="2M")
    before = (master.stat().st_size, master.stat().st_mtime)
    small = streamer.for_telegram(master, "k1", tmp_path / "previews")
    assert small.path == master and not small.copy and small.badge == ""  # Telegram takes it as it is
    monkeypatch.setattr(streamer, "LIMIT", master.stat().st_size - 1)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        import subprocess
        return subprocess.run(cmd, **kw)

    got = streamer.for_telegram(master, "k1", tmp_path / "previews", run=run)
    assert got.copy and got.path == tmp_path / "previews" / "k1.mp4" and got.path.stat().st_size < before[0]
    assert (got.width, got.height) == (720, 1280) and got.master == (1080, 1920)
    assert got.badge == "📱 Mobile Preview (1080p Master Saved on Host)"
    cmd = ran[0]
    assert cmd[cmd.index("-crf") + 1] == "24" and cmd[cmd.index("-b:a") + 1] == "128000" and "+faststart" in cmd
    assert len(ran) == 1 and (master.stat().st_size, master.stat().st_mtime) == before  # the master isn't touched
    again = streamer.for_telegram(master, "k1", tmp_path / "previews", run=lambda *a, **k: pytest.fail("made twice"))
    assert again.path == got.path and again.badge == got.badge
    assert streamer.master_name(2160, 3840) == "2160p" and streamer.master_name(0, 0) == "Full-Quality"
    assert not list((tmp_path / "previews").glob("*.part.mp4"))
    # an encode that fails leaves nothing behind and says so in words
    class Failed:
        returncode, stderr = 1, b"line one\nInvalid data found when processing input\n"

    with pytest.raises(RuntimeError, match="Couldn't make a copy small enough for Telegram: Invalid data"):
        streamer.for_telegram(master, "k2", tmp_path / "previews", run=lambda *a, **k: Failed())
    assert not (tmp_path / "previews" / "k2.mp4").exists()


def test_the_progress_line_reads_as_asked():
    assert streamer.progress_html("Why Roman Concrete Lasted", 0.78, "Rendering Video") == (
        "⚙️ Working on: “Why Roman Concrete Lasted” [████████░░] 78% (Rendering Video)")
    assert streamer.bar(None) == "[░░░░░░░░░░]" and streamer.bar(0) == "[░░░░░░░░░░] 0%"
    assert streamer.bar(1.7) == "[██████████] 100%" and streamer.bar(0.999) == "[█████████░] 99%"
    assert streamer.progress_html("<b>x</b> & y", None) == "⚙️ Working on: “&lt;b&gt;x&lt;/b&gt; &amp; y” [░░░░░░░░░░]"
    assert len(streamer.progress_html("t" * 500, 0.5, "s" * 500)) < 220
    for said, shown in [("Putting the video together 45%", "Putting the video together"), ("Drawing the pictures: 40 %", "Drawing the pictures"),
                        ("Rendering (12.5%)", "Rendering"), ("Transcribing…", "Transcribing"), ("", ""), ("Top 100 moments", "Top 100 moments")]:
        assert streamer.stage_text(said) == shown


def live(fake, clock, kept=None):
    api = tg.Api(TOKEN, transport=fake.transport, sleep=lambda s: None)
    return streamer.Live(lambda: api, now=clock, keep=(kept.append if kept is not None else None))


def edits(fake):
    return [p["text"] for m, p in fake.calls if m == "editMessageText"]


def test_one_progress_message_is_edited_in_place_every_three_seconds():
    fake, clock, kept = FakeTelegram(), Clock(), []
    lv = live(fake, clock, kept)
    mid = lv.start("job:1", 5001, "Why Roman Concrete Lasted")
    assert mid and lv.start("job:1", 5001, "Why Roman Concrete Lasted") == mid  # started once
    assert len(fake.sent("sendMessage")) == 1 and fake.last()["disable_notification"] is True
    assert kept[-1]["job:1|5001"]["mid"] == mid
    # twenty steps in a second: nothing is sent, nothing is edited
    for i in range(20):
        clock.t += 0.05
        assert lv.update("job:1", i / 20, "Rendering Video") == 0
    clock.t += 3
    assert lv.update("job:1", 0.78, "Rendering Video") == 1
    assert edits(fake) == ["⚙️ Working on: “Why Roman Concrete Lasted” [████████░░] 78% (Rendering Video)"]
    clock.t += 5
    assert lv.update("job:1", 0.78, "Rendering Video") == 0  # nothing new to say: no edit
    clock.t += 1
    assert lv.update("job:1", 0.9, "Rendering Video") == 1 and len(fake.sent("sendMessage")) == 1
    # Telegram asks for room: this message waits that long, then carries on
    fake.errors["editMessageText"] = [(429, "Too Many Requests: retry after 20", 20)] * 3
    clock.t += 4
    assert lv.update("job:1", 0.95, "Checking it") == 0
    fake.errors["editMessageText"] = []
    clock.t += 10
    assert lv.update("job:1", 0.96, "Checking it") == 0
    clock.t += 11
    assert lv.update("job:1", 0.97, "Checking it") == 1
    # each chat has its own; ending takes them away (the video follows) or leaves a last word
    lv.start("job:1", 5002, "Why Roman Concrete Lasted")
    assert sorted(lv.chats("job:1")) == [5001, 5002]
    assert lv.finish("job:1", chat=5002) == [5002] and fake.sent("deleteMessage")[-1]["chat_id"] == 5002
    assert lv.finish("job:1", "✖ It was cancelled.") == [5001] and edits(fake)[-1] == "✖ It was cancelled."
    assert lv.keys() == set() and kept[-1] == {} and lv.update("job:1", 1.0) == 0


def test_a_progress_message_survives_a_restart_and_forgets_a_deleted_one():
    fake, clock, kept = FakeTelegram(), Clock(), []
    first = live(fake, clock, kept)
    mid = first.start("sports:o1", 5001, "Lakers at Celtics", icon="🏀")
    after = live(fake, clock)
    after.restore(json.loads(json.dumps(kept[-1])))  # as telegram.json hands it back
    clock.t += 4
    assert after.update("sports:o1", 0.5, "Finding the plays") == 1 and len(fake.sent("sendMessage")) == 1
    assert edits(fake)[-1].startswith("🏀 Working on: “Lakers at Celtics” [█████░░░░░] 50%")
    assert fake.last("editMessageText")["message_id"] == mid
    # deleted in the chat: no more edits, and no new message in its place
    del fake.messages[(5001, mid)]
    clock.t += 4
    assert after.update("sports:o1", 0.6, "Finding the plays") == 0 and after.keys() == set()
    # one nobody finished is forgotten in the end
    old = live(fake, clock)
    old.restore({"job:9|5001": {"key": "job:9", "chat": 5001, "mid": 7, "title": "x", "started": clock.t - 7 * 3600}})
    assert old.keys() == set()
    stale = live(fake, clock)
    stale.start("job:8", 5001, "x")
    clock.t += 7 * 3600
    stale.prune()
    assert stale.keys() == set()
    # with the bot off there's nothing to send to, and nothing breaks
    off = streamer.Live(lambda: None, now=clock)
    assert off.start("job:1", 5001, "x") == 0 and off.update("job:1", 0.5) == 0 and off.finish("job:1") == []


# --- who the bot listens to ---------------------------------------------------------------------------------------


def test_only_a_linked_person_in_their_own_chat_is_heard():
    data = state.empty()
    state.link(data, JUSTYN, 1.0)
    me = {"id": JUSTYN["id"], "is_bot": False, "first_name": "Justyn"}
    assert state.authorized(data, JUSTYN, me) and state.authorized(data, {"id": JUSTYN["id"]}, None)
    for chat, sender in [
        (JUSTYN, {"id": 999, "first_name": "Someone else"}),  # another person, in the linked chat's place
        (JUSTYN, {"id": JUSTYN["id"], "is_bot": True}),  # a bot wearing the id
        (JUSTYN, {"id": "not a number"}),
        ({"id": JUSTYN["id"], "type": "group"}, me),  # the id as a group, a supergroup, a channel
        ({"id": JUSTYN["id"], "type": "supergroup"}, me),
        ({"id": JUSTYN["id"], "type": "channel"}, None),
        ({"id": 4242, "type": "private"}, {"id": 4242}),  # a private chat nobody linked
        ({"id": 0, "type": "private"}, None), ({"id": "x"}, None), ({}, me), (None, me), ("5001", me),
    ]:
        assert not state.authorized(data, chat, sender), (chat, sender)


def test_someone_else_in_a_linked_chats_place_is_ignored_and_logged_once(world):  # noqa: F811
    w = world
    clip_folder(w.root, clips=1)
    (it,) = w.take()
    it.review = "waiting"
    w.fake.clear()
    spoofed = w.fake.message("/pause")
    spoofed["message"]["from"] = {"id": 999, "first_name": "Mallory"}
    w.bot.handle(spoofed)
    assert not w.fake.sent("sendMessage") and w.runner.db.meta("paused") != "1"
    press = w.fake.press(f"ok:{it.id}", 1)
    press["callback_query"]["from"] = {"id": 999, "first_name": "Mallory"}
    w.bot.handle(press)
    assert w.store.get(it.id).review == "waiting"
    answer = w.fake.last("answerCallbackQuery")
    assert answer["text"] == "This chat isn't linked to ClipMonster." and answer["show_alert"] is True
    bot_press = w.fake.press(f"del:{it.id}", 1)
    bot_press["callback_query"]["from"] = {"id": JUSTYN["id"], "is_bot": True}
    w.bot.handle(bot_press)
    group = w.fake.message("/status", {"id": JUSTYN["id"], "type": "group", "title": "A group"})
    w.bot.handle(group)
    assert not w.fake.sent("sendMessage") and w.store.get(it.id) is not None
    noted = [line for line in w.logs if "not a linked person" in line]
    assert len(noted) == 2 and "Telegram user 999" in noted[0] and "(a bot)" in noted[1]  # once each, not per try
    # an update that isn't shaped like one is dropped, not crashed on
    for junk in ({}, {"update_id": 1, "message": None}, {"update_id": 2, "message": {"chat": "x"}},
                 {"update_id": 3, "callback_query": {"id": "c", "message": {"chat": {"id": "nope"}}}},
                 {"update_id": 4, "callback_query": {"id": "c", "data": "ok:" + "9" * 500, "message": {"chat": dict(JUSTYN)},
                                                     "from": dict(JUSTYN)}}):
        w.bot.handle(junk)
    # and the person themselves is still heard
    assert "Paused" in w.say("/pause")["text"] or w.runner.db.meta("paused") == "1"


def test_a_flood_from_one_chat_isnt_queued_up(world):  # noqa: F811
    w = world
    link(w)
    t = [50.0]
    w.bot.clock = lambda: t[0]
    for _ in range(botmod.FLOOD_MAX):
        w.say("/status")
    assert len(to(w, JUSTYN)) == botmod.FLOOD_MAX
    w.fake.clear()
    for _ in range(25):
        w.say("/status")
    said = to(w, JUSTYN)
    assert len(said) == 1 and said[0]["text"].startswith("⏳ That's a lot at once.")  # said once, not 25 times
    w.press("list:0", 1)
    assert w.fake.last("answerCallbackQuery")["text"] == "One moment: that's a lot of taps at once."
    w.say("/status", SAM)
    assert len(to(w, SAM)) == 1 and "⏳" not in to(w, SAM)[0]["text"]  # the other person isn't slowed by it
    t[0] += botmod.FLOOD_SPAN + 1
    w.fake.clear()
    w.say("/status")
    assert len(to(w, JUSTYN)) == 1 and "⏳" not in to(w, JUSTYN)[0]["text"]


# --- the bot's own progress messages ------------------------------------------------------------------------------


def progress(w, job_id, frac, message):
    w.runner.db.update(job_id, w.clock[0], status="working", stage=message,
                       progress=json.dumps(Status(message=message, overall=frac).to_json()))


def test_what_you_asked_for_shows_one_message_that_follows_the_work(world):  # noqa: F811
    w = world
    link(w)
    writers(w)
    w.say("/make why Roman concrete lasted for Psychology", SAM)
    job = w.runner.db.jobs(limit=5)[0]
    w.fake.clear()
    w.bot.progress_tick()
    assert not w.fake.sent()  # still waiting its turn: nothing to follow yet
    progress(w, job.id, 0.1, "Writing the script")
    w.bot.progress_tick()
    first = to(w, SAM)
    assert len(first) == 1 and first[0]["text"].startswith("⚙️ Working on: “why Roman concrete lasted” [")
    assert not to(w, JUSTYN)  # it's Sam's request: the message is in Sam's chat
    assert f"job:{job.id}|{SAM['id']}" in state.load(w.state_file)["live"]
    for frac, said in [(0.3, "Drawing the pictures 30%"), (0.5, "Recording the voice"), (0.78, "Rendering Video 78%")]:
        w.clock[0] += 3.1
        progress(w, job.id, frac, said)
        w.bot.progress_tick()
        w.bot.progress_tick()  # a second look in the same moment changes nothing
    assert len(w.fake.sent("sendMessage")) == 1  # one message, whatever the number of steps
    assert edits(w.fake) == ["⚙️ Working on: “why Roman concrete lasted” [███░░░░░░░] 30% (Drawing the pictures)",
                             "⚙️ Working on: “why Roman concrete lasted” [█████░░░░░] 50% (Recording the voice)",
                             "⚙️ Working on: “why Roman concrete lasted” [████████░░] 78% (Rendering Video)"]
    # a restart in the middle carries the same message on
    again = botmod.Bot(w.runner, api_factory=w.bot.api_factory, state_file=w.state_file,
                       status_file=w.tmp / "telegram-status.json", previews=w.tmp / "previews",
                       now=lambda: w.clock[0], posting=(w.store, w.accounts, None))
    assert again.configure()
    w.clock[0] += 3.1
    progress(w, job.id, 0.9, "Checking it")
    again.progress_tick()
    assert len(w.fake.sent("sendMessage")) == 1 and "90% (Checking it)" in edits(w.fake)[-1]
    # it failed: the progress message is taken away, and what went wrong is said
    w.runner.db.update(job.id, w.clock[0], status="failed", error="The voice engine isn't installed.")
    again.check(w.clock[0])
    assert again.live.keys() == set() and state.load(w.state_file)["live"] == {}
    assert any("The voice engine isn't installed." in m["text"] for m in to(w, SAM))


# --- the Mini App's door ------------------------------------------------------------------------------------------


def test_the_app_refuses_a_bot_and_a_flood(served):  # noqa: F811
    w = served
    now = w.clock[0]
    with pytest.raises(mini_app.AuthError):
        mini_app.verify_init_data(init_data({**SAM, "is_bot": True}, now - 5), TOKEN, now)
    tok = w.session(SAM)["token"]
    most = mini_app.RATE["post"][0]
    codes = [w.call("POST", "/api/pause", {"paused": True}, token=tok)[0] for _ in range(most + 3)]
    assert codes[:most] == [200] * most and codes[most:] == [429] * 3
    status, err, _ = w.call("POST", "/api/make", {"idea": "the moon", "category": "p1"}, token=tok)
    assert status == 429 and "That's a lot at once" in err["error"]
    assert w.call("GET", "/api/status", token=tok)[0] == 200  # looking is never limited
    assert w.call("POST", "/api/pause", {"paused": False}, token=w.session(JUSTYN)["token"])[0] == 200  # nor the other person
    # sign-ins as a whole are limited too, so nobody can hammer the door
    w.app._hits.clear()
    tries = [w.call("POST", "/api/session", {"init_data": "junk"})[0] for _ in range(mini_app.RATE["signin"][0] + 2)]
    assert tries[0] == 401 and tries[-1] == 429
    # someone removed from the bot is out at their next request, session or not
    w.app._hits.clear()
    w.bot.remove(JUSTYN["id"], SAM["id"])
    assert w.call("GET", "/api/status", token=tok)[0] == 401
