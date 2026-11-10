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
certificate must match the pinned fingerprint; a Mac with no pinned fingerprint never sends its key at all.
The key is 256 random bits (stretching adds nothing to a random key; PBKDF2 is for the short code people
type), and the PC keeps only its SHA-256.

Both ends speak TLS 1.2 or 1.3 only, with forward-secret key exchange and authenticated ciphers (harden), and
a Mac that loses the PC tries again on a growing, jittered delay (Backoff) rather than every few seconds.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import re
import secrets
import socket
import ssl

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


# TLS 1.2's suites: ECDHE key exchange (forward secrecy) with AES-GCM or ChaCha20 only (TLS 1.3's are all of that
# kind already). The PC's own key is ECDSA (certs.py).
TLS12_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!eNULL:!MD5:!DSS"


def harden(ctx: ssl.SSLContext) -> ssl.SSLContext:
    """TLS 1.2 or newer, the ciphers above, no compression or renegotiation: for both ends."""
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers(TLS12_CIPHERS)
    except ssl.SSLError:  # an OpenSSL that names them differently: its defaults are still modern
        pass
    ctx.options |= ssl.OP_NO_COMPRESSION | getattr(ssl, "OP_NO_RENEGOTIATION", 0)
    return ctx


class Backoff:
    """How long to wait before trying a lost PC again: `base` seconds, doubling each failure up to `cap`, each
    wait moved up to ±`jitter` of itself at random (so several Macs, or a Mac's own threads, don't all knock at
    once when the PC comes back). reset() after a success."""

    def __init__(self, base: float = 2.0, cap: float = 60.0, factor: float = 2.0, jitter: float = 0.2,
                 rand=random.random):
        self.base, self.cap, self.factor, self.jitter, self.rand = base, cap, factor, jitter, rand
        self.failures = 0

    def peek(self) -> float:
        """The wait the next failure gets, before jitter."""
        return min(self.cap, self.base * self.factor ** self.failures)

    def next(self) -> float:
        """One more failure: how long to wait now."""
        delay = self.peek()
        self.failures += 1
        return max(0.0, delay * (1 + self.jitter * (2 * self.rand() - 1)))

    def reset(self) -> None:
        self.failures = 0


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


def udp_no_resets(sock: socket.socket) -> None:
    """Windows: a UDP socket whose earlier packet was refused (the other side had closed) fails its next
    recvfrom with WSAECONNRESET; turn that off, so one Mac that went away can't stop discovery."""
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        try:
            sock.ioctl(socket.SIO_UDP_CONNRESET, False)
        except (OSError, ValueError):
            pass
