"""What the host (this PC) and the Mac app agree on: ports, versions, and linking a Mac with a code.

Linking (pairing) has to stop someone else on the same Wi-Fi from posing as the PC. Every connection is HTTPS
with the PC's own certificate, which a Mac has never seen the first time, so the code you type is what proves
it's talking to your PC, and the certificate is then remembered ("pinned") for every later connection:

1. The Mac asks /api/hello (no key needed) for the PC's name and the pairing salt, and notes the fingerprint
   (SHA-256) of the certificate it was shown.
2. Both sides stretch the code with PBKDF2 (ITERATIONS rounds, the salt), so trying codes one by one takes
   far longer than the code lives (ten minutes).
3. The Mac sends an HMAC of the fingerprint it saw plus a random nonce and its name. The PC checks it against
   its own certificate's fingerprint: someone in the middle shows the Mac a different certificate, so the
   proof doesn't match and five wrong tries throw the code away.
4. The PC answers with the Mac's key (a random token; the PC keeps only its hash) and its own HMAC, which
   shows the Mac that the PC knew the code too.

After that, every request carries the key ("Authorization: Bearer <device>.<token>") over a connection whose
certificate must match the pinned fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

API_VERSION = 1  # bumped when a change would break an older Mac app or host
APP = "ClipMonster"
PORT = 47320  # HTTPS (Settings → Mac app can change it)
DISCOVERY_PORT = 47321  # UDP: "who's there?" on the local network
DISCOVER_HELLO = b"CLIPMONSTER-DISCOVER 1 "  # + a nonce; the host answers with JSON

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to mix up
CODE_LENGTH = 8  # shown as ABCD-EFGH; 32^8 codes
CODE_LIFE = 10 * 60.0
MAX_WRONG = 5  # wrong proofs before the code is thrown away
ITERATIONS = 200_000  # PBKDF2-SHA256 rounds that stretch the code
MIN_ITERATIONS = 100_000  # a Mac won't link with a PC asking for fewer


def new_code() -> str:
    return "".join(secrets.choice(CODE_CHARS) for _ in range(CODE_LENGTH))


def show_code(code: str) -> str:
    """'ABCDEFGH' → 'ABCD-EFGH'."""
    c = normalize_code(code)
    return f"{c[:4]}-{c[4:]}" if len(c) == CODE_LENGTH else c


def normalize_code(text: str) -> str:
    """What was typed, as the code: upper case, without spaces or dashes."""
    return re.sub(r"[\s\-_.]", "", str(text or "")).upper()


def looks_like_code(text: str) -> bool:
    c = normalize_code(text)
    return len(c) == CODE_LENGTH and all(ch in CODE_CHARS for ch in c)


def fingerprint(der: bytes) -> str:
    """A certificate's SHA-256 fingerprint, as lower-case hex."""
    return hashlib.sha256(der).hexdigest()


def short_fingerprint(fp: str) -> str:
    """The first 16 hex digits in groups of four, for people to compare: 'a1b2 c3d4 e5f6 0718'."""
    f = (fp or "").lower()[:16]
    return " ".join(f[i:i + 4] for i in range(0, len(f), 4))


def stretch(code: str, salt: bytes, iterations: int | None = None) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode(), salt, iterations or ITERATIONS, 32)


def _mac(key: bytes, *parts: bytes | str) -> str:
    h = hmac.new(key, digestmod=hashlib.sha256)
    for p in parts:
        b = p.encode() if isinstance(p, str) else p
        h.update(len(b).to_bytes(4, "big") + b)  # length-prefixed, so parts can't be shifted between fields
    return h.hexdigest()


def mac_proof(key: bytes, fp: str, nonce: str, device_name: str) -> str:
    """The Mac's proof that it knows the code and saw certificate `fp`."""
    return _mac(key, "mac", fp.lower(), nonce, device_name)


def host_proof(key: bytes, fp: str, nonce: str, device_id: str, token: str) -> str:
    """The host's answer: it knew the code too, and this key is for that Mac."""
    return _mac(key, "host", fp.lower(), nonce, device_id, token)


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or "").encode(), str(b or "").encode())


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def auth_header(device_id: str, token: str) -> str:
    return f"Bearer {device_id}.{token}"


def parse_auth(header: str) -> tuple[str, str]:
    """'Bearer dev.token' → ('dev', 'token'); ('', '') when it isn't one."""
    h = str(header or "").strip()
    if not h.lower().startswith("bearer "):
        return "", ""
    device, _, token = h[7:].strip().partition(".")
    return (device, token) if device and token else ("", "")
