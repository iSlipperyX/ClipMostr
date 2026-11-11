"""Downloads and the sports editor (ClipMonster 3.13).

A sports video asked for from Telegram is a download: posting keeps it in the library and queues it for no
account (posting/intake.py). It can be cut again: sportsdesk/editor.py says what there is to change and checks
what the phone sends back, the desk queues the edit, and the worker makes the new version beside the first
(a stand-in reel maker and engine here). Then the chat and the Mini App: the file itself, the editor's
document, an edit from start to finish, keeping one version, and queueing a download to post after all."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ytclip.posting import intake
from ytclip.sportsdesk import editor as E
from ytclip.sportsdesk import montage
from ytclip.sportsdesk.desk import DeskError
from ytclip.telegram import mini_app_page, state

from telegram_fake import JUSTYN
from test_sports_desk import FakeEngine, desk, seed, worker  # noqa: F401 - fixtures
from test_sports_orders import run
from test_sports_reel import GAME, crowd_for, hls
from test_telegram import make_video, world  # noqa: F401 - fixtures
from test_telegram_app import SAM, link, served, to  # noqa: F401 - fixtures
from test_telegram_sports import buttons, sports  # noqa: F401 - fixtures


def plan_json(n=5, **made) -> dict:
    h = hls(n)
    reel = montage.plan(GAME, h, crowd=crowd_for(h, 600), duration=600)
    reel.story = montage.Story(opening="Boston. A one-point game.", hook="A night of dunks", caption="Top plays",
                               hashtags=["nba", "lakers"], badges={"h1": "LEBRON JAMES · POSTER DUNK"})
    return reel.to_json() | ({"made": made} if made else {})


def rows(plan: dict) -> list[dict]:
    return [{"id": s["hid"], "start": s["start"], "end": s["end"]} for s in plan["segments"] if s["kind"] == "play"]


def game(n=6) -> dict:
    return GAME | {"highlights": hls(n), "duration": 600.0}


# ---------------------------------------------------------------- the editor's rules: a reel


def test_a_reel_is_trimmed_reordered_and_loses_a_play():
    plan = plan_json()
    was = montage.Reel.from_json(plan)
    plays = rows(plan)
    first = plays[0]
    plays = [plays[1], dict(first, start=first["start"] + 2.0, end=first["end"] - 1.0)] + plays[2:-2] + plays[-1:]
    ed = E.reel_edit(plan, {"plays": plays}, game(), 600.0)
    assert ed.changed == ["took out 1 play", "put the plays in a new order", "trimmed 1 play"]
    assert [s.hid for s in ed.reel.plays] == [p["id"] for p in plays]
    moved = ed.reel.plays[1]
    assert (moved.start, moved.end) == (round(first["start"] + 2.0, 2), round(first["end"] - 1.0, 2))
    assert moved.start + 0.2 <= moved.moment <= moved.end - 0.2  # the crowd is still lifted inside the cut
    assert ed.reel.order == "custom" and ed.reel.duration < was.duration
    assert ed.reel.segments[0].kind == "teaser" and ed.reel.segments[0].hid == was.segments[0].hid  # the opening stays
    assert (ed.voice, ed.framing, ed.look) == ("en_storyteller", "follow", {"music": "normal", "game": 0.0,
                                                                              "bug": True})
    assert E.said(ed.changed) == "Took out 1 play; put the plays in a new order; trimmed 1 play"


def test_a_play_is_added_from_the_games_other_highlights_and_words_are_changed():
    plan = plan_json(5)
    plays = rows(plan) + [{"id": "h6", "start": 480.0, "end": 492.0, "label": "bonus", "badge": "late steal"}]
    plays[0]["label"] = "the dagger"
    ed = E.reel_edit(plan, {"plays": plays, "opening": {"hook": "Lakers steal it late", "words": "", "voice": "off"},
                            "words": {"caption": "What a finish", "hashtags": "#NBA lakers, #Lakers #clutch",
                                      "final": "Lakers 108-106 Celtics"},
                            "look": {"music": "off", "game": "loud", "framing": "fit", "style": "whip", "bug": False}},
                     game(6), 600.0)
    new = ed.reel.plays[-1]
    assert (new.hid, new.start, new.end, new.label, new.title) == ("h6", 480.0, 492.0, "BONUS", "Play 6")
    assert ed.reel.story.badges["h6"] == "LATE STEAL" and ed.reel.plays[0].label == "THE DAGGER"
    assert ed.reel.story.hook == "Lakers steal it late" and ed.reel.story.opening == "" and ed.voice == "off"
    assert ed.reel.story.hashtags == ["NBA", "lakers", "clutch"]  # once each, without the #
    assert ed.reel.final == "Lakers 108-106 Celtics" and ed.reel.style == "whip" and ed.framing == "fit"
    assert ed.look == {"music": "off", "game": 4.0, "bug": False}
    assert ed.changed == ["added 1 play", "put the plays in a new order"][:1] + [
        "changed the words on the plays", "changed the opening's headline", "turned the narrator off",
        "changed the caption", "changed the hashtags", "changed the final score line",
        "framed it as the whole picture", "set the transitions to whip pan", "turned the music off",
        "set the game's sound to loud", "turned the score bug off"]


def test_the_teaser_can_go_and_follows_the_last_play_when_its_own_is_dropped():
    plan = plan_json()
    off = E.reel_edit(plan, {"opening": {"on": False}}, game())
    assert off.changed == ["took the opening teaser off"] and off.reel.segments[0].kind == "play"
    best = plan["segments"][0]["hid"]  # the teaser's play: taken out, the opening is cut from the new last play
    kept = [p for p in rows(plan) if p["id"] != best]
    ed = E.reel_edit(plan, {"plays": kept}, game())
    teaser = ed.reel.segments[0]
    assert teaser.kind == "teaser" and teaser.hid == kept[-1]["id"] and 1.5 <= teaser.length <= montage.TEASER + 0.01
    assert teaser.end == round(ed.reel.plays[-1].moment - montage.TEASER_STOP, 3)
    one = E.reel_edit(plan, {"plays": kept[:1]}, game())  # a single play has nothing to tease
    assert [s.kind for s in one.reel.segments] == ["play"]


def test_what_the_footage_cant_give_is_refused_in_words():
    plan = plan_json()
    plays = rows(plan)
    with pytest.raises(E.EditError, match="Nothing was changed"):
        E.reel_edit(plan, {"plays": plays, "look": {"music": "normal"}}, game())
    with pytest.raises(E.EditError, match="at least one play"):
        E.reel_edit(plan, {"plays": []}, game())
    with pytest.raises(E.EditError, match=r"Play 1 would be 1\.0 s: the least is 2 s"):
        E.reel_edit(plan, {"plays": [dict(plays[0], end=plays[0]["start"] + 1.0)] + plays[1:]}, game())
    with pytest.raises(E.EditError, match="the most is 60 s"):
        E.reel_edit(plan, {"plays": [dict(plays[0], end=plays[0]["start"] + 61.0)] + plays[1:]}, game())
    with pytest.raises(E.EditError, match="in the reel twice"):
        E.reel_edit(plan, {"plays": plays + plays[:1]}, game())
    with pytest.raises(E.EditError, match="isn't in this game any more"):
        E.reel_edit(plan, {"plays": plays + [{"id": "nope", "start": 1, "end": 9}]}, game())
    with pytest.raises(E.EditError, match="180 s at most"):
        E.reel_edit(plan, {"plays": [dict(p, start=p["start"] - 10, end=p["start"] + 45) for p in plays]}, game())
    with pytest.raises(E.EditError, match="isn't one ClipMonster has"):
        E.reel_edit(plan, {"opening": {"voice": "darth_vader"}}, game())
    with pytest.raises(E.EditError, match="couldn't be read"):
        E.reel_edit(plan, "trim it", game())  # type: ignore[arg-type]
    with pytest.raises(montage.ReelError, match="can't be read"):
        E.reel_edit({"segments": [{"kind": "play"}]}, {}, game())
    # a cut past the end of the footage stops at the end; times that aren't numbers leave the play as it was
    long = E.reel_edit(plan, {"plays": [dict(plays[0], end=9999.0, start=560.0)] + plays[1:]}, game(), 600.0)
    assert long.reel.plays[0].end == 600.0
    same = E.reel_edit(plan, {"plays": [dict(plays[0], start="soon", end=None)] + plays[1:],
                              "words": {"caption": "x"}}, game())
    assert (same.reel.plays[0].start, same.reel.plays[0].end) == (plays[0]["start"], plays[0]["end"])


def test_an_edit_of_an_edit_starts_from_how_it_was_made():
    plan = plan_json(voice="off", framing="fit", music="off", game=4.0, bug=False)
    ed = E.reel_edit(plan, {"look": {"music": "low"}}, game())
    assert ed.changed == ["set the music to quiet"]
    assert (ed.voice, ed.framing, ed.look) == ("off", "fit", {"music": "low", "game": 4.0, "bug": False})


# ---------------------------------------------------------------- the editor's rules: a single clip


ROW = {"file": "01 - dunk.mp4", "start": 100.0, "end": 124.0, "length": 24.0, "title": "Poster dunk", "score": 88,
       "hook": "He jumped over him", "hashtags": ["nba"]}
LOOK = {"captions": True, "aspect": "vertical_blur", "crop_x": 0.5, "headline": ""}


def test_a_clip_is_cut_again_with_its_words_and_look():
    ed = E.clip_edit(ROW, LOOK, {"cut": {"start": 97.5, "end": 130.0},
                                 "words": {"headline": "Posterized", "caption": "Over him", "hashtags": "nba dunk"},
                                 "look": {"captions": False, "aspect": "vertical_crop", "crop_x": 0.7}}, 800.0)
    assert (ed.start, ed.end, ed.headline, ed.caption, ed.hashtags) == (97.5, 130.0, "Posterized", "Over him",
                                                                         ["nba", "dunk"])
    assert ed.look == {"captions": False, "aspect": "vertical_crop", "crop_x": 0.7, "headline": "Posterized"}
    assert ed.changed == ["cut it from 24 s to 32.5 s", "added a headline", "changed the caption",
                          "changed the hashtags", "turned the captions off", "made it fill the screen"]
    moved = E.clip_edit(ROW, LOOK, {"cut": {"start": 102.0, "end": 126.0}}, 800.0)
    assert moved.changed == ["moved where it's cut from"]
    with pytest.raises(E.EditError, match="Nothing was changed"):
        E.clip_edit(ROW, LOOK, {"cut": {"start": 100.0, "end": 124.0}}, 800.0)
    with pytest.raises(E.EditError, match=r"The clip would be 2\.0 s: the least is 3 s"):
        E.clip_edit(ROW, LOOK, {"cut": {"start": 100.0, "end": 102.0}}, 800.0)
    with pytest.raises(E.EditError, match="Pick the picture's shape"):
        E.clip_edit(ROW, LOOK, {"look": {"aspect": "sideways"}}, 800.0)
    # the next edit starts from how this version was made
    again = E.clip_edit(ROW | {"edit": ed.look, "start": ed.start, "end": ed.end}, LOOK,
                        {"look": {"crop_x": 0.2}}, 800.0)
    assert again.changed == ["moved the crop"] and again.headline == "Posterized"


# ---------------------------------------------------------------- the folder: what's there, and a new version


def reel_folder(root: Path, keep: bool = True, name: str = "2026-11-11 10.00 Lakers at Celtics reel") -> Path:
    folder = root / "Sports" / "Reels" / name
    folder.mkdir(parents=True)
    make_video(folder / "01 - Lakers at Celtics.mp4")
    (folder / "reel.json").write_text(json.dumps(plan_json(voice="en_storyteller", framing="follow")))
    auto = {"permission": "Asked for by Sam from Telegram", "origins": ["sports", "telegram"], "how": "manual"}
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "v0", "title": "Lakers at Celtics | Extended Highlights", "channel": "NBA", "duration": 900},
        "automation": auto | ({"keep": "download"} if keep else {"category": "Hoops", "category_id": "h1"}),
        "sports": {"matchup": "Lakers at Celtics", "sport": "basketball"},
        "clips": [{"rank": 1, "file": "01 - Lakers at Celtics.mp4", "start": 0.0, "end": 71.0, "length": 71.0,
                   "score": 100, "title": "Lakers at Celtics: top 5 plays", "hook": "A Night Of Dunks",
                   "caption": "Top plays #nba", "hashtags": ["nba"], "reel": True, "qc": {"ok": True}}]}))
    return folder


def test_the_editor_is_told_whats_in_a_reel(tmp_path):
    folder = reel_folder(tmp_path)
    name = "01 - Lakers at Celtics.mp4"
    assert E.kind_of(folder, name) == "reel" and E.kind_of(folder, "other.mp4") == ""
    doc = E.describe(folder, name, game() | {"id": "v0"})
    assert (doc["kind"], doc["title"], doc["length"], doc["why"]) == ("reel", "Lakers at Celtics: top 5 plays", 71.0, "")
    assert doc["game"] == {"id": "v0", "matchup": "Lakers at Celtics", "duration": 600.0, "source": False}
    assert len(doc["plays"]) == 5 and doc["plays"][-1]["id"] == "h1"  # a countdown: the best play last
    assert doc["plays"][-1]["badge"] == "LEBRON JAMES · POSTER DUNK" and doc["plays"][0]["label"] == "PLAY 5"
    assert doc["opening"] == {"on": True, "hook": "A night of dunks", "words": "Boston. A one-point game.",
                              "voice": "en_storyteller"}
    assert [m["id"] for m in doc["more"]] == ["h6"]  # the game's play that isn't in the reel can be added
    assert doc["words"] == {"caption": "Top plays", "hashtags": "#nba #lakers", "final": ""}
    assert doc["look"] == {"framing": "follow", "style": "mix", "music": "normal", "game": "normal", "bug": True}
    assert doc["choices"]["voices"][0] == {"id": "off", "label": "No narrator"}
    assert doc["limits"]["play_max"] == 60.0 and doc["limits"]["longest"] == 180.0
    gone = E.describe(folder, name, None)  # the game left the Sports page: it's said, not guessed around
    assert "isn't on the PC's Sports page any more" in gone["why"] and gone["more"] == []
    (folder / "reel.json").unlink()
    with pytest.raises(E.EditError, match="with its plan still beside it"):
        E.describe(folder, name, game())


def test_a_version_is_listed_beside_the_first_and_keeps_how_its_handed_over(tmp_path):
    folder = reel_folder(tmp_path)
    src = "01 - Lakers at Celtics.mp4"
    new = E.version_name(folder, src, 2)
    assert new == "01 - Lakers at Celtics (v2).mp4"
    make_video(folder / new)
    assert E.version_name(folder, new, 2) == "01 - Lakers at Celtics (v3).mp4"  # an edit of v2 is v3, not "(v2) (v2)"
    assert E.add_version(folder, src, new, {"length": 40.0, "end": 40.0, "caption": "Shorter"}, "trimmed 2 plays")
    assert E.add_version(folder, src, new, {}, "") is True  # asked twice: listed once
    index = E.index_of(folder)
    row = E.row_of(index, new)
    assert len(index["clips"]) == 2 and (row["rank"], row["edit_of"], row["edit_note"]) == (2, src, "trimmed 2 plays")
    assert row["title"] == "Lakers at Celtics: top 5 plays (v2)" and row["length"] == 40.0 and row["reel"] is True
    assert E.add_version(folder, "nope.mp4", "x (v2).mp4", {}) is False
    assert E.plan_of(folder, new)["segments"]  # until it has a plan of its own, the folder's
    E.plan_file(folder, new).write_text(json.dumps(plan_json(3)))
    assert len(E.plan_of(folder, new)["segments"]) < len(E.plan_of(folder, src)["segments"])


# ---------------------------------------------------------------- posting: a download is nobody's to post


def test_a_download_is_kept_out_of_every_queue_until_someone_queues_it(world):  # noqa: F811
    w = world
    w.s.categories = w.s.categories + [{"id": "h1", "name": "Hoops", "sport": "basketball",
                                        "account_id": w.account.id}]
    folder = reel_folder(w.root)
    items = w.take()
    assert len(items) == 1
    it = items[0]
    assert (it.keep, it.status, it.account, it.queue_pos, it.review) == ("download", "new", "", None, "")
    assert intake.downloads(w.store) == [it] and intake.unassigned(w.store) == []  # not "waiting for an account"
    assert w.take() == [] and it.status == "new"  # and the next look doesn't queue it either
    # the same reel asked for to post goes to its category's account, as before
    other = reel_folder(w.root, keep=False, name="2026-11-11 11.00 Heat at Knicks reel")
    posted = w.take()[0]
    assert (posted.keep, posted.status, posted.account) == ("", "queued", w.account.id) and other.is_dir()
    # queued after all: with no category of its own there's nowhere to send it, unless one is picked
    w.accounts.add("").username = "second"
    assert intake.to_queue(w.store, w.accounts, it, w.s) is None and it.keep == "download"
    from ytclip import categories

    owner = intake.to_queue(w.store, w.accounts, it, w.s, categories.find(w.s, "h1"))
    assert owner.id == w.account.id and (it.keep, it.status, it.category_name) == ("", "queued", "Hoops")
    assert folder.is_dir() and intake.downloads(w.store) == []


# ---------------------------------------------------------------- the desk and its worker


@pytest.fixture
def edits(monkeypatch):
    """reel.make for an edit, without rendering: the new version's file and its row, as the real one leaves them."""
    from ytclip.sportsdesk import reel as R

    made = []

    def fake_make(p, game, highlights, s, out_root, **k):  # noqa: ARG001
        made.append({"game": game["id"], **k})
        folder, src, new = k["version"]
        make_video(Path(folder) / new, seconds=1.0)
        reel = k["planned"]
        E.plan_file(folder, new).write_text(json.dumps(reel.to_json() | {"made": {
            "framing": k["framing_mode"], "voice": k["voice"], **k["look"]}}))
        assert E.add_version(folder, src, new, {"length": round(reel.duration, 2), "reel": True}, "edited")
        return R.ReelResult(Path(folder), Path(folder) / new, reel, {"length": round(reel.duration, 2)}, {}, [])

    monkeypatch.setattr(R, "make", fake_make)
    return made


