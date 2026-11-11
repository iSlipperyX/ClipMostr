"""Sports from Telegram: a game's recap reel in one tap, and a moment found by saying it.

* /games lists the games of the last 24 hours by league (sportsdesk/slate.py reads the scoreboards); a tap on
  one asks for its recap reel. /reel Lakers Celtics and /sports NBA Lakers vs Celtics say the game in words.
  Each is a sports order (sportsdesk/desk.py): automation looks for the game's highlights, picks its best
  plays and makes the reel with nobody picking along the way, and what's made is checked against the
  minimums before it's handed over (compliance/requirements.py: over a minute, upright, with sound).
* /find Mahomes 4th quarter touchdown (or /search, or just "find the red card in Arsenal Chelsea") searches a
  game for a moment said in words (sportsdesk/semantic_search.py: the commentary, the play-by-play and the
  crowd, on the PC). The game is the one the words name: one already on the Sports page, one from the last
  24 hours' scoreboards, or teams said outright; when none is named, the games are offered as buttons. What's
  found comes back as cards: when in the video, how sure, and what the commentator said, each with
  🎬 Clip Play (the moment as a clip of its own) and ➕ Add to Game Reel (set aside, then 🎞 makes one reel
  of everything set aside for that game).

A single play is 15 to 40 seconds, under the minute a video from Telegram has to be, so a clipped moment
waits for an OK instead of posting by itself; the card says so before the button is pressed.

Everything asked for carries the name of who asked, and the other person sharing the bot is told.
"""

from __future__ import annotations

import re

from . import state
from .api import ApiError, inline
from .common import MODE_LIFE, Refused, esc, plural, trim

FIND_HELP = ("Say the moment, and the game if you know it: <i>/find Mahomes 4th quarter touchdown</i>, "
             "<i>/find red card in Arsenal Chelsea</i>, <i>/find buzzer beater in Lakers Celtics</i>.")
MOMENT_CARDS = 3  # found moments sent as a card each; the rest wait behind one "show more" button
GAME_CHOICES = 8  # games offered when a search names none
_FIND_SAID = re.compile(r"^(?:please\s+)?(?:find|search(?:\s+for)?|look\s+for)\s+(?:me\s+)?(?P<q>.{3,})$",
                        re.I | re.S)
_IN_GAME = re.compile(r"^(?P<q>.+?)\s+(?:in|from|during)\s+(?:the\s+)?(?P<g>[^:]+?)"
                      r"(?:\s+(?:game|match|fight|race|highlights))?$", re.I)

SPORT_WORDS = {"hoops": "basketball", "ice hockey": "hockey", "mma": "combat", "fights": "combat",
               "racing": "motorsport", "american football": "football"}
LEAGUE_WORDS = {"f1": ("Formula 1", "motorsport"), "epl": ("Premier League", "soccer"),
                "ucl": ("Champions League", "soccer"), "laliga": ("La Liga", "soccer")}
SPORTS_FILLER = re.compile(r"\b(?:highlights?|best|top|of|the|and|a|an|from|for|with|only|just|please|games?|"
                           r"match(?:es)?|fights?|races?|clips?|plays?|reels?|recaps?|montages?|both|newest|latest|"
                           r"last|this|week|month|today|tonight|night|yesterday|me|make|get|want|i)\b", re.I)
SPORTS_HELP = ("Say the game like <i>NBA Lakers vs Celtics</i>, <i>Premier League 2 games</i> or <i>UFC</i>: the "
               "league or sport, and the teams if you have them. You get a recap reel (65-85 s); add "
               "<i>3 clips</i> for single plays too.")
GAMES_SHOWN = 10  # games listed with a button each; more than this and the leagues are offered first


def parse_sports(text: str) -> dict:
    """'NBA Lakers vs Celtics 3 clips' → what sportsdesk's Desk.order takes: {sport, league, teams, since, want,
    games, plays}. ValueError (in words for the person) when it names no sport, league or team."""
    from .. import sports
    from ..sportsdesk import games as G

    rest = " " + " ".join(str(text or "").replace("’", "'").split()) + " "
    out: dict = {"want": "reel"}

    def take(pattern: str) -> re.Match | None:
        nonlocal rest
        m = re.search(pattern, rest, re.I)
        if m:
            rest = rest[:m.start()] + " " + rest[m.end():]
        return m

    for sport_id, names in G.LEAGUES.items():
        for name in sorted(names, key=len, reverse=True):
            if "league" not in out and take(rf"(?<![\w-]){re.escape(name)}(?![\w-])"):
                out["league"], out["sport"] = name, sport_id
    for word, (name, sport_id) in LEAGUE_WORDS.items():
        if "league" not in out and take(rf"(?<![\w-]){re.escape(word)}(?![\w-])"):
            out["league"], out["sport"] = name, sport_id
    words = {sp.id: sp.id for sp in sports.SPORTS.values() if sp.id != "any"} | SPORT_WORDS
    for word in sorted(words, key=len, reverse=True):
        if take(rf"(?<![\w-]){re.escape(word)}(?![\w-])"):
            out.setdefault("sport", words[word])
    m = take(r"\b(\d{1,2})\s*(?:clips?|plays?)\b")
    if m:
        out["plays"] = int(m.group(1))
    m = take(r"\b(\d{1,2})\s*(?:games?|match(?:es)?|fights?|races?)\b")
    if m:
        out["games"] = int(m.group(1))
    low = rest.lower()
    # A recap reel unless single plays are asked for: a reel is over a minute, a play is 15-40 seconds.
    reel = bool(re.search(r"\b(?:reels?|recaps?|montages?)\b", low))
    clips = "plays" in out or bool(re.search(r"\b(?:clips?|plays?)\b", low))
    both = (reel and clips) or bool(re.search(r"\bboth\b", low))
    out["want"] = "both" if both else "clips" if clips else "reel"
    out["since"] = ("3days" if re.search(r"\b(?:today|tonight|last night|yesterday)\b", low) else
                    "month" if re.search(r"\bmonth\b", low) else "week")
    teams = re.sub(r"\s+(?:vs?\.?|versus|at|@)\s+", " , ", " " + SPORTS_FILLER.sub(" ", rest) + " ", flags=re.I)
    teams = ", ".join(t for t in (" ".join(x.split()).strip(" .!?:-") for x in re.split(r"[,;]+", teams)) if t)
    if teams:
        out["teams"] = teams
    if not (out.get("sport") or out.get("league") or out.get("teams")):
        raise ValueError("Which game? " + SPORTS_HELP)
    return out


