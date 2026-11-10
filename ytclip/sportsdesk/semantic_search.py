"""Find a moment in a game by describing it: "red card after a VAR check", "technical foul on the bench",
"game-tying shot in the final minute".

The plays the Sports tab finds on its own are the loud ones (sports.py: the crowd, the commentator's big
calls). A booking, a sideline row, a penalty that's given and then taken away are often quiet, and they're
what people ask for by name. This reads what was *said* and what the game's official timeline *recorded*,
compares both with the question, and cuts each moment found with its build-up and its aftermath.

    result = search("red card or player ejection", cues, duration, sport="soccer", fine=m.fine,
                    excitement=crowd, feed=feed, s=settings)
    for hit in result.hits:  hit.start, hit.end, hit.t_event, hit.score, hit.why, hit.badge

How it works:

1. **The question** (parse): split into its alternatives ("X or Y"), each matched to the sports ideas it
   names (CONCEPTS: red card, VAR review, technical foul, pass interference, buzzer-beater…), plus what else it
   pins down: a period ("4th quarter", "second half", "overtime"), the end of the game ("final minute"), an
   order ("after a VAR check"), and the teams or the game ("in Arsenal vs Chelsea"). An AI can read a question
   the rules don't get (the router, local first); the rules alone are enough for the common ones.
2. **What the video says**: the transcript (captions, or Whisper's timestamps to the millisecond) cut into
   15-second windows that overlap by 10, so no sentence is split from its neighbour.
3. **What the game recorded**: the play-by-play feed's events (feeds.py), placed in the video (align.py).
4. **Matching**: each window and each event against the question, three ways that back each other up: the
   sentence embedding's cosine similarity (intelligence/embeddings.py: all-MiniLM-L6-v2 on this computer),
   whether the words of the idea are actually said, and whether the crowd reacted. A window where the idea is
   only talked about ("that should have been a red") scores lower and is marked as such.
5. **The cut**: the moment T is where the idea is first named (or the feed's own second). The clip runs from
   T - 12 s (the foul, the pass, the argument starting) to T + 25 s (the referee, the crowd, the protests, the
   first replay), a little wider for the ideas that take longer (a VAR review, a fight), stretched when the
   commentary is still on it at the end, then each end moved onto a pause between sentences so nobody is cut
   off mid-word.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .. import sports
from ..subtitles import Cue, text_between
from . import align
from .feeds import KIND_WORDS, Feed, FeedEvent

WINDOW = 15.0  # seconds of commentary compared at once
STRIDE = 5.0  # a new window every this many seconds (so each overlaps the next by 10)
PAD_BEFORE = 12.0
PAD_AFTER = 25.0
STRETCH = 15.0  # at most this much more after, while the commentary is still on it
MERGE_GAP = 20.0  # matches closer than this are the same moment
MAX_HITS = 8
QUALIFY = 0.38  # a match scoring under this (0-1) isn't shown
# Cosine similarity that counts as "nothing in common" and "the same thing", per embedder
SCALE = {"minilm": (0.18, 0.62), "words": (0.05, 0.45)}

Log = Callable[[str], None]


@dataclass(frozen=True)
class Concept:
    id: str
    label: str
    says: str  # how a commentator or a feed would put it, for the sentence model
    words: tuple[str, ...]  # what names it (regular expressions, whole words)
    feed: tuple[str, ...] = ()  # the feed kinds (feeds.KINDS) that are this
    sports: tuple[str, ...] = ()  # the sports it belongs to (() = any)
    before: float = PAD_BEFORE
    after: float = PAD_AFTER
    asks: tuple[str, ...] = ()  # extra ways a *question* names it (not looked for in commentary)


CONCEPTS: dict[str, Concept] = {c.id: c for c in (
    Concept("red_card", "Red card", "The referee shows a red card and the player is sent off.",
            (r"red card", r"straight red", r"sent off", r"sending[- ]off", r"send(?:s|ing)? (?:him|her|them) off",
             r"dismiss(?:ed|al)", r"second yellow", r"second booking", r"marching orders", r"off (?:he|she) goes",
             r"down to (?:ten|10|nine|9) men", r"an early bath", r"shown? (?:a |the )?red", r"it'?s (?:a )?red",
             r"he'?s off", r"she'?s off", r"\bred\b[^.?!]{0,30}\b(?:referee|ref)\b", r"\b(?:referee|ref)\b[^.?!]{0,40}\bred\b"),
            ("red_card", "second_yellow"), ("soccer",), 14.0, 28.0, (r"sending off", r"red")),
    Concept("ejection", "Ejection", "The player is ejected from the game and has to leave.",
            (r"eject(?:ed|ion|s)?", r"tossed", r"thrown out", r"disqualif(?:ied|ication)", r"game misconduct",
             r"match penalty", r"flagrant (?:two|2)", r"run from the game", r"his (?:night|day) is (?:over|done)",
             r"been (?:run|tossed)", r"hit the showers"),
            ("ejection", "flagrant_foul"), (), 14.0, 28.0, (r"player ejection", r"kicked out", r"thrown out")),
    Concept("yellow_card", "Yellow card", "The referee books the player with a yellow card.",
            (r"yellow card", r"book(?:ed|ing)", r"caution(?:ed)?", r"into the (?:referee'?s )?book", r"goes in the book",
             r"shown? (?:a |the )?yellow", r"it'?s (?:a )?yellow"),
            ("yellow_card",), ("soccer",), 10.0, 18.0),
    Concept("var", "VAR / video review", "The decision goes to a video review and the referee checks the monitor.",
            (r"\bvar\b", r"v\.a\.r", r"video assistant", r"video review", r"pitch[- ]?side monitor", r"the monitor",
             r"to the (?:screen|monitor)", r"(?:being|under|quick|long) (?:review|check)", r"check(?:ing)? (?:complete|for|"
             r"(?:a )?possible)", r"replay review", r"upon (?:further )?review", r"(?:coach'?s|manager'?s) challenge",
             r"(?:they|he|she)(?:'ll| will| are going to|'re going to) challenge", r"challenge flag", r"booth review",
             r"review(?:ed|ing)? (?:the|this|that)", r"overturn(?:ed|s)?", r"call (?:stands|is confirmed|on the field)",
             r"taking (?:another|a second|a long) look", r"having a look at (?:this|that|it)"),
            ("var",), (), 18.0, 32.0, (r"var check", r"review", r"replay")),
    Concept("penalty", "Penalty awarded", "The referee points to the spot and gives a penalty.",
            (r"penalty", r"points? to the spot", r"spot[- ]kick", r"from (?:the spot|twelve yards|12 yards)", r"pen\b"),
            ("penalty_goal", "penalty_missed"), ("soccer",), 14.0, 30.0),
    Concept("penalty_missed", "Missed penalty", "The penalty is missed or saved by the goalkeeper.",
            (r"miss(?:es|ed)? (?:the |a |his |her )?(?:penalty|spot[- ]kick)", r"(?:penalty|spot[- ]kick) (?:is )?(?:saved|missed)",
             r"saves? the (?:penalty|spot[- ]kick)", r"penalty save", r"(?:skies|skied|blazes|blazed|puts?) it over",
             r"(?:hits|off|against) the (?:post|bar|crossbar|woodwork) from the spot", r"guesses right",
             r"keeper (?:saves|guessed|goes the right way)"),
            ("penalty_missed",), ("soccer",), 16.0, 22.0, (r"missed penalty", r"penalty miss")),
    Concept("offside", "Offside call", "The flag goes up for offside and the goal is ruled out.",
            (r"offside", r"flag (?:is|goes|went|stays) (?:up|down)", r"ruled out", r"disallowed", r"chalked off",
             r"won'?t count", r"doesn'?t count", r"no goal"),
            ("offside",), ("soccer", "hockey", "football"), 12.0, 24.0, (r"offside call",)),
    Concept("goal", "Goal", "A goal is scored and the crowd celebrates.",
            (r"go+a+l+", r"scores?\b", r"scored", r"back of the net", r"finds the net", r"equali[sz]er", r"the winner",
             r"own goal", r"puts? (?:it|that) away", r"lights? the lamp"),
            ("goal", "penalty_goal", "own_goal"), ("soccer", "hockey"), 12.0, 18.0),
    Concept("foul", "Foul", "A bad foul or a dangerous tackle brings the player down.",
            (r"\bfoul(?:ed|s)?\b", r"brought down", r"hacked down", r"wipe[sd]? (?:him|her) out", r"late (?:challenge|tackle|hit)",
             r"reckless", r"studs[- ]up", r"two[- ]footed", r"dangerous (?:tackle|play|challenge)", r"horror tackle",
             r"clatter(?:s|ed)", r"elbow", r"stamp(?:s|ed)?", r"cheap shot", r"dirty (?:play|hit)"),
            ("foul", "flagrant_foul"), (), 10.0, 20.0),
    Concept("fight", "Fight / altercation", "Tempers flare and the players push and shove each other in a fight.",
            (r"fight(?:s|ing)?\b", r"brawl", r"scuffle", r"melee", r"shov(?:e|es|ed|ing)", r"punch(?:es)? (?:thrown|are thrown)",
             r"throws? a punch", r"benches (?:clear|empty)", r"bench[- ]clearing", r"tempers (?:flare|flaring|boil)",
             r"altercation", r"squar(?:e|es|ing) up", r"push(?:ing)? and shov", r"handbags", r"goes after (?:him|her)",
             r"face[- ]to[- ]face", r"head[- ]to[- ]head", r"separat(?:e|ed|ing) them", r"drop(?:s|ped)? the gloves",
             r"getting (?:heated|chippy|ugly)", r"confrontation", r"all kicking off", r"kicking off (?:here|now)"),
            ("fight",), (), 8.0, 35.0, (r"sideline argument",)),
    Concept("technical_foul", "Technical foul", "The official calls a technical foul for arguing.",
            (r"technical foul", r"\btechnical\b", r"\btech\b", r"t'?d up", r"double technical", r"bench technical",
             r"gets (?:a|the) t\b", r"hit with a t\b"),
            ("technical_foul",), ("basketball",), 12.0, 25.0),
    Concept("flagrant", "Flagrant foul", "The officials review the hard foul and call it flagrant.",
            (r"flagrant", r"hard foul", r"excessive contact", r"wind[- ]up"),
            ("flagrant_foul",), ("basketball",), 12.0, 28.0),
    Concept("pass_interference", "Pass interference / flag", "A flag is thrown for pass interference on the play.",
            (r"pass interference", r"\bdpi\b", r"\bopi\b", r"flag (?:on the play|is down|comes? (?:in|out)|thrown)",
             r"flags? (?:fly|flies|flying)", r"there'?s a flag", r"laundry", r"roughing the (?:passer|kicker)",
             r"targeting", r"unnecessary roughness", r"personal foul", r"defensive holding", r"face ?mask",
             r"no flag", r"they (?:pick|picked) up the flag", r"(?:no|missed) call"),
            ("flag",), ("football",), 14.0, 26.0, (r"\bpi\b", r"controversial (?:flag|penalty)")),
    Concept("buzzer_beater", "Buzzer-beater / late winner",
            "The shot goes in at the buzzer to win the game as time expires.",
            (r"buzzer[- ]?beater", r"at the (?:buzzer|horn|death|whistle)", r"as time expires", r"beats the (?:buzzer|clock)",
             r"game[- ]?winner", r"game[- ]winning", r"walk[- ]?off", r"wins it", r"won it", r"last[- ](?:second|gasp)",
             r"with no time (?:left|remaining)", r"for the win", r"in the dying (?:seconds|moments|minutes)",
             r"stoppage[- ]time winner", r"injury[- ]time winner"),
            (), (), 14.0, 20.0, (r"late winner",)),
    Concept("game_tying", "Game-tying score", "The shot ties the game late and sends it to overtime.",
            (r"game[- ]tying", r"ties? (?:the game|it|this game)(?: up)?", r"tied (?:it|the game) up", r"we(?:'re| are) tied",
             r"all (?:tied|square|level)", r"(?:sends?|forces?|force|heading to|going to|headed to) (?:it |us |this (?:game|one) )?"
             r"(?:to |into )?(?:overtime|extra time|ot)\b", r"equali[sz](?:er|es|ed)", r"levels? it", r"to tie\b"),
            (), (), 14.0, 20.0, (r"tying shot",)),
    Concept("coach", "Coach / manager incident",
            "The manager is furious on the touchline, argues with the official and is booked or sent to the stands.",
            (r"(?:manager|coach|head coach|boss|gaffer|skipper)[^.?!]{0,60}\b(?:booked|yellow|red|sent (?:off|to the stands)|"
             r"ejected|tossed|furious|livid|irate|incensed|arguing|screaming|fuming|technical|warned|dismissed)",
             r"(?:booked|yellow card|red card|ejected|tossed|technical|sent to the stands|furious|livid|irate|arguing|"
             r"screaming|fuming)[^.?!]{0,60}\b(?:manager|coach|head coach|boss|gaffer|bench|technical area|dugout|sideline|"
             r"touchline)", r"on the (?:touchline|sideline|bench|technical area)[^.?!]{0,50}\b(?:furious|livid|argu|scream|"
             r"protest|fum|booked|warned)", r"sent to the stands", r"in the (?:fourth official|referee|official)'?s (?:ear|face)",
             r"bench (?:is |are )?(?:warned|furious|up in arms)", r"technical (?:on|to) the bench",
             r"(?:giving|gives|letting|lets) the (?:referee|ref|official|umpire)s? (?:an earful|have it)"),
            ("technical_foul", "ejection", "yellow_card", "red_card"), (), 12.0, 28.0,
            (r"manager", r"coach", r"sideline argument", r"touchline", r"bench")),
    Concept("controversy", "Controversial call",
            "A controversial decision by the referee that everyone is arguing about.",
            (r"controvers(?:y|ial)", r"robbed", r"never (?:a|in a million years a) (?:penalty|foul|red|card)",
             r"(?:terrible|awful|shocking|horrible|dreadful|baffling|bizarre|strange|harsh|soft|dubious|questionable|"
             r"big|huge) (?:call|decision|one)", r"blown call", r"missed (?:call|that)", r"how (?:is|was) that not",
             r"how has (?:he|she|that) (?:got|not)", r"should(?:'ve| have) been", r"can'?t believe (?:that|the|it)",
             r"got (?:that|it) (?:wrong|badly wrong)", r"gets? away with", r"lucky (?:boy|not to|to (?:stay|still be))",
             r"not happy (?:with|about)", r"furious (?:with|at|about)", r"surround(?:s|ed|ing) the (?:referee|ref|official)",
             r"protest(?:s|ing|ed)?", r"disbelief", r"incredulous", r"what (?:has|did) (?:he|she|the referee) (?:seen|see)",
             r"that'?s (?:a |an )?(?:joke|disgrace|outrage|scandal)", r"wrong (?:call|decision)"),
            ("var",), (), 14.0, 28.0, (r"controversial", r"bad call", r"terrible ref")),
    Concept("injury", "Injury", "The player is down injured and the medical staff come on.",
            (r"injur(?:y|ed|ies)", r"stretcher", r"medical (?:staff|team)", r"(?:physio|trainer)s? (?:is |are )?on",
             r"down (?:hurt|in pain|holding)", r"stays down", r"helped off", r"carted off", r"limps? off",
             r"head injury", r"concussion protocol", r"can'?t continue"),
            (), (), 12.0, 25.0),
    Concept("save", "Big save", "The goalkeeper makes an incredible save.",
            (r"what a save", r"(?:great|brilliant|stunning|superb|incredible|fingertip|double|point[- ]blank|glove) save",
             r"saved by", r"keeps? it out", r"robbed (?:him|her)", r"denied by", r"stonewall(?:s|ed)?"),
            ("save",), ("soccer", "hockey"), 10.0, 14.0),
    Concept("turnover", "Turnover", "The ball is intercepted or fumbled and possession changes.",
            (r"intercept(?:ed|ion|s)?", r"picked off", r"pick[- ]six", r"fumble[sd]?", r"strip[- ]sack", r"turnover",
             r"\bsteals?\b", r"stolen", r"coughs? it up", r"gives? it away"),
            ("turnover",), ("football", "basketball"), 10.0, 16.0),
    Concept("touchdown", "Touchdown", "A touchdown is scored.",
            (r"touchdown", r"to the house", r"house call", r"in(?:to)? the end ?zone", r"pay ?dirt"),
            ("touchdown",), ("football",), 12.0, 16.0),
    Concept("dunk", "Dunk", "A huge slam dunk over the defender.",
            (r"dunk(?:s|ed)?", r"slam(?:s|med)?", r"\bjam(?:s|med)?\b", r"posteri[sz]e[sd]?", r"alley[- ]?oop", r"throws? it down"),
            ("dunk",), ("basketball",), 8.0, 12.0),
    Concept("knockout", "Knockout", "A knockout punch and the referee stops the fight.",
            (r"knock ?out", r"\bko\b", r"knocked (?:him|her) (?:out|down|cold)", r"down goes", r"(?:it'?s|this one is) (?:all )?over",
             r"stops? (?:it|the fight|the contest)", r"waves? it off", r"knock ?down", r"out cold", r"submission", r"taps? out"),
            (), ("combat",), 12.0, 20.0),
    Concept("crash", "Crash", "A big crash takes the cars out of the race.",
            (r"crash(?:es|ed)?", r"(?:into|hits|hit) the (?:wall|barrier|barriers|gravel)", r"spins? (?:out|off|round)", r"spun",
             r"collision", r"contact!?", r"(?:red flag|safety car)", r"takes? (?:him|her|them) out", r"pile[- ]?up", r"wreck"),
            (), ("motorsport",), 12.0, 25.0),
)}

# A feed kind -> the ideas it is (the reverse of Concept.feed), for scoring an event
_FEED_CONCEPTS: dict[str, list[str]] = {}
for _c in CONCEPTS.values():
    for _k in _c.feed:
        _FEED_CONCEPTS.setdefault(_k, []).append(_c.id)

# Said about the idea, without it happening: "that should have been a red", "he's already on a yellow"
_MENTION = re.compile(
    r"\b(?:should(?:'ve| have)? been|could(?:'ve| have)? been|might(?:'ve| have)? been|would(?:'ve| have)? been|"
    r"lucky (?:not|to)|(?:not|never|no|isn'?t|wasn'?t|won'?t be|shouldn'?t be|can'?t be) (?:a |an |the )?|escapes?|"
    r"gets? away|got away|is that (?:a|an)|was that (?:a|an)|already on (?:a|an)|on a yellow|walking a tightrope|"
    r"one more and|(?:asking|appealing|calling|screaming|looking) for (?:a|an|the)|want(?:s|ed)? (?:a|an|the)|"
    r"earlier|in the first half|last (?:week|season|time|month|year)|remember (?:the|that|when))\b[^.?!]{0,28}$", re.I)

_COMPILED: dict[str, re.Pattern] = {}


def pattern(c: Concept, asks: bool = False) -> re.Pattern:
    key = c.id + ("?" if asks else "")
    if key not in _COMPILED:
        words = c.words + (c.asks if asks else ())
        _COMPILED[key] = re.compile(r"(?<![\w'])(?:" + "|".join(words) + r")(?![\w'])", re.I)
    return _COMPILED[key]


def tags(text: str) -> list[str]:
    """The ideas a text names: what lets the word-matching embedder see that two phrasings mean one thing."""
    return [c.id for c in CONCEPTS.values() if pattern(c).search(text)]


# ---------------------------------------------------------------- the question

_PERIODS = (
    (r"\b(?:first|1st) half\b", 1, "soccer"), (r"\b(?:second|2nd) half\b", 2, "soccer"),
    (r"\b(?:first|1st|opening) (?:quarter|period)\b|\bq1\b", 1, ""), (r"\b(?:second|2nd) (?:quarter|period)\b|\bq2\b", 2, ""),
    (r"\b(?:third|3rd) (?:quarter|period)\b|\bq3\b", 3, ""), (r"\b(?:fourth|4th|final) (?:quarter|period)\b|\bq4\b", 4, ""),
)
_OVERTIME = re.compile(r"\b(?:overtime|extra[- ]time|\bot\b|golden goal|sudden death)\b", re.I)
_STOPPAGE = re.compile(r"\b(?:stoppage|injury|added|additional)[- ]time\b|\bdying (?:seconds|minutes|moments)\b", re.I)
_LATE = re.compile(r"\b(?:final|last|closing|dying) (?:(\d{1,2}|one|two|three|four|five|ten|couple of|few) )?"
                   r"(minute|second|moment)s?\b|\bat the (?:death|buzzer|end)\b|\blate (?:in|on)\b|\blast[- ](?:second|gasp|minute)\b",
                   re.I)
_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10, "couple of": 2, "few": 3}
_ORDER = re.compile(r"\b(after|following|before|then|leading to|leads to|resulting in|results in)\b", re.I)
_GAME = re.compile(r"\b(?:in|during|from|of)\s+(?:the\s+)?(.+)$", re.I)
_VS = re.compile(r"([A-Z0-9][\w.&'’-]*(?: [A-Z0-9][\w.&'’-]*){0,3})\s+(?:vs?\.?|versus|against|at|@)\s+"
                 r"([A-Z0-9][\w.&'’-]*(?: [A-Z0-9][\w.&'’-]*){0,3})")
_SERIES = re.compile(r"\bgame (\d|one|two|three|four|five|six|seven)\b|\b(?:final(?! (?:minute|second|moment|quarter|period|whistle|play|drive|lap|round))|semi[- ]?final|quarter[- ]?final|derby|"
                     r"el clasico|super bowl|world series|playoffs?)\b", re.I)


@dataclass
class Query:
    text: str
    alternatives: list[str] = field(default_factory=list)  # the question's "or" parts, each searched for
    concepts: list[str] = field(default_factory=list)  # ids of CONCEPTS, the question's main ideas
    after: list[str] = field(default_factory=list)  # ideas that should come just before it ("after a VAR check")
    before: list[str] = field(default_factory=list)  # ideas that should follow it
    period: int = 0  # 0 = any
    overtime: bool = False
    stoppage: bool = False
    last_seconds: float = 0.0  # of the game (or of `period`); 0 = any time
    home: str = ""
    away: str = ""
    teams: list[str] = field(default_factory=list)  # a team named on its own ("Chiefs")
    game: str = ""  # the question's words for the game ("Arsenal vs Chelsea", "game 7")
    phrases: list[str] = field(default_factory=list)  # other wordings to compare (the AI's)
    by: str = ""  # the model that helped read it

    @property
    def labels(self) -> list[str]:
        return [CONCEPTS[c].label for c in self.concepts if c in CONCEPTS]

    def describe(self) -> str:
        bits = [" or ".join(self.labels) or f"“{self.text}”"]
        if self.after:
            bits.append("after " + " or ".join(CONCEPTS[c].label.lower() for c in self.after if c in CONCEPTS))
        if self.period:
            bits.append(f"in period {self.period}")
        if self.overtime:
            bits.append("in overtime")
        if self.stoppage:
            bits.append("in stoppage time")
        if self.last_seconds:
            bits.append(f"in the last {self.last_seconds / 60:g} min" if self.last_seconds >= 60
                        else f"in the last {self.last_seconds:g} s")
        if self.home and self.away:
            bits.append(f"in {self.home} v {self.away}")
        elif self.teams:
            bits.append("involving " + ", ".join(self.teams))
        return ", ".join(bits)

    def to_json(self) -> dict:
        return asdict(self) | {"labels": self.labels, "describe": self.describe()}


def _concepts_in(text: str, sport: str = "") -> list[str]:
    found = []
    for c in CONCEPTS.values():
        if c.sports and sport and sport not in ("any", "") and sport not in c.sports:
            continue
        if pattern(c, asks=True).search(text):
            found.append(c.id)
    # The narrower idea wins over the one it's part of ("missed penalty" isn't also "penalty awarded").
    if "penalty_missed" in found and "penalty" in found:
        found.remove("penalty")
    if "technical_foul" in found or "flagrant" in found:  # "technical foul" isn't also any foul
        found = [f for f in found if f != "foul"]
    if "coach" in found and "technical_foul" in found:  # "technical foul on the bench" is the technical
        found.remove("coach")
    elif "coach" in found:  # "manager yellow card": the manager's booking, not any booking
        found = [f for f in found if f not in ("yellow_card", "red_card", "ejection")] or found
    return found


def parse(text: str, sport: str = "") -> Query:
    """Read a question with rules alone (no AI)."""
    raw = " ".join((text or "").split())
    q = Query(raw)
    body = raw
    # the game it's in: "... in Arsenal vs Chelsea", "... in Chiefs 4th quarter", "... during game 7"
    m = _VS.search(raw)
    if m:
        q.home, q.away = m[1].strip(), m[2].strip()
        q.game = m[0]
        body = (raw[:m.start()] + " " + raw[m.end():]).strip()
        body = re.sub(r"\b(?:in|during|from|of)\s*$", "", body).strip()
    g = _SERIES.search(raw)
    if g:
        q.game = (q.game + " " + g[0]).strip()
    low = body.lower()
    for rx, period, only in _PERIODS:
        if re.search(rx, low) and (not only or not sport or sport in (only, "any")):
            q.period = period
    q.overtime = bool(_OVERTIME.search(low))
    q.stoppage = bool(_STOPPAGE.search(low))
    late = _LATE.search(low)
    if late:
        n = late[1]
        count = (int(n) if n and n.isdigit() else _NUMBERS.get(n or "", 1)) if late[2] else 0
        unit_s = {"minute": 60.0, "second": 1.0, "moment": 30.0}.get(late[2] or "", 0.0)
        q.last_seconds = count * unit_s if unit_s else 120.0
        if late[2] == "second" and not n:
            q.last_seconds = 24.0
    # order: "A after B" / "B then A"
    main, cond = body, ""
    o = _ORDER.search(body)
    if o:
        left, right = body[:o.start()].strip(), body[o.end():].strip()
        word = o[1].lower()
        if word in ("after", "following"):
            main, cond = left, right
            q.after = _concepts_in(cond, sport)
        elif word == "before":
            main, cond = left, right
            q.before = _concepts_in(cond, sport)
        else:  # "VAR check then a red card": the first leads to the second, which is the moment
            main, cond = right, left
            q.after = _concepts_in(cond, sport)
        if not main or not _concepts_in(main, sport):
            main = body  # the order word wasn't about order ("following the ball")
            q.after, q.before = [], []
    q.alternatives = [a.strip(" ,.;") for a in re.split(r"\s+or\s+|\s*/\s*|;\s*", main) if a.strip(" ,.;")] or [main]
    for alt in q.alternatives:
        for cid in _concepts_in(alt, sport):
            if cid not in q.concepts:
                q.concepts.append(cid)
    q.after = [c for c in q.after if c not in q.concepts]
    # a team named on its own: capitalised words that aren't the start of the question or one of its ideas
    if not q.home:
        tail = _GAME.search(body)
        for name in re.findall(r"\b([A-Z][a-zA-Z.&'’-]+(?: [A-Z][a-zA-Z.&'’-]+){0,2})\b", tail[1] if tail else ""):
            if not _concepts_in(name) and name.lower() not in ("var", "ref", "referee", "ot") and not re.search(r"\b(?:League|Cup|Bowl|Series|Finals?|Game)\b", name) and name not in q.teams:
                q.teams.append(name)
        if q.teams and not q.game:
            q.game = " ".join(q.teams)
    return q


AI_SYSTEM = """You read a question about a sports game and say what moment it asks for, so a search can find it \
in the game's commentary. Answer only from the question."""