def desk_reel(desk, worker) -> tuple[Path, str]:  # noqa: F811
    """A game on the desk with a made reel in its folder."""
    seed(desk, 1, highlights=True)
    folder = reel_folder(Path(worker.s.auto_output_dir))
    desk.change("v0", lambda g: g.update(highlights=hls(6), duration=600.0, reels=[
        {"at": 1.0, "folder": str(folder), "file": str(folder / "01 - Lakers at Celtics.mp4")}]))
    return folder, "01 - Lakers at Celtics.mp4"


def test_the_desk_checks_an_edit_at_once_and_the_worker_makes_it(desk, worker, edits):  # noqa: F811
    folder, name = desk_reel(desk, worker)
    doc = desk.describe_edit(folder, name)
    assert doc["game"]["id"] == "v0" and doc["editing"] is False and doc["game"]["source"] is False
    plays = [{"id": p["id"], "start": p["start"], "end": p["end"]} for p in doc["plays"]]
    with pytest.raises(DeskError, match="Nothing was changed"):
        desk.edit(folder, name, {"plays": plays})
    with pytest.raises(DeskError, match="with its plan still beside it"):
        desk.edit(folder, "02 - gone.mp4", {"plays": plays[:-1]})
    r = desk.edit(folder, name, {"plays": plays[1:], "look": {"music": "off"}, "opening": {"voice": "off"}},
                  by="Sam", by_id=5002)
    assert r["file"] == "01 - Lakers at Celtics (v2).mp4" and r["kind"] == "reel" and r["game"] == "v0"
    assert r["message"] == "Took out 1 play; turned the narrator off; turned the music off."
    assert desk.edit_state(r["id"])["status"] == "queued" and desk.describe_edit(folder, name)["editing"] is True
    assert desk.game("v0")["stage"] == "Waiting to make the edited version"
    with pytest.raises(DeskError, match="being made already"):  # one at a time: they'd both be "v2"
        desk.edit(folder, name, {"plays": plays[2:]})
    assert run(worker) == 1
    st = desk.edit_state(r["id"])
    assert (st["status"], st["file"], st["folder"]) == ("done", r["file"], str(folder))
    assert st["said"].startswith("Edited “Lakers at Celtics: top 5 plays” (Took out 1 play; turned the narrator off; "
                                 "turned the music off): ")
    k = edits[-1]
    assert [s.hid for s in k["planned"].plays] == [p["id"] for p in plays[1:]]
    assert (k["voice"], k["framing_mode"], k["keep"]) == ("off", "follow", True)
    assert k["look"] == {"music": "off", "game": 0.0, "bug": True}
    assert k["version"] == (folder, name, r["file"]) and (folder / r["file"]).is_file()
    g = desk.game("v0")
    assert g["status"] == "ready" and g["edits"][-1]["of"] == name and g["edits"][-1]["by"] == "Sam"
    assert any("Edited" in t for _, t in worker.news)
    # the next edit can be of either version; v2's starts from v2's own plan
    again = desk.describe_edit(folder, r["file"])
    assert len(again["plays"]) == 4 and again["opening"]["voice"] == "off" and again["look"]["music"] == "off"
    r3 = desk.edit(folder, r["file"], {"look": {"music": "loud"}}, version=3)
    assert r3["file"] == "01 - Lakers at Celtics (v3).mp4" and r3["message"] == "Set the music to loud."
    assert desk.edit_state("e-nope") is None


