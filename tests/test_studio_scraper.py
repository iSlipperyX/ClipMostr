"""Reading an article: which addresses are read, what's taken from a page, and what's refused."""

import http.server
import threading

import pytest

from ytclip.studio import scraper as S

PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - every name is on the public internet, unless a test says

PARAS = [
    "Cats purr at a frequency between 25 and 150 hertz, a range that researchers have linked to bone growth and "
    "the healing of soft tissue in laboratory studies going back to the 1990s.",
    "The sound is made in the larynx. A signal from the brain makes the muscles there twitch between 25 and 150 "
    "times a second, opening and closing the gap between the vocal cords as the cat breathes in and out.",
    "For a long time people assumed purring simply meant a cat was happy. But cats also purr when they are "
    "frightened, injured or giving birth, which is why some scientists now think of it as a way of calming down.",
    "In 2001 the bioacoustician Elizabeth von Muggenthaler recorded 44 cats, including cheetahs and pumas, and "
    "found that every one of them purred at frequencies used in human medicine to help fractures heal.",
    "That does not prove that purring heals the cat. It may only be that a low, steady vibration costs very "
    "little energy and keeps muscles and bones working while a cat lies still for most of the day.",
    "What is certain is that kittens start to purr when they are a few days old, long before their eyes open, "
    "and that their mother purrs back while she feeds them.",
]


def page(paras=PARAS, *, head="", wrap="article", extra="", title="Why cats purr | Example News") -> str:
    body = "".join(f"<p>{p}</p>" for p in paras)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>{head}</head><body>
