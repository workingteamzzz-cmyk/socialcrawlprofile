"""News Search with the 'Social media' target: Google Search with site: per platform -> the Medsos layout."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openpyxl  # noqa: E402

from crawler.news import NewsOptions, google_query, social_platform  # noqa: E402


def test_options_and_query():
    o = NewsOptions.from_dict({"target": "social", "social": ["instagram", "threads"], "engine": "multi",
                               "template": "online", "detail": "links"})
    assert (o.engine, o.sources, o.media_only, o.template, o.detail) == ("google", ["all_tab"], False, "medsos", "basic")
    assert google_query("pln", o) == "pln (site:instagram.com OR site:threads.com OR site:threads.net)"
    assert NewsOptions.from_dict({"target": "social"}).social == ["instagram", "threads", "tiktok", "x", "facebook", "youtube"]
    assert NewsOptions.from_dict({}).target == "news"                      # old searches / schedules unchanged


def test_post_links():
    assert social_platform("https://www.instagram.com/p/C1x2/") == ("instagram", True)
    assert social_platform("https://www.instagram.com/reel/C1x2/?igsh=1") == ("instagram", True)
    assert social_platform("https://www.instagram.com/pln_id/") == ("instagram", False)
    assert social_platform("https://www.instagram.com/explore/tags/pln/") == ("instagram", False)
    assert social_platform("https://www.threads.net/@pln_id/post/DA1") == ("threads", True)
    assert social_platform("https://m.facebook.com/plnid/posts/123") == ("facebook", True)
    assert social_platform("https://twitter.com/pln/status/9") == ("x", True)
    assert social_platform("https://www.youtube.com/watch?v=abc") == ("youtube", True)
    assert social_platform("https://www.kompas.com/a") == ("", False)


def test_job_writes_the_medsos_file(tmp_path, monkeypatch):
    from crawler import details, job, news, paths
    from crawler.browser import Result
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    seen_opt = []

    class FakeBrowser:
        def __init__(self, *a, **k):
            pass

        def search(self, kw, opt, s, e):
            seen_opt.append((opt.target, opt.sources, opt.sites))
            return Result("ok", [{"url": "https://www.instagram.com/p/C1x2/", "title": "PLN padam lagi", "media": "Instagram",
                                  "published": "2026-11-12", "ts": time.time() - 86400, "platform": "instagram",
                                  "author": "", "text": "PLN padam lagi — warga kecewa"},
                                 {"url": "https://x.com/budi/status/1856", "title": "listrik mati", "media": "X",
                                  "published": "2026-11-12", "ts": time.time() - 86400, "platform": "x",
                                  "author": "budi", "text": "listrik mati"}], "")

    monkeypatch.setattr(news, "NewsCrawler", FakeBrowser)
    monkeypatch.setattr(job, "launch_context", lambda *a, **k: object())
    monkeypatch.setattr(details, "read_details", lambda c, plat, url: {"caption": "PLN padam lagi di Bogor", "likes": 12,
                                                                       "comments": 3, "author": "pln_id" if plat == "instagram" else "budi"})
    j = job.Job()
    y = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    assert j.start_news(["pln"], y, y, "WIB", {"target": "social", "social": ["instagram", "x"], "detail": "full"}) is None
    while j.running:
        time.sleep(0.1)
    s = j.state()
    assert not s["error"], s["error"]
    assert seen_opt[0] == ("social", ["all_tab"], ["instagram.com", "x.com", "twitter.com"])
    xlsx = [f for f in s["modes"]["news"]["files"] if "_Medsos_" in f and f.endswith(".xlsx")]
    ws = openpyxl.load_workbook(tmp_path / "out" / xlsx[0]).active
    head = [c.value for c in ws[1]]
    rows = [dict(zip(head, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    ig = next(r for r in rows if r["Platform"] == "instagram")
    assert ig["Username"] == "pln_id" and ig["Text"] == "PLN padam lagi di Bogor" and ig["Likes"] == 12
    assert ig["Sentiment"] and ig["Post ID"]
    from crawler import archive
    assert archive.stats()["medsos"]["items"] == 2
