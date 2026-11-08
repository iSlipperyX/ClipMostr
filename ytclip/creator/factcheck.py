"""The fact check: a second look at a script the AI wrote, sentence by sentence, against what was looked up online
for it (web.py), before anything is recorded.

Each sentence that states something checkable (a date, a number, a name, a quote, an event) gets a verdict:

* supported: a source says so (its number is kept);
* contradicted: a source says otherwise. In "fix" mode the sentence is rewritten to what the source says;
* unsupported: no source says so. In "fix" mode a sentence with specifics nobody confirmed (a date, a number,
  a quote) is softened to keep its point without them; anything else is only marked.

Opinions, advice, hooks and common knowledge aren't checked. What was changed and what's marked goes on the
storyboard (shown in the Creator tab's table and the video's README), so nothing is changed silently. Without
sources (nothing was found, or looking things up is off) there's nothing to check against, so it doesn't run:
the AI checking itself from memory is how a 2024 date got into a 2026 video in the first place.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import director

Log = Callable[[str], None]
MODES = ("fix", "flag", "off")
VERDICTS = ("supported", "contradicted", "unsupported")
MAX_SENTENCES = 60

SYSTEM = ("You fact-check the script of a short video against sources that were found online today. Be strict "
          "about facts and leave everything else alone. The sources and today's date beat what you remember.")

ASK = """{facts}

The script, one numbered sentence a line:
{lines}

Go through the sentences that state something checkable: a date, a number, a name, a quote, an event, a
release, a record, a "first" or "only". For each one, give its number and a verdict:
- "supported": a source says it. Give the source's number.
- "contradicted": a source says something different. Give the source's number and, as "fix", the sentence
  rewritten to say what the source says, in the same voice and about as long.
- "unsupported": no source says it. If it has specifics nobody confirmed (a date, a number, a quote), give as
  "fix" the sentence rewritten to keep its point without them, or saying plainly that it's not confirmed.
Say in "why" what the source says, in a few words. Leave out opinions, advice, questions, calls to action and
common knowledge: they don't need checking. Sentence 1 is the hook: a fix for it is {hook} words or fewer."""

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "The sentence's number"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "source": {"type": "integer", "description": "The source's number, 0 if none"},
                    "why": {"type": "string", "description": "What the source says, in a few words"},
                    "fix": {"type": "string", "description": "The rewritten sentence, or empty"},
                },
                "required": ["n", "verdict"],
            },
        },
    },
    "required": ["claims"],
}

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
SPECIFIC = re.compile(r"\d|[\"“”]|\b(?:" + "|".join(MONTHS) + r"|percent|million|billion|thousand|hundred|"
                      r"first|only|record|biggest|largest|oldest|newest)\b", re.IGNORECASE)


@dataclass
class Claim:
    n: int
    scene_id: int
    sentence: str
    verdict: str
    source: int = 0
    why: str = ""
    fix: str = ""
    changed: bool = False  # the sentence was rewritten to `fix`


@dataclass
class Report:
    mode: str
    checked: int = 0  # sentences sent
    claims: list[Claim] = field(default_factory=list)
    model: str = ""
    skipped: str = ""  # why it didn't run

    @property
    def fixed(self) -> list[Claim]:
        return [c for c in self.claims if c.changed]

    @property
    def flagged(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict != "supported" and not c.changed]

    @property
    def supported(self) -> list[Claim]:
        return [c for c in self.claims if c.verdict == "supported"]

    def summary(self) -> str:
        if self.skipped:
            return f"Fact check: {self.skipped}."
        bits = [f"{len(self.supported)} confirmed"]
        if self.fixed:
            bits.append(f"{len(self.fixed)} fixed")
        if self.flagged:
            bits.append(f"{len(self.flagged)} to check yourself")
        return f"Fact check ({self.model or 'AI'}): " + ", ".join(bits) + "."

    def to_json(self) -> dict:
        return asdict(self)


def numbered(board: director.Storyboard) -> list[tuple[int, int, str]]:
    """(number, scene index, sentence) for every sentence of the narration."""
    out: list[tuple[int, int, str]] = []
    for i, sc in enumerate(board.scenes):
        for sent in director.sentences(sc.speech_text) or ([sc.speech_text] if sc.speech_text.strip() else []):
            out.append((len(out) + 1, i, sent))
    return out[:MAX_SENTENCES]


def _usable_fix(fix: str, sentence: str, first: bool) -> str:
    """The fix, tidied, or "" when it can't be used as it is."""
    fix = director.clean_speech(str(fix or "").strip().strip('"“”').strip())
    if not fix or fix.lower().rstrip(".!?") == sentence.lower().rstrip(".!?"):
        return ""
    words, was = len(fix.split()), len(sentence.split())
    if words > max(was * 2, was + 12) or words < 2:
        return ""
    if first and words > director.HOOK_WORDS:
        return ""
    if fix[-1] not in ".!?":
        fix += "."
    return fix


