"""Regression tests for issues found in the final review."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

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
        if "/articles/" in url:
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
        res = c.search("lalu lintas", NewsOptions(sources=["news_tab"], by_day=False), datetime(2026, 10, 20), datetime(2026, 10, 20))
        assert time.time() - t < 30 and [p["url"] for p in res.posts] == ["https://a.example/1"]
        assert "1 outside the period dropped" in res.note
        # the article's own link, not the ad iframe
        res = c.search("x", NewsOptions(sources=["feed"], by_day=False, cap=1), datetime(2026, 10, 20), datetime(2026, 10, 20))
        assert res.posts[0]["url"] == "https://media.example/G4"
        # Stop while links are being resolved keeps the whole batch
        stop = threading.Event()
        c2 = _news_crawler(ctx, stop)
        real = c2._resolve
        calls = {"n": 0}

        def resolve(u, wait_s=10):
            calls["n"] += 1
            if calls["n"] == 3:
                stop.set()
                c2._check_stop()
            return real(u, wait_s=wait_s)
        c2._resolve = resolve
        with pytest.raises(Stopped) as e:
            c2.search("x", NewsOptions(sources=["feed"], by_day=False), datetime(2026, 10, 20), datetime(2026, 10, 20))
        urls = [p["url"] for p in e.value.posts]
        assert len(urls) == 5 and sum(u.startswith("https://media.example/") for u in urls) == 2
    finally:
        ctx.close()
        pw.stop()


# ---------------- smart reading of results / logins kept across versions ----------------

def test_textmatch_like_a_person():
    from crawler.textmatch import verdict
    assert verdict("Diskon token listrik #PromoAkhirTahun", "listrik", ["promo"], True) == "excluded"
    assert verdict("PROMOSI token listrik", "listrik", ["promo"], True) == "excluded"
    assert verdict("Jadwal #pemadamanlistrik hari ini", "pemadaman listrik", [], True) is None
    assert verdict("Resep sambal enak", "listrik", [], True) == "off_topic"
    assert verdict("Resep sambal enak", "listrik", [], False) is None
    assert verdict("Info #PLNPeduli", "pln", [], True) is None
    assert verdict("cairan pembersih", "air", [], True) == "off_topic"       # not a word that starts with "air"
    assert verdict("", "listrik", ["promo"], True) is None                    # nothing readable: keep


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_threads_search_drops_excluded_and_off_topic_posts(tmp_path):
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    from crawler.keywords import KeywordCrawler, SearchOptions
    from tests.test_crawler import ig_code
    d = datetime(2026, 10, 20, 5, tzinfo=UTC)
    posts = [("u1", "Pemadaman listrik di Bekasi malam ini"), ("u2", "Token listrik murah #promo hari ini"),
             ("u3", "Giveaway pulsa! listrik gratis"), ("u4", "Resep sambal matah"), ("u5", "Listrik padam lagi, PLN ke mana?")]
    body = "<html><body>" + "".join(
        f'<div data-pressable-container="true"><span>{u}</span><a href="/@{u}/post/{ig_code(d + timedelta(minutes=i))}">'
        f'<time datetime="{(d + timedelta(minutes=i)).isoformat()}">1j</time></a><div>{txt}</div></div>'
        for i, (u, txt) in enumerate(posts)) + "</body></html>"
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=body))
        c = KeywordCrawler(ctx, lambda m: None, threading.Event(), datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(100)
        res = c.search_threads("listrik", SearchOptions(exclude=["promo", "giveaway"]))
        assert sorted(p.author for p in res.posts) == ["u1", "u5"]
        assert "2 with excluded words dropped" in res.note and "1 not mentioning the keyword dropped" in res.note
        res = c.search_threads("listrik", SearchOptions(exclude=["promo"], must_mention=False))
        assert sorted(p.author for p in res.posts) == ["u1", "u3", "u4", "u5"]
        ctx.close()


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_tiktok_search_keeps_cards_without_the_keyword(tmp_path):
    """TikTok matches hashtags/speech the card does not show: such cards stay; excluded words still drop a card,
    and one excluded word on one card must not drop every card of the list. The page sees no webdriver flag."""
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    from crawler.keywords import KeywordCrawler, SearchOptions
    d = datetime(2026, 10, 20, 5, tzinfo=UTC)
    cards = [("u1", "Pemadaman listrik malam ini"), ("u2", "lagi viral 😂"), ("u3", "promo token murah"), ("u4", "")]
    body = '<html><body><div data-e2e="search_video-item-list">' + "".join(
        f'<div data-e2e="search_video-item"><a href="/@{u}/video/{tt_id(d + timedelta(minutes=i))}"><img alt=""></a>'
        f'<div>{txt}</div></div>' for i, (u, txt) in enumerate(cards)) + "</div></body></html>"
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=body))
        c = KeywordCrawler(ctx, lambda m: None, threading.Event(), datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(100)
        res = c.search_tiktok("listrik", SearchOptions(exclude=["promo"]))
        assert sorted(p.author for p in res.posts) == ["u1", "u2", "u4"]
        assert c.page.evaluate("navigator.webdriver") is False
        # natively so — no patched getter for sites to spot
        assert c.page.evaluate("Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver').get.toString()").find("native code") > 0
        ctx.close()


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_x_retry_and_profile_fallback(tmp_path):
    """X sometimes shows "Something went wrong · Retry" or an empty search: press Retry; if search stays empty,
    read the profile timeline instead."""
    from playwright.sync_api import sync_playwright

    from crawler.accounts import parse_line
    from crawler.browser import BrowserCrawler, launch_context
    from tests.test_crawler import x_id
    dates = [datetime(2026, 10, 25 - i, 5, tzinfo=UTC) for i in range(8)] + [datetime(2026, 9, 1 + i, tzinfo=UTC) for i in range(4)]
    arts = "".join(f'<article><a href="/abc/status/{x_id(d)}"><time>t</time></a></article>' for d in dates)
    retry = ('<html><body><div id="r"><span>Something went wrong.</span><div role="button" onclick="'
             "document.getElementById('r').outerHTML=document.getElementById('t').innerHTML\">Retry</div></div>"
             f'<template id="t">{arts}</template></body></html>')
    profile = f"<html><body>{arts}</body></html>"
    empty = "<html><body><div>No results for this search</div></body></html>"
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        mode = {"search": retry}
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html",
                                              body=mode["search"] if "/search" in r.request.url else profile))
        c = BrowserCrawler(ctx, lambda m: None, threading.Event(), datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC))
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(100)
        res = c.x(parse_line("https://x.com/abc"))
        assert len(res.posts) == 8, res.note
        mode["search"] = empty
        res = c.x(parse_line("https://x.com/abc"))
        assert len(res.posts) == 8 and "read from the profile" in res.note
        holder = {"p": profile}
        ctx.unroute("**/*")
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html",
                                              body=empty if "/search" in r.request.url else holder["p"]))
        holder["p"] = "<html><body>Sorry, you are rate limited. Please wait a few moments then try again.</body></html>"
        res = c.x(parse_line("https://x.com/abc"))
        assert not res.posts and "rate limited" in res.note
        ctx.close()


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_x_new_layout_without_article_tags(tmp_path):
    """X's 2026 layout: no <article>, no <time>; one tweet quotes another; the sidebar links a trending tweet."""
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    from crawler.keywords import KeywordCrawler, SearchOptions
    from tests.test_crawler import x_id
    d = datetime(2026, 11, 4, 3, tzinfo=UTC)
    tw = [("pltdkayumerah", "Tarif listrik Triwulan IV 2026 tetap, tidak naik. #PLN", None),
          ("warga1", "Kok tagihan listrik naik terus ya", ("detikcom", "Ramai keluhan tagihan listrik naik")),
          ("warga2", "Resep sambal matah", None)]
    cells = ""
    for i, (u, txt, q) in enumerate(tw):
        quote = (f'<div><a href="/{q[0]}/status/{x_id(d - timedelta(minutes=50))}"><span>{q[0]}</span></a>'
                 f'<div>{q[1]}</div></div>') if q else ""
        cells += (f'<div class="cell"><div><a href="/{u}"><span>{u}</span></a> '
                  f'<a href="/{u}/status/{x_id(d + timedelta(minutes=i))}"><span>1h</span></a></div>'
                  f'<div>{txt}</div>{quote}<a href="/{u}/status/{x_id(d + timedelta(minutes=i))}/analytics">38</a></div>')
    body = (f'<html><body><main><div class="timeline">{cells}</div></main>'
            f'<aside><a href="/news/status/{x_id(d)}">Trending</a></aside></body></html>')
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=body))
        c = KeywordCrawler(ctx, lambda m: None, threading.Event(), datetime(2026, 11, 3, 17, tzinfo=UTC),
                           datetime(2026, 11, 4, 16, 59, tzinfo=UTC))
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(100)
        res = c.search_x("listrik naik", SearchOptions())
        assert sorted(p.author for p in res.posts) == ["pltdkayumerah", "warga1"], res.note
        ctx.close()


