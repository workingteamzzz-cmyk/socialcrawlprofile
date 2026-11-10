"""The FastAPI server (serve.py): key on everything, dashboard sign-in, no cross-site changes, docs, cookie import."""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

KEY = "test-key-0123456789abcdef"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from crawler import api, job, server
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(api, "OUTPUT_DIR", str(tmp_path / "out"))
    fresh = job.Job()
    monkeypatch.setattr(api, "JOB", fresh)
    os.makedirs(tmp_path / "out")
    (tmp_path / "out" / "news_links_x.xlsx").write_bytes(b"PK fake")
    c = TestClient(api.create_app(KEY), base_url="http://collector.example")
    c.job = fresh
    return c


def test_everything_needs_the_key(client):
    assert client.get("/api/health").json()["ok"] is True                    # open: for uptime checks
    for path in ("/api/status", "/api/results?mode=news", "/api/outputs", "/api/lists", "/api/logins",
                 "/download/news_links_x.xlsx", "/docs", "/openapi.json"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert client.get(path, headers={"X-API-Key": "wrong-key-wrong-key"}).status_code in (401, 429), path
    assert client.post("/api/stop", json={}).status_code == 401
    assert "Sign in to continue" in client.get("/").text                       # sign-in page, not the dashboard


def test_key_header_and_bearer_work(client):
    h = {"X-API-Key": KEY}
    assert client.get("/api/status", headers=h).json()["running"] is False
    assert client.get("/api/outputs", headers={"Authorization": f"Bearer {KEY}"}).json()["files"][0]["name"] == "news_links_x.xlsx"
    assert client.get("/download/news_links_x.xlsx", headers=h).content == b"PK fake"
    assert client.get("/download/..%2F..%2Fetc%2Fpasswd", headers=h).status_code == 404
    spec = client.get("/openapi.json", headers=h).json()
    assert "/api/news/start" in spec["paths"] and "/api/logins/cookies" in spec["paths"]
    assert "X-API-Key" in json.dumps(spec["components"]["securitySchemes"])
    # a bad request body is explained, not a crash
    r = client.post("/api/news/start", headers=h, json={"keywords": ["x"]})
    assert r.status_code == 422


def test_dashboard_sign_in_cookie_and_cross_site_protection(client):
    assert client.post("/auth", json={"key": "nope-nope-nope-nope"}).status_code == 401
    r = client.post("/auth", json={"key": KEY})
    assert r.status_code == 200 and "plc_session" in r.cookies and KEY not in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    assert '<nav id="nav"' in client.get("/").text or "Keyword Search" in client.get("/").text
    assert client.get("/api/status").status_code == 200
    assert client.get("/docs").status_code == 200
    # changes with the cookie only from this app's own page
    same = {"Origin": "http://collector.example"}
    assert client.post("/api/stop", json={}, headers=same).json()["ok"] is True
    assert client.post("/api/stop", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/stop", json={}).status_code == 403                      # no origin at all
    client.post("/logout")
    client.cookies.clear()
    assert client.get("/api/status").status_code == 401


def test_too_many_wrong_keys_are_refused_for_a_while(client):
    for _ in range(10):
        client.get("/api/status", headers={"X-API-Key": "guess-guess-guess-guess"})
    assert client.get("/api/status", headers={"X-API-Key": KEY}).status_code == 429


def test_server_mode_has_no_login_window_and_no_folder_scan(tmp_path, monkeypatch):
    from crawler import api, job, paths
    monkeypatch.setattr(paths, "SERVER_MODE", True)
    fresh = job.Job()
    monkeypatch.setattr(api, "JOB", fresh)
    c = TestClient(api.create_app(KEY), base_url="http://collector.example")
    h = {"X-API-Key": KEY}
    assert c.get("/api/logins", headers=h).json()["server"] is True
    r = c.post("/api/login", headers=h, json={}).json()
    assert r["ok"] is False and "Import the cookies" in r["error"]
    assert c.get("/api/old-profiles", headers=h).status_code == 404               # don't scan the server's folders
    assert c.post("/api/logins/import", headers=h, json={"path": "/"}).status_code == 404


def test_cookie_formats():
    from crawler.cookies import CookieError, parse_cookies
    future = time.time() + 86400 * 30
    editor = json.dumps([
        {"domain": ".instagram.com", "name": "sessionid", "value": "abc", "path": "/", "expirationDate": future,
         "httpOnly": True, "secure": True, "sameSite": "no_restriction"},
        {"domain": ".bank.example", "name": "secret", "value": "zzz"},                      # other sites: ignored
        {"domain": ".x.com", "name": "auth_token", "value": "t", "session": True, "sameSite": "lax"},
        {"domain": ".tiktok.com", "name": "old", "value": "o", "expirationDate": time.time() - 10},   # expired
    ])
    got = parse_cookies(editor)
    assert [(c["domain"], c["name"]) for c in got] == [(".instagram.com", "sessionid"), (".x.com", "auth_token")]
    assert got[0]["sameSite"] == "None" and got[0]["httpOnly"] and "expires" not in got[1] and got[1]["sameSite"] == "Lax"
    state = json.dumps({"cookies": [{"domain": ".facebook.com", "name": "c_user", "value": "1", "path": "/",
                                     "expires": -1, "httpOnly": False, "secure": True, "sameSite": "Lax"}], "origins": []})
    assert parse_cookies(state)[0]["name"] == "c_user"
    txt = ("# Netscape HTTP Cookie File\n"
           f"#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t{int(future)}\tsessionid\tsss\n"
           f".threads.com\tTRUE\t/\tTRUE\t{int(future)}\tsessionid\tttt\n")
    got = parse_cookies(txt)
    assert [c["domain"] for c in got] == [".tiktok.com", ".threads.com"] and got[0]["httpOnly"] is True
    for bad in ("", "{not json", '[{"domain": ".bank.example", "name": "a", "value": "b"}]'):
        with pytest.raises(CookieError):
            parse_cookies(bad)


@pytest.mark.skipif(not os.path.exists("/opt/pw-browsers/chromium") and not os.environ.get("CRAWLER_BROWSER_PATH"),
                    reason="no local Chromium")
def test_cookie_import_signs_the_profile_in(tmp_path, monkeypatch):
    from crawler import api, job
    from tests.test_crawler import _browser_path
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    monkeypatch.setattr(job, "PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(job, "LOGINS_FILE", str(tmp_path / "logins.json"))
    fresh = job.Job()
    monkeypatch.setattr(api, "JOB", fresh)
    c = TestClient(api.create_app(KEY), base_url="http://collector.example")
    future = time.time() + 86400 * 30
    cookies = [{"domain": ".instagram.com", "name": "sessionid", "value": "abc", "path": "/", "expirationDate": future,
                "httpOnly": True, "secure": True},
               {"domain": ".x.com", "name": "auth_token", "value": "t", "path": "/", "expirationDate": future, "secure": True}]
    r = c.post("/api/logins/cookies", headers={"X-API-Key": KEY}, json={"cookies": cookies}).json()
    assert r["ok"] and r["added"] == 2, r
    assert r["platforms"]["instagram"] and r["platforms"]["x"] and not r["platforms"]["tiktok"]
    assert json.load(open(tmp_path / "logins.json"))["platforms"]["instagram"] is True
    r = c.post("/api/logins/cookies", headers={"X-API-Key": KEY}, json={"cookies": "garbage"}).json()
    assert r["ok"] is False and r["error"]


# ---------------- hardened server ----------------

ANA, BUDI = "ana-key-0123456789abcdef", "budi-key-0123456789abcdef"


def _team(tmp_path, monkeypatch, **kw):
    from crawler import api, job
    fresh = job.Job()
    monkeypatch.setattr(api, "JOB", fresh)
    app = api.create_app({"ana": ANA, "budi": BUDI}, audit_path=str(tmp_path / "audit.log"), **kw)
    return app


def test_a_key_per_person_audit_log_and_lock_out(tmp_path, monkeypatch):
    from crawler import api
    app = _team(tmp_path, monkeypatch)
    c = TestClient(app, base_url="http://collector.example")
    assert c.get("/api/logins", headers={"X-API-Key": BUDI}).json()["who"] == "budi"
    r = c.post("/auth", json={"key": ANA})
    assert r.json()["who"] == "ana" and r.cookies["plc_session"].startswith("ana.")
    same = {"Origin": "http://collector.example"}
    assert c.post("/api/stop", json={}, headers=same).json()["ok"]
    cookies = [{"domain": ".x.com", "name": "auth_token", "value": "SECRET-COOKIE-VALUE", "path": "/",
                "expirationDate": time.time() + 9e5}]
    monkeypatch.setattr(api.JOB, "import_cookies", lambda text: {"ok": True, "added": 1, "platforms": {"x": True}})
    c.post("/api/logins/cookies", json={"cookies": cookies}, headers=same)
    TestClient(app).get("/api/status", headers={"X-API-Key": "wrong-wrong-wrong-wrong"})
    log = open(tmp_path / "audit.log").read()
    assert "SECRET-COOKIE-VALUE" not in log and ANA not in log and BUDI not in log          # never secrets
    entries = c.get("/api/audit").json()["entries"]
    acts = [(e["who"], e["action"]) for e in entries]
    assert acts[:4] == [("?", "rejected"), ("ana", "imported cookies"), ("ana", "stopped the job"), ("ana", "signed in")]
    assert entries[1]["signed_in"] == ["x"]
    # ana's key is removed from the settings (restart): her cookie and key stop working, budi's still work
    app2 = api.create_app({"budi": BUDI}, audit_path=str(tmp_path / "audit.log"))
    c2 = TestClient(app2, base_url="http://collector.example")
    c2.cookies.set("plc_session", r.cookies["plc_session"])
    assert c2.get("/api/status").status_code == 401
    assert c2.get("/api/status", headers={"X-API-Key": ANA}).status_code == 401
    assert c2.get("/api/status", headers={"X-API-Key": BUDI}).status_code == 200


def test_sign_in_expires_and_cannot_be_forged(tmp_path, monkeypatch):
    from crawler import api
    app = _team(tmp_path, monkeypatch, session_days=1)
    c = TestClient(app, base_url="http://collector.example")
    old = api._session_value("ana", ANA, int(time.time()) - 2 * 86400)
    c.cookies.set("plc_session", old)
    assert c.get("/api/status").status_code == 401                                  # expired
    forged = api._session_value("ana", "not-anas-key-0123456789", int(time.time()))
    c.cookies.set("plc_session", forged)
    assert c.get("/api/status").status_code == 401                                  # wrong signature
    c.cookies.set("plc_session", api._session_value("ana", ANA))
    assert c.get("/api/status").status_code == 200


def test_allowed_addresses_and_security_headers(tmp_path, monkeypatch):
    app = _team(tmp_path, monkeypatch, allowed_ips=["203.0.113.0/24"], secure_cookie=True)
    office = TestClient(app, base_url="https://collector.example", client=("203.0.113.7", 5000))
    other = TestClient(app, base_url="https://collector.example", client=("198.51.100.9", 5000))
    assert other.get("/api/health").status_code == 200                       # uptime checks still work
    for path in ("/", "/api/status", "/docs"):
        assert other.get(path, headers={"X-API-Key": ANA}).status_code == 403, path
    assert other.post("/auth", json={"key": ANA}).status_code == 403
    r = office.get("/api/status", headers={"X-API-Key": ANA})
    assert r.status_code == 200
    h = r.headers
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and "max-age" in h["strict-transport-security"]
    assert h["cache-control"] == "no-store"
    page = office.get("/", headers={"X-API-Key": ANA})
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    r = office.post("/auth", json={"key": ANA})
    assert "secure" in r.headers["set-cookie"].lower()


def test_old_results_and_page_copies_are_deleted(tmp_path):
    from crawler.retention import purge
    out = tmp_path / "outputs"
    (out / "debug").mkdir(parents=True)
    now = time.time()
    files = {"news_links_old.xlsx": 40, "news_links_new.xlsx": 5, "debug/page_old.html": 10, "debug/page_new.html": 2}
    for name, age_days in files.items():
        p = out / name
        p.write_text("x")
        os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    assert purge(str(out), 30, 7, now=now) == 2
    left = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
    assert left == ["debug/page_new.html", "news_links_new.xlsx"]
    assert purge(str(out), 0, 0, now=now) == 0                                   # 0 = keep


def test_server_settings_are_checked():
    import serve
    assert serve.read_keys({"COLLECTOR_API_KEYS": f"Ana={ANA}, budi={BUDI}"}) == {"ana": ANA, "budi": BUDI}
    assert serve.read_keys({"COLLECTOR_API_KEY": ANA}) == {"admin": ANA}
    for bad in ({"COLLECTOR_API_KEYS": "ana=short"}, {"COLLECTOR_API_KEYS": f"ana={ANA},budi={ANA}"},
                {"COLLECTOR_API_KEYS": f"bad name={ANA}"}, {"COLLECTOR_API_KEYS": ANA}):
        with pytest.raises(SystemExit):
            serve.read_keys(bad)
