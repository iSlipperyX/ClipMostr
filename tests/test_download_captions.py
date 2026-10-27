import yt_dlp

from ytclip import download

INFO = {"id": "abcdefghijk", "webpage_url": "https://youtu.be/abcdefghijk", "language": "en",
        "automatic_captions": {"en-orig": [{"ext": "json3", "url": "https://x/j"},
                                           {"ext": "vtt", "url": "https://x/v"}]}}


class FakeYDL:
    opened: list = []
    body = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def urlopen(self, url):
        FakeYDL.opened.append(url)
        body = FakeYDL.body

        class R:
            def read(self):
                return body
        return R()

    def extract_info(self, *a, **k):
        raise AssertionError("loading the video page again")


def test_captions_come_from_the_known_track_in_one_request(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.opened = []
    path = download._download_captions(INFO, tmp_path, None, None)
    assert FakeYDL.opened == ["https://x/v"] and path.read_bytes().startswith(b"WEBVTT")
    assert download._download_captions(INFO, tmp_path, None, None) == path and len(FakeYDL.opened) == 1  # cached


def test_a_page_instead_of_captions_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(FakeYDL, "body", b"<html>Sign in to confirm you're not a bot</html>")
    logs = []
    assert download._download_captions(INFO, tmp_path, None, logs.append) is None
    assert not list(tmp_path.iterdir()) and "continuing without them" in logs[-1]


def test_requests_are_spaced_out():
    assert download._base_opts(None, None)["sleep_interval_requests"] >= 1
    assert download.is_blocked("ERROR: [youtube] x: Sign in to confirm you're not a bot")
