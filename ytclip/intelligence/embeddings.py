"""Sentences as vectors, on this computer: what the sports event search compares a question with.

Two ways to make them, the same to whoever asks (`embed(texts)` gives one unit-length row per text, so the dot
product of two rows is their cosine similarity):

* **MiniLM** - all-MiniLM-L6-v2, the small sentence-transformers model (22 M weights, 384 numbers a sentence),
  run through onnxruntime: Core ML on a Mac, DirectML or CUDA on Windows when that onnxruntime is installed,
  else the CPU (hardware.onnx_providers; a 15-second window of commentary takes about a millisecond on a CPU,
  so the card hardly matters). It knows that "sent off" and "shown a straight red" mean the same thing without
  being told. The model (90 MB) and its word list download once from Hugging Face to
  %LOCALAPPDATA%\\ClipMonster\\models\\embeddings\\all-MiniLM-L6-v2. Its word-piece tokenizer is written out
  here (BERT's, lower-cased), so nothing but onnxruntime and numpy is needed: no PyTorch, no transformers.
* **Words** - no model at all: each text becomes a bag of its word stems and word pairs (hashed into 2,048
  slots, rare words weighted up), plus whatever `expand` adds (the event search adds a tag for every sports
  idea a phrase means, so "sent off" and "red card" still meet). Always available, and what the tests use.

`get(s)` picks: the sentence model when onnxruntime is installed and the model is here or can be downloaded,
else words (Settings → Sports → Event search reads with).
"""

from __future__ import annotations

import hashlib
import http.client
import math
import re
import threading
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

import numpy as np

HF = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/"
MODEL_NAME = "all-MiniLM-L6-v2"
FILES = {"model.onnx": ("onnx/model.onnx", 80_000_000), "vocab.txt": ("vocab.txt", 200_000)}  # name: (path, least size)
MAX_TOKENS = 256  # the model's own limit
BATCH = 32
WORD_SLOTS = 2048

Progress = Callable[[str, "float | None"], None]
Log = Callable[[str], None]


class EmbeddingError(RuntimeError):
    """A plain-English reason the sentence model can't be used."""


# ---------------------------------------------------------------- BERT's tokenizer


def _is_punct(ch: str) -> bool:
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


def basic_tokens(text: str) -> list[str]:
    """Lower-cased words and punctuation marks, accents removed (BERT's "basic tokenizer", uncased)."""
    text = unicodedata.normalize("NFD", text.lower())
    out: list[str] = []
    word: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Mn":  # an accent
            continue
        if ch.isspace() or cat in ("Cc", "Cf") or ch == "\x00" or ch == "�":
            if word:
                out.append("".join(word))
                word = []
        elif _is_punct(ch) or 0x4E00 <= ord(ch) <= 0x9FFF:
            if word:
                out.append("".join(word))
                word = []
            out.append(ch)
        else:
            word.append(ch)
    if word:
        out.append("".join(word))
    return out


class WordPiece:
    """Text to the model's token ids: words split into the longest pieces its vocabulary has."""

    def __init__(self, vocab: dict[str, int]):
        self.vocab = vocab
        try:
            self.cls, self.sep, self.pad, self.unk = (vocab[t] for t in ("[CLS]", "[SEP]", "[PAD]", "[UNK]"))
        except KeyError as exc:
            raise EmbeddingError(f"The sentence model's word list is missing {exc}.") from exc

    @classmethod
    def load(cls, path: Path) -> "WordPiece":
        try:
            lines = Path(path).read_text(encoding="utf-8").split("\n")
        except OSError as exc:
            raise EmbeddingError(f"The sentence model's word list couldn't be read ({exc}).") from exc
        return cls({tok.rstrip("\r"): i for i, tok in enumerate(lines) if tok.rstrip("\r")})

    def pieces(self, word: str) -> list[int]:
        if len(word) > 100:
            return [self.unk]
        out, start = [], 0
        while start < len(word):
            end = len(word)
            found = None
            while end > start:
                piece = ("##" if start else "") + word[start:end]
                if piece in self.vocab:
                    found = self.vocab[piece]
                    break
                end -= 1
            if found is None:
                return [self.unk]
            out.append(found)
            start = end
        return out

    def encode(self, text: str, limit: int = MAX_TOKENS) -> list[int]:
        ids: list[int] = []
        for word in basic_tokens(text):
            ids += self.pieces(word)
            if len(ids) >= limit - 2:
                break
        return [self.cls] + ids[:limit - 2] + [self.sep]


