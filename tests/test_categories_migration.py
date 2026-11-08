"""3.2 renamed niches to categories: every file an older version wrote is read, and moved over once."""

import json
import sqlite3

from ytclip import categories
from ytclip.jobs import Job, JobDB
from ytclip.settings import Settings, app_dir

OLD_NICHE = {"id": "game01", "name": "Gaming", "account": "@gameclips", "account_id": "acc1", "daily": 3,
             "channels": "@GameChan", "categories": "20, 24", "keywords": "fortnite"}


def _write_settings(data: dict):
    path = app_dir() / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_settings_from_31_read_as_categories():
    _write_settings({"niches": [OLD_NICHE], "research_niche_charts": False, "pick_daily": 7})
    s = Settings.load()
    [c] = categories.load(s)
    assert (c.id, c.name, c.account_id, c.daily, c.yt_categories) == ("game01", "Gaming", "acc1", 3, "20, 24")
    assert c.yt_category_ids() == {"20", "24"} and c.on
    assert s.research_category_charts is False and s.pick_daily == 7


def test_migrate_disk_rewrites_settings_once_and_keeps_the_old_file():
    path = _write_settings({"niches": [OLD_NICHE], "research_niche_charts": False, "pick_daily": 7})
    old = path.read_text(encoding="utf-8")
    done = categories.migrate_disk()
    assert done and "renamed 1 niche to categories" in done[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "niches" not in data and "research_niche_charts" not in data
    assert data["categories"][0]["yt_categories"] == "20, 24" and "categories" not in data["categories"][0]
    assert data["research_category_charts"] is False and data["pick_daily"] == 7
    assert (app_dir() / categories.BACKUP_FILE).read_text(encoding="utf-8") == old
    assert categories.migrate_disk() == []  # nothing left to do
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_a_niches_json_beside_the_settings_is_taken_in():
    _write_settings({"pick_daily": 2})
    legacy = app_dir() / "niches.json"
    legacy.write_text(json.dumps({"niches": [OLD_NICHE]}), encoding="utf-8")
    done = categories.migrate_disk()
    assert any("niches.json" in line for line in done)
    assert not legacy.exists() and (app_dir() / "niches.json.migrated").exists()
    [c] = categories.load(Settings.load())
    assert c.name == "Gaming" and c.yt_categories == "20, 24"


def test_a_niches_json_never_replaces_categories_already_there():
    _write_settings({"categories": [{"id": "pods", "name": "Podcasts", "account_id": "a"}]})
    (app_dir() / "niches.json").write_text(json.dumps([OLD_NICHE]), encoding="utf-8")
    categories.migrate_disk()
    assert [c.name for c in categories.load(Settings.load())] == ["Podcasts"]


def test_an_unreadable_settings_file_is_left_alone():
    path = _write_settings({})
    path.write_text("{not json", encoding="utf-8")
    assert categories.migrate_disk() == []
    assert path.read_text(encoding="utf-8") == "{not json"


def test_the_jobs_database_column_is_renamed_keeping_what_it_held(tmp_path):
    path = tmp_path / "jobs.db"
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT, category TEXT, niche TEXT, status TEXT)')
    con.execute("INSERT INTO jobs VALUES ('v1', 'u', '20', 'game01', 'queued')")
    con.commit()
    con.close()
    db = JobDB(path)
    cols = {r["name"] for r in db.con.execute("PRAGMA table_info(jobs)")}
    assert "niche" not in cols and "category_id" in cols
    j = db.get("v1")
    assert j.category_id == "game01" and j.category == "20"  # the YouTube category is untouched
    assert db.category_counts(0)["game01"]["waiting"] == 1
    db.close()
    JobDB(path).close()  # opening it again finds nothing to do


def test_both_columns_after_31_opened_the_file_again(tmp_path):
    """3.1 opened after 3.2 adds its "niche" column back: what it wrote there still counts."""
    path = tmp_path / "jobs.db"
    db = JobDB(path)
    db.add(Job("new", "u", category_id="pods"))
    db.con.execute('ALTER TABLE jobs ADD COLUMN "niche" TEXT')
    db.con.execute("INSERT INTO jobs (id, url, niche, status, origins) VALUES ('old', 'u', 'game01', 'queued', '[]')")
    db.close()
    db = JobDB(path)
    assert db.get("old").category_id == "game01" and db.get("new").category_id == "pods"


def test_picks_requests_and_research_from_31_keep_their_category():
    from ytclip.orders import Order
    from ytclip.policy import pick_category
    from ytclip.trending import TrendingVideo

    assert pick_category({"id": "v", "niche": "game01"}) == "game01"
    assert pick_category({"id": "v", "category_id": "pods"}) == "pods"
    o = Order.from_json({"id": "r1", "count": 5, "min_len": 30, "max_len": 45, "niche": "game01"})
    assert o.category_id == "game01" and "niche" not in o.to_json()
    v = TrendingVideo.from_json({"id": "v", "title": "t", "channel": "c", "published": 0.0, "duration": 60.0,
                                 "views": 1, "niche": "game01", "category": "20"})
    assert (v.category_id, v.category) == ("game01", "20")


def test_the_posting_library_and_accounts_from_31_read_the_new_names(tmp_path):
    from ytclip.posting.accounts import Accounts
    from ytclip.posting.store import Store

    lib = tmp_path / "library.json"
    lib.write_text(json.dumps({"version": 2, "items": [{"id": "i1", "path": "x.mp4", "niche": "Gaming",
                                                          "niche_id": "game01"}]}), encoding="utf-8")
    item = Store(lib).get("i1")
    assert (item.category_name, item.category_id) == ("Gaming", "game01")
    accs = tmp_path / "accounts.json"
    accs.write_text(json.dumps({"version": 2, "accounts": [{"id": "a1", "username": "u", "niche": "R6 clips"}]}),
                    encoding="utf-8")
    assert Accounts.load(accs).get("a1").audience == "R6 clips"


def test_clips_json_from_31_says_niche(tmp_path):
    from ytclip.posting import clipsjson

    data = {"video": {"id": "v", "title": "T"}, "automation": {"niche": "Gaming", "niche_id": "game01",
                                                               "account_id": "acc1"},
            "clips": [{"file": "a.mp4", "score": 80}]}
    (tmp_path / "clips.json").write_text(json.dumps(data), encoding="utf-8")
    row = clipsjson.read_folder_index(tmp_path)["a.mp4"]
    assert (row["category"], row["category_id"], row["facts"]["category"]) == ("Gaming", "game01", "Gaming")


def test_the_old_command_line_names_still_work(capsys):
    from ytclip import cli

    Settings().apply({"categories": [{"id": "g", "name": "Gaming", "account_id": "a"}]}).save()
    assert cli.main(["niches", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)[0]["name"] == "Gaming" and "ytclip categories" in out.err
    p = cli.build_find_parser()
    assert p.parse_args(["x", "--niche", "Gaming"]).category == "Gaming"
    assert "--niche is now --category" in capsys.readouterr().err
    assert p.parse_args(["x", "--category", "Pods"]).category == "Pods"
