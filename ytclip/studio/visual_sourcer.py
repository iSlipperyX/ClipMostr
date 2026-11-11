"""Something to look at for every scene, from the nearest place that has a good one.

    visuals = source_all(board, settings, work / "scenes", canvas=(1080, 1920), seconds={1: 3.2, 2: 4.0})
    visuals[0].path, .kind, .source, .credit      # a picture or a clip, where it came from, who to credit

Where a scene's picture comes from, in order:

1. **Your own B-roll folder** (Settings → Editing → B-roll folder), matched by the tags in its file and
   folder names. Nothing is downloaded.
2. **Stock footage** from Pexels and Pixabay, through their official APIs with your own (free) API keys
   (Settings → Creator). A scene's search words are sent to the site; nothing else is. A clip is chosen to
   suit the canvas (upright for 9:16, wide for 16:9), to be sharp enough, and to last as long as the scene,
   and no clip is used twice in a video. Its author, page and licence are kept with it for the credits.
3. **A picture made on this PC** by the Creator's picture models (ComfyUI, SDXL-Turbo, FLUX.1-schnell), in
   the canvas's shape.
4. **A built-in texture**, which always works.

A scene the script wrote as an illustration ("generated_image") is drawn first when a picture model is ready
on this PC, because it was described for one; stock footage then only fills in where drawing failed. Scenes
written as footage ("broll"), and every scene when no picture model is set up, go down the list above in
order. `choice="stock"` (the command line's `--visuals stock`) puts stock footage first for every scene.

**The sites' rules this keeps to.** Only the documented API endpoints are called, with the key you were
given, at most a few searches a scene. Search answers are kept for 24 hours (Pixabay asks for that) so the
same search isn't sent twice. Files are downloaded once and used from the disk, never hot-linked. A site that
says "slow down" (HTTP 429) or refuses the key is left alone for the rest of the video. Credits are written
into the video's README; Pexels asks for a link back where you can give one, and neither site's licence lets
the footage be sold on its own, or suggests the people in it endorse anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .. import __version__
from ..creator import visual_generator as vg
from ..creator.visual_generator import SceneVisual
from ..settings import app_dir
from ..vision.broll_selector import VIDEO_EXTS
from .timeline_schema import Asset

Log = Callable[[str], None]
Progress = Callable[[str, "float | None"], None]
Http = Callable[[str, dict, float], bytes]

AGENT = f"ClipMonster/{__version__} (+https://github.com/iSlipperyX/ClipMostr)"
PEXELS_API = "https://api.pexels.com"
PIXABAY_API = "https://pixabay.com/api"
PROVIDERS = ("pexels", "pixabay")
PROVIDER_NAMES = {"pexels": "Pexels", "pixabay": "Pixabay"}
KEY_SETTINGS = {"pexels": ("pexels_api_key", "PEXELS_API_KEY"), "pixabay": ("pixabay_api_key", "PIXABAY_API_KEY")}
# A download may only start at the site's own hosts (Pexels has served its videos through Vimeo).
DOWNLOAD_HOSTS = {"pexels": ("pexels.com", "vimeo.com"), "pixabay": ("pixabay.com",)}
LICENCES = {"pexels": "Pexels License (pexels.com/license)",
            "pixabay": "Pixabay Content License (pixabay.com/service/license-summary)"}
SOURCE_NAMES = {**vg.BACKEND_NAMES, **PROVIDER_NAMES}

MAX_JSON = 4_000_000
MAX_VIDEO_BYTES = 80_000_000
MAX_IMAGE_BYTES = 25_000_000
MAX_CLIP_SECONDS = 90.0  # longer stock clips are large files for a few seconds' use
CACHE_SECONDS = 24 * 3600.0
PER_PAGE = 15
QUERIES_PER_SCENE = 2
REFUSALS_IN_A_ROW = 3  # this many searches refused one after another turn a stock site off for the video
DOWNLOADS_PER_SCENE = 3  # tries, when a file won't download or isn't what the site said
MIN_KEPT = 0.45  # of a source's picture left after cropping it to the canvas's shape
MAX_UPSCALE = 1.6
SHORT_OK = 0.9  # a clip this much of the scene's length will do (its last frame holds for the rest)
SECONDS_PER_WORD = 0.4

GENERATORS = ("comfyui", "sdxl_turbo", "flux_schnell")
CHOICES = (*vg.BACKENDS, "stock")
# Words a picture description is full of that a stock search can't use.
_FILLER = frozenset("""a an the of in on at with and or to from by for over under into onto its his her their this that
    these those very close up closeup shot view scene photo photograph picture image cinematic dramatic lighting
    light moody detailed highly vertical wide angle style still film background foreground showing shows depicting
    slow motion aerial dark bright soft hard is are being as""".split())


class StockError(RuntimeError):
    """A stock site said no, or sent something unusable. `status` is the HTTP code when there was one."""

    def __init__(self, message: str, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail  # the start of what the site said (never the address asked for)


@dataclass
class StockHit:
    """One file a stock search found."""
    provider: str
    kind: str  # video | image
    id: str
    file: str  # the file's address
    page: str  # its page on the site, for the credit
    author: str = ""
    author_url: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0
    size: int = 0  # bytes, when the site says
    rank: int = 0  # its place in the search's answer

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.kind}:{self.id}"

    @property
    def credit(self) -> str:
        what = "Video" if self.kind == "video" else "Photo"
        who = f" by {self.author}" if self.author else ""
        return f"{what}{who} on {PROVIDER_NAMES.get(self.provider, self.provider)}"


@dataclass
class SourcedVisual(SceneVisual):
    """A scene's picture or clip, with where it came from."""
    credit: str = ""
    url: str = ""
    licence: str = ""
    author: str = ""
    duration: float = 0.0  # a clip's own length
    width: int = 0
    height: int = 0
    query: str = ""  # the stock search that found it
    start: float = 0.0  # where in a clip the scene starts