# ---------------------------------------------------------------- the sentence model


def folder() -> Path:
    from ..settings import models_dir

    return models_dir() / "embeddings" / MODEL_NAME


def installed(where: Path | None = None) -> bool:
    where = where or folder()
    return all((where / name).is_file() and (where / name).stat().st_size >= least
               for name, (_p, least) in FILES.items())


def runtime_ok() -> bool:
    """onnxruntime is installed and loads."""
    try:
        import onnxruntime  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - not installed, or a DLL it needs is missing
        return False


def download(progress: Progress | None = None, opener=urllib.request.urlopen, where: Path | None = None) -> Path:
    """The model's two files, fetched when they aren't here. A stopped download carries on next time."""
    where = where or folder()
    where.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda m, f: None)
    label = "Downloading the event search's sentence model (once, 90 MB)"
    for name, (remote, least) in FILES.items():
        path = where / name
        if path.is_file() and path.stat().st_size >= least:
            continue
        part = path.with_name(name + ".part")
        have = part.stat().st_size if part.is_file() else 0
        req = urllib.request.Request(HF + remote, headers={"User-Agent": "ClipMonster"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        progress(label, None)
        try:
            r = opener(req, timeout=60)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have >= least:
                part.replace(path)
                continue
            raise EmbeddingError(f"Hugging Face answered {exc.code} for the sentence model.") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EmbeddingError(f"Couldn't reach Hugging Face for the sentence model ({exc}).") from exc
        with r:
            if have and getattr(r, "status", 200) != 206:
                have = 0
            total = have + int(r.headers.get("Content-Length") or 0)
            done = have
            try:
                with open(part, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 18):
                        f.write(chunk)
                        done += len(chunk)
                        if name == "model.onnx":
                            progress(label, done / total if total else None)
            except (OSError, http.client.HTTPException) as exc:
                raise EmbeddingError(f"The sentence model's download stopped ({exc}). It carries on next "
                                     "time.") from exc
        if part.stat().st_size < least:
            part.unlink(missing_ok=True)
            raise EmbeddingError("The sentence model's download came back too small. Try again.")
        part.replace(path)
    return where


class MiniLM:
    """all-MiniLM-L6-v2 through onnxruntime."""

    name = "minilm"
    label = "all-MiniLM-L6-v2"

    def __init__(self, where: Path | None = None, providers: list[str] | None = None):
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001 - see runtime_ok
            raise EmbeddingError("The sentence model needs onnxruntime (pip install onnxruntime).") from exc
        from . import hardware

        where = where or folder()
        self.tokenizer = WordPiece.load(where / "vocab.txt")
        wanted = providers or hardware.onnx_providers()
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = None
        problem: Exception | None = None
        # The best provider this computer has; if it can't take the model (a driver, an operator), the CPU can.
        for attempt in (wanted, [hardware.CPU_PROVIDER]):
            try:
                self.session = ort.InferenceSession(str(where / "model.onnx"), sess_options=opts,
                                                    providers=list(attempt))
                break
            except Exception as exc:  # noqa: BLE001 - onnxruntime raises its own types, and plain RuntimeError
                problem = exc
        if self.session is None:
            raise EmbeddingError(f"The sentence model couldn't be loaded ({problem}).")
        self.provider = self.session.get_providers()[0]
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.device = hardware.PROVIDER_NAMES.get(self.provider, self.provider)
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 384), np.float32)
        rows = []
        for i in range(0, len(texts), BATCH):
            ids = [self.tokenizer.encode(t or "") for t in texts[i:i + BATCH]]
            width = max(len(x) for x in ids)
            tokens = np.full((len(ids), width), self.tokenizer.pad, np.int64)
            mask = np.zeros((len(ids), width), np.int64)
            for r, x in enumerate(ids):
                tokens[r, :len(x)] = x
                mask[r, :len(x)] = 1
            feed = {"input_ids": tokens, "attention_mask": mask, "token_type_ids": np.zeros_like(tokens)}
            with self._lock:
                out = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0]
            hidden = np.asarray(out, np.float32)
            if hidden.ndim == 3:  # one vector per token: average the real ones
                m = mask[:, :, None].astype(np.float32)
                hidden = (hidden * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
            rows.append(hidden)
        return unit(np.concatenate(rows, axis=0))


# ---------------------------------------------------------------- words, no model

_STOP = frozenset("a an the and or but of to in on at by for with from as is are was were be been being it its this "
                  "that these those there here he she they them his her their we you i me my our your so then than "
                  "very just now well oh uh um yeah got get gets going go goes gone have has had do does did will "
                  "would can could not no yes if up down out over into about again also still really one two".split())


def stem(word: str) -> str:
    """A light stem: 'penalties' and 'penalty', 'ejected' and 'ejection' meet without a dictionary."""
    w = word
    if w.endswith("ss"):
        return w
    for suffix, keep in (("ies", "y"), ("ing", ""), ("tion", "t"), ("ions", ""), ("ion", ""), ("ed", ""),
                         ("es", ""), ("s", "")):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[:len(w) - len(suffix)] + keep
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":  # "tripp" -> "trip"
        w = w[:-1]
    return w


def word_tokens(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", unicodedata.normalize("NFKD", text.lower()))
            if w not in _STOP and len(w) > 1]


def _slot(token: str) -> tuple[int, float]:
    h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")
    return h % WORD_SLOTS, 1.0 if (h >> 40) & 1 else -1.0


class Words:
    """Bags of word stems and pairs, hashed; `expand(text)` may add tags (they count three words each)."""

    name = "words"
    label = "word matching"
    device = "CPU"
    TAG_WEIGHT = 3.0

    def __init__(self, expand: Callable[[str], list[str]] | None = None):
        self.expand = expand
        self.idf: dict[str, float] = {}

    def fit(self, corpus: list[str]) -> "Words":
        """Learn which words are rare in this video's commentary (they say more than the common ones)."""
        n = max(1, len(corpus))
        seen: dict[str, int] = {}
        for text in corpus:
            for tok in set(word_tokens(text)):
                seen[tok] = seen.get(tok, 0) + 1
        self.idf = {tok: math.log(1 + n / (1 + c)) for tok, c in seen.items()}
        return self

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), WORD_SLOTS), np.float32)
        default = math.log(1 + max(1, len(self.idf))) if self.idf else 1.0
        for r, text in enumerate(texts):
            toks = word_tokens(text or "")
            counts: dict[str, float] = {}
            for tok in toks:
                counts[tok] = counts.get(tok, 0.0) + self.idf.get(tok, default)
            for a, b in zip(toks, toks[1:]):
                counts[f"{a}_{b}"] = counts.get(f"{a}_{b}", 0.0) + 0.5 * default
            for tag in (self.expand(text or "") if self.expand else ()):
                counts[f"#{tag}"] = self.TAG_WEIGHT ** 2 * default  # once, however often it's said
            for tok, weight in counts.items():
                i, sign = _slot(tok)
                out[r, i] += sign * math.sqrt(weight)
        return unit(out)