def test_logins_and_lists_survive_a_new_version(tmp_path, monkeypatch):
    from crawler import paths
    old = tmp_path / "Downloads" / "post-link-collector"
    new = tmp_path / "Downloads" / "post-link-collector (1)"
    for d in (old, new):
        (d / "crawler").mkdir(parents=True)
        (d / "run.py").write_text("")
        (d / "data" / "lists").mkdir(parents=True)
    (old / "browser_profile" / "Default").mkdir(parents=True)
    (old / "browser_profile" / "Default" / "Cookies").write_text("cookie-db")
    (old / "browser_profile" / "SingletonLock").write_text("x")
    (old / "data" / "lists" / "Client A.csv").write_text("Group,Platform,Handle,URL\n")
    (new / "data" / "lists" / "Ditjen SDA.csv").write_text("Group,Platform,Handle,URL\n")
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "ROOT", str(new))
    monkeypatch.setattr(paths, "BUNDLED_LISTS", str(new / "data" / "lists"))
    monkeypatch.setattr(paths, "APP_DATA", str(home / "app"))
    monkeypatch.setattr(paths, "PROFILE_DIR", str(home / "app" / "browser_profile"))
    monkeypatch.setattr(paths, "LISTS_DIR", str(home / "docs" / "lists"))
    monkeypatch.setattr(paths, "OUTPUT_DIR", str(home / "docs" / "outputs"))
    monkeypatch.setattr(paths, "LOGINS_FILE", str(home / "app" / "logins.json"))
    logs = []
    paths.migrate(logs.append)
    assert (home / "app" / "browser_profile" / "Default" / "Cookies").read_text() == "cookie-db"
    assert not (home / "app" / "browser_profile" / "SingletonLock").exists()
    assert sorted(os.listdir(home / "docs" / "lists")) == ["Client A.csv", "Ditjen SDA.csv"]
    assert any("do not need to log in again" in m for m in logs)
    os.remove(home / "docs" / "lists" / "Client A.csv")                     # the user deletes a list ...
    paths.migrate(logs.append)
    assert sorted(os.listdir(home / "docs" / "lists")) == ["Ditjen SDA.csv"]  # ... and it does not come back


