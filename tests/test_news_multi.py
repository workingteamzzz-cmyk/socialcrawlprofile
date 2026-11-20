"""News Search with the multi-source engine (the team's medmascrawler), with the network replaced by fakes."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("trafilatura")
pytest.importorskip("feedparser")
import openpyxl  # noqa: E402

ARTICLE = ("Pemadaman listrik kembali terjadi di Kota Bogor pada Senin malam. Warga mengeluhkan gangguan yang "
           "berlangsung hampir lima jam.\n\n"
           "“Kami minta maaf atas ketidaknyamanan ini,” ujar Manajer PLN UP3 Bogor, Ahmad Sutrisno, Selasa. "
           "Menurut Ahmad, gangguan disebabkan pohon tumbang yang menimpa jaringan.\n\n"
           "Wali Kota Bogor Dedie Rachim meminta PLN mempercepat pemulihan. Warga kecewa karena pemadaman "
           "berulang setiap pekan.")


@pytest.fixture
def fake_net(monkeypatch, tmp_path):
    from crawler.medmas import pipeline

    def collect(cfg, keyword, log=print, learned=None, db=None, batch=""):
        log(f"   (fake sources for {keyword})")
        return ([{"url": "https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor", "title": "Pemadaman listrik Bogor",
                  "date": "2026-11-09", "time": "20:15", "source": "google_news", "domain": "detik.com"},
                 {"url": "https://www.kompas.com/tag/pln", "title": "Berita PLN", "date": "2026-11-09", "time": "",
                  "source": "bing_news", "domain": "kompas.com"},
                 {"url": "https://radarbogor.id/2026/11/10/listrik-padam-lagi", "title": "Listrik padam lagi",
                  "date": "2026-11-10", "time": "07:00", "source": "sitemap", "domain": "radarbogor.id"}],
                {"detik.com", "radarbogor.id"})

    def extract(url, timeout=20):
        from crawler.medmas.persons import extract_people
        if "/tag/" in url:
            return {"title": "Berita PLN", "content": "daftar berita", "is_article": False, "date": "", "time": "",
                    "summary": "", "author": "", "image": "", "influencer": "", "persons": "", "media": "Kompas",
                    "method": "fake", "error": ""}
        text = ARTICLE if "detik" in url else "Gangguan jaringan membuat wilayah Cibinong gelap. Petugas sedang bekerja."
        infl, pers = extract_people(text)
        return {"title": "Pemadaman Listrik di Bogor" if "detik" in url else "Listrik padam lagi di Cibinong",
                "content": text, "is_article": True, "date": "", "time": "", "summary": text[:150],
                "author": "Rina", "image": "https://cdn.example/img.jpg", "influencer": ", ".join(infl),
                "persons": ", ".join(pers), "organizations": "", "media": "", "method": "fake", "error": ""}
    monkeypatch.setattr(pipeline, "collect_links", collect)
    monkeypatch.setattr(pipeline, "extract_article", extract)
    monkeypatch.setattr(pipeline, "_is_online", lambda timeout=4: True)


def test_multi_source_news_search_gives_the_team_online_file(tmp_path, monkeypatch, fake_net):
    from crawler import job, paths
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    j = job.Job()
    err = j.start_news(["pemadaman listrik"], "2026-11-09", "2026-11-10", "WIB",
                       {"engine": "multi", "template": "online", "exclude": "promo", "exact": True})
    assert err is None
    t = time.time()
    while j.running and time.time() - t < 60:
        time.sleep(0.1)
    s = j.state()
    assert not j.running and not s["error"], s["error"]
    logs = "\n".join(x if isinstance(x, str) else str(x) for x in s.get("logs", []))
    files = s["modes"]["news"]["files"]
    online = [f for f in files if "_Online_" in f and f.endswith(".xlsx")]
    assert online and any(f.startswith("news_links_review_") for f in files), (files, logs)
    ws = openpyxl.load_workbook(tmp_path / "out" / online[0]).active
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    by_url = {r["URL"]: r for r in rows}
    assert "https://www.kompas.com/tag/pln" not in by_url                      # a tag page is not an article
    d = by_url["https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor"]
    assert d["DATE"] == "09-Nov-2026" and d["TIME"] == "20:15:00" and d["MEDIA"] == "detik.com"
    assert "ahmad" in (d["INFLUENCER"] or "") and "dedie rachim" in (d["PERSON"] or "")    # speaker / person named
    assert d["SENTIMENT"] in ("negative", "positive", "neutral") and d["CONTENT"].startswith("<p>")
    # the keyword could not be confirmed in the Cibinong article -> needs review, not in the Online file
    assert "https://radarbogor.id/2026/11/10/listrik-padam-lagi" not in by_url
    # second run: the engine remembers links; the run still delivers the file
    err = j.start_news(["pemadaman listrik"], "2026-11-09", "2026-11-10", "WIB",
                       {"engine": "multi", "template": "online", "include_review": True})
    while j.running:
        time.sleep(0.1)
    assert not j.state()["error"]
    news = j.state()["modes"]["news"]
    assert news["files"]


def test_browser_engine_options_unchanged():
    from crawler.news import NewsOptions
    o = NewsOptions.from_dict({"template": "online"})
    assert o.engine == "google" and o.include_review is False         # old schedules keep the browser engine


def test_people_fill_online_rows_of_the_browser_engine():
    from crawler import templates
    from datetime import timezone
    row = templates.online_row({"url": "https://a.id/x", "title": "Listrik padam", "text": ARTICLE,
                                "published_full": "2026-11-09 20:15"}, timezone.utc)
    cols = templates.ONLINE_COLUMNS
    assert "ahmad" in row[cols.index("INFLUENCER")] and "dedie rachim" in row[cols.index("PERSON")]


def test_short_names_merge_into_the_full_name():
    from crawler.templates import merge_names
    assert merge_names("Ahmad Sutrisno, Ahmad, Dedie Rachim, Dedie") == "Ahmad Sutrisno, Dedie Rachim"
    assert merge_names("") == "" and merge_names("Budi") == "Budi"


def test_browser_takes_over_keywords_the_multi_engine_found_nothing_for(tmp_path, monkeypatch):
    from crawler import job, news, news_multi, paths
    from crawler.browser import Result
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))

    def fake_multi(kws, start, end, opt, tz, log, stop, data_dir, out_dir, on_row=None):
        return ([{"url": "https://a.id/1", "title": "Banjir Bogor", "media": "A", "published": "2026-11-10",
                  "ts": 1, "keyword": "banjir", "text": "Banjir melanda Bogor."}], [], {"batch": "b"})
    monkeypatch.setattr(news_multi, "run", fake_multi)
    searched = []

    class FakeBrowser:
        def __init__(self, *a, **k):
            pass

        def search(self, kw, opt, s, e):
            searched.append((kw, opt.engine, opt.detail))
            assert opt.sources == ["feed"]                                 # the feed only, never Google's search pages
            return Result("ok", [{"url": "https://b.id/2", "title": "Longsor", "media": "B", "published": "2026-11-10",
                                  "ts": 2}], "")

        def article(self, url):
            return {"text": "Longsor di Garut.", "final_url": url}
    monkeypatch.setattr(news, "NewsCrawler", FakeBrowser)
    monkeypatch.setattr(job, "launch_context", lambda *a, **k: object())
    j = job.Job()
    assert j.start_news(["banjir", "longsor"], "2026-11-10", "2026-11-10", "WIB",
                        {"engine": "multi", "template": "online", "resolve": False}) is None
    t = time.time()
    while j.running and time.time() - t < 60:
        time.sleep(0.1)
    s = j.state()
    assert not s["error"], s["error"]
    assert searched == []                       # nothing found is a normal result: no Google pages are opened
    assert {r["url"] for r in j.data["news"]["links"]} == {"https://a.id/1"}
    assert any("_Online_" in f for f in s["modes"]["news"]["files"])

    # an engine failure: every keyword goes to the browser; with fallback off nothing does
    def broken(*a, **k):
        raise RuntimeError("feedparser missing")
    monkeypatch.setattr(news_multi, "run", broken)
    searched.clear()
    j.start_news(["banjir"], "2026-11-10", "2026-11-10", "WIB", {"engine": "multi", "template": "online"})
    while j.running:
        time.sleep(0.1)
    assert searched == [("banjir", "google", "full")]                  # the engine failed: the browser takes over
    searched.clear()
    j.start_news(["banjir"], "2026-11-10", "2026-11-10", "WIB", {"engine": "multi", "fallback": False})
    while j.running:
        time.sleep(0.1)
    assert searched == []


def test_priority_media_always_first_and_coverage(tmp_path):
    from crawler import news_multi
    from crawler.medmas import pipeline
    from crawler.medmas.config import Config
    from crawler.medmas.db import Database
    from crawler.news import NewsOptions
    o = NewsOptions.from_dict({"engine": "multi", "coverage": "wide",
                               "priority_media": "https://www.kompas.com/, radarbogor.id\ndetik.com"})
    assert o.coverage == "wide" and o.priority_media == ["kompas.com", "radarbogor.id", "detik.com"]
    db = Database(str(tmp_path / "m.db"))
    pool = {f"outlet{i}.id" for i in range(50)} | {"kompas.com", "radarbogor.id"}
    learned = sorted(f"seen{i}.id" for i in range(30))          # outlets seen before would fill a small cap
    undo = news_multi._priority_first(pipeline, Database, o.priority_media)
    try:
        got = db.pick_sitemap_domains(pool, 5, first=learned)
        assert got[:3] == ["kompas.com", "radarbogor.id", "detik.com"] and len(got) == 5
        feeds = db.pick_feed_discovery(pool - {"kompas.com"}, 2)   # kompas.com: feed already known
        assert feeds[0] == "radarbogor.id" and "kompas.com" not in feeds
        cfg = Config(keywords=["pln"], max_media_harvest=3)
        assert pipeline._harvest_domains(cfg, set(learned), lambda m: None, db)[:3] == o.priority_media
    finally:
        undo()
    assert db.pick_sitemap_domains(pool, 5, first=learned)[0] == "seen0.id"   # back to the engine's own order
    db.close()


def test_sitemaps_and_feeds_read_once_per_run(monkeypatch):
    from crawler import news_multi
    from crawler.medmas.discovery import rss_feeds, sitemaps
    calls = []
    monkeypatch.setattr(sitemaps, "_fetch", lambda url, timeout=8: calls.append(url) or b"<urlset/>")
    monkeypatch.setattr(rss_feeds, "fetch_feed", lambda url, timeout=20, fallback=True: calls.append(url) or "feed")

    def dead(url, timeout=8):
        calls.append(url)
        raise TimeoutError("dead site")
    undo = news_multi._shared_reads()
    try:
        for _kw in range(5):                                       # five keywords in one run
            assert sitemaps._fetch("https://a.id/sitemap.xml") == b"<urlset/>"
            assert rss_feeds.fetch_feed("https://a.id/rss") == "feed"
        assert calls == ["https://a.id/sitemap.xml", "https://a.id/rss"]
        orig = sitemaps._fetch
        undo()
        monkeypatch.setattr(sitemaps, "_fetch", dead)
        undo = news_multi._shared_reads()
        for _kw in range(3):
            with pytest.raises(TimeoutError):
                sitemaps._fetch("https://dead.id/sitemap.xml")
        assert calls.count("https://dead.id/sitemap.xml") == 1      # a dead site costs one try per run
        assert orig
    finally:
        undo()


def test_online_people_left_empty_when_the_engine_found_none():
    """Like the team's Uploader Online v2: an empty INFLUENCER / PERSON from the crawl stays empty."""
    from crawler import templates
    from datetime import timezone
    cols = templates.ONLINE_COLUMNS
    row = templates.online_row({"url": "https://a.id/x/", "title": "PLN", "text": "Listrik di Sukabumi pulih.",
                                "influencer": "", "person": ""}, timezone.utc)
    assert row[cols.index("INFLUENCER")] == "" and row[cols.index("PERSON")] == ""
    assert row[cols.index("URL")] == "https://a.id/x" and row[cols.index("MEDIA")] == "a.id"


