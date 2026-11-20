"""News Stream reader / client API: query with AND / OR / NOT, topic, dates, sentiment counts, one article."""
import os
import sys
import time
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def rd(tmp_path, monkeypatch):
    from crawler import insights, paths, reader, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    now = time.time()
    arts = [("a.id", "YBM PLN Berbagi Kebahagiaan dengan Santri", "positive", "2026-11-20", "22:30"),
            ("b.id", "PLN Padam Lagi, Warga Bekasi Mengeluh", "negative", "2026-11-20", "21:00"),
            ("c.id", "Gangguan Listrik PLN di Bogor Pulih", "neutral", "2026-11-19", "08:00"),
            ("d.id", "Harga Cabai Naik Jelang Natal", "neutral", "2026-11-20", "10:00"),
            ("e.id", "PLN Promo Tambah Daya", "positive", "2026-11-20", "09:00")]
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, title, status, discovered, fetched, pub_date, pub_time, "
                          "summary, content, sentiment, image, persons) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [(f"{d}/{i}", f"https://{d}/berita/{i}", d, t, "success", now, now + i, day, tm, t + ".",
                            zlib.compress(f"{t}.\n\nParagraf kedua tentang {t.lower()}.".encode()), sen,
                            f"https://{d}/img{i}.jpg", "Kami, Presiden Prabowo Subianto")
                           for i, (d, t, sen, day, tm) in enumerate(arts)])
        s._db.execute("INSERT INTO sources(id, kind, domain, url, next_check, interval, tier) VALUES "
                      "('sm:a.id','sitemap','a.id','',0,3600,'active')")
        s._db.commit()
    insights.index_new()
    yield reader
    s.stop()


def test_query_dates_sentiment_and_one_article(rd):
    r = rd.articles(q="PLN NOT promo")
    assert r["total"] == 3 and r["sentiment"]["negative"]["n"] == 1 and r["sentiment"]["positive"]["pct"] == 33.3
    assert [x["domain"] for x in r["rows"]] == ["a.id", "b.id", "c.id"] and r["rows"][0]["tier"] == "active"
    assert rd.articles(q="PLN AND (padam OR gangguan)")["total"] == 2
    assert rd.articles(q="PLN", start="2026-11-20", end="2026-11-20", sentiment="positive")["matching"] == 2
    assert rd.articles(sort="oldest")["rows"][0]["domain"] == "c.id" and rd.articles()["total"] == 5
    one = rd.article(r["rows"][1]["key"])
    assert one["paragraphs"][1].startswith("Paragraf kedua") and one["persons"] == "Prabowo Subianto"
    with pytest.raises(ValueError):
        rd.articles(q="NOT pln")


