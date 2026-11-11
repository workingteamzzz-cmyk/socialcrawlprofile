"""One-click media health check: probe every outlet in the dictionary,
remember which are unreachable, and let every discovery phase skip them.

A probe is one quick robots.txt request (5s, https then http) — the same
check the sitemap phase uses. Outlets that fail get ONE retry within the
census before being declared unreachable, so a momentary hiccup doesn't
condemn a healthy outlet. Skipped outlets are automatically re-checked after
14 days: dead sites do come back, and nothing is skipped forever.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from .discovery.sitemaps import _sitemaps_from_robots


def probe(domain):
    """True if the outlet's web server answers at all."""
    return _sitemaps_from_robots(domain)[1]


def census(domains, log=print, threads=32, should_stop=lambda: False):
    """Probe many outlets in parallel; failures get one retry.
    Returns {domain: alive_bool}. Chunked: progress stays visible and the
    Stop button works."""
    items = list(dict.fromkeys(d for d in domains if d))
    log(f"🩺 Media health check: probing {len(items)} outlets "
        f"({threads} parallel, ~5s per dead outlet)...")
    out, done = {}, 0
    t0 = time.time()
    chunk = max(threads * 4, 60)
    for i in range(0, len(items), chunk):
        if should_stop():
            log("   ⛔ Stop requested — keeping the results so far.")
            break
        part = items[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            first = dict(zip(part, ex.map(probe, part)))
            retry = [d for d, ok in first.items() if not ok]
            if retry:   # one second chance before calling an outlet dead
                for d, ok in zip(retry, ex.map(probe, retry)):
                    first[d] = ok
        out.update(first)
        done += len(part)
        dead = sum(1 for ok in out.values() if not ok)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        left = (len(items) - done) / rate if rate > 0 else 0
        log(f"   health: {done}/{len(items)} checked · "
            f"💀 {dead} unreachable · ~{left / 60:.0f} min left")
    return out


def run_census(db, domains, log=print, threads=32, should_stop=lambda: False):
    """Census + persist. Returns (alive_count, dead_count)."""
    results = census(domains, log, threads, should_stop)
    # weight=2: the census already double-probed failures, one census is
    # enough evidence for the skip threshold (fails >= 2)
    db.record_domain_health(results, weight=2)
    alive = sum(1 for ok in results.values() if ok)
    dead = len(results) - alive
    log(f"✅ Health check done: {alive} outlets reachable, "
        f"{dead} unreachable.")
    if dead:
        log(f"   💀 The {dead} unreachable outlets are now SKIPPED by the "
            f"sitemap, feed-discovery and per-media phases — runs get much "
            f"faster. They are automatically re-checked after 14 days, so "
            f"an outlet that comes back online rejoins by itself.")
    return alive, dead