def apply(board: director.Storyboard, report: Report) -> None:
    """Rewrite the sentences the report fixes (in "fix" mode), keeping every other word as it was."""
    if report.mode != "fix":
        return
    lines = numbered(board)
    by_n = {n: (i, sent) for n, i, sent in lines}
    fixes: dict[int, dict[str, str]] = {}
    for c in report.claims:
        if c.verdict == "supported" or c.n not in by_n:
            continue
        i, sent = by_n[c.n]
        if c.verdict == "unsupported" and not SPECIFIC.search(sent):
            continue  # nothing specific to soften: just marked
        fix = _usable_fix(c.fix, sent, c.n == 1)
        if fix:
            fixes.setdefault(i, {})[sent] = fix
            c.fix, c.changed = fix, True
    for i, swap in fixes.items():
        sc = board.scenes[i]
        parts = director.sentences(sc.speech_text) or [sc.speech_text]
        sc.speech_text = " ".join(swap.get(p, p) for p in parts)
        if i == 0 and board.hook:
            for old, new in swap.items():
                if board.hook.strip().rstrip(".!?").lower() == old.strip().rstrip(".!?").lower():
                    board.hook = new.rstrip(".")


def check(s, board: director.Storyboard, facts: str, *, have_sources: bool, mode: str = "fix",
          log: Log | None = None, check_cancel: Callable[[], None] | None = None, progress=None,
          ask=None) -> Report:
    """Check `board` against `facts` (web.brief: today's date and the sources) and apply the fixes. `ask`
    stands in for ai.ask_json (tests). Never raises for the AI failing: the script is kept as it was."""
    from .. import ai

    log = log or (lambda m: None)
    mode = mode if mode in MODES else "fix"
    report = Report(mode)
    if mode == "off":
        report.skipped = "off (Settings: Creator)"
        return report
    if not have_sources:
        report.skipped = "nothing was found online to check against"
        log(report.summary())
        return report
    lines = numbered(board)
    if not lines:
        report.skipped = "no sentences"
        return report
    report.checked = len(lines)
    text = "\n".join(f"{n}. {sent}" for n, _, sent in lines)
    ask = ask or ai.ask_json
    try:
        answer = ask(s, "polish", SYSTEM, ASK.format(facts=facts, lines=text, hook=director.HOOK_WORDS), SCHEMA,
                     effort="low", max_tokens=4000, log=log, check=check_cancel, what="check the script's facts",
                     only=director.script_writers(s) or None, progress=progress)
    except ai.AIError as exc:
        report.skipped = f"the AI couldn't check it ({exc})"
        log(report.summary())
        return report
    report.model = getattr(answer, "model", "") or ""
    by_n = {n: (i, sent) for n, i, sent in lines}
    seen: set[int] = set()
    for raw in (answer.data or {}).get("claims") or []:
        if not isinstance(raw, dict):
            continue
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        verdict = str(raw.get("verdict") or "").lower()
        if n not in by_n or n in seen or verdict not in VERDICTS:
            continue
        seen.add(n)
        i, sent = by_n[n]
        try:
            source = max(0, int(raw.get("source") or 0))
        except (TypeError, ValueError):
            source = 0
        report.claims.append(Claim(n, board.scenes[i].scene_id, sent, verdict, source,
                                   str(raw.get("why") or "").strip()[:300], str(raw.get("fix") or "").strip()))
    apply(board, report)
    for c in report.fixed:
        log(f"  Fixed scene {c.scene_id}: \"{c.sentence}\" → \"{c.fix}\"" + (f" ({c.why})" if c.why else ""))
    for c in report.flagged:
        log(f"  To check, scene {c.scene_id} ({c.verdict}): \"{c.sentence}\"" + (f" ({c.why})" if c.why else ""))
    log(report.summary())
    return report


def marks(board: director.Storyboard) -> dict[int, list[dict]]:
    """Scene id: its fact-check claims that aren't simply confirmed (for the Creator tab's table)."""
    out: dict[int, list[dict]] = {}
    for c in (board.factcheck or {}).get("claims") or []:
        if isinstance(c, dict) and (c.get("changed") or c.get("verdict") != "supported"):
            out.setdefault(int(c.get("scene_id") or 0), []).append(c)
    return out


def readme_section(board: director.Storyboard) -> str:
    """The README's "Fact check" section ("" when it didn't run)."""
    fc = board.factcheck or {}
    if not fc:
        return ""
    if fc.get("skipped"):
        return f"\n## Fact check\n\nNot run: {fc['skipped']}.\n"
    rows = []
    sources = board.sources or []

    def cite(c: dict) -> str:
        k = int(c.get("source") or 0)
        if 1 <= k <= len(sources):
            src = sources[k - 1]
            return f" [{src.get('publisher') or src.get('title', '')}](<{src.get('url', '')}>)"
        return ""

    for c in fc.get("claims") or []:
        if c.get("changed"):
            rows.append(f"- **Fixed** (scene {c['scene_id']}): \"{c['sentence']}\" → \"{c['fix']}\"" +
                        (f". {c['why']}" if c.get("why") else "") + cite(c))
        elif c.get("verdict") != "supported":
            rows.append(f"- **To check** (scene {c['scene_id']}, {c['verdict']}): \"{c['sentence']}\"" +
                        (f". {c['why']}" if c.get("why") else "") + cite(c))
    confirmed = sum(1 for c in fc.get("claims") or [] if c.get("verdict") == "supported" and not c.get("changed"))
    head = f"\n## Fact check\n\nChecked by {fc.get('model') or 'the AI'} against the sources below: " \
           f"{confirmed} statement{'s' if confirmed != 1 else ''} confirmed."
    return head + ("\n\n" + "\n".join(rows) if rows else " Nothing to fix.") + "\n"
