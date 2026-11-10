"""Where a scene's picture comes from: your B-roll, the stock sites' APIs (faked here), a picture model, a
texture. Nothing in this file touches the network."""

import json
import urllib.parse
from pathlib import Path

import cv2
import numpy as np
import pytest

from ytclip.creator import visual_generator as vg
from ytclip.creator.director import Scene, Storyboard
from ytclip.ffmpeg import MediaInfo
from ytclip.settings import Settings
from ytclip.studio import visual_sourcer as vs

VERTICAL, WIDE = (1080, 1920), (1920, 1080)


def pexels_videos(*items):
    """items: (id, width, height, seconds)"""
    return {"videos": [{
        "id": i, "width": w, "height": h, "duration": d, "url": f"https://www.pexels.com/video/{i}/",
        "user": {"name": f"Author {i}", "url": f"https://www.pexels.com/@a{i}"},
        "video_files": [
            {"quality": "sd", "file_type": "video/mp4", "width": w // 2, "height": h // 2,
             "link": f"https://videos.pexels.com/video-files/{i}/sd.mp4"},
            {"quality": "hd", "file_type": "video/mp4", "width": w, "height": h,
             "link": f"https://videos.pexels.com/video-files/{i}/hd.mp4"},
            {"quality": "uhd", "file_type": "video/mp4", "width": w * 2, "height": h * 2,
             "link": f"https://videos.pexels.com/video-files/{i}/uhd.mp4"},
            {"quality": "hls", "file_type": "application/x-mpegURL", "width": w, "height": h,
             "link": f"https://videos.pexels.com/video-files/{i}/master.m3u8"},
        ]} for i, w, h, d in items]}


def pexels_photos(*items):
    return {"photos": [{
        "id": i, "width": w, "height": h, "url": f"https://www.pexels.com/photo/{i}/", "photographer": f"Snapper {i}",
        "photographer_url": f"https://www.pexels.com/@s{i}",
        "src": {"original": f"https://images.pexels.com/photos/{i}/o.jpeg",
                "large2x": f"https://images.pexels.com/photos/{i}/o.jpeg?dpr=2&h=650&w=940"}} for i, w, h in items]}


