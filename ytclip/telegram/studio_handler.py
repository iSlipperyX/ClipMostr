"""Asking for videos from Telegram, in plain words.

Three things can be asked for, by a command or just by saying it:

* a video made from an idea: "Make a 65s video about 3 dark psychology tricks for @PsychologyFacts" (/make);
* a video made from a web page: "Turn this link https://… into a reel for History" (the Studio reads the
  article and retells it);
* clips of someone's video: a YouTube link (/clip), or a request for several (/request 10 30-45s Gaming).

`understand` reads the sentence (what it's about, how long, which link, which voice or captions were asked
for); `resolve` finds the category it's for, in three steps:

1. it was said: "for History", "to @psychologyfacts" (a category's name, or its TikTok account's handle);
2. else what the idea is about is compared with what each category is about (its name, keywords, special
   requests and topics), with the sentence model the sports search uses when it's on this PC, else by words
   and the topics they belong to. Only a clear winner counts, and the answer says which one was picked, with a
   button to change it while the video still waits;
3. else the categories are offered as buttons, each with its TikTok account.

The category then decides how the video is made (Categories → Video Creator: voice, pictures, captions, look;
left on automatic, a voice that suits its name), and the length is kept between 62 and 85 seconds whatever was
asked for: Creator Rewards only counts videos over a minute. The request goes into automation's queue as a
Creator or Studio job (Settings → Telegram → Make Telegram's videos with), and the Studio's own pipeline does
the rest: scraper (a link) → scriptwriter → voice_studio → timeline_builder → render.
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from . import state
from .api import ApiError, inline
from .common import MODE_LIFE, URL, Refused, esc, plural, trim
from .review_pipeline import digest_html

MIN_SECONDS, MAX_SECONDS = 62.0, 85.0  # what a video asked for here is kept between (director.REWARDS_RANGE)
IDEA_MAX = 1000
ASKS_KEPT = 60  # requests remembered so their category can still be changed while they wait
# Sites whose links are videos to clip. Any other link is a page to read and retell.
VIDEO_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "twitch.tv", "vimeo.com", "tiktok.com",
               "dailymotion.com", "kick.com", "rumble.com", "streamable.com", "instagram.com", "facebook.com",
               "fb.watch", "x.com", "twitter.com")

_NOUN = r"(?:videos?|reels?|shorts?|tik\s?toks?|clips?|vids?|explainers?)"
_HELLO = re.compile(r"^(?:(?:hey|hi|hello|ok|okay|yo|please|pls)\b[,!. ]*)+", re.I)
_WISH = re.compile(r"^(?:(?:can|could|would|will) you\s+(?:please\s+)?|i(?:'d| would)? like (?:you )?to\s+|"
                   r"i (?:want|need) (?:you )?to\s+|i (?:want|need|would like|'d like)\s+|give me\s+|let'?s\s+|"
                   r"please\s+)", re.I)
_VERB = r"(?:make|create|generate|produce|build|write|render|do|draft|put together)"
_MAKE = re.compile(rf"^(?:{_VERB}\s+)?(?:(?:me|us)\s+)?(?:(?:an?|the|one|another|some)\s+)?"
                   rf"(?:(?:new|short|quick|vertical|faceless|viral|little|tiktok)\s+)*(?P<noun>{_NOUN}\b)?\s*"
                   r"(?P<about>(?:all\s+)?about|on|explaining|covering|regarding|that explains?|showing|:|-|–|—)?\s*",
                   re.I)
_TURN = re.compile(r"\b(?:please\s+)?(?:turn|make|convert|transform|retell|summari[sz]e|rewrite|adapt|use)\s+"
                   r"(?:(?:this|that|the|it|these)\s*)?(?:(?:link|article|page|story|post|url|piece|news|blog)\s*)?"
                   rf"(?:(?:in)?to\s+(?:an?\s+)?(?:\w+\s+)?{_NOUN}|for\s+(?:an?\s+)?{_NOUN}|as\s+(?:an?\s+)?{_NOUN})?",
                   re.I)
_WANTS_VIDEO = re.compile(rf"\b(?:{_NOUN}|turn|retell|summari[sz]e|explain|about)\b", re.I)
_SECONDS = (
    re.compile(rf"(?<![\w.])(?P<n>\d{{1,3}})\s*-?\s*(?:s|secs?|seconds?)\b(?=\s+(?:long\b|{_NOUN}\b))", re.I),
    re.compile(r"(?<![\w.])(?P<n>\d{2,3})s\b", re.I),  # "65s", by itself
    re.compile(r"\b(?:that'?s\s+|of\s+|in\s+|under\s+|around\s+|about\s+)?(?P<n>\d{2,3})\s*(?:secs?|seconds?)"
               r"\s+long\b", re.I),
)
_MINUTES = re.compile(rf"(?<![\w.])(?P<n>\d(?:\.\d)?|an?|one|two)\s*-?\s*(?:min|mins|minutes?)\b"
                      rf"(?=\s+(?:long\b|{_NOUN}\b))", re.I)
_HANDLE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_][A-Za-z0-9_.]{1,31})")
_CAPTIONS = re.compile(r"\b(?:(?:with\s+)?(?P<style>pop|karaoke|bounce|plain)\s+(?:captions?|subtitles?|subs)|"
                       r"(?P<off>(?:no|without)\s+(?:captions?|subtitles?|subs)))\b", re.I)
_VOICE = re.compile(r"\b(?:with|in|using|use)\s+(?:an?\s+|the\s+)?(?P<v>[\w ]{3,40}?)\s+(?:voice|narrator|narration)\b",
                    re.I)

# What a word says a video is about. Two texts that share a topic are close, also when they share no word:
# "3 dark psychology tricks" and a category called Mind Games.
TOPICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("psychology", ("psychology", "psychological", "mind", "brain", "behavior", "behaviour", "manipulation",
                    "manipulate", "bias", "biases", "persuasion", "habit", "habits", "body language", "narcissist",
                    "emotions", "mental", "subconscious", "gaslighting", "dark psychology")),
    ("history", ("history", "historical", "ancient", "rome", "roman", "romans", "empire", "medieval", "war", "wars",
                 "century", "king", "queen", "pharaoh", "egypt", "viking", "vikings", "civilization", "dynasty",
                 "revolution", "world war", "greek", "greece", "myth", "legend", "archaeology")),
    ("crime", ("crime", "true crime", "murder", "killer", "unsolved", "heist", "detective", "cold case", "missing",
               "serial killer", "scam", "fraud", "mafia", "cartel", "prison")),
    ("horror", ("scary", "horror", "creepy", "paranormal", "haunted", "ghost", "ghosts", "nosleep", "cursed",
                "demon", "urban legend")),
    ("science", ("science", "physics", "chemistry", "biology", "experiment", "space", "universe", "planet",
                 "planets", "nasa", "black hole", "quantum", "evolution", "ocean", "nature", "explained",
                 "engineering", "concrete", "invention")),
    ("gaming", ("gaming", "game", "games", "gamer", "playthrough", "speedrun", "minecraft", "fortnite", "zelda",
                "nintendo", "playstation", "xbox", "esports", "streamer", "clutch", "warzone", "gta")),
    ("sports", ("sports", "sport", "football", "soccer", "basketball", "nba", "nfl", "baseball", "hockey", "ufc",
                "boxing", "tennis", "olympics", "athlete", "goal", "touchdown", "dunk", "championship")),
    ("money", ("money", "finance", "financial", "business", "investing", "invest", "stocks", "crypto", "bitcoin",
               "wealth", "rich", "millionaire", "economy", "startup", "side hustle", "passive income", "budget")),
    ("tech", ("tech", "technology", "ai", "artificial intelligence", "software", "gadget", "gadgets", "iphone",
              "android", "robot", "robots", "computer", "internet", "coding", "programming", "app", "apps")),
    ("selfhelp", ("self-improvement", "self improvement", "motivation", "motivational", "discipline", "stoic",
                  "stoicism", "productivity", "mindset", "confidence", "success", "habits", "wellness",
                  "mindful", "meditation")),
    ("health", ("health", "fitness", "workout", "gym", "diet", "nutrition", "sleep", "muscle", "weight loss",
                "doctor", "medicine", "disease")),
    ("love", ("relationship", "relationships", "dating", "love", "marriage", "breakup", "attraction", "friendship")),
    ("food", ("food", "cooking", "recipe", "recipes", "restaurant", "chef", "baking", "kitchen")),
    ("screen", ("movie", "movies", "film", "films", "tv", "series", "actor", "anime", "marvel", "netflix", "cinema",
                "celebrity", "celebrities")),
    ("music", ("music", "song", "songs", "album", "band", "singer", "rapper", "guitar", "concert")),
    ("animals", ("animal", "animals", "wildlife", "dog", "dogs", "cat", "cats", "shark", "bird", "birds", "insects",
                 "dinosaur", "dinosaurs")),
    ("travel", ("travel", "country", "countries", "city", "cities", "island", "geography", "tourist", "abandoned")),
    ("cars", ("car", "cars", "engine", "supercar", "motorcycle", "racing", "truck", "f1", "formula 1")),
    ("mystery", ("mystery", "mysteries", "conspiracy", "secret", "secrets", "bermuda triangle", "aliens", "ufo",
                 "unexplained", "strange")),
)
# How sure a guess has to be: (the least likeness, the least lead over the runner-up)
SURE = {"words": (0.2, 0.08), "model": (0.35, 0.08)}


@dataclass
class Ask:
    """What a sentence asks for. `kind`: "make" (a video from an idea), "link" (a video from a web page),
    "clip" (clips of a video link) or "" (it isn't clear that a video is asked for at all)."""
    kind: str = ""
    idea: str = ""  # what it's about (for a link: anything said beside it)
    url: str = ""  # the page to retell
    links: list[str] = field(default_factory=list)  # every link in it
    seconds: float = 0.0  # the length asked for (0 = none said)
    handle: str = ""  # "@psychologyfacts", when an account was named
    voice: str = ""  # a voice asked for by name ("with a deep voice")
    captions: str = ""  # a caption style asked for ("karaoke captions", "no captions" = off)
    sure: bool = False  # the words say a video is wanted (else it's only a guess from the link)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d) -> "Ask":
        d = d if isinstance(d, dict) else {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def is_video_link(url: str) -> bool:
    """A link to a video (clip it) rather than to a page of words (retell it)."""
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in VIDEO_HOSTS)


def clamp_seconds(seconds: float) -> float:
    """A length asked for, kept inside what's made here (0 stays 0: Settings decide inside the same range)."""
    if not seconds:
        return 0.0
    return float(max(MIN_SECONDS, min(MAX_SECONDS, round(float(seconds)))))


def _tidy(text: str) -> str:
    text = " ".join(str(text or "").split())
    text = re.sub(r"\s+([,.!?;:])", r"\1", text).strip(" ,;:-–—.").strip()
    while True:  # what a taken-out phrase leaves dangling: "… murders and"
        cut = re.sub(r"[\s,]+(?:and|with|plus|also|but|in|for|to)$", "", text, flags=re.I)
        if cut == text:
            return text
        text = cut.strip(" ,;:-–—.")


def understand(text: str, assume: str = "") -> Ask:
    """Read a sentence. `assume` "make": it came with /make, so it's an idea whatever its words are."""
    from ..studio import retouch

    raw = " ".join(str(text or "").replace("’", "'").split())
    ask = Ask(links=[u.rstrip(").,>]") for u in URL.findall(raw)][:10])
    rest = " ".join(URL.sub(" ", raw).split())
    m = _HANDLE.search(rest)
    if m:
        ask.handle = "@" + m.group(1).rstrip(".").lower()
        rest = (rest[:m.start()].rstrip() + " " + rest[m.end():]).strip()
        rest = re.sub(r"\s*\b(?:for|to|on|at)\s*$", "", rest[:m.start()].rstrip(), flags=re.I) + " " + rest[m.start():]
        rest = " ".join(rest.split())
    for pattern in _SECONDS:
        m = pattern.search(rest)
        if m:
            ask.seconds = float(m.group("n"))
            rest = " ".join((rest[:m.start()] + " " + rest[m.end():]).split())
            break
    else:
        m = _MINUTES.search(rest)
        if m:
            n = m.group("n").lower()
            ask.seconds = 60.0 * (1.0 if n in ("a", "an", "one") else 2.0 if n == "two" else float(n))
            rest = " ".join((rest[:m.start()] + " " + rest[m.end():]).split())
    m = _CAPTIONS.search(rest)
    if m:
        ask.captions = "off" if m.group("off") else m.group("style").lower()
        rest = " ".join((rest[:m.start()] + " " + rest[m.end():]).split())
    m = _VOICE.search(rest)
    if m:
        ask.voice = retouch.voice_named(m.group(0))
        if ask.voice:
            rest = " ".join((rest[:m.start()] + " " + rest[m.end():]).split())
    rest = _WISH.sub("", _HELLO.sub("", rest), count=1).strip()
    pages = [u for u in ask.links if not is_video_link(u)]
    if ask.links and not pages:  # video links: clips of them
        ask.kind, ask.idea = "clip", _tidy(rest)
        return ask
    if pages:
        ask.kind, ask.url = "link", pages[0]
        ask.sure = bool(_WANTS_VIDEO.search(rest)) or assume == "make"
        ask.idea = _tidy(_TURN.sub(" ", rest, count=1))
        if re.fullmatch(r"(?:this|that|it|here|link|article)?", ask.idea, re.I):
            ask.idea = ""
        return ask
    m = _MAKE.match(rest)
    said = bool(m and (m.group("noun") or m.group("about")) and re.match(rf"{_VERB}\b", rest, re.I)) \
        or bool(m and m.group("noun") and m.group("about"))
    if said or assume == "make":
        ask.kind, ask.sure = "make", True
        ask.idea = _tidy(rest[m.end():] if said else rest)
    else:
        ask.idea = _tidy(rest)
    return ask


# --- which category --------------------------------------------------------------------------------------------


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def account_of(cat) -> str:
    """'@psychologyfacts' for a category that posts somewhere, else ''."""
    acc = str(getattr(cat, "account", "") or "").strip()
    return acc if acc.startswith("@") else ""


def named(text: str, cats: list, handle: str = "") -> tuple[object | None, str]:
    """The category a sentence names ("… for History", "… to @psychologyfacts"), and the sentence without
    those words. (None, text) when it names none."""
    if handle:
        want = _plain(handle)
        mine = [c for c in cats if want and want in (_plain(account_of(c)), _plain(c.name))]
        if len(mine) == 1:
            return mine[0], text
        if mine:  # several categories post to that account: the one the sentence names among them, if any
            return named(text, mine)
    for c in sorted(cats, key=lambda c: -len(c.name)):
        name = r"\s+".join(re.escape(w) for w in c.name.split())
        if not name:
            continue
        pattern = (rf"\s*(?:,\s*)?\b(?:for|to|in|on|under)\s+(?:(?:the|my|our)\s+)?{name}"
                   r"(?:\s+(?:category|account|page|channel|niche))?(?=\s*(?:[.!?,;]|$))")
        hits = list(re.finditer(pattern, text, re.I))
        if hits:
            m = hits[-1]
            cut = _tidy(text[:m.start()] + " " + text[m.end():])
            return c, (cut if len(cut.split()) >= 2 or not text[:m.start()].strip() else text)
    return None, text


def describe(cat) -> str:
    """What a category is about, in its own words: what an idea is compared with."""
    handle = re.sub(r"(?<=[a-z])(?=[A-Z])|[_.]+", " ", account_of(cat).lstrip("@"))
    lines = [ln for ln in str(getattr(cat, "creator_topics", "") or "").splitlines()
             if ln.strip() and not ln.strip().startswith("#")][:12]
    bits = [cat.name, cat.name, handle, getattr(cat, "keywords", ""), getattr(cat, "instructions", ""),
            getattr(cat, "finds", ""), getattr(cat, "searches", ""), " ".join(lines)]
    return " . ".join(" ".join(str(b).split()) for b in bits if str(b or "").strip())[:1500]


def topics(text: str) -> list[str]:
    """The TOPICS a text touches."""
    from ..intelligence.embeddings import word_tokens

    low = f" {' '.join(re.findall(r'[a-z0-9]+', str(text or '').lower()))} "
    stems = set(word_tokens(text))
    found = []
    for key, words in TOPICS:
        for w in words:
            if (" " in w or "-" in w) and f" {' '.join(re.findall(r'[a-z0-9]+', w))} " in low:
                found.append(key)
                break
            if " " not in w and "-" not in w and (f" {w} " in low or (len(w) > 3 and word_tokens(w)
                                                                       and word_tokens(w)[0] in stems)):
                found.append(key)
                break
    return found


def classify(idea: str, cats: list, embedder=None) -> tuple[object | None, float, str]:
    """The category an idea is about, when one clearly is: (it, how alike 0-1, "words" or "model").
    (None, 0, how) when none stands out. `embedder`: anything with embed(texts) (tests)."""
    from ..intelligence import embeddings as E

    idea = " ".join(str(idea or "").split())
    if len(idea) < 3 or not cats:
        return None, 0.0, "words"
    texts = [describe(c) for c in cats]
    how = "words"
    if embedder is None:
        try:
            if E.runtime_ok() and E.installed():  # never downloaded for this: a chat message shouldn't wait on it
                embedder, how = E.minilm(fetch=False), "model"
        except Exception:  # noqa: BLE001 - it doesn't load here: words decide
            embedder = None
    elif getattr(embedder, "name", "") != "words":
        how = "model"
    if embedder is None:
        embedder = E.Words(expand=topics).fit(texts + [idea])
    try:
        rows = embedder.embed([idea] + texts)
        sims = [float(x) for x in rows[1:] @ rows[0]]
    except Exception:  # noqa: BLE001 - the model failed on this text: nobody is guessed
        return None, 0.0, how
    order = sorted(range(len(cats)), key=lambda i: -sims[i])
    best = order[0]
    second = sims[order[1]] if len(order) > 1 else 0.0
    least, lead = SURE[how]
    if sims[best] >= least and sims[best] - max(0.0, second) >= lead:
        return cats[best], round(sims[best], 3), how
    return None, round(max(0.0, sims[best]), 3), how


def resolve(ask: Ask, cats: list, embedder=None) -> tuple[object | None, str, str]:
    """(the category, how it was found, the idea without the words that named it). How: "named" (it was
    said), "guessed" (the idea is clearly about it), "unknown" (an account was named that no category posts
    to) or "" (nothing to go on: ask)."""
    cat, idea = named(ask.idea, cats, ask.handle)
    if cat is not None:
        return cat, "named", idea
    if ask.handle:
        want = _plain(ask.handle)
        shared = [c for c in cats if want and want == _plain(account_of(c))]
        if not shared:
            return None, "unknown", idea
        cats = shared  # an account several categories post to: the one among them the idea fits
    if ask.kind == "make" and idea:
        cat, _score, _how = classify(idea, cats, embedder)
        if cat is not None:
            return cat, "guessed", idea
    return None, "", idea


# --- how it's made ---------------------------------------------------------------------------------------------


def build_request(ask: Ask, cat, s):
    """(the request to queue, what to say about how it'll be made). The category's own choices first
    (Categories → Video Creator), then a voice that suits its name; the length inside 62-85 s."""
    from ..creator import voice_engine
    from ..creator.orchestrator import CreateRequest
    from ..creator.visual_generator import BACKENDS
    from ..studio.orchestrator import StudioRequest

    studio = bool(ask.url) or str(getattr(s, "telegram_engine", "studio") or "studio") == "studio"
    seconds = clamp_seconds(ask.seconds)
    voice = ask.voice or (getattr(cat, "creator_voice", "") if cat else "")
    if not voice and cat is not None:
        voice = voice_engine.voice_for(cat.name, getattr(cat, "instructions", ""))
    visuals = getattr(cat, "creator_visuals", "") if cat else ""
    if visuals and not studio and visuals not in BACKENDS:
        visuals = ""
    captions = ask.captions or (getattr(cat, "creator_captions", "") if cat else "")
    common = dict(prompt=ask.idea, category=cat.id if cat else "", voice=voice, seconds=seconds, rewards=True,
                  visuals=visuals, style=getattr(cat, "creator_style", "") if cat else "")
    if studio:
        req = StudioRequest(**common, url=ask.url, captions=captions, format="short", orientation="vertical")
    else:
        req = CreateRequest(**common)
    said = []
    if cat is not None and account_of(cat):
        said.append(f"posts to {account_of(cat)}")
    if ask.seconds and seconds != round(ask.seconds):
        said.append(f"{seconds:.0f} s (you said {ask.seconds:.0f}: it's kept between {MIN_SECONDS:.0f} and "
                    f"{MAX_SECONDS:.0f} s, since Creator Rewards needs over a minute)")
    else:
        said.append(f"{seconds:.0f} s" if seconds else f"{MIN_SECONDS:.0f}–{MAX_SECONDS:.0f} s")
    said.append("upright 9:16")
    if voice and voice in voice_engine.VOICES:
        said.append(voice_engine.VOICES[voice].label + " voice")
    if visuals and visuals != "auto":
        said.append({"stock": "stock footage", "local_broll": "your B-roll", "texture": "built-in textures"}
                    .get(visuals, visuals.replace("_", " ")) + " pictures")
    if studio and captions:
        said.append("no captions" if captions == "off" else f"{captions} captions")
    return req, said


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def parse_request(text: str, cats: list) -> dict:
    """'/request 10 30-45s Gaming funny reactions only' → {count, length, category, links, notes}.
    `length` is None when none was given (the category's own is used), `category` a categories.Category
    or None. ValueError for a length it can't read."""
    from .. import orders

    links = URL.findall(text)
    rest = URL.sub(" ", text)
    length = None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)?\b", rest, re.I) or \
        re.search(r"\b(\d+(?:\.\d+)?)\s*(?:s|secs?|seconds?)\b", rest, re.I)
    if m:
        length = orders.parse_length(m.group(0))
        rest = rest[: m.start()] + " " + rest[m.end():]
    count = 0
    m = re.search(r"\b(\d{1,3})\b", rest)
    if m:
        count = int(m.group(1))
        rest = rest[: m.start()] + " " + rest[m.end():]
    category = None
    low = f" {rest.lower()} "
    for c in sorted(cats, key=lambda c: -len(c.name)):
        name = c.name.strip().lower()
        if not name:
            continue
        hit = re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", low)
        if hit:
            category = c
            low = low[: hit.start()] + " " + low[hit.end():]
            rest = re.sub(rf"(?<![\w]){re.escape(c.name.strip())}(?![\w])", " ", rest, count=1, flags=re.I)
            break
    words = rest.split()
    while words and words[0].lower().strip(",.:;") in ("clips", "clip", "of", "for", "from", "with", "about", "-"):
        words.pop(0)
    notes = " ".join(words).strip(" ,.;:-")
    return {"count": count or 5, "length": length, "category": category, "links": links, "notes": notes}


