"""The Sports section: pick the games to clip, see each game's best highlights ranked, and make the ones you want.

* games.py  - which games: the filter (sport, league, teams, when, what footage), the YouTube searches it makes
              (with the YouTube Data API key, else yt-dlp's search), and reading each result's title into a game
              ("Lakers vs Celtics, full game"), with the AI when one is set up, else by its words.
* rank.py   - each game's best highlights: the whole plays sports.py finds by the crowd and the commentary, then
              rated by the AI (Claude, Gemini, the local AI, or on a Mac the PC's), which names the play, writes a
              title and spots a replay of a play it already has. Without an AI the signals rank them.
* desk.py   - the section's state on this PC (sports.db: the filter, the games and their highlights, the work
              asked for), and the worker that does the work, one thing at a time: in automation while it runs
              (so a linked Mac's asks are done too), else in the window.

The window's page is gui/sports_view.py; ClipMonster for Mac shows the same page for its linked PC
(mac/sports_page.py), through the PC's /api/sports.
"""
