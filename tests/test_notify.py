"""Windows notifications and the clipmonster:// links their buttons open."""

from ytclip import notify


def test_sign_in_links_round_trip_and_nothing_else_is_taken():
    link = notify.sign_in_link("a1b2c3d4e5")
    assert link == "clipmonster://sign-in/a1b2c3d4e5"
    assert notify.parse_link(link) == ("sign-in", "a1b2c3d4e5")
    assert notify.parse_link("CLIPMONSTER://sign-in/a1b2c3d4e5/") == ("sign-in", "a1b2c3d4e5")
    for bad in ("https://example.com", "C:\\Videos\\clip.mp4", "clipmonster://delete/x", "clipmonster://sign-in/",
                "clipmonster://sign-in/..%2F..%2Fx", "clipmonster://sign-in/a b"):
        assert notify.parse_link(bad) is None, bad


def test_the_toast_escapes_what_it_shows():
    xml = notify.toast_xml('Account "<@x>" & co', "needs <b>signing</b> in", "Sign in again",
                           notify.sign_in_link("abc"))
    assert "<b>" not in xml and "&lt;b&gt;" in xml and "&amp; co" in xml
    assert 'activationType="protocol"' in xml and 'arguments="clipmonster://sign-in/abc"' in xml
    assert 'launch="clipmonster://sign-in/abc"' in xml
    from xml.dom.minidom import parseString

    doc = parseString(xml)
    assert [t.firstChild.data for t in doc.getElementsByTagName("text")] == ['Account "<@x>" & co',
                                                                              "needs <b>signing</b> in"]


def test_the_script_carries_the_text_as_base64_only():
    script = notify._script(notify.toast_xml("a'; Remove-Item C:\\ -Recurse; '", "x"), notify.FALLBACK_APP_ID)
    assert "Remove-Item" not in script


def test_off_windows_nothing_is_shown(monkeypatch):
    monkeypatch.setattr(notify.os, "name", "posix")
    assert notify.toast("t", "x") is False and notify.register_links() is False
