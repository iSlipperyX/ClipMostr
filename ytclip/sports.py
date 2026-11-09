"""Sports clips: find the plays in a match or a highlights video, and cut each one as a whole play.

A category with a sport (Categories → Sport) clips its videos with this instead of the usual signals:

* crowd      - the crowd erupting: loud against the last two minutes, noise-like (a roar, not one voice),
               and lasting a few seconds (a whistle or a shout from one fan doesn't).
* calls      - the commentator's big calls: "what a goal", "touchdown", "down goes…", "gooooal", per sport,
               marked over the seconds before them (the play happens just before the call).
* voice      - the commentator getting animated (analyze.voice_signal).
* replay     - "let's see that again", "another angle", "slow motion": the broadcast replays big moments,
               so the half minute before one is likely a highlight.
* aftermath  - a burst of camera cuts after a moment (close-ups, the bench, the replay angles).
* heatmap, reactions ([Cheering], [Applause]), audio and keywords as for any video.

A sports clip is cut around its moment, not grown out from the loudest seconds: a few seconds of build-up
before the crowd starts to rise, the moment, and the reaction until the roar dies down, stopping before the
replay (pick_moments). Edges aren't pulled in to the commentary (Trim dead air), since the crowd is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .analyze import HOP, SAMPLE_RATE, Clip, _per_second, _rolling, _smooth
from .subtitles import Cue


@dataclass(frozen=True)
class Sport:
    id: str
    label: str
    emoji: str
    lengths: tuple[float, float]  # a clip's shortest and longest, in seconds, unless the category says otherwise
    lead: float  # seconds of build-up kept before the crowd starts to rise
    tail: float  # seconds of reaction kept after the moment, at least
    calls: tuple[str, ...]  # the commentator's big calls (regular expressions, whole words)
    words: str  # what a highlight is, for the AI


GENERIC_CALLS = (
    r"what an? (?:goal|shot|save|play|catch|hit|finish|run|strike|ball|throw|pass|dunk|block|tackle|effort|move|"
    r"moment|kick|knock|putt|serve|return|finish|game|comeback|stop|grab)",
    r"oh my (?:god|goodness|word|days|gosh)", r"un-?believable", r"incredible", r"sensational", r"stunning",
    r"spectacular", r"magnificent", r"remarkable", r"astonishing", r"extraordinary", r"outrageous", r"ridiculous",
    r"are you (?:kidding|serious)", r"you (?:have|have got|'ve got|gotta) to be kidding", r"can you believe (?:it|that)",
    r"how did (?:he|she|they) (?:do|get|make|score)", r"no way", r"wow+", r"what a moment", r"world[- ]class",
    r"absolutely (?:brilliant|incredible|unreal|sensational|stunning)", r"unreal", r"insane", r"get in+",
    r"scenes", r"bedlam", r"pandemonium", r"the crowd (?:goes|is going|erupts) (?:wild|crazy|mad)", r"wins it",
    r"game[- ]winner", r"at the (?:death|buzzer)", r"in the final seconds", r"last[- ]gasp", r"stoppage time",
)

SPORTS: dict[str, Sport] = {s.id: s for s in (
    Sport("any", "Any sport", "🏆", (12, 40), 10, 7, (), "the best plays: scores, saves, big hits, skill, comebacks, "
          "game winners, fights and bloopers"),
    Sport("soccer", "Soccer (football)", "⚽", (15, 40), 12, 8, (
        r"go+a+l+", r"scores?", r"scored", r"back of the net", r"top (?:corner|bins|shelf)", r"bicycle kick",
        r"overhead kick", r"volley", r"header", r"free[- ]kick", r"penalty", r"spot[- ]kick", r"hat[- ]trick",
        r"equali[sz]er", r"the winner", r"own goal", r"what a save", r"(?:fingertip|double) save", r"red card",
        r"off the (?:post|bar|crossbar|woodwork)", r"nutmeg", r"screamer", r"worldie", r"rocket", r"chip(?:ped)?",
        r"solo (?:goal|run)", r"tiki[- ]taka", r"sends? (?:him|the keeper) the wrong way",
    ), "goals, great saves, skill moves, near misses off the post, red cards and last-minute winners"),
    Sport("basketball", "Basketball", "🏀", (10, 30), 8, 6, (
        r"dunk", r"slam", r"jam", r"posteri[sz]ed?", r"alley[- ]?oop", r"three(?:[- ]pointer)?", r"from (?:way )?downtown",
        r"buzzer[- ]beater", r"at the buzzer", r"and one", r"and[- ]1", r"bang+", r"splash", r"swish", r"crossover",
        r"broke his ankles", r"ankle breaker", r"block(?:ed)?", r"rejected", r"get that (?:out|outta)", r"stuff(?:ed)?",
        r"steal", r"fast break", r"triple[- ]double", r"game[- ]winner", r"no look", r"behind the back",
        r"from half ?court", r"logo", r"count it", r"bucket",
    ), "dunks, deep threes, buzzer beaters, blocks, ankle-breaking crossovers and no-look passes"),
    Sport("football", "American football", "🏈", (12, 35), 10, 7, (
        r"touchdown", r"td", r"interception", r"intercepted", r"picked off", r"pick[- ]six", r"sack(?:ed)?",
        r"fumble", r"strip sack", r"field goal", r"it's good", r"hail mary", r"one[- ]handed catch", r"what a catch",
        r"toe tap", r"juke", r"stiff[- ]arm", r"house call", r"to the house", r"end zone", r"breaks? (?:a|the) tackle",
        r"broken tackle", r"he is gone", r"he's gone", r"could go all the way", r"walk[- ]off", r"two[- ]point",
        r"blocked (?:punt|kick)", r"safety", r"trick play", r"flea flicker",
    ), "touchdowns, interceptions, sacks, one-handed catches, long runs and game-winning kicks"),
    Sport("baseball", "Baseball", "⚾", (10, 30), 6, 7, (
        r"home ?run", r"homer", r"grand slam", r"(?:it|that ball) is gone", r"gone+", r"outta here", r"see ya",
        r"deep (?:to|into) (?:left|right|center)", r"strike ?out", r"struck (?:him|her) out", r"strike three",
        r"punch(?:ed)? out", r"diving catch", r"robbed", r"over the wall", r"double play", r"triple play",
        r"walk[- ]off", r"base hit", r"rbi", r"steals? (?:second|third|home)", r"perfect game", r"no[- ]hitter",
    ), "home runs, walk-offs, diving catches, robbed homers, strikeouts and double plays"),
    Sport("hockey", "Ice hockey", "🏒", (12, 35), 10, 7, (
        r"go+a+l+", r"scores?", r"scored", r"snipe", r"top shelf", r"where mama keeps", r"five[- ]hole",
        r"glove save", r"what a save", r"robbed", r"breakaway", r"shoot[- ]?out", r"deke", r"between the legs",
        r"michigan", r"lacrosse", r"hat[- ]trick", r"overtime winner", r"ot winner", r"fight", r"drop(?:ped)? the gloves",
        r"big hit", r"huge hit", r"one[- ]timer", r"empty net",
    ), "goals, snipes, glove saves, breakaways, big hits and fights"),
    Sport("tennis", "Tennis", "🎾", (12, 45), 10, 6, (
        r"ace", r"winner", r"match point", r"set point", r"break point", r"championship point", r"tweener",
        r"between the legs", r"passing shot", r"drop shot", r"lob", r"what a rally", r"what a point", r"point of the",
        r"on the line", r"hawk[- ]?eye", r"challenge", r"game,? set,? (?:and )?match",
    ), "long rallies, aces, tweeners, passing shots, match points and dramatic challenges"),
    Sport("combat", "Boxing / MMA", "🥊", (10, 35), 8, 8, (
        r"knock ?out", r"ko+", r"knocked (?:him|her) out", r"down goes", r"(?:he|she)'?s down", r"(?:he|she) is down",
        r"knock ?down", r"(?:it'?s|this one is) (?:all )?over", r"stops? (?:it|the fight)", r"submission", r"taps?",
        r"tapped", r"choke", r"arm ?bar", r"triangle", r"rear naked", r"uppercut", r"left hook", r"right hook",
        r"right hand", r"head kick", r"spinning (?:back )?(?:fist|elbow|kick)", r"flying knee", r"walk[- ]off knockout",
        r"saved by the bell", r"one punch",
    ), "knockouts, knockdowns, submissions, spinning strikes and big comebacks"),
    Sport("golf", "Golf", "⛳", (10, 30), 8, 6, (
        r"hole[- ]in[- ]one", r"ace", r"albatross", r"eagle", r"birdie", r"chip[- ]in", r"holes? (?:it|out)",
        r"in the hole", r"drains? it", r"get in the hole", r"(?:long|huge|massive) putt", r"for the win",
        r"playoff", r"into the water", r"off the (?:pin|flag)",
    ), "holes-in-one, chip-ins, long putts, eagles and shots that hit the pin"),
    Sport("cricket", "Cricket", "🏏", (10, 30), 6, 6, (
        r"six", r"maximum", r"four", r"boundary", r"wicket", r"bowled (?:him|her)?", r"clean bowled", r"caught",
        r"what a catch", r"stumped", r"run out", r"century", r"hundred", r"hat[- ]trick", r"yorker", r"bouncer",
        r"out of the ground", r"lbw", r"that's out",
    ), "sixes, wickets, stunning catches, run outs, centuries and hat-tricks"),
    Sport("motorsport", "Motorsport", "🏎", (12, 40), 8, 7, (
        r"overtake", r"overtaking", r"pass(?:es)? (?:him|her|for)", r"side by side", r"wheel to wheel", r"crash",
        r"spins?", r"spun", r"into the (?:wall|barrier|gravel)", r"contact", r"takes the lead", r"into the lead",
        r"chequered flag", r"checkered flag", r"wins the (?:race|grand prix)", r"photo finish", r"pole position",
        r"last lap", r"red flag", r"safety car",
    ), "overtakes, crashes, side-by-side battles, photo finishes and last-lap drama"),
)}

REPLAY = re.compile(r"\b(?:let'?s (?:see|look at|have a look at|watch) (?:that|it|this) again|replay|another (?:look|angle)|"
                    r"slow[- ]?mo(?:tion)?|watch (?:that|this|it) again|one more (?:time|look)|look at (?:that|this) again|"
                    r"from (?:behind|above|the other side|this angle|another angle))\b", re.I)

# The weight of each signal for sports, in place of Settings → Picking's sliders (keywords keeps its own).
WEIGHTS = {"crowd": 0.55, "calls": 0.45, "heatmap": 0.45, "voice": 0.30, "replay": 0.25, "reactions": 0.25,
           "aftermath": 0.15, "audio": 0.15, "emotion": 0.05, "scenes": 0.0}

LABELS = {"crowd": "Crowd roar", "calls": "Commentator calls", "replay": "Replays", "aftermath": "Close-ups after"}


def get(sport_id: str) -> Sport | None:
    """The sport called `sport_id` ("" or unknown: not a sport)."""
    return SPORTS.get((sport_id or "").strip().lower())


def choices() -> list[tuple[str, str]]:
    """(id, label) of every sport, for a picker; the "not sports" choice is the caller's."""
    return [(s.id, f"{s.emoji}  {s.label}") for s in SPORTS.values()]