def test_articles_arrive_live_and_quick_sources_first(tmp_path, monkeypatch, fake_net):
    """Each accepted article is handed over the moment it is read; quick sources run for every keyword before the
    slow sitemap sweep; with "only new" the live-saved articles still make it into this run's file."""
    from crawler import job, news_multi, paths
    from crawler.medmas import pipeline
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    calls = []
    fake = pipeline.collect_links

    def spy(cfg, keyword, log=print, learned=None, db=None, batch=""):
        calls.append((keyword, cfg.use_sitemap, cfg.use_google_news))
        return fake(cfg, keyword, log, learned, db=db, batch=batch)
    monkeypatch.setattr(pipeline, "collect_links", spy)
    live_at = []
    orig_run = news_multi.run

    def run(*a, **k):
        cb = k["on_row"]
        k["on_row"] = lambda r: (live_at.append((r["url"], j.running)), cb(r))
        return orig_run(*a, **k)
    monkeypatch.setattr(news_multi, "run", run)
    j = job.Job()
    j._new_only = True
    assert j.start_news(["pemadaman listrik", "pln"], "2026-11-09", "2026-11-10", "WIB",
                        {"engine": "multi", "template": "online"}) is None
    t = time.time()
    while j.running and time.time() - t < 60:
        time.sleep(0.1)
    s = j.state()
    assert not s["error"], s["error"]
    detik = "https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor"
    assert (detik, True) in live_at                                    # handed over while the job was still running
    # quick pass for both keywords first (no sitemaps), then the slow sweep (sitemaps, no Google News)
    assert [c[1:] for c in calls[:2]] == [(False, True), (False, True)] and calls[0][0] != calls[1][0]
    assert [c[1:] for c in calls[2:]] == [(True, False), (True, False)]
    online = [f for f in s["modes"]["news"]["files"] if "_Online_" in f and f.endswith(".xlsx")]
    ws = openpyxl.load_workbook(tmp_path / "out" / online[0]).active
    assert detik in [c.value for c in ws["D"]]                        # saved live, still in this run's file
    from crawler import archive
    assert archive.stats()["online"]["items"] >= 1



