"""Kinetic captions: one to three words at a time, popping in, the spoken word lit up, keywords coloured.

Each caption group pops in (80% → 110% → 100% over 150 ms, ASS \\t transforms). As the words are spoken, the
active one lights up in the highlight colour and gives a small pop of its own, karaoke style; the others
stay white with a heavy black outline. Keywords are coloured by meaning:

* money ($, dollars, million, rich, paid...) in electric green,
* danger and conflict (fight, died, broke, exposed...) in red,
* questions and curiosity (why, how, secret, words ending in ?) in yellow.

Strong swearing is masked (f*ck) and, in the sound, bleeped (filtergraph.py). Captions sit in TikTok's safe
zone: centred, between y = 700 and 1200 on a 1080x1920 frame, clear of the buttons on the right and the
caption text at the bottom (captions.caption_style places the block).
"""

from __future__ import annotations

import re

MONEY = {"money", "cash", "dollar", "dollars", "million", "millions", "billion", "billions", "rich", "paid", "pay",
         "salary", "profit", "profits", "price", "cost", "costs", "worth", "bucks", "grand", "income", "revenue",
         "invest", "investing", "investment", "stocks", "crypto", "bitcoin", "debt", "loan", "budget", "wealth",
         "millionaire", "billionaire", "expensive", "cheap", "free", "sale", "sold", "bank", "earn", "earned"}
DANGER = {"fight", "fighting", "fought", "died", "die", "dead", "death", "kill", "killed", "killing", "broke",
          "exposed", "war", "crash", "crashed", "scam", "scammed", "arrested", "fired", "lost", "danger",
          "dangerous", "attack", "attacked", "murder", "bankrupt", "destroyed", "fail", "failed", "worst",
          "never", "stop", "warning", "illegal", "banned", "lie", "lied", "liar", "fraud", "hate", "enemy"}
QUESTION = {"why", "how", "what", "who", "secret", "secrets", "nobody", "truth", "mystery", "hidden", "really",
            "actually", "imagine", "guess", "wonder", "question", "answer", "reason", "crazy", "insane"}
DEFAULT_COLOURS = {"money": "#00FF66", "danger": "#FF3B30", "question": "#FFE600"}
_MONEY_NUMBER = re.compile(r"^\$|^\d[\d,.]*[km]?$|\d+k$", re.I)
_BARE = re.compile(r"[^\w$']")


def keyword_kind(word: str) -> str:
    """"money" | "danger" | "question" | "" for one caption word."""
    raw = word.strip()
    bare = _BARE.sub("", raw.lower()).strip("'")
    if not bare:
        return ""
    if raw.startswith("$") or _MONEY_NUMBER.match(bare) and ("$" in raw or bare.endswith(("k", "m"))):
        return "money"
    if bare in MONEY:
        return "money"
    if bare in DANGER:
        return "danger"
    if raw.rstrip("\"'”’)").endswith("?") or bare in QUESTION:
        return "question"
    return ""


def colours(s) -> dict[str, str]:
    return {"money": getattr(s, "caption_money_colour", "") or DEFAULT_COLOURS["money"],
            "danger": getattr(s, "caption_danger_colour", "") or DEFAULT_COLOURS["danger"],
            "question": getattr(s, "caption_question_colour", "") or DEFAULT_COLOURS["question"]}


def bare(word: str) -> str:
    """A caption word as a plain lower-case key ("Okay?" -> "okay"), for matching emphasis words."""
    return _BARE.sub("", word.lower()).strip("'")


def kinetic_line(words: list[str], active: int, first: bool, s, ass_colour, escape, base: str, highlight: str,
                 pop: int, emphasis: frozenset[str] | set[str] = frozenset(), emphasis_colour: str = "") -> str:
    """One caption event's text: every word with its own colour and scale tags.

    `first`: the group has just appeared, so every word pops in. Otherwise only the spoken word pops.
    `emphasis`: words (bare(), lower case) a script marked to stand out: drawn in `emphasis_colour`."""
    kinds = colours(s) if getattr(s, "caption_keyword_colours", True) else {}
    out: list[str] = []
    for j, text in enumerate(words):
        kind = keyword_kind(text) if kinds else ""
        is_active = j == active
        colour = kinds.get(kind) if kind else None
        if emphasis and bare(text) in emphasis:
            colour = emphasis_colour or kinds.get("question") or DEFAULT_COLOURS["question"]
        if is_active:
            colour = colour or highlight
        tag = f"\\1c{ass_colour(colour or base)}"
        final = pop if is_active else 100
        if first:
            tag += f"\\fscx80\\fscy80\\t(0,80,\\fscx110\\fscy110)\\t(80,150,\\fscx{final}\\fscy{final})"
        elif is_active:
            tag += f"\\fscx100\\fscy100\\t(0,90,\\fscx{final}\\fscy{final})"
        else:
            tag += "\\fscx100\\fscy100"
        out.append(f"{{{tag}}}{escape(text)}")
    return " ".join(out)
