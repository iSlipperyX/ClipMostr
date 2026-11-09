"""Looking a topic up online before the script is written, so the AI works from today's facts.

A local model only knows what it was trained on, which can be a year or more old: asked for "the latest GTA 6
news" it wrote about a 2024 release date. Before writing, the Creator now searches:

* Google News and Bing News (their free RSS feeds: headline, publisher, date, summary), newest first, and
  reads the text of the first few articles that link straight to the publisher;
* Wikipedia (its API), for the topic's current summary.

What it finds goes into the script-writer's prompt with today's date, and the sources are kept with the video
(storyboard.json, clips.json and its README). No account or API key is needed; Settings → Creator → Look it up
online first turns it off. Nothing here is required: when the internet or a site is down, the script is
written as before, with today's date and a rule against guessing at recent events.
"""

from __future__ import annotations

import email.utils
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .. import __version__

Log = Callable[[str], None]
Opener = Callable[[str, float], bytes]

USER_AGENT = f"Mozilla/5.0 (compatible; ClipMonster/{__version__}; +https://github.com/iSlipperyX/ClipMostr)"
TIMEOUT = 10.0
NEWS_KEEP = 8  # headlines given to the AI
ARTICLES = 3  # articles whose text is read
ARTICLE_CHARS = 1500  # of each article's text
WIKI_CHARS = 1800
BRIEF_CHARS = 7000  # everything together (about 1,800 tokens: room even for the 3B model's context)
MAX_PAGE_BYTES = 3_000_000

# Words that say what to make, not what it's about ("make a video about the latest GTA 6 news").
FILLER = {"make", "a", "an", "the", "video", "videos", "about", "on", "of", "tell", "me", "us", "explain", "latest",
          "newest", "new", "recent", "news", "update", "updates", "everything", "you", "need", "to", "know",
          "short", "tiktok", "please", "with", "all", "info", "information", "facts", "and", "today", "this",
          "week", "what", "whats", "what's", "is", "are", "for", "my", "some", "give"}


@dataclass
class Source:
    title: str
    url: str = ""
    publisher: str = ""
    date: str = ""  # ISO 8601 (UTC) when known
    summary: str = ""
    text: str = ""  # the article's own words, when it was read
    kind: str = "news"  # news | wiki

    def short(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k in ("title", "url", "publisher", "date", "kind") and v}


@dataclass
class Research:
    query: str
    sources: list[Source] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # sites that couldn't be reached

    @property
    def found(self) -> bool:
        return bool(self.sources)


def _get(url: str, timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html,application/xhtml+xml,application/xml,"
                                                         "application/rss+xml,application/json;q=0.9,*/*;q=0.8",
                                               "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(MAX_PAGE_BYTES)


# ---------------------------------------------------------------- the query

def query_of(prompt: str) -> str:
    """What to search for: the prompt without the asking words ("the latest GTA 6 news" -> "GTA 6")."""
    words = re.findall(r"[\w'’.+#-]+", prompt)
    kept = [w for w in words if w.lower().strip("'’.") not in FILLER]
    q = " ".join(kept[:10]).strip(" .")
    return q or " ".join(words[:10])


# ---------------------------------------------------------------- reading what comes back

def clean(text: str) -> str:
    """Plain text from an HTML fragment."""
    text = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def page_text(raw: bytes | str, limit: int = ARTICLE_CHARS) -> str:
    """An article's own words: its <p> paragraphs that read like sentences (not menus, captions or cookie
    notices), in order, up to `limit` characters."""
    doc = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    doc = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer|aside|form|template)[^>]*>.*?</\1>", " ", doc)
    article = re.search(r"(?is)<article[^>]*>(.*?)</article>", doc)
    body = article.group(1) if article else doc
    out: list[str] = []
    size = 0
    for para in re.findall(r"(?is)<p[^>]*>(.*?)</p>", body):
        line = clean(para)
        if len(line) < 60 or len(line.split()) < 10 or not re.search(r"[.!?]['\"”’)]?$", line):
            continue
        if re.search(r"(?i)cookie|subscribe|newsletter|sign up|all rights reserved|javascript", line):
            continue
        out.append(line)
        size += len(line) + 1
        if size >= limit:
            break
    return " ".join(out)[:limit].rsplit(" ", 1)[0] if size > limit else " ".join(out)


