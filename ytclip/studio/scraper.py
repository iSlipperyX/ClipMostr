"""Reading an article for the Studio: a public web page in, its own words and where they came from out.

    article = read("https://example.org/why-cats-purr")
    article.title, article.site, article.byline, article.date, article.text, article.url
    distil(article, 6000)      # the part of it handed to the script writer

**What it reads.** Only pages anyone can open in a browser without signing in:

* http and https addresses, with no user name or password in them, on the public internet. An address that
  leads to this computer or the local network (localhost, 192.168.x.x, a router) is refused, and so is a
  redirect that leads there;
* pages the site lets programs read: its robots.txt is asked first, as "ClipMonster" (the app says who it is in
  its User-Agent; it never pretends to be a browser);
* a page that answers 401, 402 or 403, or shows a paywall instead of the article, is reported as not public.
  Nothing here tries to get round a login, a paywall or a block.

**How it reads.** The article's text is found by trafilatura when it's installed, else readability-lxml, else
the reader in this file (the page's <article> or its densest run of paragraphs, without menus, forms, cookie
notices or link lists). The title, site, author and date come from the page's own metadata (JSON-LD, Open
Graph, <meta>, <time>), and the address kept is the page's canonical one.

**What it's for.** The script writer is given the article as *source material to retell in its own words*,
never to copy (scriptwriter.py checks the script against it), and the article's address, title and publisher
stay with the video: in storyboard.json, clips.json, README.md and timeline.json, and as a "Source:" line
you can put in the post.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import string
import threading
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Callable

from ..creator import web

Log = Callable[[str], None]

AGENT = "ClipMonster"  # the name robots.txt rules are matched against
TIMEOUT = 15.0
MAX_BYTES = web.MAX_PAGE_BYTES
MAX_REDIRECTS = 5
MIN_WORDS = 80  # fewer than this isn't an article (a paywall stub, a video page, an error page)
PAYWALL = re.compile(r"subscribe to (?:continue|read|keep reading)|subscribers? only|sign in to (?:continue|read)|"
                     r"log in to (?:continue|read)|already a subscriber|create (?:a free )?account to (?:continue|read)|"
                     r"this (?:article|content|story) is (?:for|available to) (?:paid |premium )?(?:subscribers|members)",
                     re.I)


class ScrapeError(RuntimeError):
    """A plain-English reason the page couldn't be read."""


@dataclass
class Page:
    """What a web address answered."""

    url: str
    status: int
    content_type: str = ""
    body: bytes = b""
    location: str = ""  # where a redirect points
    charset: str = ""


Fetch = Callable[[str, float], Page]
Resolve = Callable[[str], list[str]]


@dataclass
class Article:
    url: str  # the page's canonical address (after redirects)
    title: str = ""
    site: str = ""  # the publisher's name
    byline: str = ""
    date: str = ""  # ISO 8601 when the page says
    description: str = ""
    text: str = ""  # paragraphs separated by blank lines
    extractor: str = ""  # trafilatura | readability | built-in
    asked: str = ""  # the address that was asked for

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def paragraphs(self) -> list[str]:
        return [p.strip() for p in self.text.split("\n\n") if p.strip()]

    def source(self) -> dict:
        """As the storyboard's sources are kept (creator/web.Source.short(), plus the author)."""
        out = {"title": self.title or self.url, "url": self.url, "publisher": self.site, "date": self.date,
               "kind": "article", "author": self.byline}
        return {k: v for k, v in out.items() if v}

    def credit(self) -> str:
        """One line to credit it with: "Source: Why cats purr, Example News (example.org)"."""
        host = urllib.parse.urlsplit(self.url).hostname or ""
        host = host.removeprefix("www.")
        who = ", ".join(x for x in (self.title, self.site) if x)
        return f"Source: {who} ({host})" if who else f"Source: {host}"

    def to_json(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- which addresses may be read

def _public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve(host: str) -> list[str]:
    """A name's addresses in the order the system prefers them: they're dialled in this order, so a PC that
    reaches the world by IPv4 isn't left waiting on an IPv6 address that sorts first."""
    found = (info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM))
    return list(dict.fromkeys(found))


def check_url(url: str, resolve: Resolve | None = None, lookup: bool = True) -> str:
    """`url` tidied, if it's a public web address; raises ScrapeError saying why not. Without `lookup` the
    site's name isn't looked up (the address is only checked for what it says)."""
    url = (url or "").strip()
    if not url:
        raise ScrapeError("There's no address to read.")
    if "://" not in url and re.match(r"^[\w.-]+\.[A-Za-z]{2,}(?:[/:?#]|$)", url):
        url = "https://" + url
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ScrapeError(f"That isn't a web address ({exc}).") from exc
    if parts.scheme not in ("http", "https"):
        raise ScrapeError("Only http and https addresses can be read"
                          + (f" (this one is {parts.scheme}:)." if parts.scheme else "."))
    if parts.username or parts.password:
        raise ScrapeError("The address has a user name or password in it. Only pages anyone can open are read.")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise ScrapeError("That address has no site in it.")
    if not host.isascii():  # an international name (bücher.example) goes out in its ASCII form
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ScrapeError(f"That isn't a web address (its site's name can't be used: {exc}).") from exc
    local = "That address is on this computer or your own network, not the public web."
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise ScrapeError(local)
    addresses: list[str] | None
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        if "." not in host:
            raise ScrapeError(local) from None
        addresses = None  # a name: looked up below
        if lookup:
            try:
                addresses = (resolve or _resolve)(host)
            except OSError as exc:
                # Behind a proxy this computer may not look names up itself: the proxy does, when the page is fetched.
                if resolve is not None or parts.scheme not in urllib.request.getproxies():
                    raise ScrapeError(f"{host} couldn't be found ({exc.strerror or exc}). Check the address and "
                                      "that you're online.") from exc
    if addresses is not None and (not addresses or not all(_public(a) for a in addresses)):
        raise ScrapeError(local)
    if addresses is not None and resolve is None:
        with _approved_lock:  # fetch() connects to these, not to whatever the name answers next time
            _approved[host] = list(addresses)
    netloc = host if ":" not in host else f"[{host}]"
    if port and port != {"http": 80, "https": 443}[parts.scheme]:
        netloc += f":{port}"
    # Letters and spaces a browser accepts in an address go out percent-encoded ("%" is kept, so an address
    # that's already encoded isn't encoded twice).
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=~-._?")
    return urllib.parse.urlunsplit((parts.scheme, netloc, path, query, ""))


