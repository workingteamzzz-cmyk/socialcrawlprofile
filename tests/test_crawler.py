"""Run: python -m pytest tests  (the browser test needs Playwright + Chromium installed)."""
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawler.accounts import parse_accounts  # noqa: E402
from crawler.browser import _fb_walk  # noqa: E402
from crawler.idtime import instagram_shortcode_time, tiktok_id_time, x_status_time  # noqa: E402

ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
UTC = timezone.utc


def ig_code(dt):
    mid = (int(dt.timestamp() * 1000) - 1314220021721) << 23
    s = ""
    while mid:
        s = ALPHA[mid % 64] + s
        mid //= 64
    return s.rjust(11, "A")


def x_id(dt):
    return (int(dt.timestamp() * 1000) - 1288834974657) << 22


def tt_id(dt):
    return int(dt.timestamp()) << 32


def test_id_decoders_roundtrip():
    dt = datetime(2026, 10, 5, 3, 4, 5, tzinfo=UTC)
    assert abs(instagram_shortcode_time(ig_code(dt)) - dt) < timedelta(seconds=1)
    assert abs(x_status_time(x_id(dt)) - dt) < timedelta(seconds=1)
    assert tiktok_id_time(tt_id(dt)) == dt
    # Known real tweet: 1212092628029698048 -> 2019-12-31
    assert x_status_time(1212092628029698048).date().isoformat() == "2019-12-31"


def test_parse_accounts_validation_and_dedupe():
    text = """
    https://www.instagram.com/pu_sda
    https://x.com/pupr_sda
    https://x.com/pupr_sda
    https://x.com/pu_sda_sumatera1
    https://x.com/setwan.dsdan
    https://www.tiktok.com/@pu_sda_pompenganjeneberang
    https://www.youtube.com/@pu_sda
    https://www.threads.com/@pu_sda
    https://www.facebook.com/pu.sumberdayaair
    https://www.facebook.com/pu_sda_citarum
    bukan url
    """
    accs, bad = parse_accounts(text)
    got = {(a.platform, a.handle): bool(a.problem) for a in accs}
    assert len(accs) == 9
    assert got[("x", "pupr_sda")] is False
    assert got[("x", "pu_sda_sumatera1")] is True        # 16 chars
    assert got[("x", "setwan.dsdan")] is True             # dot
    assert got[("tiktok", "pu_sda_pompenganjeneberang")] is True  # > 24
    assert got[("facebook", "pu.sumberdayaair")] is False
    assert got[("facebook", "pu_sda_citarum")] is True    # underscore
    assert got[("instagram", "pu_sda")] is False
    assert bad == ["bukan url"]


def test_fb_walk_skips_shared_story():
    data = {"data": {"node": {"comet_sections": {"metadata": [{"story": {
        "creation_time": 1790000000, "url": "https://www.facebook.com/pu.sumberdayaair/posts/pfbid123"}}]},
        "attached_story": {"creation_time": 1780000000, "url": "https://www.facebook.com/other/posts/pfbid999"}}}}
    assert list(_fb_walk(data)) == [("https://www.facebook.com/pu.sumberdayaair/posts/pfbid123", 1790000000)]


# ---------------- browser test against fake pages ----------------

def _fake_feed_html(links, batch=6):
    """Infinite-scroll page: shows `batch` links, adds more each time the user scrolls."""
    return f"""<html><body style="height:400vh"><div id="g"></div><script>
    const L = {json.dumps(links)}; let n = 0;
    function more() {{ const g = document.getElementById('g');
      for (let i = 0; i < {batch} && n < L.length; i++, n++) {{
        const a = document.createElement('a'); a.href = L[n][0]; a.textContent = 'post';
        if (L[n][1]) {{ const t = document.createElement('time'); t.dateTime = L[n][1]; a.appendChild(t); }}
        g.appendChild(a); g.appendChild(document.createElement('br')); }}
      document.body.style.height = (400 + n * 50) + 'vh'; }}
    more(); window.addEventListener('scroll', () => setTimeout(more, 100));
    </script></body></html>"""


