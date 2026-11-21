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
    st.cfg = stream.save_settings({"top_n": 50, "top2_n": 20})
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
    assert got["active"] == 50 and got["active2"] == 20 and got["semi"] == 10 and got["rare"] == 1 and got["inactive"] == 1 and got["new"] == 1
    tier = {r["domain"]: r for r in st.q("SELECT domain, tier, next_check FROM sources")}
    assert tier["m0.id"]["tier"] == "active" and tier["m55.id"]["tier"] == "active2" and tier["m75.id"]["tier"] == "semi" and tier["lama.id"]["tier"] == "rare"
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
    assert src["sm:mati.id"]["next_check"] >= st._next_health_time() - 60                      # until the next check
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
    st.cfg.update(all_links=True, old_per_check=2, homepage=True, min_date="")
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
        assert st.check_source(st.q("SELECT * FROM sources")[0]) == 3    # today + 2 old; first visit: front page = old
        urls = [r["url"] for r in st.q("SELECT url FROM articles")]
        assert not any("lain.com" in u for u in urls)
        first = [st.next_job()[1]["url"]]
        assert first[0].endswith(("kabar-terbaru-hari-ini", "berita-di-beranda-saja"))     # news first
        st.check_source(st.q("SELECT * FROM sources")[0])                 # next visit: front page news + 2 more old
        urls = [r["url"] for r in st.q("SELECT url FROM articles")]
        assert len(urls) == 6 and "https://y.id/nasional/2026/berita-di-beranda-saja" in urls
        assert st.q("SELECT SUM(n) n FROM domain_days")[0]["n"] == 2     # activity counts new articles only
    finally:
        st.running = False


def test_restart_never_leaves_old_threads_and_purged_links_are_not_read_again(st, monkeypatch):
    import threading
    from crawler import stream
    monkeypatch.setattr(st, "sync_sources", lambda log=print: {"rss": 0, "sitemaps": 0, "added": 0})
    monkeypatch.setattr(stream.Stream, "health_loop", lambda self: None)
    st.cfg = stream.save_settings({"threads": 3})
    before = threading.active_count()
    for _ in range(3):
        st.start(lambda m: None)
        st.stop()
    time.sleep(0.5)
    assert threading.active_count() <= before + 1 and not st.running and st.qn == 0
    # purge keeps the keys: the same old link is not taken again
    st.cfg["retention_days"] = 1
    st.x("INSERT INTO articles(key, url, domain, discovered, status) VALUES ('a.id/berita/x-y-z','https://a.id/berita/x-y-z','a.id',?, 'success')",
         (time.time() - 3 * 86400,))
    assert st.purge() == 1 and st.q("SELECT COUNT(*) n FROM articles")[0]["n"] == 0
    assert st.add_found([{"url": "https://a.id/berita/x-y-z", "domain": "a.id"}], "rss") == 0
    assert st.add_found([{"url": "https://a.id/berita/baru-sekali-ini", "domain": "a.id"}], "rss") == 1
    st.recent.extend([time.time()] * 10)
    assert st.status()["per_minute"] == 2.0



def test_articles_kept_for_good_by_default(tmp_path, monkeypatch):
    import json
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    assert stream.settings()["retention_days"] == 0
    os.makedirs(tmp_path / "data" / "stream", exist_ok=True)
    (tmp_path / "data" / "stream" / "settings.json").write_text(json.dumps({"retention_days": 7, "threads": 8}))
    assert stream.settings()["retention_days"] == 0                      # the old default switched once
    stream.save_settings({"retention_days": 7})
    assert stream.settings()["retention_days"] == 7                      # a deliberate choice stays
    s = stream.Stream(str(tmp_path / "data" / "stream2"))
    s.cfg = {**stream.settings(), "retention_days": 0}
    s.x("INSERT INTO articles(key, url, discovered, status) VALUES ('a','u',?, 'success')", (time.time() - 400 * 86400,))
    assert s.purge() == 0 and s.q("SELECT COUNT(*) n FROM articles")[0]["n"] == 1
    s.stop()