# The addresses check_url() found public for each site's name. A name can answer differently the next time it's
# asked (deliberately: "DNS rebinding", public for the check and then 127.0.0.1 for the request), so the
# connection is made to an address that was checked, never by looking the name up again.
_approved: dict[str, list[str]] = {}
_approved_lock = threading.Lock()


def _pinned_connect(address, *args, **kwargs):
    host, port = address[0], address[1]
    with _approved_lock:
        known = list(_approved.get(str(host).rstrip(".").lower(), ()))
    if not known:  # a proxy's own address, or a name this computer couldn't look up itself
        return socket.create_connection(address, *args, **kwargs)
    last: OSError | None = None
    for ip in known:
        try:
            return socket.create_connection((ip, port), *args, **kwargs)
        except OSError as exc:
            last = exc
    raise last or OSError(f"{host} couldn't be reached")


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_connect


class _PinnedHTTPS(http.client.HTTPSConnection):  # TLS still checks the certificate against the site's name
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _pinned_connect


class _PinnedHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTP, req)


class _PinnedTLSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        context = getattr(self, "_context", None)
        return self.do_open(_PinnedHTTPS, req, **({"context": context} if context is not None else {}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # every hop is checked before it's followed (get())
        return None


def fetch(url: str, timeout: float = TIMEOUT) -> Page:
    """One request, redirects not followed."""
    req = urllib.request.Request(url, headers={
        "User-Agent": web.USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5",
        "Accept-Language": "en-US,en;q=0.8"})
    opener = urllib.request.build_opener(_NoRedirect, _PinnedHandler, _PinnedTLSHandler)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Page(resp.geturl(), resp.status, resp.headers.get_content_type(), resp.read(MAX_BYTES + 1),
                        charset=resp.headers.get_content_charset() or "")
    except urllib.error.HTTPError as exc:
        try:
            return Page(url, exc.code, exc.headers.get_content_type() if exc.headers else "",
                        location=(exc.headers.get("Location") or "") if exc.headers else "")
        finally:
            exc.close()


def get(url: str, *, fetcher: Fetch | None = None, resolve: Resolve | None = None, timeout: float = TIMEOUT,
        check: Callable[[], None] | None = None, hop: Callable[[str], None] | None = None) -> Page:
    """The page at a public address, following up to five redirects, each one checked like the first. `hop`
    is called with each address a redirect leads to, before it's fetched (read() asks its robots.txt there)."""
    fetcher = fetcher or fetch
    url = check_url(url, resolve)
    for _ in range(MAX_REDIRECTS + 1):
        if check:
            check()
        try:
            page = fetcher(url, timeout)
        except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError, ValueError) as exc:
            why = getattr(exc, "reason", None) or exc
            raise ScrapeError(f"{_host(url)} didn't answer ({why}).") from exc
        if page.status in (301, 302, 303, 307, 308) and page.location:
            # A header arrives as Latin-1 text: its raw bytes are percent-encoded as they were sent (as urllib
            # itself does), before the address is joined and checked.
            where = urllib.parse.quote(page.location, encoding="iso-8859-1", safe=string.punctuation)
            url = check_url(urllib.parse.urljoin(url, where), resolve)
            if hop:
                hop(url)
            continue
        return page
    raise ScrapeError(f"{_host(url)} keeps redirecting.")


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or url).removeprefix("www.")


def allowed(url: str, *, fetcher: Fetch | None = None, resolve: Resolve | None = None) -> bool:
    """Whether the site's robots.txt lets ClipMonster read this page. No robots.txt (or one that can't be
    fetched for a 4xx reason) means yes; a site whose robots.txt errors (5xx) is left alone."""
    parts = urllib.parse.urlsplit(url)
    robots = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        page = get(robots, fetcher=fetcher, resolve=resolve, timeout=8.0)
    except ScrapeError:
        return True
    if page.status >= 500:
        return False
    if page.status != 200 or not page.body:
        return True
    rules = urllib.robotparser.RobotFileParser()
    rules.parse(page.body[:500_000].decode("utf-8-sig", "replace").splitlines())  # a BOM would hide line one
    return rules.can_fetch(AGENT, url)


# ---------------------------------------------------------------- the page's own facts

