"""The Research command center: the opportunity matrix, category pills, Live arbitrage, the new table columns
and the Most replayed inspector (gui/research.py, gui/research_dashboard.py)."""

import pytest
from conftest import NOW, api_item

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtCore import Qt  # noqa: E402

from ytclip.research import ScanResult  # noqa: E402
from ytclip.settings import Settings  # noqa: E402
from ytclip.trending import DeepInfo, video_from_api  # noqa: E402

H = 3600.0
CATEGORIES = [{"id": "g", "name": "Gaming clips", "account_id": "acc1", "keywords": "gaming"}]


class Host:
    def __init__(self, **overrides):
        self.settings = Settings().apply({"research_auto": False, "youtube_api_key": "k"} | overrides)

    def get(self):
        return self.settings

    def change(self, name, value):
        self.settings = self.settings.apply({name: value})


def heatmap(duration, peaks=(0.5,)):
    out = []
    for k in range(100):
        x = (k + 0.5) / 100
        value = 0.2 + sum(0.8 for p in peaks if abs(x - p) < 0.012)
        out.append({"start_time": duration * k / 100, "end_time": duration * (k + 1) / 100, "value": value})
    return out


def _videos():
    rows = [("v0", "Big gaming moment", "20", 50_000), ("v1", "Another gaming clip", "20", 3_000),
            ("v2", "New single official video", "10", 400_000), ("v3", "Acoustic cover", "10", 2_000),
            ("v4", "Breaking: studio shuts down", "25", 90_000)]
    out = []
    for vid, title, cat, speed in rows:
        v = video_from_api(api_item(vid, title=title, category=cat, hours_old=6, minutes=20))
        v.published = NOW - 6 * H
        v.velocity, v.score = speed, 60
        out.append(v)
    out[0].deep = DeepInfo(heatmap=heatmap(1200, (0.4, 0.7)), checked=NOW)
    return out


def _make(app, monkeypatch, view="matrix", **overrides):
    import ytclip.analytics as analytics
    from ytclip.gui.research import Research

    real = analytics.insights
    monkeypatch.setattr(analytics, "insights", lambda videos, names, history=None, now=None:
                        real(videos, names, history, NOW))
    host = Host(**({"categories": CATEGORIES, "research_view": view} | overrides))
    tab = Research(host.get, host.change)
    tab._show_result(ScanResult(_videos(), NOW, ["US"]))
    tab.refresh_matrix()
    return tab, host


def visible(tab):
    return [tab._video_at(r).id for r in range(tab.table.rowCount()) if not tab.table.isRowHidden(r)]


def test_the_matrix_has_a_card_per_cluster_yours_first(app, monkeypatch):
    tab, _ = _make(app, monkeypatch)
    assert tab.view == "matrix" and tab.stack.currentIndex() == 0
    names = [c.name for c in tab.matrix.cards]
    assert names[0] == "Gaming clips" and set(names[1:]) == {"Music", "News & Politics"}
    gaming = tab.matrix.cards[0]
    assert [r.video_id for r in gaming.rows] == sorted([r.video_id for r in gaming.rows],
                                                       key=lambda i: -tab.analysis[i].viral)
    assert {x for x, _ in tab.matrix.heatmap.rows} == set(names)
    music = next(c for c in tab.matrix.cards if c.name == "Music")
    from PySide6.QtWidgets import QLabel

    assert any("not one of your categories" in lab.text() for lab in music.findChildren(QLabel))
    assert not any("not one of your categories" in lab.text() for lab in gaming.findChildren(QLabel))
    music.rows[0].clicked.emit("v2")
    assert tab._shown.id == "v2"
    assert music.property("selected") is True and gaming.property("selected") is False


def test_a_card_shows_its_cluster_in_the_table(app, monkeypatch):
    tab, host = _make(app, monkeypatch)
    tab.show_cluster("Music")
    assert tab.view == "table" and host.settings.research_view == "table"
    assert sorted(visible(tab)) == ["v2", "v3"]
    assert not tab.pills.chip.isHidden() and "Music" in tab.pills.chip.text()
    assert tab._shown.id in ("v2", "v3")  # the inspector follows the filter
    tab.pills.chip.click()  # ✕
    assert tab.cluster_filter is None and len(visible(tab)) == 5
    tab.show_cluster("Gaming clips")  # one of your categories: its pill instead
    assert tab.cluster_filter is None and tab.pills.current() == "g"
    assert sorted(visible(tab)) == ["v0", "v1"]


def test_pills_filter_by_category_with_counts(app, monkeypatch):
    tab, _ = _make(app, monkeypatch, view="table")
    assert tab.pills.labels() == ["All  5", "Gaming clips  2", "Fits none  3"]
    tab.pills.buttons[2].click()
    assert sorted(visible(tab)) == ["v2", "v3", "v4"] and tab.category_filter.currentData() == ""
    tab.pills.buttons[0].click()
    assert len(visible(tab)) == 5


def test_without_categories_there_are_no_pills(app, monkeypatch):
    tab, _ = _make(app, monkeypatch, categories=[])
    assert tab.pills.isHidden() and tab.pills.labels() == []
    tab.show_cluster("Music")
    assert not tab.pills.isHidden()  # just the cluster's ✕


def test_live_arbitrage_lists_what_to_race_for(app, monkeypatch):
    tab, host = _make(app, monkeypatch)
    racing = {k for k, i in tab.analysis.items() if i.arbitrage}
    assert racing == {"v2"}  # the fastest fifth of five
    assert "1" in tab.views.buttons["arbitrage"].text()
    tab.views.buttons["arbitrage"].click()
    assert tab.view == "arbitrage" and host.settings.research_view == "arbitrage"
    assert visible(tab) == ["v2"] and not tab.banner.isHidden()
    assert "1 video to race for" in tab.banner_text.text() and "can't search TikTok" in tab.banner_text.text()
    assert tab.count.text() == "1 to race for, of 5 videos"
    assert tab._shown.id == "v2"
    tab.set_view("table")
    assert len(visible(tab)) == 5 and tab.banner.isHidden()


