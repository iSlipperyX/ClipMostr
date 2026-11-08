"""Background music under the speech, ducked out of its way.

Music comes from a folder of your own royalty-free or licensed tracks (Settings → Editing → Music folder).
Sub-folders named for a mood are matched to the clip:

    Music/
      Suspense/     stories, curiosity and investigations
      Upbeat/       humour, hustle, wins
      Chill/        everything else (lo-fi)
      Dark/         controversy and shock

Any other layout works too: tracks are then simply rotated. The music loops for the clip's length, sits
`level` dB down, and a sidechain compressor keyed from the speech pulls it down a further ~16 dB whenever
someone talks (50 ms attack, 300 ms release), so words always win and the music swells in the gaps.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
VIBES = {
    "Suspense": ("suspense", "investigation", "mystery", "tension", "story"),
    "Upbeat": ("upbeat", "hustle", "energy", "happy", "hype", "fun"),
    "Chill": ("chill", "lofi", "lo-fi", "calm", "ambient", "relax"),
    "Dark": ("dark", "ominous", "drama", "horror", "intense"),
}
CATEGORY_VIBE = {"Story": "Suspense", "Curiosity": "Suspense", "Humor": "Upbeat", "Controversy": "Dark",
                 "Shock": "Dark"}
# The speech crossing -30 dBFS starts the duck; +6 dB of sidechain gain and 20:1 give about 16 dB of duck
# for speech around -18 dBFS.
DUCK = "sidechaincompress=threshold=0.0316:ratio=20:attack=50:release=300:makeup=1:knee=2.5:level_sc=2"


def tracks(folder: str | Path) -> list[Path]:
    root = Path(str(folder or "")).expanduser()
    if not str(folder or "").strip() or not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def vibe_of(path: Path, root: Path) -> str:
    try:
        parts = [x.lower() for x in path.relative_to(root).parts]
    except ValueError:
        parts = [path.name.lower()]
    text = " ".join(parts)
    for vibe, words in VIBES.items():
        if any(w in text for w in words):
            return vibe
    return ""


def pick(folder: str | Path, category: str = "", seed: str = "") -> Path | None:
    """A track for a clip: one of the mood its category calls for when the folder has one, else any;
    steadily varied by `seed` (the clip), so neighbouring clips get different tracks."""
    root = Path(str(folder or "")).expanduser()
    found = tracks(folder)
    if not found:
        return None
    want = CATEGORY_VIBE.get(category, "Chill")
    matching = [p for p in found if vibe_of(p, root) == want]
    pool = matching or found
    h = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def graph(speech: str, music: str, out: str, level_db: float, duration: float) -> str:
    """Filters mixing `music` (an input label, looping) under `speech`, ducked by it, into `out`."""
    fade_out = max(0.0, duration - 1.2)
    return (f"[{speech}]asplit=2[sp_main][sp_key];"
            f"[{music}]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,aresample=48000,"
            f"aformat=channel_layouts=stereo,volume={level_db:.1f}dB,"
            f"afade=t=in:d=0.8,afade=t=out:st={fade_out:.3f}:d=1.2[mus];"
            f"[sp_key]aformat=channel_layouts=stereo[key];"
            f"[mus][key]{DUCK}[ducked];"
            f"[sp_main]aformat=channel_layouts=stereo[sp_st];"
            f"[sp_st][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[{out}]")
