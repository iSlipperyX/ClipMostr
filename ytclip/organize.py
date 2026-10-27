"""Folder layout, file naming templates and the per-video index files."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .analyze import Clip
from .download import Video

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows refuses paths over 260 characters, and OneDrive/Desktop folders eat
# a lot of that budget, so names shrink to fit whatever room is left.
PATH_LIMIT = 250


def safe_name(text: str, max_len: int = 60) -> str:
    text = _ILLEGAL.sub("", str(text))
    text = " ".join(text.split()).strip(" .")
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0].rstrip(" .,;-") or text[:max_len]
    return text or "untitled"


def stamp(seconds: float, sep: str = ":") -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}{sep}{m:02d}{sep}{s:02d}" if h else f"{m}{sep}{s:02d}"


def _render(template: str, fields: dict, fallback: str) -> str:
    try:
        return template.format(**fields)
    except (KeyError, ValueError, IndexError):
        return fallback.format(**fields)


def video_dir(root: Path, video: Video, template: str = "{channel}/{date} {title} [{id}]") -> Path:
    date = video.upload_date
    date = f"{date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else ""
    root_len = len(str(root.resolve()))
    for title_len in (60, 50, 40, 30, 20, 12, 8):
        fields = {"channel": safe_name(video.channel, 30), "date": date, "id": video.id,
                  "title": safe_name(video.title, title_len), "uploader": safe_name(video.channel, 30)}
        rel = _render(template, fields, "{channel}/{date} {title} [{id}]")
        parts = [safe_name(p, 120) for p in re.split(r"[\\/]+", rel) if p.strip()]
        # Leave ~60 chars for "\thumbnails\01.jpg" / clip file names inside the folder.
        if root_len + sum(len(p) + 1 for p in parts) + 60 <= PATH_LIMIT or title_len == 8:
            return root.joinpath(*parts)
    raise AssertionError("unreachable")


def clip_filename(rank: int, clip: Clip, folder: Path, template: str = "{rank:02d} - {time} - {label}",
                  ext: str = "mp4") -> str:
    room = PATH_LIMIT - len(str(folder.resolve())) - len(ext) - 2
    for label_len in (50, 40, 30, 20, 10, 0):
        label = safe_name(clip.title, label_len) if clip.title and label_len else "clip"
        fields = {"rank": rank, "time": stamp(clip.start, "-"), "label": label, "title": label,
                  "score": int(clip.score), "start": int(clip.start), "end": int(clip.end)}
        name = safe_name(_render(template, fields, "{rank:02d} - {time} - {label}"), 200)
        if len(name) <= room or label_len == 0:
            return f"{name}.{ext}"
    raise AssertionError("unreachable")


def watch_link(video: Video, seconds: float) -> str:
    if "youtube.com" in video.url or "youtu.be" in video.url:
        return f"https://www.youtube.com/watch?v={video.id}&t={int(seconds)}s"
    return video.url


def write_index(folder: Path, video: Video, clips: list[Clip], files: list[str], signals: list[str],
                mode: str, extra: dict | None = None) -> None:
    records = [
        {
            "rank": i,
            "file": f,
            "start": round(c.start, 2),
            "end": round(c.end, 2),
            "length": round(c.length, 2),
            "score": c.score,
            "title": c.title,
            "origin": c.origin,
            "reason": c.reason,
            "watch": watch_link(video, c.start),
        }
        for i, (c, f) in enumerate(zip(clips, files), 1)
    ]
    meta = {
        "video": {k: v for k, v in video.to_json().items() if k not in ("path", "subtitles", "heatmap")},
        "source": video.url,
        "mode": mode,
        "signals": signals,
        "clips": records,
    }
    meta |= {k: v for k, v in (extra or {}).items() if k not in meta}
    (folder / "clips.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# {video.title}",
        "",
        f"**Channel:** {video.channel}  ",
        f"**Source:** {video.url}  ",
        f"**Length:** {stamp(video.duration)}  ",
        f"**Picked using:** {', '.join(signals) if signals else mode}",
        "",
        "| # | Clip | Time | Length | Score | Watch |",
        "|---|------|------|--------|-------|-------|",
    ]
    for r in records:
        thumb = f"![](thumbnails/{r['rank']:02d}.jpg)"
        lines.append(
            f"| {r['rank']} | {thumb}<br>[{r['title'] or 'clip'}](<{r['file']}>) | "
            f"{stamp(r['start'])}–{stamp(r['end'])} | {r['length']:.0f}s | {r['score']:.0f} | "
            f"[▶]({r['watch']}) |"
        )
        if r["reason"]:
            lines.append(f"| | *{r['reason']}* | | | | |")
    (folder / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
