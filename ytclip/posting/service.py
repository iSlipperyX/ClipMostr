"""The posting worker: everything that happens to a clip after automation makes it, without anyone there.

It runs as a thread of the background automation process (pipeline.Runner), next to the clipping, so
it keeps going with the window closed and starts with Windows. Each pass (tick):

1. takes in what the window changed (the queue order, a caption, an account's schedule);
2. does one thing the window asked for (actions.py: log in, post now, test posting, read now…);
3. brings in automation's new clips and queues them for their niche's account (intake.py);
4. gets them ready: a posting score (scoring.py) and the AI's hashtags;
5. posts the clip that's due, through the account's own browser login (TikTok Studio);
6. reads each account's profile every few minutes (live.py): views, likes, followers, and which TikTok
   video each post became;
7. writes each posted clip's record and deletes its files once it's on the profile (records.py), and
   keeps results.json current for learning (results.py);
8. checks TikTok's upload page once a day per account, so a change on TikTok's side is caught early.

One browser window at a time: posting, readings and the window's actions all happen on this one thread.
When the background process isn't running, the window runs a Service with posting=False, which only
does the window's actions (a login, say), so the buttons still work.

What happened goes to each account's feed (live.EventLog, which the Performance tab shows and turns
into notifications) and the automation log. status.json says what it's doing for the window.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from . import actions as actions_mod
from . import intake, jobs, live, records, results, schedule
from .accounts import Account, Accounts
from .config import Settings, app_dir
from .store import Store, VideoItem

Log = Callable[[str], None]

TICK = 15.0  # seconds between passes when nothing is due sooner
INTAKE_EVERY = 120.0  # look for new clips this often (and straight after automation makes some)
RECORDS_EVERY = 60.0
SHARE_EVERY = 30 * 60.0
PREPARE_PER_TICK = 2  # clips scored / given hashtags per pass, so a post that comes due never waits long
HASHTAG_RETRY = 6 * 3600.0  # an AI hashtag call that failed is tried again after this
STATUS_EVERY = 5.0


def status_path() -> Path:
    return app_dir() / "status.json"


def read_status(path: Path | None = None) -> dict:
    """What the posting worker last said it's doing (for the window)."""
    try:
        data = json.loads((path or status_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _alive(pid) -> bool:
    try:
        from ..procs import process_alive

        return process_alive(str(pid))
    except Exception:  # noqa: BLE001
        return False


class Stop(Exception):
    """The service was asked to stop in the middle of something."""


class Service:
    def __init__(self, log: Log | None = None, posting: bool = True, main_load: Callable | None = None,
                 now: Callable[[], float] = time.time, store: Store | None = None, accounts: Accounts | None = None,
                 actions: actions_mod.Actions | None = None, settings_load: Callable[[], Settings] = Settings.load,
                 status_file: Path | None = None, live_folder: Path | None = None):
        if main_load is None:
            from ..settings import Settings as MainSettings

            main_load = MainSettings.load
        self._log = log or (lambda m: None)
        self.posting = posting
        self.main_load = main_load
        self.now = now
        self.store = store or Store()
        self.accounts = accounts or Accounts.load()
        self.actions = actions or actions_mod.Actions()
        self.settings_load = settings_load
        self.s = settings_load()
        self.status_file = status_file or status_path()
        self.live_folder = live_folder
        self.pid = os.getpid()
        self.watchers: dict[str, live.Watcher] = {}
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.doing = ""
        self._last_intake = 0.0
        self._last_records = 0.0
        self._last_share = 0.0
        self._last_status = 0.0
        self._warned: set[str] = set()  # accounts told about once (can't post: not logged in)
        self._unassigned_said = 0
        # What each step calls, so tests can stand in for TikTok and ffmpeg
        self.post_fn = jobs.post_item
        self.read_fn = live.read
        self.refresh_fn = jobs.refresh_account
        self.analyze_fn = jobs.analyze_item
        self.hashtags_fn = jobs.ai_hashtags
        self.check_fn = None  # tiktok.check_upload
        self.profile_fn = None  # tiktok.apply_profile
        self.login_fn = None  # _login below
        self.logout_fn = None

    # --- bookkeeping --------------------------------------------------------------------

    def log(self, msg: str) -> None:
        self._log(msg)

    def event(self, account: Account | None, kind: str, text: str, alert: bool = False, **extra) -> None:
        """A line in the account's feed (and the automation log)."""
        self.log(text)
        if account is None:
            return
        try:
            live.EventLog(account.id, self.live_folder).add([live.Event(at=self.now(), kind=kind, text=text,
                                                                         alert=alert, **extra)])
        except OSError:
            pass

    def watcher(self, account_id: str) -> live.Watcher:
        w = self.watchers.get(account_id)
        if w is None:
            w = self.watchers[account_id] = live.Watcher(account_id, self.live_folder)
        return w

    def nudge(self) -> None:
        """Look for new clips now (automation just made some)."""
        self._last_intake = 0.0
        self.wake.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake.set()

    def _stopped(self) -> bool:
        return self.stop_event.is_set()

    def save(self) -> None:
        try:
            self.store.save()
        except (OSError, TimeoutError) as exc:
            self.log(f"⚠ Couldn't save the posting library: {exc}")
        try:
            self.accounts.save()
        except (OSError, TimeoutError) as exc:
            self.log(f"⚠ Couldn't save the account list: {exc}")

    def start(self) -> None:
        """Before the first pass of the process that posts: uploads a crash cut off are marked failed (they
        may be on TikTok already, so they're never posted again by themselves)."""
        if self.posting:
            n = self.store.interrupted()
            if n:
                self.log(f"⚠ {n} upload{'s' if n != 1 else ''} were cut off last time. They may be on TikTok "
                         "already, so they won't be posted again by themselves; the next stats reading marks them "
                         "posted if it finds them.")
                self.save()
        self.actions.tidy(_alive, self.now())

    def write_status(self, force: bool = False) -> None:
        now = self.now()
        if not force and now - self._last_status < STATUS_EVERY:
            return
        self._last_status = now
        accounts = {}
        for a in self.accounts:
            w = self.watchers.get(a.id)
            accounts[a.id] = {"last_ok": w.last_ok if w else 0.0, "next_at": w.next_at if w else 0.0,
                              "error": w.error if w else "", "stopped": w.stopped if w else ""}
        data = {"pid": self.pid, "at": now, "posting": self.posting, "doing": self.doing, "accounts": accounts}
        try:
            tmp = self.status_file.with_name(self.status_file.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except OSError:
            pass

    def _doing(self, text: str) -> None:
        self.doing = text
        self.write_status(force=True)

    # --- one pass -----------------------------------------------------------------------

    def refresh(self) -> None:
        """Take in what the other process saved, and the settings as they are now."""
        self.s = self.settings_load()
        try:
            self.store.refresh()
            self.accounts.refresh()
        except (OSError, TimeoutError) as exc:
            self.log(f"⚠ Couldn't read the posting library: {exc}")
        for aid in [a for a in self.watchers if self.accounts.get(a) is None]:
            del self.watchers[aid]

    def tick(self) -> float:
        """Do what's due; returns seconds until something else might be."""
        self.refresh()
        if self.store.adopt(self.accounts.ids()):
            self.save()
        worked = self.run_action()
        if self.posting and not self._stopped():
            now = self.now()
            if now - self._last_intake >= INTAKE_EVERY:
                self.intake()
            self.prepare()
            worked = self.post_due() or worked
            if not self._stopped():
                worked = self.read_due() or worked
            if not self._stopped() and not worked:
                worked = self.upload_check() or worked
            if self.now() - self._last_records >= RECORDS_EVERY:
                self.finish_posted()
        self._doing("")
        self.save()
        return 1.0 if worked else self.next_wait()

    def next_wait(self) -> float:
        """Until the next post, reading or intake is due (at most TICK)."""
        now = self.now()
        soon = [TICK]
        if self.posting:
            nxt = jobs.next_post(self.store)
            if nxt and nxt.planned_at:
                soon.append(nxt.planned_at - now)
            for a in self.accounts:
                w = self.watchers.get(a.id)
                if w and a.live and not w.stopped:
                    soon.append(w.next_at - now)
        if self.actions.active():
            soon.append(1.0)
        return max(1.0, min(soon))

    def run(self, stop: Callable[[], bool] = lambda: False, sleep_step: float = 1.0) -> None:
        """Pass after pass until `stop()` or stop()."""
        self.start()
        try:
            while not stop() and not self._stopped():
                try:
                    wait = self.tick()
                except Stop:
                    break
                except Exception as exc:  # noqa: BLE001 - one bad pass mustn't end posting for good
                    self.log(f"⚠ Posting hit a problem and carries on: {type(exc).__name__}: {exc}")
                    wait = TICK * 2
                self.wake.clear()
                end = time.monotonic() + wait
                while time.monotonic() < end and not stop() and not self._stopped():
                    if self.wake.wait(min(sleep_step, max(0.0, end - time.monotonic()))):
                        break
                    if self.actions_waiting():
                        break
        finally:
            self.doing = ""
            self.write_status(force=True)

    def actions_waiting(self) -> bool:
        try:
            return any(r.state == "new" for r in self.actions.active())
        except Exception:  # noqa: BLE001 - a locked database: look next time
            return False

    # --- 3. new clips ----------------------------------------------------------------------

    def intake(self) -> list[VideoItem]:
        self._last_intake = self.now()
        main = self.main_load()
        try:
            items, got = intake.take(self.store, self.accounts, main, now=self.now())
        except OSError as exc:
            self.log(f"⚠ Couldn't look for new clips: {exc}")
            return []
        if items:
            where = ", ".join(f"{n} for {self.accounts.label_for(aid)}" for aid, n in got.items())
            self.log(f"{len(items)} new clip{'s' * (len(items) != 1)} ready to post"
                     + (f": {where}." if where else "."))
            for aid, n in got.items():
                a = self.accounts.get(aid)
                self.event(a, "queued", f"{n} new clip{'s' * (n != 1)} queued for {a.handle if a else aid}.")
            self.save()
        waiting = len(intake.unassigned(self.store))
        if waiting and waiting != self._unassigned_said:
            self._unassigned_said = waiting
            self.log(f"⚠ {waiting} clip{'s are' if waiting != 1 else ' is'} waiting for a TikTok account: "
                     "pick the account in the Niches tab (or add one in the Accounts tab).")
        return items

    # --- 4. ready to post --------------------------------------------------------------------

    def prepare(self) -> None:
        """Score and hashtag the clips that will post soonest (a few per pass)."""
        from . import ai

        jobs.replan(self.store, self.accounts, self.s, self.now())
        q = sorted((i for i in self.store.queue() if i.status == "queued"),
                   key=lambda i: i.planned_at or float("inf"))
        done = 0
        for it in q:
            if done >= PREPARE_PER_TICK or self._stopped():
                break
            if not it.score and not it.analyze_error and it.exists():
                self._doing(f"Scoring “{it.headline}”")
                try:
                    self.analyze_fn(self.store, it.id, self.s)
                except Exception as exc:  # noqa: BLE001 - ffmpeg couldn't read it: it still posts
                    self.log(f"⚠ Couldn't score “{it.headline}”: {exc}")
                done += 1
        if not self.s.ai_hashtags or self._stopped():
            return
        try:
            has_key = ai.has_key(self.s)
        except Exception:  # noqa: BLE001
            has_key = False
        if not has_key:
            return
        made = 0
        for it in q:
            if made >= PREPARE_PER_TICK or self._stopped():
                break
            tried = float((it.ai or {}).get("hashtags_tried") or 0)
            if it.hashtags.strip() or it.imported_by != "auto" or self.now() - tried < HASHTAG_RETRY:
                continue
            account = self.accounts.get(it.account)
            with self.store.lock:
                it.ai = dict(it.ai or {}) | {"hashtags_tried": self.now()}
            self._doing(f"Writing hashtags for “{it.headline}”")
            try:
                self.hashtags_fn(self.store, it.id, self.s, account, self.log, only_if_empty=True)
            except Exception as exc:  # noqa: BLE001 - it posts with the account's own hashtags
                self.log(f"⚠ No AI hashtags for “{it.headline}”: {exc}")
            made += 1

    # --- 5. posting -----------------------------------------------------------------------------

    def post_due(self) -> bool:
        now = self.now()
        skipped: list[str] = []
        pick = jobs.autopilot_pick(self.store, self.accounts, self.s, now, log=skipped.append)
        for msg in skipped:
            self.log(msg)
        self._warn_cant_post(now)
        if not pick:
            return bool(skipped)
        account, item = pick
        self.post(account, item, why="due")
        return True

    def _warn_cant_post(self, now: float) -> None:
        for a in self.accounts:
            if not a.autopilot or a.logged_in or a.id in self._warned:
                continue
            due = any(i.status == "queued" and i.planned_at and i.planned_at <= now for i in self.store.queue(a.id))
            if due:
                self._warned.add(a.id)
                self.event(a, "error", f"⚠ {a.label} has clips due but isn't logged in to TikTok, so nothing posts. "
                                       "Log it in on the Accounts tab.", alert=True)

    def post(self, account: Account, item: VideoItem, why: str = "due", cancelled: Callable[[], bool] | None = None,
             progress: Log | None = None) -> bool:
        self._doing(f"Posting “{item.headline}” to {account.handle}")
        self.log(f"Posting “{item.headline}” to {account.handle}" + (" (Post now)" if why == "now" else "") + "…")
        try:
            self.post_fn(self.store, item.id, self.s, account, self.log, cancelled or self._stopped, progress)
        except Exception as exc:  # noqa: BLE001 - TikTok's page, the browser, a missing file
            msg = str(exc) or type(exc).__name__
            if "log in" in msg.lower() or "logged out" in msg.lower() or msg.startswith("Not logged in"):
                account.logged_in = False
                self._warned.discard(account.id)
            self.save()
            if msg == "Cancelled" or self._stopped():
                self.log(f"Stopped posting “{item.headline}”: it's marked failed in case it went up; check TikTok.")
            else:
                self.event(account, "error", f"✖ Couldn't post “{item.headline}” to {account.handle}: {msg}",
                           alert=True)
            return False
        self.save()
        self.event(account, "posted", f"✔ Posted “{item.headline}” to {account.handle}.", alert=True)
        # Read the profile soon, so the post is linked (and its files cleared) without waiting a whole interval
        w = self.watcher(account.id)
        w.next_at = min(w.next_at or self.now() + 90, self.now() + 90)
        return True

    # --- 6. stats --------------------------------------------------------------------------------

    def read_due(self) -> bool:
        now = self.now()
        for a in self.accounts:
            w = self.watcher(a.id)
            if not a.live or w.stopped or now < w.next_at or not (a.logged_in or a.username):
                continue
            nxt = jobs.next_post(self.store, a.id) if a.autopilot and a.logged_in else None
            if nxt and nxt.planned_at and nxt.planned_at <= now:
                continue  # a post is due: that first
            self.read(a, w)
            return True
        return False

    def read(self, a: Account, w: live.Watcher | None = None) -> list[live.Event]:
        w = w or self.watcher(a.id)
        now = self.now()
        full = w.want_full(now)
        w.schedule(self.s, now)
        self._doing(f"Reading {a.handle}'s stats")
        try:
            r = self.read_fn(self.s, a, full=full, log=lambda _m: None, cancelled=self._stopped)
        except live.LiveError as exc:
            if exc.code == "cancelled":
                return []
            events = w.failed(self.s, str(exc), exc.code, self.now())
            if exc.code == "logged_out" and a.logged_in:
                self._logged_out(a)
            if w.failures == 1 or w.stopped:
                self.log(f"Stats for {a.handle}: {exc}")
            return events
        except OSError as exc:
            return w.failed(self.s, f"{exc}.", "other", self.now())
        if r.logged_in is False and a.logged_in:
            self._logged_out(a)
        had = {i.id for i in self.store.items.values() if i.account == a.id and i.tiktok_id}
        events = w.apply(self.store, a, self.s, r)
        linked = [i for i in self.store.items.values() if i.account == a.id and i.tiktok_id and i.id not in had]
        for it in linked:
            self.log(f"“{it.headline}” is up on {a.handle}: {it.tiktok_url or it.tiktok_id}.")
        if w.next_at < self.now():
            w.schedule(self.s, self.now())
        self.save()
        if linked or self.now() - self._last_share >= SHARE_EVERY:
            self.share()
        if linked:
            self.finish_posted()
        return events

    def _logged_out(self, a: Account) -> None:
        a.logged_in = False
        self._warned.discard(a.id)
        self.save()
        self.event(a, "error", f"⚠ {a.label}'s TikTok login has run out. Log in again on the Accounts tab.",
                   alert=True)

    def share(self) -> None:
        self._last_share = self.now()
        try:
            results.share(self.store, now=self.now())
        except OSError as exc:
            self.log(f"⚠ Couldn't save results.json for learning: {exc}")

    # --- 7. records ---------------------------------------------------------------------------------

    def finish_posted(self) -> records.Result:
        self._last_records = self.now()
        main = self.main_load()
        r = records.finish(self.store, self.accounts, self.s, intake.roots(main), now=self.now())
        n = records.update_records(self.store, self.accounts, self.s, now=self.now())
        if r.summary():
            self.log(r.summary() + f" Records: {records.records_folder(self.s)}")
        for line in r.kept:
            self.log(f"⚠ {line}")
        for what, why in r.failed[:5]:
            self.log(f"⚠ Clearing {what}: {why}")
        if r.cleaned or r.recorded or r.kept or n:
            self.save()
        return r

    # --- 8. the daily upload-page check ----------------------------------------------------------------

    def upload_check(self) -> bool:
        a = jobs.upload_check_due(self.store, self.accounts, self.s, self.now())
        if a is None:
            return False
        self.check_posting(a, full=False, auto=True)
        return True

    def check_posting(self, a: Account, full: bool, auto: bool = False, log: Log | None = None,
                      cancelled: Callable[[], bool] | None = None) -> str:
        """TikTok Studio's upload page for `a`, without posting (full: with a 3 s test clip). Returns what
        it found, in a few lines."""
        from . import tiktok

        check = self.check_fn or tiktok.check_upload
        self._doing(f"{'Testing posting' if full else 'Checking the upload page'} for {a.handle}")
        try:
            found = check(a.apply(self.s), a, log or self.log, cancelled or self._stopped, full=full)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or type(exc).__name__
            a.upload_check_at, a.upload_check, a.upload_check_detail = self.now(), "fail", msg
            self.save()
            if auto:
                self.event(a, "error", f"⚠ Checking TikTok's upload page for {a.handle} didn't work: {msg}",
                           alert=True)
            raise
        verdict = "fail" if any(r[1] == "fail" for r in found) else \
                  "warn" if any(r[1] == "warn" for r in found) else "ok"
        lines = [f"{'✓' if r == 'ok' else '⚠' if r == 'warn' else '✖'} {step}{': ' + detail if detail else ''}"
                 for step, r, detail in found]
        # A quick check can't see the caption box or Post button, so it never replaces a full result.
        if full or verdict == "fail" or not a.upload_check_at or a.upload_check == "fail":
            a.upload_check, a.upload_check_detail = verdict, "\n".join(lines)
        a.upload_check_at = self.now()
        self.save()
        head = {"ok": f"Posting test for {a.handle} passed.",
                "warn": f"Posting test for {a.handle} passed with a warning.",
                "fail": f"Posting test for {a.handle} failed: posts would fail the same way."}[verdict]
        if verdict == "fail" and auto:
            self.event(a, "error", f"⚠ {head} " + "; ".join(lines), alert=True)
        else:
            self.log(("✓ " if verdict == "ok" else "⚠ ") + head)
        return head + "\n" + "\n".join(lines)

    # --- 2. the window's actions ----------------------------------------------------------------------

    def run_action(self) -> bool:
        """Do the oldest action the window asked for, if any. True if one ran."""
        try:
            req = self.actions.claim(self.pid, now=self.now())
        except Exception as exc:  # noqa: BLE001 - a locked database: next pass
            self.log(f"⚠ Couldn't read the window's requests: {exc}")
            return False
        if req is None:
            return False
        a = self.accounts.get(req.account)
        last_check = [0.0]
        flags = [False, False]

        def poll() -> tuple[bool, bool]:
            t = time.monotonic()
            if t - last_check[0] >= 1.0:
                last_check[0] = t
                flags[0], flags[1] = self.actions.flags(req.id)
            return flags[0], flags[1]

        def cancelled() -> bool:
            return self._stopped() or poll()[0]

        def log(line: str) -> None:
            self.actions.add_log(req.id, line)

        if a is None:
            self.actions.end(req.id, False, "That account isn't on the list any more.", now=self.now())
            return True
        try:
            result = self._action(req, a, log, cancelled, lambda: poll()[1])
        except Exception as exc:  # noqa: BLE001
            msg = str(exc) or type(exc).__name__
            if msg == "Cancelled" or type(exc).__name__ == "Cancelled":
                self.actions.end(req.id, False, "Cancelled.", now=self.now(), cancelled=True)
            else:
                self.actions.end(req.id, False, msg, now=self.now())
            self.save()
            return True
        self.actions.end(req.id, True, result or "Done.", now=self.now())
        self.save()
        return True

    def _action(self, req: actions_mod.Request, a: Account, log: Log, cancelled: Callable[[], bool],
                finish: Callable[[], bool]) -> str:
        from . import tiktok

        kind = req.kind
        if kind == "login":
            self._doing(f"Logging {a.label} in to TikTok")
            return (self.login_fn or _login)(self, a, log, cancelled, finish)
        if kind == "logout":
            self._doing(f"Logging {a.label} out")
            (self.logout_fn or _logout)(self, a, log)
            a.logged_in = False
            return f"Logged {a.handle} out of TikTok."
        if kind == "refresh":
            self._doing(f"Reading {a.handle}'s stats")
            self.refresh_fn(self.store, self.s, a, log, cancelled)
            self.watcher(a.id).resume()
            return f"{a.handle}'s stats are read."
        if kind == "read_now":
            w = self.watcher(a.id)
            w.resume()
            events = self.read(a, w)
            errors = [e.text for e in events if e.kind == "error"]
            if errors:
                raise RuntimeError(errors[-1])
            return f"{a.handle} read just now."
        if kind == "post_now":
            item = self.store.get(req.item)
            if item is None or item.status in ("uploading", "posted"):
                return "It's already posted (or no longer in the queue)."
            if item.account != a.id:
                raise RuntimeError(f"“{item.headline}” is queued for a different account now.")
            if not a.logged_in:
                raise RuntimeError(f"{a.label} isn't logged in to TikTok. Log it in first.")
            if not self.post(a, item, why="now", cancelled=cancelled, progress=log):
                raise RuntimeError(self.store.get(item.id).last_error if self.store.get(item.id) else "Not posted.")
            return f"Posted “{item.headline}” to {a.handle}."
        if kind == "test_posting":
            return self.check_posting(a, full=bool(req.args.get("full", True)), log=log, cancelled=cancelled)
        if kind == "apply_profile":
            from . import profile

            self._doing(f"Setting {a.label}'s TikTok profile")
            applying = profile.fingerprint(a)
            try:
                changed, note = (self.profile_fn or tiktok.apply_profile)(a.apply(self.s), a, log, cancelled)
            except Exception as exc:  # noqa: BLE001
                a.profile_error = str(exc) or type(exc).__name__
                raise
            if profile.fingerprint(a) == applying:
                if note:
                    a.profile_error = f"Name not changed yet. {note}"
                else:
                    profile.mark_applied(a)
            what = ", ".join(changed) if changed else "nothing needed changing"
            return f"{a.handle}'s profile is updated on TikTok ({what})." + \
                (f" The name wasn't changed: {note} Apply again after that to set it." if note else "")
        raise RuntimeError(f"Unknown request: {kind}")


def _login(service: Service, a: Account, log: Log, cancelled: Callable[[], bool], finish: Callable[[], bool]) -> str:
    """Open a plain browser window on the account's own profile for you to log in (TikTok refuses logins in
    an automated one), then check the login took and read the account."""
    from .tiktok.browser import TikTokBrowser
    from .tiktok.login import login_window

    s = service.s
    if not login_window(s, a.browser_dir(), log, cancelled, finish=finish):
        log("Couldn't find Edge or Chrome to open by itself; using ClipMonster's browser window.")
        with TikTokBrowser(s, a.browser_dir(), log, headless=False, cancelled=cancelled) as b:
            if not b.login():
                raise RuntimeError("Login wasn't completed.")
    log("Checking the login…")
    with TikTokBrowser(s, a.browser_dir(), log, headless=True, cancelled=cancelled) as b:
        if not b.logged_in():
            a.logged_in = False
            raise RuntimeError("TikTok's login wasn't saved in that window. Press Log in again, log in there and "
                               "wait for your feed to appear.")
        a.logged_in = True
        name = b.whoami()
    note = ""
    if name:
        if a.username and a.username.lower() != name.lower():
            log(f"⚠ {a.label} was @{a.username}; it's now logged in as @{name}.")
        a.username = name
        other = service.accounts.find_username(name, exclude=a.id)
        if other:
            note = (f" @{name} is also added as “{other.label}”: both would post to the same TikTok account. "
                    "Remove one.")
    service._warned.discard(a.id)
    service.save()
    try:
        service.refresh_fn(service.store, s, a, log, cancelled)
    except Exception as exc:  # noqa: BLE001 - logged in all the same; the stats come with the next reading
        log(f"Logged in, but reading the account didn't work yet: {exc}")
    service.watcher(a.id).resume()
    service.event(a, "start", f"{a.handle} is logged in to TikTok.")
    return f"Logged in as {a.handle}.{note}"


def _logout(service: Service, a: Account, log: Log) -> None:
    from .tiktok.browser import TikTokBrowser

    with TikTokBrowser(service.s, a.browser_dir(), log, headless=True) as b:
        b.logout()


class Thread:
    """The posting service on its own thread (the background process runs one; the window runs one with
    posting off while the background process is stopped)."""

    def __init__(self, service: Service):
        self.service = service
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.service.stop_event.clear()
        self._thread = threading.Thread(target=self.service.run, name="posting", daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 30.0) -> None:
        self.service.stop()
        if self._thread is not None:
            self._thread.join(timeout)


def plan_text(store: Store, accounts: Accounts, s: Settings, account_id: str, now: float | None = None) -> str:
    """'3 waiting · next at 16:05' for an account (the window's summary lines)."""
    now = time.time() if now is None else now
    q = [i for i in store.queue(account_id) if i.status == "queued"]
    nxt = jobs.next_post(store, account_id)
    text = f"{len(q)} waiting"
    if nxt and nxt.planned_at:
        text += f" · next {schedule.fmt_when(nxt.planned_at, now)}"
    return text
