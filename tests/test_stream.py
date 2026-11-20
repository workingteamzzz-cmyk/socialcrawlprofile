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
    st.cfg["all_links"] = False                                       # "recent only" mode
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
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('rss:d','rss','mati.id','https://mati.id/rss',0,600)")
    ivs = []
    for _ in range(3):
        st.check_source(st.q("SELECT * FROM sources")[0])
        ivs.append(st.q("SELECT interval FROM sources")[0]["interval"])
    src = st.q("SELECT * FROM sources")[0]
    assert ivs[:2] == [3600, 3600] and src["fails"] == 3 and src["tier"] == "dead" and src["interval"] == 7 * 86400
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('sm:q','sitemap','q.id','',0,600)")
    st.reschedule(st.q("SELECT * FROM sources WHERE id='sm:q'")[0], 0, False)    # first check, nothing recent
    q = st.q("SELECT tier, interval FROM sources WHERE id='sm:q'")[0]
    assert q["tier"] == "inactive" and q["interval"] == 3 * 86400
    st.reschedule(st.q("SELECT * FROM sources WHERE id='sm:q'")[0], 5, False)    # it publishes again: promoted
    q = st.q("SELECT tier, interval FROM sources WHERE id='sm:q'")[0]
    assert q["tier"] == "semi" and q["interval"] == 4 * 3600


def test_tiers_from_the_last_seven_days(st):
    from crawler import stream
    st.cfg = stream.save_settings({"top_n": 50})
    day = datetime.now(WIB)
    rows = []
    for i in range(80):                                              # 80 outlets published this week, 0..79 articles
        rows.append((f"m{i}.id", day.strftime("%Y-%m-%d"), 80 - i))
    rows.append(("lama.id", (day - timedelta(days=12)).strftime("%Y-%m-%d"), 4))   # only 12 days ago
    with st.lock:
        st._db.executemany("INSERT INTO domain_days VALUES (?,?,?)", rows)
        st._db.commit()
    srcs = [f"m{i}.id" for i in range(80)] + ["lama.id", "sepi.id", "baru.id"]
    for d in srcs:
        st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval, checked) VALUES (?,?,?,?,?,?,?)",
             (f"sm:{d}", "sitemap", d, "", time.time() + 9 * 86400, 9 * 86400, 0 if d == "baru.id" else time.time() - 3600))
    got = st.compute_tiers()
    assert got["active"] == 50 and got["semi"] == 30 and got["rare"] == 1 and got["inactive"] == 1 and got["new"] == 1
    tier = {r["domain"]: r for r in st.q("SELECT domain, tier, next_check FROM sources")}
    assert tier["m0.id"]["tier"] == "active" and tier["m60.id"]["tier"] == "semi" and tier["lama.id"]["tier"] == "rare"
    assert tier["m0.id"]["next_check"] < time.time() + 3600 + 5        # promoted: checked within the hour, not in 9 days
    assert tier["sepi.id"]["next_check"] > time.time() + 8 * 86400    # inactive: left alone
    assert st.status()["tiers"]["active"] == 50


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


def test_a_found_link_goes_straight_to_the_front_of_the_readers(st):
    st.running = True
    try:
        st.add_found([{"url": "https://a.id/berita/2026/lama-satu-dua", "domain": "a.id"},
                      {"url": "https://a.id/berita/2026/lama-tiga-empat", "domain": "a.id"}], "rss")
        st.add_found([{"url": "https://a.id/berita/2026/baru-saja-terbit", "domain": "a.id"}], "rss")
        dom, job = st.next_job()                                     # no refill needed: queued the moment it was found
        assert dom == "a.id" and job["url"].endswith("baru-saja-terbit")
        assert st.add_found([{"url": "https://a.id/berita/2026/baru-saja-terbit", "domain": "a.id"}], "rss") == 0
    finally:
        st.running = False