def _browser_path():
    for p in (os.environ.get("CRAWLER_BROWSER_PATH"), "/opt/pw-browsers/chromium"):
        if p and os.path.exists(p):
            return p
    return None


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_browser_crawl_with_fake_pages(tmp_path):
    from playwright.sync_api import sync_playwright

    from crawler.accounts import Account
    from crawler.browser import BrowserCrawler, launch_context

    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    # newest first; one old pinned post at the top; 3 in range per platform; then older ones
    dates = [datetime(2025, 1, 1, tzinfo=UTC)] + \
            [datetime(2026, 11, 3, tzinfo=UTC)] + \
            [datetime(2026, 10, d, 8, tzinfo=UTC) for d in (28, 15, 2)] + \
            [datetime(2026, 9, 30 - i, tzinfo=UTC) for i in range(20)]

    pages = {
        "https://www.instagram.com/pu_sda/": _fake_feed_html(
            [[f"/pu_sda/{'reel' if i % 2 else 'p'}/{ig_code(d)}/", ""] for i, d in enumerate(dates)]),
        "https://www.tiktok.com/@pu_sda": _fake_feed_html(
            [[f"https://www.tiktok.com/@pu_sda/video/{tt_id(d)}", ""] for d in dates]
            + [["https://www.tiktok.com/@someoneelse/video/1", ""]]),
        "https://www.threads.com/@pu_sda": _fake_feed_html(
            [[f"/@pu_sda/post/{ig_code(d)}", d.isoformat()] for d in dates]),
        "x-search": _fake_feed_html(
            [[f"/pu_sda/status/{x_id(d)}", ""] for d in dates[2:5]] + [["/pu_sda/status/123/photo/1", ""]]),
    }

    def route(r):
        url = r.request.url
        key = "x-search" if url.startswith("https://x.com/search") else url
        r.fulfill(status=200, content_type="text/html", body=pages.get(key, "<html><body>nothing</body></html>"))

    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    stop = threading.Event()
    logs = []
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "profile"), headless=True)
        ctx.route("**/*", route)
        c = BrowserCrawler(ctx, logs.append, stop, start, end)
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(300)

        want = {d.date() for d in dates[2:5]}
        for plat, handle in [("instagram", "pu_sda"), ("tiktok", "pu_sda"), ("threads", "pu_sda"), ("x", "pu_sda")]:
            from crawler.accounts import profile_url
            res = getattr(c, plat)(Account(plat, handle, profile_url(plat, handle)))
            assert res.status == "ok", (plat, res)
            assert {p.time_utc.date() for p in res.posts} == want, (plat, [p.url for p in res.posts])
            assert len(res.posts) == 3
        ctx.close()


def test_label_and_facebook_id_urls():
    from crawler.accounts import parse_line
    a = parse_line("https://www.facebook.com/p/PUPRSDABrantas-100083072663557/ | BBWS Brantas")
    assert (a.platform, a.handle, a.label, a.problem) == ("facebook", "100083072663557", "BBWS Brantas", "")
    assert a.url == "https://www.facebook.com/profile.php?id=100083072663557"
    y = parse_line("https://www.youtube.com/channel/UCk0AIb2JDyz8VSPkaNq0OWQ | BBWS Brantas")
    assert y.url == "https://www.youtube.com/channel/UCk0AIb2JDyz8VSPkaNq0OWQ"


def test_bundled_list_is_clean():
    from crawler import store
    accs = store.load("Ditjen SDA")
    assert len(accs) > 250
    assert [a["url"] for a in accs if a["problem"]] == []
    assert all(a["group"] and a["status"] in ("Verified", "Likely", "Unverified") for a in accs)


def test_lists_add_remove_and_migration(tmp_path, monkeypatch):
    from crawler import store
    monkeypatch.setattr(store, "LISTS_DIR", str(tmp_path / "lists"))
    monkeypatch.setattr(store, "LEGACY_BASE", str(tmp_path / "accounts.csv"))
    monkeypatch.setattr(store, "LEGACY_CUSTOM", str(tmp_path / "custom_accounts.csv"))
    # an older install had data/accounts.csv (column "Unit") -> migrated into a list
    (tmp_path / "accounts.csv").write_text("Unit,Platform,Handle,URL,Status,Source,Notes\n"
                                           "BBWS X,Instagram,abc,https://www.instagram.com/abc/,Verified,,\n", encoding="utf-8")
    assert [l["id"] for l in store.lists()] == ["Ditjen SDA"]
    assert store.load("Ditjen SDA")[0]["group"] == "BBWS X"

    assert store.create_list("  Client A ")[0] == "Client A"
    assert store.create_list("client a")[1]                       # duplicate name
    assert store.create_list("../../etc")[0] == "etc"             # path characters stripped
    r = store.add("Client A", "https://x.com/akun_contoh\nhttps://www.facebook.com/salah_satu\n"
                              "https://www.tiktok.com/@brand_a\tBrand A\nhttps://x.com/akun_contoh", "Benchmark")
    assert r["added"] == ["https://x.com/akun_contoh", "https://www.tiktok.com/@brand_a"]
    assert len(r["errors"]) == 2                                   # invalid FB username + duplicate
    accs = store.load("Client A")
    assert {a["group"] for a in accs} == {"Benchmark", "Brand A"} and all(a["status"] == "Added" for a in accs)
    assert "https://x.com/akun_contoh | Benchmark" in store.lines_for("Client A", [accs[0]["id"]])
    assert store.remove("Client A", [accs[0]["id"]]) == 1 and len(store.load("Client A")) == 1
    assert store.load("../Ditjen SDA") == [] and store.add("nope", "https://x.com/a", "")["errors"]
    assert store.delete_list("Client A") and "Client A" not in [l["id"] for l in store.lists()]


