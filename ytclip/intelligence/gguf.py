"""A GGUF model file's header: how the model is built, read without loading it.

GGUF (llama.cpp's model format) opens with metadata (the architecture, how many layers, how wide they
are, the tokenizer) and a table of every tensor: its name, shape, type and where its data starts. That's
all the local AI needs to know how much memory each layer's weights take and how much the model's memory
of the conversation (the KV cache) takes per token, so llama_engine can put as many layers on the
graphics card as fit and the rest in RAM.

Only the header is read: a few MB even for a 20 GB model, never the weights. Each tensor's size comes
from where the next one starts, so new quantisation types need no table here.
"""

from __future__ import annotations

import os
import re
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

MAGIC = b"GGUF"
# Metadata value types (gguf.h).
UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY, UINT64, INT64, FLOAT64 = range(13)
_FORMATS = {UINT8: "B", INT8: "b", UINT16: "H", INT16: "h", UINT32: "I", INT32: "i", FLOAT32: "f", BOOL: "?",
            UINT64: "Q", INT64: "q", FLOAT64: "d"}
_SIZES = {k: struct.calcsize(v) for k, v in _FORMATS.items()}
MAX_STRING = 1 << 26  # 64 MB: chat templates are long, but nothing real is that long
MAX_COUNT = 1 << 25  # tokens, merges, tensors, dimensions
MAX_HEADER = 1 << 28  # 256 MB of header means the file isn't what it says it is
DEFAULT_ALIGNMENT = 32
_LAYER = re.compile(r"blk\.(\d+)\.")
_OUTPUT = ("output.", "output_norm.", "lm_head.")
_EMBED = ("token_embd.", "token_embd_norm.", "token_types.", "position_embd.")


class GGUFError(ValueError):
    """The file isn't a GGUF model this can read."""


@dataclass
class Shape:
    """How a model is built, as far as memory goes."""

    arch: str
    name: str = ""
    n_layer: int = 0
    n_embd: int = 0
    n_ff: int = 0
    n_head: int = 0
    n_vocab: int = 0
    n_ctx_train: int = 0
    head_dim_k: int = 0
    head_dim_v: int = 0
    kv_heads: list[int] = field(default_factory=list)  # per layer; 0 = no KV cache in that layer
    layer_bytes: list[int] = field(default_factory=list)  # each repeating layer's weights
    output_bytes: int = 0  # the output layer: on the graphics card only once every layer is
    embed_bytes: int = 0  # token embeddings: llama.cpp always keeps these in RAM
    file_bytes: int = 0
    file_type: int = -1  # general.file_type (15 = Q4_K_M)
    tied_output: bool = False  # no output.weight: the token embeddings double as the output layer

    @property
    def weights_bytes(self) -> int:
        """The weights in the file. A tied output layer is the token embeddings again: counted once."""
        output = max(0, self.output_bytes - self.embed_bytes) if self.tied_output else self.output_bytes
        return sum(self.layer_bytes) + output + self.embed_bytes

    def kv_bytes_per_token(self, layers: int | None = None, k_bytes: float = 2.0, v_bytes: float = 2.0) -> float:
        """Bytes of KV cache one token of context takes in the last `layers` layers (all when None): llama.cpp
        puts the last layers on the graphics card first, and each layer's cache goes with it."""
        heads = self.kv_heads[-layers:] if layers else ([] if layers == 0 else self.kv_heads)
        return sum(h * (self.head_dim_k * k_bytes + self.head_dim_v * v_bytes) for h in heads)

    def gpu_layer_bytes(self, layers: int) -> int:
        """Weights of the last `layers` repeating layers."""
        return sum(self.layer_bytes[-layers:]) if layers > 0 else 0


# ---------------------------------------------------------------- reading