def test_fetch_fallbacks_can_be_switched_off_and_are_counted(tmp_path, monkeypatch):
    """Googlebot / cloudscraper steps of the article reader: on by default, off from the settings (a plain retry /
    skipped), and the stream counts which step got each article."""
    import sys
    from crawler import fetch_policy, paths, stream
    from crawler.medmas import extract
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    assert "Googlebot" in extract.GOOGLEBOT["User-Agent"]
    stream.save_settings({"fetch_googlebot": False, "fetch_cloudscraper": False})
    fetch_policy.apply_settings()
    try:
        assert "Googlebot" not in extract.GOOGLEBOT["User-Agent"] and sys.modules.get("cloudscraper", 0) is None
        sent = []

        class R:
            status_code, text = 403, "<html>blocked</html>"
        monkeypatch.setattr(extract.requests, "get", lambda url, headers=None, timeout=0: sent.append(headers) or R())
        _html, content, method, err = extract._fetch_html("https://x.id/a", 5)
        assert err == "HTTP 403" and not content and len(sent) >= 2          # normal + a plain retry, no cloudscraper
        assert all("Googlebot" not in (h or {}).get("User-Agent", "") for h in sent)
    finally:
        fetch_policy.apply(True, True)
    assert "Googlebot" in extract.GOOGLEBOT["User-Agent"] and sys.modules.get("cloudscraper", 0) is not None
    s = stream.Stream(str(tmp_path / "s"))
    for m in ("normal", "normal", "googlebot"):
        s.x("INSERT INTO fetch_methods(day, method, n) VALUES (date('now'), ?, 1) ON CONFLICT(day, method) DO UPDATE "
            "SET n = n + 1", (m,))
    fm = s.fetch_methods(7)
    assert fm["total"] == 3 and fm["methods"]["googlebot"]["pct"] == 33.33


def test_fresh_news_lane_comes_before_the_backlog(tmp_path, monkeypatch):
    """Millions of older links never hold up today's news: fresh first, backlog when no fresh page can be read —
    and one backlog reader keeps the archive moving."""
    import time as _t
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "domain_gap": 0, "old_hours": ""}  # any time (night-hours rule: its own test)
    today = stream.datetime.now(stream.WIB).strftime("%Y-%m-%d")
    rows = [(f"old.id/{i}", f"https://old.id/{i}", "old.id", "2020-01-01", _t.time() + i) for i in range(50)]
    rows += [(f"arch{i}.id/x", f"https://arch{i}.id/x", f"arch{i}.id", "2019-05-05", _t.time()) for i in range(5)]
    rows += [("news.id/1", "https://news.id/1", "news.id", today, _t.time() - 100),
             ("nodate.id/1", "https://nodate.id/1", "nodate.id", "", _t.time() - 200)]
    with st.lock:
        st._db.executemany("INSERT INTO articles(key, url, domain, feed_date, discovered, status) VALUES "
                           "(?,?,?,?,?,'new')", rows)
        st._db.commit()
    st.ensure_indexes()
    st.refill()
    assert st.qn == 57 and st.qn_old == 55
    first = [st.next_job()[0] for _ in range(2)]
    assert set(first) == {"news.id", "nodate.id"}                               # fresh first, though found earlier
    nxt = st.next_job()
    assert nxt[0] in ("old.id",) or nxt[0].startswith("arch")                   # then the backlog
    st.busy_domains.clear()
    st._enqueue({"key": "news.id/2", "url": "https://news.id/2", "domain": "news.id", "feed_date": today})
    assert st.next_job(backlog_first=True)[0] != "news.id"                      # the backlog reader
    assert st.next_job()[0] == "news.id"


