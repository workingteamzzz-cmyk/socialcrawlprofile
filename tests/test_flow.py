"""End-to-end flow: the local HTTP server + background jobs + browser (fake platform pages) + Excel output."""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_crawler import _browser_path, _fake_feed_html, ig_code, tt_id, x_id  # noqa: E402

UTC = timezone.utc
IN_RANGE = [datetime(2026, 10, d, 9, tzinfo=UTC) for d in (25, 12, 3)]
OLD = [datetime(2026, 9, 28 - i, tzinfo=UTC) for i in range(8)]

PAGES = {
    "instagram.com/pu_sda/": _fake_feed_html([[f"/p/{ig_code(d)}/", ""] for d in IN_RANGE + OLD]),
    "tiktok.com/@pu_sda_citarum": _fake_feed_html([[f"https://www.tiktok.com/@pu_sda_citarum/video/{tt_id(d)}", ""] for d in IN_RANGE + OLD]),
    "instagram.com/akun_hilang/": "<html><body>Sorry, this page isn't available.</body></html>",
    "x.com/search": "<html><body>" + "".join(
        f'<article><a href="/warga{i}/status/{x_id(d)}"><time>t</time></a></article>' for i, d in enumerate(IN_RANGE)) + "</body></html>",
    "tiktok.com/search/video": _fake_feed_html([[f"https://www.tiktok.com/@v{i}/video/{tt_id(d)}", ""] for i, d in enumerate(IN_RANGE + OLD)]),
}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    if _browser_path() is None:
        pytest.skip("no local Chromium")
    os.environ["CRAWLER_BROWSER_PATH"] = _browser_path()
    from crawler import browser, job, server, store

    real_launch = browser.launch_context

    def fake_launch(pw, profile_dir, headless=False):
        ctx = real_launch(pw, profile_dir, headless=True)
        ctx.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html",
                                              body=next((v for k, v in PAGES.items() if k in r.request.url), "<html></html>")))
        return ctx

    monkeypatch.setattr(job, "launch_context", fake_launch)
    monkeypatch.setattr(job, "PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(job, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(store, "LISTS_DIR", str(tmp_path / "lists"))
    monkeypatch.setattr(store, "LEGACY_BASE", str(tmp_path / "none1.csv"))
    monkeypatch.setattr(store, "LEGACY_CUSTOM", str(tmp_path / "none2.csv"))
    monkeypatch.setattr(job.Job, "_pause_between", lambda self: None)
    monkeypatch.setattr(browser.BrowserCrawler, "_pause", lambda self, lo=0, hi=0: self.page.wait_for_timeout(200))
    fresh = job.Job()
    monkeypatch.setattr(job, "JOB", fresh)
    monkeypatch.setattr(server, "JOB", fresh)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Origin": base})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw

    def wait_done(timeout=120):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = call("/api/status")
            if not s["running"]:
                return s
            time.sleep(0.5)
        raise AssertionError("job did not finish")

    yield call, wait_done, base
    httpd.shutdown()


def test_profile_flow(app):
    call, wait_done, base = app
    assert b"Post Link Collector" in call("/")
    assert call("/api/lists")["lists"] == []
    assert call("/api/lists/create", {"name": "Client A"})["id"] == "Client A"
    r = call("/api/accounts/add", {"list": "Client A", "group": "Brand",
                                   "text": "https://www.instagram.com/pu_sda\nhttps://www.tiktok.com/@pu_sda_citarum | Rival\nhttps://x.com/not.valid"})
    assert len(r["added"]) == 2 and len(r["errors"]) == 1
    accs = call("/api/accounts?list=Client%20A")["accounts"]
    assert {a["group"] for a in accs} == {"Brand", "Rival"}
    assert call("/api/start", {"text": "x", "start": "2026-10-31", "end": "2026-10-01"})["error"]

    text = ("https://www.instagram.com/pu_sda | Ditjen SDA\nhttps://www.tiktok.com/@pu_sda_citarum | BBWS Citarum\n"
            "https://www.instagram.com/akun_hilang | Test\nhttps://x.com/setwan.dsdan | Test")
    r = call("/api/start", {"text": text, "start": "2026-10-01", "end": "2026-10-31", "tz": "WIB",
                            "platforms": ["instagram", "tiktok", "x"]})
    assert r["ok"], r
    assert call("/api/keywords/start", {"keywords": ["a"], "start": "2026-10-01", "end": "2026-10-31",
                                        "platforms": ["x"]})["error"].startswith("A job is already running")
    s = wait_done()
    assert s["error"] == "", s
    res = call("/api/results?mode=profiles")
    by = {(st["platform"], st["handle"]): st for st in res["statuses"]}
    assert by[("instagram", "pu_sda")]["count"] == 3 and by[("tiktok", "pu_sda_citarum")]["count"] == 3
    assert by[("instagram", "akun_hilang")]["status"] == "not_found"
    assert by[("x", "setwan.dsdan")]["status"] == "invalid"
    assert {l["group"] for l in res["links"]} == {"Ditjen SDA", "BBWS Citarum"}
    files = s["modes"]["profiles"]["files"]
    assert len(files) == 2
    data = call("/download/" + files[0])
    assert data[:2] == b"PK"   # xlsx
    out = call("/api/outputs")["files"]
    assert out and out[0]["name"] == files[0]
    with pytest.raises(urllib.error.HTTPError):
        call("/download/..%2F..%2Fetc%2Fpasswd")


def test_profile_flow_from_a_list(app):
    call, wait_done, _ = app
    call("/api/lists/create", {"name": "Banks"})
    call("/api/accounts/add", {"list": "Banks", "text": "https://www.instagram.com/pu_sda | Bank A\nhttps://www.tiktok.com/@pu_sda_citarum | Bank B"})
    ids = [a["id"] for a in call("/api/accounts?list=Banks")["accounts"]]
    r = call("/api/start", {"list": "Banks", "ids": ids, "start": "2026-10-01", "end": "2026-10-31", "tz": "WIB",
                            "platforms": ["instagram", "tiktok"]})
    assert r["ok"], r
    wait_done()
    res = call("/api/results?mode=profiles")
    assert sorted((st["group"], st["count"]) for st in res["statuses"]) == [("Bank A", 3), ("Bank B", 3)]


def test_keyword_flow(app):
    call, wait_done, _ = app
    assert call("/api/keywords/start", {"keywords": [], "start": "2026-10-01", "end": "2026-10-31",
                                        "platforms": ["x"]})["error"]
    r = call("/api/keywords/start", {"keywords": ["listrik", "Listrik", "PLN"], "start": "2026-10-01", "end": "2026-10-31",
                                     "tz": "WIB", "platforms": ["x", "tiktok"], "options": {"cap": 2, "x_lang": "id"}})
    assert r["ok"], r
    s = wait_done()
    assert s["error"] == "", s
    res = call("/api/results?mode=keywords")
    assert res["meta"]["keywords"] == ["listrik", "PLN"]          # case-insensitive de-duplication
    assert len(res["statuses"]) == 4
    assert all(st["status"] == "ok" and st["count"] == 2 for st in res["statuses"])   # cap applied
    # same pages for both keywords -> links merged, each listing both keywords
    assert all(set(l["keywords"]) == {"listrik", "PLN"} for l in res["links"])
    assert len(res["links"]) == 4
    assert s["modes"]["keywords"]["files"][0].startswith("keyword_links_")


def test_browser_closed_mid_run_stops_cleanly(app, monkeypatch):
    call, wait_done, _ = app
    from crawler import browser

    def boom(self, acc):
        raise RuntimeError("Target page, context or browser has been closed")
    monkeypatch.setattr(browser.BrowserCrawler, "instagram", boom)
    r = call("/api/start", {"text": "https://www.instagram.com/pu_sda\nhttps://www.instagram.com/pu_sda_citarum",
                            "start": "2026-10-01", "end": "2026-10-31", "platforms": ["instagram"]})
    assert r["ok"]
    s = wait_done()
    assert "browser window was closed" in s["error"]
    res = call("/api/results?mode=profiles")
    assert [st["status"] for st in res["statuses"]] == ["stopped"]
    assert s["modes"]["profiles"]["files"]   # partial results still saved