# ---------------- keyword search ----------------

def test_query_builders():
    from crawler.keywords import SearchOptions, x_query, youtube_query
    o = SearchOptions.from_dict({"exact": True, "exclude": "giveaway, -promo", "x_lang": "id", "cap": "100"})
    assert o.exclude == ["giveaway", "promo"] and o.cap == 100
    assert x_query("pemadaman listrik", o, "2026-10-01", "2026-11-01") == \
        '"pemadaman listrik" -giveaway -promo lang:id -filter:replies -filter:nativeretweets since:2026-10-01 until:2026-11-01'
    s, e = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)
    assert youtube_query("listrik", SearchOptions(), s, e) == "listrik after:2026-09-30 before:2026-11-01"


def test_keyword_output_files(tmp_path):
    import openpyxl
    from crawler.keywords import SearchOptions
    from crawler.output import write_keyword_outputs
    rows = [{"keyword": "listrik, PLN", "keywords": ["listrik", "PLN"], "platform": "x", "author": "a",
             "url": "https://x.com/a/status/1", "date": "2026-10-02", "time": "10:00", "ts": 1}]
    st = [{"keyword": "listrik", "platform": "x", "status": "ok", "count": 1, "note": ""}]
    meta = {"tz_name": "WIB", "tz_hours": 7, "start": "2026-10-01", "end": "2026-10-31", "created": "now",
            "keywords": ["listrik", "PLN"], "options": SearchOptions()}
    write_keyword_outputs(str(tmp_path / "k.xlsx"), str(tmp_path / "k.csv"), rows, st, meta)
    wb = openpyxl.load_workbook(tmp_path / "k.xlsx")
    assert wb.sheetnames == ["Post Links", "Search Status", "Info"]
    assert list(wb["Post Links"].values)[1][1:4] == ("listrik, PLN", "X", "a")


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_keyword_search_with_fake_pages(tmp_path):
    from playwright.sync_api import sync_playwright

    from crawler.browser import launch_context
    from crawler.keywords import KeywordCrawler, SearchOptions

    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    inr = [datetime(2026, 10, d, 9, tzinfo=UTC) for d in (30, 20, 10, 5)]
    old = [datetime(2026, 9, 20, tzinfo=UTC), datetime(2025, 5, 1, tzinfo=UTC)]
    mixed = [inr[0], old[1], inr[1], inr[2], old[0], inr[3]]   # "top" feeds are not in date order

    def x_page():
        arts = "".join(f'<article><a href="/user{i}/status/{x_id(d)}"><time>t</time></a>'
                       f'<div><a href="/quoted/status/{x_id(old[1])}">quoted</a></div></article>'
                       for i, d in enumerate(inr))
        return f"<html><body>{arts}</body></html>"

    pages = {
        "x.com/search": x_page(),
        "threads.com/search": _fake_feed_html([[f"/@u{i}/post/{ig_code(d)}", d.isoformat()] for i, d in enumerate(inr + old)]),
        "instagram.com/explore/tags/listrik": _fake_feed_html([[f"/p/{ig_code(d)}/", ""] for d in mixed[:3]]),
        "instagram.com/explore/search/keyword": _fake_feed_html([[f"/someone/reel/{ig_code(d)}/", ""] for d in mixed]),
        "tiktok.com/search/video": _fake_feed_html([[f"https://www.tiktok.com/@v{i}/video/{tt_id(d)}", ""] for i, d in enumerate(mixed)]),
    }

    def route(r):
        body = next((v for k, v in pages.items() if k in r.request.url), "<html><body></body></html>")
        r.fulfill(status=200, content_type="text/html", body=body)

    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "profile"), headless=True)
        ctx.route("**/*", route)
        c = KeywordCrawler(ctx, lambda m: None, threading.Event(), start, end)
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(250)
        opt = SearchOptions()
        want = {d.date() for d in inr}

        res = c.search_x("listrik", opt)
        assert {p.time_utc.date() for p in res.posts} == want and len(res.posts) == 4   # quoted tweet ignored
        assert {p.author for p in res.posts} == {"user0", "user1", "user2", "user3"}
        res = c.search_threads("listrik", opt)
        assert {p.time_utc.date() for p in res.posts} == want and res.posts[0].author.startswith("u")
        res = c.search_instagram("listrik", opt)          # hashtag (3) + keyword search (6) merged, in-period only
        assert {p.time_utc.date() for p in res.posts} == want
        res = c.search_tiktok("listrik", opt)
        assert {p.time_utc.date() for p in res.posts} == want and len(res.posts) == 4
        res = c.search_tiktok("listrik", SearchOptions(cap=2))
        assert len(res.posts) == 2 and res.posts[0].time_utc == inr[0]
        ctx.close()


