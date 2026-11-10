"""Drive TikTok's own web uploader in your installed Edge/Chrome with a dedicated profile.

You log in yourself once, in a plain browser window on the same profile (login.py: TikTok
refuses logins in an automated window). The app never sees your password; the login is kept
in the app's own browser profile for later uploads and stat refreshes.
Every account has its own profile folder, so each one stays logged in to its own TikTok.

TikTok changes its web pages often, so every step has several selectors to try and the
fragile extras (privacy, comment/duet/stitch switches) are best-effort: if one can't be
set, the upload still goes ahead and the log says what was skipped.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from ..config import Settings, cache_dir
from ..store import Profile, TikTokVideo
from . import upload_wait

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=upload&lang=en"
LOGIN_URL = "https://www.tiktok.com/login"
HOME_URL = "https://www.tiktok.com/foryou?lang=en"
PROFILE_URL = "https://www.tiktok.com/@{username}?lang=en"
SESSION_COOKIES = ("sessionid", "sessionid_ss", "sid_tt")
# Playwright adds --no-sandbox unless told otherwise, and Edge/Chrome then show "You are using an
# unsupported command-line flag" (and run without their sandbox). Windows doesn't need it; Linux
# test machines running as root do.
SANDBOX = os.name == "nt"
# The window is the browser you installed, with your own sign-in, and says what it is: nothing is
# injected to hide that it's being driven (3.8 removed the old script that made navigator.webdriver
# read false, in line with ClipMonster not disguising its automation).

CAPTCHA = "#captcha-verify-image, div[class*='captcha_verify'], #captcha_container"
# TikTok Studio's cover editor (posting/cover.py), most specific first; any one of each may match
EDIT_COVER = ["button:has-text('Edit cover')", "div:text-is('Edit cover')", "text=/^\\s*edit cover\\s*$/i",
              "[class*='cover'] div[class*='edit-container']", "div[class*='edit-container']"]
COVER_EDITOR = ["div[class*='cover-edit-container']", "div[class*='cover-edit-panel']", "[role='dialog']:has-text('cover')"]
UPLOAD_COVER_TAB = ["div:text-is('Upload cover')", "[role='tab']:has-text('Upload')", "text=/^\\s*upload cover\\s*$/i"]
COVER_INPUT = "input[type='file'][accept*='image']"
COVER_FIND = 10.0  # seconds to look for Edit cover once the video's in
COVER_CONFIRM = ["div[class*='cover-edit-footer'] button[class*='primary']", "div[class*='cover-edit-footer'] button:has-text('Confirm')",
                 "[role='dialog'] button:has-text('Confirm')", "[role='dialog'] button:has-text('Save')",
                 "[role='dialog'] button:has-text('Done')"]
COVER_CLOSE = ["div[class*='cover-edit-footer'] button:has-text('Cancel')", "[role='dialog'] button:has-text('Cancel')",
               "[role='dialog'] [aria-label='Close']", "div[class*='cover-edit-container'] [class*='close']"]

PRIVACY_LABELS = {
    "public": ("Everyone", "Public"),
    "friends": ("Friends", "Mutual"),
    "private": ("Only you", "Only me", "Private"),
}

Log = Callable[[str], None]


class Cancelled(Exception):
    pass


class BrowserError(RuntimeError):
    pass


class TikTokBrowser:
    """Context manager around one Playwright persistent-profile browser window."""

    def __init__(self, settings: Settings, profile_dir: Path, log: Log = print, headless: bool | None = None,
                 cancelled: Callable[[], bool] = lambda: False):
        self.s = settings
        self.profile_dir = profile_dir
        self.log = log
        self.headless = (not settings.show_browser) if headless is None else headless
        self.cancelled = cancelled
        self._pw = None
        self.ctx = None
        self.page = None
        self.cover_set: bool | None = None  # the last upload's cover: True = TikTok took ours, False = its own pick

    # --- lifecycle --------------------------------------------------------------

    def __enter__(self) -> "TikTokBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(str(self.profile_dir), **self.launch_options())
        except Exception as exc:
            self._pw.stop()
            msg = str(exc)
            if "ProcessSingleton" in msg or "user data directory is already in use" in msg.lower():
                raise BrowserError("This account's TikTok browser window is already open. Close it and try again.") from exc
            if "Executable doesn't exist" in msg or "is not found" in msg:
                raise BrowserError(f"Couldn't find {self.s.browser_channel}. Pick another browser in Settings.") from exc
            raise
        try:
            self.ctx.set_default_timeout(20_000)
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            self.page.on("dialog", lambda d: d.accept())
        except BaseException:
            # `with` never calls __exit__ when __enter__ fails: close the window and Playwright here, or the
            # profile stays locked ("already open") until the app quits.
            self.__exit__(None, None, None)
            raise
        return self

    def launch_options(self) -> dict:
        """No switch that Edge/Chrome warn about (see SANDBOX) and no "controlled by automated software" bar."""
        args = ["--no-first-run", "--no-default-browser-check"]
        return dict(channel=self.s.browser_channel or "msedge", headless=self.headless, args=args,
                    ignore_default_args=["--enable-automation"], chromium_sandbox=SANDBOX,
                    viewport={"width": 1280, "height": 900}, locale="en-US")

    def __exit__(self, *exc) -> None:
        ctx, pw = self.ctx, self._pw
        self.ctx = self.page = self._pw = None  # (closing twice is a no-op)
        try:
            if ctx:
                ctx.close()
        except Exception as e:  # noqa: BLE001 - a window already gone; Playwright still has to stop
            self.log(f"The browser window didn't close cleanly ({type(e).__name__}).")
        finally:
            if pw:
                pw.stop()

    def _check(self) -> None:
        if self.cancelled():
            raise Cancelled()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self._check()
            self.page.wait_for_timeout(min(250, max(1, int((end - time.time()) * 1000))))

    # --- login ------------------------------------------------------------------

    def logged_in(self) -> bool:
        cookies = self.ctx.cookies("https://www.tiktok.com")
        return any(c["name"] in SESSION_COOKIES and c.get("value") for c in cookies)

    def login(self, timeout: float = 900) -> bool:
        """Log in inside this automated window: only a fallback when no Edge/Chrome install is found
        for login.login_window(), because TikTok often answers "Too many attempts" here."""
        if self.logged_in():
            self.log("Already logged in to TikTok.")
            return True
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        self.log("Log in to TikTok in the browser window that just opened. It closes by itself once you're in.")
        end = time.time() + timeout
        while time.time() < end:
            self._check()
            if self.page.is_closed():
                return self.logged_in()
            if self.logged_in():
                self._sleep(2.5)  # let TikTok finish setting its cookies
                self.log("Logged in.")
                return True
            try:
                self.page.wait_for_timeout(1000)
            except Exception:
                return self.logged_in()
        return False

    def logout(self) -> None:
        self.ctx.clear_cookies()

    def session_ok(self, settle: float = 6.0) -> tuple[bool | None, str]:
        """Load TikTok Studio's upload page with the saved sign-in: (True, …) when it stays there,
        (False, …) when TikTok sends it to log in or the sign-in cookie has gone, (None, why) when the
        page couldn't be reached at all."""
        if not self.logged_in():
            return False, "The TikTok sign-in cookie is gone from this account's browser profile."
        try:
            self.page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=45_000)
        except Exception as exc:  # noqa: BLE001 - offline, DNS, a timeout: can't tell
            return None, f"TikTok Studio didn't load: {str(exc).splitlines()[0][:160]}"
        end = time.time() + settle  # TikTok redirects a stale session a moment after the page starts
        while time.time() < end:
            self._check()
            url = self.page.url.lower()
            if "/login" in url or "/signup" in url:
                return False, "TikTok Studio sent it to the login page."
            self.page.wait_for_timeout(500)
        if not self.logged_in():
            return False, "TikTok cleared the sign-in cookie when Studio opened."
        return True, "TikTok Studio's upload page opened signed in."

    def require_login(self) -> None:
        # No logging in from here: TikTok refuses logins in this automated window ("Too many attempts").
        # Account → Log in opens a plain browser window for it instead (login.py).
        if not self.logged_in():
            raise BrowserError("Not logged in to TikTok. Use Account → Log in first.")

    # --- account ----------------------------------------------------------------

    def _rehydration(self) -> dict:
        try:
            raw = self.page.locator("script#__UNIVERSAL_DATA_FOR_REHYDRATION__").first.text_content(timeout=15_000)
            return (json.loads(raw or "{}").get("__DEFAULT_SCOPE__") or {})
        except Exception:
            return {}

    def whoami(self) -> str:
        """The logged-in account's @username, read from TikTok's page data."""
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        scope = self._rehydration()
        user = (scope.get("webapp.app-context") or {}).get("user") or {}
        return user.get("uniqueId") or ""

    def fetch_account(self, username: str, max_videos: int = 120,
                      captcha_wait: float | None = None) -> tuple[Profile, list[TikTokVideo]]:
        """Profile numbers plus per-video stats, read from the profile page as it loads.

        With `captcha_wait` (the live view) a captcha isn't read past: a hidden window stops with a
        BrowserError, a shown one waits up to that long for it to be solved. Without it, the profile
        numbers are kept and the videos may be missing, as before.
        """
        username = username.lstrip("@").strip()
        if not username:
            username = self.whoami()
            if not username:
                raise BrowserError("Couldn't tell which account you're logged in as. Enter your @username "
                                   "on the Accounts tab.")
        videos: dict[str, TikTokVideo] = {}

        def on_response(resp) -> None:
            if "/api/post/item_list" not in resp.url and "/api/creator/item_list" not in resp.url:
                return
            try:
                data = resp.json()
            except Exception:
                return
            for it in data.get("itemList") or data.get("items") or []:
                v = _video_from_item(it, username)
                if v:
                    videos[v.id] = v

        self.page.on("response", on_response)
        try:
            self.page.goto(f"https://www.tiktok.com/@{username}?lang=en", wait_until="domcontentloaded")
            scope = self._rehydration()
            detail = scope.get("webapp.user-detail") or {}
            info = detail.get("userInfo") or {}
            if detail.get("statusCode") not in (0, None) or not info:
                raise BrowserError(f"TikTok didn't return a profile for @{username}.")
            user, stats = info.get("user") or {}, info.get("statsV2") or info.get("stats") or {}
            profile = Profile(
                username=user.get("uniqueId") or username,
                nickname=user.get("nickname") or "",
                avatar=user.get("avatarMedium") or user.get("avatarThumb") or "",
                bio=user.get("signature") or "",
                followers=_int(stats.get("followerCount")),
                following=_int(stats.get("followingCount")),
                likes=_int(stats.get("heartCount") or stats.get("heart")),
                video_count=_int(stats.get("videoCount")),
                fetched_at=time.time(),
            )
            # Scroll to make the page load more of the video grid.
            last, still = -1, 0
            for _ in range(40):
                self._check()
                self._sleep(1.2)
                if captcha_wait is not None and self._captcha_showing():  # it can come up as the grid loads
                    self._wait_for_captcha(captcha_wait, stop_after=True)
                    still = 0
                if len(videos) >= min(max_videos, profile.video_count or max_videos):
                    break
                if len(videos) == last:
                    still += 1
                    if still >= 4:
                        break
                else:
                    still = 0
                last = len(videos)
                self.page.mouse.wheel(0, 2500)
            if not videos:
                for v in self._videos_from_grid(username):
                    videos[v.id] = v
        finally:
            self.page.remove_listener("response", on_response)
        if not videos and profile.video_count:
            self.log("Profile numbers updated, but TikTok didn't send the video list (it sometimes asks for a "
                     "captcha). Try again with 'Show the browser' on.")
        return profile, list(videos.values())

    # --- profile ----------------------------------------------------------------

    def _profile_user(self, username: str) -> dict:
        self.page.goto(PROFILE_URL.format(username=username), wait_until="domcontentloaded")
        detail = self._rehydration().get("webapp.user-detail") or {}
        return (detail.get("userInfo") or {}).get("user") or {}

    def set_profile(self, name: str = "", bio: str = "", photo: Path | None = None,
                    expected_user: str = "") -> tuple[list[str], str]:
        """Fill in TikTok's Edit profile box: photo, name and bio (empty ones are left as they are).

        Returns (what changed, a note about anything left out): TikTok only lets the name change
        once every 7 days, and then shows it read-only; the rest is still saved. After saving, the
        profile page is read again and a name or bio TikTok didn't take is an error.
        """
        self.check_user(expected_user)
        username = expected_user.lstrip("@") or self.whoami()
        if not username:
            raise BrowserError("Couldn't tell which TikTok account this browser is logged in to. Log in again.")
        before = self._profile_user(username)
        todo = []
        if photo:
            todo.append("photo")
        if name and name != (before.get("nickname") or ""):
            todo.append("name")
        if bio and bio != (before.get("signature") or ""):
            todo.append("bio")
        if not todo:
            self.log("The profile on TikTok already matches.")
            return [], ""
        self._dismiss_popups()
        edit = self._first_visible(['[data-e2e="edit-profile-entrance"]', 'button:has-text("Edit profile")'], timeout=15)
        if edit is None:
            self.screenshot("no-edit-profile")
            raise BrowserError("Couldn't find the Edit profile button on your TikTok profile.")
        edit.click()
        box = self._first_visible(['[data-e2e="edit-profile-popup"]', '[role="dialog"][aria-label="Edit profile"]'],
                                  timeout=15)
        if box is None:
            self.screenshot("no-edit-profile-box")
            raise BrowserError("TikTok's Edit profile box didn't open.")

        note = ""
        if "name" in todo:
            area = box.locator('[data-e2e="edit-profile-name-input"]')
            field = area.locator("input")
            if field.count() and field.first.is_visible():
                self.log("Setting the name…")
                field.first.fill(name)
            else:  # locked: TikTok shows the name read-only with when it can change again
                todo.remove("name")
                tip = " ".join((area.first.inner_text(timeout=2000) if area.count() else "").split())
                tip = tip.removeprefix(before.get("nickname") or "").strip()
                note = tip or "TikTok isn't letting the name change right now."
                self.log(f"⚠ Name not changed: {note}")
        if "bio" in todo:
            self.log("Setting the bio…")
            field = box.locator('[data-e2e="edit-profile-bio-input"]')
            if not field.count():
                field = box.locator("textarea")
            if not field.count():
                raise BrowserError("Couldn't find the Bio field in TikTok's Edit profile box.")
            field.first.fill(bio)
        if "photo" in todo:  # last: its crop step opens on top of the box
            self.log("Setting the profile photo…")
            box.locator('input[type="file"]').first.set_input_files(str(photo))
            crop = self._first_visible(['[role="dialog"]:has-text("Edit photo")'], timeout=20)
            apply = crop.locator('button:has-text("Apply")') if crop else None
            if apply is None or not apply.count():
                self.screenshot("no-photo-apply")
                raise BrowserError("TikTok didn't show its photo crop step, so nothing was saved.")
            apply.first.click()
            end = time.time() + 20
            while time.time() < end and crop.is_visible():
                self._sleep(0.5)
        if not todo:
            self._close_edit_box(box)
            return [], note

        save = box.locator('[data-e2e="edit-profile-save"]')
        end = time.time() + 20
        while time.time() < end and not (save.count() and save.first.is_enabled()):
            self._sleep(0.5)
        if not (save.count() and save.first.is_enabled()):
            self.screenshot("profile-save-disabled")
            self._close_edit_box(box)
            raise BrowserError("TikTok's Save button stayed greyed out, so nothing was saved.")
        save.first.click()
        message = ""
        end = time.time() + 30
        while time.time() < end:
            self._sleep(1)
            message = message or self._toast_text()
            if not box.is_visible():
                break
        else:
            self.screenshot("profile-not-saved")
            self._close_edit_box(box)
            raise BrowserError("TikTok didn't save the profile" + (f": {message}" if message else "."))

        after = self._profile_user(username)
        missed = [what for what, want, got in (("name", name, after.get("nickname")), ("bio", bio, after.get("signature")))
                  if what in todo and (got or "") != want]
        if missed:
            raise BrowserError(f"TikTok didn't take the new {' and '.join(missed)}"
                               + (f": {message}" if message else "."))
        if "photo" in todo and (after.get("avatarLarger") or "") == (before.get("avatarLarger") or ""):
            self.log("⚠ TikTok's profile page doesn't show the new photo yet; new photos can take a few minutes.")
        return todo, note

    def _close_edit_box(self, box) -> None:
        """Leave the Edit profile box without saving."""
        try:
            for sel in ('[data-e2e="edit-profile-cancel"]', '[data-e2e="edit-profile-close"]'):
                btn = box.locator(sel)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    break
            self._sleep(1)
            discard = self.page.locator('[role="dialog"] button:has-text("Discard")')
            if discard.count() and discard.first.is_visible():
                discard.first.click()
        except Exception:
            pass

    def _toast_text(self) -> str:
        """The text of a message TikTok is showing (e.g. why it refused a change), if any."""
        for sel in ('[role="alert"]', '[class*="toast" i]', '[class*="Toast"]', '[data-e2e*="toast"]'):
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    text = " ".join((loc.first.inner_text(timeout=1000) or "").split())
                    if text:
                        return text[:200]
            except Exception:
                pass
        return ""

    def _videos_from_grid(self, username: str) -> list[TikTokVideo]:
        """Fallback when TikTok withholds the JSON feed: read links and view counts off the video grid."""
        try:
            rows = self.page.evaluate("""() => [...document.querySelectorAll('[data-e2e="user-post-item"]')].map(el => {
                const a = el.querySelector('a[href*="/video/"]');
                const views = el.querySelector('[data-e2e="video-views"]');
                const img = el.querySelector('img');
                return {href: a ? a.href : '', views: views ? views.textContent : '', desc: img ? (img.alt || '') : ''};
            })""")
        except Exception:
            return []
        out = []
        for r in rows:
            m = re.search(r"/video/(\d+)", r.get("href") or "")
            if m:
                out.append(TikTokVideo(id=m[1], description=r.get("desc") or "", views=_short_number(r.get("views")),
                                       url=f"https://www.tiktok.com/@{username}/video/{m[1]}"))
        return out

    # --- upload -----------------------------------------------------------------

    def _first_visible(self, selectors: list[str], timeout: float = 10.0):
        end = time.time() + timeout
        while time.time() < end:
            self._check()
            for sel in selectors:
                loc = self.page.locator(sel)
                try:
                    if loc.count() and loc.first.is_visible():
                        return loc.first
                except Exception:
                    pass
            self.page.wait_for_timeout(300)
        return None

    def _dismiss_popups(self) -> None:
        """Close tutorial tips, cookie banners and 'split video?' style prompts if any are showing."""
        try:
            self.page.evaluate("""() => {
                const b = document.querySelector('tiktok-cookie-banner');
                if (b && b.shadowRoot) { const btn = b.shadowRoot.querySelector('div.button-wrapper button'); if (btn) btn.click(); }
            }""")
        except Exception:
            pass
        for sel in ("button:has-text('Got it')", "button:has-text('Not now')", "button:has-text('Decline optional cookies')",
                    "div[role='dialog'] button:has-text('Cancel')", "button:has-text('Skip')"):
            try:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    self.page.wait_for_timeout(400)
            except Exception:
                pass

    def _captcha_showing(self) -> bool:
        try:
            cap = self.page.locator(CAPTCHA)
            return bool(cap.count()) and cap.first.is_visible()
        except Exception:  # noqa: BLE001 - the page navigated mid-check
            return False

    def _wait_for_captcha(self, timeout: float = 300, stop_after: bool = False) -> None:
        """Wait while a captcha in the shown window is solved (a hidden one can't be: that stops). With
        `stop_after`, a captcha still there after `timeout` stops too; otherwise the caller carries on."""
        if self._captcha_showing():
            if self.headless:
                raise BrowserError("TikTok is asking for a captcha. Turn on 'Show the browser' and try again.")
            self.log("TikTok is showing a captcha: please solve it in the browser window…")
            end = time.time() + timeout
            while time.time() < end and self._captcha_showing():
                self._sleep(1)
            if stop_after and self._captcha_showing():
                raise BrowserError("TikTok's captcha wasn't solved in time.")

    def _caption_editor(self, timeout: float = 30):
        return self._first_visible(["div.public-DraftEditor-content[contenteditable='true']",
                                    "div[data-contents='true']", "div[contenteditable='true']"], timeout=timeout)

    def _type_caption(self, caption: str) -> None:
        editor = self._caption_editor()
        if not editor:
            raise BrowserError("Couldn't find the caption box on TikTok's upload page.")
        self._editor = editor
        editor.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Backspace")
        self.page.wait_for_timeout(300)
        # Hashtags and mentions are typed one by one so TikTok's picker turns them into real tags.
        picked = False
        for token in re.split(r"(\s+)", caption):
            self._check()
            if not token:
                continue
            if token.isspace():
                if picked and token[0] == " " and self._editor_ends_with_space():
                    token = token[1:]  # TikTok already added a space after the picked tag
                picked = False
                for ch in token:
                    self.page.keyboard.press("Enter" if ch == "\n" else "Space")
                continue
            if token[0] in "#@" and len(token) > 1:
                self.page.keyboard.type(token, delay=60)
                picked = self._pick_suggestion(token)
            else:
                self.page.keyboard.insert_text(token)  # handles emoji, unlike key-by-key typing
        self.page.wait_for_timeout(300)

    def _editor_ends_with_space(self) -> bool:
        try:
            text = self._editor.inner_text(timeout=1000)
            return text.endswith((" ", "\u00a0"))
        except Exception:
            return False

    def _pick_suggestion(self, token: str) -> bool:
        """Click TikTok's suggestion when it exactly matches what we typed; otherwise leave it as text."""
        popup = self._first_visible(["div[class*='mention-list-popover']", "div[class*='hashtag-suggestion']",
                                     "div[class*='mentionSuggestion']"], timeout=2.5)
        if not popup:
            return False
        want = token.lower()
        try:
            for opt in popup.locator("div[class*='hash-tag'], span.hash-tag-topic, span[class*='user-id'], div[role='option']").all()[:12]:
                text = (opt.inner_text(timeout=500) or "").strip().split("\n")[0].lower()
                if text.lstrip("#@") == want.lstrip("#@"):
                    opt.click(timeout=2000)
                    self.page.wait_for_timeout(250)
                    self._refocus_editor()
                    return True
        except Exception:
            pass
        return False

    def _refocus_editor(self) -> None:
        """Clicking a suggestion can take focus away from the caption; put the cursor back at the end."""
        try:
            self._editor.focus()
            self._editor.evaluate("""el => {
                const r = document.createRange(); r.selectNodeContents(el); r.collapse(false);
                const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
            }""")
        except Exception:
            self.page.keyboard.press("Control+End")

    def _privacy_box(self, timeout: float = 4):
        return self._first_visible(["div[data-e2e='video_visibility_container'] button[role='combobox']",
                                    "div[data-e2e='video_visibility_container'] div[role='combobox']",
                                    "div[data-e2e='permission-container'] button"], timeout=timeout)

    def _set_privacy(self, privacy: str) -> None:
        labels = PRIVACY_LABELS.get(privacy)
        if not labels:
            return
        try:
            box = self._privacy_box()
            if not box:
                raise BrowserError("privacy picker not found")
            current = (box.inner_text(timeout=1000) or "").lower()
            if any(l.lower() in current for l in labels):
                return
            box.click()
            self.page.wait_for_timeout(500)
            for l in labels:
                opt = self.page.locator(f"div[role='option']:has-text('{l}'), li[role='option']:has-text('{l}')")
                if opt.count():
                    opt.first.click(timeout=3000)
                    self.log(f"Set 'Who can watch' to {l}.")
                    return
            self.page.keyboard.press("Escape")
            raise BrowserError("privacy option not found")
        except Exception as exc:
            self.log(f"⚠ Couldn't set privacy ({exc}); TikTok's default is used.")

    def _set_switch(self, label: str, on: bool) -> None:
        try:
            box = self.page.locator(f"xpath=//label[normalize-space(.)='{label}']/following-sibling::div//input"
                                    f" | //label[normalize-space(.)='{label}']/..//input[@type='checkbox']")
            if not box.count():
                # The switches live under a collapsed "Show more" section on newer layouts.
                more = self.page.locator("div:has-text('Show more'):not(:has(div:has-text('Show more')))")
                if more.count():
                    more.first.click(timeout=2000)
                    self.page.wait_for_timeout(500)
            if not box.count():
                return
            if box.first.is_checked() != on:
                box.first.click(force=True, timeout=3000)
        except Exception as exc:
            self.log(f"⚠ Couldn't set '{label}' ({exc.__class__.__name__}).")

    def check_user(self, expected: str) -> None:
        """Refuse to post if this profile is logged in to a different TikTok account than expected."""
        expected = expected.lstrip("@").strip().lower()
        if not expected:
            return
        actual = self.whoami()
        if not actual:
            self.log("⚠ Couldn't confirm which TikTok account this browser is logged in to; posting anyway.")
        elif actual.lower() != expected:
            raise BrowserError(f"This account's browser is logged in to TikTok as @{actual}, not @{expected}. "
                               "Log in again on the Accounts tab (or change the account's @username).")

    def upload(self, video: Path, caption: str, privacy: str, allow_comments: bool = True,
               allow_duet: bool = True, allow_stitch: bool = True, ai_label: bool = False,
               progress: Callable[[str], None] | None = None, expected_user: str = "",
               cover: Path | None = None) -> None:
        step = progress or self.log
        self.cover_set = None
        self.require_login()
        if expected_user:
            step("Checking which TikTok account is logged in…")
            self.check_user(expected_user)
        step("Opening TikTok Studio…")
        self._open_upload_page()

        step("Sending the video…")
        self._file_input().set_input_files(str(video))

        # The caption box appears as soon as TikTok accepts the file.
        self._type_caption(caption)
        self._dismiss_popups()
        self._set_privacy(privacy)
        for label, on in (("Comment", allow_comments), ("Duet", allow_duet), ("Stitch", allow_stitch)):
            self._set_switch(label, on)
        if ai_label:
            self._set_switch("AI-generated content", True)

        step("Waiting for TikTok to finish processing…")
        post = self._wait_until_postable(video, step)
        if cover is not None:
            step("Setting the cover…")
            self.cover_set, why = self.set_cover(cover)
            if not self.cover_set:
                step(f"Couldn't set the cover ({why}), so TikTok's own pick stays.")
                self.log(f"⚠ The cover wasn't set: {why}. The post goes up with TikTok's own pick.")
            post = self._post_button().first  # the editor re-renders the form

        self._dismiss_popups()
        step("Posting…")
        post.scroll_into_view_if_needed()
        post.click()
        self._finish_post()

    def _wait_until_postable(self, video: Path, step: Callable[[str], None]):
        """Wait for the Post button, following the page's upload progress (upload_wait.py): a big file on a
        slow connection gets as long as it keeps moving, a stuck upload stops after a few minutes, and a big
        file's Post is only clicked once the upload is known to be done."""
        try:
            size = video.stat().st_size
        except OSError:
            size = 0
        watch = upload_wait.UploadWatch(size, time.time(), stall_seconds=upload_wait.STALL_SECONDS)
        if watch.big:
            step(f"Sending {size / upload_wait.MB:.0f} MB to TikTok; a big file takes a while…")
        while True:
            now = time.time()
            if watch.expired(now):
                self.screenshot("upload-timeout")
                raise BrowserError(f"TikTok didn't finish taking the video within {upload_wait.limit_for(size) / 60:.0f} "
                                   "minutes.")
            if watch.stalled(now):
                self.screenshot("upload-stalled")
                raise BrowserError(f"The upload to TikTok stopped at {watch.progress:.0f}% and hasn't moved for "
                                   f"{watch.stall_seconds / 60:.0f} minutes. Check the internet connection, then "
                                   "post it again.")
            self._check()
            self._wait_for_captcha()
            err = self.page.locator("text=/upload failed|couldn.t upload|unsupported file|file is too large/i")
            if err.count() and err.first.is_visible():
                raise BrowserError("TikTok rejected the file: " + (err.first.inner_text() or "upload failed").strip())
            btn = self._post_button()
            enabled = False
            if btn.count():
                b = btn.first
                disabled = (b.get_attribute("disabled") is not None or b.get_attribute("aria-disabled") == "true"
                            or b.get_attribute("data-disabled") == "true")
                enabled = not disabled and b.is_enabled()
            state = self._upload_state()
            watch.seen(now, state.get("pct"), bool(state.get("done")), enabled)
            reached = watch.report()
            if reached is not None and watch.big:
                step(f"Uploading to TikTok: {reached}%…")
            if watch.ready():
                return btn.first
            self._sleep(watch.delay)

    # --- the cover (posting/cover.py) ----------------------------------------------

    def _cover_editor(self):
        """The open cover editor, if it's showing."""
        return self._first_visible(COVER_EDITOR, timeout=0.6)

    def set_cover(self, picture: Path, timeout: float = 20.0) -> tuple[bool, str]:
        """Put `picture` in as the post's cover: Edit cover → Upload cover → the picture → Confirm. (done, why
        not). Anything missing closes the editor again and leaves TikTok's own pick; it never raises, except
        when the post is being cancelled."""
        try:
            edit = self._first_visible(EDIT_COVER, timeout=COVER_FIND)
            if edit is None:
                return False, "TikTok Studio showed no Edit cover button"
            edit.click()
            if self._first_visible(COVER_EDITOR, timeout=8) is None:
                return False, "the cover editor didn't open"
            tab = self._first_visible(UPLOAD_COVER_TAB, timeout=6)
            if tab is None:
                self._close_cover_editor()
                return False, "the cover editor has no Upload cover"
            tab.click()
            box = self.page.locator(COVER_INPUT)
            try:
                box.first.wait_for(state="attached", timeout=8000)
            except Exception:  # noqa: BLE001
                self._close_cover_editor()
                return False, "the cover editor has no place to upload a picture"
            box.first.set_input_files(str(picture))
            confirm = self._first_visible(COVER_CONFIRM, timeout=15)
            if confirm is None:
                self._close_cover_editor()
                return False, "the cover editor has no Confirm button"
            end = time.time() + timeout
            while time.time() < end:  # the picture loads into the editor before Confirm takes it
                self._check()
                if not (confirm.get_attribute("disabled") is not None
                        or confirm.get_attribute("aria-disabled") == "true"):
                    break
                self._sleep(0.5)
            confirm.click()
            end = max(end, time.time() + 8)
            while time.time() < end:  # the editor closing is the sign it took the picture
                self._check()
                if self._cover_editor() is None:
                    return True, ""
                self._sleep(0.5)
            self._close_cover_editor()
            return False, "the cover editor didn't close after Confirm"
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - a changed page: TikTok's own pick
            self._close_cover_editor()
            return False, f"the cover editor changed ({type(exc).__name__})"

    def _close_cover_editor(self) -> None:
        try:
            if self._cover_editor() is None:
                return
            for sel in COVER_CLOSE:
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=2000)
                    self.page.wait_for_timeout(500)
                    if self._cover_editor() is None:
                        return
            self.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass

    def _upload_state(self) -> dict:
        """The upload's progress as the page shows it: {"pct": 0-100 or None, "done": bool}."""
        try:
            got = self.page.evaluate(upload_wait.READ_PROGRESS)
        except Exception:  # noqa: BLE001 - the page navigated or closed mid-read: no reading this time
            return {}
        if not isinstance(got, dict):
            return {}
        pct = got.get("pct")
        return {"pct": float(pct) if isinstance(pct, (int, float)) and 0 <= pct <= 100 else None,
                "done": bool(got.get("done"))}

    def _open_upload_page(self) -> None:
        self.page.goto(UPLOAD_URL, wait_until="domcontentloaded")
        self._sleep(2)
        if "login" in self.page.url:
            raise BrowserError("TikTok asked to log in again. Use Account → Log in.")
        self._wait_for_captcha()
        self._dismiss_popups()

    def _file_input(self, timeout: float = 45):
        file_input = self.page.locator("input[type='file']")
        file_input.first.wait_for(state="attached", timeout=timeout * 1000)
        return file_input.first

    def _post_button(self):
        btn = self.page.locator("button[data-e2e='post_video_button']")
        if not btn.count():
            btn = self.page.locator("button:has-text('Post'):not(:has-text('Post now'))")
        return btn

    def check_upload_page(self, test_video: Path | None = None, expected_user: str = "") -> list[tuple[str, str, str]]:
        """Walk TikTok Studio's upload page as far as just before Post, without posting anything.

        Returns (step, "ok" | "warn" | "fail", detail) for each check; it stops at the first failure. With
        `test_video`, that clip is put in the uploader so the caption box, privacy picker and Post
        button can be found; the upload is then discarded and nothing is published. Without it,
        only the login and the page itself are checked.
        """
        results: list[tuple[str, str, str]] = []

        def ok(step: str, detail: str = "") -> None:
            results.append((step, "ok", detail))

        def fail(step: str, detail: str) -> list[tuple[str, str, str]]:
            results.append((step, "fail", detail))
            self.screenshot("upload-check")
            return results

        if not self.logged_in():
            return fail("Logged in", "This account's browser isn't logged in to TikTok. Use Account → Log in.")
        ok("Logged in")
        if expected_user:
            try:
                self.check_user(expected_user)
            except BrowserError as exc:
                return fail("Right account", str(exc))
            ok("Right account", f"@{expected_user.lstrip('@')}")
        try:
            self._open_upload_page()
        except BrowserError as exc:
            return fail("Upload page opens", str(exc))
        ok("Upload page opens")
        try:
            file_input = self._file_input(timeout=30)
        except Exception:
            return fail("Video picker", "The upload page has no file picker any more; TikTok has changed it.")
        ok("Video picker")
        if test_video is None:
            return results

        file_input.set_input_files(str(test_video))
        try:
            if not self._caption_editor(timeout=60):
                return fail("Caption box", "The caption box didn't appear after choosing a video.")
            ok("Caption box")
            self._dismiss_popups()
            if self._privacy_box(timeout=8):
                ok("Who can watch")
            else:  # posting still works without it (TikTok's default is used), so it's only a warning
                results.append(("Who can watch", "warn", "Not found, so posts would use TikTok's default privacy."))
            end = time.time() + 30
            while time.time() < end and not self._post_button().count():
                self._sleep(1)
            if not self._post_button().count():
                return fail("Post button", "The Post button wasn't found.")
            ok("Post button", "found; not clicked")
            if self._first_visible(EDIT_COVER, timeout=min(6.0, COVER_FIND)) is not None:
                ok("Cover editor", "found, so covers can be set")
            else:  # posts still go up, with TikTok's own cover
                results.append(("Cover editor", "warn", "Not found, so posts would keep TikTok's own cover."))
        finally:
            self._discard_upload()
        return results

    def _discard_upload(self) -> None:
        """Throw away a video put in the uploader by check_upload_page(), so it isn't left as a draft."""
        try:
            for sel in ("button:has-text('Discard')", "div[role='dialog'] button:has-text('Discard')"):
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    self.page.wait_for_timeout(800)
            self.page.goto("about:blank")  # dialogs ("leave this page?") are accepted by __enter__'s handler
        except Exception:
            pass

    def _finish_post(self) -> None:
        end = time.time() + 180
        while time.time() < end:
            self._check()
            # "Continue to post?" / content check prompt
            for sel in ("button:has-text('Post now')", "div[role='dialog'] button:has-text('Post')"):
                loc = self.page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=3000)
                    self.page.wait_for_timeout(800)
            if "/tiktokstudio/content" in self.page.url or "/tiktokstudio/post" in self.page.url:
                return
            done = self.page.locator("text=/your video (has been|is being) (uploaded|posted)|video published|"
                                     "leaving the page does not interrupt|manage your posts/i")
            if done.count():
                self._sleep(2)
                return
            self._wait_for_captcha()
            self._sleep(1)
        self.screenshot("post-timeout")
        raise BrowserError("Clicked Post but TikTok never confirmed it. Check TikTok Studio before retrying, "
                           "so it isn't posted twice.")

    def screenshot(self, name: str) -> Path | None:
        try:
            path = cache_dir() / "logs" / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
            self.log(f"Saved a screenshot of the page: {path}")
            return path
        except Exception:
            return None


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _short_number(text: str | None) -> int:
    """'1.2M' -> 1200000, '845' -> 845."""
    m = re.match(r"\s*([\d.,]+)\s*([KMB]?)", (text or "").upper())
    if not m:
        return 0
    num = float(m[1].replace(",", ""))
    return int(num * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m[2]])


def _video_from_item(it: dict, username: str) -> TikTokVideo | None:
    vid = str(it.get("id") or it.get("item_id") or "")
    if not vid:
        return None
    stats = it.get("statsV2") or it.get("stats") or {}
    video = it.get("video") or {}
    return TikTokVideo(
        id=vid,
        description=it.get("desc") or "",
        create_time=float(it.get("createTime") or 0),
        url=f"https://www.tiktok.com/@{username}/video/{vid}",
        cover=video.get("cover") or video.get("originCover") or "",
        duration=float(video.get("duration") or 0),
        views=_int(stats.get("playCount")),
        likes=_int(stats.get("diggCount")),
        comments=_int(stats.get("commentCount")),
        shares=_int(stats.get("shareCount")),
        saves=_int(stats.get("collectCount")),
    )
