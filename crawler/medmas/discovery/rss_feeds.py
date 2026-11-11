"""Direct RSS feeds from media sites — the fastest route to BRAND-NEW news.

Ideal for watch mode: feeds are checked every interval, and new matching
articles are caught within minutes of publication.
"""

from concurrent.futures import ThreadPoolExecutor

from . import entry_datetime, fetch_feed
from ..filters import keyword_match, url_slug
from ..media import RSS_FEEDS


def _read_feed(pair):
    domain, feed_url = pair
    findings = []
    try:
        # no feedparser fallback: a feed that died since discovery must
        # fail fast, not hang the whole RSS phase
        feed = fetch_feed(feed_url, timeout=12, fallback=False)
        for e in feed.entries:
            url = getattr(e, "link", "")
            title = getattr(e, "title", "")
            if not url.startswith("http"):
                continue
            fdate, ftime = entry_datetime(e)
            findings.append({"url": url, "title": title,
                             "date": fdate, "time": ftime,
                             "source": "rss", "domain": domain})
    except Exception:
        pass
    return findings


def harvest(keyword, log=print, feeds=None, threads=8):
    """Read all feeds, filter by keyword (title + URL slug)."""
    pairs = [(d, u) for d, urls in (feeds or RSS_FEEDS).items() for u in urls]
    log(f"   📡 RSS: checking {len(pairs)} feeds...")
    results = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for findings in ex.map(_read_feed, pairs):
            for e in findings:
                if keyword_match(keyword, e["title"], url_slug(e["url"])):
                    results.append(e)
    if results:
        log(f"      RSS: {len(results)} articles match the keyword")
    return results