# ---------------- YouTube (browser) ----------------

def _yt_id(i):
    return f"vid{i:08d}"


def _yt_route(dates, extra_pages):
    """Fake YouTube: channel tabs / search pages from extra_pages, watch pages carry publishDate + owner."""
    import re as _re

    def route(r):
        url = r.request.url
        m = _re.search(r"/watch\?v=([\w-]{11})", url)
        if m and m.group(1) in dates:
            d = dates[m.group(1)]
            body = ('<html><script>var x = {"microformat":{"playerMicroformatRenderer":{"publishDate":"%s",'
                    '"ownerProfileUrl":"http://www.youtube.com/@owner_%s"}}};</script></html>' % (d.isoformat(), m.group(1)[-2:]))
        else:
            body = next((v for k, v in extra_pages.items() if k in url), "<html><body></body></html>")
        r.fulfill(status=200, content_type="text/html", body=body)
    return route


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_youtube_channel_and_search_in_browser(tmp_path):
    from playwright.sync_api import sync_playwright

    from crawler.accounts import parse_line
    from crawler.browser import BrowserCrawler, launch_context
    from crawler.keywords import KeywordCrawler, SearchOptions

    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
    vids = [datetime(2026, 11, 1, 2, tzinfo=UTC)] + [datetime(2026, 10, d, 8, tzinfo=UTC) for d in (28, 15, 2)] + \
           [datetime(2026, 9, 30 - i, tzinfo=UTC) for i in range(10)]
    dates = {_yt_id(i): d for i, d in enumerate(vids)}
    shorts = {_yt_id(100 + i): d for i, d in enumerate([datetime(2026, 10, 20, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC),
                                                        datetime(2026, 8, 1, tzinfo=UTC)])}
    dates.update(shorts)
    pages = {
        "/@chan/videos": _fake_feed_html([[f"/watch?v={v}", ""] for v in list(dates)[:len(vids)]]),
        "/@chan/shorts": _fake_feed_html([[f"/shorts/{v}", ""] for v in shorts]),
        "/@chan/streams": "<html><body></body></html>",
        "youtube.com/results": _fake_feed_html([[f"/watch?v={v}", ""] for v in list(dates)[:len(vids)]]),
    }
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    with sync_playwright() as pw:
        ctx = launch_context(pw, str(tmp_path / "profile"), headless=True)
        ctx.route("**/*", _yt_route(dates, pages))
        c = BrowserCrawler(ctx, lambda m: None, threading.Event(), start, end)
        c._pause = lambda lo=0, hi=0: c.page.wait_for_timeout(200)
        res = c.youtube(parse_line("https://www.youtube.com/@chan"))
        assert res.status == "ok", res
        got = sorted(p.url for p in res.posts)
        assert got == sorted([f"https://www.youtube.com/watch?v={_yt_id(i)}" for i in (1, 2, 3)] +
                             [f"https://www.youtube.com/shorts/{_yt_id(100)}"])
        # stopped scrolling soon after passing the start date: not every old video's page was fetched
        assert len(c._yt_cache) < len(dates)

        k = KeywordCrawler(ctx, lambda m: None, threading.Event(), start, end)
        k._pause = lambda lo=0, hi=0: k.page.wait_for_timeout(200)
        res = k.search_youtube("listrik", SearchOptions())
        assert {p.time_utc.date() for p in res.posts} == {d.date() for d in vids[1:4]}
        assert {p.author for p in res.posts} == {"owner_01", "owner_02", "owner_03"}
        assert "after:2026-09-30 before:2026-11-01" in res.note
        ctx.close()