def clock(seconds) -> str:
    """'1:02:14', '12:05': a place in a video."""
    s = max(0, int(round(float(seconds or 0))))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def split_find(text: str) -> tuple[str, str]:
    """'red card in Arsenal Chelsea' → ('red card', 'Arsenal Chelsea'); 'Lakers Celtics: LeBron dunk' →
    ('LeBron dunk', 'Lakers Celtics'). ('…', '') when no game is set apart from the moment."""
    text = " ".join(str(text or "").split())
    if ":" in text:
        game, _, q = text.partition(":")
        if len(q.strip()) >= 3 and 0 < len(game.split()) <= 6 and not re.fullmatch(r"\s*\d+\s*", game):
            return q.strip(), game.strip()
    m = _IN_GAME.match(text)
    if m and not re.match(r"(?:the\s+)?(?:\d|first|second|third|fourth|last|final|1st|2nd|3rd|4th|over\s?time|"
                          r"extra|stoppage|injury|added)\b", m.group("g"), re.I):  # "in the 4th quarter" is no game
        return m.group("q").strip(), m.group("g").strip()
    return text, ""


def sides_named(text: str, sides: list[list[str]]) -> int:
    """How many of a game's sides `text` names (by any name each goes by, or its last word: "Lakers")."""
    from ..sportsdesk import games as G

    n = 0
    for names in sides:
        names = [x for x in names if x and str(x).strip()]
        short = [x.split()[-1] for x in names if len(x.split()) > 1 and len(x.split()[-1]) > 3]
        if G.mentions(text, names + short):
            n += 1
    return n


def moment_html(m: dict, matchup: str = "") -> str:
    """A found moment as a card: what, when in the video, how sure, and what was said."""
    length = max(0.0, float(m.get("end") or 0) - float(m.get("start") or 0))
    head = f"🎯 <b>{esc(m.get('label') or 'Moment')}</b>" + (f" · {esc(trim(m['badge'], 60))}" if m.get("badge") else "")
    lines = [head]
    if matchup:
        lines.append(esc(trim(matchup, 80)))
    lines.append(f"⏱ {clock(m.get('t'))} into the video · the clip is {clock(m.get('start'))}–{clock(m.get('end'))} "
                 f"({length:.0f} s) · {int(m.get('score') or 0)}% sure")
    if m.get("quote"):
        lines.append(f"💬 <i>“{esc(trim(m['quote'], 220))}”</i>")
    if m.get("why") and not (m.get("quote") and str(m["quote"])[:40] in str(m["why"])):  # not the quote twice
        lines.append(esc(trim(m["why"], 180)))
    if not m.get("certain", True):
        lines.append("⚠ It's talked about here, and may not be the moment itself: watch it before posting.")
    return "\n".join(lines)