def test_an_edit_whose_video_went_meanwhile_fails_in_words(desk, worker, edits):  # noqa: F811
    folder, name = desk_reel(desk, worker)
    r = desk.edit(folder, name, {"look": {"bug": False}})
    (folder / name).unlink()
    run(worker)
    st = desk.edit_state(r["id"])
    assert st["status"] == "failed" and "isn't on the PC any more" in st["error"] and not edits
    desk.remove(["v0"])
    with pytest.raises(DeskError, match="isn't on the PC's Sports page any more"):
        desk.edit(folder, name, {"look": {"bug": False}})


class ClipEngine(FakeEngine):
    """FakeEngine, exporting a real little video with the row an export writes."""

    def open(self, p):
        from types import SimpleNamespace

        super().open(p)
        p.media = SimpleNamespace(duration=800.0)

    def export(self, p):
        folder = Path(self.s.output_dir) / "Lakers at Celtics [v0]"
        folder.mkdir(parents=True, exist_ok=True)
        c = p.clips[0]
        make_video(folder / "01 - clip.mp4", seconds=1.0)
        (folder / "clips.json").write_text(json.dumps({"clips": [{
            "rank": 1, "file": "01 - clip.mp4", "start": c.start, "end": c.end, "qc": {"ok": self.s.aspect != "bad"}}]}))
        self.exported = p
        return folder