def test_topic_and_endpoint(rd, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import archive
    from tests.test_users import _app, _client
    t = archive.save_topic("PLN", "pln, listrik padam")
    assert rd.articles(topic=t["id"])["total"] == 4
    app, _ = _app(tmp_path, monkeypatch)
    r = _client(app, "citra").get("/api/articles", params={"q": "pln -promo", "sentiment": "negative"}).json()
    assert r["ok"] and r["matching"] == 1 and r["rows"][0]["title"].startswith("PLN Padam")
    assert _client(app, "citra").get("/api/articles/one", params={"key": r["rows"][0]["key"]}).json()["ok"]


def test_client_dashboard_with_api_key_and_cors(rd, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler.users import Users
    from tests.test_users import _app
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CLIENT_ORIGINS", "https://dashboard.klien.id")
    app, _ = _app(tmp_path, monkeypatch)
    key = Users(str(tmp_path / "users.json")).new_api_key("citra")      # a viewer's personal API key
    c = TestClient(app)
    assert c.get("/api/articles", params={"q": "pln"}).status_code == 401           # no key: refused
    r = c.get("/api/articles", params={"q": "pln"}, headers={"X-API-Key": key, "Origin": "https://dashboard.klien.id"})
    assert r.json()["ok"] and r.headers.get("access-control-allow-origin") == "https://dashboard.klien.id"
    bad = c.get("/api/articles", headers={"X-API-Key": key, "Origin": "https://lain.com"})
    assert "access-control-allow-origin" not in bad.headers


def test_all_databases_stream_plus_news_search(rd):
    """News Search / Keyword runs (the reporting database) show in the reader too; a link in both shows once."""
    from datetime import timedelta, timezone

    from crawler import archive, insights
    tz = timezone(timedelta(hours=7))
    archive.add_rows("online", [
        {"url": "https://www.f.id/berita/pln-tarif", "title": "PLN Umumkan Tarif Listrik Desember", "media": "f.id",
         "published": "2026-11-20", "published_full": "2026-11-20 12:00", "text": "PLN mengumumkan tarif.\nParagraf dua.",
         "description": "Tarif baru.", "sentiment": "Positive", "keyword": "pln"},
        {"url": "https://b.id/1", "title": "PLN Padam Lagi, Warga Bekasi Mengeluh", "media": "b.id",
         "published": "2026-11-20", "text": "sama", "keyword": "pln"},           # also read by the stream
        {"url": "https://g.id/old", "title": "PLN 2025", "media": "g.id", "published": "2025-01-05", "text": "x",
         "keyword": "pln"}], tz)
    assert insights.index_reports() == 3
    every = rd.articles(q="PLN")
    assert every["total"] == 6 and every["source"] == "all"                 # 4 stream + 2 new from News Search
    assert [x["origin"] for x in every["rows"]].count("reports") == 2
    assert rd.articles(q="PLN", source="stream")["total"] == 4
    assert rd.articles(q="PLN", source="reports")["total"] == 3
    day = rd.articles(q="tarif", start="2026-11-20", end="2026-11-20")
    assert day["total"] == 1 and day["sentiment"]["positive"]["n"] == 1 and day["rows"][0]["domain"] == "f.id"
    one = rd.article(day["rows"][0]["key"])
    assert one["origin"] == "reports" and one["title"].startswith("PLN Umumkan") and one["paragraphs"]
    assert rd.articles(start="2025-01-01", end="2025-12-31")["total"] == 1   # no keyword: every article of a period
    with archive._LOCK:                                                      # deleted (Exports > free up space)
        c = archive._db()
        c.execute("DELETE FROM items WHERE day < '2026-01-01'")
        c.commit()
        c.close()
    insights.prune_reports()
    assert rd.articles(start="2025-01-01", end="2025-12-31")["total"] == 0


def test_client_api_key_sees_only_its_own_news(rd, tmp_path, monkeypatch):
    """A client key: only the client's query, can only narrow it, no other endpoint, rate limit, pause / new key."""
    pytest.importorskip("fastapi")
    from crawler import client_api
    from tests.test_users import SITE, _app, _client
    app, _ = _app(tmp_path, monkeypatch)
    admin = _client(app, "ana")
    assert _client(app, "citra").post("/api/clients/save", json={"name": "x", "query": "pln"}, headers=SITE).status_code == 403
    r = admin.post("/api/clients/save", json={"name": "PLN UID", "query": "pln, padam", "rate": 6}, headers=SITE).json()
    assert r["ok"] and r["key"].startswith("wtc_") and r["client"]["query"] == "pln OR padam"
    key, cid = r["key"], r["client"]["id"]
    assert key not in (tmp_path / "data" / "client_api.json").read_text()        # only its hash is kept
    assert oct(os.stat(tmp_path / "data" / "client_api.json").st_mode)[-3:] == "600"
    c = _client(app)
    h = {"X-Client-Key": key}
    assert c.get("/api/client/articles").status_code == 401
    got = c.get("/api/client/articles", headers=h).json()
    assert got["ok"] and got["total"] == 4 and all("pln" in x["title"].lower() for x in got["rows"])
    assert "matched" not in got["rows"][0] and "tier" not in got["rows"][0]   # other clients' topics never show
    assert c.get("/api/client/articles", params={"q": "bogor"}, headers=h).json()["total"] == 1      # narrows
    assert c.get("/api/client/articles", params={"q": "x) OR (cabai"}, headers=h).json().get("total", 0) == 0   # never widens
    cabai = rd.articles(q="cabai")["rows"][0]["key"]
    assert c.get("/api/client/articles/one", params={"key": cabai}, headers=h).json()["ok"] is False
    mine = got["rows"][0]["key"]
    one = c.get("/api/client/articles/one", params={"key": mine}, headers=h).json()
    assert one["ok"] and one["paragraphs"] and "matched" not in one
    for path in ("/api/articles", "/api/outputs", "/download/news_links_a.xlsx", "/api/dashboard"):
        assert c.get(path, headers={**h, "X-API-Key": key}).status_code in (401, 403), path
    r = c.get("/api/client/me", headers={**h, "Origin": "https://app.klien.id"})
    assert r.json()["query"] == "pln OR padam" and r.headers["access-control-allow-origin"] == "https://app.klien.id"
    assert c.options("/api/client/articles", headers={"Origin": "https://app.klien.id",
                                                      "Access-Control-Request-Method": "GET"}).status_code == 204
    assert c.get("/api/client/me", headers=h).status_code == 429                 # 6 per minute: the 7th waits
    client_api._HITS.clear()
    new = admin.post("/api/clients/rotate", json={"id": cid}, headers=SITE).json()["key"]
    assert c.get("/api/client/me", headers=h).status_code == 401 and c.get(
        "/api/client/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200
    item = admin.get("/api/clients").json()["clients"][0]
    assert admin.post("/api/clients/save", json={**item, "enabled": False, "origins": "https://app.klien.id"}, headers=SITE).json()["ok"]
    assert c.get("/api/client/me", headers={"X-Client-Key": new}).status_code == 401           # paused
    admin.post("/api/clients/save", json={**item, "enabled": True, "origins": "https://app.klien.id"}, headers=SITE)
    assert c.get("/api/client/me", headers={"X-Client-Key": new, "Origin": "https://lain.com"}).status_code == 403
    assert admin.post("/api/clients/save", json={**item, "expires": "2020-01-01"}, headers=SITE).json()["ok"]
    assert c.get("/api/client/me", headers={"X-Client-Key": new}).status_code == 401           # expired
    assert admin.post("/api/clients/delete", json={"id": cid}, headers=SITE).json()["ok"] and not client_api.public()


def test_time_windows_and_late_arrivals(rd):
    """Hourly / 6-hour / 2-day pulls: start and end to the minute (WIB), last_hours, and received_since — what reached
    the server after a moment, late articles too (pass the previous answer's server_time)."""
    import time as _t
    assert rd.articles(start="2026-11-20 20:00")["total"] == 2                 # 22:30 and 21:00
    assert rd.articles(start="2026-11-20T09:30", end="2026-11-20 21:00")["total"] == 2    # 21:00 and 10:00
    assert rd.articles(start="2026-11-19", end="2026-11-19")["total"] == 1
    with pytest.raises(ValueError):
        rd.articles(start="20 Nov")
    r = rd.articles()
    assert r["server_time"] and r["rows"][0]["received_at"].endswith("+07:00") and r["pages"] == 1
    fetched = sorted(x["fetched"] for x in r["rows"])
    since = fetched[2] - 0.5                                                   # the last three arrivals
    assert rd.articles(received_since=str(since))["total"] == 3
    assert rd.articles(received_since="2000-01-01T00:00:00+07:00")["total"] == 5     # ISO with offset
    assert rd.articles(sort="received")["rows"][0]["fetched"] == fetched[-1]
    assert rd.articles(last_hours=24 * 365 * 5)["total"] == 5 and rd.articles(last_hours=0.001)["total"] == 0
    assert rd.articles(received_since=_t.time() + 3600)["total"] == 0


def test_client_api_social_media_posts(rd, tmp_path, monkeypatch):
    """A key with social media: the client's posts and comments (Keyword Search / Collect / API runs), filtered by its
    query, platform, time and arrival; a key without it gets 403; deleted rows leave the index."""
    pytest.importorskip("fastapi")
    from datetime import timedelta, timezone

    from crawler import archive, insights
    from tests.test_users import SITE, _app, _client
    tz = timezone(timedelta(hours=7))
    archive.add_rows("medsos", [
        {"platform": "tiktok", "url": "https://www.tiktok.com/@warga/video/1", "author": "warga", "text":
            "Listrik padam lagi, PLN kapan normal?", "date": "2026-11-20", "time": "08:15", "likes": 120,
         "comments": 4, "views": 5000, "keyword": "pln"},
        {"platform": "x", "url": "https://x.com/budi/status/2", "author": "budi", "text": "PLN cepat tanggap, mantap",
         "date": "2026-11-20", "time": "21:40", "likes": 3, "keyword": "pln"},
        {"platform": "instagram", "url": "https://www.instagram.com/p/abc/", "author": "kuliner", "text":
            "Resep sambal enak", "date": "2026-11-20", "time": "10:00", "keyword": "sambal"}], tz)
    assert insights.index_social() == 3
    app, _ = _app(tmp_path, monkeypatch)
    admin = _client(app, "ana")
    news_only = admin.post("/api/clients/save", json={"name": "A", "query": "pln"}, headers=SITE).json()["key"]
    both = admin.post("/api/clients/save", json={"name": "B", "query": "pln", "social": True}, headers=SITE).json()["key"]
    social_only = admin.post("/api/clients/save", json={"name": "C", "query": "pln", "news": False, "social": True},
                             headers=SITE).json()["key"]
    assert not admin.post("/api/clients/save", json={"name": "D", "query": "pln", "news": False},
                          headers=SITE).json()["ok"]
    c = _client(app)
    assert c.get("/api/client/posts", headers={"X-Client-Key": news_only}).status_code == 403
    r = c.get("/api/client/posts", headers={"X-Client-Key": both}).json()
    assert r["ok"] and r["total"] == 2 and {x["username"] for x in r["rows"]} == {"warga", "budi"}   # not the sambal one
    p = r["rows"][-1]
    assert p["url"] and p["text"] and p["sentiment"] in ("positive", "negative", "neutral") and p["received_at"]
    assert c.get("/api/client/posts", params={"platform": "tiktok"}, headers={"X-Client-Key": both}).json()["total"] == 1
    assert c.get("/api/client/posts", params={"platform": "x"}, headers={"X-Client-Key": both}).json()["total"] == 1
    assert c.get("/api/client/posts", params={"start": "2026-11-20 12:00"},
                 headers={"X-Client-Key": both}).json()["total"] == 1
    assert c.get("/api/client/posts", params={"received_since": r["server_time"]},
                 headers={"X-Client-Key": both}).json()["total"] == 0
    assert c.get("/api/client/articles", headers={"X-Client-Key": social_only}).status_code == 403
    assert c.get("/api/client/me", headers={"X-Client-Key": both}).json()["social_media"] is True
    with archive._LOCK:
        db = archive._db()
        db.execute("DELETE FROM items WHERE kind='medsos'")
        db.commit()
        db.close()
    insights.prune_reports()
    assert c.get("/api/client/posts", headers={"X-Client-Key": both}).json()["total"] == 0


def test_client_guide_page_is_public_and_holds_no_data(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from tests.test_users import _app, _client
    app, _ = _app(tmp_path, monkeypatch)
    r = _client(app).get("/client-docs")
    assert r.status_code == 200 and "X-Client-Key" in r.text and "received_since" in r.text


def test_client_key_limits_cannot_be_got_round(rd, tmp_path, monkeypatch):
    """Found in review: odd spacing before start must not skip the client's 'data from'; a huge page is no error."""
    pytest.importorskip("fastapi")
    from tests.test_users import SITE, _app, _client
    app, _ = _app(tmp_path, monkeypatch)
    key = _client(app, "ana").post("/api/clients/save", json={"name": "A", "query": "pln", "since": "2026-11-20"},
                                   headers=SITE).json()["key"]
    c, h = _client(app), {"X-Client-Key": key}
    base = c.get("/api/client/articles", headers=h).json()["total"]
    assert base == 3                                                            # the 19 Nov article is before 'from'
    for odd in (" 2020-01-01", "　2020-01-01", " 2020-01-01", "2020-01-01"):
        assert c.get("/api/client/articles", params={"start": odd}, headers=h).json().get("total") == base, odd
    assert c.get("/api/client/articles", params={"start": "２０２０-01-01"}, headers=h).json()["ok"] is False
    r = c.get("/api/client/articles", params={"page": "10000000000000000000"}, headers=h)
    assert r.status_code == 200 and r.json()["rows"] == []
    assert c.get("/api/client/posts", params={"page": "10000000000000000000"}, headers=h).status_code == 403
