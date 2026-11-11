"""Crawl news sitemaps DIRECTLY from media sites — no Google middleman.

This is the most complete source: news sites must publish a news-sitemap
(all articles of the last 48h, with title & publication date) to be indexed
by Google News. Many sites also expose daily/monthly archive sitemaps.

Per-domain strategy:
1. Read robots.txt -> collect all Sitemap URLs.
2. Also try common locations: /sitemap.xml, /sitemap_news.xml, ...
3. Parse (including nested sitemapindex), take <url> + <news:title> +
   publication date, then filter by keyword (title/URL slug) and date range.
"""

import gzip
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import re

import requests
from lxml import etree

from ..filters import keyword_match, url_slug, within_range

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
COMMON_PATHS = ("/sitemap.xml", "/sitemap_news.xml", "/news-sitemap.xml",
                "/sitemap-news.xml", "/sitemap/news.xml", "/googlenewssitemap.xml")
MAX_SUB_SITEMAPS = 40   # per-domain sub-sitemap cap (indexes can hold thousands)
MAX_URLS_PER_DOMAIN = 20000


def _fetch(url, timeout=8):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body


def _sitemaps_from_robots(domain):
    """(sitemap URLs from robots.txt, site_is_alive). An HTTP error (404,
    403...) still means the server answered — alive. Connection failures on
    both schemes mean the site is dead: the caller skips it entirely
    instead of probing 6 more sitemap locations that will also time out."""
    for scheme in ("https", "http"):
        try:
            text = _fetch(f"{scheme}://{domain}/robots.txt",
                          timeout=5).decode("utf-8", "ignore")
        except requests.exceptions.HTTPError:
            return [], True
        except Exception:
            continue
        return [m.group(1).strip() for m in
                re.finditer(r"(?im)^sitemap:\s*(\S+)", text)], True
    return [], False


def _parse_sitemap(xml_body):
    """Return ('index', [sub_sitemap_urls]) or ('urlset', [dicts])."""
    try:
        root = etree.fromstring(xml_body)
    except etree.XMLSyntaxError:
        return "urlset", []
    tag = etree.QName(root).localname
    ns = {"sm": root.nsmap.get(None) or "http://www.sitemaps.org/schemas/sitemap/0.9",
          "news": "http://www.google.com/schemas/sitemap-news/0.9"}
    if tag == "sitemapindex":
        subs = [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", ns)
                if loc.text]
        return "index", subs
    entries = []
    for u in root.findall(".//sm:url", ns):
        loc = u.find("sm:loc", ns)
        if loc is None or not loc.text:
            continue
        title_el = u.find(".//news:title", ns)
        date_el = (u.find(".//news:publication_date", ns)
                   if u.find(".//news:publication_date", ns) is not None
                   else u.find("sm:lastmod", ns))
        entries.append({
            "url": loc.text.strip(),
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "date": (date_el.text or "").strip()[:10] if date_el is not None else "",
        })
    return "urlset", entries


def _months_in_range(start, end):
    """{'2026-08', ...} for every month the date range touches."""
    try:
        a = datetime.strptime(start, "%Y-%m-%d")
        b = datetime.strptime(end, "%Y-%m-%d")
    except (TypeError, ValueError):
        return frozenset()
    months, y, m = set(), a.year, a.month
    while (y, m) <= (b.year, b.month):
        months.add(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return frozenset(months)


_URL_MONTH_RE = re.compile(r"(20\d{2})[-/_.]?(0[1-9]|1[0-2])(?!\d)")


def _sitemap_priority(url, months=frozenset()):
    """'news'/'post'/'article' sitemaps first; image/video dropped.
    Date-stamped archive sitemaps (…2024-03…) OUTSIDE the requested range
    are skipped entirely — big speed win on sites with years of monthly
    archives — and ones matching the requested months jump the queue.
    Only a full year+month match may drop a sitemap (conservative:
    anything ambiguous is still read — anti missed clip)."""
    u = url.lower()
    if any(x in u for x in ("image", "video", "foto", "category", "tag")):
        return None
    score = 0
    m = _URL_MONTH_RE.search(u)
    if m and months:
        if f"{m.group(1)}-{m.group(2)}" in months:
            score -= 4          # exactly the month we want: read first
        else:
            return None         # dated archive outside the range: skip
    if "news" in u or "berita" in u:
        score -= 2
    if "post" in u or "artikel" in u or "article" in u:
        score -= 1
    return score


def harvest_domain(domain, keyword, start, end, log=print, max_seconds=45):
    """Collect keyword-matching articles from one domain's sitemaps.
    Returns (findings, site_is_alive). Dead sites are skipped after one
    quick check; a per-domain time budget keeps one huge/slow site from
    stalling the whole sweep."""
    months = _months_in_range(start, end)
    deadline = time.time() + max_seconds
    roots, alive = _sitemaps_from_robots(domain)
    if not alive:
        return [], False
    if not roots:
        roots = [f"https://{domain}{p}" for p in COMMON_PATHS]
    findings, queue, seen = [], list(dict.fromkeys(roots)), set()
    processed = 0
    while (queue and processed < MAX_SUB_SITEMAPS
           and len(findings) < MAX_URLS_PER_DOMAIN
           and time.time() < deadline):
        sm_url = queue.pop(0)
        if sm_url in seen:
            continue
        seen.add(sm_url)
        try:
            body = _fetch(sm_url)
        except Exception:
            continue
        processed += 1
        kind, data = _parse_sitemap(body)
        if kind == "index":
            ranked = [(u, _sitemap_priority(u, months)) for u in data]
            ranked = sorted((x for x in ranked if x[1] is not None), key=lambda x: x[1])
            queue.extend(u for u, _ in ranked[:MAX_SUB_SITEMAPS])
            continue
        for e in data:
            if not within_range(e["date"], start, end):
                continue
            if not keyword_match(keyword, e["title"], url_slug(e["url"])):
                continue
            e.update({"source": "sitemap", "domain": domain})
            findings.append(e)
    return findings, True


def harvest(keyword, start, end, domains, log=print, threads=16,
            should_stop=lambda: False, health=None):
    """Parallel sitemap harvest across many domains. Sitemap reading is
    light I/O, so parallelism can be much higher than article extraction —
    with thousands of outlets in the dictionary this is what keeps a run
    fast (dead/slow domains no longer serialize the whole sweep).

    Chunked, with steady progress + ETA lines — a quiet stretch of dead or
    slow outlets must never look like a frozen run — and stop-aware."""
    ordered = list(dict.fromkeys(domains))   # keep caller's priority order
    log(f"   🗺️ Sitemaps: checking {len(ordered)} domains ({threads} parallel)...")
    results, done = [], 0
    t0 = time.time()
    chunk = max(threads * 3, 30)
    for i in range(0, len(ordered), chunk):
        if should_stop():
            log("      ⛔ Stop requested — keeping the sitemap results so far.")
            break
        part = ordered[i:i + chunk]
        with ThreadPoolExecutor(max_workers=threads) as ex:
            for domain, (found, alive) in zip(part, ex.map(
                    lambda d: harvest_domain(d, keyword, start, end, log), part)):
                if health is not None:
                    health[domain] = alive
                if found:
                    log(f"      {domain}: {len(found)} matching articles")
                results.extend(found)
        done += len(part)
        elapsed = time.time() - t0
        rate = done / elapsed if elapsed > 0 else 0
        left = (len(ordered) - done) / rate if rate > 0 else 0
        log(f"      sitemaps: {done}/{len(ordered)} outlets checked · "
            f"📰 {len(results)} matches · ~{left / 60:.0f} min left")
    return results