def test_a_single_clip_is_exported_again_with_the_editors_cut(desk, worker):  # noqa: F811
    seed(desk, 1, highlights=True)
    worker.engine_factory = ClipEngine
    folder = Path(worker.s.auto_output_dir) / "Sports" / "Highlights" / "2026-11-11 Lakers at Celtics"
    folder.mkdir(parents=True)
    make_video(folder / "01 - dunk.mp4")
    (folder / "clips.json").write_text(json.dumps({
        "video": {"id": "v0", "duration": 900}, "automation": {"keep": "download", "category_id": "h1"},
        "sports": {"matchup": "Lakers at Celtics"}, "clips": [ROW | {"rank": 1, "qc": {"ok": True}}]}))
    desk.change("v0", lambda g: g.setdefault("made", []).append({"folder": str(folder), "clips": 1}))
    doc = desk.describe_edit(folder, "01 - dunk.mp4")
    assert doc["kind"] == "clip" and doc["cut"] == {"start": 100.0, "end": 124.0}
    assert doc["look"]["aspect"] == "vertical_blur" and doc["words"]["caption"] == "He jumped over him"
    r = desk.edit(folder, "01 - dunk.mp4", {"cut": {"start": 96.0, "end": 121.0},
                                            "words": {"headline": "Posterized", "caption": "Over him"},
                                            "look": {"aspect": "vertical_crop", "crop_x": 0.8}}, by="Sam")
    assert r["kind"] == "clip" and r["file"] == "01 - dunk (v2).mp4"
    assert run(worker) == 1
    assert desk.edit_state(r["id"])["status"] == "done"
    eng = next(e for e in FakeEngine.made[::-1] if hasattr(e, "exported"))
    c = eng.exported.clips[0]
    assert (c.start, c.end, c.top_text, c.crop_x, c.title) == (96.0, 121.0, "Posterized", 0.8, "Poster dunk")
    assert (eng.s.aspect, eng.s.banner_top, eng.s.ai_polish) == ("vertical_crop", True, False)
    assert eng.exported.meta["automation"]["keep"] == "download"
    row = E.row_of(E.index_of(folder), r["file"])
    assert (row["start"], row["end"], row["length"], row["caption"], row["edit_of"]) == (96.0, 121.0, 25.0, "Over him",
                                                                                         "01 - dunk.mp4")
    assert row["edit"] == {"captions": row["edit"]["captions"], "aspect": "vertical_crop", "crop_x": 0.8,
                           "headline": "Posterized"}
    assert (folder / r["file"]).is_file() and not (Path(worker.s.cache_path) / "edits").exists() or \
        not any((Path(worker.s.cache_path) / "edits").iterdir())  # the work folder is cleared


