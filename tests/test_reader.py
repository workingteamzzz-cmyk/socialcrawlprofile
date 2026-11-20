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