def calls_pattern(sport: Sport) -> re.Pattern:
    words = sport.calls + GENERIC_CALLS if sport.id != "any" else (
        GENERIC_CALLS + tuple(dict.fromkeys(c for s in SPORTS.values() for c in s.calls)))
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


# ---------------------------------------------------------------- measuring


def spectral_flatness(frames: np.ndarray, rate: int = SAMPLE_RATE) -> np.ndarray:
    """How noise-like each row of `frames` sounds, 0-1, over 300-3800 Hz: a crowd roaring is near 0.3-0.6,
    one voice or music near 0.02-0.1. Silent rows give 0."""
    if not len(frames):
        return np.zeros(0, np.float32)
    x = frames.astype(np.float32)
    x = x * np.hanning(x.shape[1]).astype(np.float32)
    power = np.abs(np.fft.rfft(x, axis=1)) ** 2
    freqs = np.fft.rfftfreq(x.shape[1], 1 / rate)
    band = power[:, (freqs >= 300) & (freqs <= 3800)] + 1e-3
    arith = band.mean(axis=1)
    geo = np.exp(np.log(band).mean(axis=1))
    loud = arith > 1e2  # under that it's silence, which is flat but isn't a crowd
    return np.where(loud, geo / arith, 0.0).astype(np.float32)


