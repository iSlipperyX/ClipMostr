"""WebVTT parsing (copes with YouTube's rolling auto-captions) and word timing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})")
_INLINE_TIME = re.compile(r"<((?:\d+:)?\d{2}:\d{2}[.,]\d{3})>")
_TAG = re.compile(r"<[^>]+>")
REACTION = re.compile(r"[\[(](?:laughter|laughs|laughing|applause|cheering|cheers)[\])]", re.I)
_BRACKETED = re.compile(r"[\[(][^\])]*[\])]")
_SENTENCE_END = (".", "!", "?", "…")


@dataclass
class Cue:
    start: float
    end: float
    text: str


@dataclass
class Word:
    start: float
    end: float
    text: str


def to_dicts(items) -> list[dict]:
    return [asdict(i) for i in items]


def _seconds(text: str) -> float:
    m = _TIME.match(text)
    if not m:
        return 0.0
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000


def _blocks(path: Path):
    """Yield (start, end, raw_lines) for every cue in a VTT file."""
    for block in re.split(r"\r?\n\r?\n", path.read_text(encoding="utf-8", errors="replace")):
        lines = block.strip().splitlines()
        idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        times = _TIME.findall(lines[idx])
        if len(times) < 2:
            continue
        a, b = (t.strip() for t in lines[idx].split("-->")[:2])
        yield _seconds(a), _seconds(b.split()[0]), lines[idx + 1 :]


def parse_vtt(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    last_line = ""
    for start, end, raw_lines in _blocks(path):
        # YouTube auto-captions repeat the previous line at the top of every cue;
        # keep only lines we haven't just emitted.
        new_lines = []
        for raw in raw_lines:
            line = " ".join(_TAG.sub("", raw).split())
            if line and line != last_line:
                new_lines.append(line)
                last_line = line
        if new_lines:
            cues.append(Cue(start, end, " ".join(new_lines)))
    return cues


def _clean_word(text: str) -> str:
    return " ".join(_BRACKETED.sub(" ", _TAG.sub("", text)).split())


def parse_vtt_words(path: Path) -> list[Word]:
    """Word-level timing. Uses YouTube's inline <00:00:01.000> tags when present,
    otherwise spreads each cue's words evenly over the cue."""
    blocks = list(_blocks(path))
    has_inline = any(_INLINE_TIME.search(ln) for _, _, lines in blocks for ln in lines)
    words: list[Word] = []
    if has_inline:
        for start, end, lines in blocks:
            for line in lines:
                if not _INLINE_TIME.search(line):
                    continue  # the repeated "previous line" carries no timing tags
                parts = _INLINE_TIME.split(line)
                stamps = [start] + [_seconds(t) for t in parts[1::2]]
                for t, chunk in zip(stamps, parts[0::2]):
                    for w in _clean_word(chunk).split():
                        words.append(Word(t, t, w))
        words.sort(key=lambda w: w.start)
    else:
        for cue in parse_vtt(path):
            toks = _clean_word(cue.text).split()
            step = (cue.end - cue.start) / max(1, len(toks))
            words += [Word(cue.start + i * step, cue.start + (i + 1) * step, w) for i, w in enumerate(toks)]
    return _fix_ends(words)


def _fix_ends(words: list[Word]) -> list[Word]:
    for i, w in enumerate(words):
        nxt = words[i + 1].start if i + 1 < len(words) else w.start + 0.6
        if w.end <= w.start or w.end > nxt:
            w.end = max(w.start + 0.05, min(nxt, w.start + 1.2))
    return words


_CLOSERS = "\"'”’)]»"


def sentences(words: list[Word], pause: float = 0.9, max_words: int = 32) -> list[Cue]:
    """Group words into sentences with exact start/end times: break at . ! ? …, at
    long pauses (auto-captions have no punctuation), or when a run gets too long."""
    out: list[Cue] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends_sentence = w.text.rstrip(_CLOSERS).endswith(_SENTENCE_END)
        if nxt is None or ends_sentence or nxt.start - w.end > pause or len(cur) >= max_words:
            out.append(Cue(cur[0].start, cur[-1].end, " ".join(x.text for x in cur)))
            cur = []
    return out


def text_between(cues: list[Cue], start: float, end: float) -> str:
    """Spoken text in [start, end], with [Music]/[Laughter]-style tags removed."""
    parts = [c.text for c in cues if c.end > start and c.start < end]
    return " ".join(_BRACKETED.sub(" ", " ".join(parts)).split())


def words_between(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w.end > start and w.start < end]


def srt(words: list[Word], offset: float, per_line: int = 7) -> str:
    """Build an .srt from words, with times relative to `offset`."""
    def ts(t: float) -> str:
        t = max(0.0, t)
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    out, n = [], 1
    for i in range(0, len(words), per_line):
        chunk = words[i : i + per_line]
        out.append(f"{n}\n{ts(chunk[0].start - offset)} --> {ts(chunk[-1].end - offset)}\n"
                   f"{' '.join(w.text for w in chunk)}\n")
        n += 1
    return "\n".join(out)
