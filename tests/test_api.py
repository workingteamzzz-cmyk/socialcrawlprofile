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
    assert "Enter the server's access key" in client.get("/").text            # sign-in page, not the dashboard


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
