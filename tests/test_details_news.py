"""Caption/engagement readers and the Google News collector, against fake pages."""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_crawler import _browser_path  # noqa: E402

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))


def test_parse_count():
    from crawler.details import parse_count
    cases = {"987": 987, "1,234": 1234, "1.234": 1234, "1.234.567": 1234567, "1.2K": 1200, "1,2 rb": 1200,
             "12,5 rb": 12500, "3 jt": 3_000_000, "12.5M": 12_500_000, "2,1 juta": 2_100_000, "": None, "abc": None, 42: 42}
    for text, want in cases.items():
        assert parse_count(text) == want, text


def test_news_helpers():
    from crawler.news import NewsOptions, google_news_url, google_query, parse_news_date
    now = datetime(2026, 11, 2, 10, tzinfo=WIB)
    assert parse_news_date("3 hari lalu", now).date() == datetime(2026, 10, 30).date()
    assert parse_news_date("2 hours ago", now) == now - timedelta(hours=2)
    assert parse_news_date("5 Okt 2026", now).date().isoformat() == "2026-10-05"
    assert parse_news_date("Oct 5, 2026", now).date().isoformat() == "2026-10-05"
    assert parse_news_date("Kompas.com", now) is None
    o = NewsOptions.from_dict({"exact": True, "exclude": "promo", "sites": "https://www.kompas.com/x, detik.com, bad site",
                               "detail": "full", "cap": "50"})
    assert o.sites == ["kompas.com", "detik.com"] and o.detail == "full" and o.cap == 50
    q = google_query("tarif listrik", o)
    assert q == '"tarif listrik" -promo (site:kompas.com OR site:detik.com)'
    url = google_news_url(q, datetime(2026, 10, 1), datetime(2026, 10, 31), "id")
    assert "tbm=nws" in url and "cd_min:10%2F01%2F2026" in url and "cd_max:10%2F31%2F2026" in url and "gl=ID" in url
    assert NewsOptions.from_dict({"detail": "weird"}).detail == "basic"


# ---------------- fake pages ----------------
TIKTOK_DATA = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {
    "desc": "Video caption #listrik", "stats": {"diggCount": 1500, "commentCount": 12, "shareCount": 3, "playCount": 45000}}}}}}

DETAIL_PAGES = {
    "x.com/u/status/1": '<article data-testid="tweet"><div data-testid="tweetText">Tweet text here</div>'
                        '<div role="group" aria-label="12 replies, 34 reposts, 1,234 likes, 5 bookmarks, 9,876 views"></div></article>',
    "instagram.com/p/ABC": '<html><head><meta property="og:description" content="1.234 suka, 56 komentar - pu_sda pada '
                           '5 Oktober 2026: &quot;Caption Instagram&quot;"></head><body>post</body></html>',
    "tiktok.com/@a/video/1": '<html><body><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                             + json.dumps(TIKTOK_DATA) + '</script></body></html>',
    "youtube.com/watch?v=abcdefghijk": '<html><script>var ytInitialPlayerResponse = {"videoDetails":{"videoId":"abcdefghijk",'
                                       '"title":"Video Title","shortDescription":"Line 1\\nLine 2","viewCount":"12345"}};'
                                       'var d = {"label":"1,2 rb suka"};</script></html>',
    "threads.com/@u/post/CODE1": '<html><head><meta property="og:description" content="fallback"></head><script>'
                                 '{"code":"CODE1","caption":{"text":"Threads caption"},"like_count":77,'
                                 '"text_post_app_info":{"direct_reply_count":5,"repost_count":2,"quote_count":1}}</script></html>',
    "facebook.com/page/posts/1": '<html><head><meta property="og:description" content="Facebook caption"></head>'
                                 '<body><div>Semua reaksi: 1,2 rb</div><div>34 komentar</div><div>5 kali dibagikan</div></body></html>',
}

GOOGLE_P1 = """<html><body><div id="rso">
<a href="https://www.kompas.com/a1"><div>Kompas.com</div><div role="heading">Tarif listrik naik</div><div>snippet</div><span>5 Okt 2026</span></a>
<a href="https://news.detik.com/a2"><div>detikNews</div><div role="heading">PLN umumkan</div><span>12 Okt 2026</span></a>
<a href="https://old.example.com/a0"><div>Old</div><div role="heading">Too old</div><span>1 Jan 2025</span></a>
<a href="https://www.google.com/search?q=related"><div role="heading">related search</div></a>
</div><a id="pnnext" href="/search?q=x&start=10">Next</a></body></html>"""
GOOGLE_P2 = """<html><body><div id="rso">
<a href="https://www.cnnindonesia.com/a3"><div>CNN Indonesia</div><div role="heading">Listrik padam</div><span>20 Okt 2026</span></a>
<a href="https://www.kompas.com/a1"><div>Kompas.com</div><div role="heading">Tarif listrik naik</div><span>5 Okt 2026</span></a>
</div></body></html>"""
ARTICLE = """<html><head><meta property="og:title" content="Tarif listrik naik mulai November">
<meta property="og:site_name" content="Kompas.com"><meta property="article:published_time" content="2026-10-05T08:30:00+07:00">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"x","author":{"@type":"Person","name":"Budi"}}</script>
</head><body><nav><p>menu menu menu menu menu menu menu menu menu menu menu</p></nav><article>
<p>JAKARTA, KOMPAS.com - Pemerintah menaikkan tarif listrik untuk golongan tertentu mulai bulan depan.</p>
<p>Baca juga: Artikel lain yang tidak relevan untuk isi berita ini sama sekali.</p>
<p>Kenaikan ini disebut sejalan dengan perubahan kurs dan harga energi primer dunia pada triwulan ini.</p>
</article></body></html>"""


