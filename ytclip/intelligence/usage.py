"""What the AIs have been asked, and what that cost: every answer's tokens are added to ai_usage.json, and a
spending limit (Settings → AI) stops the paid AIs for the rest of the day or month once it's reached.

The file is `%APPDATA%\\ClipMonster\\ai_usage.json` on Windows and `~/Library/Application Support/ClipMonster/
ai_usage.json` on a Mac (settings.app_dir()). It keeps one line of sums per day, AI and model:

    {"version": 1,
     "days": {"2026-11-10": {"claude": {"claude-opus-5-5": {"calls": 3, "in": 41200, "out": 2900, "usd": 0.2228}},
                             "local":  {"Mistral Nemo 12B Instruct": {"calls": 12, "in": 96000, "out": 7100,
                                                                      "usd": 0.0}}}},
     "updated": 1794316800.0}

Every AI call in ClipMonster goes through ai.ask_json, which calls `note()` here after each answer and
`guard()` before each paid one, so the Studio, automation, the Sports tab and the router (agent_router.py) all
count towards the same budget. The local AI is counted too (it's free, and it's good to see how much it did).

Prices are the providers' public list prices per million tokens, in US dollars. They change: a model that isn't
in the table is counted at its family's dearest price and marked "estimated", and `ai_prices.json` beside the
usage file overrides any of them ({"claude-opus-5-5": [4.0, 20.0]}). What the provider bills is the truth; this
is ClipMonster's own running count, there so a runaway job can't spend more than you allowed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

VERSION = 1
KEEP_DAYS = 400
FILE = "ai_usage.json"
PRICES_FILE = "ai_prices.json"
FREE = ("local", "pc")  # run on your own hardware

# model id (or the start of one) -> (input, output) US dollars per million tokens. Checked November 2026 against
# platform.claude.com's and ai.google.dev's pricing pages.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
    "gemini-3.8-flash": (1.50, 7.50),  # (0.75, 3.75) until the end of 2026: DATED below
    "gemini-3.5-flash": (1.50, 9.0),
    "gemini-3.1-pro": (2.0, 12.0),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash": (0.50, 3.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}
# Introductory prices: model -> (the last day they apply, (input, output))
DATED: dict[str, tuple[str, tuple[float, float]]] = {"gemini-3.8-flash": ("2026-12-31", (0.75, 3.75))}
# A model that isn't listed (a new one, or one whose price isn't published yet) counts at these
UNKNOWN = {"claude": (10.0, 50.0), "gemini": (2.0, 12.0)}

_lock = threading.RLock()
_local = threading.local()


@dataclass
class Call:
    """One answer's tokens, as its AI reported them."""

    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    estimated: bool = False  # the price was a guess (a model that isn't in PRICES)

    def to_json(self) -> dict:
        return {"provider": self.provider, "model": self.model, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "usd": round(self.usd, 6), "estimated": self.estimated}


class Budget(dict):
    """What `status()` returns: spent/limit per period, and whether the paid AIs are stopped."""

    @property
    def over(self) -> str:
        """"day" or "month" when that limit is reached, else ""."""
        for period in ("day", "month"):
            limit = float(self.get(f"{period}_limit") or 0)
            if limit > 0 and float(self.get(f"{period}_usd") or 0) >= limit:
                return period
        return ""


def path() -> Path:
    from ..settings import app_dir

    return app_dir() / FILE


