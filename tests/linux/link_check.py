"""CI, after the Linux host installer (tests.yml, linux-host): link to the installed host the way the Mac app
does, with the IP address, port and authorization code `clipmonster link-mac --json` printed, then use it.

    python tests/linux/link_check.py link.json
"""

import json
import sys

from ytclip.mac.client import Client


def main() -> int:
    got = json.loads(open(sys.argv[1], encoding="utf-8").read())
    assert got["running"], "automation's server isn't running"
    address = "127.0.0.1"
    assert got["addresses"], "no address for the Mac"
    print(f"Host says: {', '.join(got['addresses'])}, port {got['port']}, code {got['code']}")
    c = Client(address, got["port"], timeout=30.0)
    linked = c.pair(got["code"], "CI Mac")
    assert linked["fp"] == got["fingerprint"], "the certificate isn't the one the host reported"
    me = Client(address, got["port"], linked["fp"], linked["device_id"], linked["token"])
    who = me.get("/api/whoami")
    status = me.get("/api/status")
    print(f"Linked as {who}; host {status['host']['name']} runs ClipMonster {status['host']['version']}")
    assert status["automation"]["running"]
    me.get("/api/videos")
    me.get("/api/categories")
    stranger = Client(address, got["port"], linked["fp"], linked["device_id"], "0" * 43)
    try:
        stranger.get("/api/status")
    except Exception as exc:  # noqa: BLE001
        print(f"A wrong key is refused: {exc}")
    else:
        raise AssertionError("a wrong key was let in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