def test_disk_warnings_and_the_brake(tmp_path, monkeypatch):
    """75% and 90% full: one Telegram message each; under 5 GB free: older links are not taken in, news still is."""
    from crawler import overview, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = stream.settings()
    sent, state = [], {"pct": 50.0, "free": 30e9}
    monkeypatch.setattr(overview, "disk", lambda: {"used_pct": state["pct"], "free": state["free"], "total": 60e9,
                                                   "used": 0})
    assert st.disk_guard(sent.append)["warned"] == 0 and not sent
    state.update(pct=76.0, free=14e9)
    st.disk_guard(sent.append)
    st.disk_guard(sent.append)
    assert len(sent) == 1 and "76%" in sent[0]                                  # once per level
    state.update(pct=93.0, free=4e9)
    out = st.disk_guard(sent.append)
    assert len(sent) == 2 and out["brake"] and st.disk_brake
    today = stream.datetime.now(stream.WIB).strftime("%Y-%m-%d")
    n = st.add_found([{"url": "https://a.id/berita/2020/lama-sekali", "date": "2020-01-01", "domain": "a.id"},
                      {"url": "https://a.id/berita/hari-ini-baru", "date": today, "domain": "a.id"}], "sitemap")
    assert n == 1                                                               # the news only
    state.update(pct=60.0, free=25e9)
    assert not st.disk_guard(sent.append)["brake"] and not st.disk_brake


def test_dashboard_reads_on_while_the_worker_holds_the_database(tmp_path, monkeypatch):
    """After an update the worker builds indexes once (a long write): the web process's Stream still opens, status()
    answers at once (counts follow in the background) and the settings have their own instant endpoint."""
    import sqlite3
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    folder = str(tmp_path / "s")
    stream.Stream(folder)._db.execute("DROP TABLE fetch_methods")         # a table this version adds
    hold = sqlite3.connect(f"{folder}/stream.db", timeout=1)
    hold.execute("BEGIN EXCLUSIVE")                                        # the worker, mid index build
    real_connect = sqlite3.connect
    monkeypatch.setattr(stream.sqlite3, "connect", lambda *a, **k: real_connect(*a, **{**k, "timeout": 0.5}))
    try:
        st = stream.Stream(folder)
        assert st.schema_busy
    finally:
        hold.rollback()
        hold.close()
    out = st.status(5)
    assert "queue" in out and out["settings"]["threads"]


def test_international_outlets_pause_and_catch_up(tmp_path, monkeypatch):
    """Non-Indonesian outlets: recognised (address, then the language of their titles, the admin's mark first),
    paused on request (their sources are not checked), and the paused period caught up later."""
    import json as _j
    import time as _t
    from crawler import intl, paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "data" / "stream"))
    st.cfg = stream.settings()
    doms = ["kompas.com", "bbc.com", "thestar.com.my", "radar.id", "baru.com"]
    with st.lock:
        st._db.executemany("INSERT INTO sources(id, kind, domain, url, next_check, interval, checked) VALUES "
                           "(?, 'sitemap', ?, '', 0, 3600, 1)", [(f"sm:{d}", d) for d in doms])
        rows = [(f"kompas/{i}", "kompas.com", f"Warga Bekasi minta pemerintah perbaiki jalan rusak {i}") for i in range(10)]
        rows += [(f"bbc/{i}", "bbc.com", f"UK inflation falls as energy prices ease {i}") for i in range(10)]
        rows += [("baru/1", "baru.com", "Harga cabai naik di pasar")]
        st._db.executemany("INSERT INTO articles(key, url, domain, title, status, fetched) VALUES "
                           "(?, 'https://x/' || ?, ?, ?, 'success', ?)", [(k, k, d, t, _t.time()) for k, d, t in rows])
        st._db.commit()
    cnt = intl.classify(st)
    got = {r["domain"]: r["country"] for r in st.rq("SELECT domain, country FROM outlet_country")}
    assert got == {"kompas.com": "id", "bbc.com": "intl", "thestar.com.my": "intl", "radar.id": "id",
                   "baru.com": "unknown"} and cnt["intl"] == 2
    assert intl.mark(st, ["kompas.com"], "intl") == 1 and intl.mark(st, ["kompas.com"], "") == 1
    assert {r["domain"]: r["country"] for r in st.rq("SELECT domain, country FROM outlet_country")}["kompas.com"] in \
        ("intl", "id")
    intl.mark(st, ["kompas.com"], "id")
    stream.save_settings({"pause_international": True})
    st.cfg = stream.settings()
    intl.apply_pause(st)
    assert {r["domain"] for r in st.due_sources()} == {"kompas.com", "radar.id", "baru.com"}
    assert intl.status(st)["paused"] and intl.status(st)["paused_since"]
    stream.save_settings({"pause_international": False})
    st.cfg = stream.settings()
    intl.apply_pause(st)
    assert len(st.due_sources()) == 5
    missed = intl.status(st)["missed"]
    assert missed and missed["start"] == missed["end"]
    sent = []
    monkeypatch.setattr(stream, "command", lambda c, a="": sent.append((c, _j.loads(a))))
    out = intl.catch_up(st, missed["start"], missed["end"])
    assert out["how"] == "sweep" and sent[0][0] == "catchup_domains" and set(sent[0][1]["domains"]) == {
        "bbc.com", "thestar.com.my"} and intl.status(st)["missed"] is None
    out = intl.catch_up(st, "2025-01-01", "2025-03-31")                          # a long period: archive jobs
    assert out["how"] == "archive" and out["jobs"] == 2
    assert {r["domain"] for r in st.rq("SELECT domain FROM backfill")} == {"bbc.com", "thestar.com.my"}
    swept = []
    monkeypatch.setattr(st, "check_sitemap", lambda src, catchup=0: swept.append((src["domain"], catchup)) or (0, False, {}))
    st.run_catchup(days=2, domains=["bbc.com"])                                  # the worker's one-off sweep
    assert swept == [("bbc.com", 2)] and not st.meta("catchup_last")             # the daily sweep's date untouched


