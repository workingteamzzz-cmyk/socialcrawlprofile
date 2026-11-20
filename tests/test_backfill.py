"""Media archive: one outlet over a period from its sitemaps — dated archives outside the period skipped, articles
read, kept through the stream's clean-up, one Excel."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def site(monkeypatch):
    from crawler.medmas.discovery import sitemaps as sm
    months = [(y, m) for y in (2023, 2024, 2025, 2026) for m in range(1, 13) if (y, m) <= (2026, 11)]
    idx = "".join(f"<sitemap><loc>https://warta.id/sitemap-{y}-{m:02d}.xml</loc></sitemap>" for y, m in months)
    pages = {"https://warta.id/robots.txt": "User-agent: *\nSitemap: https://warta.id/sitemap_index.xml\n",
             "https://warta.id/sitemap_index.xml": f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{idx}'
                                                  '<sitemap><loc>https://warta.id/image-sitemap.xml</loc></sitemap></sitemapindex>'}
    for y, m in months:
        urls = "".join(f"<url><loc>https://warta.id/berita/{y}/{m:02d}/{d:02d}/kabar-penting-nomor-{d}</loc>"
                       f"<lastmod>{y}-{m:02d}-{d:02d}T10:00:00+07:00</lastmod></url>" for d in (5, 20))
        pages[f"https://warta.id/sitemap-{y}-{m:02d}.xml"] = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    fetched = []

    def fetch(url, timeout=8):
        fetched.append(url)
        if url not in pages:
            raise OSError("404")
        return pages[url].encode()
    monkeypatch.setattr(sm, "_fetch", fetch)
    return fetched


def test_archive_one_outlet_over_two_years(tmp_path, monkeypatch, site):
    import openpyxl
    from crawler import backfill, paths, stream
    from crawler.medmas import extract
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    s = stream.Stream(str(tmp_path / "data" / "stream"))
    s.cfg = stream.settings()
    with pytest.raises(ValueError):
        backfill.start(s, "warta.id", "2026-01-01", "2025-01-01")
    j = backfill.start(s, "https://www.warta.id/", "2024-07-01", "2026-06-30", gap=0.5)
    assert j["domain"] == "warta.id" and j["status"] == "queued"
    for _ in range(10):
        backfill.step(s, budget_s=60)
    j = backfill.job(s, j["id"])
    assert j["status"] == "reading" and j["found"] == 48 and j["waiting"] == 48          # 24 months x 2 articles
    assert not any("2023" in u or "2026-07" in u or "image" in u for u in site if "sitemap-" in u)   # skipped, unread

    def fake(url, timeout=20):
        d = "-".join(url.split("/berita/")[1].split("/")[:3])
        text = "Isi berita arsip yang cukup panjang untuk dianggap artikel. " * 6
        return {"url": url, "media": "warta.id", "title": f"Kabar {d}", "author": "", "date": d, "time": "10:00",
                "summary": text[:60], "content": text, "image": "", "influencer": "", "persons": "", "organizations": "",
                "method": "", "error": "", "is_index": False, "is_article": True}
    monkeypatch.setattr(extract, "extract_article", fake)
    for r in s.q("SELECT key, url, domain, title, feed_date, feed_time, source FROM articles"):
        s.read(dict(r))
    backfill.step(s)
    j = backfill.job(s, j["id"])
    assert j["status"] == "done" and j["read"] == 48 and j["waiting"] == 0
    s.cfg["retention_days"] = 1
    s.x("UPDATE articles SET discovered=?", (time.time() - 30 * 86400,))
    assert s.purge() == 0                                                  # archive articles are kept for good
    name = backfill.export(s, j["id"], str(tmp_path / "out"))
    ws = openpyxl.load_workbook(tmp_path / "out" / name).active
    assert ws.max_row == 49 and name.startswith("Media_warta_id_20240701-20260630")
    assert backfill.delete(s, j["id"]) and s.q("SELECT COUNT(*) n FROM articles")[0]["n"] == 0
    s.stop()
