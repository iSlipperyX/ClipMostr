"""Each account's TikTok profile as you want it: photo, name and bio.

You set them per account on the Accounts tab (Profile…),
then "Apply to TikTok" fills in TikTok's own Edit profile box in the account's browser
(tiktok/browser.py: set_profile). What was applied is remembered as a fingerprint, so the
app can tell when the profile you set up differs from what it last put on TikTok.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .accounts import Account
from .config import app_dir
from .media import _run, ffmpeg_exe

NAME_MAX = 30  # TikTok's limits for the display name and the bio
BIO_MAX = 80
PHOTO_SIZE = 720  # square, in pixels; TikTok shows it as a circle
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic"}


def photos_dir() -> Path:
    path = app_dir() / "profile-photos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def photo_path(a: Account) -> Path | None:
    """The account's prepared profile photo, if it has one and the file is still there."""
    if not a.profile_photo:
        return None
    path = photos_dir() / a.profile_photo
    return path if path.is_file() else None


def prepare_photo(src: Path, account_id: str) -> Path:
    """Crop `src` to its centre square and size it for TikTok, in the app's photo folder.

    Each version gets its own file name, so nothing shows a cached old picture. use_photo()
    makes it the account's photo; a prepared photo that isn't used can simply be deleted.
    """
    if not src.is_file():
        raise FileNotFoundError(f"The picture is missing: {src}")
    dest = photos_dir() / f"{account_id}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000:03d}.jpg"
    tmp = dest.with_name(dest.stem + ".part.jpg")
    try:
        _run([ffmpeg_exe(), "-hide_banner", "-y", "-v", "error", "-i", str(src), "-frames:v", "1",
              "-vf", f"crop='min(iw,ih)':'min(iw,ih)',scale={PHOTO_SIZE}:{PHOTO_SIZE}:flags=lanczos,format=yuvj420p",
              "-q:v", "2", str(tmp)])
    except RuntimeError as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"That file couldn't be read as a picture ({src.name}).") from exc
    tmp.replace(dest)
    return dest


def use_photo(a: Account, prepared: Path | None) -> None:
    """Make a prepared photo the account's photo (None removes it); the old file is deleted."""
    old = photo_path(a)
    a.profile_photo = prepared.name if prepared else ""
    if old and old != prepared:
        old.unlink(missing_ok=True)


def problems(name: str, bio: str) -> list[str]:
    """What TikTok would refuse, in plain words (empty when it's fine)."""
    out = []
    if len(name) > NAME_MAX:
        out.append(f"The name is {len(name)} characters; TikTok allows {NAME_MAX}.")
    if len(bio) > BIO_MAX:
        out.append(f"The bio is {len(bio)} characters; TikTok allows {BIO_MAX}.")
    return out


def is_set(a: Account) -> bool:
    return bool(a.profile_name or a.profile_bio or photo_path(a))


def fingerprint(a: Account) -> str:
    """Changes whenever the name, bio or photo set up for the account changes."""
    h = hashlib.sha256()
    h.update(a.profile_name.encode("utf-8") + b"\0" + a.profile_bio.encode("utf-8") + b"\0")
    photo = photo_path(a)
    if photo:
        h.update(hashlib.sha256(photo.read_bytes()).digest())
    return h.hexdigest()[:16]


def pending(a: Account) -> bool:
    """True when something is set up that hasn't been put on TikTok yet."""
    return is_set(a) and fingerprint(a) != a.profile_applied


def status(a: Account) -> str:
    """One line for the Accounts tab."""
    if not is_set(a):
        return "No profile set up yet."
    if a.profile_error and a.profile_applied != fingerprint(a):
        return f"Not fully applied: {a.profile_error}"
    if pending(a):
        return "Profile set up, not applied to TikTok yet."
    when = time.strftime("%d %b %H:%M", time.localtime(a.profile_applied_at)) if a.profile_applied_at else ""
    return f"Profile applied to TikTok{' on ' + when if when else ''}."


def mark_applied(a: Account) -> None:
    a.profile_applied = fingerprint(a)
    a.profile_applied_at = time.time()
    a.profile_error = ""