<header><nav><ul><li><a href="/">Home</a></li><li><a href="/pets">Pets and other animals you may like</a></li></ul></nav></header>
<div class="cookie-banner"><p>We use cookies to make this site work, and by reading on you agree that we may keep doing so forever.</p></div>
<{wrap} class="story"><h1>Why cats purr</h1>{body}
<h2>What nobody knows yet</h2><p>Whether a purring cat on your lap does anything for your own bones has never been tested in a proper trial, whatever the internet says.</p>
<div class="share-tools"><p>Share this story with every single one of your friends on all of the social networks right now.</p></div>
</{wrap}>
<aside><p>Sign up to our newsletter to get the best stories about animals in your inbox every single morning.</p></aside>
{extra}<footer><p>Copyright 2026 Example News Limited. All rights reserved, including the right to purr.</p></footer></body></html>"""


HEAD = """<meta property="og:site_name" content="Example News"><meta property="og:title" content="Why cats purr">
<meta name="description" content="The science of the purr."><meta name="author" content="Meta Author">
<link rel="canonical" href="https://www.example.org/science/why-cats-purr">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"x"},
{"@type":"NewsArticle","headline":"Why cats purr","datePublished":"2026-03-04T09:30:00+01:00",
"author":[{"@type":"Person","name":"Dana Reyes"},{"@type":"Person","name":"Sam Okafor"}],
"publisher":{"@type":"Organization","name":"Example News"}}]}</script>"""


def site(pages: dict, robots: str | None = None, log: list | None = None):
    """A fetcher for a made-up web: {url: html | Page | Exception}."""
    def fetch(url: str, timeout: float) -> S.Page:
        if log is not None:
            log.append(url)
        if url.endswith("/robots.txt"):
            if isinstance(robots, S.Page):
                return robots
            return S.Page(url, 200 if robots is not None else 404, "text/plain", (robots or "").encode())
        got = pages.get(url)
        if got is None:
            return S.Page(url, 404, "text/html")
        if isinstance(got, Exception):
            raise got
        if isinstance(got, S.Page):
            return got
        return S.Page(url, 200, "text/html", got.encode("utf-8"), charset="utf-8")
    return fetch


URL = "https://www.example.org/science/why-cats-purr?utm_source=x"


# ---------------------------------------------------------------- addresses

@pytest.mark.parametrize("url, said", [
    ("", "no address"),
    ("ftp://example.org/a", "Only http and https"),
    ("file:///etc/passwd", "Only http and https"),
    ("javascript:alert(1)", "Only http and https"),
    ("https://user:pw@example.org/a", "user name or password"),
    ("https://", "no site"),
    ("http://localhost:8080/admin", "this computer or your own network"),
    ("http://printer/", "this computer or your own network"),
    ("http://nas.local/", "this computer or your own network"),
    ("http://127.0.0.1/", "this computer or your own network"),
    ("http://192.168.1.1/", "this computer or your own network"),
    ("http://10.0.0.5:8188/", "this computer or your own network"),
    ("http://169.254.169.254/latest/meta-data/", "this computer or your own network"),
    ("http://[::1]/", "this computer or your own network"),
    ("http://[::ffff:127.0.0.1]/", "this computer or your own network"),
    ("http://0.0.0.0/", "this computer or your own network"),
    ("http://example.org:99999/", "isn't a web address"),
])
def test_addresses_that_are_refused(url, said):
    with pytest.raises(S.ScrapeError, match=said):
        S.check_url(url, PUBLIC)


def test_addresses_that_are_read_are_tidied():
    assert S.check_url("example.org/a b?x=1#frag", PUBLIC) == "https://example.org/a%20b?x=1"
    assert S.check_url("HTTP://Example.ORG.", PUBLIC) == "http://example.org/"
    assert S.check_url("https://example.org:8443/x", PUBLIC) == "https://example.org:8443/x"
    assert S.check_url("https://example.org:443/x", PUBLIC) == "https://example.org/x"
    assert S.check_url("https://93.184.216.34/x") == "https://93.184.216.34/x"


def test_a_public_name_that_points_inside_is_refused():
    with pytest.raises(S.ScrapeError, match="own network"):
        S.check_url("https://intranet.example.org/", lambda host: ["93.184.216.34", "10.1.2.3"])

    def nowhere(host):
        raise OSError(-2, "Name or service not known")

    with pytest.raises(S.ScrapeError, match="couldn't be found"):
        S.check_url("https://no-such-site.example/", nowhere)
    assert S.check_url("https://no-such-site.example/", nowhere, lookup=False)  # only what the address says


def test_redirects_are_followed_but_never_inside():
    hops = []
    pages = {"https://example.org/a": S.Page("https://example.org/a", 302, location="/b"),
             "https://example.org/b": S.Page("https://example.org/b", 301, location="https://www.example.org/c"),
             "https://www.example.org/c": "<p>here</p>",
             "https://example.org/in": S.Page("", 302, location="http://192.168.0.10/router"),
             "https://example.org/loop": S.Page("", 302, location="/loop")}
    got = S.get("https://example.org/a", fetcher=site(pages, log=hops), resolve=PUBLIC)
    assert got.status == 200 and hops == ["https://example.org/a", "https://example.org/b", "https://www.example.org/c"]
    with pytest.raises(S.ScrapeError, match="own network"):
        S.get("https://example.org/in", fetcher=site(pages), resolve=PUBLIC)
    with pytest.raises(S.ScrapeError, match="keeps redirecting"):
        S.get("https://example.org/loop", fetcher=site(pages), resolve=PUBLIC)
    with pytest.raises(S.ScrapeError, match=r"example\.org didn't answer \(timed out\)"):
        S.get("https://example.org/x", fetcher=site({"https://example.org/x": TimeoutError("timed out")}), resolve=PUBLIC)


# ---------------------------------------------------------------- reading

def test_an_article_is_read_with_where_it_came_from():
    said = []
    art = S.read(URL, fetcher=site({URL: page(head=HEAD)}), resolve=PUBLIC, log=said.append)
    assert art.title == "Why cats purr" and art.site == "Example News"
    assert art.byline == "Dana Reyes, Sam Okafor" and art.date == "2026-03-04T08:30:00Z"
    assert art.url == "https://www.example.org/science/why-cats-purr" and art.asked == URL  # the canonical address
    assert art.description == "The science of the purr."
    assert art.paragraphs[0] == PARAS[0] and art.paragraphs[5] == PARAS[5]
    assert "## What nobody knows yet" in art.paragraphs and art.words > 200
    for junk in ("cookies", "newsletter", "Copyright", "Share this story", "Home", "Pets and other"):
        assert junk not in art.text
    assert art.source() == {"title": "Why cats purr", "url": art.url, "publisher": "Example News",
                            "date": "2026-03-04T08:30:00Z", "kind": "article", "author": "Dana Reyes, Sam Okafor"}
    assert art.credit() == "Source: Why cats purr, Example News (example.org)"
    assert "Read \"Why cats purr\" from Example News" in said[0] and "by Dana Reyes" in said[0]


def test_a_plain_page_without_metadata_still_reads():
    art = S.article_from(page(wrap="div", title="Why cats purr"), "https://blog.example.net/p/1")
    assert art.title == "Why cats purr" and art.site == "blog.example.net" and art.byline == "" and art.date == ""
    assert art.url == "https://blog.example.net/p/1" and art.paragraphs[0] == PARAS[0]
    assert art.extractor in ("trafilatura", "readability", "built-in")


def test_the_built_in_reader_copes_with_untidy_pages():
    # Paragraphs never closed, one <div> a paragraph, the lot inside a wrapper whose class says "sidebar".
    untidy = "<html><body><div class='layout has-sidebar'>" + "".join(
        f"<div class='para'><p>{p}" for p in PARAS) + "</div><p>Read more: <a href='/x'>ten other things your cat " \
        "does that will surprise you today</a></body></html>"
    text = S.builtin_text(untidy)
    assert text.split("\n\n")[:6] == PARAS and "Read more" not in text
    assert S.builtin_text("<html><body><p>Too short.</p></body></html>") == ""
    assert S.builtin_text("<<<>>> not html at all") == ""
    listy = page(PARAS[:3] + ["<ul><li>Kittens begin to purr within days of being born, before their eyes open.</li>"
                              "<li>Short.</li></ul>"])
    assert "- Kittens begin to purr within days of being born, before their eyes open." in S.builtin_text(listy)
    hidden = page(PARAS, extra="<div style='display: none'><p>" + PARAS[0].replace("Cats", "Dogs") + "</p></div>")
    assert "Dogs purr" not in S.builtin_text(hidden)


def test_a_canonical_address_elsewhere_isnt_trusted():
    head = '<link rel="canonical" href="https://evil.example.com/steal">'
    assert S.article_from(page(head=head), "https://www.example.org/a").url == "https://www.example.org/a"
    head = '<link rel="canonical" href="http://192.168.1.1/">'
    assert S.article_from(page(head=head), "https://www.example.org/a").url == "https://www.example.org/a"


@pytest.mark.parametrize("answer, said", [
    (S.Page("", 401), "doesn't show that page to everyone"),
    (S.Page("", 403), "doesn't show that page to everyone"),
    (S.Page("", 402), "sign-in or a subscription"),
    (S.Page("", 404), "has no page at that address"),
    (S.Page("", 429), "asked too often"),
    (S.Page("", 500), "answered 500"),
    (S.Page("", 200, "application/pdf", b"%PDF"), "is a file"),
    (S.Page("", 200, "text/html", b"<html><body><p>Hello.</p></body></html>"), "No article text was found"),
    (S.Page("", 200, "text/html", ("<html><body><article><p>" + PARAS[0][:120] + "</p><div class='gate'><p>Subscribe "
            "to continue reading this story.</p></div></article></body></html>").encode()), "subscribers only"),
    (S.Page("", 200, "text/html", ('<html><head><script type="application/ld+json">{"@type":"NewsArticle",'
            '"isAccessibleForFree":false}</script></head><body><p>' + PARAS[0] + "</p></body></html>").encode()),
     "subscribers only"),
])
def test_pages_that_arent_public_articles_say_so(answer, said):
    answer.url = URL
    with pytest.raises(S.ScrapeError, match=said):
        S.read(URL, fetcher=site({URL: answer}), resolve=PUBLIC)


def test_robots_txt_is_asked_first_and_obeyed():
    asked = []
    pages = {URL: page()}
    S.read(URL, fetcher=site(pages, "User-agent: *\nDisallow: /private/\n", asked), resolve=PUBLIC)
    assert asked[0] == "https://www.example.org/robots.txt" and asked[1] == URL
    for rules in ("User-agent: *\nDisallow: /\n", "User-agent: ClipMonster\nDisallow: /science/\n\nUser-agent: *\nAllow: /\n"):
        asked.clear()
        with pytest.raises(S.ScrapeError, match="asks programs not to read"):
            S.read(URL, fetcher=site(pages, rules, asked), resolve=PUBLIC)
        assert asked == ["https://www.example.org/robots.txt"]  # the page itself was never requested
    # Rules for other robots don't apply; no robots.txt, or one that can't be had, means reading is fine.
    assert S.read(URL, fetcher=site(pages, "User-agent: GPTBot\nDisallow: /\n"), resolve=PUBLIC).words > 100
    assert S.read(URL, fetcher=site(pages, None), resolve=PUBLIC).words > 100
    assert S.read(URL, fetcher=site(pages, S.Page("", 403)), resolve=PUBLIC).words > 100
    with pytest.raises(S.ScrapeError, match="asks programs not to read"):  # a site whose robots.txt is broken
        S.read(URL, fetcher=site(pages, S.Page("", 503)), resolve=PUBLIC)


def test_other_encodings_and_big_pages():
    latin = page(["Señor Muñoz dice que su gato ronronea a veinticinco hercios cada mañana, y que eso le cura "
                  "el reúma, aunque ningún estudio lo ha comprobado todavía en personas."] + PARAS).replace("utf-8", "iso-8859-1")
    got = S.Page(URL, 200, "text/html", latin.encode("iso-8859-1"))
    assert "Señor Muñoz" in S.read(URL, fetcher=site({URL: got}), resolve=PUBLIC).text
    said = []
    big = S.Page(URL, 200, "text/html", page().encode() + b" " * (S.MAX_BYTES + 10))
    assert S.read(URL, fetcher=site({URL: big}), resolve=PUBLIC, log=said.append).words > 100
    assert "reading its first part" in said[0]


def test_distil_keeps_the_opening_and_the_telling_sentences():
    filler = [f"The {n} cat in the shelter was called {name} and liked to sit by the window watching the rain "
              "fall on the yard for most of the afternoon, which is not unusual." for n, name in
              zip(("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"),
                  ("Milo", "Luna", "Olive", "Jasper", "Hazel", "Felix", "Poppy", "Otis"))]
    art = S.article_from(page(PARAS[:2] + filler + PARAS[2:]), "https://www.example.org/a")
    whole = S.distil(art, 100_000)
    assert whole.startswith(PARAS[0]) and "## " not in whole and len(whole) > 2500
    cut = S.distil(art, 1500)
    assert len(cut) <= 1500 and cut.startswith(PARAS[0] + "\n\n" + PARAS[1])  # the opening, whole
    assert "In 2001 the bioacoustician" in cut  # then what the article is about, numbers first
    assert cut.count("in the shelter") <= 4  # rather than whatever came next (eight sentences that say the same)
    ready = S.prepare(art, 1500)
    assert ready.cut and ready.text == cut and not S.prepare(art, 100_000).cut


def test_fetch_talks_http_and_reports_redirects_without_following():
    class Handler(http.server.BaseHTTPRequestHandler):
        seen = []

        def do_GET(self):  # noqa: N802
            Handler.seen.append((self.path, self.headers.get("User-Agent")))
            if self.path == "/moved":
                self.send_response(302)
                self.send_header("Location", "/article")
                self.end_headers()
            elif self.path == "/secret":
                self.send_response(403)
                self.end_headers()
            else:
                body = page().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        ok = S.fetch(base + "/article")
        assert ok.status == 200 and ok.content_type == "text/html" and ok.charset == "utf-8" and b"purr" in ok.body
        moved = S.fetch(base + "/moved")
        assert moved.status == 302 and moved.location == "/article"
        assert S.fetch(base + "/secret").status == 403
        assert [p for p, _ in Handler.seen] == ["/article", "/moved", "/secret"]  # the redirect wasn't followed
        assert all("ClipMonster/" in agent for _, agent in Handler.seen)  # and it says who it is
        with pytest.raises(S.ScrapeError, match="this computer"):  # read() itself never goes there
            S.read(base + "/article")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("name, module", [("trafilatura", "trafilatura"), ("readability", "readability"),
                                          ("built-in", "html")])
def test_each_reader_finds_the_same_article(name, module):
    pytest.importorskip(module)
    art = S.article_from(page(head=HEAD), "https://www.example.org/science/why-cats-purr", only=name)
    assert art.extractor == name and art.title == "Why cats purr"
    paras = [p for p in art.paragraphs if not p.startswith("## ")]
    assert paras[:6] == PARAS
    for junk in ("cookies", "newsletter", "Copyright", "Pets and other"):
        assert junk not in art.text


def test_without_the_libraries_the_built_in_reader_is_used(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_libraries(name, *args, **kwargs):
        if name.split(".")[0] in ("trafilatura", "readability"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_libraries)
    assert S.article_from(page(), "https://www.example.org/a").extractor == "built-in"


# ---------------------------------------------------------------- what the review of the Studio found

def test_a_page_that_says_it_is_paid_for_is_not_read_even_with_its_text_in_the_html():
    """A paywall drawn by JavaScript ships the whole article in the page; the page still says it isn't free."""
    gated = "".join(f"<p>{p}</p>" for p in PARAS)
    doc = page(PARAS[:1], head='<script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree":false,'
               '"hasPart":{"@type":"WebPageElement","isAccessibleForFree":false,"cssSelector":".paywall"}}</script>',
               extra=f'<div class="paywall">{gated}</div>')
    with pytest.raises(S.ScrapeError, match="subscribers only"):
        S.article_from(doc, URL)
    # Without the flag, the part marked as the subscribers' is still never what's read: by any pass of the reader.
    marked = page([], extra=f'<div class="paywall">{gated}</div>')
    assert len(S.builtin_text(marked).split()) < 80
    assert "bioacoustician" not in S.builtin_text(marked)