class SportsHandler:
    """The bot's sports side (mixed into bot.Bot, which gives it say, did, name, settings, lock and the rest)."""

    @property
    def desk(self):
        """The Sports section's state (the same sports.db the PC's window shows)."""
        if self._desk is None:
            from ..sportsdesk.desk import Desk

            self._desk = Desk(settings=self.settings, now=self.now)
        return self._desk

    def sports_lines(self) -> list[str]:
        """What the Sports worker is on right now, for /status: the game, the step and how far."""
        try:
            d = self.desk.doing()
            waiting = self.desk.waiting() if d is None else d["waiting"]
        except Exception:  # noqa: BLE001 - sports.db busy for a moment: the rest of the status still helps
            return []
        if d is None:
            return [f"🏆 {plural(waiting, 'sports job')} waiting to start"] if waiting else []
        how = d["stage"] + (f" · {round(100 * d['frac'])}%" if d["frac"] is not None and "%" not in d["stage"] else "")
        line = f"{d['emoji']} {esc(d['what'])} “{esc(trim(d['title'], 70))}”" + (f" ({esc(d['by'])} asked)"
                                                                               if d["by"] else "")
        if how:
            line += f"\n    {esc(trim(how, 120))}"
        if d["waiting"]:
            line += f"\n    {plural(d['waiting'], 'more sports job')} waiting"
        return [line]

    def cmd_sports(self, chat: int, text: str = "") -> None:
        text = text.strip()
        if text.lower() in ("cancel", "stop"):
            mine = [o for o in self.desk.orders(40) if o["status"] in ("queued", "searching", "working")]
            o = next((o for o in mine if o.get("by_id") == chat), mine[0] if mine else None)
            if o is None:
                self.say(chat, "No sports request is waiting or under way.")
                return
            try:
                self.say(chat, "✖ " + esc(self.sports_cancel(chat, o["id"])["message"]))
            except Refused as exc:
                self.say(chat, f"⚠ {esc(exc)}")
            return
        if not text:
            self.modes[chat] = ("sports", self.now() + MODE_LIFE)
            going = [o for o in self.desk.orders(12) if o["status"] in ("queued", "searching", "working")]
            lines = ["🏆 <b>Which game?</b> " + SPORTS_HELP, "",
                     "I look for the game's highlights on YouTube, pick its best plays and make them into a recap "
                     "reel on the PC, then check it (over a minute, upright, with sound) before it's handed over. "
                     "/games lists the last 24 hours' games to tap. League footage often gets copyright claims on "
                     "TikTok."]
            if going:
                lines += ["", "<b>Under way</b>"] + [f"• {esc(o['title'])} ({esc(o['by'] or 'someone')}): "
                                                    f"{esc(o['doing'] or 'waiting')}" for o in going]
                lines.append("/sports cancel stops the newest of yours.")
            self.say(chat, "\n".join(lines))
            return
        try:
            r = self.sports_order(chat, parse_sports(text))
        except ValueError as exc:
            self.modes[chat] = ("sports", self.now() + MODE_LIFE)
            self.say(chat, f"⚠ {exc}")
            return
        except Refused as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return
        self.say(chat, "🏆 " + r["html"], self.order_buttons(r["order"]))

    def order_buttons(self, o: dict) -> dict:
        rows = [[("✖ Cancel it", f"so:{o['id']}")]]
        if self.app_url:
            rows.insert(0, [self.app_row("📱 Follow it in the app")])
        return inline(rows)

    def cmd_reel(self, chat: int, text: str = "") -> None:
        """/reel Lakers Celtics: a recap reel and nothing else; /reel alone lists the games to tap."""
        if not text.strip():
            self.cmd_games(chat, "")
            return
        try:
            r = self.sports_order(chat, {**parse_sports(text), "want": "reel"})
        except ValueError as exc:
            self.say(chat, f"⚠ {exc}")
            return
        except Refused as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return
        self.say(chat, "🏆 " + r["html"], self.order_buttons(r["order"]))

    def played(self, sport: str = "any", league: str = "") -> dict:
        """The games of the last 24 hours from the scoreboards (sportsdesk/slate.py)."""
        from ..sportsdesk import slate

        return slate.played(sport, league, now=self.now(), get=self.slate_get, deadline=10.0)

    def cmd_games(self, chat: int, text: str = "") -> None:
        sport, league = "any", ""
        if text.strip():
            try:
                said = parse_sports(text)
                sport, league = said.get("sport") or "any", said.get("league") or ""
            except ValueError:
                pass
        text_, buttons = self.games_message(sport, league)
        self.say(chat, text_, buttons)

    def games_message(self, sport: str = "any", league: str = "") -> tuple[str, dict | None]:
        from ..sportsdesk import slate

        if sport != "any" and not slate.covers(sport):
            return ("🏆 The scoreboards I read list soccer, basketball, American football, hockey and baseball. For "
                    "anything else, say the event: " + SPORTS_HELP), None
        out = self.played(sport, league)
        games = [g for g in out["games"] if g["state"] != "soon"]
        soon = [g for g in out["games"] if g["state"] == "soon"]
        if not games and not soon:
            why = f"\n⚠ {esc('; '.join(out['problems'][:2]))}" if out["problems"] else ""
            return (f"🏆 No {esc(league or '')} games in the last 24 hours on the scoreboards.{why}\n"
                    "/sports asks for an older one in words.").replace("  ", " "), None
        now = self.now()
        if len(out["leagues"]) > 1 and len(games) > GAMES_SHOWN:  # too many to list: the leagues first
            lines = [f"🏆 <b>Games played, last 24 hours</b> ({len(games)})", ""]
            lines += [f"{r['emoji']} {esc(r['league'])}: {plural(r['games'], 'game')}"
                      + (f", {r['live']} being played" if r["live"] else "") for r in out["leagues"] if r["games"]]
            lines += ["", "Pick a league."]
            rows, row = [], []
            for r in (r for r in out["leagues"] if r["games"]):
                code = next(c for _sp, c, name in slate.LEAGUES if name == r["league"])
                row.append((f"{r['emoji']} {trim(r['league'], 22)} ({r['games']})", f"gl:{code}"))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            return "\n".join(lines), inline(rows)
        lines = ["🏆 <b>Games played, last 24 hours</b>" + (f": {esc(league)}" if league else "")]
        last = None
        rows = []
        for g in games[:24]:
            if g["league"] != last and not league:
                lines.append(f"\n{g['emoji']} <b>{esc(g['league'])}</b>")
                last = g["league"]
            lines.append(f"• {esc(g['line'])} · {esc(slate.when(g, now))}")
            if g["state"] == "final":
                rows.append([(f"🎬 {trim(g['label'], 40)}", f"gg:{g['id']}")])
        if len(games) > 24:
            lines.append(f"… and {len(games) - 24} more: /games with a league shows them.")
        if soon:
            lines.append("\n<i>Still to come: " + esc("; ".join(f"{g['label']} ({slate.when(g, now)})"
                                                              for g in soon[:4])) + "</i>")
        lines.append("\nTap a game for its recap reel (65-85 s, checked before it's handed over)." if rows else
                     "\nNone has finished yet: a game's highlights go up after it ends.")
        if out["problems"]:
            lines.append(f"⚠ {esc('; '.join(out['problems'][:2]))}")
        return "\n".join(lines), inline(rows[:GAMES_SHOWN * 2]) if rows else None

    def press_league(self, chat, mid, arg, video) -> str:
        from ..sportsdesk import slate

        row = next((r for r in slate.LEAGUES if r[1] == arg), None)
        if row is None:
            return "That league isn't on the scoreboards"
        text, buttons = self.games_message(row[0], row[2])
        self.say(chat, text, buttons)
        return ""

    def press_game(self, chat, mid, arg, video) -> str:
        """A game tapped in /games: its recap reel is asked for, there and then."""
        from ..sportsdesk import slate

        g = slate.find(arg, now=self.now(), get=self.slate_get)
        if g is None:
            return "That game isn't in the last 24 hours any more: send /games again"
        try:
            r = self.sports_order(chat, slate.order_values(g, "reel"))
        except Refused as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return ""
        self.say(chat, "🏆 " + r["html"], self.order_buttons(r["order"]))
        return "On it"

    def press_sports_cancel(self, chat, mid, arg, video) -> str:
        try:
            r = self.sports_cancel(chat, arg)
        except Refused as exc:
            return str(exc)
        self.say(chat, "✖ " + esc(r["message"]))
        return "Stopped"

    def sports_order(self, chat: int, values: dict) -> dict:
        """Ask automation for a game's best plays as clips, a recap reel or both (sportsdesk's Desk.order), in
        `chat`'s name. Raises Refused."""
        from ..sportsdesk.desk import DeskError

        try:
            r = self.desk.order(values if isinstance(values, dict) else {}, by=self.name(chat), by_id=chat)
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        o = r["order"]
        self._remember(f"sports:{o['id']}", chat, o["title"], "sports")
        self.log(f"{self.name(chat)} asked for sports highlights: {o['title']}.")
        self.did(chat, "sports", o["title"])
        where = f" for {o['category']}" if o.get("category") else ""
        checked = (" It's checked before it's handed over (over a minute, upright, with sound)."
                   if o.get("requires") else "")
        message = (f"Got it: {o['title']}{where}. Automation looks for the game, finds its best plays and makes "
                   f"them; they show up under Videos, and I'll tell you when they're ready.{checked}")
        notes = list(r.get("notes") or [])
        return {"order": o, "message": message, "notes": notes,
                "html": esc(message) + "".join(f"\nℹ {esc(n)}" for n in notes)}

    def sports_cancel(self, chat: int, order_id: str) -> dict:
        from ..sportsdesk.desk import DeskError

        try:
            r = self.desk.cancel_order(str(order_id))
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        with self.lock, state.edit(self.state_file) as data:
            data["asked"].pop(f"sports:{order_id}", None)
        self.log(f"{self.name(chat)} cancelled a sports request ({r['order']['title']}).")
        self.did(chat, "cancel", r["order"]["title"])
        return r

    def sports_report(self, o: dict, items: int, made: bool) -> str:
        """How a finished sports order went, as HTML with {whose} left for who's told ("Your sports request")."""
        title = esc(trim(o.get("title") or "", 160)).replace("{", "(").replace("}", ")")
        said = [esc(trim(st.get("said") or "", 200)).replace("{", "(").replace("}", ")")
                for st in o.get("steps") or [] if st.get("kind") in ("clips", "reel") and st.get("status") == "done"]
        if o.get("status") == "failed":
            why = esc(trim(o.get("error") or "It failed.", 300)).replace("{", "(").replace("}", ")")
            return f"✖ <b>{{whose}} couldn't be done</b> ({title}): {why}"
        from ..sportsdesk.games import GameFilter

        emoji = GameFilter.from_json(o.get("filter")).sport_info.emoji
        lines = [f"{emoji} <b>{{whose}} is ready</b>: {title}"] + [f"• {s}" for s in said]
        if not said:
            lines.append(esc(trim(o.get("message") or "", 300)).replace("{", "(").replace("}", ")"))
        if o.get("error"):
            lines.append("⚠ " + esc(trim(o["error"], 300)).replace("{", "(").replace("}", ")"))
        from ..sportsdesk.desk import order_review

        rv = order_review(o)
        if rv.get("checked"):
            if not rv["held"]:
                lines.append("✓ Checked: " + ("it meets" if rv["checked"] == 1 else f"all {rv['checked']} meet")
                             + " the minimums (over a minute, upright, with sound).")
            else:
                lines.append(f"⏸ {plural(rv['held'], 'video is', 'videos are')} under the minimums and "
                             f"{'waits' if rv['held'] == 1 else 'wait'} for an OK, not posted by "
                             f"{'itself' if rv['held'] == 1 else 'themselves'}: "
                             + esc("; ".join(rv["why"])).replace("{", "(").replace("}", ")") + "."
                             + (f" {plural(rv['passed'], 'other meets', 'others meet')} them." if rv["passed"] else ""))
        if made and not items:
            lines.append("They're in the Library on the PC (not queued to post: its category has no TikTok "
                         "account?).")
        return "\n".join(lines)

    def sports_json(self) -> dict:
        """What the app's sports form offers, and the sports requests so far."""
        from .. import sports
        from ..compliance import requirements
        from ..sportsdesk import desk as D
        from ..sportsdesk import games as G
        from ..sportsdesk import slate

        s = self.settings()
        return {"sports": [{"id": sp.id, "label": sp.label, "emoji": sp.emoji,
                            "leagues": list(G.LEAGUES.get(sp.id, ()))} for sp in sports.SPORTS.values()],
                "since": [{"id": k, "label": label} for k, label, _days in G.SINCE],
                "wants": [{"id": k, "label": v} for k, v in D.ORDER_WANTS.items() if k != "find"],
                "categories": [{"id": c["id"], "name": c["name"], "sport": c["sport"], "account": c["account"]}
                               for c in D.category_list(s)],
                "most": {"games": D.ORDER_GAMES, "plays": D.ORDER_PLAYS, "open": D.MAX_OPEN_ORDERS},
                "want": "reel", "requires": requirements.rules(s), "scoreboards": list(slate.SPORTS),
                "doing": self.desk.doing(), "orders": (orders := self.desk.orders(12)),
                # for "Find a moment": the games on the Sports page (the page adds the last 24 hours' itself),
                # and which found moments are set aside for each game's reel
                "find_games": self.game_choices(12, slate_too=False),
                "baskets": self._baskets({m.get("game") for o in orders for m in o.get("moments") or []})}

    def _baskets(self, game_ids) -> dict:
        out = {}
        for gid in sorted(g for g in game_ids if g):
            try:
                out[gid] = self.desk.basket(gid)
            except Exception:  # noqa: BLE001 - the game went since: nothing is set aside
                out[gid] = []
        return out

    def played_json(self, sport: str = "any", league: str = "") -> dict:
        """The games of the last 24 hours for the app: each with what its row says and whether it can be asked
        for yet."""
        from .. import sports
        from ..sportsdesk import slate

        sport = sport if sports.get(sport) else "any"
        return slate.public(self.played(sport, " ".join(str(league or "").split())[:60]), self.now())

    def sports_game(self, chat: int, game_id: str, want: str = "reel", plays=None, category: str = "") -> dict:
        """Ask for a listed game by its id (the app's drop-down and its Games played list). Raises Refused."""
        from ..sportsdesk import slate

        g = slate.find(str(game_id), now=self.now(), get=self.slate_get)
        if g is None:
            raise Refused("That game isn't in the last 24 hours any more. Load the games again.", 404)
        try:
            plays = int(plays) if plays else None
        except (TypeError, ValueError):
            plays = None
        return self.sports_order(chat, slate.order_values(g, want or "reel", plays, category))

    # --- a moment, found by saying it ----------------------------------------------------------------------------

    def cmd_find(self, chat: int, text: str = "") -> None:
        """/find Mahomes 4th quarter touchdown · /search red card in Arsenal Chelsea"""
        text = " ".join(text.split())
        if len(text) < 3:
            self.modes[chat] = ("find", self.now() + MODE_LIFE)
            self.say(chat, "🔎 <b>Which moment?</b> " + FIND_HELP + "\n\nI search the game's commentary and "
                           "play-by-play on the PC and send back what I find, each with a button to clip it or add "
                           "it to a reel.")
            return
        self.find(chat, text)

    def sports_text(self, chat: int, text: str) -> bool:
        """A message with no command: "find the red card in Arsenal Chelsea". True when it was taken as a
        search, which is only when its words name a game (anything else is left for the chat's other reading)."""
        m = _FIND_SAID.match(" ".join(str(text or "").split()))
        return bool(m) and self.find(chat, m.group("q"), ask=False)

    def find(self, chat: int, text: str, ask: bool = True) -> bool:
        """Search a game for the moment `text` says. With no game named: offer the games (`ask`), or leave it."""
        query, where = split_find(text)
        target = self.find_target(where or query, said=bool(where))
        if target is None:
            if not ask:
                return False
            self.ask_game(chat, query, where)
            return True
        self.start_find(chat, query, *target)
        return True

    def desk_games(self) -> list[dict]:
        """The games on the Sports page that have a video to search, the newest first."""
        try:
            games = self.desk.state().get("games") or []
        except Exception:  # noqa: BLE001 - sports.db busy for a moment
            return []
        games = [g for g in games if g.get("video") and g.get("status") not in ("queued", "analysing", "making")]
        return sorted(games, key=lambda g: -(g.get("analysed") or g.get("found") or g.get("published") or 0))

    def find_target(self, words: str, said: bool = False) -> tuple[str, dict] | None:
        """The game `words` names: ("desk", a game on the Sports page), ("slate", one of the last 24 hours'
        games) or ("words", what Desk.order takes, when `said` set the game apart: "… in Arsenal Chelsea").
        None when they name no game, or more than one as well as each other."""
        from ..sportsdesk import slate

        def best(games: list[dict], sides) -> dict | None:
            scored = [(sides_named(words, sides(g)), g) for g in games]
            top = max((n for n, _g in scored), default=0)
            winners = [g for n, g in scored if n == top]
            return winners[0] if top and (len(winners) == 1 or top == 2) else None

        g = best(self.desk_games(), lambda g: [[g.get("home") or ""], [g.get("away") or ""]])
        if g is not None:
            return "desk", g
        try:
            played = [x for x in self.played()["games"] if x["state"] == "final"]
        except Exception:  # noqa: BLE001 - the scoreboards can't be read right now
            played = []
        g = best(played, lambda g: [list(g.get("home_names") or [g["home"]]), list(g.get("away_names") or [g["away"]])])
        if g is not None:
            return "slate", slate.order_values(g, "reel")
        if said:
            try:
                values = parse_sports(words)
            except ValueError:
                return None
            if values.get("teams") or values.get("league"):
                return "words", values
        return None

    def game_choices(self, most: int = GAME_CHOICES, slate_too: bool = True) -> list[dict]:
        """The games a search can be pointed at: {kind: "desk" | "slate", id, label}. The ones on the Sports
        page first (their video is here already), then the last 24 hours' finished games."""
        out = [{"kind": "desk", "id": g["id"],
                "label": f"{g.get('emoji') or '🏆'} {g.get('matchup') or g.get('title') or ''}".strip()}
               for g in self.desk_games()[:max(1, most // 2)]]
        if slate_too:
            try:
                played = [x for x in self.played()["games"] if x["state"] == "final"]
            except Exception:  # noqa: BLE001 - the scoreboards can't be read right now
                played = []
            out += [{"kind": "slate", "id": g["id"], "label": f"{g['emoji']} {g['label']}"} for g in played]
        return out[:most]

    def ask_game(self, chat: int, query: str, where: str = "") -> None:
        """The search named no game (or one that isn't known): offer the ones there are."""
        choices = self.game_choices()
        lead = (f"I don't know a game called “{esc(trim(where, 60))}”. " if where else "")
        if not choices:
            self.say(chat, f"🔎 {lead}Which game is “{esc(trim(query, 100))}” in? Name it with the moment, like "
                           "<i>/find red card in Arsenal Chelsea</i> (the league helps: <i>… in NFL Chiefs Bills</i>).")
            return
        token = self._ask("find", chat, query=query, games=[[c["kind"], c["id"]] for c in choices])
        rows = [[(trim(c["label"], 44), f"fg:{token}:{n}")] for n, c in enumerate(choices)]
        rows.append([("✖ Cancel", f"x:{token}")])
        self.say(chat, f"🔎 {lead}Which game is “{esc(trim(query, 100))}” in?\n<i>Or name it with the moment: "
                       "/find red card in Arsenal Chelsea</i>", inline(rows))

    def _target(self, kind: str, ident: str) -> dict:
        """A game picked from game_choices, as sports_find takes it. Raises Refused when it's gone."""
        from ..sportsdesk import slate
        from ..sportsdesk.desk import DeskError

        if kind == "desk":
            try:
                return self.desk.game(ident)
            except DeskError:
                raise Refused("That game isn't on the Sports page any more.", 404) from None
        g = slate.find(ident, now=self.now(), get=self.slate_get)
        if g is None:
            raise Refused("That game isn't in the last 24 hours any more.", 404)
        return slate.order_values(g, "reel")

    def app_find(self, chat: int, query: str, game: str = "") -> dict:
        """The Mini App's search: `game` is "desk:<id>" or "slate:<id>" (picked from its list), or empty, when
        the words have to name it ("red card in Arsenal Chelsea"). Raises Refused: 422 when no game is named."""
        query = " ".join(str(query or "").split())
        if len(query) < 3:
            raise Refused("Say the moment to look for, such as “red card” or “4th quarter touchdown”.", 400)
        kind, _, ident = str(game or "").partition(":")
        if kind in ("desk", "slate") and ident:
            return self.sports_find(chat, query, kind, self._target(kind, ident[:80]))
        q, where = split_find(query)
        target = self.find_target(where or q, said=bool(where))
        if target is None:
            raise Refused("Pick the game it's in (or name it: “red card in Arsenal Chelsea”).", 422)
        return self.sports_find(chat, q, *target)

    def app_basket(self, game_id: str, hit_id: str, add: bool = True) -> dict:
        from ..sportsdesk.desk import DeskError

        try:
            return {"game": game_id, "basket": self.desk.basket(game_id, hit_id, add=add)}
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc

    def press_find_game(self, chat, mid, arg, video) -> str:
        token, _, n = arg.partition(":")
        p = self._take(token, chat)
        if p is None:
            return "That question has expired: send /find again"
        try:
            kind, ident = p["games"][int(n)]
            target = self._target(kind, ident)
        except (ValueError, IndexError, TypeError):
            return "That game isn't on the list any more"
        except Refused as exc:
            return trim(str(exc), 190)
        try:
            self.api.edit_buttons(chat, mid, None)
        except ApiError:
            pass
        self.start_find(chat, p["query"], kind, target)
        return "Searching"

    def start_find(self, chat: int, query: str, kind: str, target: dict) -> dict | None:
        """Ask the desk to search `target` for `query`, in `chat`'s name, and say so. None when it was refused
        (and that was said)."""
        try:
            r = self.sports_find(chat, query, kind, target)
        except Refused as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return None
        self.say(chat, "🔎 " + r["html"], self.order_buttons(r["order"]))
        return r

    def sports_find(self, chat: int, query: str, kind: str, target: dict) -> dict:
        """The search itself, for the chat and the Mini App. Raises Refused."""
        from ..sportsdesk.desk import DeskError

        query = " ".join(str(query or "").split())
        try:
            if kind == "desk":
                r = self.desk.find_in(str(target.get("id") or ""), query, by=self.name(chat), by_id=chat)
            else:
                r = self.desk.order({**target, "find": query}, by=self.name(chat), by_id=chat)
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        o = r["order"]
        self._remember(f"sports:{o['id']}", chat, o["title"], "find")
        self.log(f"{self.name(chat)} asked to search a game: {o['title']}.")
        self.did(chat, "find", o["title"])
        message = (f"Searching {o['title']}. " if kind == "desk" else
                   f"Looking for {o['title']}: first the game's video on YouTube, then the moment in it. ")
        message += "I'll send what I find, each with a button to clip it or add it to a reel."
        return {"order": o, "message": message, "html": esc(message)}

    def sports_found(self, chat: int, o: dict, others: list[int], who: str) -> None:
        """A search is over: the moments as cards for whoever asked, and a line for the other person."""
        title = esc(trim(o.get("title") or "", 160))
        if o.get("status") == "failed":
            self.say(chat, f"✖ <b>Couldn't search</b> {title}: {esc(trim(o.get('error') or 'It failed.', 300))}")
            return
        moments = list(o.get("moments") or [])
        game_id = next((st.get("game") for st in o.get("steps") or [] if st.get("kind") == "events"), "")
        matchup = next((st.get("matchup") for st in o.get("steps") or [] if st.get("kind") == "events"), "")
        if not moments:
            self.say(chat, f"🔎 <b>Nothing found</b> for {title}.\nThe search reads the commentary and the "
                           "play-by-play, so try the words a commentator would use (<i>red card</i>, <i>touchdown "
                           "pass</i>, a player's name), or another upload of the game on the Sports page.")
            return
        shown = moments[:MOMENT_CARDS]
        rest = moments[MOMENT_CARDS:]
        head = (f"🔎 <b>{plural(len(moments), 'moment')} found</b>: {title}\n"
                "🎬 makes the moment a clip of its own (a single play is under a minute, so it waits for your OK "
                "rather than posting by itself). ➕ sets it aside for one reel of the moments you pick.")
        rows = [[(f"▾ Show {plural(len(rest), 'more moment')}", f"fm:m:{o['id']}")]] if rest else []
        if self.app_url:
            rows.append([self.app_row("📱 Open in the app")])
        self.say(chat, head, inline(rows) if rows else None)
        for m in shown:
            self.say(chat, moment_html(m, matchup), self.moment_buttons(game_id, m))
        for other in others:
            self.say(other, f"🔎 {who} searched {title}: {plural(len(moments), 'moment')} found.", quiet=True)

    def moment_buttons(self, game_id: str, m: dict, basket: list[str] | None = None) -> dict:
        """🎬 Clip Play · ➕ Add to Game Reel (and, once something is set aside, 🎞 Make the reel)."""
        length = max(0.0, float(m.get("end") or 0) - float(m.get("start") or 0))
        if basket is None:
            try:
                basket = self.desk.basket(game_id)
            except Exception:  # noqa: BLE001 - the game went meanwhile: the press says so
                basket = []
        inside = m.get("id") in basket
        rows = [[(f"🎬 Clip Play ({length:.0f}s)", f"fm:c:{game_id}:{m.get('id')}"),
                 ("✓ In the reel · take out" if inside else "➕ Add to Game Reel",
                  f"fm:{'x' if inside else 'a'}:{game_id}:{m.get('id')}")]]
        if basket:
            rows.append([(f"🎞 Make the reel ({plural(len(basket), 'moment')})", f"fm:r:{game_id}")])
        return inline(rows)

    def press_moment(self, chat, mid, arg, video) -> str:
        """A button under a found moment: c clip it, a add it to the reel, x take it out, r make the reel,
        m show the moments not shown yet."""
        from ..sportsdesk.desk import DeskError

        what, _, rest = arg.partition(":")
        game_id, _, hit_id = rest.partition(":")
        try:
            if what == "m":
                o = self.desk.find_order(game_id) or {}
                more = list(o.get("moments") or [])[MOMENT_CARDS:]
                if not more:
                    return "There are no more"
                gid = next((st.get("game") for st in o.get("steps") or [] if st.get("kind") == "events"), "")
                name = next((st.get("matchup") for st in o.get("steps") or [] if st.get("kind") == "events"), "")
                try:
                    self.api.edit_buttons(chat, mid, None)
                except ApiError:
                    pass
                for m in more:
                    self.say(chat, moment_html(m, name), self.moment_buttons(gid, m))
                return ""
            if what in ("a", "x"):
                basket = self.desk.basket(game_id, hit_id, add=what == "a")
                _g, hit = self.desk.moment(game_id, hit_id)
                try:
                    self.api.edit_buttons(chat, mid, self.moment_buttons(game_id, _as_moment(game_id, hit), basket))
                except ApiError:
                    pass
                return (f"Set aside: {plural(len(basket), 'moment')} for this game's reel. 🎞 makes it." if what == "a"
                        else "Taken out of the reel")
            if what == "c":
                r = self.sports_moments(chat, game_id, [hit_id], "clips")
            elif what == "r":
                ids = self.desk.basket(game_id)
                if not ids:
                    return "Add a moment to the reel first (➕)"
                r = self.sports_moments(chat, game_id, ids, "reel")
            else:
                return "That button is from an older version"
        except DeskError as exc:
            return trim(exc.message, 190)
        except Refused as exc:
            return trim(str(exc), 190)
        self.say(chat, ("🎬 " if what == "c" else "🎞 ") + r["html"], self.order_buttons(r["order"]))
        return "On it"

    def sports_moments(self, chat: int, game_id: str, ids: list[str], want: str = "clips",
                       category: str | None = None) -> dict:
        """Make found moments into videos (each a clip, or all one reel), in `chat`'s name. Raises Refused."""
        from ..sportsdesk.desk import DeskError

        try:
            r = self.desk.order_moments(str(game_id), list(ids or []), want, by=self.name(chat), by_id=chat,
                                        category=category)
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        o = r["order"]
        self._remember(f"sports:{o['id']}", chat, o["title"], "sports")
        self.log(f"{self.name(chat)} asked for found moments to be made: {o['title']}.")
        self.did(chat, "sports", o["title"])
        where = f" for {o['category']}" if o.get("category") else ""
        message = f"Got it: {o['title']}{where}. I'll tell you when it's ready."
        notes = list(r.get("notes") or [])
        if want == "reel" and len(ids) < 3:
            notes.insert(0, f"A reel of {plural(len(ids), 'moment')} usually comes in under a minute, so it will "
                            "wait for your OK. Three or more moments make one that's over a minute.")
        return {"order": o, "message": message, "notes": notes,
                "html": esc(message) + "".join(f"\nℹ {esc(n)}" for n in notes)}

    def moments_json(self, game_id: str) -> dict:
        """A game's found moments for the Mini App: what a card shows, and which are set aside for its reel."""
        from ..sportsdesk.desk import DeskError

        try:
            g = self.desk.game(str(game_id))
        except DeskError as exc:
            raise Refused(exc.message, exc.status) from exc
        ev = g.get("events") or {}
        listed = {h["id"] for h in g.get("highlights") or [] if h.get("origin") == "event"}
        hits = [h for h in ev.get("hits") or [] if h.get("id") in listed]
        basket = self.desk.basket(g["id"])
        return {"game": g["id"], "matchup": g.get("matchup") or g.get("title") or "", "query": ev.get("query") or "",
                "url": g.get("url") or "", "basket": basket, "busy": g.get("status") in ("queued", "analysing", "making"),
                "moments": [{**_as_moment(g["id"], h), "in_reel": h.get("id") in basket,
                             "at": clock(h.get("t_event")), "html": moment_html(_as_moment(g["id"], h))}
                            for h in sorted(hits, key=lambda h: -(h.get("score") or 0))]}


def _as_moment(game_id: str, h: dict) -> dict:
    """A found moment as the desk keeps it (semantic_search.EventHit.to_json) → as a card reads it."""
    return {"game": game_id, "id": h.get("id"), "t": round(float(h.get("t_event") or 0), 1),
            "start": round(float(h.get("start") or 0), 1), "end": round(float(h.get("end") or 0), 1),
            "label": h.get("label") or "Moment", "quote": " ".join(str(h.get("quote") or "").split())[:240],
            "why": " ".join(str(h.get("why") or "").split())[:200], "score": round(float(h.get("score") or 0)),
            "certain": bool(h.get("certain", True)), "badge": h.get("badge") or ""}