AI_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {"type": "array", "items": {"type": "string"},
                     "description": "ids from the list given that the wanted moment IS"},
        "after": {"type": "array", "items": {"type": "string"},
                  "description": "ids of what must happen just before it, if the question says so"},
        "phrases": {"type": "array", "items": {"type": "string"},
                    "description": "3 to 5 short sentences a TV commentator might say at that moment"},
        "home": {"type": "string"}, "away": {"type": "string"},
        "period": {"type": "integer", "description": "half/quarter/period number, 0 if not said"},
        "last_seconds": {"type": "integer", "description": "only the last N seconds of the game, 0 if not said"},
    },
    "required": ["concepts", "after", "phrases", "home", "away", "period", "last_seconds"],
    "additionalProperties": False,
}


def understand(text: str, sport: str = "", s=None, *, use_ai: bool = True, log: Log | None = None,
               check: Callable[[], None] | None = None) -> Query:
    """Read a question: the rules, and (when an AI is set up) the AI for what the rules don't see. The AI adds;
    it never takes away what the rules found. Local AI first (it's a small job), a cloud one if it can't."""
    q = parse(text, sport)
    if not use_ai or s is None:
        return q
    from ..intelligence import agent_router

    if not agent_router.available(s, "local_first"):
        return q
    listing = "\n".join(f"- {c.id}: {c.label}" for c in CONCEPTS.values()
                        if not c.sports or not sport or sport == "any" or sport in c.sports)
    try:
        got = agent_router.query_intelligence(
            f"Sport: {sport or 'not said'}\nQuestion: {text}\n\nIdeas you may name (id: meaning):\n{listing}",
            AI_SCHEMA, "local_first", system=AI_SYSTEM, settings=s, max_tokens=700, effort="low",
            local_timeout=45, log=None, check=check, what="read the question")
    except agent_router.RouterError as exc:
        if log:
            log(f"The AI couldn't read the question ({exc.tried[-1]['error'] if exc.tried else exc}); the search "
                "goes by its words.")
        return q
    d = got.data
    for cid in d.get("concepts") or []:
        if cid in CONCEPTS and cid not in q.concepts and cid not in q.after:
            q.concepts.append(cid)
    for cid in d.get("after") or []:
        if cid in CONCEPTS and cid not in q.after and cid not in q.concepts:
            q.after.append(cid)
    q.phrases = [" ".join(str(p).split())[:160] for p in (d.get("phrases") or []) if str(p).strip()][:5]
    if not q.home and d.get("home") and d.get("away"):
        q.home, q.away = str(d["home"])[:40], str(d["away"])[:40]
    if not q.period and isinstance(d.get("period"), int) and 0 < d["period"] <= 9:
        q.period = d["period"]
    if not q.last_seconds and isinstance(d.get("last_seconds"), int) and 0 < d["last_seconds"] <= 1800:
        q.last_seconds = float(d["last_seconds"])
    q.by = got.model
    return q


