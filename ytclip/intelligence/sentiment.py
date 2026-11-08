"""A small, fast sentiment scorer for transcripts, with no model to download.

A word list in the spirit of VADER: each word has a valence from -4 (very negative) to +4 (very
positive). Intensifiers ("so", "really", "insanely") make the next word count for more, a negation
("not", "never", "n't") flips the next few words, and "!" or ALL CAPS add weight. The score of a line
is squashed to -1..+1.

What makes a moment clippable is less the sign than the swing: an emotional delta (a calm line followed
by an outraged or delighted one) is where people stop scrolling. viral_heuristics.py uses both.
"""

from __future__ import annotations

import math
import re

VALENCE: dict[str, float] = {
    # strong positive
    "amazing": 3.2, "awesome": 3.1, "incredible": 3.2, "insane": 2.0, "unbelievable": 2.4, "love": 3.0,
    "loved": 2.9, "best": 3.0, "perfect": 3.0, "beautiful": 2.9, "brilliant": 3.0, "legendary": 3.0,
    "genius": 3.0, "wow": 2.8, "omg": 2.0, "hilarious": 2.9, "fantastic": 3.2, "excellent": 3.0,
    "won": 2.7, "win": 2.8, "winning": 2.6, "rich": 2.0, "millionaire": 2.2, "proud": 2.4, "happy": 2.7,
    "excited": 2.6, "crazy": 1.2, "wild": 1.4, "huge": 1.6, "massive": 1.6, "great": 3.1, "good": 1.9,
    "nice": 1.8, "cool": 1.3, "funny": 1.9, "laugh": 2.0, "laughing": 2.0, "lol": 1.8, "yes": 1.2,
    "finally": 1.4, "free": 1.5, "success": 2.6, "successful": 2.6, "thank": 1.5, "thanks": 1.5,
    "respect": 2.0, "goat": 2.5, "fire": 1.6, "clutch": 2.2, "epic": 2.6, "blessed": 2.5, "dream": 2.0,
    # negative
    "hate": -2.7, "hated": -2.7, "worst": -3.1, "terrible": -2.9, "horrible": -3.0, "awful": -2.9,
    "disgusting": -3.0, "scary": -2.2, "terrifying": -2.8, "afraid": -2.2, "fear": -2.2, "angry": -2.6,
    "furious": -3.0, "mad": -2.0, "sad": -2.3, "cry": -2.0, "crying": -2.2, "died": -2.9, "dead": -2.7,
    "death": -2.9, "die": -2.8, "kill": -3.0, "killed": -3.1, "murder": -3.3, "fight": -1.8,
    "fighting": -1.8, "war": -2.6, "broke": -2.0, "bankrupt": -2.7, "lost": -2.0, "lose": -2.0,
    "losing": -2.0, "fail": -2.4, "failed": -2.5, "failure": -2.6, "scam": -2.9, "scammed": -3.0,
    "fraud": -2.9, "lie": -2.3, "lied": -2.5, "lying": -2.4, "liar": -2.8, "exposed": -1.9, "fired": -2.4,
    "arrested": -2.7, "jail": -2.6, "prison": -2.6, "crash": -2.4, "crashed": -2.5, "disaster": -3.1,
    "problem": -1.6, "wrong": -2.1, "bad": -2.5, "stupid": -2.4, "dumb": -2.2, "idiot": -2.6,
    "ridiculous": -1.8, "insult": -2.3, "betrayed": -3.0, "cheated": -2.7, "pain": -2.4, "hurt": -2.3,
    "shocked": -1.0, "shocking": -1.4, "no": -1.0, "never": -0.8, "nightmare": -2.9, "toxic": -2.6,
    "cringe": -1.8, "embarrassing": -2.0, "weird": -0.8,
}
INTENSIFIERS = {"so": 0.3, "very": 0.3, "really": 0.3, "extremely": 0.5, "insanely": 0.6, "incredibly": 0.5,
                "absolutely": 0.5, "totally": 0.4, "completely": 0.4, "literally": 0.2, "super": 0.4,
                "most": 0.3, "freaking": 0.5, "fucking": 0.6, "damn": 0.4, "hella": 0.4}
NEGATIONS = {"not", "no", "never", "nobody", "nothing", "neither", "nor", "without", "cannot", "dont", "didnt",
             "doesnt", "isnt", "wasnt", "arent", "werent", "wont", "wouldnt", "shouldnt", "couldnt", "cant"}
_TOKEN = re.compile(r"[A-Za-z']+|[!?]")
ALPHA = 15.0  # VADER's normalisation constant


def score(text: str) -> float:
    """Sentiment of a line, -1 (very negative) to +1 (very positive)."""
    tokens = _TOKEN.findall(text or "")
    total, boost, negate = 0.0, 0.0, 0
    bangs = 0
    for tok in tokens:
        if tok == "!":
            bangs += 1
            continue
        if tok == "?":
            continue
        low = tok.lower().replace("'", "")
        if low in NEGATIONS:
            negate = 3
            continue
        if low in INTENSIFIERS:
            boost += INTENSIFIERS[low]
            continue
        v = VALENCE.get(low)
        if v is not None:
            v *= 1 + boost
            if tok.isupper() and len(tok) > 1:
                v *= 1.3
            if negate:
                v *= -0.74
            total += v
        boost = 0.0
        negate = max(0, negate - 1)
    if total:
        total += math.copysign(min(bangs, 4) * 0.29, total)
    return total / math.sqrt(total * total + ALPHA)


def intensity(text: str) -> float:
    """How emotional a line is, whichever way: 0 to 1."""
    return abs(score(text))
