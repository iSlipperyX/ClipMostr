import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    """Settings, research history and caches go to a temp folder, never the real profile."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for key in ("YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)  # tests never reach a real API with the developer's keys
    return tmp_path


NOW = 1_800_000_000.0  # a fixed "now" so ages and velocities are exact


def api_item(vid="abc", title="A video", views=100_000, likes=5_000, comments=400, minutes=20.0,
             hours_old=10.0, category="20", live="none", caption=False, channel="Chan", license="youtube",
             language=""):
    """One item as videos.list returns it (chart=mostPopular, or by id)."""
    from datetime import datetime, timezone

    published = datetime.fromtimestamp(NOW - hours_old * 3600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = int(minutes * 60)
    stats = {"viewCount": str(views)}
    if likes is not None:
        stats["likeCount"] = str(likes)
    if comments is not None:
        stats["commentCount"] = str(comments)
    item = {
        "id": vid,
        "snippet": {"title": title, "channelTitle": channel, "channelId": "UC" + channel, "publishedAt": published,
                    "categoryId": category, "liveBroadcastContent": live,
                    "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}}},
        "statistics": stats,
        "contentDetails": {"duration": f"PT{total // 3600}H{total % 3600 // 60}M{total % 60}S",
                           "caption": "true" if caption else "false"},
        "status": {"license": license},
    }
    if language:
        item["snippet"]["defaultAudioLanguage"] = language
    return item


def spiky_heatmap(duration=1200, peaks=(300, 700, 1000), base=0.15, segments=100):
    """A Most-replayed heatmap: low everywhere except sharp peaks at the given seconds."""
    seg = duration / segments
    out = []
    for i in range(segments):
        mid = (i + 0.5) * seg
        value = base + max((max(0.0, 1 - abs(mid - p) / 25) for p in peaks), default=0) * (1 - base)
        out.append({"start_time": i * seg, "end_time": (i + 1) * seg, "value": value})
    return out


def close_leftovers(qapp, folder) -> None:
    """Close and delete every window still open, with the settings folders pointed at `folder`. A window saves
    its settings when it closes, and by the end of the run each test's own temp profile is no longer in force,
    so without this it would save over the developer's real ones."""
    from PySide6.QtCore import QCoreApplication, QEvent

    keys = ("APPDATA", "LOCALAPPDATA")
    before = {key: os.environ.get(key) for key in keys}
    os.environ["APPDATA"] = str(Path(folder) / "roaming")
    os.environ["LOCALAPPDATA"] = str(Path(folder) / "local")
    try:
        for w in qapp.topLevelWidgets():
            w.close()
            w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    finally:
        for key, was in before.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


@pytest.fixture(scope="session")
def app(tmp_path_factory):
    """One QApplication for the whole run. A module-scoped app was freed between test files while
    widgets from the earlier file were still alive, which crashed Qt at exit."""
    widgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    qapp = widgets.QApplication.instance() or widgets.QApplication([])
    yield qapp
    close_leftovers(qapp, tmp_path_factory.mktemp("leftovers"))
    import gc

    gc.collect()


def write_gguf(path, *, arch="llama", n_layer=2, n_embd=64, n_ff=128, n_head=4, kv_heads=4, key_length=None,
               ctx=4096, vocab=32, layer_bytes=4096, output_bytes=2048, embed_bytes=8192, tied=False,
               name="test model", endian="<", file_type=15, extra=None):
    """A GGUF model file with the given shape: a real header (metadata and tensor table, laid out as
    llama.cpp's gguf-py writes them) and zero-filled tensor data, which is all ClipMonster reads of it.
    `kv_heads` and `layer_bytes` are one number or one per layer; sizes should be multiples of 32 (the
    alignment). The tensor table lists the tensors in a different order from their data, as real files
    may, so sizes have to come from the offsets."""
    import struct

    def u32(v):
        return struct.pack(endian + "I", v)

    def u64(v):
        return struct.pack(endian + "Q", v)

    def text(t):
        b = t.encode()
        return u64(len(b)) + b

    def value(v):
        if isinstance(v, bool):
            return u32(7) + struct.pack(endian + "?", v)
        if isinstance(v, int):
            return u32(4) + u32(v)
        if isinstance(v, float):
            return u32(6) + struct.pack(endian + "f", v)
        if isinstance(v, str):
            return u32(8) + text(v)
        if isinstance(v, list) and v and isinstance(v[0], str):
            return u32(9) + u32(8) + u64(len(v)) + b"".join(text(x) for x in v)
        if isinstance(v, list):
            return u32(9) + u32(5) + u64(len(v)) + b"".join(struct.pack(endian + "i", x) for x in v)
        raise TypeError(v)

    p = arch + "."
    meta = {"general.architecture": arch, "general.name": name, "general.file_type": file_type,
            p + "block_count": n_layer, p + "context_length": ctx, p + "embedding_length": n_embd,
            p + "feed_forward_length": n_ff, p + "attention.head_count": n_head,
            p + "attention.head_count_kv": kv_heads}
    if key_length:
        meta[p + "attention.key_length"] = key_length
    if vocab:
        meta["tokenizer.ggml.tokens"] = [f"tok{i}" for i in range(vocab)]
    meta.update(extra or {})
    per_layer = layer_bytes if isinstance(layer_bytes, list) else [layer_bytes] * n_layer
    data = [("token_embd.weight", embed_bytes)]
    for i, size in enumerate(per_layer):
        data += [(f"blk.{i}.attn_q.weight", size // 2), (f"blk.{i}.ffn_down.weight", size - size // 2)]
    data += [("output_norm.weight", 256)] + ([] if tied else [("output.weight", output_bytes)])
    offsets, at = {}, 0
    for tensor, size in data:
        offsets[tensor] = at
        at += -(-size // 32) * 32
    infos = b""
    for tensor, size in reversed(data):
        infos += text(tensor) + u32(2) + u64(max(1, size // 4)) + u64(1) + u32(0) + u64(offsets[tensor])
    head = b"GGUF" + u32(3) + u64(len(data)) + u64(len(meta))
    head += b"".join(text(k) + value(v) for k, v in meta.items()) + infos
    head += b"\0" * (-len(head) % 32)
    with open(path, "wb") as f:
        f.write(head)
        f.truncate(len(head) + at)
    return path