# ---------------------------------------------------------------- Telegram: a sports request is a download


def test_what_is_said_about_posting_or_downloading_is_understood():
    from ytclip.telegram import bot as botmod

    assert "deliver" not in botmod.parse_sports("NBA Lakers vs Celtics")  # the desk decides: a download
    for text in ("NBA Lakers vs Celtics, post it", "NBA Lakers vs Celtics and post them", "NBA Lakers vs Celtics "
                 "queue it"):
        got = botmod.parse_sports(text)
        assert (got["deliver"], got["teams"]) == ("post", "Lakers, Celtics")
    got = botmod.parse_sports("NBA Lakers vs Celtics 3 clips to download")
    assert (got["deliver"], got["teams"], got["plays"]) == ("download", "Lakers, Celtics", 3)


def test_a_request_from_the_chat_is_made_to_download_with_nothing_queued(sports):  # noqa: F811
    w = sports
    sent = w.say("/sports NBA Lakers vs Celtics 2 clips", SAM)
    assert "yours to download and edit (not posted, no account tied to them)" in sent["text"]
    assert "ℹ It's made to download: it isn't queued to post and no TikTok account is tied to it." in sent["text"]
    assert "will wait for your OK" not in sent["text"]
    o = w.bot.desk.orders()[0]
    assert (o["deliver"], o["requires"]) == ("download", {})
    assert w.work() == 3
    items = w.take()
    assert len(items) == 2 and all((i.keep, i.status, i.account, i.review) == ("download", "new", "", "")
                                   for i in items)
    assert w.store.queue() == [] and items[0].category_id == "h1" and items[0].for_account == ""
    w.bot.check(w.clock[0])
    mine = to(w, SAM)[-1]
    assert "Made 2 clips from Lakers at Celtics, to download" in mine["text"]
    assert "⬇ Made to download: nothing is queued to post and no account is tied to it." in mine["text"]
    taps = buttons(mine["reply_markup"])
    assert (taps[0][1], taps[1]) == (f"v:{items[0].id}", ("⬇ File", f"dl:{items[0].id}"))
    # its card says what it is, and offers the file, the queue, or the bin
    text, markup = w.bot.card(items[0])
    assert "⬇ Yours to download: not queued to post, no account tied to it" in text
    assert [b[1] for b in buttons(markup)] == [f"dl:{items[0].id}", f"del:{items[0].id}", f"pq:{items[0].id}",
                                               "list:0"]


