"""TikTok accounts: each has its own login, queue, schedule, posting defaults and stats.

The posting library is shared. A clip belongs to an account once it's queued for it
(`VideoItem.account`), and the account's stats live in the library file under its id. Each category
names the account it posts to (Category.account_id), so automation's clips go straight to its queue.

accounts.json is shared by the window and the background worker the same way the library is
(safefile.merge_entries): you change a schedule in the window while the worker records a login check.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import safefile
from .config import Settings, app_dir, cache_dir
from .schedule import WARM_UP_DAYS

# Settings fields that each account has its own copy of. Account.apply() lays them over the
# posting Settings, so the scheduler, caption builder and AI prompts don't need to know about accounts.
PER_ACCOUNT = {
    "posts_per_day": "posts_per_day",
    "first_post_time": "first_post_time",
    "last_post_time": "last_post_time",
    "min_gap_minutes": "min_gap_minutes",
    "vary_minutes": "vary_minutes",
    "warm_up_start": "warm_up_start",
    "id": "schedule_seed",
    "autopilot": "autopilot",
    "logged_in": "browser_logged_in",
    "default_privacy": "default_privacy",
    "default_hashtags": "default_hashtags",
    "audience": "ai_audience",
}


@dataclass
class Account:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""  # optional label; "@username" is shown when empty
    username: str = ""  # TikTok @handle, filled in after logging in
    created_at: float = field(default_factory=time.time)
    browser_profile: str = ""  # where its login lives, under %LOCALAPPDATA%\ClipMonster\posting
    logged_in: bool = False  # TikTok's login cookies were seen in its browser profile
    live: bool = True  # read its stats every few minutes (live.py) while it's logged in
    # Schedule. Autopilot off pauses the account's posting: its clips wait in the queue.
    autopilot: bool = True
    posts_per_day: int = 2
    first_post_time: str = "11:00"
    last_post_time: str = "21:00"
    min_gap_minutes: int = 180
    vary_minutes: int = 15  # each post time moves by up to this much, differently each day
    warm_up_start: float = 0.0  # set when the account is marked new: it posts less for two weeks
    # Posting defaults
    default_privacy: str = "public"
    default_hashtags: str = "#fyp"
    audience: str = ""  # audience / topic, used by the AI's hashtags and bio suggestions
    # Last check of TikTok's upload page (tiktok.check_upload): when, "ok" | "warn" | "fail", what it found
    upload_check_at: float = 0.0
    upload_check: str = ""
    upload_check_detail: str = ""
    # The TikTok profile set up for this account (profile.py) and what was last applied to TikTok
    profile_name: str = ""
    profile_bio: str = ""
    profile_photo: str = ""  # file name in profile-photos/
    profile_applied: str = ""  # profile.fingerprint() of what was applied
    profile_applied_at: float = 0.0
    profile_error: str = ""
    # The sign-in check every few hours (service.session_check): when, "ok" | "expired" | "unknown", and
    # what it saw. "expired" sends a Windows notification with a button that signs the account in again.
    session_checked_at: float = 0.0
    session_check: str = ""
    session_detail: str = ""
    # How it was added: "link" (signed in to an account you had) or "create" (made on TikTok from here)
    onboarded: str = ""

    def __post_init__(self) -> None:
        self.browser_profile = self.browser_profile or f"browser-profiles/{self.id}"

    @property
    def label(self) -> str:
        if self.name.strip():
            return self.name.strip()
        return f"@{self.username}" if self.username else "New account"

    @property
    def handle(self) -> str:
        """'@name' when known, else the label; used in log lines and notifications."""
        return f"@{self.username}" if self.username else self.label

    def browser_dir(self) -> Path:
        path = cache_dir() / self.browser_profile
        path.mkdir(parents=True, exist_ok=True)
        return path

    def start_warm_up(self, now: float | None = None) -> None:
        """Mark the account as new on TikTok: it posts less for two weeks (schedule.WARM_UP), counted
        from when it was added here if that's recent enough, else from now."""
        now = time.time() if now is None else now
        recent = now - self.created_at < WARM_UP_DAYS * 86400
        self.warm_up_start = self.created_at if recent else now

    def apply(self, s: Settings) -> Settings:
        """A copy of the posting settings with this account's schedule and posting defaults."""
        return replace(s, **{target: getattr(self, src) for src, target in PER_ACCOUNT.items()})