def pixabay_videos(*items):
    return {"hits": [{
        "id": i, "pageURL": f"https://pixabay.com/videos/id-{i}/", "duration": d, "user": "sam", "user_id": 7,
        "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0},
                   "medium": {"url": f"https://cdn.pixabay.com/video/{i}_medium.mp4", "width": w, "height": h,
                              "size": size},
                   "tiny": {"url": f"https://cdn.pixabay.com/video/{i}_tiny.mp4", "width": w // 4, "height": h // 4,
                            "size": size // 16}}} for i, w, h, d, size in items]}


def pixabay_photos(*items):
    return {"hits": [{"id": i, "pageURL": f"https://pixabay.com/photos/id-{i}/", "imageWidth": w, "imageHeight": h,
                      "largeImageURL": f"https://pixabay.com/get/{i}_1280.jpg", "user": "ann", "user_id": 9}
                     for i, w, h in items]}


def save_picture(dest, width, height):
    ok, data = cv2.imencode(Path(dest).suffix or ".png", np.full((height, width, 3), 60, np.uint8))
    assert ok
    Path(dest).write_bytes(data.tobytes())
    return Path(dest)


class Web:
    """A pretend Pexels and Pixabay. `answers`: (provider, kind) -> a dict, or a function of the query."""

    def __init__(self, answers=None, sizes=None, fail=None):
        self.answers, self.sizes, self.fail = answers or {}, sizes or {}, fail or {}
        self.calls, self.fetched = [], []

    def http(self, url, headers, timeout):
        parts = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(parts.query)
        provider = "pexels" if parts.hostname == "api.pexels.com" else "pixabay"
        kind = "video" if "/videos" in parts.path else "image"
        query = (q.get("query") or q.get("q"))[0]
        self.calls.append((provider, kind, query, dict(headers), {k: v[0] for k, v in q.items()}))
        if (provider, kind) in self.fail:
            raise vs.StockError(f"HTTP {self.fail[(provider, kind)]}", self.fail[(provider, kind)])
        answer = self.answers.get((provider, kind), {})
        if callable(answer):
            answer = answer(query)
        return json.dumps(answer).encode()

    def download(self, url, dest, cap, check=None):
        self.fetched.append(url)
        if url in self.fail:
            raise vs.StockError(f"HTTP {self.fail[url]}", self.fail[url])
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        if str(dest).endswith(".mp4"):
            Path(dest).write_bytes(b"clip")
        else:
            save_picture(dest, *self.sizes.get(url, (1080, 1920)))
        return Path(dest)

    def probe_for(self, sizes=None, seconds=None):
        def probe(path):
            name = Path(path).name
            for key, (w, h, d) in (sizes or {}).items():
                if key in name:
                    return MediaInfo(duration=d, width=w, height=h)
            return MediaInfo(duration=seconds or 12.0, width=1080, height=1920)
        return probe


def settings(**over):
    return Settings().apply({"pexels_api_key": "PEX-KEY", "pixabay_api_key": "PIX-KEY", **over})


def stock(tmp_path, web, s=None, canvas=VERTICAL, log=None, **kw):
    return vs.Stock(s or settings(), tmp_path / "scenes", canvas=canvas, log=log, http=web.http,
                    download=web.download, probe=kw.pop("probe", web.probe_for()), cache=tmp_path / "cache", **kw)


def scene(i=1, kind="broll", query="city skyline night", **kw):
    return Scene(i, kw.pop("text", "The city never sleeps and neither do its secrets"), kind,
                 kw.pop("prompt", "a wide cinematic shot of a rainy city skyline at night"),
                 keywords=kw.pop("keywords", ["city", "night"]), stock_query=query, **kw)


def board(*scenes):
    return Storyboard("Night city", scenes=list(scenes))


NOTHING_DRAWS = {"comfyui": "not running", "sdxl_turbo": "not set up", "flux_schnell": "not set up",
                 "local_broll": "no folder", "texture": ""}


# ---------------------------------------------------------------- searching

def test_search_words_come_from_the_script_then_keywords_then_the_description():
    sc = scene(query="City skyline, at NIGHT!", keywords=["Tokyo", "neon signs"])
    assert vs.queries_for(sc) == ["city skyline at night", "tokyo neon signs", "rainy city skyline"]
    bare = Scene(2, "Words", visual_prompt="A dramatic close up photo of the old lighthouse keeper")
    assert vs.queries_for(bare) == ["old lighthouse keeper"]
    assert vs.queries_for(Scene(3, "Words")) == []


def test_the_documented_addresses_are_asked_with_the_key_where_each_site_wants_it(tmp_path):
    web = Web()
    st = stock(tmp_path, web)
    for provider in ("pexels", "pixabay"):
        for kind in ("video", "image"):
            st.search(provider, kind, "red fox")
    by = {(c[0], c[1]): c for c in web.calls}
    assert by[("pexels", "video")][3] == {"Authorization": "PEX-KEY"}
    assert by[("pexels", "video")][4] == {"query": "red fox", "orientation": "portrait", "per_page": "15"}
    assert "key" not in by[("pexels", "image")][4]
    assert by[("pixabay", "video")][3] == {} and by[("pixabay", "video")][4]["key"] == "PIX-KEY"
    assert by[("pixabay", "video")][4]["safesearch"] == "true"
    assert by[("pixabay", "image")][4]["orientation"] == "vertical"
    assert by[("pixabay", "image")][4]["image_type"] == "photo"
    assert vs.search_url("pexels", "video", "x", VERTICAL).startswith("https://api.pexels.com/videos/search?")
    assert vs.search_url("pexels", "image", "x", WIDE).startswith("https://api.pexels.com/v1/search?")
    assert "orientation=landscape" in vs.search_url("pexels", "image", "x", WIDE)
    assert vs.search_url("pixabay", "video", "x", WIDE, "K").startswith("https://pixabay.com/api/videos/?key=K")
    assert "orientation=horizontal" in vs.search_url("pixabay", "image", "x", WIDE, "K")
    assert "orientation" not in vs.search_url("pixabay", "image", "x", (1080, 1080), "K")


def test_an_answer_is_kept_for_a_day_and_not_asked_twice(tmp_path):
    web = Web({("pexels", "video"): pexels_videos((1, 1080, 1920, 12))})
    clock = [1000.0]
    st = stock(tmp_path, web, now=lambda: clock[0])
    assert [h.id for h in st.search("pexels", "video", "fox")] == ["1"]
    st.search("pexels", "video", "fox")
    again = stock(tmp_path, web, now=lambda: clock[0])  # another video, the same day
    assert [h.id for h in again.search("pexels", "video", "fox")] == ["1"]
    assert len(web.calls) == 1 and again.searches == 0
    clock[0] += vs.CACHE_SECONDS + 1
    later = stock(tmp_path, web, now=lambda: clock[0])
    later.search("pexels", "video", "fox")
    assert len(web.calls) == 2
    saved = "".join(p.read_text() for p in (tmp_path / "cache").glob("*.json"))
    assert "PEX-KEY" not in saved and "PIX-KEY" not in saved  # the cache never holds a key


def test_the_right_file_of_a_clip_is_chosen_for_the_canvas():
    hit = vs.parse_pexels_videos(pexels_videos((5, 1080, 1920, 9)), VERTICAL)[0]
    assert (hit.width, hit.height) == (1080, 1920) and hit.file.endswith("/hd.mp4")  # not UHD, not the HLS list
    assert hit.credit == "Video by Author 5 on Pexels" and hit.page == "https://www.pexels.com/video/5/"
    small = vs.parse_pexels_videos(pexels_videos((6, 720, 1280, 9)), VERTICAL)[0]
    assert (small.width, small.height) == (1440, 2560)  # the HD file would be enlarged, the next one up isn't
    pix = vs.parse_pixabay_videos(pixabay_videos((7, 1920, 1080, 20, 9_000_000)), WIDE)[0]
    assert pix.file.endswith("7_medium.mp4") and pix.size == 9_000_000 and pix.author == "sam"
    assert pix.author_url == "https://pixabay.com/users/sam-7/"
    photo = vs.parse_pexels_photos(pexels_photos((8, 6000, 9000)), VERTICAL)[0]
    assert "dpr=2" in photo.file and (photo.width, photo.height) == (867, 1300)  # a huge original: the 2x size
    assert vs.parse_pexels_photos(pexels_photos((9, 2000, 3000)), VERTICAL)[0].file.endswith("o.jpeg")
    p = vs.parse_pixabay_photos(pixabay_photos((10, 4000, 6000)), VERTICAL)[0]
    assert (p.width, p.height) == (853, 1280) and p.credit == "Photo by ann on Pixabay"
    assert vs.parse_pexels_videos({"videos": [None, {"id": 1}, "x"]}, VERTICAL) == []
    assert vs.parse_pixabay_photos({"hits": [None, {"id": 3}]}, VERTICAL) == []
    assert vs.parse_pixabay_videos({}, VERTICAL) == [] and vs.parse_pexels_photos(None, VERTICAL) == []


def test_a_clip_must_suit_the_canvas_the_scene_and_the_size_limit(tmp_path):
    st = stock(tmp_path, Web())
    ok = vs.StockHit("pexels", "video", "1", "https://videos.pexels.com/a.mp4", "", width=1080, height=1920, duration=8)
    assert st.score(ok, 5.0) is not None
    assert st.score(vs.replace(ok, width=1920, height=1080), 5.0) is None  # a wide clip in an upright video
    assert st.score(vs.replace(ok, width=540, height=960), 5.0) is None  # would be doubled in size
    assert st.score(vs.replace(ok, duration=3.0), 5.0) is None  # too short for the scene
    assert st.score(vs.replace(ok, duration=4.6), 5.0) < st.score(ok, 5.0)  # nearly long enough: allowed, marked down
    assert st.score(vs.replace(ok, duration=400.0), 5.0) is None
    assert st.score(vs.replace(ok, size=vs.MAX_VIDEO_BYTES + 1), 5.0) is None
    assert st.score(vs.replace(ok, rank=6), 5.0) < st.score(ok, 5.0)  # the site's own order counts
    square = vs.replace(ok, width=1920, height=1920)
    assert st.score(square, 5.0) < st.score(ok, 5.0)  # usable, but more of it is cropped away
    st.used.add(ok.key)
    assert st.score(ok, 5.0) is None
    wide = stock(tmp_path, Web(), canvas=WIDE)
    assert wide.score(vs.replace(ok, width=1920, height=1080), 5.0) is not None


def test_downloads_only_start_at_the_sites_own_https_addresses():
    assert vs.allowed_download("pexels", "https://videos.pexels.com/video-files/1/a.mp4")
    assert vs.allowed_download("pexels", "https://player.vimeo.com/external/1.hd.mp4?s=x")
    assert vs.allowed_download("pixabay", "https://cdn.pixabay.com/video/1.mp4")
    assert vs.allowed_download("pixabay", "https://pixabay.com/get/x_1280.jpg")
    for bad in ("http://videos.pexels.com/a.mp4", "https://pexels.com.evil.example/a.mp4", "file:///etc/passwd",
                "https://user:pw@videos.pexels.com/a.mp4", "https://127.0.0.1/a.mp4", "https://evilpexels.com/a.mp4",
                "", "https://[bad"):
        assert not vs.allowed_download("pexels", bad), bad
    assert not vs.allowed_download("pixabay", "https://videos.pexels.com/a.mp4")


def test_an_answer_pointing_somewhere_else_is_never_downloaded(tmp_path):
    answer = pexels_videos((1, 1080, 1920, 12))
    for f in answer["videos"][0]["video_files"]:
        f["link"] = "https://example.com/tracker.mp4"
    web = Web({("pexels", "video"): answer})
    st = stock(tmp_path, web, s=settings(pixabay_api_key=""))
    assert st.find(scene(), 4.0, ["video"]) is None and web.fetched == []


# ---------------------------------------------------------------- one scene

def test_a_scene_gets_a_clip_with_its_credit_and_licence(tmp_path):
    web = Web({("pexels", "video"): pexels_videos((11, 1080, 1920, 20), (12, 1080, 1920, 9))})
    log = []
    st = stock(tmp_path, web, log=log.append, probe=web.probe_for(seconds=20.0))
    v = st.find(scene(3), 5.0)
    assert v.kind == "video" and v.source == "pexels" and v.scene_id == 3
    assert v.path == tmp_path / "scenes" / "scene_03_pexels_11.mp4" and v.path.is_file()
    assert v.credit == "Video by Author 11 on Pexels" and v.url == "https://www.pexels.com/video/11/"
    assert "Pexels License" in v.licence and v.query == "city skyline night"
    assert (v.width, v.height, v.duration) == (1080, 1920, 20.0)
    assert v.start == 1.6  # a little way in, when the clip has room
    assert len(web.calls) == 1  # the first search was enough: one request
    assert any("searching Pexels and Pixabay" in m for m in log)
    again = st.find(scene(4), 5.0)  # the same search words: answered from memory, and a different clip
    assert again.path.name == "scene_04_pexels_12.mp4" and len(web.calls) == 1


def test_photos_are_the_fallback_and_the_other_site_is_asked_when_one_has_nothing(tmp_path):
    web = Web({("pixabay", "image"): pixabay_photos((21, 3000, 4500))},
              sizes={"https://pixabay.com/get/21_1280.jpg": (853, 1280)})
    st = stock(tmp_path, web)
    v = st.find(scene(1), 4.0)
    assert v.kind == "image" and v.source == "pixabay" and v.path.suffix == ".jpg"
    assert (v.width, v.height) == (853, 1280) and v.duration == 0 and v.start == 0
    assert v.credit == "Photo by ann on Pixabay" and "Pixabay Content License" in v.licence
    asked = [(c[0], c[1], c[2]) for c in web.calls]
    assert asked[0] == ("pexels", "video", "city skyline night")  # videos first, Pexels first
    assert ("pixabay", "video", "city night") in asked  # the second search words, before giving up on video
    assert asked[-1] == ("pixabay", "image", "city skyline night")
    photos_first = stock(tmp_path / "b", web, s=settings(studio_stock_video=False))
    assert photos_first.kinds() == ["image", "video"]


def test_a_file_that_is_not_what_the_site_said_is_dropped_and_the_next_one_tried(tmp_path):
    web = Web({("pexels", "video"): pexels_videos((31, 1080, 1920, 20), (32, 1080, 1920, 20))})
    log = []
    probe = web.probe_for(sizes={"pexels_31": (640, 360, 2.0)}, seconds=20.0)
    st = stock(tmp_path, web, log=log.append, probe=probe)
    v = st.find(scene(1), 5.0)
    assert v.path.name == "scene_01_pexels_32.mp4"
    assert not (tmp_path / "scenes" / "scene_01_pexels_31.mp4").exists()
    assert any("was skipped" in m for m in log)


def test_a_moved_file_forgets_the_old_answer_and_gives_up_after_a_few_tries(tmp_path):
    items = [(i, 1080, 1920, 20) for i in range(40, 46)]
    gone = {f"https://videos.pexels.com/video-files/{i}/hd.mp4": 404 for i, *_ in items}
    web = Web({("pexels", "video"): pexels_videos(*items)}, fail=gone)
    st = stock(tmp_path, web, s=settings(pixabay_api_key=""))
    assert st.find(scene(1), 5.0, ["video"]) is None
    assert len(web.fetched) == vs.DOWNLOADS_PER_SCENE
    assert not list((tmp_path / "cache").glob("*.json"))  # asked afresh next time


@pytest.mark.parametrize("status, words", [(429, "fewer requests"), (401, "refused the API key"), (0, "couldn't be reached")])
def test_a_site_that_says_no_is_left_alone_for_the_rest_of_the_video(tmp_path, status, words):
    web = Web({("pixabay", "video"): pixabay_videos((51, 1080, 1920, 15, 5_000_000))},
              fail={("pexels", "video"): status})
    log = []
    st = stock(tmp_path, web, log=log.append)
    v = st.find(scene(1), 5.0)
    assert v.source == "pixabay" and st.providers() == ["pixabay"]
    assert any(words in m for m in log)
    st.find(scene(2, query="forest path"), 5.0)
    assert [c[0] for c in web.calls].count("pexels") == 1  # not asked again
    assert not any("PEX-KEY" in m or "PIX-KEY" in m for m in log)


def test_no_key_or_switched_off_means_no_stock(tmp_path, monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    web = Web()
    assert "no stock footage key" in stock(tmp_path, web, s=Settings()).missing()
    assert "turned off" in stock(tmp_path, web, s=settings(studio_stock=False)).missing()
    assert stock(tmp_path, web).missing() == ""
    monkeypatch.setenv("PIXABAY_API_KEY", "from-env")
    st = stock(tmp_path, web, s=Settings())
    assert st.missing() == "" and st.providers() == ["pixabay"]


def test_the_real_http_helper_never_puts_the_address_in_an_error(monkeypatch):
    import urllib.error
    import urllib.request

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(vs.StockError) as err:
        vs._http("https://pixabay.com/api/?key=SECRET&q=x", {}, 5.0)
    assert err.value.status == 429 and "SECRET" not in str(err.value)

    def offline(req, timeout=0):
        raise urllib.error.URLError(OSError("no route to pixabay.com/api/?key=SECRET"))

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    with pytest.raises(vs.StockError) as err:
        vs._http("https://pixabay.com/api/?key=SECRET&q=x", {}, 5.0)
    assert "SECRET" not in str(err.value)


def test_the_real_download_stops_at_the_size_limit(tmp_path, monkeypatch):
    import io
    import urllib.request

    class Reply(io.BytesIO):
        headers = {}

        def geturl(self):
            return "https://videos.pexels.com/a.mp4"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Reply(b"x" * 5000))
    dest = tmp_path / "a.mp4"
    assert vs._download("https://videos.pexels.com/a.mp4", dest, 10_000).read_bytes() == b"x" * 5000
    with pytest.raises(vs.StockError, match="over"):
        vs._download("https://videos.pexels.com/a.mp4", tmp_path / "b.mp4", 1000)
    assert not (tmp_path / "b.mp4").exists() and not (tmp_path / "b.mp4.part").exists()

    class Told(Reply):
        headers = {"Content-Length": "999999999"}

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Told(b"x"))
    with pytest.raises(vs.StockError, match="limit"):
        vs._download("https://videos.pexels.com/a.mp4", tmp_path / "c.mp4", 1000)

    class Downgraded(Reply):
        def geturl(self):
            return "http://elsewhere.example/a.mp4"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: Downgraded(b"x"))
    with pytest.raises(vs.StockError, match="https"):
        vs._download("https://videos.pexels.com/a.mp4", tmp_path / "d.mp4", 1000)


# ---------------------------------------------------------------- every scene

def test_footage_scenes_go_broll_then_stock_and_the_rest_are_textured(tmp_path):
    lib = tmp_path / "broll"
    (lib / "city").mkdir(parents=True)
    (lib / "city" / "skyline night.mp4").write_bytes(b"x")
    web = Web({("pexels", "video"): lambda q: pexels_videos((61, 1080, 1920, 14)) if "ocean" in q else {}})
    s = settings(broll_folder=str(lib), pixabay_api_key="")
    b = board(scene(1), scene(2, query="ocean waves", keywords=["ocean"], text="Far out at sea the water moves"),
              scene(3, kind="texture", query="", text="Here is the plain truth"),
              scene(4, query="glacier cave", keywords=["glacier"], text="Deep inside the ice nothing stirs"))
    log = []
    out = vs.source_all(b, s, tmp_path / "scenes", seconds={1: 4.0, 2: 5.0, 3: 3.0, 4: 4.0}, log=log.append,
                        stock=stock(tmp_path, web, s=s, probe=web.probe_for(seconds=14.0)),
                        probe=lambda p: MediaInfo(duration=30.0, width=1920, height=1080), ready=NOTHING_DRAWS)
    assert [v.scene_id for v in out] == [1, 2, 3, 4]
    assert [v.source for v in out] == ["local_broll", "pexels", "texture", "texture"]
    assert out[0].path == lib / "city" / "skyline night.mp4" and out[0].duration == 30.0 and out[0].start == 2.0
    assert out[1].credit and out[1].path.parent == tmp_path / "scenes"
    assert out[2].path.name == "scene_03.png" and (out[2].width, out[2].height) == (1350, 2400)
    assert any("Pictures used: 2 built-in textures, 1 from your B-roll, 1 from Pexels" in m for m in log)
    assert not any(c[2].startswith("city") for c in web.calls)  # your own footage came first: no search for it
    assert vs.credits(out) == ["Scene 2: Video by Author 61 on Pexels (https://www.pexels.com/video/61/)"]
    assert vs.credit_summary(out) == "Stock footage from Pexels."


def test_illustration_scenes_are_drawn_when_a_model_is_ready_and_stock_rescues_failures(tmp_path):
    web = Web({("pexels", "image"): pexels_photos((71, 3000, 2000), (72, 3000, 2000))},
              sizes={f"https://images.pexels.com/photos/{i}/o.jpeg": (3000, 2000) for i in (71, 72)})
    s = settings(pixabay_api_key="", studio_stock_video=False)
    drawn = []

    class FakeComfy:
        def generate(self, prompt, negative, seed, dest, width=768, height=1344):
            drawn.append((prompt, width, height))
            if "broken" in prompt:
                raise RuntimeError("out of memory")
            return save_picture(dest, width, height)

    b = board(scene(1, kind="generated_image", prompt="a fox"), scene(2, kind="broll"),
              scene(3, kind="generated_image", prompt="a broken thing"))
    ready = {**NOTHING_DRAWS, "comfyui": ""}
    out = vs.source_all(b, s, tmp_path / "scenes", canvas=WIDE, stock=stock(tmp_path, web, s=s, canvas=WIDE),
                        comfy=FakeComfy(), ready=ready)
    # Scene 2 is footage: stock. Scene 3's drawing failed: stock again (another photo), not a texture.
    assert [v.source for v in out] == ["comfyui", "pexels", "pexels"]
    assert out[1].path.name != out[2].path.name and not (tmp_path / "scenes" / "scene_03.png").exists()
    assert drawn[0] == ("a fox", 1344, 768)  # drawn wide for a wide video
    assert (out[0].width, out[0].height) == (1344, 768)


def test_stock_rescues_a_scene_that_would_have_been_a_texture(tmp_path):
    web = Web({("pexels", "image"): pexels_photos((81, 2000, 3000))},
              sizes={"https://images.pexels.com/photos/81/o.jpeg": (2000, 3000)})
    s = settings(pixabay_api_key="", studio_stock_video=False)

    class Broken:
        def generate(self, *a, **k):
            raise RuntimeError("the card ran out of memory")

    b = board(scene(1, kind="generated_image", prompt="a lighthouse", query="lighthouse storm"))
    log = []
    out = vs.source_all(b, s, tmp_path / "scenes", stock=stock(tmp_path, web, s=s), comfy=Broken(),
                        ready={**NOTHING_DRAWS, "comfyui": ""}, log=log.append)
    assert out[0].source == "pexels" and out[0].kind == "image"
    assert not (tmp_path / "scenes" / "scene_01.png").exists()  # the texture it replaced is gone
    assert any("ComfyUI couldn't make the picture" in m for m in log)


def test_choosing_stock_puts_it_first_and_choosing_a_maker_leaves_stock_out(tmp_path):
    web = Web({("pexels", "video"): lambda q: pexels_videos((abs(hash(q)) % 10_000, 1080, 1920, 14))})
    s = settings(pixabay_api_key="")
    b = board(scene(1, kind="generated_image", query="red fox snow"), scene(2, kind="generated_image", query="owl"))
    out = vs.source_all(b, s, tmp_path / "a", choice="stock", stock=stock(tmp_path, web, s=s),
                        ready={**NOTHING_DRAWS, "comfyui": ""})
    assert [v.source for v in out] == ["pexels", "pexels"]
    calls = len(web.calls)
    out = vs.source_all(b, s, tmp_path / "b", choice="texture", stock=stock(tmp_path, web, s=s))
    assert [v.source for v in out] == ["texture", "texture"] and len(web.calls) == calls
    log = []
    out = vs.source_all(b, Settings().apply({"studio_stock": False}), tmp_path / "c", choice="stock",
                        ready=NOTHING_DRAWS, log=log.append)
    assert [v.source for v in out] == ["texture", "texture"]
    assert any("Stock footage was asked for but can't be used" in m for m in log)


def test_wide_videos_get_wide_textures_and_assets_carry_the_credit(tmp_path):
    out = vs.source_all(board(scene(1, kind="texture")), Settings(), tmp_path / "s", canvas=WIDE, choice="texture")
    assert (out[0].width, out[0].height) == (2400, 1350)
    a = vs.asset_of(out[0])
    assert (a.kind, a.source, a.width, a.credit) == ("image", "built-in textures", 2400, "")
    v = vs.SourcedVisual(2, "video", Path("x.mp4"), "pixabay", "p", credit="Video by sam on Pixabay",
                         url="https://pixabay.com/videos/id-1/", licence=vs.LICENCES["pixabay"], duration=9.0,
                         width=1920, height=1080, query="owl")
    a = vs.asset_of(v)
    assert (a.kind, a.source, a.duration, a.credit, a.url, a.prompt) == \
        ("video", "Pixabay", 9.0, "Video by sam on Pixabay", "https://pixabay.com/videos/id-1/", "owl")
    assert vs.describe([v, out[0]]) == "1 from Pixabay, 1 built-in texture"
    plain = vg.SceneVisual(1, "image", out[0].path, "sdxl_turbo")
    assert vs.asset_of(plain).source == "SDXL-Turbo" and vs.asset_of(plain).width == 2400


def test_a_clip_starts_a_little_way_in_only_when_there_is_room():
    assert vs.start_in(20.0, 5.0) == 1.6
    assert vs.start_in(60.0, 5.0) == 2.0
    assert vs.start_in(5.2, 5.0) == 0.0
    assert vs.start_in(6.0, 5.0) == 0.48
    assert vs.start_in(4.0, 5.0) == 0.0