class _Reader:
    """Little reads from a stream, through a buffer: a tokenizer is hundreds of thousands of short strings."""

    CHUNK = 1 << 20

    def __init__(self, stream: BinaryIO, endian: str = "<"):
        self.stream, self.endian = stream, endian
        self.buf = b""
        self.pos = 0
        self.consumed = 0  # bytes handed out so far: the header's length once it's all read

    def _need(self, n: int) -> None:
        if self.pos + n <= len(self.buf):
            return
        rest = self.buf[self.pos:]
        parts = [rest]
        have = len(rest)
        while have < n:
            chunk = self.stream.read(max(self.CHUNK, n - have))
            if not chunk:
                raise GGUFError("The file ends inside its header (an unfinished download?).")
            parts.append(chunk)
            have += len(chunk)
        self.buf, self.pos = b"".join(parts), 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.consumed + n > MAX_HEADER:
            raise GGUFError("The header is far too big to be real.")
        self._need(n)
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        self.consumed += n
        return out

    def skip(self, n: int) -> None:
        if n <= len(self.buf) - self.pos:
            self.pos += n
            self.consumed += n
            return
        self.take(n)

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        self._need(size)
        values = struct.unpack_from(self.endian + fmt, self.buf, self.pos)
        self.pos += size
        self.consumed += size
        return values[0] if len(values) == 1 else values

    def count(self) -> int:
        n = self.unpack("Q")
        if n > MAX_COUNT:
            raise GGUFError("The header has an impossible count in it.")
        return n

    def string(self) -> str:
        n = self.unpack("Q")
        if n > MAX_STRING:
            raise GGUFError("The header has an impossibly long text in it.")
        return self.take(n).decode("utf-8", "replace")

    def skip_string(self) -> None:
        n = self.unpack("Q")
        if n > MAX_STRING:
            raise GGUFError("The header has an impossibly long text in it.")
        self.skip(n)


def _value(r: _Reader, kind: int, keep: bool):
    """One metadata value; `keep` False skips it (most of the tokenizer) without building it."""
    if kind in _FORMATS:
        return r.unpack(_FORMATS[kind])
    if kind == STRING:
        if keep:
            return r.string()
        r.skip_string()
        return None
    if kind == ARRAY:
        sub, n = r.unpack("I"), r.count()
        if sub in _SIZES and not keep:
            r.skip(_SIZES[sub] * n)
            return _Count(n)
        if sub == STRING and not keep:
            for _ in range(n):
                r.skip_string()
            return _Count(n)
        if sub not in _FORMATS and sub not in (STRING, ARRAY):
            raise GGUFError(f"Unknown metadata type {sub}.")
        return [_value(r, sub, True) for _ in range(n)]
    raise GGUFError(f"Unknown metadata type {kind}.")


class _Count(int):
    """A skipped array: only how long it was."""


def _wanted(key: str) -> bool:
    return key.startswith("general.") or (not key.startswith("tokenizer.") and "." in key)


