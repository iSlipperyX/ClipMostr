"""hosts.json: the PCs this Mac is linked to (name, address, port, the certificate it pinned), and which one
it used last. Each PC's key is in the Keychain (keychain.py), under "<pc id>:<this Mac's id there>"."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

from . import keychain
from .paths import support_dir


@dataclass
class Host:
    id: str = ""  # the PC's own id (remote.json host_id), the same after its address changes
    name: str = ""  # the PC's name
    address: str = ""
    port: int = 47320
    fp: str = ""  # the pinned certificate's SHA-256
    device_id: str = ""  # this Mac's id on that PC
    linked_at: float = 0.0
    seen_at: float = 0.0
    addresses: list = field(default_factory=list)  # other addresses it answered on (Wi-Fi, Tailscale)

    @property
    def key_account(self) -> str:
        return f"{self.id}:{self.device_id}"

    def token(self) -> str:
        return keychain.load(self.key_account)

    @property
    def where(self) -> str:
        return f"{self.address}:{self.port}" if self.port != 47320 else self.address


def _path():
    return support_dir() / "hosts.json"


def load() -> tuple[list[Host], str]:
    """(the linked PCs, the id of the one used last)."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], ""
    known = {f.name for f in fields(Host)}
    out = []
    for d in data.get("hosts") or []:
        if isinstance(d, dict) and d.get("id") and d.get("fp"):
            try:
                out.append(Host(**{k: v for k, v in d.items() if k in known}))
            except TypeError:
                continue
    return out, str(data.get("last") or "")


def save(hosts: list[Host], last: str) -> None:
    p = _path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "last": last, "hosts": [asdict(h) for h in hosts]}, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


def remember(host: Host, token: str = "") -> None:
    """Add or update a PC (by its id) and make it the last one used."""
    hosts, _last = load()
    hosts = [h for h in hosts if h.id != host.id] + [host]
    if token:
        keychain.save(host.key_account, token)
    save(hosts, host.id)


def forget(host_id: str) -> None:
    hosts, last = load()
    for h in hosts:
        if h.id == host_id:
            keychain.delete(h.key_account)
    save([h for h in hosts if h.id != host_id], "" if last == host_id else last)


def last() -> Host | None:
    hosts, last_id = load()
    for h in hosts:
        if h.id == last_id:
            return h
    return hosts[-1] if hosts else None
