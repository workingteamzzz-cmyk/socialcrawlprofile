"""The team's API scraper (Social Scraper v58) run from the dashboard, against a fake scraping API."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("pandas")
pytest.importorskip("fastapi")
import openpyxl  # noqa: E402

from crawler.apiscraper import runner, settings  # noqa: E402

NOW = int(time.time()) - 3600
POSTS = [
    {"aweme_id": "7705797219735129365", "desc": "Mati listrik lagi, kecewa banget sama pelayanan PLN", "create_time": NOW,
     "author": {"unique_id": "kaltimpostnews", "uid": "7370322159623865350", "nickname": "Kaltim Post",
                "avatar_thumb": {"url_list": ["https://p16-sign.tiktokcdn.com/a.jpeg"]}, "follower_count": 5100},
     "statistics": {"digg_count": 10, "comment_count": 2, "share_count": 1, "play_count": 1000}},
    {"aweme_id": "7705797219735129366", "desc": "Alhamdulillah listrik sudah normal, terima kasih petugas PLN",
     "create_time": NOW - 60, "author": {"unique_id": "warga_bahagia", "uid": "123",
                                         "avatar_thumb": {"url_list": ["https://p16-sign.tiktokcdn.com/b.jpeg"]}},
     "statistics": {"digg_count": 50, "comment_count": 5, "share_count": 3, "play_count": 4000}},
]


@pytest.fixture
def fake_api(tmp_path, monkeypatch):
    calls = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("x-api-key")))
            if "credit-balance" in self.path:
                body = {"creditCount": 1000}
            elif "/tiktok/search/keyword" in self.path:
                body = {"search_item_list": [{"aweme_info": p} for p in POSTS]}
            else:
                body = {"success": True, "data": []}
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "_path", lambda: str(tmp_path / "api_scraper.json"))
    eng = runner.engine()
    monkeypatch.setattr(eng, "BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    yield calls
    srv.shutdown()


def test_keys_stay_on_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_path", lambda: str(tmp_path / "api_scraper.json"))
    assert settings.public()["api_key_set"] is False
    pub = settings.save(api_key="secret-key-abcd", twitter_api_key="tw-key-9876", default_budget=50)
    assert pub == {"api_key_set": True, "api_key_tail": "…abcd", "twitter_set": True, "twitter_tail": "…9876",
                   "default_budget": 50}
    assert oct(os.stat(tmp_path / "api_scraper.json").st_mode)[-3:] == "600"
    assert settings.save(clear_twitter=True)["twitter_set"] is False and settings.load()["api_key"] == "secret-key-abcd"


def test_run_gives_the_team_excel_with_ids_and_emotion(tmp_path, fake_api):
    settings.save(api_key="test-key-123")
    logs, prog = [], []
    files = runner.run({"mode": "keyword", "platform": "TikTok (keyword)", "queries": "listrik", "limit": 10, "budget": 20},
                       logs.append, lambda d, t, lbl: prog.append((d, t, lbl)), threading.Event(),
                       str(tmp_path / "out"), str(tmp_path / "data"))
    assert any(f.endswith(".xlsx") for f in files) and any(f.endswith(".csv") for f in files)
    assert all(k == "test-key-123" for _, k in fake_api)
    wb = openpyxl.load_workbook(tmp_path / "out" / [f for f in files if f.endswith(".xlsx")][0])
    assert wb.sheetnames == ["Sheet1", "Rekap", "Codebook"]
    ws = wb["Sheet1"]
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    by_user = {r["Username"]: r for r in rows}
    k = by_user["kaltimpostnews"]
    assert str(k["User ID"]) == "7370322159623865350" and k["Follower"] in (5100, "5100")
    assert k["Sentiment"] == "negative" and k["Emotion"] and k["User Image"]
    assert by_user["warga_bahagia"]["Sentiment"] == "positive"
    assert logs and prog


def test_stop_ends_the_run(tmp_path, fake_api):
    settings.save(api_key="test-key-123")
    stop = threading.Event()
    stop.set()
    runner.run({"mode": "keyword", "platform": "TikTok (keyword)", "queries": "listrik\npln\ngangguan", "limit": 10},
               lambda _: None, lambda *a: None, stop, str(tmp_path / "out"), str(tmp_path / "data"))
    assert not [p for p, _ in fake_api if "/search/" in p]          # stopped before any search request


def test_no_key_no_run(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_path", lambda: str(tmp_path / "none.json"))
    with pytest.raises(RuntimeError, match="API settings"):
        runner.run({"queries": "x"}, print, lambda *a: None, threading.Event(), str(tmp_path), str(tmp_path))


def test_roles_for_the_api_scraper(tmp_path, monkeypatch):
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(settings, "_path", lambda: str(tmp_path / "api_scraper.json"))
    app, _ = _app(tmp_path, monkeypatch)
    admin, op, viewer = _client(app, "ana"), _client(app, "budi"), _client(app, "citra")
    assert viewer.get("/api/apiscraper/info").status_code == 403
    assert op.get("/api/apiscraper/settings").status_code == 403
    assert op.post("/api/apiscraper/settings", json={"api_key": "x"}, headers=SITE).status_code == 403
    info = op.get("/api/apiscraper/info").json()
    assert info["ready"] is False and "ALL platforms" in info["platforms"]
    assert "API settings" in op.post("/api/apiscraper/start", json={"queries": "pln"}, headers=SITE).json()["error"]
    r = admin.post("/api/apiscraper/settings", json={"api_key": "sk-live-000011112222"}, headers=SITE).json()
    assert r["ok"] and r["api_key_tail"] == "…2222"
    got = admin.get("/api/apiscraper/settings").text
    assert "sk-live-000011112222" not in got and "…2222" in got
    assert op.get("/api/apiscraper/info").json()["ready"] is True
