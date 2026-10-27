import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    """Settings, research history and caches go to a temp folder, never the real profile."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return tmp_path


NOW = 1_800_000_000.0  # a fixed "now" so ages and velocities are exact


def api_item(vid="abc", title="A video", views=100_000, likes=5_000, comments=400, minutes=20.0,
             hours_old=10.0, category="20", live="none", caption=False, channel="Chan", license="youtube",
             language=""):
    """One item as videos.list returns it (chart=mostPopular, or by id)."""
    from datetime import datetime, timezone

    published = datetime.fromtimestamp(NOW - hours_old * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = int(minutes * 60)
    stats = {"viewCount": str(views)}
    if likes is not None:
        stats["likeCount"] = str(likes)
    if comments is not None:
        stats["commentCount"] = str(comments)
    item = {
        "id": vid,
        "snippet": {"title": title, "channelTitle": channel, "channelId": "UC" + channel, "publishedAt": published,
                    "categoryId": category, "liveBroadcastContent": live,
                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}}},
        "statistics": stats,
        "contentDetails": {"duration": f"PT{total // 3600}H{total % 3600 // 60}M{total % 60}S",
                           "caption": "true" if caption else "false"},
        "status": {"license": license},
    }
    if language:
        item["snippet"]["defaultAudioLanguage"] = language
    return item


def spiky_heatmap(duration=1200, peaks=(300, 700, 1000), base=0.15, segments=100):
    """A Most-replayed heatmap: low everywhere except sharp peaks at the given seconds."""
    seg = duration / segments
    out = []
    for i in range(segments):
        mid = (i + 0.5) * seg
        value = base + max((max(0.0, 1 - abs(mid - p) / 25) for p in peaks), default=0) * (1 - base)
        out.append({"start_time": i * seg, "end_time": (i + 1) * seg, "value": value})
    return out


@pytest.fixture(scope="session")
def app():
    """One QApplication for the whole run. A module-scoped app was freed between test files while
    widgets from the earlier file were still alive, which crashed Qt at exit."""
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from PySide6.QtCore import QCoreApplication, QEvent

    qapp = widgets.QApplication.instance() or widgets.QApplication([])
    yield qapp
    for w in qapp.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    import gc

    gc.collect()