def _overrides() -> dict[str, tuple[float, float]]:
    from ..settings import app_dir

    try:
        raw = json.loads((app_dir() / PRICES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for model, pair in (raw.items() if isinstance(raw, dict) else ()):
        try:
            out[str(model)] = (float(pair[0]), float(pair[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def price(provider: str, model: str, day: str = "") -> tuple[float, float, bool]:
    """(input, output) dollars per million tokens for `model`, and whether that's a guess."""
    if provider in FREE:
        return 0.0, 0.0, False
    model = (model or "").strip().removeprefix("models/")
    own = _overrides()
    for table in (own, PRICES):
        # the longest listed name the model's id starts with ("claude-opus-5-5" before "claude-opus-5")
        for name in sorted(table, key=len, reverse=True):
            if model == name or model.startswith(name):
                if table is PRICES and name in DATED and (day or today()) <= DATED[name][0]:
                    return (*DATED[name][1], False)
                return (*table[name], False)
    return (*UNKNOWN.get(provider, (10.0, 50.0)), True)


def cost(provider: str, model: str, tokens_in: int, tokens_out: int, day: str = "") -> tuple[float, bool]:
    p_in, p_out, guess = price(provider, model, day)
    return (max(0, tokens_in) * p_in + max(0, tokens_out) * p_out) / 1_000_000, guess


def today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


@contextmanager
def _file_lock(target: Path):
    """One process at a time changes the file (the window and automation both count)."""
    lock = target.with_suffix(".lock")
    f = None
    try:
        f = open(lock, "a+b")  # noqa: SIM115 - held for the whole block
        deadline = time.monotonic() + 3.0
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # counting matters less than answering: go on without the lock
                time.sleep(0.03)
    except OSError:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            f.close()


def load(file: Path | None = None) -> dict:
    try:
        data = json.loads((file or path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        data = {"version": VERSION, "days": {}}
    return data


def _save(data: dict, file: Path) -> None:
    days = data["days"]
    for old in sorted(days)[:-KEEP_DAYS]:
        days.pop(old, None)
    data["version"] = VERSION
    data["updated"] = time.time()
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, file)


def note(provider: str, model: str, tokens_in: int = 0, tokens_out: int = 0, *, now: float | None = None,
         file: Path | None = None) -> Call:
    """Count one answer. Never raises: a usage file that can't be written must not lose an answer."""
    day = today(now)
    usd, guess = cost(provider, model, int(tokens_in or 0), int(tokens_out or 0), day)
    call = Call(provider, model or "?", int(tokens_in or 0), int(tokens_out or 0), usd, guess)
    _local.last = call
    try:
        target = file or path()
        with _lock, _file_lock(target):
            data = load(target)
            row = data["days"].setdefault(day, {}).setdefault(provider, {}).setdefault(
                call.model, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
            row["calls"] = int(row.get("calls", 0)) + 1
            row["in"] = int(row.get("in", 0)) + call.tokens_in
            row["out"] = int(row.get("out", 0)) + call.tokens_out
            row["usd"] = round(float(row.get("usd", 0.0)) + usd, 6)
            if guess:
                row["estimated"] = True
            _save(data, target)
    except Exception:  # noqa: BLE001 - see the docstring
        pass
    return call


def last() -> Call | None:
    """The answer this thread counted most recently (the router reads it straight after asking)."""
    return getattr(_local, "last", None)


def forget_last() -> None:
    _local.last = None


def spent(period: str = "day", *, now: float | None = None, file: Path | None = None, paid_only: bool = True) -> float:
    """Dollars counted today ("day") or this calendar month ("month")."""
    key = today(now)
    prefix = key if period == "day" else key[:7]
    total = 0.0
    for day, providers in load(file)["days"].items():
        if not day.startswith(prefix):
            continue
        for provider, models in providers.items():
            if paid_only and provider in FREE:
                continue
            total += sum(float(m.get("usd", 0.0)) for m in models.values() if isinstance(m, dict))
    return round(total, 6)


def status(s=None, *, now: float | None = None, file: Path | None = None) -> Budget:
    """Spent and limits: {"day_usd", "day_limit", "month_usd", "month_limit"}; `.over` names a reached limit."""
    return Budget(day_usd=spent("day", now=now, file=file), month_usd=spent("month", now=now, file=file),
                  day_limit=max(0.0, float(getattr(s, "ai_budget_day", 0) or 0)),
                  month_limit=max(0.0, float(getattr(s, "ai_budget_month", 0) or 0)))


def guard(s, provider: str, *, now: float | None = None, file: Path | None = None) -> str:
    """"" when `provider` may be asked; else the sentence saying why not (a limit is reached). Free AIs always
    may."""
    if provider in FREE:
        return ""
    st = status(s, now=now, file=file)
    period = st.over
    if not period:
        return ""
    limit, used = st[f"{period}_limit"], st[f"{period}_usd"]
    when = "today" if period == "day" else "this month"
    back = "tomorrow" if period == "day" else "next month"
    return (f"The AI spending limit is reached: ${used:.2f} of ${limit:.2f} {when} (Settings → AI). The paid AIs "
            f"start again {back}; the local AI isn't limited.")


def summary(days: int = 30, *, now: float | None = None, file: Path | None = None) -> list[dict]:
    """Rows for `ytclip ai usage`, newest day first: {"day", "provider", "model", "calls", "in", "out", "usd"}."""
    data = load(file)["days"]
    out = []
    for day in sorted(data, reverse=True)[:max(1, days)]:
        for provider in sorted(data[day]):
            for model, row in sorted(data[day][provider].items()):
                if isinstance(row, dict):
                    out.append({"day": day, "provider": provider, "model": model, "calls": int(row.get("calls", 0)),
                                "in": int(row.get("in", 0)), "out": int(row.get("out", 0)),
                                "usd": float(row.get("usd", 0.0)), "estimated": bool(row.get("estimated"))})
    return out
