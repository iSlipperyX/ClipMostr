"""The PC's side of ClipMonster for Mac: a Mac (or another computer) on the same network uses this PC.

The PC is the host. Automation runs a small HTTPS server next to the Telegram bot (server.py) that the Mac app
(ytclip/mac/) talks to: what automation is doing, the videos waiting to post (watch, OK, post now, delete),
asking for videos, clips and requests, pausing, the weekly report and the log, and the PC's local AI and video
maker lent to the Mac (studio.py). It's off until Settings → Mac app turns it on, and it only answers computers
linked with a code from that page (pairing in protocol.py).

protocol.py is shared by both ends and needs nothing but the standard library, so the Mac app carries no
server code.
"""
