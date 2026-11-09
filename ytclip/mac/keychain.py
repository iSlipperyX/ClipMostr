"""The Mac's key for each PC, kept in the macOS Keychain (through keyring), never in a plain file there.

Where keyring has no safe store (a Linux test machine without a keyring daemon), the key goes in a file only
this user can read, in the app's support folder.
"""

from __future__ import annotations

import json
import os
import sys

from . import APP_NAME
from .paths import support_dir

SERVICE = APP_NAME


def _keyring():
    if os.environ.get("CLIPMONSTER_REMOTE_HOME"):  # tests: never touch the real Keychain
        return None
    try:
        import keyring
        from keyring.backends import fail

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring):
            return None
        return keyring
    except Exception:  # noqa: BLE001 - no keyring: the file below
        return None


def _file():
    return support_dir() / "keys.json"


def _read_file() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file(data: dict) -> None:
    p = _file()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def where() -> str:
    """Where keys are kept, in words."""
    if _keyring() is not None:
        return "the macOS Keychain" if sys.platform == "darwin" else "the system keyring"
    return "a private file"


def save(account: str, secret: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, account, secret)
            return
        except Exception:  # noqa: BLE001 - the file instead
            pass
    data = _read_file()
    data[account] = secret
    _write_file(data)


def load(account: str) -> str:
    kr = _keyring()
    if kr is not None:
        try:
            got = kr.get_password(SERVICE, account)
            if got:
                return got
        except Exception:  # noqa: BLE001
            pass
    return str(_read_file().get(account) or "")


def delete(account: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, account)
        except Exception:  # noqa: BLE001
            pass
    data = _read_file()
    if data.pop(account, None) is not None:
        _write_file(data)
