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
