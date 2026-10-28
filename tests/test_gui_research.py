import pytest
from conftest import NOW, api_item

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtCore import Qt  # noqa: E402

from ytclip.research import ScanResult  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from ytclip.trending import DeepInfo, video_from_api  # noqa: E402


class Host:
    """Stands in for the main window: owns the settings the tab reads and writes."""

    def __init__(self, **overrides):
        self.settings = Settings().apply({"research_auto": False} | overrides)
        self.changes = []

    def get(self):
        return self.settings

    def change(self, name, value):
        self.changes.append((name, value))
        self.settings = self.settings.apply({name: value})


def _result():
    videos = []
    for i, (title, channel, score) in enumerate([("Big gaming moment", "GameChan", 88), ("Calm news", "News", 41),
                                                 ("Funny podcast", "Pod", 67)]):
        v = video_from_api(api_item(f"v{i}", title=title, channel=channel, hours_old=3))
        v.regions = {"US": i + 1, "GB": 4}
        v.score, v.new, v.velocity = score, i != 1, 1000 * (i + 1)
        v.parts = {"momentum": 0.9, "length": 1.0, "category": 0.9, "freshness": 1.0, "transcript": 0.6}
        v.reasons = [f"reason for {title}"]
        videos.append(v)
    videos[0].deep = DeepInfo(heatmap=[], checked=NOW)
    return ScanResult(videos, NOW, ["US", "GB"])


def _make(app, **overrides):
    from ytclip.gui.research import Research

    host = Host(**overrides)
    tab = Research(host.get, host.change)
    return tab, host


def test_without_a_key_the_setup_panel_shows(app):
    tab, _ = _make(app)
    assert not tab.key_panel.isHidden()
    assert "API key" in tab.summary.text()
    assert not tab.timer.isActive()
    tab.check()  # does nothing but focus the key box
    assert tab.job is None


def test_results_fill_the_table_best_first(app):
    tab, _ = _make(app, youtube_api_key="k")
    assert tab.key_panel.isHidden()
    tab._scan_done(_result(), auto=False)
    assert tab.table.rowCount() == 3
    titles = [tab.table.item(r, 1).text() for r in range(3)]
    assert titles[0] == "● Big gaming moment" and titles[-1] == "Calm news"
    assert tab.table.item(0, 3).text().startswith("US #1")
    assert tab._shown.id == "v0"  # best result selected and shown
    assert tab.d_score.text() == "88" and "Excellent" in tab.d_verdict.text()
    assert "reason for Big gaming moment" in tab.d_reasons.text()
    assert tab.parts_grid.count() == 3 * 7 + 1  # label, bar, value for every part + a heading


