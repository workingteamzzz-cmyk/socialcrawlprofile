"""Registered media: edit the list, activity + access per outlet, weekly report, paid-media logins."""
import json
import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def md(tmp_path, monkeypatch):
    from crawler import media_db, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "_STREAM", None)
    media_db._CACHE["t"] = 0
    yield media_db
    stream.get().stop()


def test_add_remove_and_the_bundled_list(md):
    reg = md.registry()
    assert len(reg) > 10000 and "detik.com" in reg                        # bundled dictionary + core outlets
    r = md.add("https://www.RadarBaru.id/berita/x, Radar Baru\nbukan domain\ndetik.com")
    assert r["added"] == ["radarbaru.id"] and r["already"] == ["detik.com"] and r["invalid"] == ["bukan domain"]
    assert md.registry()["radarbaru.id"] == "Radar Baru"
    from crawler import stream
    assert stream.get().q("SELECT id FROM sources WHERE domain='radarbaru.id'")        # checked right away
    assert md.remove(["radarbaru.id", "tidakada.id"])["removed"] == ["radarbaru.id"]
    assert "radarbaru.id" not in md.registry() and not stream.get().q("SELECT id FROM sources WHERE domain='radarbaru.id'")
    out = md.listing(q="detik.com")
    assert out["rows"][0]["domain"] == "detik.com" and out["rows"][0]["access"] == "unknown"


def test_activity_access_and_weekly_report(md, tmp_path):
    from crawler import stream
    import openpyxl
    md.add("baca.id\nblokir.id\nbayar.id\nsepi.id")
    s = stream.get()
    now = time.time()
    day = time.strftime("%Y-%m-%d")
    with s.lock:
        s._db.executemany("INSERT INTO domain_days VALUES (?,?,?)", [("baca.id", day, 70), ("blokir.id", day, 14),
                                                                      ("bayar.id", day, 7)])
        rows = [(f"baca.id/{i}", "baca.id", "success", "") for i in range(9)] + [("baca.id/x", "baca.id", "failed", "Timeout")]
        rows += [(f"blokir.id/{i}", "blokir.id", "failed", "HTTP 403") for i in range(6)]
        rows += [(f"bayar.id/{i}", "bayar.id", "filtered", "") for i in range(4)] + [("bayar.id/ok", "bayar.id", "success", "")]
        s._db.executemany("INSERT INTO articles(key, url, domain, status, error, discovered) VALUES (?,?,?,?,?,?)",
                          [(k, "https://" + k, d, st, e, now) for k, d, st, e in rows])
        s._db.execute("UPDATE sources SET checked=?, tier='active' WHERE domain IN ('baca.id','blokir.id','bayar.id')", (now,))
        s._db.execute("UPDATE sources SET checked=?, tier='inactive' WHERE domain='sepi.id'", (now,))
        s._db.commit()
    md._CACHE["t"] = 0
    got = {d: md.listing(q=d)["rows"][0] for d in ("baca.id", "blokir.id", "bayar.id", "sepi.id")}
    assert got["baca.id"]["access"] == "ok" and got["baca.id"]["per_day"] == 10.0 and got["baca.id"]["tier"] == "active"
    assert got["blokir.id"]["access"] == "blocked" and got["blokir.id"]["error"] == "HTTP 403"
    assert got["bayar.id"]["access"] == "paywall" and got["sepi.id"]["access"] == "quiet"
    r = md.report(str(tmp_path / "out"))
    wb = openpyxl.load_workbook(tmp_path / "out" / r["file"])
    assert wb.sheetnames == ["Summary", "Cannot be read", "Readable", "All media"]
    bad = [c.value for c in wb["Cannot be read"]["A"]][1:]
    assert bad[:2] == ["blokir.id", "bayar.id"] and "baca.id" in [c.value for c in wb["Readable"]["A"]]
    assert "blokir.id" in r["text"] and "Readable" in r["text"]


def test_paid_login_cookies_used_for_that_outlet_only(md):
    from crawler.medmas import extract
    with pytest.raises(ValueError):
        md.set_subscription("kompas.id", "tidak ada cookie di sini")
    md.set_subscription("kompas.id", "Cookie: sess=abc; uid=7", note="langganan tim")
    p = md._subs_path()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and json.load(open(p))["kompas.id"]["cookie"] == "sess=abc; uid=7"
    exported = json.dumps([{"name": "tok", "value": "1", "domain": ".tempo.co"}, {"name": "x", "value": "2", "domain": "lain.com"}])
    md.set_subscription("tempo.co", exported)
    assert md.subscriptions()["tempo.co"]["cookie"] == "tok=1"
    sent = []

    class Real:
        class exceptions:
            Timeout = TimeoutError

        @staticmethod
        def get(url, headers=None, timeout=None):
            sent.append((url, dict(headers or {})))
    extract.requests = md._CookieRequests(Real)
    md.install()
    extract.requests.get("https://www.kompas.id/baca/premium", headers={"User-Agent": "x"}, timeout=5)
    extract.requests.get("https://www.detik.com/berita", headers={"User-Agent": "x"}, timeout=5)
    assert sent[0][1]["Cookie"] == "sess=abc; uid=7" and "Cookie" not in sent[1][1]
    for trick in ("https://evil.tld?.kompas.id/x", "https://kompas.id:x@evil.tld/2026/11/19/slug",
                  "https://evil.tld/?u=https://kompas.id", "ftp://kompas.id/x", "https://notkompas.id/x"):
        assert md.cookie_for(trick) == "", trick                       # never sent to another host
    assert md.cookie_for("https://epaper.kompas.id/x") == "sess=abc; uid=7"
    assert extract.requests.exceptions.Timeout is TimeoutError
    md.set_subscription("kompas.id", None, clear=True)
    assert "kompas.id" not in md.subscriptions() and md.cookie_for("https://kompas.id/x") == ""
    import requests
    extract.requests = requests