# ---------------------------------------------------------------- the web, kept small and replaceable

def _http(url: str, headers: dict, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(MAX_JSON + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            said = exc.read(200).decode("utf-8", "replace")
        except (OSError, ValueError, AttributeError):
            said = ""
        exc.close()
        # Never the address in the message: Pixabay's carries the key.
        raise StockError(f"HTTP {code}", code, said) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise StockError(f"no connection ({type(getattr(exc, 'reason', exc)).__name__})") from None


def _download(url: str, dest: Path, cap: int, check: Callable[[], None] | None = None,
              timeout: float = 60.0) -> Path:
    """`url` saved as `dest`, never more than `cap` bytes of it."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT})
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if urllib.parse.urlsplit(r.geturl()).scheme != "https":
                raise StockError("the download left https")
            length = int(r.headers.get("Content-Length") or 0)
            if length > cap:
                raise StockError(f"the file is {length / 1e6:.0f} MB (the limit is {cap / 1e6:.0f} MB)")
            got = 0
            with open(part, "wb") as f:
                while chunk := r.read(1 << 18):
                    if check:
                        check()
                    got += len(chunk)
                    if got > cap:
                        raise StockError(f"the file is over {cap / 1e6:.0f} MB")
                    f.write(chunk)
        if not got:
            raise StockError("the file was empty")
        os.replace(part, dest)
        return dest
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise StockError(f"HTTP {code}", code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        if isinstance(exc, StockError):
            raise
        raise StockError(f"the download failed ({type(getattr(exc, 'reason', exc)).__name__})") from None
    finally:
        part.unlink(missing_ok=True)


def allowed_download(provider: str, url: str) -> bool:
    """A file address is only followed when it's https at one of the site's own hosts."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or not host:
        return False
    return any(host == h or host.endswith("." + h) for h in DOWNLOAD_HOSTS.get(provider, ()))


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- reading the sites' answers

def cover_scale(width: int, height: int, canvas: tuple[int, int]) -> float:
    """How much a width x height picture is enlarged to cover the canvas (under 1 = it's bigger than needed)."""
    if width <= 0 or height <= 0:
        return 99.0
    return max(canvas[0] / width, canvas[1] / height)


def kept(width: int, height: int, canvas: tuple[int, int]) -> float:
    """The share of a picture still seen once it's cropped to the canvas's shape (1 = the same shape)."""
    if width <= 0 or height <= 0:
        return 0.0
    a, c = width / height, canvas[0] / canvas[1]
    return min(a / c, c / a)


def best_rendition(files: list[tuple[int, int, str, int]], canvas: tuple[int, int]) -> tuple[int, int, str, int] | None:
    """Of a clip's (width, height, address, bytes) files: the smallest that covers the canvas without being
    enlarged, else the largest there is."""
    files = [f for f in files if f[0] > 0 and f[1] > 0 and f[2]]
    if not files:
        return None
    enough = [f for f in files if cover_scale(f[0], f[1], canvas) <= 1.0]
    if enough:
        return min(enough, key=lambda f: f[0] * f[1])
    return max(files, key=lambda f: f[0] * f[1])


def parse_pexels_videos(data: dict, canvas: tuple[int, int]) -> list[StockHit]:
    out = []
    for rank, v in enumerate((data or {}).get("videos") or []):
        if not isinstance(v, dict):
            continue
        files = [(_int(f.get("width")), _int(f.get("height")), str(f.get("link") or ""), _int(f.get("size")))
                 for f in v.get("video_files") or []
                 if isinstance(f, dict) and str(f.get("file_type") or "video/mp4").lower() == "video/mp4"]
        pick = best_rendition(files, canvas)
        if pick is None:
            continue
        user = v.get("user") if isinstance(v.get("user"), dict) else {}
        out.append(StockHit("pexels", "video", str(v.get("id") or ""), pick[2], str(v.get("url") or ""),
                            str(user.get("name") or ""), str(user.get("url") or ""), pick[0], pick[1],
                            _float(v.get("duration")), pick[3], rank))
    return [h for h in out if h.id]


def parse_pexels_photos(data: dict, canvas: tuple[int, int]) -> list[StockHit]:
    out = []
    for rank, p in enumerate((data or {}).get("photos") or []):
        if not isinstance(p, dict):
            continue
        src = p.get("src") if isinstance(p.get("src"), dict) else {}
        w, h = _int(p.get("width")), _int(p.get("height"))
        file = str(src.get("original") or "")
        if max(w, h) > 4500 or not file:  # a huge original: the site's own 2x "large" size instead
            big = str(src.get("large2x") or src.get("large") or "")
            if big and w > 0 and h > 0:
                f = min(1880 / w, 1300 / h, 1.0)
                file, w, h = big, max(1, round(w * f)), max(1, round(h * f))
        if not file:
            continue
        out.append(StockHit("pexels", "image", str(p.get("id") or ""), file, str(p.get("url") or ""),
                            str(p.get("photographer") or ""), str(p.get("photographer_url") or ""), w, h,
                            rank=rank))
    return [h for h in out if h.id]


def parse_pixabay_videos(data: dict, canvas: tuple[int, int]) -> list[StockHit]:
    out = []
    for rank, v in enumerate((data or {}).get("hits") or []):
        if not isinstance(v, dict):
            continue
        sizes = v.get("videos") if isinstance(v.get("videos"), dict) else {}
        files = [(_int(f.get("width")), _int(f.get("height")), str(f.get("url") or ""), _int(f.get("size")))
                 for f in sizes.values() if isinstance(f, dict)]
        pick = best_rendition(files, canvas)
        if pick is None:
            continue
        user = str(v.get("user") or "")
        out.append(StockHit("pixabay", "video", str(v.get("id") or ""), pick[2], str(v.get("pageURL") or ""), user,
                            f"https://pixabay.com/users/{urllib.parse.quote(user)}-{_int(v.get('user_id'))}/"
                            if user and v.get("user_id") else "",
                            pick[0], pick[1], _float(v.get("duration")), pick[3], rank))
    return [h for h in out if h.id]


def parse_pixabay_photos(data: dict, canvas: tuple[int, int]) -> list[StockHit]:
    out = []
    for rank, p in enumerate((data or {}).get("hits") or []):
        if not isinstance(p, dict):
            continue
        w, h = _int(p.get("imageWidth")), _int(p.get("imageHeight"))
        file = str(p.get("largeImageURL") or "")
        if not file or w <= 0 or h <= 0:
            continue
        f = min(1280 / max(w, h), 1.0)  # "largeImageURL" is at most 1280 on its long side
        user = str(p.get("user") or "")
        out.append(StockHit("pixabay", "image", str(p.get("id") or ""), file, str(p.get("pageURL") or ""), user,
                            f"https://pixabay.com/users/{urllib.parse.quote(user)}-{_int(p.get('user_id'))}/"
                            if user and p.get("user_id") else "",
                            max(1, round(w * f)), max(1, round(h * f)), rank=rank))
    return [h for h in out if h.id]


def orientation_of(canvas: tuple[int, int]) -> str:
    a = canvas[0] / canvas[1]
    return "portrait" if a < 0.9 else ("landscape" if a > 1.1 else "square")


def search_url(provider: str, kind: str, query: str, canvas: tuple[int, int], key: str = "") -> str:
    """The documented search address for `query` (Pixabay's carries the key; Pexels' goes in a header)."""
    o = orientation_of(canvas)
    if provider == "pexels":
        path = "/videos/search" if kind == "video" else "/v1/search"
        return f"{PEXELS_API}{path}?" + urllib.parse.urlencode({"query": query, "orientation": o, "per_page": PER_PAGE})
    args = {"key": key, "q": query, "safesearch": "true", "per_page": PER_PAGE}
    if kind == "video":
        return f"{PIXABAY_API}/videos/?" + urllib.parse.urlencode(args)
    args["image_type"] = "photo"
    if o != "square":
        args["orientation"] = "vertical" if o == "portrait" else "horizontal"
    return f"{PIXABAY_API}/?" + urllib.parse.urlencode(args)


PARSERS = {("pexels", "video"): parse_pexels_videos, ("pexels", "image"): parse_pexels_photos,
           ("pixabay", "video"): parse_pixabay_videos, ("pixabay", "image"): parse_pixabay_photos}


# ---------------------------------------------------------------- what to search for

def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{1,}", str(text or "").lower()) if w not in _FILLER]


def queries_for(scene) -> list[str]:
    """A scene's stock searches, the best first: what the script asked for, its keywords, then the first few
    plain words of its picture description. Short, because stock sites match few words best."""
    out = []
    asked = " ".join(str(getattr(scene, "stock_query", "") or "").split()[:5])
    if asked:
        out.append(asked)
    keys = [str(k).strip() for k in getattr(scene, "keywords", []) or [] if str(k).strip()]
    if keys:
        out.append(" ".join(" ".join(keys[:2]).split()[:4]))
    described = _terms(getattr(scene, "visual_prompt", ""))[:3]
    if described:
        out.append(" ".join(described))
    seen, unique = set(), []
    for q in out:
        q = re.sub(r"[^\w\s'-]", " ", q).strip().lower()
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def start_in(duration: float, need: float) -> float:
    """Where in a clip to start: a little way in when there's room (openings are often a fade or a wobble)."""
    spare = duration - need
    if spare <= 0.5:
        return 0.0
    return round(min(spare, max(0.0, duration * 0.08), 2.0), 2)


# ---------------------------------------------------------------- stock footage

class Stock:
    """Searches Pexels and Pixabay and downloads what it picks. One per video: it remembers what it has used."""

    def __init__(self, s, folder: Path, *, canvas: tuple[int, int] = (1080, 1920), log: Log | None = None,
                 check: Callable[[], None] | None = None, http: Http | None = None, download=None, probe=None,
                 cache: Path | None = None, now: Callable[[], float] = time.time):
        self.s, self.folder, self.canvas = s, Path(folder), (int(canvas[0]), int(canvas[1]))
        self.log = log or (lambda m: None)
        self.check = check
        self.http = http or _http
        self.download = download or _download
        self.probe = probe
        self.cache = cache if cache is not None else app_dir() / "stock-cache"
        self.now = now
        self.keys = {p: self.key_for(p) for p in PROVIDERS}
        self.off: dict[str, str] = {}  # provider: why it's left alone for the rest of this video
        self.used: set[str] = set()
        self.searches = 0  # sent to a site (not answered from the cache)
        self.downloads = 0
        self._memory: dict[str, list[StockHit]] = {}
        self._told = False
        self._refusals: dict[str, int] = {}  # provider: searches it has just refused (HTTP 400), in a row

    def key_for(self, provider: str) -> str:
        setting, env = KEY_SETTINGS[provider]
        return (str(getattr(self.s, setting, "") or "") or os.environ.get(env, "")).strip()

    def providers(self) -> list[str]:
        return [p for p in PROVIDERS if self.keys.get(p) and p not in self.off]

    def missing(self) -> str:
        """"" when stock footage can be searched, else why not."""
        if not getattr(self.s, "studio_stock", True):
            return "stock footage is turned off (Settings → Creator → Use stock footage sites)"
        if not any(self.keys.values()):
            return ("no stock footage key (Settings → Creator: a free Pexels or Pixabay API key, from "
                    "pexels.com/api or pixabay.com/api/docs)")
        if not self.providers():
            return "; ".join(f"{PROVIDER_NAMES[p]}: {why}" for p, why in self.off.items())
        return ""

    def kinds(self) -> list[str]:
        return ["video", "image"] if getattr(self.s, "studio_stock_video", True) else ["image", "video"]

    # -- searching

    def _cache_file(self, provider: str, kind: str, query: str) -> Path:
        name = hashlib.sha1(f"{provider}|{kind}|{orientation_of(self.canvas)}|{self.canvas}|{query}".encode()).hexdigest()
        return self.cache / f"{name}.json"

    def _cached(self, provider: str, kind: str, query: str) -> list[StockHit] | None:
        try:
            raw = json.loads(self._cache_file(provider, kind, query).read_text(encoding="utf-8"))
            if self.now() - float(raw["at"]) > CACHE_SECONDS or raw.get("query") != query:
                return None
            return [StockHit(**h) for h in raw["hits"]]
        except Exception:  # noqa: BLE001 - no cache, an old layout, a half-written file
            return None

    def _remember(self, provider: str, kind: str, query: str, hits: list[StockHit]) -> None:
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            path = self._cache_file(provider, kind, query)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"at": self.now(), "query": query, "hits": [asdict(h) for h in hits]}),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def forget(self, provider: str, kind: str, query: str) -> None:
        self._memory.pop(f"{provider}|{kind}|{query}", None)
        try:
            self._cache_file(provider, kind, query).unlink(missing_ok=True)
        except OSError:
            pass

    def search(self, provider: str, kind: str, query: str) -> list[StockHit]:
        """What `provider` has for `query` (from the last day's answers when it was asked before)."""
        tag = f"{provider}|{kind}|{query}"
        if tag in self._memory:
            return self._memory[tag]
        hits = self._cached(provider, kind, query)
        if hits is None:
            if not self._told:
                self._told = True
                names = " and ".join(PROVIDER_NAMES[p] for p in self.providers())
                self.log(f"Stock footage: searching {names} with each scene's search words (your API key).")
            headers = {"Authorization": self.keys[provider]} if provider == "pexels" else {}
            self.searches += 1
            try:
                body = self.http(search_url(provider, kind, query, self.canvas, self.keys[provider]), headers, 20.0)
                data = json.loads(body)
            except StockError as exc:
                self._failed(provider, exc)
                return []
            except ValueError:
                self._failed(provider, StockError("an answer that isn't JSON"))
                return []
            self._refusals.pop(provider, None)
            hits = PARSERS[(provider, kind)](data if isinstance(data, dict) else {}, self.canvas)
            hits = [h for h in hits if allowed_download(provider, h.file)]
            self._remember(provider, kind, query, hits)
        self._memory[tag] = hits
        return hits

    def _failed(self, provider: str, exc: StockError) -> None:
        name = PROVIDER_NAMES[provider]
        if exc.status in (401, 403):
            why = "it refused the API key (check it in Settings → Creator)"
        elif exc.status == 429:
            why = "it asked for fewer requests (its hourly limit); it's left alone for the rest of this video"
        elif exc.status == 400:
            # Pixabay answers a wrong key with 400 ("Invalid or missing API key"), not 401. And whatever a 400
            # is about, three in a row means every search will get one: stop sending them (and the key).
            self._refusals[provider] = self._refusals.get(provider, 0) + 1
            if "key" in getattr(exc, "detail", "").lower():
                why = "it refused the API key (check it in Settings → Creator)"
            elif self._refusals[provider] >= REFUSALS_IN_A_ROW:
                why = (f"it didn't accept {REFUSALS_IN_A_ROW} searches in a row (if its API key was only just "
                       "added, check it in Settings → Creator)")
            else:
                self.log(f"Stock footage: {name} didn't accept one search; moving on.")
                return
        else:
            why = f"it couldn't be reached ({exc})"
        self.off[provider] = why
        self.log(f"Stock footage: {name} isn't used from here on: {why}.")

    # -- choosing

    def score(self, hit: StockHit, need: float) -> float | None:
        """How well a hit suits a scene `need` seconds long on this canvas (None = not at all)."""
        if hit.key in self.used:
            return None
        share = kept(hit.width, hit.height, self.canvas)
        up = cover_scale(hit.width, hit.height, self.canvas)
        if share < MIN_KEPT or up > MAX_UPSCALE:
            return None
        cap = MAX_VIDEO_BYTES if hit.kind == "video" else MAX_IMAGE_BYTES
        if hit.size and hit.size > cap:
            return None
        score = 2.0 * share - 1.5 * max(0.0, up - 1.0) - 0.12 * hit.rank
        if hit.kind == "video":
            if hit.duration <= 0 or hit.duration > MAX_CLIP_SECONDS or hit.duration < need * SHORT_OK:
                return None
            if hit.duration < need:
                score -= 0.5
        return score

    def ranked(self, hits: list[StockHit], need: float) -> list[StockHit]:
        scored = [(self.score(h, need), h) for h in hits]
        return [h for sc, h in sorted(((sc, h) for sc, h in scored if sc is not None), key=lambda x: -x[0])]

    # -- fetching

    def fetch(self, hit: StockHit, dest: Path) -> Path:
        if not allowed_download(hit.provider, hit.file):
            raise StockError("the file isn't at the site's own address")
        cap = MAX_VIDEO_BYTES if hit.kind == "video" else MAX_IMAGE_BYTES
        self.downloads += 1
        return Path(self.download(hit.file, dest, cap, self.check))

    def measure(self, path: Path, hit: StockHit) -> tuple[int, int, float]:
        """(width, height, seconds) of a downloaded file as it really is."""
        if hit.kind == "image":
            w, h = image_size(path)
            if w <= 0:
                raise StockError("the picture couldn't be opened")
            return w, h, 0.0
        probe = self.probe
        if probe is None:
            from ..ffmpeg import probe
        info = probe(path)
        if info.width <= 0 or info.height <= 0 or info.duration <= 0:
            raise StockError("the clip couldn't be read")
        return info.width, info.height, float(info.duration)

    def find(self, scene, need: float, kinds: list[str] | None = None) -> SourcedVisual | None:
        """Stock footage for one scene, downloaded into the folder, or None when nothing suits."""
        tries = 0
        for kind in kinds or self.kinds():
            for query in queries_for(scene)[:QUERIES_PER_SCENE]:
                for provider in self.providers():
                    if self.check:
                        self.check()
                    for hit in self.ranked(self.search(provider, kind, query), need):
                        if tries >= DOWNLOADS_PER_SCENE:
                            return None
                        tries += 1
                        ext = ".mp4" if kind == "video" else (Path(urllib.parse.urlsplit(hit.file).path).suffix.lower()
                                                              or ".jpg")
                        if kind == "image" and ext not in vg.IMAGE_EXTS:
                            ext = ".jpg"
                        dest = self.folder / f"scene_{int(scene.scene_id):02d}_{provider}_{_safe(hit.id)}{ext}"
                        try:
                            path = self.fetch(hit, dest)
                            w, h, d = self.measure(path, hit)
                            real = replace(hit, width=w, height=h, duration=d or hit.duration)
                            self.used.add(hit.key)  # whether or not it suits: don't download it again
                            if kept(w, h, self.canvas) < MIN_KEPT or cover_scale(w, h, self.canvas) > MAX_UPSCALE \
                                    or (kind == "video" and d < need * SHORT_OK):
                                raise StockError("it isn't the size or length the site said")
                        except StockError as exc:
                            dest.unlink(missing_ok=True)
                            self.used.add(hit.key)
                            if exc.status in (403, 404, 410):  # an old answer whose file has moved
                                self.forget(provider, kind, query)
                            self.log(f"Stock footage: a {PROVIDER_NAMES[provider]} file for scene {scene.scene_id} "
                                     f"was skipped ({exc}).")
                            continue
                        return SourcedVisual(
                            scene.scene_id, kind, path, provider, getattr(scene, "visual_prompt", ""),
                            f"searched \"{query}\"", credit=real.credit, url=real.page, licence=LICENCES[provider],
                            author=real.author, duration=real.duration if kind == "video" else 0.0, width=w,
                            height=h, query=query,
                            start=start_in(real.duration, need) if kind == "video" else 0.0)
        return None


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(text))[:24] or "x"


