"""Sign-in with username + password, and what viewer / operator / admin may do."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from crawler.users import UserError, Users  # noqa: E402

PW = {"ana": "ana-password-1", "budi": "budi-password-1", "citra": "citra-password-1"}
SITE = {"Origin": "http://collector.example"}


def test_accounts_basics(tmp_path):
    u = Users(str(tmp_path / "users.json"))
    u.save("ana", "admin", PW["ana"])
    stored = json.load(open(tmp_path / "users.json"))
    assert PW["ana"] not in json.dumps(stored) and stored["ana"]["pw"].startswith("scrypt$")   # only a hash
    assert oct(os.stat(tmp_path / "users.json").st_mode)[-3:] == "600"
    assert u.check_login("ANA", PW["ana"]) == "ana" and u.check_login("ana", "wrong-password") is None
    for bad, msg in [(("Ana Budi", "viewer", PW["ana"]), "Username"), (("x", "viewer", PW["ana"]), "Username"),
                     (("budi", "boss", PW["ana"]), "role"), (("budi", "viewer", "short"), "at least 10")]:
        with pytest.raises(UserError, match=msg):
            u.save(*bad)
    with pytest.raises(UserError, match="at least one active admin"):
        u.save("ana", "viewer", by="someone-else")                # the only admin cannot lose admin rights
    with pytest.raises(UserError, match="yourself"):
        u.delete("ana", by="ana")
    # five wrong passwords lock the name for a while, even the right one then fails
    u.save("budi", "operator", PW["budi"])
    for _ in range(5):
        assert u.check_login("budi", "nope-nope-nope") is None
    assert u.locked("budi") and u.check_login("budi", PW["budi"]) is None
    # an account added from the command line while the server runs is picked up
    other = Users(str(tmp_path / "users.json"))
    other.save("citra", "viewer", PW["citra"])
    assert u.check_login("citra", PW["citra"]) == "citra"


def _app(tmp_path, monkeypatch, keys=None):
    from crawler import api, job
    fresh = job.Job()
    monkeypatch.setattr(api, "JOB", fresh)
    monkeypatch.setattr(api, "OUTPUT_DIR", str(tmp_path / "out"))
    os.makedirs(tmp_path / "out", exist_ok=True)
    (tmp_path / "out" / "news_links_a.xlsx").write_bytes(b"PK")
    app = api.create_app(keys, audit_path=str(tmp_path / "audit.log"), users_path=str(tmp_path / "users.json"))
    users = Users(str(tmp_path / "users.json"))
    users.save("ana", "admin", PW["ana"])
    users.save("budi", "operator", PW["budi"])
    users.save("citra", "viewer", PW["citra"])
    return app, fresh


def _client(app, name=None):
    c = TestClient(app, base_url="http://collector.example")
    if name:
        r = c.post("/auth", json={"username": name, "password": PW[name]})
        assert r.status_code == 200 and r.json()["role"] and r.cookies["plc_session"].startswith("u.")
    return c


def test_sign_in_page_and_wrong_password(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    c = _client(app)
    page = c.get("/").text
    assert 'id="u"' in page and 'id="p"' in page and "access key instead" in page
    assert c.get("/api/status").status_code == 401
    r = c.post("/auth", json={"username": "ana", "password": "not-her-password"})
    assert r.status_code == 401 and r.json()["error"] == "Wrong username or password."
    me = _client(app, "ana").get("/api/me").json()
    assert me["who"] == "ana" and me["role"] == "admin" and me["password"] is True


def test_what_each_role_may_do(tmp_path, monkeypatch):
    app, job = _app(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(job, "start_news", lambda *a: started.append(a) or None)
    viewer, operator, admin = _client(app, "citra"), _client(app, "budi"), _client(app, "ana")
    news = {"keywords": ["pln"], "start": "2026-11-09", "end": "2026-11-09"}
    # everyone signed in may look and download
    for c in (viewer, operator, admin):
        assert c.get("/api/status").status_code == 200 and c.get("/api/results?mode=news").status_code == 200
        assert c.get("/download/news_links_a.xlsx").content == b"PK" and c.get("/api/schedules").status_code == 200
    # a viewer may not change anything
    for path, body in [("/api/news/start", news), ("/api/stop", {}), ("/api/schedules/run", {"id": "x"}),
                       ("/api/lists/create", {"name": "x"}), ("/api/tools/decode", {"name": "a.csv", "data": ""}),
                       ("/api/captcha", {"type": "skip"}), ("/api/logins/cookies", {"cookies": "[]"}),
                       ("/api/users/save", {"username": "x", "role": "viewer"})]:
        r = viewer.post(path, json=body, headers=SITE)
        assert r.status_code == 403 and "viewer" in r.json()["error"], path
    assert viewer.get("/api/audit").status_code == 403 and viewer.get("/api/captcha/screenshot").status_code == 403
    # an operator runs searches, but does not manage users, cookies or the audit log
    assert operator.post("/api/news/start", json=news, headers=SITE).json()["ok"] and started
    for path, body in [("/api/logins/cookies", {"cookies": "[]"}), ("/api/users/save", {"username": "x", "role": "viewer"})]:
        assert operator.post(path, json=body, headers=SITE).status_code == 403
    assert operator.get("/api/users").status_code == 403 and operator.get("/api/audit").status_code == 403
    # an admin does everything
    assert admin.get("/api/users").json()["users"][0]["username"] == "ana"
    assert admin.get("/api/audit").status_code == 200
    acts = [(e["who"], e["action"]) for e in admin.get("/api/audit").json()["entries"]]
    assert ("budi", "started news search") in acts


def test_admin_manages_users_and_sessions_end(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    admin, budi = _client(app, "ana"), _client(app, "budi")
    r = admin.post("/api/users/save", json={"username": "dewi", "role": "viewer", "password": "dewi-password-1"}, headers=SITE)
    assert r.json()["ok"]
    assert admin.post("/api/users/save", json={"username": "ana", "role": "viewer"}, headers=SITE).json()["error"].startswith("You cannot")
    # changing budi's role signs him out at once
    assert budi.get("/api/status").status_code == 200
    admin.post("/api/users/save", json={"username": "budi", "role": "viewer"}, headers=SITE)
    assert budi.get("/api/status").status_code == 401
    budi = _client(app, "budi")
    assert budi.get("/api/me").json()["role"] == "viewer"
    # disabling ends access; a disabled user cannot sign in
    admin.post("/api/users/save", json={"username": "budi", "role": "viewer", "disabled": True}, headers=SITE)
    assert budi.get("/api/status").status_code == 401
    assert _client(app).post("/auth", json={"username": "budi", "password": PW["budi"]}).status_code == 401
    # personal API key: works with that user's role, shown once, replaced by a new one
    key = admin.post("/api/users/apikey", json={"username": "dewi"}, headers=SITE).json()["api_key"]
    raw = TestClient(app)
    assert raw.get("/api/status", headers={"X-API-Key": key}).status_code == 200
    assert raw.post("/api/stop", json={}, headers={"X-API-Key": key}).status_code == 403          # dewi is a viewer
    assert key not in open(tmp_path / "users.json").read()
    key2 = admin.post("/api/users/apikey", json={"username": "dewi"}, headers=SITE).json()["api_key"]
    assert raw.get("/api/status", headers={"X-API-Key": key}).status_code == 401
    assert raw.get("/api/status", headers={"X-API-Key": key2}).status_code == 200
    assert admin.post("/api/users/delete", json={"username": "dewi"}, headers=SITE).json()["ok"]
    assert raw.get("/api/status", headers={"X-API-Key": key2}).status_code == 401


def test_change_own_password(tmp_path, monkeypatch):
    app, _ = _app(tmp_path, monkeypatch)
    c = _client(app, "citra")
    assert c.post("/api/me/password", json={"old": "wrong-one-xyz", "new": "x" * 12}, headers=SITE).json()["ok"] is False
    assert c.post("/api/me/password", json={"old": PW["citra"], "new": "citra-new-pass-2"}, headers=SITE).json()["ok"]
    assert c.get("/api/status").status_code == 401                                   # signed out everywhere
    r = _client(app).post("/auth", json={"username": "citra", "password": "citra-new-pass-2"})
    assert r.status_code == 200


def test_access_keys_still_work_as_admin(tmp_path, monkeypatch):
    key = "emergency-key-0123456789abcdef"
    app, _ = _app(tmp_path, monkeypatch, keys={"admin": key})
    raw = TestClient(app)
    assert raw.get("/api/users", headers={"X-API-Key": key}).status_code == 200
    c = _client(app)
    r = c.post("/auth", json={"key": key})
    assert r.json()["role"] == "admin" and c.get("/api/me").json()["password"] is False


def test_add_user_from_the_command_line(tmp_path):
    env = {**os.environ, "CRAWLER_HOME": str(tmp_path), "NEW_PASSWORD": "first-admin-pass"}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, os.path.join(root, "serve.py"), "add-user", "Ana", "admin"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["ana", "admin", "first-admin-pass"], out.stderr
    assert Users(str(tmp_path / "users.json")).check_login("ana", "first-admin-pass") == "ana"
    bad = subprocess.run([sys.executable, os.path.join(root, "serve.py"), "add-user", "x y"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0 and "Username" in bad.stderr
