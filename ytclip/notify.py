"""Windows notifications that work with the window closed, and the clipmonster:// link they open.

Automation runs in the background (Start with Windows), often with no ClipMonster window open, so
something that needs you, like a TikTok account whose sign-in ran out, is shown as a Windows
notification with a button. The button opens a clipmonster:// link; Windows starts ClipMonster with
it, and the app goes straight to what the link asks for (gui/app.py hands it to a window already
open). The link is registered for this Windows user only (HKCU), each time the app starts, so it
always points at the copy you're running.

Links:
    clipmonster://sign-in/<account id>   the Accounts tab, signing that account in again
    clipmonster://report/week            the weekly report (Performance tab)
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape, quoteattr

SCHEME = "clipmonster"
APP_ID = "ClipMonster.Studio"  # the window's AppUserModelID; the installer's shortcuts carry it too
# Without the installer's Start menu shortcut, Windows only shows notifications from a known app:
# PowerShell's id is the usual stand-in.
FALLBACK_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def sign_in_link(account_id: str) -> str:
    return f"{SCHEME}://sign-in/{quote(account_id, safe='')}"


def report_link() -> str:
    return f"{SCHEME}://report/week"


def parse_link(text: str) -> tuple[str, str] | None:
    """('sign-in', account id) or ('report', 'week') for a clipmonster:// link, None for anything else."""
    try:
        u = urlparse(text.strip())
    except ValueError:
        return None
    if u.scheme.lower() != SCHEME:
        return None
    action = (u.netloc or "").lower()
    arg = unquote(u.path.strip("/"))
    if action == "sign-in" and arg and all(c.isalnum() or c in "-_" for c in arg):
        return action, arg
    if action == "report" and arg == "week":
        return action, arg
    return None


def app_command() -> list[str]:
    """How to start the app's window from this install (what a link runs)."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve().parent / "ClipMonster.exe")]
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return [str(pythonw if os.name == "nt" and pythonw.exists() else exe), "-m", "ytclip.gui.app"]


def register_links() -> bool:
    """Point clipmonster:// at this copy of the app, for this Windows user. True if it's registered."""
    if os.name != "nt":
        return False
    import winreg

    command = " ".join(f'"{c}"' for c in app_command()) + ' "%1"'
    base = rf"Software\Classes\{SCHEME}"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:ClipMonster")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command", 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except OSError:
        return False
    return True


def toast_xml(title: str, text: str, button: str = "", link: str = "") -> str:
    """The notification, in Windows' toast XML: clicking it (or its button) opens `link`."""
    launch = f" activationType=\"protocol\" launch={quoteattr(link)}" if link else ""
    actions = (f"<actions><action content={quoteattr(button)} activationType=\"protocol\" "
               f"arguments={quoteattr(link)}/></actions>") if button and link else ""
    return (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
            f"<text>{escape(text)}</text></binding></visual>{actions}</toast>")


def _script(xml: str, app_id: str) -> str:
    # The XML goes in as base64, so nothing in a title or account name can break out of the script.
    data = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] "
        "| Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        f"$x = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{data}'));"
        "$d = New-Object Windows.Data.Xml.Dom.XmlDocument; $d.LoadXml($x);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app_id}')"
        ".Show([Windows.UI.Notifications.ToastNotification]::new($d))"
    )


def installed_shortcut() -> bool:
    """The installer's Start menu shortcut is there (it carries APP_ID, so Windows shows our name)."""
    appdata = os.environ.get("APPDATA", "")
    return bool(appdata) and (Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
                              "ClipMonster.lnk").exists()


def toast(title: str, text: str, button: str = "", link: str = "") -> bool:
    """Show a Windows notification (it doesn't wait for it). False when it can't be shown here."""
    if os.name != "nt":
        return False
    if link:
        register_links()
    script = _script(toast_xml(title, text, button, link), APP_ID if installed_shortcut() else FALLBACK_APP_ID)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        return False
    return True


def sign_in_needed(handle: str, account_id: str) -> bool:
    """'TikTok account @x needs signing in again', with a button that opens the sign-in."""
    return toast("ClipMonster", f"TikTok account {handle} needs signing in again. Nothing posts to it until "
                 "it is.", button="Sign in again", link=sign_in_link(account_id))
