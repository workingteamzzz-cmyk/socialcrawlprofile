"""The server-side Google News decoder: second pass after a news search, and fixing the links in a file."""
import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler import gnews  # noqa: E402

NEW_ID = "CBMiAU_yqLtest01"


class FakeGoogle:
    """Answers like news.google.com: the signature page, then batchexecute (optionally "too many requests" first)."""
    def __init__(self, busy_first=0):
        self.calls, self.busy = [], busy_first

    def __call__(self, url, data=None, headers=None, timeout=20):
        import urllib.error
        self.calls.append(url)
        if "/rss/articles/" in url:
            return '<c-wiz><div data-n-a-sg="SIG" data-n-a-ts="1730000000"></div></c-wiz>'
        if "batchexecute" in url:
            if self.busy:
                self.busy -= 1
                raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
            gid = json.loads(json.loads(__import__("urllib.parse").parse.unquote(data.decode()[6:]))[0][0][1])[2]
            return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", json.dumps(["garturlres", f"https://media.example/{gid}", 1])]])
        raise AssertionError(url)


def dec(fake, **kw):
    d = gnews.Decoder(delay=0, sleep=lambda s: None, **kw)
    d._http = fake
    gnews._cache.clear()
    return d


def test_decoder_retries_when_google_is_busy_and_caches():
    fake = FakeGoogle(busy_first=2)
    d = dec(fake)
    url = f"https://news.google.com/rss/articles/{NEW_ID}?oc=5"
    assert d.decode(url) == (f"https://media.example/{NEW_ID}", "OK")
    n = len(fake.calls)
    assert d.decode(url)[1] == "OK" and len(fake.calls) == n                     # from the cache
    assert gnews.is_gnews(url) and gnews.is_gnews(f"https://news.google.com/articles/{NEW_ID}")
    assert not gnews.is_gnews("https://evil.example/?u=https://news.google.com/rss/articles/x")
    assert d.decode("https://www.kompas.com/a")[1] == "not a Google News link"
    stop = threading.Event()
    stop.set()
    assert dec(FakeGoogle(), stop=stop).decode(url)[1] == "stopped"


def test_second_pass_after_a_news_search():
    from crawler.job import Job
    j = Job()
    r1 = {"url": f"https://news.google.com/rss/articles/{NEW_ID}?oc=5", "keywords": ["a"], "keyword": "a"}
    r2 = {"url": f"https://media.example/{NEW_ID}", "keywords": ["b"], "keyword": "b"}          # same article, other kw
    r3 = {"url": "https://news.google.com/rss/articles/CBMiAU_yqLtest02", "keywords": ["a"], "keyword": "a"}
    r4 = {"url": "https://www.kompas.com/x", "keywords": ["a"], "keyword": "a"}
    from crawler.news import _key
    seen = {_key(r["url"]): r for r in (r1, r2, r3, r4)}
    b = {"links": [r1, r2, r3, r4], "version": 0}
    fake = FakeGoogle()
    orig = gnews.Decoder.__init__

    def init(self, *a, **k):
        orig(self, delay=0, sleep=lambda s: None, stop=k.get("stop"))
        self._http = fake
    gnews._cache.clear()
    gnews.Decoder.__init__ = init
    try:
        j._second_pass(seen, b)
    finally:
        gnews.Decoder.__init__ = orig
    urls = sorted(r["url"] for r in b["links"])
    assert urls == [f"https://media.example/{NEW_ID}", "https://media.example/CBMiAU_yqLtest02", "https://www.kompas.com/x"]
    assert r2["keywords"] == ["b", "a"] and r2["keyword"] == "b, a"                  # merged, not duplicated
    assert not any("news.google" in r["url"] for r in seen.values()) and len(seen) == 3
    assert any("second pass: 2/2 decoded" in m for m in j.logs)


def test_fix_links_in_files(tmp_path):
    from openpyxl import Workbook, load_workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["DATE", "TITLE", "URL"])
    ws.append(["09-Nov-2026", "A", f"https://news.google.com/rss/articles/{NEW_ID}?oc=5"])
    ws.append(["09-Nov-2026", "B", "https://www.kompas.com/b"])
    ws2 = wb.create_sheet("More")
    ws2["C5"] = "https://news.google.com/rss/articles/CBMiAU_yqLtest03"
    wb.save(tmp_path / "in.xlsx")
    ok, failed = gnews.fix_file(str(tmp_path / "in.xlsx"), str(tmp_path / "out.xlsx"), lambda m: None, decoder=dec(FakeGoogle()))
    assert (ok, failed) == (2, 0)
    out = load_workbook(tmp_path / "out.xlsx")
    assert out["Sheet"]["C2"].value == f"https://media.example/{NEW_ID}" and out["Sheet"]["C3"].value == "https://www.kompas.com/b"
    assert out["More"]["C5"].value == "https://media.example/CBMiAU_yqLtest03"
    log = [[c.value for c in r] for r in out["Decode Log"].iter_rows()]
    assert log[0] == ["Cell", "Google News Link", "Original Link", "Status"] and log[1][0] == "Sheet!C2" and log[1][3] == "OK"
    (tmp_path / "in.csv").write_text(f"a,{'https://news.google.com/rss/articles/' + NEW_ID}\nb,c\n", encoding="utf-8")
    assert gnews.fix_file(str(tmp_path / "in.csv"), str(tmp_path / "out.csv"), lambda m: None, decoder=dec(FakeGoogle())) == (1, 0)
    assert open(tmp_path / "out.csv", encoding="utf-8-sig").read().splitlines()[0] == f"a,https://media.example/{NEW_ID}"


def test_file_tool_through_the_job(tmp_path, monkeypatch):
    from openpyxl import Workbook, load_workbook

    from crawler import job as job_mod
    monkeypatch.setattr(job_mod, "OUTPUT_DIR", str(tmp_path))
    orig = gnews.Decoder.__init__

    def init(self, *a, **k):
        orig(self, delay=0, sleep=lambda s: None, stop=k.get("stop"))
        self._http = FakeGoogle()
    monkeypatch.setattr(gnews.Decoder, "__init__", init)
    gnews._cache.clear()
    wb = Workbook()
    wb.active.append(["URL", f"https://news.google.com/rss/articles/{NEW_ID}"])
    wb.save(tmp_path / "x.xlsx")
    j = job_mod.Job()
    assert j.start_decode("bad.pdf", b"x") == "Choose an .xlsx, .csv or .txt file."
    assert j.start_decode("20261108_Online_Manual_24.00.xlsx", (tmp_path / "x.xlsx").read_bytes()) is None
    j.thread.join(10)
    out = tmp_path / "20261108_Online_Manual_24.00_decoded.xlsx"
    assert out.exists() and load_workbook(out).active["B1"].value == f"https://media.example/{NEW_ID}"
    assert not os.listdir(tmp_path / "uploads")                                  # the upload is not kept


@pytest.mark.skipif(not os.environ.get("LIVE_GNEWS"), reason="live check against news.google.com (LIVE_GNEWS=1)")
def test_live_decoder():
    import re
    import urllib.request
    xml = urllib.request.urlopen("https://news.google.com/rss/search?q=listrik&hl=id&gl=ID&ceid=ID:id", timeout=20).read().decode()
    link = re.findall(r"<link>(https://news.google.com/rss/articles/[^<]+)</link>", xml)[0]
    real, status = gnews.Decoder(delay=0.5, retries=1).decode(link)
    assert status == "OK" and "news.google" not in real
