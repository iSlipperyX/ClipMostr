"""`ytclip remote`: the Mac app's server from a command line, for a PC with no screen (a Linux cloud VM, run
by the Linux host installer) as much as for Windows.

    ytclip remote status            on or off, its port, the addresses to type on the Mac, the linked Macs
    ytclip remote link              show the IP address, port and authorization code, and wait for the Mac
    ytclip remote on [--port N]     the same as ticking Settings → Mac app (automation starts it)
    ytclip remote off
    ytclip remote unlink MAC        a linked Mac's name or id
"""

from __future__ import annotations

import argparse
import json
import time

from rich.console import Console
from rich.markup import escape

from ..settings import Settings
from . import protocol, state

console = Console(highlight=False)


def _when(ts: float, now: float) -> str:
    if not ts:
        return "never"
    gap = max(0, int(now - ts))
    if gap < 90:
        return "just now"
    if gap < 3600:
        return f"{gap // 60} min ago"
    if gap < 86400:
        return f"{gap // 3600} h ago"
    return time.strftime("%d %b %Y", time.localtime(ts))


def addresses(status: dict) -> list[str]:
    """Where a Mac reaches this computer: the running server's list, else worked out here."""
    found = list(status.get("addresses") or [])
    if not found:
        from .server import mac_addresses

        found = mac_addresses()
    return found


def run_status(s: Settings, now: float) -> int:
    st = state.read_status(now=now)
    data = state.load()
    if not s.remote_on:
        console.print("○ Off. Turn it on with [bold]ytclip remote on[/] (or Settings → Mac app).")
    elif st.get("running"):
        console.print(f"[green]●[/] Running in automation on port {st.get('port')}.")
    elif st.get("error") and now - float(st.get("at") or 0) < state.STATUS_FRESH:
        console.print(f"[red]✗ {escape(str(st['error']))}[/]")
    else:
        console.print(f"[yellow]○[/] On (port {s.remote_port}), and it starts with automation: automation isn't "
                      "running.")
    if s.remote_on:
        ips = addresses(st)
        console.print("IP address" + ("es" if len(ips) > 1 else "") + " for the Mac: "
                      + (", ".join(ips) if ips else "none found"))
        if st.get("fingerprint"):
            console.print(f"This PC's ID: {protocol.short_fingerprint(st['fingerprint'])}")
    devices = data["devices"]
    if not devices:
        console.print("Linked Macs: none yet. Link one with [bold]ytclip remote link[/].")
    else:
        console.print("Linked Macs:")
        for d in devices:
            where = f" from {d['address']}" if d.get("address") else ""
            console.print(f"  💻 {escape(d.get('name') or 'Mac')}  [dim]({d['id']}) · linked "
                          f"{_when(d.get('linked_at') or 0, now)} · last seen {_when(d.get('seen_at') or 0, now)}"
                          f"{escape(where)}[/]")
    return 0