# ---------------------------------------------------------------- signals


def _robust01(x: np.ndarray, lo_pct: float = 50, hi_pct: float = 99.5) -> np.ndarray | None:
    """0 at the typical value, 1 at the top few: peaks keep their shape (unlike ranks) and one freak
    second doesn't squash the rest (unlike min-max)."""
    if not len(x):
        return None
    lo, hi = np.percentile(x, lo_pct), np.percentile(x, hi_pct)
    if hi - lo < 1e-6:
        return None
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def crowd_signal(db: np.ndarray, flatness: np.ndarray, n: int) -> np.ndarray | None:
    """The crowd erupting, per second."""
    if not len(db):
        return None
    sec = _per_second(db, n)
    baseline = _rolling(sec, min(n, 120), np.median)  # what this part of the match sounds like
    surge = np.clip(sec - baseline, 0.0, None)  # dB louder than that
    if len(flatness):
        flat = _per_second(flatness, n)
        noisy = np.clip(flat / max(1e-6, float(np.percentile(flat, 90))), 0.0, 1.0)
        surge = surge * (0.35 + 0.65 * noisy)
    sustained = _rolling(surge, 3, np.min)  # a roar lasts; a whistle or a door doesn't
    roar = 0.7 * _rolling(sustained, 3, np.max) + 0.3 * _smooth(surge, 3)
    return _robust01(roar, 60, 99.5)