def test_login_status_from_cookies():
    from crawler.browser import login_status

    class Ctx:
        def cookies(self):
            return [{"name": "sessionid", "domain": ".instagram.com", "value": "x", "expires": time.time() + 999},
                    {"name": "auth_token", "domain": ".x.com", "value": "y", "expires": -1},
                    {"name": "sessionid", "domain": ".tiktok.com", "value": "z", "expires": time.time() - 10},   # expired
                    {"name": "c_user", "domain": ".facebook.com", "value": ""}]                                  # empty
    assert login_status(Ctx()) == {"instagram": True, "x": True, "tiktok": False, "threads": False, "facebook": False}



def test_logins_found_elsewhere_and_imported_over_an_unused_profile(tmp_path, monkeypatch):
    """The case from the field: the new version already created an (unsigned) profile, and the old copy sits on the
    Desktop instead of next to the new one."""
    from crawler import paths
    home = tmp_path / "home"
    new = home / "Downloads" / "post-link-collector-HEAD" / "post-link-collector"
    old = home / "Desktop" / "post-link-collector"
    for d in (new, old):
        (d / "crawler").mkdir(parents=True)
        (d / "run.py").write_text("")
    (old / "browser_profile" / "Default" / "Network").mkdir(parents=True)
    (old / "browser_profile" / "Default" / "Network" / "Cookies").write_text("signed-in")
    app = home / "app"
    (app / "browser_profile" / "Default").mkdir(parents=True)
    (app / "browser_profile" / "Default" / "Preferences").write_text("{}")       # used once, never signed in
    monkeypatch.setattr(paths, "ROOT", str(new))
    monkeypatch.setattr(paths, "BUNDLED_LISTS", str(new / "data" / "lists"))
    monkeypatch.setattr(paths, "APP_DATA", str(app))
    monkeypatch.setattr(paths, "PROFILE_DIR", str(app / "browser_profile"))
    monkeypatch.setattr(paths, "LOGINS_FILE", str(app / "logins.json"))
    monkeypatch.setattr(paths, "LISTS_DIR", str(home / "docs" / "lists"))
    monkeypatch.setattr(paths, "OUTPUT_DIR", str(home / "docs" / "outputs"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) + p[1:] if p.startswith("~") else p)
    found = paths.find_old_profiles()
    assert [f["app"] for f in found] == [str(old)] and found[0]["last_used"] > 0
    logs = []
    paths.migrate(logs.append)
    assert (app / "browser_profile" / "Default" / "Network" / "Cookies").read_text() == "signed-in"
    assert any(n.startswith("browser_profile_backup_") for n in os.listdir(app))   # the unused profile is kept aside
    # once this copy is signed in, start-up never replaces its logins
    (app / "logins.json").write_text('{"platforms": {"instagram": true}}')
    (old / "browser_profile" / "Default" / "Network" / "Cookies").write_text("other")
    os.utime(old / "browser_profile" / "Default" / "Network" / "Cookies", (time.time() + 100, time.time() + 100))
    paths.migrate(logs.append)
    assert (app / "browser_profile" / "Default" / "Network" / "Cookies").read_text() == "signed-in"
    # manual import (dashboard button): app folder or its browser_profile folder; bad paths explained
    assert paths.import_profile(str(old)) == ""
    assert (app / "browser_profile" / "Default" / "Network" / "Cookies").read_text() == "other"
    assert "No logins found" in paths.import_profile(str(tmp_path / "nothing"))