def _iso(value: str) -> str:
    """An RSS date (RFC 822) or ISO date as ISO 8601 UTC, '' when it can't be read."""
    value = (value or "").strip()
    if not value:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rss(raw: bytes, kind: str = "news") -> list[Source]:
    """Items of an RSS feed (Google News, Bing News)."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out: list[Source] = []
    for item in root.iter("item"):
        def text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = clean(text("title"))
        publisher = clean(text("source"))
        for el in item:  # Bing: <News:Source>; namespaced tags
            if el.tag.lower().endswith("}source") and el.text and not publisher:
                publisher = clean(el.text)
        if publisher and title.endswith(" - " + publisher):  # Google News puts the publisher in the title
            title = title[: -len(publisher) - 3].strip()
        summary = clean(text("description"))
        if summary.startswith(title):
            summary = summary[len(title):].strip(" -–—")
        if publisher and summary.endswith(publisher):
            summary = summary[: -len(publisher)].strip(" -–— ")
        if title:
            out.append(Source(title, text("link"), publisher, _iso(text("pubDate")), summary[:400], kind=kind))
    return out


# ---------------------------------------------------------------- the sites

def google_news(query: str, opener: Opener = _get) -> list[Source]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return parse_rss(opener(url, TIMEOUT))


def bing_news(query: str, opener: Opener = _get) -> list[Source]:
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": query, "format": "rss",
                                                                         "qft": "sortbydate=\"1\""})
    found = parse_rss(opener(url, TIMEOUT))
    for src in found:  # Bing wraps links in its own redirect; the publisher's address is in "url="
        if "bing.com" in src.url:
            target = urllib.parse.parse_qs(urllib.parse.urlparse(src.url).query).get("url")
            if target:
                src.url = target[0]
    return found


def wikipedia(query: str, opener: Opener = _get) -> list[Source]:
    """The best-matching Wikipedia article's current introduction."""
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": "2", "generator": "search", "gsrsearch": query,
        "gsrlimit": "1", "prop": "extracts|info|revisions", "exintro": "1", "explaintext": "1", "inprop": "url",
        "rvprop": "timestamp", "redirects": "1"})
    data = json.loads(opener(url, TIMEOUT).decode("utf-8"))
    out = []
    for page in (data.get("query") or {}).get("pages") or []:
        extract = " ".join(str(page.get("extract") or "").split())
        if not extract:
            continue
        revs = page.get("revisions") or [{}]
        out.append(Source(str(page.get("title") or query), str(page.get("fullurl") or ""), "Wikipedia",
                          _iso(str(revs[0].get("timestamp") or "")), text=extract[:WIKI_CHARS], kind="wiki"))
    return out


# ---------------------------------------------------------------- all together

def _key(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()[:70]


def gather(prompt: str, *, log: Log | None = None, check: Callable[[], None] | None = None,
           opener: Opener = _get, query: str = "") -> Research:
    """Search the news and Wikipedia for the prompt's topic (or `query`, as it is) and read the newest articles.
    Never raises for a site that's down: what couldn't be reached is in .problems."""
    log = log or (lambda m: None)
    query = " ".join(query.split()) or query_of(prompt)
    res = Research(query)
    news: list[Source] = []
    for name, fn in (("Google News", google_news), ("Bing News", bing_news), ("Wikipedia", wikipedia)):
        if check:
            check()
        try:
            got = fn(query, opener)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError, ET.ParseError) as exc:
            res.problems.append(f"{name}: {getattr(exc, 'reason', None) or exc}")
            continue
        if name == "Wikipedia":
            res.sources += got
        else:
            news += got
    # Newest first, each story once.
    seen: set[str] = set()
    news.sort(key=lambda s: s.date or "", reverse=True)
    kept: list[Source] = []
    for src in news:
        k = _key(src.title)
        if k in seen:
            continue
        seen.add(k)
        kept.append(src)
        if len(kept) >= NEWS_KEEP:
            break
    # Read the newest articles that link to the publisher (Google News links need a browser to follow).
    read = 0
    for src in kept:
        if read >= ARTICLES:
            break
        if not src.url.startswith("http") or "news.google.com" in src.url:
            continue
        if check:
            check()
        try:
            src.text = page_text(opener(src.url, TIMEOUT))
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            continue
        read += bool(src.text)
    res.sources = kept + res.sources
    if res.found:
        newest = next((s for s in kept if s.date), None)
        log(f"Looked \"{query}\" up online: {len(kept)} news stories" +
            (f" (newest {newest.date[:10]}, {newest.publisher or 'no publisher'})" if newest else "") +
            (", Wikipedia's article" if any(s.kind == "wiki" for s in res.sources) else "") +
            (f", read {read} article{'s' if read != 1 else ''}" if read else "") + ".")
    elif res.problems:
        log("Couldn't look it up online (" + "; ".join(res.problems) + "): writing from what the AI knows.")
    else:
        log(f"Nothing came up online for \"{query}\": writing from what the AI knows.")
    return res


def today_text(now: float | None = None) -> str:
    t = time.localtime(now or time.time())
    return time.strftime("%A %d %B %Y", t).replace(" 0", " ")


def brief(res: Research | None, now: float | None = None, limit: int = BRIEF_CHARS) -> str:
    """The part of the script-writer's prompt with today's date and what was found."""
    today = today_text(now)
    head = (f"Today's date is {today}. What you learnt in training may be out of date: things you remember as "
            "upcoming may have happened, been delayed or been cancelled since.")
    if res is None or not res.found:
        return head + (" If the topic is news, a release, a launch or anything else that changes over time, don't "
                       "state dates, prices or \"coming soon\" claims you can't be sure are still true today; talk "
                       "about what's certain instead.")
    parts = [head, "", f"Sources found online just now for \"{res.query}\" (newest first):"]
    size = sum(len(p) for p in parts)
    for i, s in enumerate(res.sources, 1):
        when = s.date[:10] if s.date else "date unknown"
        line = f"[{i}] {s.title} — {s.publisher or 'unknown'}, {when}"
        body = s.text or s.summary
        if body:
            line += f"\n    {body}"
        if size + len(line) > limit:
            line = line[: max(0, limit - size)].rsplit(" ", 1)[0]
            if len(line) < 80:
                break
        parts.append(line)
        size += len(line) + 1
    parts += ["", "Rules for facts: base every date, number, name and claim about this topic on these sources, not "
                  "on memory. When they disagree, the newest source wins (you may say the date moved). Leave out "
                  "what the sources don't support rather than guess. Something the sources say already happened is "
                  "never \"coming\", and say \"as of\" the month when a date could still change."]
    return "\n".join(parts)