def test_robots_exclude_and_all_articles_export(st, monkeypatch, tmp_path):
    import openpyxl
    import requests
    from crawler import stream
    from crawler.medmas import extract
    st.cfg = stream.save_settings({"exclude_domains": "https://www.dilarang.id/\nlarangan.id"})
    assert st.cfg["exclude_domains"] == ["dilarang.id", "larangan.id"]
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None:
                        Resp(200, b"", None) if "robots" not in url else
                        type("R", (), {"status_code": 200, "text": "User-agent: *\nDisallow: /premium/\n"})())
    read = []

    def fake_extract(url, timeout=20):
        read.append(url)
        text = "Isi berita yang cukup panjang untuk dianggap artikel sungguhan oleh pembaca. " * 5
        return {"url": url, "media": "", "title": "Judul " + url[-6:], "author": "", "date": "2026-11-20",
                "time": "09:30", "summary": text[:60], "content": text, "image": "", "influencer": "", "persons": "",
                "organizations": "", "method": "", "error": "", "is_index": False, "is_article": True}
    monkeypatch.setattr(extract, "extract_article", fake_extract)
    st.cfg["max_age_hours"] = 24 * 3650
    jobs = [{"key": k, "url": u, "domain": "", "title": "", "feed_date": "", "feed_time": ""} for k, u in [
        ("a", "https://www.dilarang.id/berita/satu-dua-tiga"),
        ("b", "https://boleh.id/premium/berita-berbayar-satu"),
        ("c", "https://boleh.id/berita/berita-gratis-satu"),
        ("d", "https://boleh.id/berita/berita-gratis-dua")]]
    for j in jobs:
        st.x("INSERT INTO articles(key, url, discovered) VALUES (?,?,?)", (j["key"], j["url"], time.time()))
    assert [st.read(j) for j in jobs] == ["skipped", "skipped", "success", "success"]
    assert read == [jobs[2]["url"], jobs[3]["url"]]                    # excluded outlet + robots.txt: never opened
    name = st.export_day("2026-11-20", str(tmp_path / "out"))
    ws = openpyxl.load_workbook(tmp_path / "out" / name).active
    assert [c.value for c in ws[1]][:4] == ["DATE", "TIME", "TITLE", "URL"] and ws.max_row == 3
    assert ws["A2"].value == "20-Nov-2026" and ws["B2"].value == "09:30:00" and ws["K2"].value.startswith("<p>")
    assert ws["A1"].fill.fgColor.rgb == "FF15677B" and st.export["rows"] == 2 and not st.export["running"]


def test_catchup_sweep_takes_the_last_two_days_late_articles(st, monkeypatch):
    from crawler import stream
    from crawler.medmas.discovery import sitemaps as sm
    st.cfg = stream.save_settings({"catchup_days": 2, "all_links": False})
    now = datetime.now(WIB)
    day = {k: (now - timedelta(days=k)).strftime("%Y-%m-%d") for k in (0, 1, 2, 10)}
    idx = "".join(f"<sitemap><loc>https://x.id/sitemap-{day[k]}.xml</loc></sitemap>" for k in (10, 2, 1, 0))
    pages = {"https://x.id/sitemap.xml": f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{idx}</sitemapindex>'}
    for k in (0, 1, 2, 10):
        pages[f"https://x.id/sitemap-{day[k]}.xml"] = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>https://x.id/berita/{day[k]}/judul-berita-hari-{k}</loc><lastmod>{day[k]}T08:00:00+07:00</lastmod></url>"
            "</urlset>")
    monkeypatch.setattr(sm, "_fetch", lambda u, timeout=8: pages[u].encode())
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval, checked) VALUES "
         "('sm:x.id','sitemap','x.id',?,?,3600,?)", ('["https://x.id/sitemap.xml"]', time.time() + 3600, time.time()))
    st.running = True
    try:
        st.add_found([{"url": f"https://x.id/berita/{day[0]}/judul-berita-hari-0", "date": day[0], "domain": "x.id"}], "rss")
        out = st.run_catchup()
        assert out["total"] == 1 and out["found"] == 2 and not out["running"]       # D-1, D-2; today's known; D-10 too old
        urls = {r["url"] for r in st.q("SELECT url FROM articles")}
        assert {f"https://x.id/berita/{day[k]}/judul-berita-hari-{k}" for k in (0, 1, 2)} == urls
        dom, job = st.next_job()
        assert job["url"].endswith("hari-0")                                           # late links wait behind the news
        assert st.meta("catchup_last") == now.strftime("%Y-%m-%d") and st.status()["catchup"]["last_result"]["found"] == 2
    finally:
        st.running = False