def _route_for(pages):
    def route(r):
        url = r.request.url
        if "google.com/search" in url:
            body = GOOGLE_P2 if "start=10" in url else GOOGLE_P1
        elif url.startswith("https://www.kompas.com/a1"):
            body = ARTICLE
        else:
            body = next((v for k, v in pages.items() if k in url), "<html><body>generic article <p>short</p></body></html>")
        r.fulfill(status=200, content_type="text/html", body=body)
    return route


@pytest.fixture()
def browser_ctx(tmp_path):
    if _browser_path() is None:
        pytest.skip("no local Chromium")
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "profile"), headless=True)
        ctx.route("**/*", _route_for(DETAIL_PAGES))
        yield ctx
        ctx.close()


def _fast(c):
    c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(100)
    return c


def test_detail_readers(browser_ctx):
    from crawler.browser import BrowserCrawler
    from crawler.details import read_details
    c = _fast(BrowserCrawler(browser_ctx, lambda m: None, threading.Event(), datetime(2026, 10, 1, tzinfo=UTC),
                             datetime(2026, 10, 31, tzinfo=UTC)))
    d = read_details(c, "x", "https://x.com/u/status/1")
    assert d == {"caption": "Tweet text here", "likes": 1234, "comments": 12, "shares": 34, "views": 9876}
    d = read_details(c, "instagram", "https://www.instagram.com/p/ABC/")
    assert (d["caption"], d["likes"], d["comments"]) == ("Caption Instagram", 1234, 56)
    d = read_details(c, "tiktok", "https://www.tiktok.com/@a/video/1")
    assert d == {"caption": "Video caption #listrik", "likes": 1500, "comments": 12, "shares": 3, "views": 45000}
    d = read_details(c, "youtube", "https://www.youtube.com/watch?v=abcdefghijk")
    assert d["caption"] == "Video Title\n\nLine 1\nLine 2" and d["views"] == 12345 and d["likes"] == 1200
    d = read_details(c, "threads", "https://www.threads.com/@u/post/CODE1")
    assert d == {"caption": "Threads caption", "likes": 77, "comments": 5, "shares": 3, "views": None}
    d = read_details(c, "facebook", "https://www.facebook.com/page/posts/1")
    assert (d["caption"], d["likes"], d["comments"], d["shares"]) == ("Facebook caption", 1200, 34, 5)


def test_google_news_search_and_article(browser_ctx):
    from crawler.news import NewsCrawler, NewsOptions
    start, end = datetime(2026, 9, 30, 17, tzinfo=UTC), datetime(2026, 10, 31, 16, 59, tzinfo=UTC)
    c = _fast(NewsCrawler(browser_ctx, lambda m: None, threading.Event(), start, end, tz=WIB))
    res = c.search("listrik", NewsOptions(), datetime(2026, 10, 1), datetime(2026, 10, 31))
    assert res.status == "ok", res
    got = {r["url"]: r for r in res.posts}
    assert set(got) == {"https://www.kompas.com/a1", "https://news.detik.com/a2", "https://www.cnnindonesia.com/a3"}
    assert got["https://www.kompas.com/a1"]["media"] == "Kompas.com"
    assert got["https://www.kompas.com/a1"]["title"] == "Tarif listrik naik"
    assert got["https://www.cnnindonesia.com/a3"]["published"] == "2026-10-20"
    assert "2 result pages" in res.note
    res = c.search("listrik", NewsOptions(cap=2), datetime(2026, 10, 1), datetime(2026, 10, 31))
    assert len(res.posts) == 2

    a = c.article("https://www.kompas.com/a1")
    assert a["title"] == "Tarif listrik naik mulai November" and a["site"] == "Kompas.com" and a["author"] == "Budi"
    assert a["published_full"] == "2026-10-05 08:30"
    assert "menaikkan tarif listrik" in a["text"] and "Baca juga" not in a["text"] and "menu menu" not in a["text"]


def test_excel_survives_control_characters_and_long_links(tmp_path):
    import openpyxl
    from crawler.news import NewsOptions
    from crawler.output import write_news_outputs
    long_url = "https://example.com/" + "a" * 2100
    rows = [{"url": "https://a.b/c", "keyword": "k", "keywords": ["k"], "title": "bad\x0bchar\x08", "text": "x" * 40000, "ts": 0},
            {"url": long_url, "keyword": "k", "keywords": ["k"], "title": "long", "ts": 0}]
    meta = {"options": NewsOptions(detail="full"), "start": "a", "end": "b", "tz_name": "WIB", "created": "n", "keywords": ["k"]}
    write_news_outputs(str(tmp_path / "n.xlsx"), str(tmp_path / "n.csv"), rows, [], meta)
    ws = openpyxl.load_workbook(tmp_path / "n.xlsx")["News Links"]
    vals = list(ws.values)
    assert "badchar" in vals[1] and len(vals[1][-1]) <= 32000


def test_locked_output_file_does_not_stop_the_run():
    from crawler.job import Job
    j = Job()

    def locked():
        raise PermissionError("file is open")
    j._safe_save(locked, "/x/post_links.xlsx")
    j._safe_save(locked, "/x/post_links.xlsx")
    assert sum("is it open in Excel" in line for line in j.logs) == 1   # warned once, no exception
