"""Stress tests: big runs, big lists, many parallel dashboard requests, random input. Run with the rest:
python -m pytest tests   (about a minute on a laptop)."""
import json
import os
import random
import string
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_crawler import _browser_path  # noqa: E402

UTC = timezone.utc


def test_excel_20k_rows_with_details_is_fast(tmp_path):
    from crawler.output import write_outputs
    links = [{"order": i % 6, "platform_label": "Instagram", "handle": f"h{i % 700}", "label": f"Group {i % 40}",
              "url": f"https://www.instagram.com/p/C{i:09d}/", "time": datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=i),
              "caption": ("=== " if i % 50 == 0 else "") + "caption \x0b" + "x" * 400, "likes": i, "comments": i % 9,
              "shares": None, "views": i * 3} for i in range(20000)]
    statuses = [{"platform_label": "Instagram", "handle": f"h{i}", "label": "G", "url": "u", "status": "ok", "count": 30, "note": ""}
                for i in range(700)]
    meta = {"tz_hours": 7, "tz_name": "WIB", "start": "2026-10-01", "end": "2026-10-31", "created": "now", "details": True}
    t = time.time()
    write_outputs(str(tmp_path / "big.xlsx"), str(tmp_path / "big.csv"), links, statuses, meta)
    took = time.time() - t
    assert took < 30, f"20k-row save took {took:.1f}s"
    import openpyxl
    ws = openpyxl.load_workbook(tmp_path / "big.xlsx", read_only=True)["Post Links"]
    assert ws.max_row == 20001


def test_big_account_list(tmp_path, monkeypatch):
    from crawler import store
    monkeypatch.setattr(store, "LISTS_DIR", str(tmp_path / "lists"))
    monkeypatch.setattr(store, "LEGACY_BASE", str(tmp_path / "x.csv"))
    monkeypatch.setattr(store, "LEGACY_CUSTOM", str(tmp_path / "y.csv"))
    store.create_list("Big")
    plats = ["https://www.instagram.com/{}", "https://x.com/{}", "https://www.tiktok.com/@{}", "https://www.youtube.com/@{}",
             "https://www.threads.com/@{}", "https://www.facebook.com/{}"]
    text = "\n".join(plats[i % 6].format(f"acc{i}") + f" | Group {i % 300}" for i in range(5000))
    t = time.time()
    r = store.add("Big", text, "")
    assert len(r["added"]) == 5000 - sum(1 for i in range(5000) if i % 6 == 5 and len(f"acc{i}") < 5) and time.time() - t < 20
    t = time.time()
    accs = store.load("Big")
    assert len(accs) == len(r["added"]) and time.time() - t < 5
    ids = [a["id"] for a in accs[:2000]]
    assert len(store.lines_for("Big", ids).splitlines()) == 2000
    assert store.remove("Big", ids[:1000]) == 1000 and len(store.load("Big")) == len(accs) - 1000


def _rand_text(rng, n):
    pool = string.ascii_letters + string.digits + " .,:;/?&=%#@+-_'\"<>()[]{}|\\\n\t\x00\x0bäöü€😀ñ"
    return "".join(rng.choice(pool) for _ in range(rng.randint(0, n)))


def test_parsers_never_crash_on_random_input():
    from crawler.accounts import parse_line
    from crawler.browser import _parse_iso
    from crawler.details import parse_count
    from crawler.news import NewsOptions, parse_news_date
    from crawler.keywords import SearchOptions
    rng = random.Random(7)
    now = datetime(2026, 11, 2, tzinfo=UTC)
    prefixes = ["", "https://", "https://www.instagram.com/", "https://x.com/", "https://www.facebook.com/p/", "youtube.com/@",
                "https://www.tiktok.com/@", "12 ", "3 jam lalu ", "Okt ", "2026-10-"]
    for _ in range(5000):
        t = rng.choice(prefixes) + _rand_text(rng, 40)
        parse_line(t)
        parse_count(t)
        parse_news_date(t, now)
        _parse_iso(t)
    for _ in range(300):
        d = {k: rng.choice([None, "", 0, -5, "abc", "100", 10 ** 9, [], ["a", 3], _rand_text(rng, 20), True])
             for k in ("cap", "detail", "exclude", "sites", "region", "exact", "by_day", "x_lang", "ig_hashtag")}
        try:
            NewsOptions.from_dict(d)
            SearchOptions.from_dict(d)
        except (TypeError, ValueError):
            pass                     # rejected cleanly -> the job reports "Invalid search options"


class _FakeCrawler:
    """Stands in for the browser: every account instantly returns 15 dated posts."""
    def __init__(self, ctx, log, stop_flag, start, end):
        self.start, self.end, self.stop_flag = start, end, stop_flag

    def _in_range(self, posts):
        return [p for p in posts if p.time_utc and self.start <= p.time_utc <= self.end]

    def _make(self, acc):
        from crawler.browser import Post, Result
        return Result("ok", [Post(f"{acc.url.rstrip('/')}/p/{i}", self.start + timedelta(hours=i * 7)) for i in range(15)])

    def __getattr__(self, platform):
        return self._make


