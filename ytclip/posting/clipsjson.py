"""Read the clips.json index ClipMonster writes next to each video's clips (titles, scores, hooks, the
quality check, and why automation picked the video), for the posting queue and each clip's record."""

from __future__ import annotations

import json
from pathlib import Path

REJECTED_DIR = "_rejected"  # ClipMonster's quality check moves failed clips here


def read_folder_index(folder: Path) -> dict[str, dict]:
    """Map clip file name -> what ClipMonster knows about it, for a ClipMonster output folder.

    Clips its quality check rejected are listed with "rejected": True so they can be left out."""
    index = folder / "clips.json"
    if not index.is_file():
        return {}
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    video = data.get("video") or {}
    auto = data.get("automation") or {}
    group = video.get("id") or video.get("url") or str(folder)
    out: dict[str, dict] = {}
    for clip in data.get("clips", []):
        file = clip.get("file")
        if not file:
            continue
        qc = clip.get("qc") or {}
        score = clip.get("score")
        rating = clip.get("rating")
        part, parts = clip.get("part"), clip.get("parts")
        out[Path(file).name] = {
            "title": clip.get("title") or "",
            "score": float(score) if isinstance(score, (int, float)) else None,
            "reason": clip.get("reason") or "",
            "group": str(group),
            "source_title": video.get("title", ""),
            "channel": video.get("channel", ""),
            "url": clip.get("watch") or data.get("source") or video.get("url", ""),
            "hook": clip.get("hook") or "",
            "rating": int(rating) if isinstance(rating, (int, float)) else None,
            "permission": auto.get("permission", ""),
            # ClipMonster 2.3: the category the clip was made for and the TikTok account that category posts to
            # (3.2 renamed niches to categories: older files say "niche" and "niche_id")
            "category": str(auto.get("category") or auto.get("niche") or ""),
            "category_id": str(auto.get("category_id") or auto.get("niche_id") or ""),
            "account": str(auto.get("account") or "").strip().lstrip("@").strip(),
            # ClipMonster 2.4: a part series ("series" is unique within this folder only)
            "series": str(clip.get("series") or ""),
            "part": int(part) if isinstance(part, (int, float)) and part >= 1 else None,
            "parts": int(parts) if isinstance(parts, (int, float)) and parts >= 1 else None,
            "license": video.get("license") or "",
            "warnings": list(qc.get("warnings") or []),
            # Posting's account id for the category (3.0): no name matching needed
            "account_id": str(auto.get("account_id") or ""),
            # Kept with the posting entry for its record, since the clips.json row goes when the clip is deleted
            "facts": _facts(data, clip),
            "rejected": qc.get("ok") is False or file.replace("\\", "/").startswith(f"{REJECTED_DIR}/"),
            # ClipMonster 3.3: made by the Video Creator, so it's labelled AI-generated when posted, and comes
            # with its own caption and hashtags
            "ai_generated": bool(clip.get("ai_generated") or (data.get("creator") or {}).get("ai_generated")),
            "caption": str(clip.get("caption") or ""),
            "hashtags": " ".join(f"#{str(h).lstrip('#')}" for h in clip.get("hashtags") or [] if str(h).strip("# ")),
        }
    return out


def needs_credit(meta: dict) -> bool:
    """Creative Commons clips must credit the creator. ClipMonster's picking rules say so in the
    permission; for videos added by hand, the video's licence says it."""
    return any("creative commons" in (meta.get(k) or "").lower() for k in ("permission", "license"))


def credit_line(channel: str) -> str:
    return f"🎥 {channel} on YouTube (CC BY)" if channel else ""


def _facts(data: dict, clip: dict) -> dict:
    """What a posted clip's record says about where it came from and how it was made."""
    video = data.get("video") or {}
    auto = data.get("automation") or {}
    request = auto.get("request") or {}
    qc = clip.get("qc") or {}
    out = {
        "source_title": video.get("title", ""),
        "source_channel": video.get("channel", ""),
        "source_url": data.get("source") or video.get("url", ""),
        "source_id": video.get("id", ""),
        "source_length": video.get("duration"),
        "source_uploaded": video.get("upload_date", ""),
        "license": video.get("license", ""),
        "start": clip.get("start"),
        "end": clip.get("end"),
        "length": clip.get("length"),
        "watch": clip.get("watch", ""),
        "clip_score": clip.get("score"),
        "origin": clip.get("origin", ""),
        "reason": clip.get("reason", ""),
        "hook": clip.get("hook", ""),
        "rating": clip.get("rating"),
        "rating_note": clip.get("rating_note", ""),
        "qc_warnings": list(qc.get("warnings") or []),
        "finder": data.get("mode", ""),
        "signals": list(data.get("signals") or []),
        "style": auto.get("recipe", ""),
        "how_picked": auto.get("how", ""),
        "permission": auto.get("permission", ""),
        "picked_at": auto.get("picked_at"),
        "category": auto.get("category") or auto.get("niche", ""),
        "request": request.get("notes", "") if isinstance(request, dict) else "",
        "series_part": clip.get("part"),
        "series_parts": clip.get("parts"),
    }
    return {k: v for k, v in out.items() if v not in ("", None, [])}
