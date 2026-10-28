"""GUI walk-through: screenshots of what's new, on a real profile (a soak test's, for example).

The app opens offscreen with the profile given, a media lab video is clipped in the Studio (so
the clip list shows a part series), and each new page is saved as a PNG:

    QT_QPA_PLATFORM=offscreen python tests/gui_tour/tour.py PROFILE_DIR LAB_VIDEO.mp4 OUT_DIR
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    profile, video, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(sys.argv[3]).resolve()
    os.environ["APPDATA"] = str(profile / "roaming")
    os.environ["LOCALAPPDATA"] = str(profile / "local")
    out.mkdir(parents=True, exist_ok=True)
    from PySide6.QtWidgets import QApplication

    from ytclip.gui import theme

    app = QApplication(sys.argv)
    theme.apply(app)
    from ytclip.engine import Engine, Project
    from ytclip.gui.main_window import MainWindow
    from ytclip.gui.requests_panel import RequestDialog

    mw = MainWindow()
    mw.resize(1440, 900)
    mw.show()
    shots: list[str] = []

    def settle(n: int = 30) -> None:
        for _ in range(n):
            app.processEvents()

    def shot(widget, name: str) -> None:
        settle()
        path = out / f"{len(shots) + 1:02d}-{name}.png"
        widget.grab().save(str(path))
        shots.append(path.name)

    # The Studio, with a video whose long moment comes out as a part series
    s = mw.settings.copy(output_dir=str(out / "studio"), cache_dir=str(out / "studio" / ".cache"), ai_polish=False)
    p = Project(str(video))
    engine = Engine(s)
    engine.prepare(p)
    mw.projects.append(p)
    mw.load_project(p)
    shot(mw, "studio-clip-list-with-parts")

    auto = mw.automation
    mw.tabs.setCurrentWidget(auto)
    for i in range(auto.pages.count()):
        auto.pages.setCurrentIndex(i)
        auto.refresh()
        shot(mw, "automation-" + auto.pages.tabText(i).lower().replace(" ", "-"))
    dialog = RequestDialog(mw.settings)
    dialog.resize(560, dialog.sizeHint().height())
    dialog.show()
    shot(dialog, "new-request-dialog")
    dialog.close()
    mw.tabs.setCurrentWidget(mw.niches)
    if mw.niches.list.count():
        mw.niches.list.setCurrentRow(0)
    shot(mw, "niches-form")
    print("\n".join(shots))
    mw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