def test_big_run_with_parallel_dashboard_traffic(tmp_path, monkeypatch):
    from crawler import job, server
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(job, "launch_context", lambda pw, d, headless=False: type("Ctx", (), {"close": lambda self: None})())
    monkeypatch.setattr(job, "BrowserCrawler", _FakeCrawler)
    monkeypatch.setattr(job.Job, "_pause_between", lambda self: None)
    monkeypatch.setattr(job.Job, "AUTOSAVE_SECONDS", 2)
    fresh = job.Job()
    monkeypatch.setattr(server, "JOB", fresh)

    plats = ["https://www.instagram.com/{}", "https://x.com/{}", "https://www.tiktok.com/@{}"]
    text = "\n".join(plats[i % 3].format(f"acc{i}") + f" | Group {i % 100}" for i in range(1500))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    errors, calls = [], [0]

    def hammer():
        for _ in range(60):
            for path in ("/api/status?since=0", "/api/results?mode=profiles", "/api/outputs", "/api/lists"):
                try:
                    with urllib.request.urlopen(base + path, timeout=90) as r:
                        json.loads(r.read())
                        calls[0] += 1
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{path}: {e}")
            time.sleep(0.02)
    t = time.time()
    assert fresh.start_crawl(text, "2026-10-01", "2026-10-31", "WIB", ["instagram", "x", "tiktok"]) is None
    workers = [threading.Thread(target=hammer) for _ in range(20)]
    for w in workers:
        w.start()
    while fresh.running and time.time() - t < 480:
        time.sleep(0.2)
    for w in workers:
        w.join()
    took = time.time() - t
    httpd.shutdown()
    s = fresh.state()
    assert not fresh.running and s["error"] == "", s["error"]
    assert errors == [], errors[:5]
    assert calls[0] >= 4000
    res = fresh.results_view("profiles")
    assert len(res["links"]) == 1500 * 15 and len(res["statuses"]) == 1500
    assert took < 480, f"1,500 accounts took {took:.0f}s"
    files = s["modes"]["profiles"]["files"]
    import openpyxl
    ws = openpyxl.load_workbook(os.path.join(tmp_path / "out", files[0]), read_only=True)["Post Links"]
    assert ws.max_row == 1500 * 15 + 1


@pytest.mark.skipif(_browser_path() is None, reason="no local Chromium")
def test_dashboard_with_thousands_of_rows(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    from crawler import job, server, store
    monkeypatch.setattr(store, "LISTS_DIR", str(tmp_path / "lists"))
    monkeypatch.setattr(store, "LEGACY_BASE", str(tmp_path / "x.csv"))
    monkeypatch.setattr(store, "LEGACY_CUSTOM", str(tmp_path / "y.csv"))
    store.create_list("Big")
    store.add("Big", "\n".join(f"https://www.instagram.com/acc{i} | Group {i % 200}" for i in range(3000)), "")
    fresh = job.Job()
    b = fresh.data["profiles"]
    b["period"] = {"start": "2026-10-01", "end": "2026-10-31", "tz": "WIB"}
    b["meta"] = {"details": True}
    b["links"] = [{"platform": "instagram", "group": f"Group {i % 200}", "handle": f"acc{i}", "url": f"https://www.instagram.com/p/X{i}/",
                   "date": "2026-10-05", "time": "10:00", "caption": "<b>not html</b> " + "c" * 2000, "likes": i, "comments": 1,
                   "shares": None, "views": None} for i in range(5000)]
    b["statuses"] = [{"platform": "instagram", "group": "G", "handle": f"acc{i}", "url": "u", "status": "ok", "count": 2, "note": ""}
                     for i in range(3000)]
    b["version"] = 1
    monkeypatch.setattr(server, "JOB", fresh)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    with urllib.request.urlopen(base + "/api/results?mode=profiles") as r:
        payload = r.read()
    assert len(payload) < 5_000_000          # captions are previews in the dashboard feed
    errs = []
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=_browser_path())
        p = br.new_page()
        p.on("pageerror", lambda e: errs.append(str(e)))
        t = time.time()
        p.goto(base + "/")
        p.wait_for_function("document.getElementById('navAcc').textContent === '3000'", timeout=20000)
        p.click("#nav button[data-p=results]")
        p.wait_for_function("document.querySelectorAll('#resBody tr').length === 5000", timeout=20000)
        took = time.time() - t
        p.click("#nav button[data-p=accounts]")
        p.click("#btnExpand")
        p.wait_for_function("document.querySelectorAll('.acc').length === 3000", timeout=20000)
        p.fill("#q", "Group 7")
        p.wait_for_timeout(300)
        assert p.evaluate("document.querySelectorAll('.acc').length") > 0
        assert p.evaluate("document.querySelector('#resBody b')") is None   # captions are escaped, not rendered as HTML
        br.close()
    httpd.shutdown()
    assert errs == [] and took < 20, (errs, took)