def test_robots_is_asked_again_wherever_a_redirect_leads():
    """A short link whose own site allows everything must not get round the story's site saying no."""
    asked = []

    def fetch(url: str, timeout: float) -> S.Page:
        asked.append(url)
        if url == "https://short.example/robots.txt":
            return S.Page(url, 404, "text/plain")
        if url == "https://news.example/robots.txt":
            return S.Page(url, 200, "text/plain", b"User-agent: *\nDisallow: /members/\n")
        if url == "https://short.example/x":
            return S.Page(url, 302, location="https://news.example/members/story")
        if url == "https://short.example/y":
            return S.Page(url, 302, location="https://news.example/open/story")
        if url.startswith("https://news.example/"):
            return S.Page(url, 200, "text/html", page().encode(), charset="utf-8")
        return S.Page(url, 404, "text/html")

    with pytest.raises(S.ScrapeError, match=r"news\.example asks programs not to read that page"):
        S.read("https://short.example/x", fetcher=fetch, resolve=PUBLIC)
    assert "https://news.example/members/story" not in asked  # refused before it was fetched
    assert S.read("https://short.example/y", fetcher=fetch, resolve=PUBLIC).words > 100
    assert asked.count("https://news.example/robots.txt") == 2


def test_a_robots_file_with_a_byte_order_mark_still_says_no():
    fetch = site({}, robots=S.Page("", 200, "text/plain", b"\xef\xbb\xbfUser-agent: *\nDisallow: /\n"))
    assert S.allowed("https://example.org/story", fetcher=fetch, resolve=PUBLIC) is False