def test_queue_in_memory_is_bounded_and_old_undated_links_wait_behind(tmp_path, monkeypatch):
    """Millions of links found in a day never fill the worker's memory; older undated links never count as news."""
    import time as _t
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "QUEUE_MAX", 50)
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "old_per_check": 1000}
    st.running = True
    st.add_found([{"url": f"https://a{i}.id/berita/x{i}", "domain": f"a{i}.id"} for i in range(200)], "sitemap")
    assert st.qn == 50                                                       # the rest wait in the database
    old = [{"url": f"https://b.id/berita/lama-{i}", "domain": "b.id", "old": True} for i in range(5)]
    st.add_found(old, "sitemap")
    rows = st.rq("SELECT source FROM articles WHERE domain='b.id'")
    assert rows and all(r["source"] == "sitemap:old" for r in rows)
    assert not st.is_fresh({"source": "sitemap:old", "feed_date": ""})
    assert st.is_fresh({"source": "sitemap", "feed_date": "", "discovered": _t.time()})
    assert not st.is_fresh({"source": "sitemap", "feed_date": "", "discovered": _t.time() - 3 * 86400})
    st.running = False


def test_reader_processes_share_the_outlets(tmp_path, monkeypatch):
    """Several reader processes: each outlet belongs to exactly one (one page at a time per outlet holds), together
    they cover all; the pace shown is the sum; the worker starts / stops them with the stream."""
    import json as _j
    import time as _t
    from crawler import paths, stream, stream_worker
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    folder = str(tmp_path / "data" / "stream")
    a, b = stream.Stream(folder), stream.Stream(folder)
    today = stream.datetime.now(stream.WIB).strftime("%Y-%m-%d")
    with a.lock:
        a._db.executemany("INSERT INTO articles(key, url, domain, feed_date, discovered, status) VALUES "
                          "(?,?,?,?,?, 'new')", [(f"d{i}.id/x", f"https://d{i}.id/x", f"d{i}.id", today, _t.time())
                                                 for i in range(40)])
        a._db.commit()
    for st, k in ((a, 0), (b, 1)):
        st.cfg, st.shard = stream.settings(), (k, 2)
        st.refill()
    da = {j["domain"] for q in a.by_domain.values() for j in q}
    db_ = {j["domain"] for q in b.by_domain.values() for j in q}
    assert da and db_ and not (da & db_) and len(da | db_) == 40
    a._enqueue({"key": "zz/1", "url": "https://zz.id/1", "domain": next(iter(db_)), "feed_date": today})
    assert "zz/1" not in a.queued                                            # another process's outlet
    stream.write_live({"t": _t.time(), "running": True, "per_minute": 50.0, "per_day_pace": 72000, "readers_busy": 8})
    stream.write_reader_live(1, {"t": _t.time(), "running": True, "per_minute": 40.0, "per_day_pace": 57600,
                                 "readers_busy": 7})
    live = stream.read_live()
    assert live["per_minute"] == 90.0 and live["processes"] == 2 and live["readers_busy"] == 15
    started = []

    class P:
        def __init__(self, args, **k):
            started.append(args[-2:])
            self.stdin, self.returncode = type("I", (), {"close": lambda s: None})(), None

        def poll(self):
            return self.returncode

        def wait(self, timeout=0):
            self.returncode = 0
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", P)
    monkeypatch.setattr(stream, "reader_processes", lambda cfg=None: 3)
    procs, s = {}, type("S", (), {"running": True})()
    stream_worker._manage_readers(stream, s, procs)
    assert sorted(started) == [["1", "3"], ["2", "3"]] and set(procs) == {1, 2}
    s.running = False
    stream_worker._manage_readers(stream, s, procs)
    assert procs == {}
    assert _j.loads(open(f"{folder}/live_r1.json").read())["per_minute"] == 40.0
    monkeypatch.undo()
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream.os, "cpu_count", lambda: 4)
    assert stream.reader_processes({"reader_processes": 0}) == 3 and stream.reader_processes({"reader_processes": 2}) == 2
    monkeypatch.setattr(stream.os, "cpu_count", lambda: 2)
    assert stream.reader_processes({"reader_processes": 0}) == 1