def _mark(cues: list[Cue], n: int, pattern: re.Pattern, before: float, after: float,
          weight=lambda m: 1.0) -> tuple[np.ndarray, list[tuple[float, str]]]:
    sig = np.zeros(n)
    hits: list[tuple[float, str]] = []
    for cue in cues:
        for m in pattern.finditer(cue.text):
            a, b = max(0, int(cue.start - before)), min(n, int(np.ceil(cue.end + after)))
            if a < b:
                sig[a:b] = np.maximum(sig[a:b], weight(m))
                hits.append((cue.start, m.group(0)))
    return sig, hits


def calls_signal(cues: list[Cue], n: int, sport: Sport) -> np.ndarray | None:
    """The commentator's big calls, over the seconds before them (where the play is) and just after."""
    pattern = calls_pattern(sport)
    # A shouted, stretched call ("gooooal", "wowww") counts double.
    sig, hits = _mark(cues, n, pattern, 8, 2, lambda m: 1.0 if re.search(r"(\w)\1\1", m.group(0)) else 0.7)
    if not hits:
        return None
    # Several calls close together is a bigger moment than one.
    return np.clip(_smooth(sig, 3) * (0.6 + 0.4 * np.clip(_rolling(sig > 0, 20, np.mean) * 3, 0, 1)) / 0.7, 0, 1)


def replay_signal(cues: list[Cue], n: int) -> np.ndarray | None:
    """The half minute before the broadcast says it's showing a replay."""
    sig, hits = _mark(cues, n, REPLAY, 30, -3)
    return np.clip(_smooth(sig, 5), 0, 1) if hits else None


def replay_times(cues: list[Cue]) -> list[float]:
    return [c.start for c in cues if REPLAY.search(c.text)]