def test_addresses_with_letters_a_browser_accepts_are_encoded_not_crashed_on():
    assert S.check_url("https://de.wikipedia.org/wiki/Käse", PUBLIC) == "https://de.wikipedia.org/wiki/K%C3%A4se"
    assert S.check_url("https://bücher.example/a", PUBLIC) == "https://xn--bcher-kva.example/a"
    assert S.check_url("https://example.org/a%20b?q=%C3%A4", PUBLIC) == "https://example.org/a%20b?q=%C3%A4"  # once only
    seen = []

    def fetch(url: str, timeout: float) -> S.Page:
        seen.append(url)
        if url.endswith("/start"):  # a Location header's raw UTF-8 bytes arrive as Latin-1 text
            return S.Page(url, 302, location="/cafÃ© au lait")
        return S.Page(url, 200, "text/html", b"ok")

    assert S.get("https://example.org/start", fetcher=fetch, resolve=PUBLIC).status == 200
    assert seen[-1] == "https://example.org/caf%C3%A9%20au%20lait"


def test_whatever_the_connection_throws_is_a_plain_reason():
    import http.client

    for boom in (http.client.BadStatusLine("x"), http.client.InvalidURL("bad"), UnicodeEncodeError("ascii", "é", 0, 1, "no"),
                 TimeoutError("slow")):
        with pytest.raises(S.ScrapeError, match="example.org didn't answer"):
            S.get("https://example.org/a", fetcher=site({"https://example.org/a": boom}), resolve=PUBLIC)