def test_sorting_by_a_column_uses_numbers(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab._scan_done(_result(), auto=False)
    tab.table.sortItems(5, Qt.DescendingOrder)  # views per hour
    assert [tab._video_at(r).id for r in range(3)] == ["v2", "v1", "v0"]


def test_filter_and_hide_sent(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab._scan_done(_result(), auto=False)
    tab.search.setText("pod")
    assert [tab._video_at(r).id for r in range(3) if not tab.table.isRowHidden(r)] == ["v2"]
    assert tab.count.text() == "1 of 3 videos"
    tab.search.clear()

    sent = []
    tab.send.connect(sent.append)
    tab.table.selectRow(0)
    tab._send_selected()
    assert sent == [["https://www.youtube.com/watch?v=v0"]]
    assert tab.history.videos["v0"]["sent"]
    assert tab.table.item(0, 1).text().startswith("✓ ")
    tab.hide_sent.setChecked(True)
    assert tab.table.isRowHidden(0)
    assert tab.count.text() == "2 of 3 videos"


def test_new_high_scorers_raise_hits_while_hidden(app):
    tab, host = _make(app, youtube_api_key="k", research_alert=60)
    counts, notices = [], []
    tab.hits_changed.connect(counts.append)
    tab.notice.connect(notices.append)
    tab._scan_done(_result(), auto=True)  # widget never shown, like a background tab
    assert counts == [2]
    assert "2 new videos worth clipping" in notices[0]
    tab.show()
    assert counts[-1] == 0


def test_filters_save_settings_and_schedule_a_check(app):
    tab, host = _make(app, youtube_api_key="k")
    tab._toggle_region("DE", True)
    assert host.settings.research_regions.endswith("DE")
    assert tab.regions_btn.text().endswith("DE")
    assert tab._debounce.isActive()
    tab._toggle_region("US", False)
    assert "US" not in host.settings.research_regions
    tab.category.setCurrentIndex(tab.category.findData("20"))
    tab.category.activated.emit(tab.category.currentIndex())
    assert host.settings.research_category == "20"
    tab._debounce.stop()


def test_auto_check_timer(app):
    tab, host = _make(app, youtube_api_key="k", research_auto=True, research_interval=30)
    assert tab.timer.isActive()  # no previous scan: checks shortly after startup
    assert tab.timer.remainingTime() <= 5000
    tab.auto.setChecked(False)
    assert not tab.timer.isActive() and host.settings.research_auto is False
    tab.shutdown()


def test_settings_dialog_has_a_research_page(app):
    from PySide6.QtWidgets import QTabWidget

    from ytclip.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Settings())
    tabs = dlg.findChild(QTabWidget)
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert "Research" in names
    dlg._changed("youtube_api_key", "  AIzaKEY ")
    assert dlg.result_settings.youtube_api_key == "AIzaKEY"


def test_errors_stay_visible_until_the_next_good_scan(app):
    tab, _ = _make(app, youtube_api_key="k")
    notices = []
    tab.notice.connect(notices.append)
    tab._scan_failed("Today's YouTube API quota is used up.")
    tab._update_summary()
    assert "quota is used up" in tab.summary.text() and "quota" in notices[0]
    tab._scan_failed("Cancelled")  # a cancel isn't an error
    assert len(notices) == 1
    tab._scan_done(_result(), auto=False)
    assert "quota" not in tab.summary.text() and "3 videos from US, GB" in tab.summary.text()


def test_filter_change_during_a_scan_checks_again(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab.job = object()  # pretend a scan is running
    tab.check()
    assert tab._again
    tab.job = None
    tab._job_finished()
    assert tab._debounce.isActive() and not tab._again
    tab._debounce.stop()


def test_pick_column_and_detail_explain_the_rules(app):
    tab, _ = _make(app, youtube_api_key="k", pick_from="any", pick_min_score=60)
    tab._scan_done(_result(), auto=False)
    from ytclip.gui.research import PICK_COLUMN

    picks = {tab._video_at(r).id: tab.table.item(r, PICK_COLUMN) for r in range(3)}
    assert picks["v0"].text() == "✓ Pick" and picks["v2"].text() == "✓ Pick"
    assert picks["v1"].text() == "–" and "Clip-ability 41 is under your minimum of 60" in picks["v1"].toolTip()
    assert "✓ Picked." in tab.d_pick.text() and "no permission on record" in tab.d_pick.text()
    assert tab.count.text() == "3 videos · 2 picked"
    assert tab.send_picks_btn.text() == "➜  Send 2 picks to Studio"


def test_default_rules_need_permission(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab._scan_done(_result(), auto=False)
    assert tab.picked_videos() == []
    assert "Not on your channel list" in tab.d_pick.text()
    assert tab.send_picks_btn.isHidden()


def test_picks_only_filter(app):
    tab, _ = _make(app, youtube_api_key="k", pick_from="any", pick_min_score=60)
    tab._scan_done(_result(), auto=False)
    tab.picks_only.setChecked(True)
    assert sorted(tab._video_at(r).id for r in range(3) if not tab.table.isRowHidden(r)) == ["v0", "v2"]


def test_sending_picks_records_them_and_counts_towards_the_limit(app):
    tab, host = _make(app, youtube_api_key="k", pick_from="any", pick_min_score=60, pick_daily=2)
    tab._scan_done(_result(), auto=False)
    sent = []
    tab.send.connect(sent.append)
    tab._send_picks()
    assert sorted(u.rsplit("=", 1)[-1] for u in sent[0]) == ["v0", "v2"]
    assert {p["id"]: p["how"] for p in tab.picks.picks} == {"v0": "studio", "v2": "studio"}
    assert tab.picked_videos() == []  # both done, and the daily limit of 2 is used up
    host.change("pick_min_score", 0)
    tab.settings_changed()
    v1 = next(d for d in tab.decisions.values() if d.video.id == "v1")
    assert v1.rule == "daily_cap"


def test_sending_an_unpicked_video_does_not_use_up_picks(app):
    tab, _ = _make(app, youtube_api_key="k", pick_from="any", pick_min_score=60)
    tab._scan_done(_result(), auto=False)
    row = next(r for r in range(3) if tab._video_at(r).id == "v1")
    tab.table.selectRow(row)
    tab._send_selected()
    assert tab.picks.picks == []


def test_rules_button_opens_the_picking_settings(app):
    tab, _ = _make(app, youtube_api_key="k")
    asked = []
    tab.open_settings.connect(asked.append)
    tab.rules_btn.click()
    assert asked == ["Picking"]


def test_settings_dialog_picking_page(app):
    from PySide6.QtWidgets import QListWidget, QTabWidget

    from ytclip.gui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(Settings(), tab="Picking")
    tabs = dlg.findChild(QTabWidget)
    assert tabs.tabText(tabs.currentIndex()) == "Picking"
    editor = dlg._editors["pick_block_categories"]
    assert isinstance(editor.widget, QListWidget)
    assert editor.get() == "1, 10"  # Film & Animation and Music, in list order
    music = next(editor.widget.item(i) for i in range(editor.widget.count())
                 if editor.widget.item(i).data(Qt.UserRole) == "10")
    music.setCheckState(Qt.Unchecked)
    assert dlg.result_settings.pick_block_categories == "1"


def test_a_sign_in_request_during_a_tab_check_pauses_automation_too(app):
    from ytclip.jobs import JobDB
    from ytclip.pipeline import paused_until

    tab, _ = _make(app, youtube_api_key="k")
    result = _result()
    result.blocked = "Sign in to confirm you're not a bot"
    logs = []
    tab.log.connect(logs.append)
    tab._scan_done(result, auto=True)
    import time
    assert paused_until(JobDB(), time.time()) and "carries on by itself" in logs[0]


def _found():
    from ytclip import lookup

    videos = []
    for i, (title, match, score) in enumerate([("MW4 funny moments", 90, 50), ("MW4 ranked", 60, 80),
                                               ("MW4 trolling", 85, 70)]):
        v = video_from_api(api_item(f"f{i}", title=title, hours_old=30))
        v.match, v.match_why, v.score, v.origins = match, f"why {i}", score, ["request"]
        v.reasons = [f"Match {match}: why {i}"]
        videos.append(v)
    plan = lookup.Plan("MW4 with good comedy", ["MW4 funny moments"], ["mw4"], ["funny"], "Funny MW4")
    return lookup.LookupResult("MW4 with good comedy", videos, NOW, plan, quota=201, weak=4)


def test_a_requests_results_show_by_match_until_back_to_trending(app):
    from ytclip.gui.research import MATCH_COLUMN

    tab, _ = _make(app, youtube_api_key="k")
    tab._show_result(_result())
    tab._find_done(_found())
    assert tab.mode == "find" and not tab.table.isColumnHidden(MATCH_COLUMN)
    order = [tab._video_at(r).title for r in range(tab.table.rowCount())]
    assert order == ["MW4 trolling", "MW4 funny moments", "MW4 ranked"]  # mostly match, then clip-ability
    text = tab.find_text.text()
    assert "3 videos" in text and "4 that didn't match" in text and "201 API units" in text
    assert "Your request" in tab.table.item(0, 3).text()
    tab._show_result(_result())  # a trending check finishing meanwhile doesn't replace them
    assert tab.mode == "find" and tab.table.rowCount() == 3
    tab.back_btn.click()
    assert tab.mode == "trending" and tab.table.isColumnHidden(MATCH_COLUMN)
    assert tab._video_at(0).title == "Big gaming moment"
    assert not tab.last_find_btn.isHidden()
    tab.last_find_btn.click()
    assert tab.mode == "find" and tab.find_box.text() == "MW4 with good comedy"


def test_a_request_is_remembered_for_next_time(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab._find_done(_found())
    again, _ = _make(app, youtube_api_key="k")
    assert again.found.request == "MW4 with good comedy" and again.mode == "trending"
    assert "MW4 with good comedy" in again._recent.model().stringList()


def test_find_needs_words_and_a_key(app):
    tab, _ = _make(app)
    notices = []
    tab.notice.connect(notices.append)
    tab.find()
    assert tab.job is None and "Call of Duty" in notices[-1]
    tab.find_box.setText("MW4 funny")
    tab.find()
    assert tab.job is None and not tab.key_panel.isHidden()


def test_queue_in_automation_sends_each_videos_niche(app):
    tab, _ = _make(app, youtube_api_key="k")
    tab._show_result(_result())
    got = []
    tab.queue.connect(got.append)
    tab.table.selectRow(0)
    tab.queue_btn.click()
    assert got == [[("https://www.youtube.com/watch?v=v0", "Big gaming moment", "")]]
