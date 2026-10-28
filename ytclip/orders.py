"""Clip requests: "make 10 clips of 30 to 45 seconds for this niche", filled by automation.

A request says how many clips, how long, which niche (or none), and where they come from: links
you give it, or videos automation picks for the niche. It can carry special requests in plain
words ("only the funniest reactions"), which go to the AI that picks the moments and to the AI
that writes hooks and bottom text.

Requests live in jobs.db next to the queue. Each video a request needs is a queued job that
carries the request's id, and the runner clips it with the request's count, length and special
requests. Clips that pass the quality check count towards the request; when it has them all it's
done. Its clips go to <niche folder>/Requests/<request>/, so a video clipped again for a request
never replaces the clips it made before.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import asdict, dataclass, field, fields

from .download import is_url, video_id
from .organize import safe_name

MAX_COUNT = 200
MIN_LEN, MAX_LEN = 5.0, 600.0
STATUSES = ("open", "done", "short", "cancelled")
STATUS_WORDS = {"open": "Working on it", "done": "Done", "short": "Finished short", "cancelled": "Cancelled"}
SEP = "~"  # a video clipped again for a request is queued as <video id>~<request id>


@dataclass
class Order:
    id: str = ""
    count: int = 5  # clips wanted, after the quality check
    min_len: float = 20.0
    max_len: float = 60.0
    niche: str = ""  # niche id, "" for none
    links: list[str] = field(default_factory=list)  # [] = automation picks videos for the niche
    notes: str = ""  # special requests, told to the AI
    status: str = "open"
    made: int = 0  # clips ready so far
    message: str = ""  # how it's going, in words
    created: float = 0.0
    updated: float = 0.0

    @classmethod
    def from_json(cls, d) -> "Order | None":
        if not isinstance(d, dict):
            return None
        known = {f.name for f in fields(cls)}
        o = cls(**{k: v for k, v in d.items() if k in known})
        try:
            o.count = max(1, min(MAX_COUNT, int(o.count)))
            o.min_len = max(MIN_LEN, min(MAX_LEN, float(o.min_len)))
            o.max_len = max(o.min_len, min(MAX_LEN, float(o.max_len)))
            o.made = max(0, int(o.made))
        except (TypeError, ValueError):
            return None
        o.links = [str(x) for x in o.links] if isinstance(o.links, list) else []
        o.notes = str(o.notes or "")
        o.status = o.status if o.status in STATUSES else "open"
        return o if o.id else None

    def to_json(self) -> dict:
        return asdict(self)

    @property
    def picks(self) -> bool:
        """Automation picks the videos (no links were given)."""
        return not self.links

    @property
    def remaining(self) -> int:
        return max(0, self.count - self.made)

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def length_text(self) -> str:
        return f"{self.min_len:g}–{self.max_len:g} s"

    def title(self, niche_name: str = "") -> str:
        where = f" for {niche_name}" if niche_name else ""
        source = f" from {len(self.links)} link{'s' if len(self.links) != 1 else ''}" if self.links else ""
        return f"{self.count} clips of {self.length_text()}{where}{source}"

    @property
    def folder(self) -> str:
        """Its folder inside the niche's (or the Automation) folder's Requests folder."""
        stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(self.created or time.time()))
        return safe_name(f"{stamp} {self.count} clips {self.min_len:g}-{self.max_len:g}s", 60)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def parse_length(text: str) -> tuple[float, float]:
    """'30-45', '30 to 45', '45' (up to 45 s) or '30-45s' -> (min, max) seconds. ValueError if unreadable."""
    t = (text or "").lower().replace("seconds", "").replace("sec", "").replace("s", "").replace("to", "-")
    t = t.replace("–", "-").replace("—", "-")
    parts = [p.strip() for p in t.split("-") if p.strip()]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"Can't read the length {text!r}. Write it like 30-45.") from None
    if len(nums) == 1:
        lo, hi = max(MIN_LEN, min(nums[0] * 0.6, nums[0] - 5)), nums[0]
    elif len(nums) == 2:
        lo, hi = sorted(nums)
    else:
        raise ValueError(f"Can't read the length {text!r}. Write it like 30-45.")
    if hi < MIN_LEN or lo > MAX_LEN:
        raise ValueError(f"Clips can be {MIN_LEN:g} to {MAX_LEN:g} seconds long.")
    lo, hi = max(MIN_LEN, lo), min(MAX_LEN, hi)
    if hi - lo < 3:  # the clip finder needs a little room to land on sentence breaks
        lo = max(MIN_LEN, hi - 3)
    return lo, hi


def make(count: int, min_len: float, max_len: float, niche: str = "", links: list[str] | None = None,
         notes: str = "", now: float | None = None) -> Order:
    """A new request, checked. ValueError says what's wrong in words."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ValueError("Say how many clips you want.") from None
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"Ask for 1 to {MAX_COUNT} clips.")
    if not (MIN_LEN <= min_len <= max_len <= MAX_LEN):
        raise ValueError(f"Clip length must be between {MIN_LEN:g} and {MAX_LEN:g} seconds, shortest first.")
    links = [x.strip() for x in links or [] if x.strip()]
    bad = [x for x in links if not is_url(x)]
    if bad:
        raise ValueError(f"These aren't links: {', '.join(bad[:3])}")
    seen: set[str] = set()
    unique = [x for x in links if not (video_id(x) in seen or seen.add(video_id(x)))]
    now = now or time.time()
    return Order(id=new_id(), count=count, min_len=float(min_len), max_len=float(max_len), niche=niche,
                 links=unique, notes=" ".join(notes.split()) if "\n" not in notes else notes.strip(),
                 created=now, updated=now)


def job_id(vid: str, order_id: str) -> str:
    return f"{vid}{SEP}{order_id}"


def video_of(job_id: str) -> str:
    """The video id inside a job id (a request's own copy of a video carries a suffix)."""
    return job_id.split(SEP, 1)[0]


def clips_for_video(order: Order, per_video: int, links_left: int) -> int:
    """How many clips to ask one video for. With links, the request's remaining clips are shared
    between the links still to clip (they were chosen for this). Picked videos give at most
    `per_video` each, so one video doesn't fill a whole request."""
    if order.remaining <= 0:
        return 0
    if order.links:
        return max(1, math.ceil(order.remaining / max(1, links_left)))
    return max(1, min(order.remaining, per_video))


def videos_needed(order: Order, per_video: int, waiting: int) -> int:
    """Picked videos a request still needs, beyond the ones already waiting for it."""
    if not order.is_open or not order.picks:
        return 0
    return max(0, math.ceil(order.remaining / max(1, per_video)) - waiting)


def instructions(order: Order) -> str:
    """The special requests as the AI is told them."""
    notes = order.notes.strip()
    return f"Special requests for these clips: {notes}" if notes else ""


def dumps(order: Order) -> str:
    return json.dumps(order.to_json(), ensure_ascii=False)
