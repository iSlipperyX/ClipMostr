"""WebVTT parsing (copes with YouTube's rolling auto-captions) and word timing."""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})")
_INLINE_TIME = re.compile(r"<((?:\d+:)?\d{2}:\d{2}[.,]\d{3})>")
_TAG = re.compile(r"<[^>]+>")
REACTION = re.compile(r"[\[(](?:laughter|laughs|laughing|applause|cheering|cheers)[\])]", re.I)
_BRACKETED = re.compile(r"[\[(][^\])]*[\])]")
_SENTENCE_END = (".", "!", "?", "…")
_SPEAKER = re.compile(r"(?:^|(?<=\s))>{2,}(?=\s|$)")  # ">>": YouTube's "someone else speaks" mark
_WORDY = re.compile(r"\w")


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


# Strong swearing only. Each is a stem: "fuck" also catches fucking, fucked, motherfucker...
# Matches start at a word's start, so "Scunthorpe" or "classic" are left alone.
_SWEARS = ("fuck", "shit", "bitch", "cunt", "nigga", "nigger", "faggot", "asshole", "pussy", "bastard", "whore",
           "slut", "twat", "wanker", "bollocks")
_SAFE = ("shitake", "pussycat", "pussyfoot")
_SWEAR = re.compile(r"\b(mother)?(" + "|".join(_SWEARS) + r")(\w*)", re.I)


def censor(text: str) -> str:
    """Mask the vowels of strong swear words: "fucking" -> "f*cking", "SHIT!" -> "SH*T!"."""
    def mask(m: re.Match) -> str:
        word = m.group(0)
        if word.lower().startswith(_SAFE):
            return word
        stem_from = len(m.group(1) or "")
        stem = m.group(2)
        masked = stem[0] + re.sub(r"[aeiouAEIOU]", "*", stem[1:])
        return word[:stem_from] + masked + m.group(3)
    return _SWEAR.sub(mask, text)


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


def clean_text(text: str) -> str:
    """Caption text as people read it: no tags, HTML entities decoded (YouTube's translated captions
    encode them twice: "&amp;gt;&amp;gt;"), no ">>" speaker marks, single spaces."""
    text = _TAG.sub("", text)
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(_SPEAKER.sub(" ", text).split())


def parse_vtt(path: Path) -> list[Cue]:
    cues: list[Cue] = []
    last_line = ""
    for start, end, raw_lines in _blocks(path):
        # YouTube auto-captions repeat the previous line at the top of every cue;
        # keep only lines we haven't just emitted.
        new_lines = []
        for raw in raw_lines:
            line = clean_text(raw)
            if line and line != last_line:
                new_lines.append(line)
                last_line = line
        if new_lines:
            cues.append(Cue(start, end, " ".join(new_lines)))
    return cues


def _clean_word(text: str) -> str:
    return " ".join(_BRACKETED.sub(" ", clean_text(text)).split())


def _attach_punctuation(words: list[Word]) -> list[Word]:
    """Tokens with no letters or digits ("," "." "..." "-") join the word before them, so captions never
    show a lone comma as a word. Leading ones are dropped."""
    out: list[Word] = []
    for w in words:
        if _WORDY.search(w.text):
            out.append(w)
        elif out:
            prev = out[-1]
            prev.text += w.text if w.text[:1] in ",.!?…:;)]\"'”’" else " " + w.text
            prev.end = max(prev.end, w.end)
    return out


def parse_vtt_words(path: Path) -> list[Word]:
    """Word-level timing. Uses YouTube's inline <00:00:01.000> tags when present,
    otherwise spreads each cue's words evenly over the cue."""
    blocks = list(_blocks(path))
    has_inline = any(_INLINE_TIME.search(ln) for _, _, lines in blocks for ln in lines)
    words: list[Word] = []
    if has_inline:
        # YouTube sometimes ends a timed line with a bare time stamp ("... Don't blame<00:00:04.074>")
        # and only shows the words after it ("me for") in the next cue's untimed repeat of the line.
        # `dangling` remembers such a line, so those words can be picked up from the repeat.
        dangling: tuple[str, float] | None = None  # (the timed line's text, its last stamp)
        for start, end, lines in blocks:
            for line in lines:
                if not _INLINE_TIME.search(line):
                    # the repeated "previous line" carries no timing tags
                    if dangling:
                        said, at = dangling
                        full = _clean_word(line)
                        if full != said and full.startswith(said):
                            toks = full[len(said):].split()
                            step = (end - at) / len(toks) if end > at else 0.0
                            words += [Word(at + i * step, at + i * step, w) for i, w in enumerate(toks)]
                            dangling = None
                    continue
                parts = _INLINE_TIME.split(line)
                stamps = [start] + [_seconds(t) for t in parts[1::2]]
                for n, (t, chunk) in enumerate(zip(stamps, parts[0::2])):
                    toks = _clean_word(chunk).split()
                    if not toks:
                        continue
                    # Several words under one time stamp (common in translated captions): spread them
                    # over the time until the next stamp, so each word gets its own moment.
                    until = stamps[n + 1] if n + 1 < len(stamps) else end
                    step = max(0.0, until - t) / len(toks) if until > t else 0.0
                    words += [Word(t + i * step, t + i * step, w) for i, w in enumerate(toks)]
                ends_bare = len(stamps) > 1 and not _clean_word(parts[-1])
                dangling = (_clean_word(line), stamps[-1]) if ends_bare else None
        words.sort(key=lambda w: w.start)
    else:
        for cue in parse_vtt(path):
            toks = _clean_word(cue.text).split()
            step = (cue.end - cue.start) / max(1, len(toks))
            words += [Word(cue.start + i * step, cue.start + (i + 1) * step, w) for i, w in enumerate(toks)]
    return _fix_ends(_attach_punctuation(words))


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
