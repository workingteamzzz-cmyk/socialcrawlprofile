"""Bing News RSS — complements Google News.

Bing indexes some outlets that Google News misses or picks up late, so a
second search engine lowers the missed-clip risk. Bing links are sometimes
apiclick redirects with a url= parameter — resolved here.
"""

from urllib.parse import parse_qs, quote, urlparse

from . import entry_datetime, fetch_feed


def _real_url(u: str) -> str:
    try:
        p = urlparse(u)
        if "bing.com" in p.netloc.lower():
            q = parse_qs(p.query).get("url")
            if q and q[0].startswith("http"):
                return q[0]
    except ValueError:
        pass
    return u


def harvest(keyword, log=print):
    url = f"https://www.bing.com/news/search?q={quote(keyword)}&format=rss&setmkt=id-ID"
    results = []
    try:
        feed = fetch_feed(url)
        for e in feed.entries:
            link = _real_url(getattr(e, "link", ""))
            if not link.startswith("http"):
                continue
            fdate, ftime = entry_datetime(e)
            results.append({"url": link, "title": getattr(e, "title", ""),
                            "date": fdate, "time": ftime,
                            "source": "bing_news", "domain": ""})
    except Exception as e:
        log(f"   ⚠️ Bing News error ({type(e).__name__})")
    if results:
        log(f"   🅱️ Bing News: {len(results)} results")
    return results