def test_older_links_have_a_daily_budget_and_archive_floods_are_not_news(tmp_path, monkeypatch):
    """Older links: at most old_per_day a day (kept across restarts); thousands of 'new' links in one visit are an
    archive listing and go to the backlog lane."""
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "FRESH_PER_CHECK", 5)
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "old_per_check": 100, "old_per_day": 7}
    old = [{"url": f"https://b.id/berita/lama-{i}", "domain": "b.id", "old": True} for i in range(10)]
    assert st.add_found(old, "sitemap") == 7
    assert st.add_found([{**o, "url": o["url"] + "x"} for o in old], "sitemap") == 0     # the day's budget is used
    st2 = stream.Stream(str(tmp_path / "s"))                                            # a restart keeps the count
    st2.cfg = st.cfg
    assert st2.add_found([{**o, "url": o["url"] + "y"} for o in old], "sitemap") == 0
    st2.cfg = {**st.cfg, "old_per_day": 1000}
    many = [{"url": f"https://c.id/berita/baru-{i}", "domain": "c.id"} for i in range(9)]
    assert st2.add_found(many, "sitemap") == 9
    src = [r["source"] for r in st2.rq("SELECT source FROM articles WHERE domain='c.id' ORDER BY rowid")]
    assert src.count("sitemap") == 5 and src.count("sitemap:old") == 4


def test_older_links_are_read_only_in_the_night_hours(tmp_path, monkeypatch):
    """By day every reader is on fresh news; older links wait for old_hours (WIB); media-archive jobs never wait."""
    from datetime import datetime
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "old_hours": "1-6"}
    noon = datetime(2026, 11, 21, 12, 0, tzinfo=stream.WIB).timestamp()
    night = datetime(2026, 11, 21, 3, 0, tzinfo=stream.WIB).timestamp()
    assert not st.backlog_open(noon) and st.backlog_open(night)
    st._enqueue({"key": "a", "url": "https://a.id/berita/lama", "domain": "a.id", "source": "sitemap:old"})
    st._enqueue({"key": "b", "url": "https://b.id/berita/arsip", "domain": "b.id", "source": "backfill:x"})
    monkeypatch.setattr(st, "backlog_open", lambda now=None: False)
    got = [st.next_job(True), st.next_job(True)]
    assert [g[1]["key"] if g else None for g in got] == ["b", None]        # by day: only the media-archive job
    monkeypatch.setattr(st, "backlog_open", lambda now=None: True)
    assert st.next_job(True)[1]["key"] == "a"
    assert stream.save_settings({"old_hours": "22 - 5"})["old_hours"] == "22-5"
    st.cfg["old_hours"] = "22-5"
    assert stream.Stream.backlog_open(st, datetime(2026, 11, 21, 23, 0, tzinfo=stream.WIB).timestamp())


