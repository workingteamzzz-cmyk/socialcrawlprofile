"""Discovery modules: each sub-module collects news links from one source.

All sources return a list of finding dicts:
    {"url": ..., "title": ..., "date": ..., "source": ..., "domain": ...}
"""

import calendar
import datetime as _dt

import feedparser
import requests


def entry_datetime(entry):
    """(date, time) in WIB from a feed entry's published/updated stamp.
    feedparser's *_parsed structs are UTC — WIB is UTC+7. Feeds carry the
    publish TIME too, which most article pages fail to expose; storing it
    fills the TIME column when page metadata comes up empty."""
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                wib = (_dt.datetime.fromtimestamp(calendar.timegm(t), _dt.timezone.utc)
                       + _dt.timedelta(hours=7))
                return wib.strftime("%Y-%m-%d"), wib.strftime("%H:%M")
            except Exception:
                pass
    return "", ""

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def fetch_feed(url, timeout=20, fallback=True):
    """Fetch an RSS/Atom feed with a real browser User-Agent, then parse.

    feedparser's own fetching uses a Python User-Agent that Google/Bing and
    many news sites throttle or block — a major cause of 'too few results'.
    Falls back to feedparser's fetching if the request fails.

    fallback=False for PROBING unknown/possibly-dead sites: feedparser's
    own fetch has NO timeout and can hang for minutes on a dead host —
    fine for trusted endpoints like Google/Bing, disastrous in bulk."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        if r.ok and r.content:
            return feedparser.parse(r.content)
    except Exception:
        pass
    if fallback:
        try:
            return feedparser.parse(url)
        except Exception:
            pass
    return feedparser.parse("")