def read_stream(stream: BinaryIO, file_bytes: int = 0) -> Shape:
    """A model's shape from the start of its file. `file_bytes` (the whole file's size) sizes the last
    tensor; without it, that one tensor is left out."""
    magic = stream.read(4)
    if magic != MAGIC:
        raise GGUFError("This isn't a GGUF model file.")
    raw_version = stream.read(4)
    if len(raw_version) < 4:
        raise GGUFError("The file ends inside its header.")
    version = struct.unpack("<I", raw_version)[0]
    endian = "<"
    if version > 0xFFFF:  # written big-endian
        version, endian = struct.unpack(">I", raw_version)[0], ">"
    if version < 2:
        raise GGUFError(f"GGUF version {version} is too old; convert the model again with a newer llama.cpp.")
    r = _Reader(stream, endian)
    r.consumed = 8
    n_tensors, n_kv = r.count(), r.count()
    meta: dict[str, object] = {}
    n_tokens = 0
    for _ in range(n_kv):
        key = r.string()
        kind = r.unpack("I")
        value = _value(r, kind, _wanted(key))
        if key == "tokenizer.ggml.tokens":
            n_tokens = int(value) if isinstance(value, int) else len(value or [])
        elif value is not None and not isinstance(value, _Count):
            meta[key] = value
    tensors: list[tuple[str, int]] = []  # (name, offset)
    for _ in range(n_tensors):
        name = r.string()
        dims = r.unpack("I")
        if dims > 8:
            raise GGUFError("A tensor has an impossible number of dimensions.")
        r.skip(8 * dims)  # the shape; sizes come from the offsets
        r.unpack("I")  # type
        tensors.append((name, r.unpack("Q")))
    alignment = int(meta.get("general.alignment") or DEFAULT_ALIGNMENT) or DEFAULT_ALIGNMENT
    data_start = -(-r.consumed // alignment) * alignment
    return _shape(meta, n_tokens, tensors, data_start, file_bytes)


def _int(meta: dict, key: str, default: int = 0) -> int:
    v = meta.get(key, default)
    if isinstance(v, list):
        v = max((int(x) for x in v), default=default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _per_layer(meta: dict, key: str, n: int, default: int) -> list[int]:
    v = meta.get(key, default)
    if isinstance(v, list):
        values = [int(x) for x in v][:n]
        return values + [values[-1] if values else default] * (n - len(values))
    try:
        return [int(v)] * n
    except (TypeError, ValueError):
        return [default] * n


def _shape(meta: dict, n_tokens: int, tensors: list[tuple[str, int]], data_start: int, file_bytes: int) -> Shape:
    arch = str(meta.get("general.architecture") or "")
    if not arch:
        raise GGUFError("The file doesn't say which model architecture it is.")
    p = arch + "."
    n_layer = _int(meta, p + "block_count")
    n_embd = _int(meta, p + "embedding_length")
    n_head = _int(meta, p + "attention.head_count")
    head_k = _int(meta, p + "attention.key_length") or (n_embd // n_head if n_head else 0)
    head_v = _int(meta, p + "attention.value_length") or head_k
    kv_heads = _per_layer(meta, p + "attention.head_count_kv", n_layer, n_head)
    shape = Shape(arch=arch, name=str(meta.get("general.name") or ""), n_layer=n_layer, n_embd=n_embd,
                  n_ff=_int(meta, p + "feed_forward_length"), n_head=n_head,
                  n_vocab=_int(meta, p + "vocab_size") or n_tokens, n_ctx_train=_int(meta, p + "context_length"),
                  head_dim_k=head_k, head_dim_v=head_v, kv_heads=kv_heads, file_bytes=file_bytes,
                  file_type=_int(meta, "general.file_type", -1))
    ordered = sorted(tensors, key=lambda t: t[1])
    data_bytes = file_bytes - data_start if file_bytes > data_start else 0
    layers = [0] * n_layer
    has_output = False
    embed = 0
    for i, (name, offset) in enumerate(ordered):
        if i + 1 < len(ordered):
            size = ordered[i + 1][1] - offset
        elif data_bytes:
            size = max(0, data_bytes - offset)
        else:
            continue
        m = _LAYER.match(name)
        if m and int(m[1]) < n_layer:
            layers[int(m[1])] += size
        elif name.startswith(_OUTPUT):
            shape.output_bytes += size
            has_output = has_output or name in ("output.weight", "lm_head.weight")
        elif name.startswith(_EMBED):
            shape.embed_bytes += size
            if name == "token_embd.weight":
                embed = size
        else:
            shape.output_bytes += size  # small extras (rope factors): count them with the output layer
    shape.layer_bytes = layers
    if not has_output and embed:  # tied: the output layer is the token embeddings again, copied over
        shape.tied_output = True
        shape.output_bytes += embed
    if n_layer <= 0 or n_embd <= 0:
        raise GGUFError(f"The file's {arch} header has no layer count or width.")
    return shape


_cache: dict[tuple[str, int, int], Shape] = {}
_cache_lock = threading.Lock()


def read(path: Path | str) -> Shape:
    """The shape of the model in a GGUF file (remembered until the file changes). Raises GGUFError."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        raise GGUFError(f"Can't open {path.name}: {exc}") from exc
    key = (str(path), st.st_size, int(st.st_mtime))
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        with open(path, "rb") as f:
            shape = read_stream(f, st.st_size)
    except OSError as exc:
        raise GGUFError(f"Can't read {path.name}: {exc}") from exc
    except (struct.error, UnicodeDecodeError, MemoryError) as exc:
        raise GGUFError(f"{path.name} has a damaged header.") from exc
    with _cache_lock:
        _cache[key] = shape
    return shape


def describe(shape: Shape) -> str:
    """'qwen2 · 28 layers · 4.4 GB of weights · 32,768-token context'."""
    return (f"{shape.arch} · {shape.n_layer} layers · {shape.weights_bytes / 1e9:.1f} GB of weights"
            + (f" · {shape.n_ctx_train:,}-token context" if shape.n_ctx_train else ""))


def file_size(path: Path | str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
