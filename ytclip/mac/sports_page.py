"""Sports, on the Mac: the linked PC's Sports section (gui/sports_view.py), the same page the PC shows.

The filter, the games found, each game's ranked highlights and Make clips are all the PC's (sportsdesk/ on the
PC, through its /api/sports): the PC searches YouTube, downloads and listens to each game, and its AI (Claude,
Gemini or its local AI, as its Settings → AI says) ranks the plays, so nothing heavy runs on the Mac. The clips
are made for a category on the PC; a sports category sends them to this Mac's Sports clips (inbox.py) by itself.
Open in Studio brings a game's ticked plays into this Mac's own Studio instead, to edit and export here.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..gui.sports_view import Backend, SportsView
from .client import ApiError

OLD_PC = ("The ClipMonster on your PC is older than this Mac's and has no Sports section yet. Update it on the PC "
          "(it updates itself overnight, or Settings → General → Check for updates now).")


class RemoteBackend(Backend):
    """The linked PC's desk, over the link (each call on the session's worker threads)."""

    local = False

    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link

    @property
    def where(self) -> str:  # type: ignore[override]
        return "your PC"

    def available(self) -> bool:
        return self.link.session is not None

    def call(self, name: str, *args, ok=None, fail=None) -> None:
        s = self.link.session
        if s is None:
            if fail is not None:
                from .client import Offline

                fail(Offline("No PC is linked."))
            return
        request = getattr(self, f"_{name}")

        def run(client):
            try:
                return request(client, *args)
            except ApiError as exc:
                if exc.status == 404 and exc.message in ("No such request.", "The PC said 404."):
                    raise ApiError(404, OLD_PC) from exc
                raise

        s.call(run, ok, fail)

    def picture(self, game_id: str, highlight_id: str) -> bytes | None:
        client = self.link.client()
        if client is None:
            return None
        try:
            return client.fetch(f"/api/sports/thumbs/{game_id}/{highlight_id}", limit=2_000_000)
        except Exception:  # noqa: BLE001 - a missing picture leaves a blank box
            return None

    # --- the desk's methods, as requests --------------------------------------------------------------------

    @staticmethod
    def _state(c, rev: str = ""):
        return c.get("/api/sports", rev=rev or None)

    @staticmethod
    def _set_filter(c, f: dict):
        return c.post("/api/sports/filter", {"filter": f})

    @staticmethod
    def _find_games(c, f: dict | None = None):
        return c.post("/api/sports/find", {"filter": f} if f else {})

    @staticmethod
    def _pick(c, ids: list, picked: bool = True):
        return c.post("/api/sports/pick", {"ids": list(ids), "picked": bool(picked)})

    @staticmethod
    def _analyse(c, ids: list, again: bool = False):
        return c.post("/api/sports/analyse", {"ids": list(ids), "again": bool(again)})

    @staticmethod
    def _choose(c, game_id: str, ids: list, chosen: bool = True):
        return c.post(f"/api/sports/games/{game_id}/choose", {"ids": list(ids), "chosen": bool(chosen)})

    @staticmethod
    def _make(c, game_id: str, ids: list | None = None, category: str | None = None):
        body: dict = {"ids": list(ids or [])}
        if category is not None:
            body["category"] = category
        return c.post(f"/api/sports/games/{game_id}/make", body)

    @staticmethod
    def _version(c, game_id: str, video: str):
        return c.post(f"/api/sports/games/{game_id}/version", {"video": video})

    @staticmethod
    def _remove(c, ids: list):
        return c.post("/api/sports/remove", {"ids": list(ids)})

    @staticmethod
    def _clear(c):
        return c.post("/api/sports/clear", {})

    @staticmethod
    def _cancel(c, game_id: str):
        return c.post("/api/sports/cancel" if game_id == "search" else f"/api/sports/games/{game_id}/cancel", {})


class SportsPage(QWidget):
    """The Sports row under Your PC."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ = window
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 16, 24, 16)
        self.backend = RemoteBackend(window, self)
        self.view = SportsView(self.backend)
        self.view.notice.connect(lambda text: window.notice(text))
        v.addWidget(self.view)

    def load(self) -> None:
        self.view.refresh(force=True)

    def news(self, e: dict) -> None:
        """The PC's news about Sports (a game's highlights are ready, clips were made): show it now."""
        if self.isVisible():
            self.view.refresh(force=True)

    def shutdown(self) -> None:
        self.view.shutdown()
