"""A stand-in for the account's TikTok browser (tiktok.browser.TikTokBrowser), for the live view's tests.

`install(monkeypatch)` swaps it in. It "reads" the profile page from the numbers on the object,
newest videos first, up to the `max_videos` asked for, and records each opening (the profile
folder and whether the window was hidden). `fail_with` raises that exception on the next
openings; `withhold_videos` answers with the profile but no videos, as TikTok does with a captcha.
"""

from __future__ import annotations

import threading
import time

from ytclip.posting.store import Profile, TikTokVideo


class FakeTikTok:
    def __init__(self, username: str = "cats"):
        self.username = username
        self.followers = 10
        self.likes = 100
        self.videos: list[TikTokVideo] = []
        self.fail_with: Exception | None = None
        self.withhold_videos = False
        self.grid_only = False  # videos as read off the grid: link and views only
        self.session = True  # the profile still has its TikTok login
        self.opened: list[dict] = []  # one per browser opening: profile folder, headless, max_videos
        self.lock = threading.Lock()

    def add_video(self, vid: str, desc: str, views: int = 0, likes: int = 0, comments: int = 0, shares: int = 0,
                  create_time: float | None = None) -> TikTokVideo:
        v = TikTokVideo(id=vid, description=desc, create_time=create_time or time.time(), views=views, likes=likes,
                        comments=comments, shares=shares, url=f"https://www.tiktok.com/@{self.username}/video/{vid}")
        with self.lock:
            self.videos.append(v)
            self.videos.sort(key=lambda x: x.create_time, reverse=True)
        return v

    def bump(self, vid: str, views: int = 0, likes: int = 0, comments: int = 0, shares: int = 0) -> None:
        with self.lock:
            v = next(v for v in self.videos if v.id == vid)
            v.views += views
            v.likes += likes
            v.comments += comments
            v.shares += shares

    def readings(self) -> int:
        return sum(1 for o in self.opened if "max_videos" in o)

    def install(self, monkeypatch) -> "FakeTikTok":
        from ytclip.posting.tiktok import browser

        fake = self

        class Browser:
            def __init__(self, settings, profile_dir, log=print, headless=None, cancelled=lambda: False):
                self.entry = {"profile_dir": profile_dir,
                              "headless": (not settings.show_browser) if headless is None else headless}

            def __enter__(self):
                if fake.fail_with is not None:
                    raise fake.fail_with
                return self

            def __exit__(self, *exc):
                return False

            def require_login(self):
                if not fake.session:
                    raise browser.BrowserError("Not logged in to TikTok. Use Account → Log in first.")

            def logged_in(self):
                return fake.session

            def fetch_account(self, username, max_videos=120, captcha_wait=None):
                with fake.lock:
                    fake.opened.append({**self.entry, "max_videos": max_videos, "captcha_wait": captcha_wait})
                    vids = [] if fake.withhold_videos else [TikTokVideo(**{**v.__dict__}) for v in fake.videos]
                    if fake.grid_only:
                        vids = [TikTokVideo(id=v.id, description=v.description, views=v.views, url=v.url) for v in vids]
                    p = Profile(username=fake.username, nickname="Cats", followers=fake.followers, likes=fake.likes,
                                video_count=len(fake.videos), fetched_at=time.time())
                return p, vids[:max_videos]

        monkeypatch.setattr(browser, "TikTokBrowser", Browser)
        return self