def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    """A name can answer the check with a public address and the request with 127.0.0.1 ("DNS rebinding"): the
    request is sent to an address check_url() approved, never to a fresh lookup of the name."""
    lookups = []

    def answers(host, *a, **k):
        lookups.append(host)
        ip = "93.184.216.34" if len(lookups) == 1 else "127.0.0.1"
        return [(S.socket.AF_INET, S.socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(S.socket, "getaddrinfo", answers)
    assert S.check_url("http://rebind.example.com/") == "http://rebind.example.com/"
    dialled = []

    def dial(address, *a, **k):
        dialled.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(S.socket, "create_connection", dial)
    with pytest.raises(OSError):
        S._pinned_connect(("rebind.example.com", 80), 5)
    assert dialled == [("93.184.216.34", 80)] and lookups == ["rebind.example.com"]
    # A name that was never checked here (a proxy's own) is dialled as it is.
    with pytest.raises(OSError):
        S._pinned_connect(("proxy.corp.example", 3128), 5)
    assert dialled[-1] == ("proxy.corp.example", 3128)
    # And the opener fetch() builds really uses the pinned connections.
    opener = S.urllib.request.build_opener(S._NoRedirect, S._PinnedHandler, S._PinnedTLSHandler)
    kinds = {type(h).__name__ for h in opener.handlers}
    assert {"_PinnedHandler", "_PinnedTLSHandler"} <= kinds and "HTTPSHandler" not in kinds and "HTTPHandler" not in kinds