def test_the_file_is_sent_into_the_chat_under_its_name_and_a_download_can_be_queued(sports):  # noqa: F811
    w = sports
    w.say("/sports NBA Lakers vs Celtics 2 clips", SAM)
    w.work()
    a, b = w.take()
    w.bot.check(w.clock[0])
    mid = max(k[1] for k in w.fake.messages if k[0] == SAM["id"])
    w.fake.clear()
    w.press(f"dl:{a.id}", mid, SAM)
    doc = w.fake.sent("sendDocument")[-1]
    assert doc["chat_id"] == str(SAM["id"]) or doc["chat_id"] == SAM["id"]
    assert doc["document"]["name"] == Path(a.path).stem.split(" - ", 1)[-1] + ".mp4" or doc["document"]["name"].endswith(".mp4")
    assert doc["document"]["size"] == Path(a.path).stat().st_size and "The full-quality file" in doc["caption"]
    assert "<b>Sam</b> took the file of" in to(w, JUSTYN)[-1]["text"]
    # over what a bot may send: said, with where to get it instead
    from ytclip.telegram import editor_handler

    big = editor_handler.LIMIT
    try:
        editor_handler.LIMIT = 10
        assert "a bot may send 50 MB at most" in w.bot.press_file(SAM["id"], mid, a.id, None)
    finally:
        editor_handler.LIMIT = big
    assert "isn't in the library any more" in w.bot.press_file(SAM["id"], mid, "nope", None)
    # 📤 queue it to post after all: its category's account takes it
    w.fake.clear()
    w.press(f"pq:{b.id}", mid, SAM)
    assert (b.keep, b.status, b.account) == ("", "queued", w.account.id)
    assert "<b>Sam</b> queued" in to(w, JUSTYN)[-1]["text"]
    assert "It isn't a download" in w.bot.press_queue(SAM["id"], mid, b.id, None)


# ---------------------------------------------------------------- the Mini App: download, edit, keep, queue