# ---------------------------------------------------------------- what it finds


@dataclass
class EventHit:
    id: str
    t_event: float  # video seconds: where the moment is
    start: float
    end: float
    score: float  # 0-100: how sure the search is that this is what was asked for
    concept: str  # CONCEPTS id ("" when it matched by meaning alone)
    label: str  # "Red card"
    why: str  # one sentence a person can check
    quote: str = ""  # what was said
    sources: list[str] = field(default_factory=list)  # commentary | feed | crowd | meaning
    certain: bool = True  # False: it's talked about, and may not have happened here
    feed: dict | None = None  # the feed's event, when one backs it
    badge: str = ""  # for a lower-third: "67' RED CARD · B. SAKA"
    parts: dict = field(default_factory=dict)  # the score's pieces, 0-100 each

    @property
    def length(self) -> float:
        return self.end - self.start

    def to_json(self) -> dict:
        return asdict(self) | {"length": round(self.length, 2)}


@dataclass
class SearchResult:
    query: Query
    hits: list[EventHit]
    notes: list[str] = field(default_factory=list)
    reader: str = ""  # the embedder's name
    device: str = ""
    feed: str = ""  # the feed's name, when one was used
    feed_events: int = 0
    windows: int = 0

    def to_json(self) -> dict:
        return {"query": self.query.to_json(), "hits": [h.to_json() for h in self.hits], "notes": self.notes,
                "reader": self.reader, "device": self.device, "feed": self.feed, "feed_events": self.feed_events,
                "windows": self.windows}