def test_endpoints_roles(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths, stream
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "_STREAM", None)
    app, _ = _app(tmp_path, monkeypatch)
    viewer, op, admin = _client(app, "citra"), _client(app, "budi"), _client(app, "ana")
    assert viewer.get("/api/media?q=kompas").json()["total"] >= 1
    assert op.post("/api/media/add", json={"text": "baru.id"}, headers=SITE).status_code == 403
    assert admin.post("/api/media/add", json={"text": "baru.id"}, headers=SITE).json()["added"] == ["baru.id"]
    assert admin.post("/api/media/login", json={"domain": "baru.id", "cookies": "a=1"}, headers=SITE).json()["subscribed"]
    assert op.post("/api/media/report", headers=SITE).json()["ok"]
    stream.get().stop()


def test_outlets_found_by_searches_join_the_list(tmp_path, monkeypatch):
    """News Search / Bulk links find an outlet the list lacks -> it is registered (the stream reads it from now on);
    platforms, aggregators, excluded outlets and sub-domains of registered ones are not."""
    from crawler import media_db, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    media_db.add("detik.com\nkompas.com")
    stream.save_settings({"exclude_domains": "dilarang.id"})
    got = media_db.discover(["https://news.detik.com/berita/1", "https://www.radarbaru.id/2026/11/x",
                             "https://radarbaru.id/y", "https://www.msn.com/id-id/news/z", "https://x.com/a/status/1",
                             "https://dilarang.id/a", "https://kabarbaru.co.id/b"])
    assert got == ["kabarbaru.co.id", "radarbaru.id"] and "radarbaru.id" in media_db.registry()
    assert media_db.discover(["https://radarbaru.id/z"]) == []                     # once
    stream.save_settings({"auto_add_media": False})
    assert media_db.discover(["https://lain.id/a"]) == []


def test_temporary_failures_are_tried_again(tmp_path, monkeypatch):
    import time as _t
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    s = stream.Stream(str(tmp_path / "s"))
    s.cfg = stream.settings()
    old = _t.time() - 5 * 3600
    rows = [("a/1", "Timeout", old, 0), ("a/2", "HTTP 503", old, 1), ("a/3", "HTTP 404", old, 0),
            ("a/4", "Timeout", _t.time(), 0), ("a/5", "ConnectionError", old, 2)]
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, status, discovered, fetched, error, tries) VALUES "
                          "(?, 'https://a.id/x', 'a.id', 'failed', ?, ?, ?, ?)",
                          [(k, old, f, e, t) for k, e, f, t in rows])
        s._db.commit()
    assert s.retry_failed() == 2                                   # not a 404, not too soon, not a third time
    assert {r["key"] for r in s.rq("SELECT key FROM articles WHERE status='new'")} == {"a/1", "a/2"}


def test_compare_with_a_competitors_list(tmp_path, monkeypatch):
    """Each link of another list: in the database, or why not — and what can be read at once."""
    import time as _t
    from crawler import compare, media_db, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    media_db.add("ada.id\ngagal.id\nbelum.id")
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    with s.lock:
        s._db.executemany("INSERT INTO articles(key, url, domain, status, discovered, error, title) VALUES "
                          "(?,?,?,?,?,?,?)",
                          [(stream.norm_key(u), u, d, st, _t.time(), e, "T")
                           for u, d, st, e in [("https://ada.id/1", "ada.id", "success", ""),
                                               ("https://gagal.id/2", "gagal.id", "failed", "HTTP 403"),
                                               ("https://ada.id/kode-etik", "ada.id", "filtered", ""),
                                               ("https://ada.id/3", "ada.id", "new", "")]])
        s._db.commit()
    out = compare.compare("https://www.ada.id/1/\nhttps://gagal.id/2\nhttps://ada.id/kode-etik\nhttps://ada.id/3\n"
                          "https://belum.id/9\nhttps://lain.co.id/7")
    st = {r["url"]: r["status"] for r in out["rows"]}
    assert st == {"https://www.ada.id/1/": "have", "https://gagal.id/2": "failed", "https://ada.id/kode-etik":
                  "not_article", "https://ada.id/3": "waiting", "https://belum.id/9": "not_found",
                  "https://lain.co.id/7": "not_registered"}
    assert out["have"] == 1 and out["have_pct"] == 16.7 and out["missing_outlets"] == [("lain.co.id", 1)]
    assert set(out["fetchable"]) == {"https://gagal.id/2", "https://ada.id/3", "https://belum.id/9",
                                     "https://lain.co.id/7"}
    assert [r for r in out["rows"] if r["status"] == "failed"][0]["error"] == "HTTP 403"