def test_spokesperson_columns_keep_only_people():
    """INFLUENCER / PERSON: pronouns, ordinary words, bare titles and institutions are dropped; ranks, places and
    units glued in front of a name are removed."""
    from crawler import templates
    from datetime import timezone
    cols = templates.ONLINE_COLUMNS
    row = templates.online_row({"url": "https://a.id/x", "title": "PLN", "text": "x",
                                "influencer": "Kami, Saya, Menurutnya, Sumsel Irjen Pol Rony Samtana, Kapolresta",
                                "person": "Rusia Vladimir Putin, Kostrad, Pertamina, Lapas Kotabaru, Irjen Pol, "
                                          "Presiden Prabowo Subianto, Imam Sapto Riadi, Dedi, Pertumbuhan"}, timezone.utc)
    assert row[cols.index("INFLUENCER")] == "rony samtana"
    assert row[cols.index("PERSON")] == "vladimir putin,prabowo subianto,imam sapto riadi,dedi"


def test_site_pages_never_in_the_online_file():
    from crawler import templates
    from datetime import timezone
    rows = [{"url": u, "title": t, "text": "Isi halaman yang cukup panjang. " * 10} for t, u in [
        ("Local news, breaking news, weather and traffic", "https://a.com/"),
        ("Terms of Use", "https://a.com/terms-of-use"), ("Privacy Policy", "https://a.com/privacy-policy"),
        ("Just a moment...", "https://a.com/berita/x"),
        ("Mengungkap Fakta Tersembunyi di Balik Stasiun Kereta Api Tertua", "https://a.com/travel/2026/11/20/stasiun")]]
    cols, values = templates.rows_for("online", rows, timezone.utc)
    assert [v[cols.index("TITLE")] for v in values] == ["Mengungkap Fakta Tersembunyi di Balik Stasiun Kereta Api Tertua"]


