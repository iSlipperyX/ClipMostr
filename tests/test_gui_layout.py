import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)  # no Qt or no display libraries

from PySide6.QtWidgets import QInputDialog  # noqa: E402

from ytclip.gui.layout_tab import LayoutTab  # noqa: E402
from ytclip.settings import LAYOUT_PRESETS, Settings, load_user_layouts  # noqa: E402


def _tab(**overrides):
    tab = LayoutTab(Settings().apply(overrides), lambda name, value: None)
    picked = []
    tab.presetChosen.connect(lambda name, values: picked.append((name, values)))
    return tab, picked


def test_view_presets_are_listed_and_emitted(app):
    tab, picked = _tab()
    names = [tab.preset.itemData(i) for i in range(tab.preset.count())]
    assert all(n in names for n in LAYOUT_PRESETS)
    i = names.index("Gameplay above")
    tab._preset_picked(i)
    assert picked == [("Gameplay above", LAYOUT_PRESETS["Gameplay above"])]
    assert tab.preset.currentIndex() == 0  # the box goes back to its prompt
    assert not tab.delete_btn.isEnabled()  # nothing of the user's to delete yet


def test_save_and_delete_a_view(app, monkeypatch):
    tab, picked = _tab(banner_top=True, banner_top_text="WAIT", stack_enabled=True)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("My view", True))
    tab._save_preset()
    assert load_user_layouts()["My view"]["banner_top_text"] == "WAIT"
    names = [tab.preset.itemData(i) for i in range(tab.preset.count())]
    assert "My view" in names and tab.delete_btn.isEnabled()
    tab._preset_picked(names.index("My view"))
    assert picked[-1][0] == "My view" and picked[-1][1]["stack_enabled"] is True

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("My view", True))
    tab._delete_preset()
    assert "My view" not in load_user_layouts() and not tab.delete_btn.isEnabled()


def test_sketch_shows_the_tiktok_frame_without_a_layout(app):
    tab, _ = _tab()
    tab._redraw()
    assert tab.sketch.plan is not None and (tab.sketch.plan.width, tab.sketch.plan.height) == (1080, 1920)
    assert tab.sketch.plan.safe is not None
