"""Safe mode: budgets, block pages, resting, schedules."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sf(tmp_path, monkeypatch):
    from crawler import paths, safety
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path))
    monkeypatch.setattr("crawler.captcha.notify", lambda *a, **k: None)
    return safety


def test_platforms_and_budget(sf):
    assert sf.platform_of("https://www.google.com/search?q=pln") == "google"
    assert sf.platform_of("https://news.google.com/rss/articles/x") == ""          # feed / article links: not counted
    assert sf.platform_of("https://m.facebook.com/a/posts/1") == "facebook"
    assert sf.platform_of("https://www.kompas.com/x") == ""
    url = "https://www.instagram.com/p/X/"
    sf.set_limits("careful")
    for _ in range(sf.PER_HOUR["instagram"]):
        sf.check(url)
        sf.spend(url)
    with pytest.raises(sf.Resting, match="this hour's safe-mode limit"):
        sf.check(url)
    sf.check(url, now=time.time() + 3700)                                           # next hour: fine again
    sf.set_enabled(False)
    sf.check(url)                                                                   # off: no limits
    assert sf.pause_factor() == 1.0
    sf.set_enabled(True)
    assert sf.pause_factor() > 1


def test_block_page_rests_the_site(sf):
    assert sf.detect("https://www.instagram.com/explore/", "Try Again Later. We restrict certain activity") \
        .startswith('"try again later')
    assert sf.detect("https://www.facebook.com/checkpoint/828281030927956/", "") == "sent to checkpoint"
    assert sf.detect("https://www.instagram.com/p/X/", "pln padam lagi") == ""
    until = sf.rest("instagram", '"try again later"')
    assert until > time.time() + 23 * 3600
    with pytest.raises(sf.Resting, match="resting until"):
        sf.check("https://www.instagram.com/p/Y/")
    st = {p["id"]: p for p in sf.status()["platforms"]}
    assert st["instagram"]["resting_until"] and st["x"]["resting_until"] == 0
    sf.release("instagram")
    sf.check("https://www.instagram.com/p/Y/")


def test_browser_goto_stops_on_a_block(sf):
    from crawler.browser import BrowserCrawler

    class Page:
        url = "https://www.instagram.com/explore/search/keyword/?q=pln"

        def goto(self, url, **k):
            self.url = url

        def wait_for_timeout(self, ms):
            pass

        def inner_text(self, sel, timeout=0):
            return "Try Again Later — We restrict certain activity to protect our community."
    c = BrowserCrawler.__new__(BrowserCrawler)
    c.page, c.log = Page(), (lambda m: None)
    import threading
    c.stop_flag = threading.Event()
    with pytest.raises(sf.Resting, match="showed a block"):
        c._goto("https://www.instagram.com/explore/search/keyword/?q=pln")
    with pytest.raises(sf.Resting, match="resting until"):          # nothing is opened while it rests
        c._goto("https://www.instagram.com/p/ABC/")


def test_schedules_spaced_out(sf):
    from crawler.schedule import ScheduleError, clean
    with pytest.raises(ScheduleError, match="every 6 hours"):
        clean({"kind": "keywords", "payload": {"keywords": ["pln"]}, "repeat": "hours", "hours": 1, "window": "today"})
    ok = clean({"kind": "news", "payload": {"keywords": ["pln"], "options": {"engine": "multi"}}, "repeat": "hours",
                "hours": 1, "window": "today"})
    assert ok["hours"] == 1                                                         # multi-source: hourly is fine
    with pytest.raises(ScheduleError):
        clean({"kind": "news", "payload": {"keywords": ["pln"], "options": {"engine": "google"}}, "repeat": "hours",
               "hours": 2, "window": "today"})


def test_levels_and_own_numbers(sf):
    st = {p["id"]: p for p in sf.status()["platforms"]}
    assert sf.status()["level"] == "normal" and st["instagram"]["per_day"] == 2 * sf.PER_DAY["instagram"]
    sf.set_limits("loose")
    assert {p["id"]: p for p in sf.status()["platforms"]}["instagram"]["per_hour"] == 4 * sf.PER_HOUR["instagram"]
    sf.set_limits(custom={"instagram": {"hour": 150, "day": 900}})
    st = {p["id"]: p for p in sf.status()["platforms"]}
    assert (st["instagram"]["per_hour"], st["instagram"]["per_day"], st["instagram"]["custom"]) == (150, 900, True)
    with pytest.raises(ValueError, match="above the day"):
        sf.set_limits(custom={"x": {"hour": 500, "day": 100}})
    sf.set_limits("careful")                                        # a level clears own numbers
    assert {p["id"]: p for p in sf.status()["platforms"]}["instagram"]["per_day"] == sf.PER_DAY["instagram"]