def aftermath_signal(cuts: list[float], n: int) -> np.ndarray | None:
    """A burst of camera cuts in the 20 seconds after each second: close-ups, the bench and replay angles
    follow a big moment. Credited to the moment, before the burst."""
    if len(cuts) < 6:
        return None
    counts = np.zeros(n + 25)
    for t in cuts:
        if 0 <= t < n:
            counts[int(t)] += 1
    ahead = np.convolve(counts, np.ones(20), mode="full")[19:19 + n + 25]  # cuts in [s, s+20)
    after = np.roll(ahead, -2)[:n]  # (s+2, s+22]
    return _robust01(_smooth(after, 3), 60, 99)


def build_signals(m, sport: Sport, keywords: list[str]) -> dict[str, np.ndarray]:
    """analyze.build_signals plus the sports signals; the plain scene-cut density is left out."""
    from .analyze import build_signals as base

    out = {k: v for k, v in base(m, keywords).items() if k != "scenes"}
    n = m.seconds
    extra = {"crowd": crowd_signal(m.db, getattr(m, "flatness", np.zeros(0)), n),
             "calls": calls_signal(m.cues, n, sport),
             "replay": replay_signal(m.cues, n),
             "aftermath": aftermath_signal(m.scene_cuts, n)}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def weights(user: dict[str, float]) -> dict[str, float]:
    """The sports weights, with your Keywords slider."""
    return {**WEIGHTS, "keywords": user.get("keywords", 0.3)}


# ---------------------------------------------------------------- cutting each play


def pick_moments(score: np.ndarray, crowd: np.ndarray | None, count: int, min_len: float, max_len: float,
                 sport: Sport, gap: float = 8.0, floor: float = 0.4, replays: list[float] = ()) -> list[Clip]:
    """The `count` best moments, each cut as a whole play: `sport.lead` seconds of build-up before the
    reaction starts, the moment, and the reaction until it dies down (at least `sport.tail` seconds), ending
    before a replay starts. Clips stay within min_len..max_len and `gap` seconds apart."""
    n = len(score)
    if n == 0:
        return []
    if n <= min_len:
        return [Clip(0.0, float(n), round(float(score.mean()) * 100, 1), origin="auto")]
    peaky = _smooth(score, 3)
    ref = _smooth(crowd, 2) if crowd is not None and len(crowd) == n else peaky
    taken = np.zeros(n, bool)
    clips: list[Clip] = []
    replays = sorted(replays)
    while len(clips) < count:
        cand = np.where(taken, -np.inf, peaky)
        peak = int(np.argmax(cand))
        if not np.isfinite(cand[peak]) or cand[peak] <= 0:
            break
        # The loudest second of the reaction near the peak, and where the reaction started climbing to it.
        top = peak
        for t in range(peak, min(n, peak + 6)):
            if taken[t]:
                break
            if ref[t] > ref[top]:
                top = t
        onset = top
        quiet = 0.15 * ref[top]  # below this the crowd hasn't started reacting yet
        while (onset > max(0, top - 15) and not taken[onset - 1] and ref[onset - 1] > quiet
               and ref[onset - 1] <= ref[onset] + 0.03):
            onset -= 1
        start = float(onset) - sport.lead
        end = float(max(top, peak)) + sport.tail
        # the reaction goes on while the crowd is still well up
        while end < n - 1 and ref[int(end)] > 0.45 * ref[top] and end - start < max_len and not taken[int(end)]:
            end += 1.0
        nxt = next((r for r in replays if top + 2 < r < end), None)
        if nxt is not None:  # stop where the replay starts
            end = max(float(top) + 3.0, nxt - 0.5)
        # inside the video and clear of the clips already picked
        left = onset
        while left > 0 and not taken[left - 1] and left > start:
            left -= 1
        start = max(start, float(left))
        right = int(min(end, n))
        stop = int(top)
        while stop < right and not taken[min(n - 1, stop)]:
            stop += 1
        end = min(end, float(stop), float(n))
        if end - start > max_len:  # too long: less reaction first, then less build-up
            end = max(float(top) + 3.0, start + max_len)
            start = max(start, end - max_len)
        if end - start < min_len:  # too short: more build-up (up to twice as much), then more reaction
            want = min_len - (end - start)
            room = 0
            while room < min(want, sport.lead) and start - room - 1 >= 0 and not taken[int(start - room - 1)]:
                room += 1
            start -= room
            while end - start < min_len and end < n and not taken[min(n - 1, int(end))]:
                end += 1.0
        start, end = max(0.0, start), min(float(n), end)
        if end - start < min(min_len, n) - 0.5:  # squeezed in between earlier picks
            taken[max(0, peak - 2): min(n, peak + 3)] = True
            continue
        seg = score[int(start): max(int(start) + 1, int(end))]
        value = 0.6 * float(peaky[peak]) + 0.4 * float(seg.mean() if len(seg) else 0)
        clips.append(Clip(round(start, 2), round(end, 2), round(value * 100, 1), origin="auto"))
        taken[max(0, int(start - gap)): min(n, int(end + gap))] = True
    best = max((c.score for c in clips), default=0)
    clips = [c for c in clips if c.score >= best * floor]
    clips.sort(key=lambda c: c.score, reverse=True)
    return clips