@pytest.fixture
def editing(served, monkeypatch, edits):  # noqa: F811
    """The served app with a downloadable reel of a game on the desk, and a worker to make its edits."""
    from ytclip import ai
    from ytclip.sportsdesk.desk import DeskWorker

    w = served
    w.s.categories = w.s.categories + [{"id": "h1", "name": "Hoops", "sport": "basketball",
                                        "account_id": w.account.id}]
    w.s.cache_dir = str(w.root / "cache")  # never the real profile's downloads
    monkeypatch.setattr(ai, "order", lambda s: [])
    monkeypatch.setattr(DeskWorker, "_youtube_pause", lambda self: None)
    d = w.bot.desk
    seed(d, 1, highlights=True)
    w.folder = reel_folder(w.root)
    d.change("v0", lambda g: g.update(highlights=hls(6), duration=600.0, reels=[
        {"at": 1.0, "folder": str(w.folder), "file": str(w.folder / "01 - Lakers at Celtics.mp4")}]))
    w.worker = DeskWorker(d, lambda: w.s, "test:1", engine_factory=FakeEngine)
    w.it = w.take()[0]
    w.edits = edits
    yield w
    w.bot.stop()


def test_downloading_from_the_app(editing):
    w = editing
    d = w.session(SAM)
    status, vids, _ = w.call("GET", "/api/videos", token=d["token"])
    v = next(x for x in vids["items"] if x["id"] == w.it.id)
    assert (v["state"], v["keep"], v["editable"], v["account"]) == ("download", "download", "reel", "")
    assert v["can"]["queue"] is True and v["can"]["ok"] is False and v["can"]["post_now"] is False
    size = Path(w.it.path).stat().st_size
    status, body, r = w.call("GET", f"/api/videos/{w.it.id}/file?m={d['media']}&dl=1")
    assert status == 200 and len(body) == size
    assert r.getheader("Content-Disposition") == ("attachment; filename=\"A Night Of Dunks.mp4\"; "
                                                  "filename*=UTF-8''A%20Night%20Of%20Dunks.mp4")
    assert r.getheader("Access-Control-Allow-Origin") == "https://web.telegram.org"
    _, _, played = w.call("GET", f"/api/videos/{w.it.id}/file?m={d['media']}")
    assert played.getheader("Content-Disposition") is None  # watched in the page: not a download
    assert w.call("GET", f"/api/videos/{w.it.id}/file?dl=1")[0] == 401  # and never without the media token
    # the bot sends the file into the chat of whoever asked
    w.fake.clear()
    status, r, _ = w.call("POST", f"/api/videos/{w.it.id}/send", token=d["token"])
    assert status == 200 and "Sending the file" in r["message"]
    sent = w.fake.sent("sendDocument")
    assert len(sent) == 1 and int(sent[0]["chat_id"]) == SAM["id"] and sent[0]["document"]["name"] == "A Night Of Dunks.mp4"
    # the game's own footage is streamed for the editor only while it's in the PC's cache
    assert w.call("GET", f"/api/sports/source/v0?m={d['media']}")[0] == 404
    make_video(Path(w.s.cache_path) / "v0.mp4", seconds=1.0)
    status, body, r = w.call("GET", f"/api/sports/source/v0?m={d['media']}", headers={"Range": "bytes=0-9"})
    assert status == 206 and len(body) == 10
    assert w.call("GET", "/api/sports/source/v0")[0] == 401 and w.call("GET", f"/api/sports/source/nope?m={d['media']}")[0] == 404