def test_bulk_links_read_every_pasted_article(tmp_path, monkeypatch, fake_net):
    """Bulk links: pasted links are read like search results — no date / keyword filter — into the Online file,
    the dashboard and the reporting database (News Stream / Reports)."""
    from crawler import archive, gnews, job, news_multi, paths
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(gnews.Decoder, "decode", lambda self, u: ("https://radarbogor.id/2026/11/10/listrik-padam-lagi",
                                                                  "OK"))
    assert news_multi.clean_links("x https://a.id/1, https://a.id/1\nftp://b <https://c.id/2>") == \
        ["https://a.id/1", "https://c.id/2"]
    j = job.Job()
    assert j.start_bulk("tidak ada link") and "Paste" in j.start_bulk("")
    text = ("https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor\n"
            "https://www.kompas.com/tag/pln\n"
            "https://news.google.com/rss/articles/CBMiabc\n")
    assert j.start_bulk(text) is None
    t = time.time()
    while j.running and time.time() - t < 60:
        time.sleep(0.1)
    s = j.state()
    assert not s["error"], s["logs"]
    news = s["modes"]["news"]
    assert news["count"] == 2                      # the Cibinong article (no keyword needed) + detik; not the tag page
    online = [f for f in news["files"] if "_Online_" in f and f.endswith(".xlsx")]
    ws = openpyxl.load_workbook(tmp_path / "out" / online[0]).active
    head = [c.value for c in ws[1]]
    urls = {dict(zip(head, [c.value for c in r]))["URL"] for r in ws.iter_rows(min_row=2)}
    assert urls == {"https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor",
                    "https://radarbogor.id/2026/11/10/listrik-padam-lagi"}
    assert archive.stats()["online"]["items"] == 2
    assert j.results_view("news")["statuses"][0]["note"].startswith("2 of 3 links read")


