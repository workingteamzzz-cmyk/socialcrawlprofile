"""Live news stream: RSS / sitemap sources -> reading queue -> polite readers -> stream DB + reporting database."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("feedparser")
pytest.importorskip("trafilatura")
WIB = timezone(timedelta(hours=7))
NOW = datetime.now(timezone.utc)
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Banjir rendam Bekasi</title><link>https://www.contoh.id/berita/2026/banjir-rendam-bekasi</link>
<pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
<item><title>Harga cabai naik</title><link>https://www.contoh.id/ekonomi/harga-cabai-naik-lagi</link>
<pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>
<item><title>Lama</title><link>https://www.contoh.id/berita/2019/berita-lama-sekali</link>
<pubDate>Mon, 07 Jan 2019 10:00:00 +0000</pubDate></item>
</channel></rss>""".encode()


class Resp:
    def __init__(self, code, body=b"", headers=None):
        self.status_code, self.content, self.headers = code, body, headers or {}
        self.ok = 200 <= code < 300


@pytest.fixture
def st(tmp_path, monkeypatch):
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    stream.save_settings({"keywords": "banjir", "domain_gap": 0.5})
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    s.cfg = stream.settings()
    yield s
    s.stop()


def test_rss_to_queue_to_reader_to_reports(st, monkeypatch):
    import requests
    from crawler import archive
    from crawler.medmas import extract
    seen_headers = []

    def get(url, headers=None, timeout=None):
        seen_headers.append(dict(headers or {}))
        if headers and headers.get("If-None-Match") == '"v1"':
            return Resp(304)
        return Resp(200, RSS, {"ETag": '"v1"'})
    monkeypatch.setattr(requests, "get", get)
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('rss:f','rss','contoh.id',"
         "'https://www.contoh.id/rss',0,600)")
    src = st.q("SELECT * FROM sources")[0]
    assert st.check_source(src) == 2                                  # the 2019 article is too old for the stream
    src = st.q("SELECT * FROM sources")[0]
    assert src["etag"] == '"v1"' and src["next_check"] > time.time()
    assert st.check_source(src) == 0 and seen_headers[-1]["If-None-Match"] == '"v1"'   # unchanged feed: 304

    def fake_extract(url, timeout=20):
        flood = "banjir" in url
        text = ("Banjir merendam ratusan rumah di Bekasi sejak Senin malam. " * 6) if flood else \
               ("Harga cabai rawit di pasar tradisional naik tajam pekan ini. " * 6)
        return {"url": url, "media": "contoh.id", "title": "Banjir rendam Bekasi" if flood else "Harga cabai naik",
                "author": "Rina", "date": NOW.astimezone(WIB).strftime("%Y-%m-%d"), "time": "08:15",
                "summary": text[:80], "content": text, "image": "", "influencer": "", "persons": "",
                "organizations": "", "method": "fake", "error": "", "is_index": False, "is_article": True}
    monkeypatch.setattr(extract, "extract_article", fake_extract)
    st.refill()
    a = st.next_job()
    assert a is not None and st.next_job() is None                    # one page at a time per outlet
    st.read(a[1])
    with st.lock:
        st.busy_domains.discard(a[0])
    b = st.next_job()
    st.read(b[1])
    st.flush_counts()
    rows = {r["title"]: dict(r) for r in st.q("SELECT title, status, matched FROM articles")}
    assert rows["Banjir rendam Bekasi"]["status"] == "success" and rows["Banjir rendam Bekasi"]["matched"] == "banjir"
    assert rows["Harga cabai naik"]["matched"] == ""
    s = st.status()
    assert s["today"]["read"] == 2 and s["today"]["matched"] == 1 and s["today"]["found"] == 2 and s["queue"] == 0
    assert archive.stats()["online"]["items"] == 1                    # only the keyword match went to reports
    assert [x["title"] for x in s["latest"]][0] in ("Banjir rendam Bekasi", "Harga cabai naik")


def test_quiet_and_dead_sources_are_checked_less_often(st, monkeypatch):
    import requests
    from crawler import stream
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('rss:d','rss','mati.id','https://mati.id/rss',0,600)")
    for _ in range(3):
        st.check_source(st.q("SELECT * FROM sources")[0])
    src = st.q("SELECT * FROM sources")[0]
    assert src["fails"] == 3 and src["interval"] == stream.DEAD_INTERVAL
    quiet = {"id": "rss:q", "kind": "rss", "interval": 600, "fails": 0, "hits": 0}
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('rss:q','rss','q.id','u',0,600)")
    st.reschedule(quiet, 0, False)
    assert st.q("SELECT interval FROM sources WHERE id='rss:q'")[0]["interval"] == 900


def test_settings_limits_and_endpoint_roles(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths, stream
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "_STREAM", None)
    assert stream.save_settings({"threads": 500})["threads"] == 64
    with pytest.raises(ValueError):
        stream.save_settings({"rss_minutes": "abc"})
    app, _ = _app(tmp_path, monkeypatch)
    viewer, op, admin = _client(app, "citra"), _client(app, "budi"), _client(app, "ana")
    assert viewer.get("/api/stream").json()["running"] is False
    assert op.post("/api/stream", json={"threads": 8}, headers=SITE).status_code == 403
    r = admin.post("/api/stream", json={"threads": 8, "keywords": "banjir, pln"}, headers=SITE).json()
    assert r["ok"] and r["threads"] == 8 and r["keywords"] == ["banjir", "pln"]
    stream.get().stop()