_META = re.compile(r"(?is)<meta\b([^>]*)>")
_ATTR = re.compile(r"""(?is)([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")


def _attrs(tag: str) -> dict[str, str]:
    return {m[1].lower(): html.unescape(m[2] or m[3] or m[4] or "").strip() for m in _ATTR.finditer(tag)}


def _json_ld(doc: str) -> list[dict]:
    out: list[dict] = []
    for block in re.findall(r"(?is)<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", doc):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack += item
            elif isinstance(item, dict):
                out.append(item)
                stack += [v for k, v in item.items() if k in ("@graph", "mainEntity", "mainEntityOfPage")
                          and isinstance(v, (dict, list))]
    return out


def _name(value) -> str:
    """A JSON-LD person or organisation's name ("Jane Doe", {"name": ...} or a list of them)."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _name(value.get("name"))
    if isinstance(value, list):
        return ", ".join(n for n in (_name(v) for v in value[:3]) if n)
    return ""


def metadata(doc: str, url: str) -> dict:
    """title, site, byline, date, description, canonical and whether the page says it's paid-for."""
    meta: dict[str, str] = {}
    for tag in _META.findall(doc[:400_000]):
        a = _attrs(tag)
        key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
        if key and a.get("content") and key not in meta:
            meta[key] = " ".join(a["content"].split())
    ld = [d for d in _json_ld(doc) if any(t in str(d.get("@type", "")) for t in ("Article", "Posting", "Report"))]
    first = ld[0] if ld else {}
    title_tag = re.search(r"(?is)<title[^>]*>(.*?)</title>", doc)
    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", doc)
    site = meta.get("og:site_name") or _name(first.get("publisher")) or meta.get("application-name", "")
    title = (_name(first.get("headline")) or meta.get("og:title") or meta.get("twitter:title")
             or (web.clean(h1.group(1)) if h1 else "") or (web.clean(title_tag.group(1)) if title_tag else ""))
    if site:  # "Why cats purr | Example News" -> "Why cats purr"
        title = re.sub(rf"\s*[|\-–—:·]\s*{re.escape(site)}\s*$", "", title, flags=re.I).strip() or title
    time_tag = re.search(r"(?is)<time[^>]+datetime\s*=\s*[\"']([^\"']+)[\"']", doc)
    date = (str(first.get("datePublished") or "") or meta.get("article:published_time") or meta.get("date")
            or meta.get("dc.date") or meta.get("datepublished") or (time_tag.group(1) if time_tag else ""))
    canonical = ""
    for tag in re.findall(r"(?is)<link\b([^>]*)>", doc[:400_000]):
        a = _attrs(tag)
        if "canonical" in a.get("rel", "").lower().split() and a.get("href"):
            canonical = urllib.parse.urljoin(url, a["href"])
            break
    free = first.get("isAccessibleForFree")
    return {"title": title[:300], "site": site[:120],
            "byline": (_name(first.get("author")) or meta.get("author") or meta.get("article:author", ""))[:160],
            "date": web._iso(date), "description": (meta.get("og:description") or meta.get("description", ""))[:500],
            "canonical": canonical or meta.get("og:url", ""),
            "paid": str(free).lower() == "false"}


# ---------------------------------------------------------------- the article's text

_SKIP = {"script", "style", "noscript", "svg", "template", "nav", "header", "footer", "aside", "form", "button",
         "figure", "figcaption", "iframe", "select", "dialog", "menu", "canvas", "video", "audio", "picture"}
_BLOCK = {"p", "h2", "h3", "h4", "li", "blockquote", "pre"}
_VOID = {"br", "img", "hr", "input", "meta", "link", "source", "track", "wbr", "area", "base", "col", "embed"}
_NOISE = re.compile(r"(?i)\b(?:cookie|newsletter|sign up|subscribe|all rights reserved|javascript|advertisement|"
                    r"share this|follow us|read more:|related:|click here|terms of (?:use|service)|privacy policy)\b")
_BOILER = re.compile(r"(?i)comment|sidebar|footer|related|promo|newsletter|share|social|cookie|banner|popup|modal|"
                     r"breadcrumb|advert|sponsor|subscribe|paywall|menu|\bnav")
_GATED = re.compile(r"(?i)paywall|premium[-_ ]?content|subscribers?[-_ ]?only|members?[-_ ]?only|piano-|regwall")
# Class names that only look like a mark. A blog labels each post with its tags and categories, so a free story
# about paywalls carries "tag-paywall" and a music site "category-piano-lessons": on a label only the words that
# say who may read count ("category-members-only"). And some names say the opposite ("no-paywall").
_LABEL = re.compile(r"(?i)^(?:tags?|category|categories|cat|term|topic)[-_]")
_LABEL_GATED = re.compile(r"(?i)premium[-_]?content|subscribers?[-_]?only|members?[-_]?only")
_SPACED = re.compile(r"(?i)\b(?:premium content|subscribers? only|members? only)\b")
_UNGATED = re.compile(r"(?i)^(?:no|non|not|without)[-_]?(?:paywall|regwall)|"
                      r"(?:paywall|regwall)[-_]?(?:off|free|false|disabled|none|exempt|inactive)$")


def _gated(tag: str, a: dict) -> bool:
    """Whether an element is marked as being for subscribers (its class, id or role says paywall,
    subscriber-only, members-only...). Never the page's own <body> or <html>."""
    if tag in ("body", "html"):
        return False
    classes = str(a.get("class") or "").split()
    if _SPACED.search(" ".join(classes)):  # "premium content", "members only": two class names that say it together
        return True
    for name in classes + [str(a.get(k)) for k in ("id", "role") if a.get(k)]:
        if _UNGATED.search(name):
            continue
        if (_LABEL_GATED if _LABEL.match(name) else _GATED).search(name):
            return True
    return False


class _Reader(HTMLParser):
    """Collects text blocks with the element they sit in: (container id, tag, text, share of it that's links)."""

    def __init__(self, strict: bool = True):
        super().__init__(convert_charrefs=True)
        self.strict = strict  # leave out elements whose class or id says "sidebar", "related", "share"...
        self.blocks: list[tuple[int, str, str, float]] = []
        self.stack: list[tuple[str, int, bool]] = []  # tag, container id, skipped
        self.containers: dict[int, str] = {0: "body"}
        self.next_id = 1
        self.cur: list[str] | None = None
        self.cur_tag = ""
        self.links = 0
        self.in_link = 0
        self.skip = 0

    def container(self) -> int:
        for tag, cid, _ in reversed(self.stack):
            if cid:
                return cid
        return 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if tag == "br" and self.cur is not None:
                self.cur.append(" ")
            return
        a = dict(attrs)
        marks = f"{a.get('class') or ''} {a.get('id') or ''} {a.get('role') or ''}"
        skipped = tag in _SKIP or (self.strict and bool(_BOILER.search(marks))
                                   and tag not in ("body", "html", "main", "article"))
        # The part of a page marked as being for subscribers is never read, however the page is laid out.
        skipped = skipped or _gated(tag, a)
        hidden = "hidden" in a or "display:none" in (a.get("style") or "").replace(" ", "").lower() \
            or a.get("aria-hidden") == "true"
        skipped = skipped or hidden
        cid = 0
        if tag in ("article", "main", "section", "div", "td") and not skipped:
            cid = self.next_id
            self.next_id += 1
            self.containers[cid] = tag
        self.stack.append((tag, cid, skipped))
        if skipped:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BLOCK:
            self.flush()
            self.cur, self.cur_tag, self.links = [], tag, 0
        elif tag == "a":
            self.in_link += 1

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        # Close back to the matching tag (pages leave <p> and <li> open).
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for closed, _, skipped in reversed(self.stack[i:]):
                    if skipped:
                        self.skip -= 1
                    if closed == "a" and not skipped and self.in_link:
                        self.in_link -= 1
                    if closed == self.cur_tag and self.cur is not None and not self.skip:
                        self.flush()
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.skip or self.cur is None:
            return
        self.cur.append(data)
        if self.in_link:
            self.links += len(data.strip())

    def flush(self):
        if self.cur is not None:
            text = " ".join("".join(self.cur).split())
            if text:
                self.blocks.append((self.container(), self.cur_tag, text, self.links / max(1, len(text))))
        self.cur, self.cur_tag = None, ""


def _prose(tag: str, text: str, link_share: float) -> bool:
    if link_share > 0.5 or _NOISE.search(text) and len(text) < 220:
        return False
    if tag in ("h2", "h3", "h4"):
        return 3 <= len(text) <= 140
    words = len(text.split())
    if tag == "li":
        return words >= 6 and len(text) >= 40
    return words >= 8 and len(text) >= 50


def builtin_text(doc: str) -> str:
    """The article's paragraphs, found without any library: the element holding the most prose (an <article>
    or <main> when there is one), read in order, headings kept, menus and notices left out."""
    text = _read_blocks(doc, strict=True)
    if len(text.split()) < MIN_WORDS:
        # A page whose whole layout sits in a "has-sidebar" wrapper: read it again without judging by class.
        loose = _read_blocks(doc, strict=False)
        if len(loose.split()) > len(text.split()):
            return loose
    return text


def _read_blocks(doc: str, strict: bool) -> str:
    reader = _Reader(strict)
    try:
        reader.feed(doc)
        reader.close()
        reader.flush()
    except Exception:  # noqa: BLE001 - html.parser is forgiving, but a page can still be too broken to read
        return ""
    weight: dict[int, int] = {}
    for cid, tag, text, share in reader.blocks:
        if tag in ("p", "blockquote") and _prose(tag, text, share):
            weight[cid] = weight.get(cid, 0) + len(text)
    if not weight:
        return ""
    best = max(weight, key=lambda c: weight[c])
    total = sum(weight.values())
    # A page split over many small containers (one <div> a paragraph): take every container's prose.
    keep = {best} if weight[best] >= 0.5 * total else {c for c, w in weight.items() if w >= 0.04 * total}
    out: list[str] = []
    for cid, tag, text, share in reader.blocks:
        if cid not in keep or not _prose(tag, text, share):
            continue
        if tag in ("h2", "h3", "h4"):
            if out and out[-1].startswith("## "):
                out.pop()  # a heading with nothing under it
            out.append("## " + text)
        elif text not in out:
            out.append(("- " if tag == "li" else "") + text)
    while out and out[-1].startswith("## "):
        out.pop()
    return "\n\n".join(out)


# How the cut of a marked element ends. The rule that matters: a cut may take more than the element, never
# less, because what's left is read. The measure is what a browser makes of the page, broken tags and all
# (tests/test_studio_scraper.py holds the cases an HTML5 parser was asked about), and wherever this simpler
# reading can't be sure of it, the cut goes on.
#
# A marked element ends at its own end tag, when a browser would heed that tag there:
#   - what opens inside it is followed as a browser follows it: a </div> ends the <div> opened last, a </li>
#     doesn't reach out of a list inside the item, a </span> is ignored while a block is open in the span;
#   - while a table, a text box, a drawing or the like is open inside it, tags aren't counted at all;
#   - a marked element still open inside it when it ends keeps the cut going until that one has ended too;
#   - one that nothing ends takes the rest of the page.
#
# The exception is the handful of elements whose end tag HTML lets a page leave out, where "the rest of the
# page" would be a free article lost to a menu item. Those also end where a browser is sure to have ended
# them, unless a table, a text box or a marked element is open inside them:
#   - at the tag that takes their place: a <p> at the next block (<p>, <div>, <h2>...), an <li> at the next
#     <li>, a cell at the next cell or row;
#   - an <li>, a cell or a row at the end of the list or table it's in, when that list or table has held
#     nothing unusual since it opened (so there's no doubt it is still open).
_INLINE = {"a", "span", "b", "i", "em", "strong", "small", "u", "s", "font", "label", "code", "sup", "sub", "mark",
           "time", "abbr", "cite", "q", "strike", "big", "tt", "nobr", "kbd", "var", "samp", "dfn", "bdi", "bdo"}
_TEXT = _INLINE | _VOID  # what never stands between an element and the tag that ends it
# The start tags that always end an open <p>. Not <form> (ignored inside another form) or <table> (not on
# every page), nor the newest elements, which an older browser reads as plain ones.
_ENDS_P = {"address", "article", "aside", "blockquote", "center", "details", "dir", "div", "dl", "fieldset",
           "figcaption", "figure", "footer", "header", "main", "menu", "nav", "ol", "p", "section", "summary",
           "ul", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "li", "dd", "dt", "hr"}
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# The elements a browser treats in a way of its own: one left open is opened again in the block that follows,
# and its end tag reaches it through the blocks opened inside it.
_FORMATTING = {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
# The blocks whose end tag ends them whatever is open inside them...
_BLOCK_ENDS = {"address", "article", "aside", "blockquote", "button", "center", "details", "dir", "div", "dl",
               "fieldset", "figure", "footer", "header", "listing", "menu", "nav", "ol", "pre", "section", "ul"}
# ...the ones only today's browsers count among those (they get the careful reading of both)...
_UNSURE = {"main", "figcaption", "summary", "hgroup", "search", "dialog"}
_TABLE_PARTS = {"td", "th", "tr", "tbody", "thead", "tfoot", "caption"}
# ...and everything that, open inside a <span>, a <label> or a made-up tag, makes a browser ignore that
# element's end tag.
_BLOCKING = _BLOCK_ENDS | _UNSURE | _TABLE_PARTS | set(_HEADINGS) | {"p", "li", "dd", "dt", "form", "table"}
# What an end tag doesn't reach through, looking for the element it ends.
_STOPS = {"li": {"ul", "ol", "table"}, "p": {"button", "table"}, "table": set()}
# Start tags that end a <select> they are in (a table's only in some places).
_ENDS_SELECT = {"select", "input", "textarea", "keygen", "table", "caption", "tbody", "tfoot", "thead", "tr", "td", "th"}
_DEEP = 7  # a browser carries a <b> (<i>, <a>...) on into the blocks open inside it: eight deep, no further
_MOST = 2000  # elements open inside one marked element; past that, the cut takes the rest of the page
# Elements that change how what's inside them is read: an end tag there doesn't reach an element outside.
_SHUT = {"table", "object", "applet", "marquee", "template", "svg", "math", "select", "textarea", "title",
         "noscript", "iframe", "noembed", "noframes", "xmp", "plaintext", "listing", "script", "style", "frameset"}
_LISTS, _GROUPS, _BLOCKS = {"ul", "ol", "menu"}, {"tbody", "thead", "tfoot"}, {"p", "div", "address"}
_ROWS = {"tr"} | _GROUPS
# The elements whose end tag a page may leave out, and the start tags that end one.
_LEFT_OPEN = {
    "p": _ENDS_P, "li": {"li"}, "dt": {"dt", "dd"}, "dd": {"dt", "dd"},
    "td": {"td", "th"} | _ROWS, "th": {"td", "th"} | _ROWS, "tr": _ROWS,
    "tbody": _GROUPS, "thead": _GROUPS, "tfoot": _GROUPS, "option": {"option", "optgroup"},
}
# What holds such elements: (the tags that may come and go inside it without a doubt that it's still open,
# what it holds and the end tags that then end one).
_CELL_TAGS = {"td", "th", "col", "colgroup"} | _ROWS
_HOLDERS = {
    "ul": (_TEXT | {"li", "p"}, {"li": {"ul"}}), "ol": (_TEXT | {"li", "p"}, {"li": {"ol"}}),
    "menu": (_TEXT | {"li", "p"}, {"li": {"menu"}}),
    "dl": (_TEXT | {"dt", "dd", "p"}, {"dt": {"dl"}, "dd": {"dl"}}),
    "table": (_TEXT | _CELL_TAGS | {"p"}, {"td": {"tr", "table"}, "th": {"tr", "table"}, "tr": {"table"},
                                          "tbody": {"table"}, "thead": {"table"}, "tfoot": {"table"}}),
    "select": ({"option", "optgroup"}, {"option": {"select"}}),
    "datalist": ({"option"} | _VOID, {"option": {"datalist"}}),
}
# The items each holds, whose end tag HTML lets a page leave out.
_ITEMS = {"ul": {"li"}, "ol": {"li"}, "menu": {"li"}, "dl": {"dt", "dd"}, "table": _TABLE_PARTS,
          "select": {"option", "optgroup"}, "datalist": {"option"}}
_MURKY = (_SHUT | {"button"}) - {"table", "script", "style"}  # inside these, a list isn't sure to be a list


class _Gates(HTMLParser):
    """Where a page's subscriber-marked elements start and end: (start, end, whole) with each a (line,
    column) and `whole` saying whether the tag at `end` is the element's own end tag (and goes with it) or
    the tag that ended it (and stays). `end` is None for one that nothing ever closes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.spans: list[tuple[tuple[int, int], tuple[int, int] | None, bool]] = []
        # The open lists and tables outside any marked element: [tag, still sure, its items open now].
        self.lists: list[list] = []
        self.murk: dict[str, int] = {}  # the text boxes, drawings... open there
        self.styled: dict[str, int] = {}  # the <b>, <i>, <a>... open anywhere
        self.gate = ""  # the tag of the marked element being passed over
        self.began = (0, 0)
        # What's open inside it, in order: (tag, is it marked too, has what it was opened in ended).
        self.open: list[tuple[str, bool, bool]] = []
        # The <b>, <i>, <a>... open inside it, by tag: (is it marked, how much was open when it opened).
        self.marks: dict[str, list[tuple[bool, int]]] = {}
        self.shut: dict[str, int] = {}  # the tables, text boxes... open inside it
        self.formed = False  # it's a form, and its </form> has come while something was open inside it
        self.ends: set[str] = set()  # the end tags of what holds it, where it's sure to be held
        self.owed = False  # it has ended, but a marked element opened inside it hasn't
        self.endless = False  # nothing ends it

    def _end(self, whole: bool) -> None:
        self.spans.append((self.began, self.getpos(), whole))
        self.gate = ""

    def _doubt(self) -> None:
        for entry in self.lists:
            entry[1] = False

    def _shuts(self, tag: str) -> bool:
        """Whether a tag inside the marked element is one that opens (or closes) a table, a text box...
        (A table in a marked cell or row is followed like any other element.)"""
        return tag in _SHUT and not (tag == "table" and self.gate in _TABLE_PARTS)

    def _marked(self) -> bool:
        """Whether a marked element is open inside the one being passed over."""
        return any(marked for _, marked, _ in self.open) or any(
            marked for opened in self.marks.values() for marked, _ in opened)

    def _bare(self) -> bool:
        """Whether nothing is open inside the marked element now."""
        return not (self.open or self.owed or self.endless or any(self.marks.values()) or any(self.shut.values()))

    def _settled(self) -> bool:
        """Whether a tag that ends the marked element from outside ends everything open inside it too: a
        table, a text box or a marked element in there is another matter."""
        return not (self.owed or self.endless or any(self.shut.values()) or self._marked()
                    or any(tag == "table" for tag, _, _ in self.open))

    def _replaced(self, tag: str) -> bool:
        """Whether a start tag takes the marked element's place, as a browser reads it: a <p> at the next
        block, an <li> at the next <li>, a cell at the next cell or row."""
        if tag not in _LEFT_OPEN.get(self.gate, ()) or not self._settled():
            return False
        if self.gate == "option":
            return self._bare()
        if self.gate == "p":
            return not any(inside == "button" for inside, _, _ in self.open)
        if self.gate in ("li", "dd", "dt"):  # the nearest item, with no block but a <div> or a <p> in between
            return not any(inside in _BLOCKING and inside not in _BLOCKS for inside, _, _ in self.open)
        return True

    def _blocks(self, since: int = 0) -> int:
        return sum(1 for tag, _, _ in self.open[since:] if tag in _BLOCKING)

    def _find(self, tag: str, stops, ordinary: bool = False) -> int:
        """Which open element inside the marked one an end tag ends: its place, -1 when it's none of them,
        -2 when a browser ignores the tag there."""
        for i in range(len(self.open) - 1, -1, -1):
            inside = self.open[i][0]
            if inside == tag:
                return i
            if inside in stops or ordinary and inside in _BLOCKING:
                return -2
        return -1

    def _drop(self, at: int) -> None:
        """An element open inside the marked one has ended, and with it what was open inside it. Except a
        marked element in there, which only its own end tag ends: if a tag that wasn't followed had already
        ended the element, that one opened after it and is still open. (Such a one's end tag then ends
        nothing but it: what opened after it may not be inside it.)"""
        if self.open[at][2]:
            del self.open[at]
        else:
            self.open[at:] = [(tag, True, True) for tag, marked, _ in self.open[at + 1:] if marked]

    def _opened(self, tag: str, marked: bool, styled: bool) -> None:
        """Follow a start tag inside the marked element."""
        if (self.gate == "select" or self.shut.get("select")) and (marked or tag in _ENDS_SELECT):
            self.endless = True  # a list of choices that a browser may have ended here, or not
            return
        if self._shuts(tag):
            self.shut[tag] = self.shut.get(tag, 0) + 1
            return
        if any(self.shut.values()) or tag in _VOID:
            return
        if tag in _FORMATTING:
            self.marks.setdefault(tag, []).append((marked, len(self.open)))
            return
        if (tag in _TABLE_PARTS or tag == "colgroup") and self.gate not in _TABLE_PARTS and not any(
                inside == "table" for inside, _, _ in self.open):
            return  # outside a table, a browser reads a cell's or a row's tag as nothing at all
        if tag in _ENDS_P and (at := self._find("p", _STOPS["p"])) >= 0:
            self._drop(at)  # a block ends the paragraph before it, and what was open in that
        if tag in ("li", "dd", "dt"):  # and the item before it, unless a block stands between them
            same = ("li",) if tag == "li" else ("dd", "dt")
            for i in range(len(self.open) - 1, -1, -1):
                inside = self.open[i][0]
                if inside in same:
                    self._drop(i)
                    break
                if inside in _BLOCKING and inside not in _BLOCKS:
                    break
        elif tag in _HEADINGS and self.open and self.open[-1][0] in _HEADINGS and not styled:
            self.open.pop()  # a heading right inside a heading ends it (not one in a <b> in a heading)
        elif tag == "button" and (at := self._find("button", {"table"})) >= 0:
            self._drop(at)  # nor a button a button
        if tag == "form" and marked and styled:
            self.endless = True  # a <b> (<i>, <a>...) open before a form outlasts the </form>, inside the form
        self.open.append((tag, marked, False))
        if len(self.open) > _MOST:
            self.endless = True

    def _closed(self, tag: str) -> str:
        """Follow an end tag inside the marked element: "own" when it's that element's own and a browser
        heeds it there, "in" when it belongs to something inside it (or is ignored there), "out" when it
        is neither."""
        if self.shut.get(tag):
            self.shut[tag] -= 1
            return "in"
        if any(self.shut.values()):
            return "in"
        if tag in _FORMATTING:
            if opened := self.marks.get(tag):
                marked, since = opened[-1]
                if not (marked and self._blocks(since) >= _DEEP):  # (carried too deep: it stays open)
                    opened.pop()
                return "in"
            if tag != self.gate:
                return "out"
            return "own" if self._blocks() < _DEEP else "in"
        if tag in _HEADINGS:  # any heading's end tag ends the heading opened last, whatever its number
            for i in range(len(self.open) - 1, -1, -1):
                if self.open[i][0] in _HEADINGS:
                    self._drop(i)
                    return "in"
                if self.open[i][0] == "table":
                    return "in"
            return "own" if self.gate in _HEADINGS else "out"
        at = self._find(tag, _STOPS.get(tag, {"table"}), ordinary=tag not in _BLOCKING or tag in _UNSURE)
        if at >= 0:
            if tag != "form":
                self._drop(at)
            elif at == len(self.open) - 1 and not any(self.marks.values()):
                self.open.pop()
            elif not self.open[at][1]:
                del self.open[at]  # a </form> leaves open what is open inside the form, still inside it
            return "in"
        if at == -2:
            return "in"
        if tag != self.gate:
            return "out"
        if tag == "form":  # the same goes for the marked form itself: it ends when those have ended
            self.formed = True
            return "in"
        return "own"

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)  # "<div/>" opens a <div>: a browser doesn't heed the slash

    def handle_starttag(self, tag, attrs):
        marked = _gated(tag, dict(attrs))
        styled = any(self.styled.values())
        if tag in _FORMATTING:
            self.styled[tag] = self.styled.get(tag, 0) + 1
        if self.gate:
            if self._replaced(tag):
                self._end(False)  # the tag that takes its place; it's read like any other tag, below
            else:
                if not self.endless:
                    self._opened(tag, marked, styled)
                return
        # Outside a marked element: is the list (the table) this sits in still surely open?
        sure = not any(self.murk.values())
        top = self.lists[-1] if self.lists else None
        if tag in _HOLDERS:
            if not (top and tag in _LISTS and top[0] in _LISTS):  # a list in a list item is nothing unusual
                self._doubt()
            if not marked:
                self.lists.append([tag, sure, set()])
        elif not (top and tag in _HOLDERS[top[0]][0]):
            self._doubt()
        elif tag in _ITEMS[top[0]]:  # the item (the cell, the row) that is open in it now
            keep = _ROWS if tag in ("td", "th") else _GROUPS if tag == "tr" else ()
            top[2] = {inside for inside in top[2] if inside in keep} | {tag}
        if tag in _MURKY and tag not in _VOID and not marked:
            self.murk[tag] = self.murk.get(tag, 0) + 1
        if marked and tag not in _VOID:
            held = _HOLDERS[top[0]][1].get(tag, set()) if top and top[1] and sure else set()
            self.gate, self.began, self.ends = tag, self.getpos(), set(held)
            self.open, self.marks, self.shut, self.owed, self.formed = [], {}, {}, False, False
            # In a <select> a browser may not read a tag as a tag at all. And a <b> (<i>, <a>...) left open
            # before a form is opened again inside it, and stays open past the </form>.
            self.endless = bool(self.murk.get("select")) or tag == "form" and styled

    def handle_endtag(self, tag):
        if self.styled.get(tag):
            self.styled[tag] -= 1
        if self.gate:
            if self.endless:
                return
            what = self._closed(tag)
            if self.owed or what == "own" or self.formed and self._bare():
                # A marked <b> (<i>, <a>...) left open inside it is carried on into what follows by a
                # browser, until its own end tag. And any marked element inside it may have outlived it,
                # if a tag before this one had already ended it.
                self.owed = self._marked()
                if not self.owed:
                    self._end(True)
                    self._doubt()  # what closed inside it may have closed more than it
                return
            if not (what == "out" and tag in self.ends and self._settled()
                    and (self.gate != "option" or self._bare())):
                return
            self._end(False)  # the list (the row, the table) it's in ends here: so has it
        if self.murk.get(tag):
            self.murk[tag] -= 1
        top = self.lists[-1] if self.lists else None
        if top and tag == top[0]:
            self.lists.pop()
        elif tag in _HOLDERS or not (top and tag in _HOLDERS[top[0]][0]):
            self._doubt()
        elif tag in _ITEMS[top[0]]:
            if tag not in top[2]:
                self._doubt()  # an item's end tag with no item open: it may end one outside, and the list
            top[2] -= {"td", "th"} if tag in ("td", "th") else {"td", "th", "tr"} if tag == "tr" else set(top[2])

    def close(self):
        super().close()
        if self.gate:  # never closed: nothing after it is read
            self.spans.append((self.began, None, True))
            self.gate = ""


def ungated(doc: str) -> str:
    """The page without the elements marked as being for subscribers (a "paywall" or "subscriber-only" class,
    id or role), so no reader, ours or a library's, sees what's in them."""
    if not _GATED.search(doc):
        return doc
    gates = _Gates()
    try:
        gates.feed(doc)
        gates.close()
    except Exception:  # noqa: BLE001 - markup the parser chokes on: the readers' own passes still skip them
        return doc
    if not gates.spans:
        return doc
    starts, at = [0], doc.find("\n")  # the parser counts lines by "\n" alone
    while at != -1:
        starts.append(at + 1)
        at = doc.find("\n", at + 1)
    offset = lambda pos: starts[pos[0] - 1] + pos[1]  # noqa: E731
    out, at = [], 0
    for began, ended, whole in gates.spans:
        a = offset(began)
        if a < at:
            continue
        out.append(doc[at:a])
        if ended is None:
            at = len(doc)
            break
        at = offset(ended)
        if whole:
            close = doc.find(">", at)
            at = len(doc) if close == -1 else close + 1
    out.append(doc[at:])
    return "".join(out)


def _with_trafilatura(doc: str, url: str) -> str:
    import trafilatura  # type: ignore[import-not-found]

    text = trafilatura.extract(doc, url=url, include_comments=False, include_tables=False, favor_precision=True)
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # It gives headings as bare lines: mark them as the other readers do.
        heading = len(line) <= 140 and len(line.split()) <= 14 and not re.search(r"[.!?:;,][\"”’)]*$", line) \
            and not line.startswith(("- ", "* "))
        out.append("## " + line.lstrip("# ") if heading or line.startswith("#") else line)
    while out and out[-1].startswith("## "):
        out.pop()
    return "\n\n".join(out)


def _with_readability(doc: str, url: str) -> str:
    from readability import Document  # type: ignore[import-not-found]

    return builtin_text(Document(doc, url=url).summary(html_partial=True))


EXTRACTORS: tuple[tuple[str, Callable[[str, str], str]], ...] = (
    ("trafilatura", _with_trafilatura), ("readability", _with_readability),
    ("built-in", lambda doc, url: builtin_text(doc)))


def extract(doc: str, url: str, only: str = "") -> tuple[str, str]:
    """(the article's text, which reader found it). `only` names one of EXTRACTORS."""
    best, how = "", ""
    doc = ungated(doc)  # before any reader, a library's included, sees the page
    for name, fn in EXTRACTORS:
        if only and name != only:
            continue
        try:
            text = fn(doc, url)
        except ImportError:
            continue
        except Exception:  # noqa: BLE001 - a library choking on a page moves on to the next reader
            continue
        if len(text.split()) >= MIN_WORDS:
            return text, name
        if len(text) > len(best):
            best, how = text, name
    return best, how


def decode(page: Page) -> str:
    raw = page.body[:MAX_BYTES]
    declared = re.search(rb"(?i)<meta[^>]+charset\s*=\s*[\"']?\s*([\w-]+)", raw[:4096])
    for name in (page.charset, declared.group(1).decode("ascii", "replace") if declared else "", "utf-8"):
        if not name:
            continue
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def article_from(doc: str, url: str, asked: str = "", only: str = "") -> Article:
    """An Article from a page's HTML. Raises ScrapeError when there's no article in it to read."""
    facts = metadata(doc, url)
    text, how = extract(doc, url, only)
    words = len(text.split())
    host = _host(url)
    paid = ScrapeError(f"{host} shows this article to subscribers only. ClipMonster only reads pages anyone "
                       "can open; paste the part you're allowed to use as your own script instead.")
    if facts["paid"]:  # the page says so itself, whether or not its text happens to be in the HTML
        raise paid
    if words < MIN_WORDS:
        # Too little to retell, and either the page speaks of subscribing or a part marked as the
        # subscribers' was left unread that could have been the article (not a menu item or an empty box):
        # say that, not "no article".
        whole = web.clean(doc)
        if PAYWALL.search(whole[:20000]) or len(whole.split()) - len(web.clean(ungated(doc)).split()) >= MIN_WORDS:
            raise paid
        raise ScrapeError(f"No article text was found at {host} ({words} words). The page may be a video, a list of "
                          "links, or drawn by JavaScript. Paste its text as a prompt or script instead.")
    paras = text.split("\n\n")
    while paras and paras[0].startswith("## "):  # the headline again, or a heading before anything is said
        paras.pop(0)
    text = "\n\n".join(paras)
    canonical = facts["canonical"]
    try:
        final = check_url(canonical, lookup=False) if canonical else url
    except ScrapeError:
        final = url
    if _host(final) != host:  # a canonical address on another site isn't taken on trust
        final = url
    return Article(url=final, title=facts["title"], site=facts["site"] or host, byline=facts["byline"],
                   date=facts["date"], description=facts["description"], text=text, extractor=how, asked=asked or url)


def read(url: str, *, fetcher: Fetch | None = None, resolve: Resolve | None = None, log: Log | None = None,
         check: Callable[[], None] | None = None, robots: bool = True) -> Article:
    """Read the article at a public address. Raises ScrapeError with a reason a person can act on."""
    log = log or (lambda m: None)
    asked = check_url(url, resolve)
    host = _host(asked)
    def ask(address: str) -> None:
        if robots and not allowed(address, fetcher=fetcher, resolve=resolve):
            raise ScrapeError(f"{_host(address)} asks programs not to read that page (its robots.txt), so "
                              "ClipMonster doesn't. Paste the text you're allowed to use as your own script instead.")

    ask(asked)
    page = get(asked, fetcher=fetcher, resolve=resolve, check=check, hop=ask)  # a redirect's site is asked too
    host = _host(page.url or asked)
    if page.status in (401, 402, 403, 407, 451):
        raise ScrapeError(f"{host} doesn't show that page to everyone (it answered {page.status}): it needs a "
                          "sign-in or a subscription, or blocks programs. ClipMonster only reads public pages.")
    if page.status == 404 or page.status == 410:
        raise ScrapeError(f"{host} has no page at that address ({page.status}). Check the link.")
    if page.status == 429:
        raise ScrapeError(f"{host} says it's being asked too often (429). Try again in a few minutes.")
    if page.status != 200:
        raise ScrapeError(f"{host} answered {page.status} instead of the page.")
    kind = (page.content_type or "").lower()
    if kind and not (kind.startswith("text/") or "html" in kind or "xml" in kind):
        raise ScrapeError(f"That address is a file ({kind}), not an article page.")
    if len(page.body) > MAX_BYTES:
        log(f"The page is over {MAX_BYTES // 1_000_000} MB; reading its first part.")
    art = article_from(decode(page), page.url or asked, asked)
    log(f"Read \"{art.title or host}\" from {art.site or host}: {art.words} words"
        + (f", by {art.byline}" if art.byline else "") + (f", {art.date[:10]}" if art.date else "")
        + f" ({art.extractor} reader).")
    return art


# ---------------------------------------------------------------- what the writer is handed

_SENT = re.compile(r"(?<=[.!?])[\"”’)]*\s+(?=[\"“‘(]?[A-Z0-9])")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]{2,}")


def distil(article: Article, limit: int = 6000) -> str:
    """The article cut to `limit` characters for the script writer: all of it when it fits; otherwise its
    opening paragraphs whole (where an article says what it's about) and, from the rest, the sentences that
    carry the most of the article's own vocabulary without repeating each other, in their original order."""
    paras = [p for p in article.paragraphs if not p.startswith("## ")]
    whole = "\n\n".join(paras)
    if len(whole) <= limit:
        return whole
    from ..creator.director import STOPWORDS

    count: dict[str, float] = {}
    for w in _WORD.findall(whole.lower()):
        if w not in STOPWORDS:
            count[w] = count.get(w, 0.0) + 1.0
    head: list[str] = []
    size = 0
    for p in paras:
        if size + len(p) > limit * 0.35 and head:
            break
        head.append(p)
        size += len(p) + 2
    rest = [s.strip() for p in paras[len(head):] for s in _SENT.split(p) if len(s.split()) >= 6][:600]
    bags = [{w for w in _WORD.findall(sent.lower()) if w not in STOPWORDS} for sent in rest]
    for p in head:  # what the opening already said counts for less
        for w in set(_WORD.findall(p.lower())):
            if w in count:
                count[w] *= 0.5
    keep: set[int] = set()
    left = set(range(len(rest)))
    while left:
        def score(i: int) -> float:
            if not bags[i]:
                return 0.0
            value = sum(count.get(w, 0) for w in bags[i]) / (len(bags[i]) ** 0.5)
            # Numbers, dates and amounts are what a script gets wrong without them.
            return value * (1.25 if re.search(r"\d", rest[i]) else 1.0)

        best = max(left, key=lambda i: (score(i), -i))
        left.discard(best)
        if score(best) <= 0 or size + len(rest[best]) + 1 > limit:
            if size > limit * 0.97:
                break
            continue
        keep.add(best)
        size += len(rest[best]) + 1
        for w in bags[best]:  # the next pick should add something new, not say this again
            count[w] *= 0.5
    tail = " ".join(s for i, s in enumerate(rest) if i in keep)
    return ("\n\n".join(head) + ("\n\n" + tail if tail else ""))[:limit]


@dataclass
class Distilled:
    """An article ready for the writer."""

    article: Article
    text: str
    cut: bool = False
    notes: list[str] = field(default_factory=list)


def prepare(article: Article, limit: int = 6000) -> Distilled:
    text = distil(article, limit)
    return Distilled(article, text, cut=len(text) < len("\n\n".join(article.paragraphs)) - 50)