def test_news_search_takes_articles_the_live_stream_already_read(tmp_path, monkeypatch, fake_net):
    """A link the Live Stream already read is not downloaded again: its text comes from the stream's database."""
    import zlib

    from crawler import job, paths, stream
    from crawler.medmas import pipeline
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    url = "https://www.detik.com/jabar/berita/d-1/pemadaman-listrik-bogor"
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    with s.lock:
        s._db.execute("INSERT INTO articles(key, url, domain, title, status, discovered, fetched, pub_date, pub_time, "
                      "content, author) VALUES (?,?,?,?, 'success', 1, 1, '2026-11-09', '20:15', ?, 'Rina')",
                      (stream.norm_key(url), url, "detik.com", "Pemadaman Listrik di Bogor", zlib.compress(ARTICLE.encode())))
        s._db.commit()
    fetched = []
    real = pipeline.extract_article
    monkeypatch.setattr(pipeline, "extract_article", lambda u, timeout=20: fetched.append(u) or real(u, timeout))
    j = job.Job()
    assert j.start_news(["pemadaman listrik"], "2026-11-09", "2026-11-10", "WIB",
                        {"engine": "multi", "template": "online"}) is None
    t = time.time()
    while j.running and time.time() - t < 60:
        time.sleep(0.1)
    st = j.state()
    assert not st["error"]
    assert url not in fetched and fetched                                   # the other links were downloaded
    assert j.run_stats["reused"] == 1 and any("taken from the Live Stream" in x for x in st["logs"])
    assert any(x.startswith("[") and "📊 This run:" in x for x in st["logs"]) and j.run_stats["new"] >= 1