@dataclass
class Window:
    start: float
    end: float
    text: str


def windows(cues: list[Cue], duration: float, size: float = WINDOW, stride: float = STRIDE) -> list[Window]:
    """The transcript in overlapping stretches (only those where something is said)."""
    out: list[Window] = []
    if not cues:
        return out
    cues = sorted(cues, key=lambda c: c.start)
    end_all = min(duration or cues[-1].end, cues[-1].end + 1)
    t = max(0.0, (cues[0].start // stride) * stride)
    i = 0
    while t < end_all:
        while i < len(cues) and cues[i].end <= t:
            i += 1
        parts = []
        j = i
        while j < len(cues) and cues[j].start < t + size:
            parts.append(cues[j].text)
            j += 1
        text = text_between([Cue(0, 1, " ".join(parts))], 0, 1) if parts else ""
        if text:
            out.append(Window(t, min(t + size, end_all), text))
        t += stride
    return out


def _scale(cos: np.ndarray, reader: str) -> np.ndarray:
    lo, hi = SCALE.get(reader, SCALE["minilm"])
    return np.clip((cos - lo) / (hi - lo), 0.0, 1.0)


def _cache(s) -> Path | None:
    try:
        return Path(s.cache_path) / "embeddings"
    except Exception:  # noqa: BLE001 - no settings: don't cache
        return None


def embed_cached(embedder, texts: list[str], s=None, key: str = "") -> np.ndarray:
    """`texts` as vectors, kept on disk per video for the sentence model (a whole game is ~1,400 windows)."""
    if embedder.name != "minilm" or not key or s is None or not texts:
        return embedder.embed(texts)
    folder = _cache(s)
    digest = hashlib.sha1(("\n".join(texts)).encode("utf-8")).hexdigest()[:16]
    path = folder / f"{re.sub(r'[^\w-]', '_', key)[:60]}.{digest}.npy" if folder else None
    if path is not None and path.is_file():
        try:
            got = np.load(path)
            if got.shape[0] == len(texts):
                return got
        except (OSError, ValueError):
            pass
    got = embedder.embed(texts)
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            np.save(path, got)
        except OSError:
            pass
    return got


def _said(c: Concept, text: str) -> tuple[re.Match | None, bool]:
    """(where the idea is named in `text`, whether it's named as happening rather than talked about)."""
    for m in pattern(c).finditer(text):
        if not _MENTION.search(text[:m.start()]):
            return m, True
    m = pattern(c).search(text)
    return m, False


def _first_naming(c: Concept, cues: list[Cue], lo: float, hi: float) -> tuple[float, str, bool] | None:
    """(seconds, the sentence, happening?) of the first place between lo and hi the idea is named; a naming
    as happening is preferred to an earlier mere mention."""
    mention = None
    for cue in cues:
        if cue.end <= lo or cue.start >= hi:
            continue
        m, real = _said(c, cue.text)
        if m is None:
            continue
        # where in the sentence it's said, by the share of its characters (word times aren't always there)
        at = cue.start + (cue.end - cue.start) * (m.start() / max(1, len(cue.text)))
        if real:
            return at, cue.text, True
        mention = mention or (at, cue.text, False)
    return mention


def game_clock(t: float, duration: float, sport: str, anchors: list[align.Anchor]) -> tuple[int, float, bool] | None:
    """(period, seconds left in it, exact?) at video second `t`: from the anchors when there are any, else by
    the video's proportions (a whole game takes its periods in equal shares; a guess, and marked as one)."""
    from .feeds import CLOCKS

    periods, length, _down, ratio = CLOCKS.get(sport, (0, 0, True, 0.0))
    if not periods or not length:
        return None
    mine = sorted((a for a in anchors if a.video <= t), key=lambda a: a.video)
    if mine:
        a = mine[-1]
        elapsed = a.elapsed + (t - a.video) / (ratio or 1.0)
        if elapsed <= length * 1.05:  # past the period's end the anchor says nothing about now
            return a.period, max(0.0, length - elapsed), True
    if not align.is_full_game(duration, sport):
        return None
    share = min(0.9999, max(0.0, t / max(1.0, duration)))
    period = int(share * periods) + 1
    into = share * periods - (period - 1)
    return period, (1 - into) * length, False


def _fits_time(q: Query, clock: tuple[int, float, bool] | None, ev: FeedEvent | None, sport: str, t: float,
               duration: float) -> tuple[float, str]:
    """(a multiplier for the score, a note) for the question's "when" (period, overtime, final minute)."""
    from .feeds import CLOCKS

    if not (q.period or q.overtime or q.stoppage or q.last_seconds):
        return 1.0, ""
    periods, length, _down, _ratio = CLOCKS.get(sport, (0, 0, True, 0.0))
    if ev is not None and ev.period:  # the feed knows exactly
        period, left = ev.period, (max(0.0, length - ev.elapsed) if ev.elapsed is not None and length else None)
        exact = True
        stoppage = "+" in ev.minute
    elif clock is not None:
        period, left, exact = clock
        stoppage = False
    else:  # no clock at all: only "late in the game" can be judged, by where in the video it is
        if q.last_seconds and not q.period:
            return (1.0, "") if t >= 0.85 * duration else (0.55, "not near the end of the video")
        return 0.8, ""
    ok = True
    if q.period and period != q.period:
        ok = False
    if q.overtime and periods and period <= periods:
        ok = False
    if q.stoppage and ev is not None and not stoppage:
        ok = False
    if q.last_seconds and ok:
        last_period = period >= periods if not q.period else True
        if not last_period or (left is not None and left > q.last_seconds * (1.0 if exact else 2.5)):
            ok = False
    if ok:
        return 1.0, ""
    return (0.0, "") if exact else (0.5, "may not be at the time asked for")


def badge_for(ev: FeedEvent | None, c: Concept | None) -> str:
    """A lower-third's words: "90+4' WINNER · E. HAALAND" from the feed; else the idea's name."""
    if ev is None:
        return c.label.upper() if c else ""
    word = KIND_WORDS.get(ev.kind, (c.label if c else ev.kind).upper())
    who = _short_name(ev.players[0]) if ev.players else ""
    when = ev.minute or (f"Q{ev.period} {ev.clock}" if ev.clock and ev.period else "")
    return " ".join(x for x in (when, word) if x) + (f" · {who}" if who else "")


def _short_name(name: str) -> str:
    parts = name.split()
    if len(parts) >= 2 and len(parts[0]) > 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}".upper()
    return name.upper()


# ---------------------------------------------------------------- the cut


def snap_edges(start: float, end: float, t_event: float, cues: list[Cue], pauses, duration: float) -> tuple[float, float]:
    """Move the clip's ends so no sentence is cut: the start to the beginning of a sentence, the end to the
    end of one, each then into the nearest pause the sound shows."""
    for cue in cues:  # the start falls inside a sentence: take the whole sentence, or leave it out
        if cue.start < start < cue.end:
            start = cue.start if (start - cue.start <= 3.5 and cue.start < t_event) else min(cue.end, t_event - 2.0)
            break
    for cue in cues:
        if cue.start < end < cue.end:
            end = cue.end if cue.end - end <= 4.0 else max(cue.start, t_event + 3.0)
            break
    if pauses:
        start = _to_pause(start, pauses, 0.8, prefer_end=True)
        end = _to_pause(end, pauses, 0.8, prefer_end=False)
    start = max(0.0, min(start, t_event - 1.0))
    end = min(duration, max(end, t_event + 2.0)) if duration else max(end, t_event + 2.0)
    return round(start, 3), round(end, 3)


def _to_pause(t: float, pauses, reach: float, prefer_end: bool) -> float:
    runs = pauses.runs(t - reach, t + reach, 0.12)
    if not runs:
        return t
    a, b = min(runs, key=lambda r: abs((r[0] + r[1]) / 2 - t))
    # a clip starts at the end of a pause (just before the next word) and ends early in one
    return max(a, b - 0.12) if prefer_end else min(b, a + 0.2)


def cut(t_event: float, c: Concept | None, cues: list[Cue], duration: float, *, before: float, after: float,
        pauses=None, lead_from: float | None = None) -> tuple[float, float]:
    """The clip around a moment: its build-up, the moment, its aftermath."""
    b = before * ((c.before / PAD_BEFORE) if c else 1.0)
    a = after * ((c.after / PAD_AFTER) if c else 1.0)
    start = t_event - b
    if lead_from is not None and t_event - b - 35.0 <= lead_from < start + 4.0:
        start = lead_from - 4.0  # what led to it (the foul before the card) started a little earlier
    end = t_event + a
    if c is not None:  # still being talked about at the end: stay with it, a little
        more = 0.0
        while more < STRETCH:
            said = text_between(cues, end + more, end + more + 5.0)
            if not said or not pattern(c).search(said):
                break
            more += 5.0
        end += more
    start, end = max(0.0, start), (min(duration, end) if duration else end)
    return snap_edges(start, end, t_event, cues, pauses, duration)


# ---------------------------------------------------------------- the search


def event_match(embedder, reader: str, event_vecs: np.ndarray, window_vecs: np.ndarray, events: list[FeedEvent],
                wins: list[Window]) -> np.ndarray:
    """How well each feed event matches each window, 0-1: the meaning, lifted when the window names the event's
    own idea or its player."""
    cos = _scale(event_vecs @ window_vecs.T, reader) if len(events) and len(wins) else np.zeros((len(events), len(wins)))
    out = cos.copy()
    for i, ev in enumerate(events):
        ids = _FEED_CONCEPTS.get(ev.kind, [])
        names = [p.split()[-1].lower() for p in ev.players if p.split()]
        for j, w in enumerate(wins):
            low = w.text.lower()
            named = any(_said(CONCEPTS[c], w.text)[1] for c in ids)
            who = any(re.search(rf"\b{re.escape(n)}\b", low) for n in names if len(n) > 2)
            out[i, j] = min(1.0, cos[i, j] * 0.55 + (0.4 if named else 0.0) + (0.2 if who else 0.0)
                            + (0.1 if named and who else 0.0))
    return out


def search(query: str | Query, cues: list[Cue], duration: float, *, sport: str = "", s=None, fine=None,
           excitement: np.ndarray | None = None, feed: Feed | None = None, anchors: list[align.Anchor] | None = None,
           kind: str = "", embedder=None, limit: int = 5, use_ai: bool = True, cache_key: str = "",
           log: Log | None = None, check: Callable[[], None] | None = None,
           progress: Callable[[str, "float | None"], None] | None = None) -> SearchResult:
    """The moments of a video that match `query`, best first.

    `cues` is its transcript; `fine` its fine loudness (analyze.FINE frames), for cutting in pauses;
    `excitement` a 0-1 value per second (the crowd); `feed` its play-by-play (feeds.py) and `anchors` where its
    periods start (align.py); `kind` "full", "extended" or "highlights" when known. Nothing here downloads or
    reads the video itself."""
    from ..intelligence import embeddings

    log = log or (lambda m: None)
    check = check or (lambda: None)
    progress = progress or (lambda m, f: None)
    sport = sport if sport and sport != "any" else (feed.sport if feed and feed.sport not in ("", "any") else sport)
    q = query if isinstance(query, Query) else understand(query, sport, s, use_ai=use_ai, log=log, check=check)
    result = SearchResult(q, [])
    if embedder is None:
        embedder = embeddings.get(s, expand=tags, log=log, progress=progress)
    reader = embedder.name
    result.reader, result.device = getattr(embedder, "label", reader), getattr(embedder, "device", "")
    wins = windows(cues, duration)
    result.windows = len(wins)
    if not wins and feed is None:
        result.notes.append("This video has no commentary to search (no captions and no speech was found), and no "
                            "play-by-play feed.")
        return result
    if reader == "words" and wins:
        embedder.fit([w.text for w in wins])
    check()
    progress("Reading the commentary", None)
    wvecs = embed_cached(embedder, [w.text for w in wins], s, cache_key) if wins else np.zeros((0, 1), np.float32)
    check()
    concepts = [CONCEPTS[c] for c in q.concepts]
    asked = list(dict.fromkeys(q.alternatives + q.phrases + [c.says for c in concepts]))
    qvecs = embedder.embed(asked) if wins else np.zeros((0, 1), np.float32)
    # the question's own words count fully, the ideas' stock sentences a little less
    weights = np.array([1.0] * len(q.alternatives) + [0.95] * len(q.phrases) + [0.9] * len(concepts), np.float32)
    meaning = (_scale(qvecs @ wvecs.T, reader) * weights[:, None]).max(axis=0) if len(wins) and len(asked) else np.zeros(len(wins))

    all_anchors = list(anchors or []) + align.transcript_anchors(cues, sport, duration)
    pauses = None
    if fine is not None and len(fine):
        from ..cuts import pauses_in

        pauses = pauses_in(fine)
    before = float(getattr(s, "event_pad_before", PAD_BEFORE) or PAD_BEFORE)
    after = float(getattr(s, "event_pad_after", PAD_AFTER) or PAD_AFTER)

    def crowd(t: float, lo: float = -3.0, hi: float = 12.0) -> float:
        if excitement is None or not len(excitement):
            return 0.0
        a, b = max(0, int(t + lo)), min(len(excitement), int(t + hi) + 1)
        return float(np.max(excitement[a:b])) if b > a else 0.0

    # --- candidates from the commentary: windows that name the idea, or mean the question
    cands: list[dict] = []
    for j, w in enumerate(wins):
        best = None
        for c in concepts:
            m, real = _said(c, w.text)
            if m is not None and (best is None or (real and not best[1])):
                best = (c, real)
        sem = float(meaning[j])
        if best is None and sem < 0.6:
            continue
        lex = 0.0 if best is None else (1.0 if best[1] else 0.45)
        cands.append({"j": j, "t": w.start, "c": best[0] if best else None, "real": bool(best and best[1]) or best is None,
                      "lex": lex, "sem": sem, "w": w})
    # the same moment shows in three overlapping windows: keep one per run, where it's first named
    cands.sort(key=lambda d: d["t"])
    moments: list[dict] = []
    for d in cands:
        c = d["c"]
        if c is not None:
            got = _first_naming(c, cues, d["w"].start, d["w"].end)
            if got is None:
                continue
            d["at"], d["quote"], d["real"] = got
            d["lex"] = 1.0 if d["real"] else 0.45
        else:
            d["at"], d["quote"] = d["w"].start + WINDOW / 3, d["w"].text
        prev = moments[-1] if moments else None
        if prev is not None and d["at"] - prev["at"] <= MERGE_GAP and (prev["c"] is c or c is None or prev["c"] is None):
            # one moment: keep the surer naming and the higher meaning
            if (d["lex"], d["sem"]) > (prev["lex"], prev["sem"]) and d["at"] - prev["at"] < 1.0:
                prev.update(lex=d["lex"], quote=d["quote"], real=d["real"], c=c or prev["c"])
            prev["sem"] = max(prev["sem"], d["sem"])
            if prev["c"] is None and c is not None:
                prev.update(c=c, at=d["at"], quote=d["quote"], lex=d["lex"], real=d["real"])
            continue
        moments.append(d)

    # --- candidates from the feed: its events that are the idea, placed in the video
    placed: list[align.Placed] = []
    if feed is not None and feed.events:
        result.feed, result.feed_events = feed.name, len(feed.events)
        want_kinds = {k for c in concepts for k in c.feed}
        chosen = [e for e in feed.events if e.kind in want_kinds]
        if not chosen and asked:  # by meaning: the feed's own sentences against the question
            sentences = [e.sentence() for e in feed.events]
            if reader == "words":
                fvecs = embedder.embed(sentences)
            else:
                fvecs = embed_cached(embedder, sentences, s, cache_key + ".feed" if cache_key else "")
            fq = (_scale(embedder.embed(asked) @ fvecs.T, reader)).max(axis=0)
            chosen = [e for e, v in zip(feed.events, fq) if v >= 0.62][:12]
        if chosen:
            progress("Placing the play-by-play in the video", None)
            evecs = embedder.embed([e.sentence() for e in chosen]) if wins else np.zeros((0, 1), np.float32)
            strength = event_match(embedder, reader, evecs, wvecs, chosen, wins) if wins else np.zeros((len(chosen), 0))
            index = {id(e): i for i, e in enumerate(chosen)}
            starts = np.array([w.start for w in wins]) if wins else np.zeros(0)

            def match(ev: FeedEvent, lo: float, hi: float):
                if not len(starts):
                    return None
                inside = np.flatnonzero((starts >= lo - WINDOW / 2) & (starts <= hi))
                if not len(inside):
                    return None
                row = strength[index[id(ev)], inside]
                k = int(inside[int(np.argmax(row))])
                found = None
                for cid in _FEED_CONCEPTS.get(ev.kind, []):
                    found = found or _first_naming(CONCEPTS[cid], cues, wins[k].start, wins[k].end)
                return (found[0] if found else wins[k].start + WINDOW / 3), float(strength[index[id(ev)], k])

            def order_scores(evs: list[FeedEvent]):
                rows = np.array([strength[index[id(e)]] for e in evs]) if len(wins) else np.zeros((len(evs), 0))
                return rows, starts + WINDOW / 3

            placed, note = align.place(feed, chosen, duration, cues, anchors=anchors, kind=kind, match=match,
                                       order_scores=order_scores)
            if note:
                result.notes.append(note)
        elif want_kinds:
            result.notes.append(f"{feed.name}'s timeline for this game has no {' or '.join(q.labels).lower()}.")
    check()

    # --- one list: a feed event and a commentary moment at the same place are one hit
    hits: list[EventHit] = []
    used: set[int] = set()
    for p in placed:
        ev = p.event
        c = next((CONCEPTS[cid] for cid in _FEED_CONCEPTS.get(ev.kind, []) if cid in q.concepts), None) or (
            concepts[0] if concepts and ev.kind in {k for cc in concepts for k in cc.feed} else None)
        near = [(i, m) for i, m in enumerate(moments) if i not in used and abs(m["at"] - p.video) <= max(12.0, p.slack + 6.0)
                and (m["c"] is None or c is None or m["c"] is c or ev.kind in m["c"].feed)]
        t = p.video
        quote, sem, sources = "", 0.0, ["feed"]
        if near:
            i, m = min(near, key=lambda im: abs(im[1]["at"] - p.video))
            used.add(i)
            t, quote, sem = m["at"], m["quote"], m["sem"]
            sources.append("commentary")
        elif p.how in ("clock", "time of day") and excitement is not None and len(excitement) and p.slack > 12:
            # no words to go by: the crowd's loudest second inside the margin is the likeliest place
            a, b = max(0, int(t - p.slack)), min(len(excitement), int(t + p.slack) + 1)
            if b - a > 2 and float(np.max(excitement[a:b])) > 0.5:
                t = a + float(np.argmax(excitement[a:b]))
                sources.append("crowd")
        roar = crowd(t)
        if roar >= 0.6 and "crowd" not in sources:
            sources.append("crowd")
        mult, when_note = _fits_time(q, game_clock(t, duration, sport, all_anchors), ev, sport, t, duration)
        if mult == 0.0:
            continue
        placed_well = 1.0 if p.how in ("file", "commentary") or near else max(0.55, 1.0 - p.slack / 240.0)
        score = (0.62 + 0.2 * (1.0 if near else sem) + 0.1 * roar + 0.08) * placed_well * mult
        hits.append(_hit(len(hits), t, c, score, quote, sources, True, ev, q, cues, duration, before, after, pauses,
                         {"feed": 100, "meaning": round(sem * 100), "crowd": round(roar * 100),
                          "placed": round(placed_well * 100)}, when_note, p))
    for i, m in enumerate(moments):
        if i in used:
            continue
        c = m["c"]
        roar = crowd(m["at"])
        sources = (["commentary"] if c is not None else ["meaning"]) + (["crowd"] if roar >= 0.6 else [])
        mult, when_note = _fits_time(q, game_clock(m["at"], duration, sport, all_anchors), None, sport, m["at"], duration)
        if mult == 0.0:
            continue
        score = (0.5 * m["lex"] + 0.35 * m["sem"] + 0.15 * roar) * mult
        if c is None:
            score = (0.75 * m["sem"] + 0.15 * roar) * mult  # by meaning alone: needs a strong match to show
        hits.append(_hit(len(hits), m["at"], c, score, m["quote"], sources, m["real"], None, q, cues, duration, before,
                         after, pauses, {"said": round(m["lex"] * 100), "meaning": round(m["sem"] * 100),
                                         "crowd": round(roar * 100)}, when_note, None))

    # --- "after a VAR check": what should come just before (or after) lifts or lowers it
    for h in hits:
        for ids, lo, hi, word in ((q.after, -150.0, 5.0, "after"), (q.before, 0.0, 150.0, "before")):
            if not ids:
                continue
            said = text_between(cues, h.t_event + lo, h.t_event + hi)
            in_feed = feed is not None and any(pl.event.kind in {k for cid in ids for k in CONCEPTS[cid].feed}
                                               and lo <= pl.video - h.t_event <= hi for pl in placed)
            found = next((CONCEPTS[cid] for cid in ids if pattern(CONCEPTS[cid]).search(said)), None)
            if found is not None or in_feed:
                h.score = min(100.0, h.score * 1.15)
                h.why += f" It comes {word} {(found.label if found else CONCEPTS[ids[0]].label).lower()}."
                if word == "after" and found is not None:  # keep the review in the clip
                    got = _first_naming(found, cues, h.t_event + lo, h.t_event)
                    if got and h.start > got[0] - 4.0 and h.t_event - got[0] <= 60.0:
                        h.start, _ = snap_edges(max(0.0, got[0] - 6.0), h.end, h.t_event, cues, pauses, duration)
            else:
                h.score *= 0.6
                h.why += f" No {CONCEPTS[ids[0]].label.lower()} was heard {'before' if word == 'after' else 'after'} it."

    # --- best first; two that overlap are one
    hits = [h for h in hits if h.score >= QUALIFY * 100]
    hits.sort(key=lambda h: h.score, reverse=True)
    kept: list[EventHit] = []
    for h in hits:
        if any(h.start < k.end - 3.0 and h.end > k.start + 3.0 for k in kept):
            continue
        kept.append(h)
        if len(kept) >= max(1, min(limit, MAX_HITS)):
            break
    for n, h in enumerate(kept, 1):
        h.id = f"e{n}"
        h.score = round(min(100.0, h.score), 1)
    result.hits = kept
    if not kept:
        what = " or ".join(q.labels).lower() or "that"
        result.notes.append(f"Nothing in this video matches {what}"
                            + (" at the time asked for." if (q.period or q.last_seconds or q.overtime) and hits else ".")
                            + ("" if cues else " It has no commentary to read."))
    if not q.concepts and reader == "words":
        result.notes.append("The question names nothing the word search knows, so it matched loosely. The sentence "
                            "model (Settings → Sports) understands free wording.")
    return result


def _hit(n: int, t: float, c: Concept | None, score: float, quote: str, sources: list[str], real: bool,
         ev: FeedEvent | None, q: Query, cues: list[Cue], duration: float, before: float, after: float, pauses,
         parts: dict, when_note: str, p: align.Placed | None) -> EventHit:
    lead = None
    if c is not None and c.id in ("red_card", "yellow_card", "ejection", "penalty", "technical_foul", "flagrant"):
        got = _first_naming(CONCEPTS["foul"], cues, t - 45.0, t)  # the foul that drew the card
        lead = got[0] if got else None
    start, end = cut(t, c, cues, duration, before=before, after=after, pauses=pauses, lead_from=lead)
    label = c.label if c else (KIND_WORDS.get(ev.kind, ev.kind).title() if ev else "Matches the question")
    quote = " ".join(quote.split())
    short = quote if len(quote) <= 150 else quote[:147].rsplit(" ", 1)[0] + "…"
    if ev is not None:
        where = {"file": "at the time your file gives", "commentary": "where the commentary names it",
                 "clock": f"placed by the game clock (within about {p.slack:.0f} s)" if p else "",
                 "time of day": f"placed by the time of day it happened (within about {p.slack:.0f} s)" if p else "",
                 "order": "where the commentary names it, in the game's order"}.get(p.how if p else "", "")
        why = f"{ev.when + ': ' if ev.when else ''}{ev.text.rstrip('.')}, {where}."
        if "commentary" in sources and short:
            why += f" Said: “{short}”"
    elif c is not None and real:
        why = f"The commentary names it: “{short}”"
    elif c is not None:
        why = f"It's talked about here, and may not have happened: “{short}”"
    else:
        why = f"Close in meaning to the question: “{short}”"
    if when_note:
        why += f" ({when_note})"
    return EventHit(f"e{n + 1}", round(t, 2), start, end, round(min(100.0, max(0.0, score * 100)), 1), c.id if c else "",
                    label, why, short, sources, real, ev.to_json() if ev else None, badge_for(ev, c), parts)