def reason(clip: Clip, signals: dict[str, np.ndarray], cues: list[Cue], sport: Sport) -> str:
    """Why this moment, in a few words: 'Crowd roar · commentator: "what a goal"'."""
    a, b = int(clip.start), max(int(clip.start) + 1, int(clip.end))
    names = {"crowd": "crowd roar", "heatmap": "most replayed", "voice": "excited commentary", "replay": "replayed",
             "reactions": "cheering", "aftermath": "close-ups after", "audio": "loud"}
    strong = sorted(((float(np.max(signals[k][a:b])), k) for k in names if k in signals and len(signals[k][a:b])),
                    reverse=True)
    parts = [names[k] for v, k in strong if v >= 0.6][:2]
    pattern = calls_pattern(sport)
    said = next((m.group(0) for c in cues if clip.start - 1 <= c.start <= clip.end for m in [pattern.search(c.text)] if m),
                "")
    if said:
        parts.append(f"commentator: “{said.lower()}”")
    return (sport.emoji + " " + " · ".join(parts)).strip() if parts else sport.emoji


def ai_guide(sport: Sport) -> str:
    """What to tell the AI picking clips from a sports video."""
    what = sport.label.lower() if sport.id != "any" else "sports"
    return (f"This is {what} footage. Each clip is one whole play: start a few seconds before the play begins "
            f"(the build-up, so a viewer sees it coming), include the moment itself and the reaction or celebration, "
            f"and end before the replay. Pick {sport.words}. The crowd getting loud and the commentator's calls mark "
            f"the moments. Skip studio talk, interviews, ads, line-ups and quiet spells of play.")


def polish_guide(sport: Sport) -> str:
    """What to tell the AI polish about the hooks for sports clips."""
    return (f"These are {sport.label.lower() if sport.id != 'any' else 'sports'} highlights. A hook builds suspense "
            "about the play without giving away how it ends (\"Watch the keeper…\", \"He had no business making "
            "this\"), and names a player or team only when the transcript does.")


def outward(start: float, end: float, sents: list[Cue], max_len: float, duration: float,
            reach: float = 3.0) -> tuple[float, float]:
    """Edges that don't cut the commentator off mid-sentence, moving only outwards (never into the
    build-up or the reaction): the start back to the start of the sentence it falls in, the end on to the
    end of its sentence, each only when that's within `reach` seconds and the clip stays within max_len."""
    for s in sents:
        if s.start < start < s.end and start - s.start <= reach and end - s.start <= max_len:
            start = max(0.0, s.start - 0.1)
            break
    for s in sents:
        if s.start < end < s.end and s.end - end <= reach and s.end - start <= max_len:
            end = min(duration, s.end + 0.2)
            break
    return start, end
