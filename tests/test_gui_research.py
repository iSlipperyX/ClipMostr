import pytest
from conftest import NOW, api_item

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ytclip.research import ScanResult  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from ytclip.trending import DeepInfo, video_from_api  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


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
