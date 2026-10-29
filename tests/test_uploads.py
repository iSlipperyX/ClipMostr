"""Uploading finished clips (uploads.py) to Google Drive (gdrive.py) or a folder, against a fake Google
that speaks OAuth (with PKCE), Drive's folder calls and resumable uploads over local HTTP, and can drop
connections, expire sign-ins, fill up and lose folders."""

import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ytclip import cleanup, cli, gdrive, uploads
from ytclip.jobs import JobDB
from ytclip.settings import Settings

OLD = time.time() - 3600


class FakeGoogle:
    def __init__(self):
        self.files: dict[str, dict] = {}  # id -> {name, parents, folder, data, trashed, description}
        self.sessions: dict[str, dict] = {}
        self.challenge = ""
        self.refresh_ok = True
        self.full = False
        self.drop_once_at = None  # drop the connection once a piece crossing this offset arrives
        self.expired_once = False  # answer 401 once, as if the access token ran out early
        self.puts: list[str] = []
        self.next_id = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def authed(self):
                if self.headers.get("Authorization", "").startswith("Bearer at"):
                    if fake.expired_once:
                        fake.expired_once = False
                        self.reply(401, {"error": {"code": 401, "message": "Invalid Credentials"}})
                        return False
                    return True
                self.reply(401, {"error": {"code": 401, "message": "no token"}})
                return False

            def do_POST(self):
                url = urllib.parse.urlsplit(self.path)
                raw = self.body()
                if url.path == "/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode()))
                    if form.get("client_secret") != "sec":
                        return self.reply(401, {"error": "invalid_client"})
                    if form["grant_type"] == "authorization_code":
                        digest = hashlib.sha256(form["code_verifier"].encode()).digest()
                        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != fake.challenge:
                            return self.reply(400, {"error": "invalid_grant", "error_description": "PKCE"})
                        return self.reply(200, {"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                                                "scope": gdrive.SCOPES})
                    if form.get("refresh_token") != "rt1" or not fake.refresh_ok:
                        return self.reply(400, {"error": "invalid_grant", "error_description": "Token revoked"})
                    return self.reply(200, {"access_token": "at2", "expires_in": 3600})
                if url.path == "/revoke":
                    return self.reply(200, {})
                if not self.authed():
                    return
                if url.path == "/drive/v3/files":
                    meta = json.loads(raw)
                    fid = fake.add(meta["name"], meta["parents"][0], folder=True)
                    return self.reply(200, {"id": fid})
                if url.path == "/upload/drive/v3/files":
                    return self.start(json.loads(raw), "")
                self.reply(404, {"error": {"message": "no such call"}})

            def do_PATCH(self):
                url = urllib.parse.urlsplit(self.path)
                raw = self.body()
                if not self.authed():
                    return
                m = re.fullmatch(r"/upload/drive/v3/files/(\w+)", url.path)
                if m and m.group(1) in fake.files:
                    return self.start(json.loads(raw), m.group(1))
                self.reply(404, {"error": {"message": "File not found"}})

            def start(self, meta, replace):
                if fake.full:
                    return self.reply(403, {"error": {"errors": [{"reason": "storageQuotaExceeded"}],
                                                      "message": "The user's Drive storage quota has been exceeded."}})
                parent = (meta.get("parents") or [""])[0]
                if parent and (parent not in fake.files or fake.files[parent]["trashed"]):
                    return self.reply(404, {"error": {"message": f"File not found: {parent}"}})
                sid = f"s{len(fake.sessions)}"
                fake.sessions[sid] = {"meta": meta, "replace": replace, "data": b"",
                                      "size": int(self.headers["X-Upload-Content-Length"])}
                self.reply(200, {}, {"Location": f"{fake.base}/session/{sid}"})

            def do_PUT(self):
                sid = self.path.rsplit("/", 1)[1]
                raw = self.body()
                session = fake.sessions.get(sid)
                if session is None:
                    return self.reply(404, {"error": {"message": "session gone"}})
                rng = self.headers["Content-Range"]
                fake.puts.append(rng)
                m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", rng)
                if m:
                    start = int(m.group(1))
                    if fake.drop_once_at is not None and start + len(raw) > fake.drop_once_at:
                        fake.drop_once_at = None
                        session["data"] = session["data"][:start] + raw[: len(raw) // 2]  # half of it arrived
                        self.close_connection = True
                        self.connection.close()
                        return
                    if start == len(session["data"]):
                        session["data"] += raw
                got = len(session["data"])
                if got >= session["size"]:
                    fid = session["replace"] or fake.add(session["meta"]["name"], session["meta"]["parents"][0])
                    fake.files[fid].update(data=session["data"], description=session["meta"].get("description", ""))
                    return self.reply(200 if session["replace"] else 201,
                                      {"id": fid, "webViewLink": f"https://drive.example/{fid}"})
                self.reply(308, None, {"Range": f"bytes=0-{got - 1}"} if got else {})

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if not self.authed():
                    return
                if url.path == "/userinfo":
                    return self.reply(200, {"email": "me@example.com"})
                if url.path == "/drive/v3/files":
                    q = urllib.parse.parse_qs(url.query)["q"][0]
                    name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q).group(1).replace("\\'", "'")
                    parent = re.search(r"'(\w+)' in parents", q).group(1)
                    found = [{"id": i} for i, f in fake.files.items()
                             if f["folder"] and f["name"] == name and f["parents"] == [parent] and not f["trashed"]]
                    return self.reply(200, {"files": found})
                m = re.fullmatch(r"/drive/v3/files/(\w+)", url.path)
                if m and m.group(1) in fake.files:
                    return self.reply(200, {"id": m.group(1), "trashed": fake.files[m.group(1)]["trashed"]})
                self.reply(404, {"error": {"message": "File not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, name, parent, folder=False):
        self.next_id += 1
        fid = f"f{self.next_id}"
        self.files[fid] = {"name": name, "parents": [parent], "folder": folder, "data": b"", "trashed": False,
                           "description": ""}
        return fid

    def path_of(self, fid):
        parts = []
        while fid in self.files:
            parts.insert(0, self.files[fid]["name"])
            fid = self.files[fid]["parents"][0]
        return "/".join(parts)

    def uploads(self):
        return {self.path_of(i): f for i, f in self.files.items() if not f["folder"]}

    def browser(self, url):
        """Stands in for the person signing in: Google's page sends the code back to ClipMonster."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        assert q["code_challenge_method"] == "S256" and "drive.file" in q["scope"] and q["access_type"] == "offline"
        assert q["redirect_uri"].startswith("http://127.0.0.1:")
        self.challenge = q["code_challenge"]
        with urllib.request.urlopen(f"{q['redirect_uri']}/?state={q['state']}&code=abc", timeout=10) as r:
            assert b"signed in" in r.read()


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setenv("CLIPMONSTER_GOOGLE_BASE", fake.base)
    monkeypatch.setattr(gdrive, "RETRY_WAITS", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(gdrive, "CHUNK", 256 * 1024)
    gdrive._tokens.clear()
    uploads.TARGETS["gdrive"] = uploads.GoogleDrive()  # no folders remembered from another test
    yield fake
    fake.server.shutdown()
    gdrive._tokens.clear()


def settings(tmp_path, **kw):
    return Settings(output_dir=str(tmp_path / "clips"), upload_target="gdrive", gdrive_client_id="cid",
                    gdrive_client_secret="sec").apply(kw)


def make_video(root: Path, rel: str, clips: dict[str, bytes], rejected: tuple[str, ...] = ()) -> Path:
    folder = root / rel
    (folder / "_rejected").mkdir(parents=True, exist_ok=True)
    records = []
    for i, (name, data) in enumerate(clips.items(), 1):
        file = f"_rejected/{name}" if name in rejected else name
        (folder / file).write_bytes(data)
        os.utime(folder / file, (OLD, OLD))
        records.append({"rank": i, "file": file, "title": f"Clip {i}", "score": 80, "reason": "funny",
                        **({"qc": {"ok": False}} if name in rejected else {})})
    (folder / "clips.json").write_text(json.dumps({"video": {"id": "vid", "title": "The video"},
                                                   "source": "https://youtu.be/vid", "clips": records}))
    return folder


def signed_in(google):
    return gdrive.sign_in("cid", "sec", open_browser=google.browser)


# ---------------------------------------------------------------- signing in

def test_sign_in_with_pkce_and_the_token_is_kept_sealed(google):
    account = signed_in(google)
    assert account.email == "me@example.com" and account.refresh_token == "rt1"
    saved = gdrive.token_path().read_text(encoding="utf-8")
    assert "rt1" not in saved if os.name == "nt" else '"plain:rt1"' in saved  # DPAPI on Windows
    assert gdrive.load_account("cid").refresh_token == "rt1"
    assert gdrive.load_account("another-client") is None  # a new client ID needs a new sign-in
    gdrive._tokens.clear()
    assert gdrive.access_token("cid", "sec") == "at2"  # refreshed from the saved sign-in
    gdrive.sign_out()
    assert gdrive.load_account() is None


def test_sign_in_needs_the_client_and_a_matching_answer(google, monkeypatch):
    with pytest.raises(gdrive.DriveError, match="client ID and secret") as err:
        gdrive.sign_in("", "")
    assert err.value.kind == "setup"

    def wrong_state(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        urllib.request.urlopen(f"{q['redirect_uri']}/?state=forged&code=abc", timeout=10).read()

    with pytest.raises(gdrive.DriveError, match="didn't match"):
        gdrive.sign_in("cid", "sec", open_browser=wrong_state)

    def denied(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        urllib.request.urlopen(f"{q['redirect_uri']}/?state={q['state']}&error=access_denied", timeout=10).read()

    with pytest.raises(gdrive.DriveError, match="wasn't allowed"):
        gdrive.sign_in("cid", "sec", open_browser=denied)
    with pytest.raises(gdrive.DriveError, match="cancelled"):
        gdrive.sign_in("cid", "sec", open_browser=lambda url: None, cancelled=lambda: True)
    assert gdrive.load_account() is None


# ---------------------------------------------------------------- uploading

def test_finished_clips_upload_once_in_their_folders(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "Gaming/Chan/2026-10-28 Video [vid]", {"01.mp4": b"a" * 600_000, "02.mp4": b"b",
                                                                          "03.mp4": b"c"}, rejected=("03.mp4",))
    make_video(s.output_path, "Other/2026-10-27 Mine [x]", {"01.mp4": b"m" * 10})
    fresh = make_video(s.output_path, "Other/Just now [y]", {"01.mp4": b"new"})
    os.utime(fresh / "01.mp4", None)  # still being exported
    db = JobDB()
    assert uploads.sync(db, s) == 3
    logs = []
    while uploads.upload_next(db, s, "1:test", logs.append):
        pass
    got = google.uploads()
    assert sorted(got) == ["ClipMonster/Automation/Gaming/Chan/2026-10-28 Video [vid]/01.mp4",
                           "ClipMonster/Automation/Gaming/Chan/2026-10-28 Video [vid]/02.mp4",
                           "ClipMonster/Other/2026-10-27 Mine [x]/01.mp4"]
    big = got["ClipMonster/Automation/Gaming/Chan/2026-10-28 Video [vid]/01.mp4"]
    assert big["data"] == b"a" * 600_000 and "Clip 1" in big["description"] and "youtu.be/vid" in big["description"]
    assert len([p for p in google.puts if p.startswith("bytes 0-")]) >= 1 and len(google.puts) >= 3  # in pieces
    st = uploads.status(db)
    assert st.done == 3 and st.waiting == 0 and st.done_today == 3 and "3 uploaded today" in st.text()
    assert uploads.sync(db, s) == 0  # nothing new
    folders = [f for f in google.files.values() if f["folder"]]
    assert len(folders) == len({google.path_of(i) for i, f in google.files.items() if f["folder"]})  # no duplicates


def test_a_clip_exported_again_replaces_its_upload(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    folder = make_video(s.auto_output_path, "V", {"01.mp4": b"first"})
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    (folder / "01.mp4").write_bytes(b"second version")
    os.utime(folder / "01.mp4", (OLD + 60, OLD + 60))
    assert uploads.sync(db, s) == 1
    assert uploads.upload_next(db, s, "1:t", print)
    got = google.uploads()
    assert list(got) == ["ClipMonster/Automation/V/01.mp4"] and got["ClipMonster/Automation/V/01.mp4"]["data"] == \
        b"second version"


def test_a_dropped_connection_carries_on_where_it_stopped(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    data = os.urandom(900_000)
    make_video(s.auto_output_path, "V", {"01.mp4": data})
    google.drop_once_at = 400_000
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    assert google.uploads()["ClipMonster/Automation/V/01.mp4"]["data"] == data
    assert any(p.startswith("bytes */") for p in google.puts)  # it asked how far it had got


def test_an_upload_cut_off_by_closing_resumes_its_session(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    data = os.urandom(800_000)
    make_video(s.auto_output_path, "V", {"01.mp4": data})
    db = JobDB()
    uploads.sync(db, s)
    sent = []

    def stop_after_one_piece():
        sent.append(1)
        return len(sent) > 1

    assert not uploads.upload_next(db, s, "1:t", print, cancelled=stop_after_one_piece)  # closed mid-upload
    row = db.con.execute("SELECT state, session FROM uploads").fetchone()
    assert row["state"] == "waiting" and row["session"]
    google.puts.clear()
    assert uploads.upload_next(db, s, f"{os.getpid()}:t", print)
    assert google.uploads()["ClipMonster/Automation/V/01.mp4"]["data"] == data
    assert google.puts[0].startswith("bytes */") and not google.puts[1].startswith("bytes 0-")  # carried on
    assert len(google.sessions) == 1


def test_a_dead_uploader_hands_its_clip_back(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.claim(db, "999999999:gone", "gdrive")  # an automation that crashed mid-upload
    assert uploads.upload_next(db, s, f"{os.getpid()}:t", print)


def test_an_expired_access_token_is_renewed(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    db = JobDB()
    uploads.sync(db, s)
    google.expired_once = True
    assert uploads.upload_next(db, s, "1:t", print)


def test_a_sign_in_that_stopped_working_pauses_uploads(google, tmp_path):
    signed_in(google)
    gdrive._tokens.clear()
    google.refresh_ok = False  # removed at myaccount.google.com, or expired
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x", "02.mp4": b"y"})
    db = JobDB()
    uploads.sync(db, s)
    logs = []
    assert not uploads.upload_next(db, s, "1:t", logs.append)
    assert "Sign in again" in db.meta("upload_problem") and db.meta_float("upload_hold") > time.time()
    assert not uploads.upload_next(db, s, "1:t", logs.append) and len(logs) == 1  # held, not tried clip by clip
    st = uploads.status(db)
    assert st.waiting == 2 and st.text().startswith("Uploads paused: ")


def test_a_full_drive_pauses_uploads(google, tmp_path):
    signed_in(google)
    google.full = True
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    db = JobDB()
    uploads.sync(db, s)
    assert not uploads.upload_next(db, s, "1:t", print)
    assert "Drive is full" in db.meta("upload_problem")
    google.full = False
    db.set_meta("upload_hold", 0)  # what Retry or new settings do
    assert uploads.upload_next(db, s, "1:t", print)


def test_a_folder_deleted_in_drive_is_made_again(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "A", {"01.mp4": b"x"})
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    for f in google.files.values():
        f["trashed"] = True  # you emptied ClipMonster's folder in Drive
    make_video(s.auto_output_path, "B", {"01.mp4": b"y"})
    uploads.TARGETS["gdrive"] = uploads.GoogleDrive()  # a new run: folders come from jobs.db
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    live = {p for p, f in google.uploads().items() if not f["trashed"]}
    assert live == {"ClipMonster/Automation/B/01.mp4"}


def test_failures_back_off_then_wait_for_retry(google, tmp_path, monkeypatch):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    db = JobDB()
    uploads.sync(db, s)
    monkeypatch.setattr(uploads.GoogleDrive, "upload", lambda self, *a, **kw: (_ for _ in ()).throw(
        uploads.UploadError("Google Drive answered 400: bad.")))
    clock = [time.time()]
    for _ in range(uploads.GIVE_UP):
        uploads.upload_next(db, s, "1:t", lambda m: None, now=lambda: clock[0])
        clock[0] += 7 * 3600
    st = uploads.status(db, clock[0])
    assert st.failed == 1 and "bad" in st.last_error
    assert uploads.retry_failed(db) == 1 and uploads.status(db).waiting == 1


def test_not_set_up_says_what_is_missing(tmp_path):
    db = JobDB()
    s = settings(tmp_path, gdrive_client_secret="")
    assert not uploads.upload_next(db, s, "1:t", print)
    assert "client ID and secret" in db.meta("upload_problem")
    s = settings(tmp_path)
    assert uploads.target_for(s).problem(s) == "Sign in to Google Drive in Settings → Upload."
    assert uploads.target_for(s.copy(upload_target="")) is None


def test_only_automation_clips_when_asked(tmp_path):
    s = settings(tmp_path, upload_what="automation")
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    make_video(s.output_path, "Mine", {"01.mp4": b"x"})
    assert [i.remote_dir for i in uploads.finished(s)] == [["Automation", "V"]]


# ---------------------------------------------------------------- a folder (a NAS)

def test_upload_to_a_folder(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    s = settings(tmp_path, upload_target="folder", upload_folder=str(nas))
    make_video(s.auto_output_path, "Gaming/V", {"01.mp4": b"clip"})
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    assert (nas / "Automation" / "Gaming" / "V" / "01.mp4").read_bytes() == b"clip"
    assert not list(nas.rglob("*.part"))
    gone = settings(tmp_path, upload_target="folder", upload_folder=str(tmp_path / "unplugged"))
    assert "isn't there" in uploads.target_for(gone).problem(gone)


def test_switching_server_uploads_again_to_the_new_one(tmp_path):
    nas, other = tmp_path / "nas", tmp_path / "other"
    nas.mkdir()
    other.mkdir()
    s = settings(tmp_path, upload_target="folder", upload_folder=str(nas))
    make_video(s.auto_output_path, "V", {"01.mp4": b"clip"})
    db = JobDB()
    uploads.sync(db, s)
    assert uploads.upload_next(db, s, "1:t", print)
    s2 = s.copy(upload_folder=str(other))
    assert uploads.sync(db, s2) == 0  # the same kind of server: already up
    s3 = s.copy(upload_target="gdrive")
    assert uploads.sync(db, s3) == 1  # another server: up it goes there too


# ---------------------------------------------------------------- the uploader thread, and the command line

def test_the_uploader_thread_uploads_and_stops(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    logs = []
    up = uploads.Uploader(lambda: s, logs.append)
    up.start()
    end = time.time() + 20
    while not google.uploads() and time.time() < end:
        time.sleep(0.05)
    up.stop()
    assert not up.busy and list(google.uploads()) == ["ClipMonster/Automation/V/01.mp4"]
    assert any("Uploaded 01.mp4 to Google Drive" in line for line in logs)


def test_the_window_leaves_uploading_to_automation_while_it_runs(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    s = settings(tmp_path, upload_target="folder", upload_folder=str(nas))
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})
    up = uploads.Uploader(lambda: s, print, idle_when=lambda db: True)
    up.start()
    time.sleep(0.5)
    up.stop()
    assert not list(nas.rglob("*.mp4"))


def test_cli_upload_now(google, tmp_path, monkeypatch):
    signed_in(google)
    s = settings(tmp_path)
    s.save()
    make_video(s.auto_output_path, "V", {"01.mp4": b"x", "02.mp4": b"y"})
    assert cli.main(["upload", "--now"]) == 0
    assert len(google.uploads()) == 2


# ---------------------------------------------------------------- Clean up clears what's safely uploaded

def test_clean_up_clears_uploaded_clips_tikTok_manager_does_not_need(tmp_path):
    nas = tmp_path / "nas"
    nas.mkdir()
    s = settings(tmp_path, upload_target="folder", upload_folder=str(nas))
    folder = make_video(s.auto_output_path, "V", {"01.mp4": b"x", "02.mp4": b"y", "03.mp4": b"z"})
    db = JobDB()
    uploads.sync(db, s)
    while uploads.upload_next(db, s, "1:t", print):
        pass
    (folder / "03.mp4").write_bytes(b"exported again, not uploaded yet")
    os.utime(folder / "03.mp4", (OLD, OLD))
    tm = cleanup.TikTokManager(found=True, keep={cleanup._key(folder / "02.mp4")}, watch_folders=[str(folder)])
    later = time.time() + 5 * 86400
    p = cleanup.plan(s, now=later, kinds=["uploaded"], tm=tm, older_than_days=3)
    assert [it.path.name for it in p.items] == ["01.mp4"] and p.kept_for_tm == 1
    assert cleanup.plan(s, now=time.time() + 86400, kinds=["uploaded"], tm=tm, older_than_days=3).items == []
    cleanup.apply(p, ["uploaded"])
    assert not (folder / "01.mp4").exists() and (folder / "02.mp4").exists()
    assert (nas / "Automation" / "V" / "01.mp4").exists()  # the uploaded copy stays


def test_each_folders_clips_json_goes_up_first_for_tiktok_manager(google, tmp_path):
    signed_in(google)
    s = settings(tmp_path)
    folder = make_video(s.auto_output_path, "Gaming/Chan/V", {"01.mp4": b"a", "02.mp4": b"b"})
    os.utime(folder / "clips.json", (OLD, OLD))
    db = JobDB()
    assert uploads.sync(db, s) == 3
    logs = []
    assert uploads.upload_next(db, s, "1:t", logs.append)
    assert logs[-1].startswith("Uploaded clips.json")  # ahead of its clips, so they arrive with their details
    while uploads.upload_next(db, s, "1:t", logs.append):
        pass
    got = google.uploads()
    index = got["ClipMonster/Automation/Gaming/Chan/V/clips.json"]
    assert json.loads(index["data"])["clips"][0]["title"] == "Clip 1"
    st = uploads.status(db)
    assert st.done == 2 and "2 uploaded today" in st.text()  # the list isn't counted as a clip
    # it changes (a clip exported again, say): the copy in Drive is replaced, not doubled
    (folder / "clips.json").write_text(json.dumps({"clips": [{"file": "01.mp4", "hook": "New hook"}]}))
    os.utime(folder / "clips.json", (OLD + 10, OLD + 10))
    assert uploads.sync(db, s) == 1
    while uploads.upload_next(db, s, "1:t", logs.append):
        pass
    got = google.uploads()
    assert json.loads(got["ClipMonster/Automation/Gaming/Chan/V/clips.json"]["data"])["clips"][0]["hook"] == "New hook"
    assert len([p for p in got if p.endswith("clips.json")]) == 1


def test_a_clips_json_being_written_waits_for_the_next_look(tmp_path):
    s = settings(tmp_path)
    make_video(s.auto_output_path, "V", {"01.mp4": b"x"})  # clips.json written just now
    assert [i.name for i in uploads.finished(s)] == ["01.mp4"]
    assert [i.name for i in uploads.finished(s, now=time.time() + 120)] == ["clips.json", "01.mp4"]