class Accounts:
    """The account list (accounts.json), shared with the other process."""

    def __init__(self, path: Path | None = None):
        self.path = path or (app_dir() / "accounts.json")
        self.lock = threading.RLock()
        self.items: list[Account] = []
        self._base: dict[str, dict] = {}
        self._sig = (0, 0)

    # --- persistence ------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> "Accounts":
        acc = cls(path)
        with safefile.file_lock(acc.path):
            data = safefile.load(acc.path, "your account list")
            acc._sig = safefile.signature(acc.path)
        acc._take(_entries(data))
        acc._base = acc._snapshot()
        return acc

    def save(self) -> None:
        with self.lock, safefile.file_lock(self.path):
            if safefile.signature(self.path) != self._sig:
                disk = safefile.read_raw(self.path)
                if isinstance(disk, dict):
                    self._take(safefile.merge_entries(self._base, self._snapshot(), _entries(disk)))
            snap = self._snapshot()
            safefile.save(self.path, {"version": 2, "accounts": list(snap.values())})
            self._base = snap
            self._sig = safefile.signature(self.path)

    def refresh(self) -> bool:
        """Take in what the other process saved. True if anything came in."""
        if safefile.signature(self.path) == self._sig:
            return False
        with self.lock, safefile.file_lock(self.path):
            sig = safefile.signature(self.path)
            disk = safefile.read_raw(self.path)
            if sig == self._sig or not isinstance(disk, dict):
                return False
            entries = _entries(disk)
            self._take(safefile.merge_entries(self._base, self._snapshot(), entries))
            self._base = entries
            self._sig = sig
            return True

    def _snapshot(self) -> dict[str, dict]:
        return {a.id: asdict(a) for a in self.items}

    def _take(self, entries: dict[str, dict]) -> None:
        """Become `entries`, updating the Account objects in place so references to them stay good."""
        known = {f.name for f in fields(Account)}
        old = {a.id: a for a in self.items}
        out: list[Account] = []
        for aid, d in entries.items():
            a = old.get(aid)
            if a is None:
                out.append(Account(**{k: v for k, v in d.items() if k in known}))
            else:
                for k, v in d.items():
                    if k in known:
                        setattr(a, k, v)
                out.append(a)
        self.items = out

    # --- access -----------------------------------------------------------------

    def __iter__(self):
        return iter(list(self.items))

    def __len__(self) -> int:
        return len(self.items)

    def ids(self) -> list[str]:
        return [a.id for a in self.items]

    def get(self, account_id: str | None) -> Account | None:
        return next((a for a in self.items if a.id == account_id), None)

    def label_for(self, account_id: str) -> str:
        a = self.get(account_id)
        return a.label if a else "removed account"

    def find_username(self, username: str, exclude: str = "") -> Account | None:
        name = username.lstrip("@").strip().lower()
        if not name:
            return None
        return next((a for a in self.items if a.id != exclude and a.username.lower() == name), None)

    # --- changes ----------------------------------------------------------------

    def add(self, name: str = "", s: Settings | None = None, copy_schedule_from: Account | None = None) -> Account:
        """A new account with its own (empty) login and a two-week warm-up (a new TikTok account that posts
        a lot at once looks like spam). It copies the schedule of `copy_schedule_from` if given."""
        a = Account(name=name.strip())
        src = copy_schedule_from
        for f in ("posts_per_day", "first_post_time", "last_post_time", "min_gap_minutes", "vary_minutes",
                  "default_privacy", "default_hashtags"):
            if src is not None:
                setattr(a, f, getattr(src, f))
            elif s is not None:
                setattr(a, f, getattr(s, f))
        a.start_warm_up()
        with self.lock:
            self.items.append(a)
        return a

    def remove(self, account_id: str) -> Account | None:
        """Take an account off the list. Its login is left for delete_login() so the caller can delete it
        off the GUI thread."""
        with self.lock:
            a = self.get(account_id)
            if a is None:
                return None
            self.items.remove(a)
            return a

    @staticmethod
    def delete_login(a: Account) -> None:
        """Delete a removed account's browser profile (its TikTok login) from this PC."""
        shutil.rmtree(cache_dir() / a.browser_profile, ignore_errors=True)

    def move(self, account_id: str, delta: int) -> None:
        with self.lock:
            a = self.get(account_id)
            if a is None:
                return
            i = self.items.index(a)
            j = max(0, min(len(self.items) - 1, i + delta))
            self.items.insert(j, self.items.pop(i))


def _entries(data) -> dict[str, dict]:
    if not isinstance(data, dict):
        return {}
    out = {}
    for d in data.get("accounts", []):
        if isinstance(d, dict) and d.get("id"):
            if "niche" in d:  # saved before 3.2: an account's "niche" is its audience now
                d = {k: v for k, v in d.items() if k != "niche"} | {"audience": d.get("audience") or d["niche"]}
            out[d["id"]] = d
    return out
