import json
import os
import time

import pytest

from ytclip import cleanup
from ytclip.cleanup import TikTokManager, _key
from ytclip.settings import Settings

OLD = time.time() - 30 * 86400  # a month ago: old enough for anything


def _write(path, data=b"x" * 1000, when=OLD):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (when, when))
    return path


def _video(folder, clips, video_id="vid00000001"):
    """An exported video folder: clips.json, README, thumbnails, and each clip's .mp4/.srt."""
    records = []
    for rank, (name, rejected) in enumerate(clips, 1):
        file = f"_rejected/{name}" if rejected else name
        _write(folder / file)
        _write((folder / file).with_suffix(".srt"), b"1\n")
        _write(folder / "thumbnails" / f"{rank:02d}.jpg", b"j" * 100)
        rec = {"rank": rank, "file": file, "title": f"Clip {rank}"}
        if rejected:
            rec["qc"] = {"ok": False, "problems": ["silent"]}
        records.append(rec)
    (folder / "clips.json").write_text(json.dumps({"video": {"id": video_id, "title": "T"}, "clips": records}),
                                       encoding="utf-8")
    _write(folder / "README.md", b"# T")
    os.utime(folder / "clips.json", (OLD, OLD))
    return folder


@pytest.fixture
def setup(tmp_path):
    out = tmp_path / "out"
    s = Settings().apply({"output_dir": str(out), "niches": [{"id": "g", "name": "Gaming"}]})
    a = _video(out / "Automation" / "Gaming" / "Chan" / "2026-01-01 A [aaaaaaaaaaa]",
               [("01.mp4", False), ("02.mp4", False), ("03.mp4", True), ("04.mp4", False)], "aaaaaaaaaaa")
    b = _video(out / "Automation" / "Gaming" / "Chan" / "2026-01-02 B [bbbbbbbbbbb]",
               [("01.mp4", False)], "bbbbbbbbbbb")
    tm = TikTokManager(found=True, keep={_key(a / "01.mp4")}, posted={_key(a / "02.mp4"): OLD,
                                                                        _key(b / "01.mp4"): time.time() - 3600},
                       removed={_key(a / "04.mp4")}, watch_folders=[str(out / "Automation")])
    return s, a, b, tm


def test_the_plan_sorts_clips_by_what_tiktok_manager_did_with_them(setup):
    s, a, b, tm = setup
    p = cleanup.plan(s, tm=tm, kinds=cleanup.CLIP_KINDS)
    got = {(it.kind, it.path.relative_to(s.output_path).as_posix()) for it in p.items}
    assert got == {("rejected", f"{a.relative_to(s.output_path).as_posix()}/_rejected/03.mp4"),
                   ("posted", f"{a.relative_to(s.output_path).as_posix()}/02.mp4"),
                   ("posted", f"{b.relative_to(s.output_path).as_posix()}/01.mp4"),
                   ("removed", f"{a.relative_to(s.output_path).as_posix()}/04.mp4")}
    assert p.kept_for_tm == 1 and not p.notes
    posted = next(it for it in p.items if it.path == a / "02.mp4")
    assert posted.companions == [a / "02.srt", a / "thumbnails" / "02.jpg"] and posted.size == 1000 + 2 + 100


def test_posted_clips_count_their_age_from_when_they_were_posted(setup):
    s, a, b, tm = setup
    p = cleanup.plan(s, tm=tm, kinds=["posted"], older_than_days=1)
    assert [it.path for it in p.items] == [a / "02.mp4"]  # b's was posted an hour ago