def run_link(s: Settings, args, now: float) -> int:
    if not s.remote_on:
        console.print("[yellow]The Mac app's server is off.[/] Turn it on first: [bold]ytclip remote on[/]")
        return 1
    with state.edit() as data:
        state.host_id(data)
        code = state.start_pairing(data, now)
        before = {d["id"] for d in data["devices"]}
        until = float(data["pairing"]["until"])
    st = state.read_status(now=now)
    ips = addresses(st)
    port = int(st.get("port") or s.remote_port) if st.get("running") else int(s.remote_port)
    if args.json:
        print(json.dumps({"addresses": ips, "port": port, "code": protocol.show_code(code), "until": until,
                          "running": bool(st.get("running")), "fingerprint": st.get("fingerprint", "")}))
        return 0
    console.print("In ClipMonster Remote on your Mac, type:\n")
    console.print(f"  IP address          [bold]{ips[0] if ips else '(this computer’s address)'}[/]"
                  + (f"   [dim]or {', '.join(ips[1:3])}[/]" if len(ips) > 1 else ""))
    console.print(f"  Port                [bold]{port}[/]")
    console.print(f"  Authorization code  [bold]{protocol.show_code(code)}[/]\n")
    if st.get("fingerprint"):
        console.print(f"The Mac shows this PC's ID as {protocol.short_fingerprint(st['fingerprint'])}.")
    if not st.get("running"):
        console.print("[yellow]Automation isn't running, so the Mac can't reach this computer yet: start it "
                      "(ytclip auto, or on a Linux host: sudo clipmonster start).[/]")
    if args.no_wait:
        console.print(f"The code works for {protocol.CODE_LIFE // 60} minutes.")
        return 0
    console.print(f"The code works for {protocol.CODE_LIFE // 60} minutes. Waiting for the Mac… (Ctrl+C stops "
                  "the code)")
    try:
        while True:
            time.sleep(args.poll)
            data = state.load()
            new = [d for d in data["devices"] if d["id"] not in before]
            if new:
                console.print(f"[green]✓ Linked {escape(new[-1].get('name') or 'a Mac')}.[/] It can use ClipMonster "
                              "now.")
                return 0
            if not state.pairing(data, time.time()):
                wrong = int((data.get("pairing") or {}).get("wrong") or 0)
                console.print("[yellow]The code stopped working" + (" (typed wrong too many times)" if wrong >=
                              protocol.MAX_WRONG else " (it ran out)") + ".[/] Run this again for a new one.")
                return 1
    except KeyboardInterrupt:
        with state.edit() as data:
            state.stop_pairing(data)
        console.print("\nStopped: that code doesn't work any more.")
        return 1


def run_unlink(which: str) -> int:
    with state.edit() as data:
        key = " ".join(which.split()).lower()
        found = [d for d in data["devices"] if d["id"] == which or (d.get("name") or "").lower() == key]
        if len(found) != 1:
            names = ", ".join(f"{d.get('name')} ({d['id']})" for d in data["devices"]) or "none"
            console.print(f"[red]{'More than one' if found else 'No'} linked Mac matches {escape(which)}.[/] "
                          f"Linked: {escape(names)}")
            return 1
        state.unlink(data, found[0]["id"])
    console.print(f"Unlinked {escape(found[0].get('name') or 'that Mac')}. It can't use ClipMonster until it's "
                  "linked again.")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="ytclip remote", description="The Mac app's server (Settings → Mac app): "
                                "turn it on, link a Mac with an IP address, port and authorization code, and "
                                "see or unlink the linked Macs. It runs inside automation.")
    sub = p.add_subparsers(dest="action")
    sub.add_parser("status", help="on or off, the addresses to type on the Mac, and the linked Macs")
    link = sub.add_parser("link", help="show the IP address, port and authorization code for the Mac, and wait")
    link.add_argument("--no-wait", action="store_true", help="show them and exit (the code still works 10 min)")
    link.add_argument("--json", action="store_true", help="print them as JSON and exit (for scripts)")
    link.add_argument("--poll", type=float, default=1.0, help=argparse.SUPPRESS)
    on = sub.add_parser("on", help="let Macs use ClipMonster (it runs with automation)")
    on.add_argument("--port", type=int, help=f"the port to listen on (default {protocol.PORT})")
    sub.add_parser("off", help="stop the server; linked Macs stay linked")
    un = sub.add_parser("unlink", help="a linked Mac can't use ClipMonster any more")
    un.add_argument("mac", help="its name or id (from ytclip remote status)")
    args = p.parse_args(argv)
    s = Settings.load()
    now = time.time()
    if args.action == "on":
        port = args.port or s.remote_port or protocol.PORT
        if not 1024 <= port <= 65535:
            console.print("[red]The port is a number from 1024 to 65535.[/]")
            return 2
        s.copy(remote_on=True, remote_port=port).save()
        console.print(f"On, port {port}. Automation starts it within a few seconds (or when it next starts).")
        return 0
    if args.action == "off":
        s.copy(remote_on=False).save()
        console.print("Off. Linked Macs stay linked for when it's on again.")
        return 0
    if args.action == "link":
        return run_link(s, args, now)
    if args.action == "unlink":
        return run_unlink(args.mac)
    return run_status(s, now)