def test_priorities_late_news_by_day_archive_at_night_nothing_before_min_date(tmp_path, monkeypatch):
    """Fresh first; late news (last late_days days) by day when a reader is free; the archive only at night;
    nothing published before min_date unless a media-archive job; a title matching a keyword jumps the queue."""
    from datetime import datetime, timedelta
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "old_hours": "1-6", "late_days": 7, "min_date": "2026-01-01", "domain_gap": 0,
              "keywords": ["banjir bandang"]}
    day = lambda n: (datetime.now(stream.WIB) - timedelta(days=n)).strftime("%Y-%m-%d")
    assert st.too_old("2025-12-31") and not st.too_old("2026-01-01")
    assert st.too_old("", "https://a.id/2019/05/berita") and not st.too_old("", "https://a.id/2026/05/berita")
    st.cfg["min_date"] = ""
    assert not st.too_old("2019-01-01")
    st.cfg["min_date"] = "2026-01-01"
    n = st.add_found([{"url": "https://x.id/berita/lama", "domain": "x.id", "date": "2024-03-01"},
                      {"url": "https://x.id/2018/02/berita-lama", "domain": "x.id"}], "sitemap")
    assert n == 0
    st._enqueue({"key": "late", "url": "https://l.id/a", "domain": "l.id", "source": "sitemap:old", "feed_date": day(4)})
    st._enqueue({"key": "arch", "url": "https://r.id/a", "domain": "r.id", "source": "sitemap:old", "feed_date": day(20)})
    st._enqueue({"key": "kw", "url": "https://k.id/a", "domain": "k.id", "source": "sitemap:old", "feed_date": day(20),
                 "title": "Banjir bandang di Garut"})
    assert "k.id" in st.by_domain                                           # keyword in the title: fresh lane
    monkeypatch.setattr(st, "backlog_open", lambda now=None: False)
    got = [st.next_job(), st.next_job(), st.next_job()]
    assert [g[1]["key"] if g else None for g in got] == ["kw", "late", None]   # by day: the archive waits
    monkeypatch.setattr(st, "backlog_open", lambda now=None: True)
    assert st.next_job()[1]["key"] == "arch"
    assert set(st.lag_minutes()) == {"n", "median", "within30"}


def test_late_news_is_not_limited_by_the_older_links_budget(tmp_path, monkeypatch):
    """News of the last late_days days always comes in; only the archive (older) shares the daily budget."""
    from datetime import datetime, timedelta
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "old_per_check": 2, "old_per_day": 2, "late_days": 7, "min_date": ""}
    day = lambda n: (datetime.now(stream.WIB) - timedelta(days=n)).strftime("%Y-%m-%d")
    late = [{"url": f"https://a.id/berita/telat-{i}", "domain": "a.id", "date": day(4)} for i in range(6)]
    arch = [{"url": f"https://a.id/berita/kabar-lama-{i}", "domain": "a.id", "date": day(30)} for i in range(6)]
    assert st.add_found(late + arch, "sitemap") == 8                       # 6 late + 2 archive