def image_size(path: Path) -> tuple[int, int]:
    """(width, height) of a picture file, (0, 0) when it can't be opened."""
    try:
        from PIL import Image  # reads only the header, when Pillow is there

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        pass
    try:
        import cv2
        import numpy as np

        # Not cv2.imread: it can't open a path with non-English letters on Windows.
        im = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        return (int(im.shape[1]), int(im.shape[0])) if im is not None else (0, 0)
    except Exception:  # noqa: BLE001
        return 0, 0


# ---------------------------------------------------------------- every scene's picture

def default_seconds(board) -> dict[int, float]:
    return {sc.scene_id: max(1.5, len(sc.words) * SECONDS_PER_WORD) for sc in board.scenes}


def _local(scene, lib, used, need: float, probe) -> SourcedVisual | None:
    hit = vg.match_broll(list(scene.keywords) + _terms(scene.stock_query) + scene.speech_text.split(), lib, used)
    if hit is None:
        return None
    path, word = hit
    if path.suffix.lower() in VIDEO_EXTS:
        w = h = 0
        d = 0.0
        try:
            info = probe(path)
            w, h, d = int(info.width), int(info.height), float(info.duration)
        except Exception:  # noqa: BLE001 - an unreadable file is found out by the render, with its name
            pass
        return SourcedVisual(scene.scene_id, "video", path, "local_broll", scene.visual_prompt, f"matched \"{word}\"",
                             duration=d, width=w, height=h, start=start_in(d, need) if d else 0.0)
    w, h = image_size(path)
    return SourcedVisual(scene.scene_id, "image", path, "local_broll", scene.visual_prompt, f"matched \"{word}\"",
                         width=w, height=h)


