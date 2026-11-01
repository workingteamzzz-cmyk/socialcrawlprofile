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
