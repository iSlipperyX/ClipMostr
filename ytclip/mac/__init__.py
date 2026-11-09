"""ClipMonster for Mac: ClipMonster's Studio and Library on a Mac, and the PC it's linked to beside them.

It makes clips by itself: paste YouTube links or drop videos, and it downloads them, types them out, finds the
best moments with AI and exports them, all on the Mac (ytclip/gui's Studio and Library, with the Mac's own
folders, its media engine and Metal: settings.MAC). The AI is Claude or Gemini (their keys in the Keychain), a
local model on the Mac, or, once a PC is linked, the PC's local AI.

Linking a Windows PC (or the Linux host) that runs ClipMonster, with the IP address, port and authorization
code from its Settings → Mac app, adds what the PC does under Your PC in the sidebar: its automation, the videos
waiting to post (watch them, OK, post, retry or delete), the sports clips it sends, new videos and requests,
its categories, the week's report and its log (pc.py and the pages in this folder). The PC also lends the Mac
its local AI (pc_ai.py) and makes whole videos for it ("Make on: Your PC", pc_jobs.py), over HTTPS pinned to
the PC's own certificate (client.py; ytclip/remote/server.py and studio.py on the PC).

It's PySide6 and the standard library, plus keyring for the macOS Keychain, so it runs on Windows and Linux
too, which is how most of it is tested.
"""

APP_NAME = "ClipMonster"
BUNDLE_ID = "com.islipperyx.clipmonster"
OLD_NAME = "ClipMonster Remote"  # this app's name in its first builds, when it only steered the PC