def _wrap(v: SceneVisual) -> SourcedVisual:
    if isinstance(v, SourcedVisual):
        return v
    w, h = image_size(v.path) if v.kind == "image" else (0, 0)
    return SourcedVisual(v.scene_id, v.kind, Path(v.path), v.source, v.prompt, v.note, width=w, height=h)


def source_all(board, s, folder: Path, *, canvas: tuple[int, int] = (1080, 1920),
               seconds: dict[int, float] | None = None, choice: str = "", style=None, log: Log | None = None,
               progress: Progress | None = None, check: Callable[[], None] | None = None,
               stock: Stock | None = None, comfy=None, probe=None, ready: dict[str, str] | None = None
               ) -> list[SourcedVisual]:
    """A picture or a clip for every scene of `board`, in the scenes' order, kept in `folder`.

    `seconds`: how long each scene (by its id) is on screen, so clips are chosen long enough. `choice`: one of
    CHOICES (default Settings → Creator → Pictures); "stock" puts stock footage first for every scene, and a
    picture model or "texture" named outright is used as asked, without stock footage."""
    log = log or (lambda m: None)
    progress = progress or (lambda m, f: None)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    canvas = (int(canvas[0]), int(canvas[1]))
    seconds = {**default_seconds(board), **(seconds or {})}
    choice = choice or getattr(s, "creator_visuals", "auto")
    if choice not in CHOICES:
        choice = "auto"
    if probe is None:
        from ..ffmpeg import probe

    stock_first = choice == "stock"
    with_stock = choice in ("auto", "stock")
    with_local = choice in ("auto", "stock", "local_broll")
    if with_stock:
        stock = stock or Stock(s, folder, canvas=canvas, log=log, check=check, probe=probe)
        why = stock.missing()
        if why:
            if stock_first:
                log(f"Stock footage was asked for but can't be used: {why}. Using the other picture sources.")
            stock = None
    else:
        stock = None
    if ready is None:
        ready = vg.available(s) if choice != "texture" else {"texture": ""}
    if comfy is not None:
        ready = {**ready, "comfyui": ""}
    can_draw = choice != "texture" and any(not ready.get(k, "x") for k in GENERATORS)
    lib = vg.broll_library(getattr(s, "broll_folder", "")) if with_local else []
    used: dict[str, int] = {}
    found: dict[int, SourcedVisual] = {}
    tried_stock: set[int] = set()
    n = len(board.scenes)

    # 1 and 2: your B-roll, then stock footage, for the scenes that are footage (or all, when nothing can draw).
    for i, sc in enumerate(board.scenes):
        if check:
            check()
        if sc.visual_type == "texture" or choice in (*GENERATORS, "texture"):
            continue
        footage = sc.visual_type == "broll" or stock_first or not can_draw or choice == "local_broll"
        if not footage:
            continue
        need = float(seconds.get(sc.scene_id, 3.0))
        progress(f"Making pictures: finding footage for scene {i + 1} of {n}", 0.5 * i / max(1, n))
        hit = _local(sc, lib, used, need, probe) if lib else None
        if hit is None and stock is not None and stock.providers():
            tried_stock.add(sc.scene_id)
            hit = stock.find(sc, need)
        if hit is not None:
            found[sc.scene_id] = hit

    # 3 and 4: what's left is drawn (or textured) by the Creator's picture makers, in the canvas's shape.
    rest = [sc for sc in board.scenes if sc.scene_id not in found]
    if rest:
        sub = replace(board, scenes=rest)
        made = vg.make_all(sub, s, folder, choice="auto" if choice == "stock" else choice, style=style, log=log,
                           progress=lambda m, f: progress(m, None if f is None else 0.5 + 0.45 * f), check=check,
                           comfy=comfy, landscape=canvas[0] > canvas[1])
        for v in made:
            found[v.scene_id] = _wrap(v)

    # A scene that ended on a texture though it wasn't written as one: stock footage is better, if not yet tried.
    if stock is not None:
        by_id = {sc.scene_id: sc for sc in board.scenes}
        for sid, v in list(found.items()):
            sc = by_id[sid]
            if v.source != "texture" or sc.visual_type == "texture" or sid in tried_stock or not stock.providers():
                continue
            if check:
                check()
            hit = stock.find(sc, float(seconds.get(sid, 3.0)))
            if hit is not None:
                try:
                    if Path(v.path).parent == folder:
                        Path(v.path).unlink(missing_ok=True)
                except OSError:
                    pass
                found[sid] = hit

    out = [found[sc.scene_id] for sc in board.scenes]
    log("Pictures used: " + describe(out) + ".")
    if stock is not None and (stock.searches or stock.downloads):
        log(f"Stock footage: {stock.searches} search(es) sent, {stock.downloads} file(s) downloaded.")
    progress("Making pictures", 1.0)
    return out