def test_cleaning_removes_clips_their_extras_and_emptied_folders(setup):
    s, a, b, tm = setup
    p = cleanup.plan(s, tm=tm, kinds=cleanup.CLIP_KINDS)
    r = cleanup.apply(p, cleanup.CLIP_KINDS)
    assert r.removed == 4 and r.by_kind == {"rejected": 1, "posted": 2, "removed": 1} and not r.failed
    assert r.summary() == "1 rejected clip, 2 posted clips and 1 removed clip (4 KB freed)"
    # A keeps the clip TikTok Manager still has, and its clips.json now lists only that one.
    assert sorted(f.name for f in a.iterdir()) == ["01.mp4", "01.srt", "README.md", "clips.json", "thumbnails"]
    data = json.loads((a / "clips.json").read_text(encoding="utf-8"))
    assert [c["file"] for c in data["clips"]] == ["01.mp4"] and data["cleaned_up"]
    assert sorted(f.name for f in (a / "thumbnails").iterdir()) == ["01.jpg"]
    # B had nothing left: it goes, but the niche folder stays (TikTok Manager may watch it).
    assert not b.exists() and r.folders == 1
    assert (s.output_path / "Automation" / "Gaming").is_dir()


def test_a_partly_cleaned_folder_keeps_its_date_and_its_counts_and_readme_follow(setup):
    s, a, b, tm = setup
    data = json.loads((a / "clips.json").read_text(encoding="utf-8"))
    data["qc"] = {"passed": 3, "rejected": 1, "warned": 0}
    (a / "clips.json").write_text(json.dumps(data), encoding="utf-8")
    os.utime(a / "clips.json", (OLD, OLD))
    rows = ["# T", "", "| # | Clip | Time |", "|---|------|------|"]
    for rec in data["clips"]:
        rows += [f"| {rec['rank']} | [Clip](<{rec['file']}>) | 0:00 |", "| | *why it was picked* | |"]
    (a / "README.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    cleanup.apply(cleanup.plan(s, tm=tm, kinds=cleanup.CLIP_KINDS), cleanup.CLIP_KINDS)
    data = json.loads((a / "clips.json").read_text(encoding="utf-8"))
    assert data["qc"] == {"passed": 1, "rejected": 0, "warned": 0}
    assert (a / "clips.json").stat().st_mtime == pytest.approx(OLD)  # the Library's order stays
    readme = (a / "README.md").read_text(encoding="utf-8")
    assert "(<01.mp4>)" in readme and "02.mp4" not in readme and "03.mp4" not in readme
    assert readme.count("why it was picked") == 1


def test_the_recycle_bin_summary_doesnt_say_freed():
    r = cleanup.Result(removed=1, freed=2048, by_kind={"posted": 1})
    assert r.summary(trashed=True) == "1 posted clip (2 KB moved to the Recycle Bin; emptying it frees the space)"


def test_busy_videos_include_the_studios_list_and_the_real_id_being_clipped(tmp_path):
    from ytclip.jobs import Job, JobDB

    db = JobDB(tmp_path / "jobs.db")
    db.add(Job("https://example.com/v/odd", "https://example.com/v/odd", status="working"))
    db.add(Job("done1234567", "u", status="done"))
    db.set_meta("working_video", "realid12345")
    session = tmp_path / "session.json"
    mine = tmp_path / "my talk.mp4"
    mine.write_bytes(b"video")
    session.write_text(json.dumps([{"source": "https://youtu.be/studio12345"}, {"source": str(mine)},
                                   {"source": str(tmp_path / "gone.mp4")},
                                   {"source": "https://vimeo.com/123", "id": "vimeo123"},
                                   {"source": "https://www.youtube.com/embed/embedded123"}, "junk"]),
                       encoding="utf-8")
    from ytclip.download import local_id

    assert cleanup.busy_ids(db, session) == {"https://example.com/v/odd", "realid12345", "studio12345",
                                             "embedded123", local_id(mine), "vimeo123", "https://vimeo.com/123"}
    db.update("https://example.com/v/odd", status="done")
    assert cleanup.busy_ids(db, tmp_path / "none.json") == set()  # nothing is being clipped now


def test_the_last_folders_go_but_never_the_niche_folder(setup):
    s, a, b, tm = setup
    tm.keep.clear()
    tm.posted[_key(a / "01.mp4")] = OLD
    r = cleanup.apply(cleanup.plan(s, tm=tm, kinds=cleanup.CLIP_KINDS), cleanup.CLIP_KINDS)
    assert r.folders == 2 and not (s.output_path / "Automation" / "Gaming" / "Chan").exists()
    assert (s.output_path / "Automation" / "Gaming").is_dir()


def test_only_the_kinds_asked_for_go(setup):
    s, a, b, tm = setup
    p = cleanup.plan(s, tm=tm)
    r = cleanup.apply(p, ["rejected"])
    assert r.by_kind == {"rejected": 1}
    assert (a / "02.mp4").exists() and not (a / "_rejected").exists()


def test_without_tiktok_manager_only_rejected_clips_can_go(setup):
    s, a, b, _ = setup
    p = cleanup.plan(s, tm=TikTokManager(), kinds=cleanup.CLIP_KINDS)
    assert [it.kind for it in p.items] == ["rejected"]
    assert "isn't set up on this PC" in p.notes[0]


def test_an_unreadable_library_keeps_every_clip(setup):
    s, *_ = setup
    p = cleanup.plan(s, tm=TikTokManager(unreadable=True))
    assert not [it for it in p.items if it.kind in cleanup.CLIP_KINDS]
    assert "couldn't be read" in p.notes[0]


def test_an_unwatched_automation_folder_is_pointed_out(setup):
    s, a, b, tm = setup
    tm.watch_folders = [str(s.output_path / "Elsewhere")]
    assert "isn't watching your Automation folder" in cleanup.plan(s, tm=tm, kinds=["posted"]).notes[0]
    tm.watch_folders = [str(s.output_path / "Automation" / "Gaming")]  # a niche's folder counts
    assert not cleanup.plan(s, tm=tm, kinds=["posted"]).notes


def test_a_video_being_clipped_and_fresh_files_are_left_alone(setup):
    s, a, b, tm = setup
    assert not [it for it in cleanup.plan(s, tm=tm, busy={"aaaaaaaaaaa"}).items if it.folder == a]
    os.utime(b / "01.mp4", None)  # just written
    assert not [it for it in cleanup.plan(s, tm=tm).items if it.folder == b]


def test_clips_json_cant_point_outside_its_folder(setup, tmp_path):
    s, a, b, tm = setup
    outside = _write(tmp_path / "precious.mp4")
    data = json.loads((b / "clips.json").read_text(encoding="utf-8"))
    data["clips"].append({"rank": 9, "file": "../../../../../../precious.mp4", "qc": {"ok": False}})
    (b / "clips.json").write_text(json.dumps(data), encoding="utf-8")
    p = cleanup.plan(s, tm=tm)
    assert outside not in [it.path for it in p.items]
    cleanup.apply(p, cleanup.KINDS)
    assert outside.exists()


def test_rejected_clips_an_older_export_left_behind(setup):
    s, a, b, tm = setup
    stray = _write(b / "_rejected" / "07 - old.mp4")
    p = cleanup.plan(s, tm=tm, kinds=["rejected"])
    assert stray in [it.path for it in p.items]


def test_the_download_cache(tmp_path):
    cache = tmp_path / "out" / ".downloads"
    s = Settings().apply({"output_dir": str(tmp_path / "out")})
    files = {name: _write(cache / name) for name in (
        "aaaaaaaaaaa.mp4", "aaaaaaaaaaa.analysis.npz", "aaaaaaaaaaa.whisper-base.json", "aaaaaaaaaaa.en.vtt",
        "aaaaaaaaaaa.video.json", "bbbbbbbbbbb.mp4.part", "bbbbbbbbbbb.f137.mp4", "busy_video1.mp4",
        "holiday.mp4", "notes.txt", "x.part")}
    _write(cache / "ccccccccccc.webm", when=time.time())  # still being written
    _write(cache / "ddddddddddd.mp4.part", when=time.time() - 600)  # touched 10 minutes ago
    p = cleanup.plan(s, busy={"busy_video1"}, tm=TikTokManager(), kinds=cleanup.CACHE_KINDS)
    got = {it.path.name: it.kind for it in p.items}
    assert got == {"aaaaaaaaaaa.mp4": "downloads", "aaaaaaaaaaa.analysis.npz": "analysis",
                   "aaaaaaaaaaa.whisper-base.json": "analysis", "aaaaaaaaaaa.en.vtt": "analysis",
                   "bbbbbbbbbbb.mp4.part": "partial", "bbbbbbbbbbb.f137.mp4": "partial"}
    download = next(it for it in p.items if it.kind == "downloads")
    assert download.companions == [files["aaaaaaaaaaa.video.json"]] and download.size == 2000  # its details go too
    cleanup.apply(p, ["partial", "downloads"])
    assert not files["aaaaaaaaaaa.mp4"].exists() and not files["aaaaaaaaaaa.video.json"].exists()
    assert files["aaaaaaaaaaa.analysis.npz"].exists()
    assert files["holiday.mp4"].exists() and files["busy_video1.mp4"].exists() and files["x.part"].exists()


def test_details_left_without_their_video_are_analysis(tmp_path):
    cache = tmp_path / "out" / ".downloads"
    s = Settings().apply({"output_dir": str(tmp_path / "out")})
    _write(cache / "aaaaaaaaaaa.video.json")  # the video went after clipping (Delete source videos)
    assert [(it.path.name, it.kind) for it in cleanup.plan(s, tm=TikTokManager(), kinds=cleanup.CACHE_KINDS).items] \
        == [("aaaaaaaaaaa.video.json", "analysis")]


def test_leftover_streams_with_a_format_suffix_are_unfinished_downloads(tmp_path):
    cache = tmp_path / "out" / ".downloads"
    s = Settings().apply({"output_dir": str(tmp_path / "out")})
    for name in ("aaaaaaaaaaa.f251-drc.webm", "aaaaaaaaaaa.f137-sr.mp4", "aaaaaaaaaaa.f140.m4a.part-Frag3",
                 "aaaaaaaaaaa.temp.mp4", "aaaaaaaaaaa.f137.mp4.part.ytdl"):
        _write(cache / name)
    got = {it.path.name: it.kind for it in cleanup.plan(s, tm=TikTokManager(), kinds=["partial"]).items}
    assert set(got.values()) == {"partial"} and len(got) == 5


def test_in_a_cache_folder_you_picked_only_clipmonsters_files_count(tmp_path):
    """A shared folder (Downloads, D:\\Videos) can hold files that just happen to have 11-letter names."""
    cache = tmp_path / "c"
    s = Settings().apply({"output_dir": str(tmp_path / "out"), "cache_dir": str(cache)})
    mine = {n: _write(cache / n) for n in ("12345.mp4", "12345.video.json", "aaaaaaaaaaa.analysis.npz",
                                            "bbbbbbbbbbb.f137.mp4.part", "ccccccccccc.whisper-base.json")}
    theirs = {n: _write(cache / n) for n in ("Holiday2023.mp4", "Tax_returns.pdf.part", "lecture_001.en.vtt",
                                              "family_trip.mkv", "Holiday2024.mp4.part")}  # a paused browser download
    p = cleanup.plan(s, tm=TikTokManager(), kinds=cleanup.CACHE_KINDS)
    assert {it.path.name for it in p.items} == {"12345.mp4", "aaaaaaaaaaa.analysis.npz", "bbbbbbbbbbb.f137.mp4.part",
                                                "ccccccccccc.whisper-base.json"}
    cleanup.apply(p, cleanup.CACHE_KINDS)
    assert all(f.exists() for f in theirs.values()) and not mine["12345.video.json"].exists()


def test_unfinished_downloads_keep_to_the_age_you_pick(tmp_path):
    """The daily run only takes things at least a day old, unfinished downloads too."""
    cache = tmp_path / "out" / ".downloads"
    s = Settings().apply({"output_dir": str(tmp_path / "out")})
    stopped = _write(cache / "aaaaaaaaaaa.f137.mp4.part", when=time.time() - 2 * 3600)  # two hours ago
    assert cleanup.plan(s, tm=TikTokManager(), kinds=["partial"], older_than_days=1).items == []
    assert [it.path for it in cleanup.plan(s, tm=TikTokManager(), kinds=["partial"]).items] == [stopped]


def test_preview_renders(tmp_path):
    temp = tmp_path / "temp"
    old = _write(temp / "ClipMonster_previews" / "preview_abc_12.mp4")
    _write(temp / "ClipMonster_previews" / "preview_new_1.mp4", when=time.time())
    layout = _write(temp / "ClipMonster_layout_3.jpg")
    _write(temp / "someone_else.mp4")
    p = cleanup.plan(Settings().apply({"output_dir": str(tmp_path / "o")}), tm=TikTokManager(), kinds=["previews"],
                     temp_dir=temp)
    assert sorted(it.path for it in p.items) == sorted([old, layout])


def test_reading_tiktok_managers_library(tmp_path):
    folder = tmp_path / "TikTokManager"
    folder.mkdir()
    clip = tmp_path / "c.mp4"
    (folder / "library.json").write_text(json.dumps({
        "version": 2,
        "items": [{"id": "1", "path": str(tmp_path / "q.mp4"), "status": "queued"},
                  {"id": "2", "path": str(clip), "status": "posted", "posted_at": 123.0},
                  {"id": "3", "path": str(tmp_path / "n.mp4"), "status": "new"},
                  {"id": "4", "path": None}, "junk"],
        "ignored": [_key(tmp_path / "r.mp4"), _key(tmp_path / "q.mp4")],
    }), encoding="utf-8")
    (folder / "settings.json").write_text(json.dumps({"watch_folders": ["C:/x", ""]}), encoding="utf-8")
    tm = cleanup.read_tiktok_manager(folder)
    assert tm.found and tm.posted == {_key(clip): 123.0}
    assert tm.keep == {_key(tmp_path / "q.mp4"), _key(tmp_path / "n.mp4")}
    assert tm.removed == {_key(tmp_path / "r.mp4")}  # a removed file added back by hand is kept
    assert tm.watch_folders == ["C:/x"]
    (folder / "library.json").write_text("{ half a file", encoding="utf-8")
    assert cleanup.read_tiktok_manager(folder).unreadable
    assert not cleanup.read_tiktok_manager(tmp_path / "nowhere").found


def test_files_that_cant_be_removed_are_reported(setup):
    s, a, b, tm = setup

    def stuck(path):
        if path.suffix == ".mp4":
            raise PermissionError("in use")
        path.unlink()

    r = cleanup.apply(cleanup.plan(s, tm=tm, kinds=["posted"]), ["posted"], remove=stuck)
    assert r.removed == 0 and len(r.failed) == 2 and "in use" in r.failed[0][1]
    assert (a / "02.mp4").exists() and (a / "02.srt").exists()  # nothing of a stuck clip goes


def test_kinds_and_sizes():
    assert cleanup.parse_kinds("posted, nonsense REJECTED") == ["rejected", "posted"]
    assert cleanup.size_text(512) == "512 bytes" and cleanup.size_text(5 * 1024 ** 3) == "5.0 GB"


def test_ytclip_clean(setup, capsys, monkeypatch):
    from ytclip import cli

    s, a, b, tm = setup
    s.save()
    monkeypatch.setattr(cleanup, "read_tiktok_manager", lambda folder=None: tm)
    assert cli.main(["clean"]) == 0
    out = capsys.readouterr().out
    assert "Clips the quality check rejected" in out and "Run again with --delete" in out
    assert (a / "02.mp4").exists()
    assert cli.main(["clean", "--only", "posted", "--delete"]) == 0
    assert "Cleared 2 posted clips" in capsys.readouterr().out
    assert not (a / "02.mp4").exists() and (a / "_rejected" / "03.mp4").exists()
    assert cli.main(["clean", "--only", "nonsense"]) == 2
