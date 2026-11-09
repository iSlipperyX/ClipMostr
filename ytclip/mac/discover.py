"""Finding ClipMonster PCs on the local network: a broadcast "who's there?" that each PC's server answers
(remote/server.py), with its name, id, port and certificate fingerprint. The answer is only a hint for the
list: linking checks the certificate properly (protocol.py)."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import time

from ..remote import protocol


def _targets() -> list[str]:
    """Where to send the broadcast: everywhere on this network, and the /24 this Mac is on."""
    out = ["255.255.255.255"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        out.append(str(net.broadcast_address))
    except (OSError, ValueError):
        pass
    return out


def discover(timeout: float = 1.5, port: int = protocol.DISCOVERY_PORT, targets: list[str] | None = None) -> list[dict]:
    """The PCs that answered within `timeout` seconds: [{name, id, address, port, fp, version, pairing}]."""
    nonce = secrets.token_hex(8)
    found: dict[str, dict] = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        for t in targets or _targets():
            try:
                s.sendto(protocol.DISCOVER_HELLO + nonce.encode(), (t, port))
            except OSError:
                continue
        until = time.monotonic() + timeout
        while True:
            left = until - time.monotonic()
            if left <= 0:
                break
            s.settimeout(left)
            try:
                data, addr = s.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("app") != protocol.APP or d.get("nonce") != nonce:
                continue
            key = str(d.get("id") or addr[0])
            if key in found:
                continue
            found[key] = {"name": str(d.get("name") or addr[0]), "id": str(d.get("id") or ""), "address": addr[0],
                          "port": int(d.get("port") or protocol.PORT), "fp": str(d.get("fp") or ""),
                          "version": str(d.get("version") or ""), "api": int(d.get("api") or 0),
                          "pairing": bool(d.get("pairing"))}
    finally:
        s.close()
    return sorted(found.values(), key=lambda d: d["name"].lower())