# ---------------------------------------------------------------- for the timeline and the credits

def describe(visuals: list[SceneVisual]) -> str:
    """"4 from Pexels, 2 from your B-roll, 3 by SDXL-Turbo, 1 built-in texture"."""
    counts: dict[str, int] = {}
    for v in visuals:
        counts[v.source] = counts.get(v.source, 0) + 1
    parts = []
    for key, c in sorted(counts.items(), key=lambda x: -x[1]):
        name = SOURCE_NAMES.get(key, key)
        if key in PROVIDER_NAMES or key == "local_broll":
            parts.append(f"{c} from {name}")
        elif key == "texture":
            parts.append(f"{c} built-in texture{'s' if c != 1 else ''}")
        else:
            parts.append(f"{c} by {name}")
    return ", ".join(parts) or "none"


def asset_of(v: SceneVisual) -> Asset:
    """The timeline's record of a scene's picture or clip."""
    v = _wrap(v)
    return Asset(kind=v.kind, path=str(v.path), duration=float(v.duration if v.kind == "video" else 0.0),
                 width=int(v.width), height=int(v.height), source=SOURCE_NAMES.get(v.source, v.source),
                 credit=v.credit, url=v.url, licence=v.licence, prompt=v.query or v.prompt)


def credits(visuals: list[SceneVisual]) -> list[str]:
    """One line for each stock file used, for the README and (if you like) the video's description."""
    out = []
    for v in visuals:
        if not getattr(v, "credit", ""):
            continue
        line = f"Scene {v.scene_id}: {v.credit}"
        if getattr(v, "url", ""):
            line += f" ({v.url})"
        if line not in out:
            out.append(line)
    return out


def credit_summary(visuals: list[SceneVisual]) -> str:
    """One short sentence naming the sites, for a description: "Stock footage from Pexels and Pixabay."."""
    names = [PROVIDER_NAMES[p] for p in PROVIDERS if any(v.source == p for v in visuals)]
    return f"Stock footage from {' and '.join(names)}." if names else ""
