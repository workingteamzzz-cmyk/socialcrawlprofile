"""Regression tests for issues found in the final review."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_crawler import _browser_path, _fake_feed_html, tt_id  # noqa: E402

UTC = timezone.utc
HEAD = "Group;Platform;Handle;URL;Status;Source;Notes"


@pytest.fixture()
def lists_dir(tmp_path, monkeypatch):
    from crawler import store
    monkeypatch.setattr(store, "LISTS_DIR", str(tmp_path / "lists"))
    monkeypatch.setattr(store, "LEGACY_BASE", str(tmp_path / "a.csv"))
    monkeypatch.setattr(store, "LEGACY_CUSTOM", str(tmp_path / "b.csv"))
    os.makedirs(tmp_path / "lists")
    return tmp_path / "lists"


def test_semicolon_csv_from_excel_is_read_and_kept(lists_dir):
    from crawler import store
    (lists_dir / "Banks.csv").write_bytes(("﻿" + HEAD + "\r\nBank A;Instagram;bank_a;https://www.instagram.com/bank_a/;Added;manual;\r\n"
                                           "Bank B;X;bank_b;https://x.com/bank_b;Added;manual;\r\n").encode("utf-8"))
    assert [a["handle"] for a in store.load("Banks")] == ["bank_a", "bank_b"]
    assert store.lists()[0]["count"] == 2
    store.add("Banks", "https://www.tiktok.com/@bank_c", "Bank C")
    text = (lists_dir / "Banks.csv").read_text(encoding="utf-8-sig")
    assert text.splitlines()[0] == HEAD                        # still semicolons, so Excel keeps opening it
    assert [a["handle"] for a in store.load("Banks")] == ["bank_a", "bank_b", "bank_c"]   # nothing wiped


def test_non_utf8_and_broken_lists_do_not_break_the_others(lists_dir):
    from crawler import store
    (lists_dir / "Old.csv").write_bytes("Group,Platform,Handle,URL\r\nCaf\xe9,Instagram,cafe,https://www.instagram.com/cafe/\r\n".encode("cp1252"))
    (lists_dir / "Weird.csv").write_text("just some text\nwithout columns\n", encoding="utf-8")
    assert {l["id"] for l in store.lists()} == {"Old", "Weird"}
    assert store.load("Old")[0]["group"] == "Café"
    r = store.add("Weird", "https://x.com/abc", "")
    assert r["added"] == [] and "no URL column" in r["errors"][0]
    assert (lists_dir / "Weird.csv").read_text(encoding="utf-8") == "just some text\nwithout columns\n"   # untouched
    assert store.create_list("con")[0] == "con list"


def test_excel_keeps_equals_text_as_text(tmp_path):
    import openpyxl
    from crawler.news import NewsOptions
    from crawler.output import write_news_outputs
    rows = [{"url": "https://a.b/c", "keyword": "=== INFO ===", "keywords": ["k"], "title": "=)", "ts": 0}]
    meta = {"options": NewsOptions(detail="basic"), "start": "a", "end": "b", "tz_name": "WIB", "created": "n", "keywords": ["k"]}
    write_news_outputs(str(tmp_path / "n.xlsx"), str(tmp_path / "n.csv"), rows, [], meta)
    cells = openpyxl.load_workbook(tmp_path / "n.xlsx")["News Links"][2]
    assert [(c.value, c.data_type) for c in cells[1:2]] == [("=== INFO ===", "s")]
    assert "'=== INFO ===" in (tmp_path / "n.csv").read_text(encoding="utf-8-sig")


def test_iso_dates_with_millis_and_no_colon_offset():
    from crawler.browser import _parse_iso
    assert _parse_iso("2026-10-05T14:30:00.000+07:00").isoformat() == "2026-10-05T14:30:00+07:00"
    assert _parse_iso("2026-10-05T14:30:00+0700").isoformat() == "2026-10-05T14:30:00+07:00"
    assert _parse_iso("2026-10-05T07:30:00.1234567Z").isoformat().startswith("2026-10-05T07:30:00.123456")


def test_log_keeps_flowing_past_3000_lines():
    from crawler.job import Job
    j = Job()
    for i in range(3500):
        j.log(f"line {i}")
    s = j.state(since=3400)
    assert s["log_count"] == 3500 and len(s["logs"]) == 100 and s["logs"][-1].endswith("line 3499")
    assert j.state(since=3500)["logs"] == []


def test_failed_browser_start_reports_error_and_writes_no_files(tmp_path, monkeypatch):
    from crawler import job
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))

    def no_browser(*a, **k):
        raise RuntimeError("Could not open a browser. chrome: not found")
    monkeypatch.setattr(job, "launch_context", no_browser)
    j = job.Job()
    first = j.job_id
    assert j.start_crawl("https://www.instagram.com/abc", "2026-10-01", "2026-10-31", "WIB", ["instagram"]) is None
    for _ in range(100):
        if not j.running:
            break
        time.sleep(0.05)
    s = j.state()
    assert s["job_id"] == first + 1 and "Could not open a browser" in s["error"]
    assert s["modes"]["profiles"]["files"] == [] and not os.listdir(tmp_path / "out")


def test_origin_check_is_exact():
    from http.server import ThreadingHTTPServer

    from crawler import server
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/api/stop"
    try:
        for origin, ok in [("http://localhost.attacker.example", False), ("http://127.0.0.1:8770", True)]:
            req = urllib.request.Request(url, data=b"{}", headers={"Origin": origin, "Content-Type": "text/plain"})
            try:
                urllib.request.urlopen(req, timeout=10)
                assert ok, origin
            except urllib.error.HTTPError as e:
                assert not ok and e.code == 403
    finally:
        httpd.shutdown()


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_stop_keeps_links_collected_so_far(tmp_path):
    from playwright.sync_api import sync_playwright

    from crawler.accounts import parse_line
    from crawler.browser import BrowserCrawler, Stopped, launch_context
    dates = [datetime(2026, 10, 30 - i, tzinfo=UTC) for i in range(25)]
    page = _fake_feed_html([[f"https://www.tiktok.com/@abc/video/{tt_id(d)}", ""] for d in dates], batch=3)
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    stop = threading.Event()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=page))
        c = BrowserCrawler(ctx, lambda m: None, stop, datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
        rounds = {"n": 0}

        def pause(lo=0, hi=0):
            rounds["n"] += 1
            if rounds["n"] == 4:
                stop.set()
            c.page.wait_for_timeout(100)
            c._check_stop()
        c._pause = pause
        with pytest.raises(Stopped) as e:
            c.tiktok(parse_line("https://www.tiktok.com/@abc"))
        assert len(e.value.posts) >= 3          # what was on the page when Stop was pressed
        ctx.close()


# ---------------- engine review ----------------

def test_article_key_merges_url_variants():
    from crawler.news import _key
    a = _key("https://www.kompas.com/read/2026/10/20/abc?utm_source=google&utm_medium=x#top")
    assert a == _key("http://m.kompas.com/read/2026/10/20/abc/") == _key("https://amp.kompas.com/read/2026/10/20/abc/amp")
    assert a != _key("https://www.kompas.com/read/2026/10/20/abd")
    assert _key("https://site.example/a?id=5") != _key("https://site.example/a?id=6")


def _news_ctx(tmp_path, route):
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    pw = sync_playwright().start()
    ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
    ctx.route("**/*", route)
    return pw, ctx


def _news_crawler(ctx, stop=None, start=datetime(2026, 10, 19, 17, tzinfo=UTC), end=datetime(2026, 10, 20, 16, 59, tzinfo=UTC)):
    from datetime import timedelta as td

    from crawler.news import NewsCrawler
    c = NewsCrawler(ctx, lambda m: None, stop or threading.Event(), start, end, tz=timezone(td(hours=7)))
    c._pause = lambda lo=0, hi=0: (c.page.wait_for_timeout(50), c._check_stop())
    return c


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_news_engine_review_fixes(tmp_path):
    from crawler.browser import Stopped
    from crawler.news import NewsOptions
    feed = ('<?xml version="1.0"?><rss><channel>' + "".join(
        f"<item><title>Berita {i} - Media</title><link>https://news.google.com/rss/articles/G{i}</link>"
        f"<pubDate>Tue, 20 Oct 2026 0{i}:00:00 GMT</pubDate><source url='https://m.example'>Media</source></item>" for i in range(5))
        + "</channel></rss>")
    google = """<html><body><div id="rso">
      <a href="https://a.example/1"><div>Media A</div><div role="heading">Arus lalu lintas tidak biasa di tol Cikampek</div><span>20 Okt 2026</span></a>
      <a href="https://a.example/2"><div>Media B</div><div role="heading">Berita kemarin sebelum periode</div><span>19 Okt 2026</span></a>
    </div></body></html>"""

    def route(r):
        url = r.request.url
        if "/rss/search" in url:
            return r.fulfill(status=200, content_type="application/xml", body=feed)
        if "/rss/articles/" in url:
            gid = url.rsplit("/", 1)[-1]
            return r.fulfill(status=200, content_type="text/html", body=(
                "<html><body><iframe src='https://ads.doubleclick.net/x'></iframe>"
                f"<script>setTimeout(() => location.href = 'https://media.example/{gid}', 400)</script></body></html>"))
        if "google.com/search" in url:
            return r.fulfill(status=200, content_type="text/html", body=google)
        r.fulfill(status=200, content_type="text/html", body="<html><body>x</body></html>")
    pw, ctx = _news_ctx(tmp_path, route)
    try:
        c = _news_crawler(ctx)
        # ordinary traffic news is not a captcha; an absolute date the day before the period is dropped
        t = time.time()
        res = c.search("lalu lintas", NewsOptions(source="search", by_day=False), datetime(2026, 10, 20), datetime(2026, 10, 20))
        assert time.time() - t < 30 and [p["url"] for p in res.posts] == ["https://a.example/1"]
        assert "1 outside the period dropped" in res.note
        # the article's own link, not the ad iframe
        res = c.search("x", NewsOptions(source="news", by_day=False, cap=1), datetime(2026, 10, 20), datetime(2026, 10, 20))
        assert res.posts[0]["url"] == "https://media.example/G4"
        # Stop while links are being resolved keeps the whole batch
        stop = threading.Event()
        c2 = _news_crawler(ctx, stop)
        real = c2._resolve
        calls = {"n": 0}

        def resolve(u):
            calls["n"] += 1
            if calls["n"] == 3:
                stop.set()
                c2._check_stop()
            return real(u)
        c2._resolve = resolve
        with pytest.raises(Stopped) as e:
            c2.search("x", NewsOptions(source="news", by_day=False), datetime(2026, 10, 20), datetime(2026, 10, 20))
        urls = [p["url"] for p in e.value.posts]
        assert len(urls) == 5 and sum(u.startswith("https://media.example/") for u in urls) == 2
    finally:
        ctx.close()
        pw.stop()