def test_documents_not_writable_falls_back(tmp_path, monkeypatch):
    """macOS can refuse Terminal access to Documents; the app must still start (it used to crash -> 'refused to connect')."""
    from crawler import paths
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.delenv("CRAWLER_HOME", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(home) + p[1:] if p.startswith("~") else p)
    real = paths._writable
    monkeypatch.setattr(paths, "_writable", lambda f: False if "Documents" in f else real(f))
    assert paths._documents() == str(home / "Post Link Collector")
    monkeypatch.setattr(paths, "_writable", lambda f: False)
    assert paths._documents() == paths._app_data()


def test_app_starts_and_reports_a_busy_port(tmp_path):
    import socket
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PORT=str(port), CRAWLER_HOME=str(tmp_path / "home"))
    app = subprocess.Popen([sys.executable, os.path.join(root, "run.py"), "--no-browser"], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as r:
                    assert json.loads(r.read())["running"] is False
                    break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        else:
            raise AssertionError("app did not start")
        second = subprocess.run([sys.executable, os.path.join(root, "run.py"), "--no-browser"], env=env, cwd=str(tmp_path),
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        assert "Could not start on port" in second.stdout and "already running" in second.stdout
    finally:
        app.terminate()
        app.wait(10)
    log = os.path.join(root, "startup_error.log")
    if os.path.exists(log):
        os.remove(log)


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_fast_details_x_from_list_and_tiktok_youtube_in_background(tmp_path):
    """X: counts come from the timeline, no tweet is opened. TikTok/YouTube: post pages are read in the background
    (no navigation per post); a post that can't be read that way is opened normally."""
    import json as _json
    import types

    from playwright.sync_api import sync_playwright

    from crawler.browser import BrowserCrawler, Post, launch_context
    from crawler.job import Job
    from tests.test_crawler import x_id
    d = datetime(2026, 11, 4, 3, tzinfo=UTC)
    x_page = ('<html><body><main><div>' + "".join(
        f'<div><a href="/u{i}/status/{x_id(d + timedelta(minutes=i))}">1h</a><div data-testid="tweetText">tweet {i}</div>'
        f'<div role="group" aria-label="{i} replies, {i * 2} reposts, {i * 10} likes, 1 bookmark, {i * 100} views"></div></div>'
        for i in range(1, 4)) + '</div></main></body></html>')

    def tt_page(i):
        data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {
            "desc": f"video {i} #listrik", "author": {"uniqueId": "abc"},
            "stats": {"diggCount": i * 10, "commentCount": i, "shareCount": i * 2, "playCount": i * 1000}}}}}}
        return f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{_json.dumps(data)}</script></html>'
    yt_page = ('<html><script>var ytInitialPlayerResponse = {"videoDetails":{"videoId":"v1","title":"Judul video",'
               '"viewCount":"12345","shortDescription":"isi"}};</script></html>')
    navigations = []

    def route(r):
        u = r.request.url
        if r.request.is_navigation_request():
            navigations.append(u)
        if "x.com" in u:
            body = x_page
        elif "/video/9" in u:
            body = "<html>blocked</html>"
        elif "tiktok.com/@abc/video/" in u:
            body = tt_page(int(u.rsplit("/", 1)[-1]))
        elif "youtube.com/watch" in u:
            body = yt_page
        elif "instagram.com/p/" in u:
            body = ('<html><head><meta property="og:description" content="1,234 likes, 56 comments - pu_sda on '
                    'October 5, 2026: &quot;Banjir surut&quot;"></head></html>')
        else:
            body = "<html><body>home</body></html>"
        r.fulfill(status=200, content_type="text/html", body=body)

    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "p"), headless=True)
        ctx.route("**/*", route)
        c = BrowserCrawler(ctx, lambda m: None, threading.Event(), datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, tzinfo=UTC))
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(50)
        job = types.SimpleNamespace(stop_flag=threading.Event(), current="", log=lambda m: None)
        fill = lambda plat, posts: Job._fill_details(job, c, plat, posts, "t")   # noqa: E731

        c.page.goto("https://x.com/search?q=x")
        rows = c._x_rows()
        from crawler.browser import _x_listed
        posts = [Post("https://x.com" + h, None, listed=_x_listed(g, v, cap)) for h, _t, g, v, cap in rows]
        navigations.clear()
        assert fill("x", posts) is False
        assert [p.details["likes"] for p in posts] == [10, 20, 30] and posts[2].details["views"] == 300
        assert posts[0].details["comments"] == 1 and posts[0].details["shares"] == 2 and posts[0].details["caption"] == "tweet 1"
        assert navigations == []                                    # no tweet was opened

        posts = [Post(f"https://www.tiktok.com/@abc/video/{i}", None) for i in (1, 2, 3, 9)]
        navigations.clear()
        assert fill("tiktok", posts) is False
        assert [p.details.get("views") for p in posts[:3]] == [1000, 2000, 3000]
        assert posts[1].details["likes"] == 20 and posts[1].details["caption"] == "video 2 #listrik"
        opened = [u for u in navigations if "/video/" in u]
        assert opened == ["https://www.tiktok.com/@abc/video/9"]    # only the one that couldn't be read in the background

        posts = [Post("https://www.youtube.com/watch?v=v1", None)]
        assert fill("youtube", posts) is False
        assert posts[0].details["views"] == 12345 and posts[0].details["caption"].startswith("Judul video")

        posts = [Post("https://www.instagram.com/p/ABC123/", None)]
        navigations.clear()
        assert fill("instagram", posts) is False
        assert posts[0].details["likes"] == 1234 and posts[0].details["caption"] == "Banjir surut"
        assert not any("/p/" in u for u in navigations)              # read in the background, not opened
        ctx.close()