def test_site_check_marks_down_sites_and_brings_back_living_ones(st, monkeypatch):
    import threading

    class NoWait(threading.Event):
        def wait(self, timeout=None):
            return False
    st.stop_event = NoWait()
    now = time.time()
    for d, tier, fails in [("hidup.id", "active", 0), ("bangkit.id", "dead", 3), ("mati.id", "inactive", 0),
                           ("hidup2.id", "semi", 0), ("hidup3.id", "rare", 0)]:
        st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval, tier, fails) VALUES (?,?,?,?,?,?,?,?)",
             (f"sm:{d}", "sitemap", d, "", now + 9 * 86400, 3600, tier, fails))
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('rss:mati','rss','mati.id','u',0,600)")
    calls = []

    def check(d):
        calls.append(d)
        return (d != "mati.id"), (["https://bangkit.id/sitemap_news.xml"] if d == "bangkit.id" else [])
    monkeypatch.setattr(st, "check_site", check)
    out = st.run_health()
    assert out["alive"] == 4 and out["down"] == 1 and calls.count("mati.id") == 2          # a second chance
    src = {r["id"]: r for r in st.q("SELECT * FROM sources")}
    assert src["sm:mati.id"]["tier"] == "dead" and src["rss:mati"]["tier"] == "dead"          # feed and sitemap: off
    assert src["sm:mati.id"]["next_check"] > now + 86400                                       # until the next check
    assert src["sm:bangkit.id"]["tier"] == "inactive" and src["sm:bangkit.id"]["fails"] == 0
    assert src["sm:bangkit.id"]["next_check"] <= time.time() and "sitemap_news" in src["sm:bangkit.id"]["url"]
    assert src["sm:hidup.id"]["tier"] == "active"                                              # untouched
    assert st.status()["health"]["last_result"]["down"] == 1
    # the server's own internet down: nothing is marked
    monkeypatch.setattr(st, "check_site", lambda d: (False, []))
    out = st.run_health()
    assert "nothing was marked" in out["error"] and st.q("SELECT tier FROM sources WHERE id='sm:hidup.id'")[0]["tier"] == "active"



def test_every_link_not_yet_known_is_taken_old_ones_behind_the_news(st, monkeypatch):
    from crawler.medmas.discovery import sitemaps as sm
    st.cfg.update(all_links=True, old_per_check=2, homepage=True)
    today = datetime.now(WIB).strftime("%Y-%m-%d")
    urlset = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
              f"<url><loc>https://y.id/berita/{today}/kabar-terbaru-hari-ini</loc><lastmod>{today}</lastmod></url>"
              + "".join(f"<url><loc>https://y.id/berita/2019/01/0{i}/kabar-lama-nomor-{i}</loc><lastmod>2019-01-0{i}</lastmod></url>"
                        for i in range(1, 6))
              + "<url><loc>https://y.id/berita/tanpa-tanggal-sama-sekali</loc></url></urlset>")
    home = ('<html><a href="/nasional/2026/berita-di-beranda-saja">Berita di beranda</a>'
            '<a href="https://lain.com/berita/bukan-situs-ini">x</a><a href="/tag/politik">tag</a></html>')
    pages = {"https://y.id/sitemap.xml": urlset, "https://y.id/": home}
    monkeypatch.setattr(sm, "_fetch", lambda u, timeout=8: pages[u].encode())
    st.x("INSERT INTO sources(id, kind, domain, url, next_check, interval) VALUES ('sm:y.id','sitemap','y.id',?,0,3600)",
         ('["https://y.id/sitemap.xml"]',))
    st.running = True
    try:
        assert st.check_source(st.q("SELECT * FROM sources")[0]) == 4    # today + 2 old (share per visit) + front page
        urls = [r["url"] for r in st.q("SELECT url FROM articles")]
        assert "https://y.id/nasional/2026/berita-di-beranda-saja" in urls and not any("lain.com" in u for u in urls)
        first = [st.next_job()[1]["url"]]
        assert first[0].endswith(("kabar-terbaru-hari-ini", "berita-di-beranda-saja"))     # news first
        st.check_source(st.q("SELECT * FROM sources")[0])                 # next visit: the next share of old links
        assert st.q("SELECT COUNT(*) n FROM articles")[0]["n"] == 6
        assert st.q("SELECT SUM(n) n FROM domain_days")[0]["n"] == 2     # activity counts new articles only
    finally:
        st.running = False