def test_the_new_columns(app, monkeypatch):
    from ytclip.gui.research import LIFESPAN_COLUMN, MOMENTUM_COLUMN, VIRAL_COLUMN

    tab, _ = _make(app, monkeypatch, view="table")
    row = next(r for r in range(5) if tab._video_at(r).id == "v4")
    assert tab.table.item(row, VIRAL_COLUMN).text() == f"{tab.analysis['v4'].viral:.0f}"
    assert "Velocity (35%)" in tab.table.item(row, VIRAL_COLUMN).toolTip()
    assert tab.table.item(row, MOMENTUM_COLUMN).text() == "Measuring"
    assert tab.table.item(row, LIFESPAN_COLUMN).text() == "Ephemeral"  # "Breaking:"
    head = tab.table.horizontalHeader()
    assert head.visualIndex(VIRAL_COLUMN) == 2 and head.visualIndex(MOMENTUM_COLUMN) == 8
    tab.table.sortItems(VIRAL_COLUMN, Qt.DescendingOrder)
    vir = [tab.analysis[tab._video_at(r).id].viral for r in range(5)]
    assert vir == sorted(vir, reverse=True)


def test_columns_can_be_hidden_and_stay_hidden(app, monkeypatch):
    from ytclip.gui.research import COLUMNS, Research

    tab, host = _make(app, monkeypatch, view="table")
    channel = COLUMNS.index("Channel")
    tab.show_column("Channel", False)
    assert tab.table.isColumnHidden(channel) and host.settings.research_hidden_columns == "Channel"
    again = Research(host.get, host.change)
    assert again.table.isColumnHidden(channel) and again.view == "table"
    tab.show_column("Channel", True)
    assert not tab.table.isColumnHidden(channel) and host.settings.research_hidden_columns == ""
    tab._fit_title()
    assert tab.table.horizontalHeader().sectionSize(COLUMNS.index("Video")) >= 230


def test_the_inspector_reads_the_most_replayed_graph(app, monkeypatch):
    tab, _ = _make(app, monkeypatch)
    tab.select_video("v0")
    assert len(tab.replay.windows) == 2 and tab.replay.drops
    assert "Likely clips" in tab.d_windows.text() and not tab.d_windows.isHidden()
    assert tab.viral_grid.count() == 4 * 3
    assert tab.d_viral.text() == f"{tab.analysis['v0'].viral:.0f}"
    assert "measuring" in tab.d_momentum.text().lower() and "Evergreen" in tab.d_life.text()
    assert "<" not in tab.d_life.text().split("<b>")[1].split("</b>")[0]  # "Ephemeral (< 48 h)" is escaped
    tab.select_video("v1")  # no graph read yet
    assert tab.replay.windows == [] and "Check replays" in tab.replay.message and tab.d_windows.isHidden()
    tab.replay.resize(380, 168)
    tab.replay.grab()  # paints without the graph


def test_charts_paint_and_describe(app):
    from ytclip import analytics
    from ytclip.gui.research_dashboard import MomentumHeatmap, ReplayGraph, Sparkline

    g = ReplayGraph()
    v = _videos()[0]
    g.set_video(v)
    g.resize(400, 168)
    g.grab()
    w = g.windows[0]
    assert "Likely clip" in g.describe(w.peak) and "% of the top" in g.describe(5)
    assert "Likely clips" in g.summary()

    values = [None] * 20 + [1000.0, 2000.0, 1500.0, 3000.0]
    s = Sparkline()
    s.set_values(values, NOW)
    s.resize(260, 36)
    s.grab()
    assert "3K views an hour" in s.describe(23) and "No checks" in s.describe(0)

    h = MomentumHeatmap()
    h.set_rows([("Gaming", values), ("Music", [None] * analytics.HOURS)], NOW)
    h.resize(700, h.height())
    h.grab()
    assert h.has_data() and h.height() == 2 * (h.ROW_H + h.GAP) + h.AXIS_H + 2
    x0, cell, n = h._cells()
    assert n == 24 and h.cell_at(x0 + 23 * (cell + h.GAP) + 1, 2) == (0, 23)
    assert "3K views an hour" in h.describe(0, 23) and "No two checks" in h.describe(1, 0)
    picked = []
    h.picked.connect(picked.append)
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    h.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, QPointF(10, h.ROW_H + h.GAP + 2), QPointF(),
                                  Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    assert picked == ["Music"]


def test_a_heatmap_row_click_shows_that_cluster(app, monkeypatch):
    tab, _ = _make(app, monkeypatch)
    tab.matrix.heatmap.picked.emit("News & Politics")
    assert tab.view == "table" and visible(tab) == ["v4"]


def test_performance_marks_posts_far_above_the_accounts_usual():
    from ytclip.gui.posting_pages import outlier_views
    from ytclip.posting.store import TikTokVideo

    vids = [TikTokVideo(id=f"t{i}", create_time=NOW - 3 * 86400, views=1000 + 50 * i) for i in range(6)]
    vids.append(TikTokVideo(id="big", create_time=NOW - 2 * 86400, views=50_000))
    vids.append(TikTokVideo(id="new", create_time=NOW - 3600, views=90_000))  # under a day old: not compared yet
    usual, standouts = outlier_views(vids, NOW)
    assert standouts == {"big"} and usual == 1150
    assert outlier_views(vids[:3], NOW) == (1050, set())  # too few to call anything an outlier
    assert outlier_views([], NOW) == (0.0, set())
