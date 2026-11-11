"""The PC's own HTTPS certificate for the Mac app: made once, kept in %APPDATA%\\ClipMonster\\remote.

It's self-signed (no certificate authority knows a PC on a home network), so the Mac trusts it by its
fingerprint, which pairing proves and the Mac then pins (protocol.py). A new certificate (after deleting the
folder) means each Mac has to be linked again.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import ssl
from pathlib import Path

from ..settings import app_dir
from . import protocol

KEY_FILE = "host-key.pem"
CERT_FILE = "host-cert.pem"
YEARS = 20


def folder() -> Path:
    p = app_dir() / "remote"
    p.mkdir(parents=True, exist_ok=True)
    return p


def paths(where: Path | None = None) -> tuple[Path, Path]:
    d = where or folder()
    return d / KEY_FILE, d / CERT_FILE


def ensure(where: Path | None = None, name: str = "") -> tuple[Path, Path]:
    """The key and certificate files, made now if they aren't there (or can't be read)."""
    key_path, cert_path = paths(where)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.is_file() and cert_path.is_file():
        try:
            fingerprint_of(cert_path)
            return key_path, cert_path
        except (OSError, ValueError):
            pass
    make(key_path, cert_path, name or socket.gethostname() or "ClipMonster")
    return key_path, cert_path


def make(key_path: Path, cert_path: Path, name: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"ClipMonster on {name}"[:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ClipMonster")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365 * YEARS))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("clipmonster.local")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    tmp = key_path.with_name(key_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # only this user can read the key
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)
    os.replace(tmp, key_path)
    tmp = cert_path.with_name(cert_path.name + ".tmp")
    tmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(tmp, cert_path)


def fingerprint_of(cert_path: Path) -> str:
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    return protocol.fingerprint(der)


def server_context(key_path: Path, cert_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    protocol.harden(ctx)
    ctx.options |= getattr(ssl, "OP_CIPHER_SERVER_PREFERENCE", 0)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    return ctx
