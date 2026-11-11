"""Auto-discover RSS feeds for the outlets in the media dictionary.

The curated RSS list covers only the biggest outlets; the user's dictionary
holds thousands more. Most Indonesian news sites run WordPress, so /feed/
usually works — and homepages advertise their feed via a <link> tag. Every
run probes a rotating slice of the dictionary; working feeds are remembered
in the database and read on every later run. Over successive runs the whole
dictionary gets feed coverage — the fastest route to brand-new articles.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests

from . import fetch_feed

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
COMMON_FEED_PATHS = ("/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml",
                     "/atom.xml", "/index.xml")
_LINK_RE = re.compile(
    r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
# Feeds for comments/categories are noise — skip them.
_SKIP_FEED = re.compile(r"comment|category|/tag/|kategori", re.I)


def _advertised_feeds(domain, timeout=8):
    """(feed URLs the homepage advertises, site_is_alive). A connection
    failure marks the site dead so the caller can skip path probing."""
    try:
        r = requests.get(f"https://{domain}", headers=HEADERS,
                         timeout=timeout)
        base = r.url
        found = []
        for tag in _LINK_RE.findall(r.text[:200_000]):
            m = _HREF_RE.search(tag)
            if m and not _SKIP_FEED.search(m.group(1)):
                found.append(urljoin(base, m.group(1)))
        return found, True
    except Exception:
        return [], False


def _feed_works(url):
    try:
        # short timeout + NO feedparser fallback: probing thousands of
        # possibly-dead sites must fail fast, never hang
        feed = fetch_feed(url, timeout=6, fallback=False)
        return bool(feed.entries)
    except Exception:
        return False


def discover(domain):
    """Return (feed_url_or_'', site_is_alive). Unreachable sites are
    skipped immediately — no path probing."""
    candidates, alive = _advertised_feeds(domain)
    if not alive:
        return "", False
    candidates += [f"https://{domain}{p}" for p in COMMON_FEED_PATHS]
    for url in dict.fromkeys(candidates):
        if _feed_works(url):
            return url, True
    return "", True


def discover_many(domains, log=print, threads=24, should_stop=lambda: False,
                  health=None):
    """Probe many domains in parallel. Returns {domain: feed_url_or_''};
    fills `health` with {domain: alive} when given. Chunked so the Stop
    button stays responsive."""
    out = {}
    items = list(dict.fromkeys(domains))
    chunk = max(threads * 3, 30)
    for i in range(0, len(items), chunk):
        if should_stop():
            break
        part = items[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for domain, (url, alive) in zip(part, ex.map(discover, part)):
                out[domain] = url
                if health is not None:
                    health[domain] = alive
        found = sum(1 for u in out.values() if u)
        log(f"      feed discovery: {len(out)}/{len(items)} outlets probed, "
            f"📡 {found} feeds found")
    return out
