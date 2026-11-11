"""Google News RSS with recursive date-splitting + per-media harvesting.

Google News RSS caps at ~100 results per query. Strategies to break it:
1. The date range is split per month, then recursively halved (down to
   single days) whenever a query maxes out at ~100 results.
2. "Per-media harvest": `keyword site:domain` queries for every known
   media domain — each also date-split. Thousands of links total.
"""

from datetime import datetime, timedelta
from urllib.parse import quote, urlparse

from . import entry_datetime, fetch_feed
from ..media import MEDIA_DICT


def search(keyword, start=None, end=None, limit=100):
    """One Google News RSS query. Return (findings, source_domains)."""
    q = keyword
    if start:
        q += f" after:{start}"
    if end:
        # 'before:' is exclusive -> shift +1 day so the end date is included
        try:
            shifted = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
            q += f" before:{shifted.strftime('%Y-%m-%d')}"
        except ValueError:
            q += f" before:{end}"
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=id&gl=ID&ceid=ID:id"
    findings, domains = [], set()
    try:
        feed = fetch_feed(url)
        for e in feed.entries[:limit]:
            fdate, ftime = entry_datetime(e)
            item = {"url": e.link, "title": getattr(e, "title", ""),
                    "date": fdate, "time": ftime,
                    "source": "google_news", "domain": ""}
            try:
                d = urlparse(e.source.href).netloc.replace("www.", "").lower()
                if d:
                    item["domain"] = d
                    domains.add(d)
            except Exception:
                pass
            findings.append(item)
    except Exception:
        pass
    return findings, domains


def search_full(keyword, start, end, log=print, verbose=True):
    """Auto-split: per month, recursively halved when a query maxes at ~100.
    Return (findings, detected_domains)."""
    if not (start and end):
        results, doms = search(keyword, start, end)
        if verbose:
            log(f"      No date range: max ~100 results ({len(results)} received)")
        return results, doms

    d1 = datetime.strptime(start, "%Y-%m-%d").date()
    d2 = datetime.strptime(end, "%Y-%m-%d").date()
    results, doms = [], set()

    def recurse(a, b, level=0):
        r, d = search(keyword, a.isoformat(), b.isoformat())
        doms.update(d)
        if len(r) >= 95 and (b - a).days >= 1 and level < 9:
            mid = a + (b - a) / 2
            recurse(a, mid, level + 1)
            recurse(mid + timedelta(days=1), b, level + 1)
        else:
            results.extend(r)
            if verbose and r:
                log(f"      {a} to {b}: {len(r)} results")

    cursor = d1
    while cursor <= d2:
        month_end = (cursor.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        recurse(cursor, min(month_end, d2))
        cursor = month_end + timedelta(days=1)
    return results, doms


def per_media_harvest(keyword, start, end, domains, log=print, threads=6,
                      should_stop=lambda: False):
    """`site:domain` query per outlet — breaks the global ~100 cap.

    Runs in parallel chunks: the caller's priority order is preserved
    (productive outlets first — do NOT re-sort), progress + ETA is logged,
    and the Stop button is honored between chunks."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    ordered = list(dict.fromkeys(d for d in domains if d))
    log(f"   🔥 Per-media harvest: {len(ordered)} outlets, "
        f"{threads} parallel...")
    results, done, with_hits = [], 0, 0
    t0 = time.time()
    chunk = max(threads * 5, 30)

    def worker(domain):
        r, _ = search_full(f"{keyword} site:{domain}", start, end,
                           log=log, verbose=False)
        for item in r:
            item["domain"] = item["domain"] or domain
        return r

    for i in range(0, len(ordered), chunk):
        if should_stop():
            log("      ⛔ Stop requested — keeping the outlets harvested so far.")
            break
        part = ordered[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for domain, r in zip(part, ex.map(worker, part)):
                if r:
                    with_hits += 1
                    name = MEDIA_DICT.get(domain, domain)
                    log(f"      {name}: {len(r)} results")
                results.extend(r)
        done += len(part)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        left = (len(ordered) - done) / rate if rate > 0 else 0
        log(f"      per-media: {done}/{len(ordered)} outlets · "
            f"📰 {len(results)} links from {with_hits} outlets · "
            f"~{left / 60:.0f} min left")
    return results