def test_an_edit_from_the_app_comes_back_as_v2_and_one_can_be_kept(editing):
    w = editing
    d = w.session(SAM)
    assert w.call("GET", f"/api/videos/{w.it.id}/edit")[0] == 401
    status, doc, _ = w.call("GET", f"/api/videos/{w.it.id}/edit", token=d["token"])
    assert status == 200 and (doc["kind"], doc["item"], doc["headline"], doc["editing"]) == ("reel", w.it.id,
                                                                                            "A Night Of Dunks", False)
    plays = [{"id": p["id"], "start": p["start"], "end": p["end"], "label": p["label"], "badge": p["badge"]}
             for p in doc["plays"]]
    status, err, _ = w.call("POST", f"/api/videos/{w.it.id}/edit", {"changes": {"plays": plays}}, token=d["token"])
    assert status == 400 and "Nothing was changed" in err["error"]
    w.fake.clear()
    changes = {"plays": plays[1:], "opening": dict(doc["opening"], voice="off"), "words": doc["words"],
               "look": dict(doc["look"], music="off")}
    status, r, _ = w.call("POST", f"/api/videos/{w.it.id}/edit", {"changes": changes}, token=d["token"])
    assert status == 200 and r["version"] == 2 and r["held"] is False
    assert r["message"] == ("Took out 1 play; turned the narrator off; turned the music off. The PC is cutting v2 "
                            "now; it comes back beside this one, each yours to download.")
    assert r["critique"]["how"] == "edit" and r["critique"]["name"] == "Sam"
    assert to(w, JUSTYN)[-1]["text"] == ("✂️ <b>Sam</b> edited “A Night Of Dunks”:\n<i>Took out 1 play; turned the "
                                         "narrator off; turned the music off.</i>")
    status, err, _ = w.call("POST", f"/api/videos/{w.it.id}/edit", {"changes": changes}, token=d["token"])
    assert status == 409 and "being made already" in err["error"]
    _, vids, _ = w.call("GET", "/api/videos", token=d["token"])
    v = next(x for x in vids["items"] if x["id"] == w.it.id)
    assert v["fixing"] is True and v["critiques"][0]["status"] == "queued"
    # the PC cuts it, posting brings it in, and both are told with the new version's card
    assert run(w.worker) == 1
    new = w.take()[0]
    assert Path(new.path).name == "01 - Lakers at Celtics (v2).mp4" and (new.keep, new.status) == ("download", "new")
    w.fake.clear()
    w.bot.check(w.clock[0])
    videos = {int(v["chat_id"]): v for v in w.fake.sent("sendVideo")}
    mine, theirs = videos[SAM["id"]], videos[JUSTYN["id"]]
    assert mine["caption"].startswith("✂️ <b>v2 (Edited)</b>: here's your edit of “A Night Of Dunks”. Both versions "
                                      "are yours to download")
    assert theirs["caption"].startswith("✂️ <b>v2 (Edited)</b>: Sam's edit of “A Night Of Dunks”.")
    assert state.load(w.state_file)["asked"] == {}
    _, vids, _ = w.call("GET", "/api/videos", token=d["token"])
    by_id = {x["id"]: x for x in vids["items"]}
    assert by_id[w.it.id]["version"] == "v1" and by_id[new.id]["version"] == "v2 (Edited)"
    assert by_id[w.it.id]["fixing"] is False and by_id[new.id]["editable"] == "reel"
    assert [(x["version"], x["keep"], x["how"]) for x in by_id[new.id]["versions"]] == [(1, True, ""), (2, True, "edit")]
    # keeping only v2 deletes v1 from the PC; nothing is queued by it
    old = Path(w.it.path)
    status, r, _ = w.call("POST", f"/api/videos/{new.id}/approve", token=d["token"])
    assert status == 200 and r["message"] == "v2 (Edited) is the one kept. v1 was deleted."
    assert not old.exists() and w.store.get(w.it.id) is None and Path(new.path).is_file()
    assert (new.keep, new.status, w.store.queue()) == ("download", "new", [])
    assert "<b>Sam</b> kept v2 (Edited) of “A Night Of Dunks”" in to(w, JUSTYN)[-1]["text"]
    # and it's queued to post only when someone says so
    w.accounts.add("").username = "second"  # (with two accounts, which one isn't guessed)
    status, err, _ = w.call("POST", f"/api/videos/{new.id}/queue", {}, token=d["token"])
    assert status == 409 and "Pick the category" in err["error"] and "still yours to download" in err["error"]
    assert new.keep == "download" and w.store.queue() == []
    status, r, _ = w.call("POST", f"/api/videos/{new.id}/queue", {"category": "h1"}, token=d["token"])
    assert status == 200 and r["message"].startswith("Queued to post to @psych.") and r["video"]["state"] in ("queued",
                                                                                                             "review")
    assert new.keep == "" and new.account == w.account.id


def test_an_edit_that_fails_says_so_and_leaves_the_first_version(editing):
    w = editing
    d = w.session(SAM)
    status, r, _ = w.call("POST", f"/api/videos/{w.it.id}/edit", {"changes": {"look": {"bug": False}}},
                          token=d["token"])
    assert status == 200

    def boom(*a, **k):
        raise montage.ReelError("The footage couldn't be read.")

    from ytclip.sportsdesk import reel as R

    R.make = boom  # (the fixture's monkeypatch puts the real one back)
    run(w.worker)
    w.fake.clear()
    w.bot.check(w.clock[0])
    assert to(w, SAM)[-1]["text"] == ("✖ Couldn't make the edit of “A Night Of Dunks”: The footage couldn't be read.\n"
                                      "The first version is as it was.")
    _, vids, _ = w.call("GET", "/api/videos", token=d["token"])
    v = next(x for x in vids["items"] if x["id"] == w.it.id)
    assert v["fixing"] is False and v["critiques"][0]["status"] == "failed" and v["versions"] == []
    assert Path(w.it.path).is_file() and state.load(w.state_file)["asked"] == {}


def test_only_a_sports_video_with_its_plan_opens_in_the_editor(editing):
    from test_telegram import clip_folder

    w = editing
    clip_folder(w.root)
    other = w.take()[0]
    d = w.session(SAM)
    status, err, _ = w.call("GET", f"/api/videos/{other.id}/edit", token=d["token"])
    assert status == 409 and "Only a sports clip or reel" in err["error"]
    assert w.bot.editable(other) == "" and w.bot.editable(w.it) == "reel" and w.bot.editable("nope") == ""
    assert w.call("GET", "/api/videos/nope/edit", token=d["token"])[0] == 404
    assert w.call("POST", f"/api/videos/{other.id}/edit", {"changes": {}}, token=d["token"])[0] == 409
    assert w.call("POST", f"/api/videos/{other.id}/queue", {}, token=d["token"])[0] == 409  # it isn't a download


def test_the_page_has_downloads_and_the_editor():
    js = mini_app_page.JS
    for needle in ("['download', 'Downloads']", "function openEditor(", "function trimmer(", "api/sports/source/",
                   "'/edit', { changes: changesOf() }", "tg.downloadFile", "'&dl=1'", "/send'", "/queue'",
                   "I’ll download it", "Number them again", "Make this version"):
        assert needle in js, needle
    assert ".ed-foot" in mini_app_page.CSS and ".trim" in mini_app_page.CSS