def test_watchdog_restarts_a_hung_or_stalled_worker_but_not_one_starting_up(tmp_path, monkeypatch):
    """Live state not written for 5 min (after it was once) = hung; stream on, fresh links waiting, nothing read
    for 20 min = stalled; a worker still starting up, a stopped stream or only archive links waiting = fine."""
    import json as _j
    import os as _o
    import time as _t
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(stream, "settings", lambda: {**stream.DEFAULTS, "enabled": True})
    live = stream._live_path()
    _o.makedirs(_o.path.dirname(live), exist_ok=True)
    now = _t.time()
    assert stream._stuck(now, 0.0) == ("", 0.0)                              # no live state yet: starting up
    def put(**kw):
        with open(live, "w") as f:
            _j.dump({"running": True, "queue_mem": 10, "queue_backlog_mem": 0, "per_minute": 0, **kw}, f)
    put()
    _o.utime(live, (now - 400, now - 400))
    assert stream._stuck(now - 1000, 0.0)[0].startswith("stopped")          # written once, then silent: hung
    assert stream._stuck(now, 0.0)[0] == ""                                  # older than this start: starting up
    put()
    why, idle = stream._stuck(now - 1000, 0.0)
    assert why == "" and idle > 0                                            # idle: the clock starts
    assert stream._stuck(now - 1000, idle - stream.STALL_AFTER - 1)[0].endswith("(stalled)")
    put(queue_backlog_mem=10)                                                # only archive links: by day, fine
    assert stream._stuck(now - 1000, now - 5000) == ("", 0.0)
    put(per_minute=40)
    assert stream._stuck(now - 1000, now - 5000) == ("", 0.0)


def test_an_unchanged_sitemap_file_is_not_processed_again(tmp_path, monkeypatch):
    """A sitemap file identical to the last visit's is skipped (its links are known); a changed one is read; index
    files are always followed."""
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    body = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://a.id/x</loc></url></urlset>'
    assert not st._unchanged("https://a.id/s.xml", body)
    assert st._unchanged("https://a.id/s.xml", body) and st.sm_unchanged == 1
    assert not st._unchanged("https://a.id/s.xml", body + b" ")
    idx = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></sitemapindex>'
    assert not st._unchanged("https://a.id/i.xml", idx) and not st._unchanged("https://a.id/i.xml", idx)


def test_clearing_older_waiting_links_keeps_news_of_the_last_days(tmp_path, monkeypatch):
    """'Clear older waiting links' removes only waiting links older than late_days (and undated old ones); news of
    the last days (even marked :old), read articles and fresh news stay; sitemaps are read again afterwards."""
    import time as _t
    from datetime import datetime, timedelta
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    st.cfg = {**stream.settings(), "late_days": 7}
    day = lambda n: (datetime.now(stream.WIB) - timedelta(days=n)).strftime("%Y-%m-%d")
    now = _t.time()
    rows = [("fresh", day(0), "sitemap", "new", now), ("late", day(4), "sitemap:old", "new", now),
            ("arch", day(30), "sitemap:old", "new", now), ("undated_old", "", "sitemap:old", "new", now),
            ("undated_new", "", "rss", "new", now), ("undated_stale", "", "rss", "new", now - 5 * 86400),
            ("read_old", day(30), "sitemap:old", "success", now)]
    for k, fd, src, status, disc in rows:
        st.x("INSERT INTO articles(key, url, domain, source, feed_date, status, discovered) VALUES (?,?,?,?,?,?,?)",
             (k, f"https://a.id/{k}", "a.id", src, fd, status, disc))
    st._sm_seen = {"https://a.id/s.xml": (1, 2)}
    out = st.trim_backlog()
    left = {r["key"] for r in st.rq("SELECT key FROM articles")}
    assert left == {"fresh", "late", "undated_new", "read_old"} and out["removed"] == 3
    assert not st.__dict__.get("_sm_seen")


def test_where_stuck_names_the_waiting_step_and_live_state_never_blocks(tmp_path, monkeypatch):
    """While a long step holds the stream's lock, the live state still comes (lock_busy) and the main thread's place
    is named."""
    import threading as _th
    import time as _t
    from crawler import paths, stream
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    st = stream.Stream(str(tmp_path / "s"))
    held = _th.Event()

    def hold():
        with st.lock:
            held.set()
            _t.sleep(2.5)
    _th.Thread(target=hold, daemon=True).start()
    held.wait(2)
    t0 = _t.time()
    live = st.live_state()
    assert live["lock_busy"] and _t.time() - t0 < 2
    assert "test_stream.py" in stream.where_stuck(_th.get_ident())