def unit(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return (rows / np.maximum(norms, 1e-9)).astype(np.float32)


# ---------------------------------------------------------------- which one

_loaded: dict[str, MiniLM] = {}
_load_lock = threading.Lock()


def minilm(progress: Progress | None = None, fetch: bool = True) -> MiniLM:
    """The sentence model, loaded once a run (downloaded first when `fetch` and it isn't here)."""
    with _load_lock:
        where = folder()
        key = str(where)
        if key not in _loaded:
            if not runtime_ok():
                raise EmbeddingError("The sentence model needs onnxruntime, which isn't installed here.")
            if not installed(where):
                if not fetch:
                    raise EmbeddingError("The sentence model isn't downloaded yet.")
                download(progress, where=where)
            _loaded[key] = MiniLM(where)
        return _loaded[key]


def forget() -> None:
    with _load_lock:
        _loaded.clear()


def get(s=None, *, expand: Callable[[str], list[str]] | None = None, log: Log | None = None,
        progress: Progress | None = None):
    """The embedder Settings ask for: "minilm" (raises EmbeddingError when it can't be had), "words", or
    "auto" (the sentence model when it can be had, else words, saying why)."""
    want = str(getattr(s, "event_search_model", "auto") or "auto")
    if want == "words":
        return Words(expand)
    try:
        return minilm(progress)
    except EmbeddingError as exc:
        if want == "minilm":
            raise
        if log:
            log(f"{exc} Searching by words instead.")
        return Words(expand)