class StudioHandler:
    """The bot's asking side (mixed into bot.Bot)."""

    # --- a video from an idea, or from a link ------------------------------------------------------------------

    def on_text(self, chat: int, text: str) -> bool:
        """A message that's no command. True when it was taken as a request for a video (and answered)."""
        ask = understand(text)
        if ask.kind == "clip":
            self.cmd_clip(chat, text)
            return True
        if ask.kind == "link" and not ask.sure:  # a bare link to a page: it could be either
            token = self._ask("link", chat, ask=ask.to_json(), text=text)
            self.say(chat, f"🔗 A link to {esc(host_of(ask.url) or 'a page')}. What should I do with it?", inline([
                [("📰 Make a video from the article", f"lk:{token}:read")],
                [("✂️ Clip the video on that page", f"lk:{token}:clip")],
                [("✖ Nothing", f"x:{token}")]]))
            return True
        if ask.kind in ("make", "link"):
            self.ask_make(chat, ask)
            return True
        return False

    def cmd_make(self, chat: int, idea: str = "") -> None:
        ask = understand(idea, assume="make")
        if ask.kind == "clip":  # /make with a video link: there's nothing to make a video *about*
            self.say(chat, "That's a link to a video. /clip cuts clips from it; /make takes an idea, or a link to "
                           "an article.")
            return
        self.ask_make(chat, ask)

    def ask_make(self, chat: int, ask: Ask) -> None:
        """Queue what `ask` asks for, once it's known which category it's for: named, guessed, or picked."""
        if ask.kind == "make" and not ask.idea:
            self.modes[chat] = ("make", self.now() + MODE_LIFE)
            self.say(chat, "What should the video be about? Send the idea, like <i>the real story of the "
                           "Bermuda Triangle</i>, or a link to an article.")
            return
        if len(ask.idea) > IDEA_MAX:
            self.say(chat, f"⚠ That's too long for an idea: keep it under {IDEA_MAX:,} characters.")
            return
        cats = self._categories()
        if not cats:
            self.make(chat, ask.idea, None, ask)
            return
        cat, how, idea = resolve(ask, cats)
        ask.idea = idea
        if cat is None:
            token = self._ask("make", chat, idea=ask.idea, ask=ask.to_json(), cats=[c.id for c in cats[:24]])
            what = (f"A video from {esc(host_of(ask.url) or 'that page')}" if ask.url else
                    f"A video about “{esc(trim(ask.idea, 200))}”")
            unknown = (f" No category posts to {esc(ask.handle)}." if how == "unknown" else "")
            self.say(chat, f"✨ {what}.{unknown} Which category is it for?",
                     self._category_buttons("mk", token, cats))
            return
        self.make(chat, ask.idea, cat, ask, how)

    def make(self, chat: int, idea: str, cat, ask: Ask | None = None, how: str = "") -> str:
        job_id, text = self.queue_make(chat, idea, cat, ask, how)
        rows = []
        if how == "guessed":  # it was the bot's reading of the idea: one tap puts it right
            rows.append([(trim(f"↔ Not {cat.name}? Change it", 40), f"rc:{job_id}")])
        self.say(chat, text, inline(rows) if rows else None)
        return text

    def queue_make(self, chat: int, idea: str, cat, ask: Ask | None = None, how: str = "") -> tuple[str, str]:
        """Queue a video `chat` asked for: (the job's id, what to tell them, as HTML)."""
        from ..creator.orchestrator import queue as queue_creation

        ask = ask or Ask(kind="make", idea=idea, sure=True)
        ask.idea = idea
        s = self.settings()
        db = self.runner.db
        req, said = build_request(ask, cat, s)
        job_id = queue_creation(db, req, cat.id if cat else "", self.now(), origins=["creator", "telegram"])
        self._remember(f"job:{job_id}", chat, req.label(), "make")
        self._keep_ask(job_id, ask)
        db.set_meta("nudge", "1")
        what = (f"a video from {host_of(ask.url) or 'a page'}" if ask.url else f"a video about “{trim(idea, 80)}”")
        self.log(f"{self.name(chat)} asked for {what}" + (f" for {cat.name}" if cat else "") + ".")
        text = self.made_text(ask, cat, how, said)
        self.did(chat, "make", idea or ask.url)
        return job_id, text

    def made_text(self, ask: Ask, cat, how: str, said: list[str]) -> str:
        """What's said once a video is queued: what, for which category (and how that was found), when, how."""
        what = (f"a video from the article at {esc(host_of(ask.url) or 'that link')}"
                + (f" (“{esc(trim(ask.idea, 120))}”)" if ask.idea else "") if ask.url else
                f"a video about “{esc(trim(ask.idea, 200))}”")
        text = (f"{'🔗' if ask.url else '✨'} Got it: {what}" + (f" for {esc(cat.name)}" if cat else "")
                + f". {self._when(cat)} I'll send it here when it's made" + (", for your OK before it posts."
                                                                             if self._held(cat) else "."))
        if how == "guessed":
            text += f"\n🧭 It reads like one for <b>{esc(cat.name)}</b>, so that's where I put it."
        if said:
            text += "\n🎛 " + esc(" · ".join(said))
        return text

    def _keep_ask(self, job_id: str, ask: Ask) -> None:
        asks = self.__dict__.setdefault("asks", {})
        asks[job_id] = ask.to_json()
        for old in list(asks)[:-ASKS_KEPT]:
            del asks[old]

    def press_recat(self, chat, mid, arg, video) -> str:
        """↔ under a video whose category the bot guessed: offer the categories again, while it still waits."""
        job = self.runner.db.get(arg)
        if job is None or job.status != "queued":
            try:
                self.api.edit_buttons(chat, mid, None)
            except ApiError:
                pass
            return "It's being made already (or done), so it stays where it is"
        cats = self._categories()
        token = self._ask("recat", chat, job=job.id, cats=[c.id for c in cats[:24]], mid=mid)
        self.say(chat, f"Which category is “{esc(trim(job.title, 100))}” for?", self._category_buttons("rcp", token, cats))
        return ""

    def press_recat_pick(self, chat, mid, arg, video) -> str:
        from ..studio.orchestrator import read_request

        token, _, choice = arg.partition(":")
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired"
        db = self.runner.db
        job = db.get(p["job"])
        if job is None or job.status != "queued":
            self._settle_question(chat, mid, "")
            return "It's being made already (or done), so it stays where it is"
        kept = self.__dict__.get("asks", {}).get(job.id)
        if kept:
            ask = Ask.from_json(kept)
        else:  # asked before the bot last started: what the queued request itself says
            old = read_request(job.request)
            ask = Ask(kind="link" if getattr(old, "url", "") else "make", idea=old.prompt,
                      url=getattr(old, "url", ""), seconds=float(old.seconds or 0), sure=True)
        req, said = build_request(ask, cat, self.settings())
        db.update(job.id, self.now(), category_id=cat.id if cat else "", request=req.to_json())
        self.log(f"{self.name(chat)} moved “{trim(job.title, 80)}” to {cat.name if cat else 'no category'}.")
        text = self.made_text(ask, cat, "", said)
        self._settle_question(chat, mid, "")
        try:
            self.api.edit(chat, int(p.get("mid") or 0), text, None)
        except ApiError:
            self.say(chat, text)
        return f"Moved to {cat.name}" if cat else "No category"

    def press_link(self, chat, mid, arg, video) -> str:
        """What to do with a bare link to a page: retell the article, or clip the video on it."""
        token, _, what = arg.partition(":")
        p = self._take(token, chat)
        if p is None:
            return "That question has expired: send the link again"
        self._settle_question(chat, mid, "")
        if what == "clip":
            self.cmd_clip(chat, p["text"])
        else:
            ask = Ask.from_json(p["ask"])
            ask.sure = True
            self.ask_make(chat, ask)
        return ""

    def _held(self, cat) -> bool:
        """Will the video wait for your OK (Settings → Creator → Check videos before they post)?"""
        from ..posting.intake import REVIEW_DAYS

        mode = getattr(self.settings(), "creator_review", "new")
        if mode in ("always", "off"):
            return mode == "always"
        if cat is None or not cat.account_id:
            return False
        store = self.posting()[0]
        with store.lock:
            first = min((i.posted_at or 0 for i in store.items.values() if i.account == cat.account_id
                         and i.ai_generated and i.status == "posted" and i.posted_at), default=0)
        return not first or self.now() - first < REVIEW_DAYS * 86400

    def _when(self, cat=None, mine: int = 1) -> str:
        """When what was just queued (`mine` jobs) will start, roughly."""
        from ..pipeline import hours_wait

        s = self.settings()
        db = self.runner.db
        now = self.now()
        ahead = max(0, sum(1 for j in db.jobs(limit=3000) if j.status == "working"
                           or (j.status == "queued" and j.next_try <= now)) - mine)
        if cat is not None and cat.paused:
            return f"{esc(cat.name)} is paused, so it waits until you resume it (Automation → Categories)."
        if cat is not None and cat.needs_account:
            return (f"{esc(cat.name)} has no TikTok account yet, so it waits until you pick one (Categories tab on "
                    "the PC).")
        if db.meta("paused") == "1":
            return "Automation is paused: it starts once you /resume."
        wait = hours_wait(now, s.auto_start_hour, s.auto_end_hour)
        if wait:
            return f"It starts at {s.auto_start_hour}:00, when working hours begin."
        return "It's next." if not ahead else f"{plural(ahead, 'video is', 'videos are')} ahead of it."

    def cmd_clip(self, chat: int, text: str = "") -> None:
        links = URL.findall(text or "")
        if not links:
            self.modes[chat] = ("clip", self.now() + MODE_LIFE)
            self.say(chat, "Send the YouTube link (or several, one after another in one message).")
            return
        cats = self._categories()
        if not cats:
            self.clip(chat, links, None)
            return
        said = understand(text)
        cat, _rest = named(said.idea, cats, said.handle)
        if cat is not None:  # "clip https://… for Gaming"
            self.clip(chat, links[:10], cat)
            return
        token = self._ask("clip", chat, links=links[:10], cats=[c.id for c in cats[:24]])
        self.say(chat, f"✂️ Clip {plural(len(links[:10]), 'video')}. Which category are the clips for?",
                 self._category_buttons("cl", token, cats))

    def clip(self, chat: int, links: list[str], cat) -> str:
        from ..download import is_url
        from ..pipeline import _video_id

        added, before, bad = [], [], []
        for url in links:
            url = url.rstrip(").,>]")
            if not is_url(url):
                bad.append(url)
                continue
            try:
                if self.runner.add_manual(url, category_id=cat.id if cat else ""):
                    added.append(url)
                    self._remember(f"job:{_video_id(url)}", chat, url, "clip")
                else:
                    before.append(url)
            except ValueError:
                bad.append(url)
        if added:
            self.runner.db.set_meta("nudge", "1")
            self.log(f"{self.name(chat)} asked to clip {plural(len(added), 'video')}"
                     + (f" for {cat.name}" if cat else "") + ".")
            self.did(chat, "clip", plural(len(added), "video") + (f" for {cat.name}" if cat else ""))
        lines = []
        if added:
            lines.append(f"✂️ Queued {plural(len(added), 'video')} to clip" + (f" for {esc(cat.name)}" if cat else "")
                         + f". {self._when(cat, len(added))} I'll send the clips here when they're made.")
        if before:
            lines.append(f"{plural(len(before), 'video was', 'videos were')} clipped before (or already waiting), so "
                         "not queued again. To clip one again: /request 3 " + esc(before[0]))
        if bad:
            lines.append("These aren't video links: " + esc(", ".join(trim(b, 60) for b in bad[:3])))
        text = "\n".join(lines) or "Nothing to clip."
        self.say(chat, text)
        return text

    def cmd_request(self, chat: int, text: str = "") -> None:
        if not text.strip():
            self.modes[chat] = ("request", self.now() + MODE_LIFE)
            self.say(chat, "Say what you'd like, like <i>10 clips 30-45s Gaming, only clutch plays</i>: how many, "
                           "how long, the category, links if you have them, and anything special for the AI.")
            return
        cats = self._categories()
        try:
            want = parse_request(text, cats)
        except ValueError as exc:
            self.say(chat, f"⚠ {esc(exc)}")
            return
        if want["category"] is None and cats:
            token = self._ask("request", chat, want={**want, "category": None}, cats=[c.id for c in cats[:24]])
            self.say(chat, f"📋 {self._request_line(want)}. Which category are they for?",
                     self._category_buttons("rq", token, cats))
            return
        self.request(chat, want, want["category"])

    def _request_line(self, want: dict, cat=None) -> str:
        n = want["count"]
        length = want["length"]
        where = f" for {esc(cat.name)}" if cat else ""
        text = f"{plural(n, 'clip')}" + (f" of {length[0]:g}–{length[1]:g} s" if length else "") + where
        if want["links"]:
            text += f" from {plural(len(want['links']), 'link')}"
        if want["notes"]:
            text += f": “{esc(trim(want['notes'], 160))}”"
        return text

    def request(self, chat: int, want: dict, cat) -> str:
        from .. import orders, pipeline
        from ..jobs import Job
        from ..research import youtube_key

        s = self.settings()
        length = want["length"]
        if length is None:
            style = pipeline.settings_for(Job(id="", url="", category_id=cat.id if cat else ""), s)[0]
            length = (style.min_len, style.max_len)
        if not want["links"] and not youtube_key(s):
            text = ("⚠ To pick videos by itself, automation needs a YouTube Data API key (Settings → Research). "
                    "Send links with the request instead.")
            self.say(chat, text)
            return text
        try:
            order = orders.make(want["count"], length[0], length[1], category_id=cat.id if cat else "",
                                links=want["links"], notes=want["notes"], now=self.now())
        except ValueError as exc:
            text = f"⚠ {esc(exc)}"
            self.say(chat, text)
            return text
        order = self.runner.place(order)
        self._remember(f"order:{order.id}", chat, order.title(cat.name if cat else ""), "request")
        self.did(chat, "request", order.title(cat.name if cat else ""))
        lines = [f"📋 Asked for {self._request_line({**want, 'length': length}, cat)}. I'll tell you when they're "
                 "ready."]
        if want["notes"] and not pipeline.has_ai_key(s):
            lines.append("⚠ Special requests need a Claude or Gemini key (Settings → AI): without one, smart signals "
                         "pick the clips and the special requests aren't used.")
        if cat is not None and cat.paused:
            lines.append(f"⚠ {esc(cat.name)} is paused, so nothing happens until you resume it.")
        text = "\n".join(lines)
        self.say(chat, text, inline([[("📋 Requests", "reqs")]]))
        return text

    def cmd_requests(self, chat: int, arg: str = "") -> None:
        text, buttons = self.requests_html()
        self.say(chat, text, buttons)

    def requests_html(self) -> tuple[str, dict]:
        from .. import categories, orders

        db = self.runner.db
        s = self.settings()
        mine = db.orders()[:8]
        if not mine:
            return ("No requests yet. Ask for clips like <i>/request 10 30-45s Gaming funny reactions</i>, or send a "
                    "link to clip it.", inline([[("➕ New request", "rq:new:-")]]))
        lines = ["<b>📋 Your requests</b>"]
        rows = []
        for o in mine:
            name = categories.name_of(s, o.category_id)
            mark = {"open": "⏳", "done": "✅", "short": "⚠", "cancelled": "✖"}.get(o.status, "•")
            lines.append(f"{mark} {esc(o.title(name))}: {esc(orders.STATUS_WORDS.get(o.status, o.status))}"
                         + (f". {esc(trim(o.message, 120))}" if o.message else "")
                         + (f"\n    “{esc(trim(o.notes, 100))}”" if o.notes else ""))
            if o.status == "open":
                rows.append([(f"✖ Cancel: {trim(o.title(name), 40)}", f"cancel:{o.id}")])
        rows.append([("➕ New request", "rq:new:-")])
        return "\n".join(lines), inline(rows)

    def press_make(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send /make again"
        ask = Ask.from_json(p["ask"]) if p.get("ask") else None
        text = self.make(chat, p["idea"], cat, ask)
        self._settle_question(chat, mid, text)
        return ""

    def press_clip(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send the link again"
        text = self.clip(chat, p["links"], cat)
        self._settle_question(chat, mid, text)
        return ""

    def press_request(self, chat, mid, arg, video) -> str:
        token, _, choice = arg.partition(":")
        if token == "new":
            self.cmd_request(chat, "")
            return ""
        p, cat = self._picked(token, chat, choice)
        if p is None:
            return "That question has expired: send the request again"
        text = self.request(chat, p["want"], cat)
        self._settle_question(chat, mid, text)
        return ""

    def press_idea(self, chat, mid, arg, video) -> str:
        token, _, what = arg.partition(":")
        p = self._take(token, chat)
        if p is None:
            return "That question has expired: send it again"
        try:
            self.api.edit_buttons(chat, mid, None)
        except ApiError:
            pass
        if what == "make":
            self.cmd_make(chat, p["text"])
        else:
            self.cmd_request(chat, p["text"])
        return ""

    def press_requests(self, chat, mid, arg, video) -> str:
        self.cmd_requests(chat)
        return ""

    def press_cancel(self, chat, mid, arg, video) -> str:
        o = self.runner.db.get_order(arg)
        if o is None or o.status != "open":
            return "It isn't open any more"
        buttons = inline([[("✖ Yes, cancel it", f"CANCEL:{o.id}"), ("↩ Keep it", "x:-")]])
        self.say(chat, f"Cancel the request for {esc(o.title())}? Its videos still waiting leave the queue (one "
                       "being clipped finishes).", buttons)
        return ""

    def press_cancelled(self, chat, mid, arg, video) -> str:
        o = self.runner.db.get_order(arg)
        if o is None or o.status != "open":
            return "It isn't open any more"
        n = self.runner.cancel(o.id)
        with self.lock, state.edit(self.state_file) as data:
            data["asked"].pop(f"order:{o.id}", None)
        self.log(f"{self.name(chat)} cancelled a request from Telegram ({o.title()}).")
        self.did(chat, "cancel", o.title())
        try:
            self.api.edit(chat, mid, f"✖ Cancelled. {plural(n, 'waiting video')} left the queue.")
        except ApiError:
            pass
        return "Cancelled"

    def press_job(self, chat, mid, arg, video) -> str:
        items = self._items_of_job(arg)
        if not items:
            return "Those clips aren't in the library any more"
        text, buttons = digest_html("✂️ The clips", items, self.posting()[1], self.now(), self.app_url)
        self.say(chat, text, buttons)
        return ""

    def app_make(self, chat: int, idea: str, category_id: str = "", seconds=0) -> dict:
        """The Mini App's "make a video": queued like /make, answered in the app. `idea` is read the way a
        message is (an idea, or a link to an article; a length, voice or caption style said in it count), and
        `category_id` "auto" finds the category from it. Raises Refused: with status 422 when the category
        couldn't be told and has to be picked."""
        if len(str(idea or "")) > IDEA_MAX + 300:
            raise Refused(f"That's too long: keep it under {IDEA_MAX:,} characters.", 400)
        ask = understand(str(idea or ""), assume="make")
        if ask.kind == "clip":
            raise Refused("That's a link to a video. Send it to the bot in the chat to clip it; here, give an idea "
                          "or a link to an article.", 400)
        if not ask.idea and not ask.url:
            raise Refused("Say what the video should be about.", 400)
        if len(ask.idea) > IDEA_MAX:
            raise Refused(f"That idea is too long: keep it under {IDEA_MAX:,} characters.", 400)
        try:
            if seconds:
                ask.seconds = float(seconds)
        except (TypeError, ValueError):
            raise Refused("That length couldn't be read: give it in seconds.", 400) from None
        cats = self._categories()
        cat, how = None, ""
        if category_id and category_id != "auto":
            cat = next((c for c in cats if c.id == category_id), None)
            if cat is None:
                raise Refused("That category isn't there any more.", 400)
            ask.idea = named(ask.idea, [cat], ask.handle)[1]  # "… for History", with History picked: said twice
        elif cats:
            found, how, rest = resolve(ask, cats)
            if found is not None and (category_id == "auto" or how == "named"):
                cat, ask.idea = found, rest
            else:
                how = ""
            if cat is None and category_id == "auto":
                raise Refused("I couldn't tell which category that's for. Pick one.", 422)
        if ask.kind == "make" and not ask.idea:
            raise Refused("Say what the video should be about.", 400)
        _req, said = build_request(ask, cat, self.settings())
        job_id, _text = self.queue_make(chat, ask.idea, cat, ask, how)
        what = (f"a video from the article at {host_of(ask.url) or 'that link'}" if ask.url else
                f"a video about “{trim(ask.idea, 120)}”")
        return {"job": job_id, "held": self._held(cat), "category": cat.name if cat else "",
                "category_id": cat.id if cat else "", "how": how, "made": said,
                "message": f"Got it: {what}" + (f" for {cat.name}" if cat else "") + ". "
                           + html.unescape(self._when(cat)) + " It shows up under Videos when it's made"
                           + (", and waits for an OK before it posts." if self._held(cat) else ".")
                           + (f" It reads like one for {cat.name}, so that's where it went." if how == "guessed"
                              else "")}
