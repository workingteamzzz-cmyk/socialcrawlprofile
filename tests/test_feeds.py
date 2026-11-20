"""Client feeds: a matching article read by the stream reaches the client's server within one sender round; a down
server makes the articles wait (outbox), nothing is lost; signed with the token."""
import hashlib
import hmac
import http.server
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Sink(http.server.BaseHTTPRequestHandler):
    got: list = []
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Sink.got.append((dict(self.headers), body))
        self.send_response(Sink.status)
        self.end_headers()


@pytest.fixture
def sink():
    Sink.got, Sink.status = [], 200
    srv = http.server.HTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/terima"
    srv.shutdown()


def test_realtime_push_with_retry(tmp_path, monkeypatch, sink):
    from crawler import feeds, paths, stream
    from crawler.medmas import extract
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    s.cfg = stream.settings()
    s.cfg["max_age_hours"] = 24 * 3650
    with pytest.raises(ValueError):
        feeds.save({"name": "PLN", "query": "pln", "url": "ftp://x"})
    f = feeds.save({"name": "PLN Jabar", "query": "pln AND (padam OR gangguan) NOT promo", "url": sink, "token": "rahasia"})
    assert f["token_set"] and "token" not in f and "rahasia" not in json.dumps(feeds.public())

    def fake(url, timeout=20):
        promo = "promo" in url
        text = ("Listrik PLN padam di Bogor sejak pagi, warga mengeluh. " + ("Ada promo tambah daya. " if promo else "")) * 6
        return {"url": url, "media": "a.id", "title": "PLN padam di Bogor", "author": "Rina", "date": "2026-11-20",
                "time": "08:15", "summary": text[:80], "content": text, "image": "https://a.id/i.jpg", "influencer": "",
                "persons": "Kami, Ahmad Sutrisno", "organizations": "", "method": "", "error": "", "is_index": False,
                "is_article": True}
    monkeypatch.setattr(extract, "extract_article", fake)
    for k, u in (("a", "https://a.id/berita/pln-padam-bogor"), ("b", "https://a.id/berita/pln-promo-tambah-daya")):
        s.x("INSERT INTO articles(key, url, domain, discovered) VALUES (?,?,?,?)", (k, u, "a.id", time.time()))
        s.read({"key": k, "url": u, "domain": "a.id", "title": "", "feed_date": "", "feed_time": ""})
    assert s.q("SELECT COUNT(*) n FROM feed_outbox")[0]["n"] == 1           # the promo article does not match
    Sink.status = 503                                                       # the client's server is down
    assert feeds.send_due(s) == 0
    row = s.q("SELECT tries, next_try FROM feed_outbox")[0]
    assert row["tries"] == 1 and row["next_try"] > time.time() + 20 and "503" in feeds.public()[0]["last_error"]
    Sink.status = 200
    s.x("UPDATE feed_outbox SET next_try=0")
    assert feeds.send_due(s) == 1 and s.q("SELECT COUNT(*) n FROM feed_outbox")[0]["n"] == 0
    headers, body = Sink.got[-1]
    sig = "sha256=" + hmac.new(b"rahasia", body, hashlib.sha256).hexdigest()
    assert headers["X-Collector-Signature"] == sig and headers["Authorization"] == "Bearer rahasia"
    d = json.loads(body)
    a = d["articles"][0]
    assert d["event"] == "articles" and d["feed"]["name"] == "PLN Jabar" and d["count"] == 1
    assert a["title"] == "PLN padam di Bogor" and a["date"] == "2026-11-20" and a["content"].startswith("<p>")
    assert a["person"] == "ahmad sutrisno" and a["sentiment"] in ("negative", "neutral", "positive")
    assert feeds.public()[0]["sent"] == 1 and feeds.test(f["id"])["ok"]
    assert stat_mode(feeds._path()) == 0o600
    s.stop()


def stat_mode(p):
    import stat
    return stat.S_IMODE(os.stat(p).st_mode)


def test_endpoints_admin_only(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths, stream
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "_STREAM", None)
    app, _ = _app(tmp_path, monkeypatch)
    op, admin = _client(app, "budi"), _client(app, "ana")
    body = {"name": "Klien", "query": "banjir", "url": "https://api.klien.id/x", "token": "t"}
    assert op.post("/api/feeds/save", json=body, headers=SITE).status_code == 403
    assert admin.post("/api/feeds/save", json=body, headers=SITE).json()["ok"]
    got = admin.get("/api/feeds").json()["feeds"]
    assert got[0]["name"] == "Klien" and got[0]["token_set"] and "token" not in got[0] and got[0]["waiting"] == 0
    assert admin.post("/api/feeds/delete", json={"id": got[0]["id"]}, headers=SITE).json()["ok"]
    stream.get().stop()



def test_every_article_and_keyword_lists(tmp_path, monkeypatch):
    from crawler import feeds, paths
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    assert feeds.normalize_query("pln, listrik padam\ngardu induk") == 'pln OR "listrik padam" OR "gardu induk"'
    assert feeds.normalize_query("pln AND (padam, gangguan)") == "pln AND (padam, gangguan)"   # a real query: as typed
    every = feeds.save({"name": "Semua", "all": True, "url": "https://x.id/hook"})
    assert every["all"] and every["query"] == ""
    kw = feeds.save({"name": "Kata kunci", "query": "banjir, tanah longsor", "url": "https://x.id/hook"})
    assert kw["query"] == 'banjir OR "tanah longsor"'

    class S:
        import threading
        lock = threading.RLock()
        rows = []

        class _db:
            @staticmethod
            def executemany(sql, rows):
                S.rows += rows

            @staticmethod
            def commit():
                pass
    monkeypatch.setattr(feeds, "article_body", lambda *a: {"title": "x"})
    job = {"key": "k", "url": "https://a.id/berita/harga-cabai-naik"}
    n = feeds.match_and_queue(S, job, {"title": "Harga cabai naik", "content": "Harga cabai naik lagi."}, "", "", "")
    assert n == 1                                              # no keyword in it: only the every-article feed
    n = feeds.match_and_queue(S, job, {"title": "Tanah longsor di Garut", "content": "Tanah longsor."}, "", "", "")
    assert n == 2
