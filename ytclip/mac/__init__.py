"""ClipMonster Remote: the Mac app that runs ClipMonster on your PC.

The PC (the host) keeps doing the work: finding videos, clipping, making Creator videos, the local AI and
posting to TikTok. This app shows what it's doing and lets you steer it from the Mac: watch the videos waiting
to post and OK, post, retry or delete them, ask for videos, clips and requests, pause automation or a category,
and read the week's report and the log. It talks to the server automation runs (ytclip/remote/server.py)
over HTTPS, linked once with a code from the PC's Settings → Mac app.

It's plain PySide6 and the standard library (plus keyring for the macOS Keychain), with ClipMonster's own look
(ytclip/gui/theme.py), so it runs on Windows and Linux too, which is how it's tested.
"""

APP_NAME = "ClipMonster Remote"
BUNDLE_ID = "com.islipperyx.clipmonster.remote"
