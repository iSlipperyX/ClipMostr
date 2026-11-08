"""How long to wait for TikTok Studio to take a video, and how often to look.

A clip over 100 MB can take longer to upload than a fixed limit allows on a slow connection, and TikTok
Studio only enables Post once the file is in. So the wait follows the page's own upload progress:

* the page is read often while the upload moves, and less and less often while it doesn't (a backoff from
  1 s up to 10 s), so a long upload isn't watched with hundreds of pointless reads;
* an upload whose percentage hasn't moved for STALL_SECONDS has stalled: the post stops with that said,
  instead of waiting out the whole limit (only while the page shows a percentage under 100: once it's gone
  or full, TikTok is checking the video, which the overall limit covers);
* the overall limit grows with the file: 15 minutes, plus the rest of the file at a slow 2 Mbit/s;
* when Post lights up for a big file, the page also has to say the upload is done (or Post has to stay
  lit for a few more reads) before it's clicked, so a half-sent video is never published.

Pure bookkeeping on (time, progress) readings: browser.py reads the page, this decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MB = 1_000_000
BIG_FILE = 100 * MB  # over this, Post has to be confirmed before it's clicked
BASE_SECONDS = 900.0  # a file of up to 100 MB gets 15 minutes, as before
SLOW_BYTES_PER_SECOND = 250_000  # 2 Mbit/s: the slowest upload the limit allows for
STALL_SECONDS = 240.0  # no progress for this long: stalled
FIRST_DELAY, MAX_DELAY, FACTOR = 1.0, 10.0, 1.6  # the backoff between reads
SETTLE_READS = 3  # a big file's Post button has to stay enabled this many reads when the page shows no "done"

# What the page shows of the upload, read by browser.py in one call: a progress bar's value, a percentage
# beside the word upload, and whether it says the upload is done.
READ_PROGRESS = r"""() => {
  const out = {pct: null, done: false, text: ""};
  for (const bar of document.querySelectorAll('[role="progressbar"]')) {
    const now = parseFloat(bar.getAttribute('aria-valuenow'));
    const max = parseFloat(bar.getAttribute('aria-valuemax') || '100');
    if (isFinite(now) && max > 0) { out.pct = Math.max(out.pct ?? 0, 100 * now / max); }
  }
  const text = (document.body && document.body.innerText) || '';
  const m = text.match(/upload\w*[^\n%]{0,40}?(\d{1,3}(?:\.\d+)?)\s*%/i) || text.match(/(\d{1,3}(?:\.\d+)?)\s*%[^\n]{0,24}upload/i);
  if (out.pct === null && m) { out.pct = parseFloat(m[1]); }
  // "Uploaded" alone is done; "45% uploaded" is still going.
  out.done = /\buploaded\b|upload(ed)? (complete|successful|finished)/i.test(text) && !(out.pct !== null && out.pct < 100);
  return out;
}"""


def limit_for(size_bytes: int) -> float:
    """Seconds an upload of this size may take in all."""
    return BASE_SECONDS + max(0, size_bytes - BIG_FILE) / SLOW_BYTES_PER_SECOND


@dataclass
class UploadWatch:
    size_bytes: int
    started: float
    stall_seconds: float = STALL_SECONDS
    deadline: float = 0.0
    progress: float | None = None  # the last percentage read; None until the page has shown one
    showing: bool = False  # the last read showed a percentage
    changed_at: float = 0.0
    delay: float = FIRST_DELAY
    done: bool = False  # the page said the upload is complete
    enabled_reads: int = 0  # reads in a row with Post enabled
    reads: int = 0
    logged: list[int] = field(default_factory=list)  # the tens of percent already reported

    def __post_init__(self) -> None:
        self.deadline = self.deadline or self.started + limit_for(self.size_bytes)
        self.changed_at = self.started

    @property
    def big(self) -> bool:
        return self.size_bytes > BIG_FILE

    def seen(self, now: float, progress: float | None, done: bool = False, post_enabled: bool = False) -> None:
        """One reading of the page."""
        self.reads += 1
        moved = progress is not None and (self.progress is None or progress > self.progress + 0.05
                                          or not self.showing)  # back after a gap: counted afresh
        self.showing = progress is not None
        if done and not self.done:
            self.done, moved = True, True
        if moved:
            self.progress = max(progress or 0.0, self.progress or 0.0) if progress is not None else self.progress
            self.changed_at = now
            self.delay = FIRST_DELAY
        else:
            self.delay = min(MAX_DELAY, self.delay * FACTOR)
        self.enabled_reads = self.enabled_reads + 1 if post_enabled else 0
        if post_enabled:  # Post is lit: the settle reads come quickly, not at the backed-off pace
            self.delay = FIRST_DELAY

    def ready(self) -> bool:
        """Post can be clicked: it's enabled and, for a big file, the upload is known to be done."""
        if self.enabled_reads == 0:
            return False
        if not self.big or self.done or (self.progress is not None and self.progress >= 99.95):
            return True
        return self.enabled_reads >= SETTLE_READS

    def stalled(self, now: float) -> bool:
        """The page shows a percentage under 100, and it hasn't moved for stall_seconds."""
        return (self.showing and self.progress is not None and self.progress < 99.95 and not self.done
                and now - self.changed_at > self.stall_seconds)

    def expired(self, now: float) -> bool:
        return now > self.deadline

    def report(self) -> int | None:
        """The progress to put in the log, once per ten percent (None when there's nothing new)."""
        if self.progress is None:
            return None
        tens = int(self.progress // 10) * 10
        if tens in self.logged or tens == 0:
            return None
        self.logged.append(tens)
        return tens
