"""GDELT Doc 2.0 API — a free global news index with HISTORICAL reach.

Sitemaps and RSS mostly carry the last 48 hours; Google throttles; Bing
is shallow. GDELT (gdeltproject.org, a research project) indexes world
news continuously and lets us query BY DATE RANGE for free, no API key —
ideal for backfilling a monitoring window. Indonesian coverage comes via
the sourcecountry:ID filter.
"""

import datetime as _dt

import requests

URL = "https://api.gdeltproject.org/api/v2/doc/doc"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}
MAX_SLICES = 7          # date range is split into at most this many queries
RECORDS_PER_CALL = 250  # GDELT's cap per request


def _slices(start, end):
    try:
        a = _dt.date.fromisoformat(start)
        b = _dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        today = _dt.date.today()
        return [(today - _dt.timedelta(days=3), today)]
    if b < a:
        a, b = b, a
    days = (b - a).days + 1
    step = max(1, -(-days // MAX_SLICES))     # ceil division
    out, cur = [], a
    while cur <= b:
        out.append((cur, min(cur + _dt.timedelta(days=step - 1), b)))
        cur += _dt.timedelta(days=step)
    return out


def _entry(article):
    url = str(article.get("url", ""))
    if not url.startswith("http"):
        return None
    seen = str(article.get("seendate", ""))       # 20260814T093000Z (UTC)
    date, time_ = "", ""
    try:
        dt = (_dt.datetime.strptime(seen, "%Y%m%dT%H%M%SZ")
              + _dt.timedelta(hours=7))           # WIB
        date, time_ = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
    except ValueError:
        pass
    domain = str(article.get("domain", "")).lower()
    domain = domain[4:] if domain.startswith("www.") else domain
    return {"url": url, "title": str(article.get("title", "")),
            "date": date, "time": time_, "source": "gdelt", "domain": domain}


def harvest(query, start, end, log=print):
    """Query GDELT for the keyword over the date range. The engine-syntax
    query ('"bukit asam" OR ptba -promo') passes through unchanged —
    GDELT understands quoted phrases, OR and -exclusions."""
    slices = _slices(start, end)
    log(f"   🌏 GDELT (historical index): {len(slices)} date slice(s)...")
    findings, seen = [], set()
    for a, b in slices:
        params = {
            "query": f"({query}) sourcecountry:ID",
            "mode": "ArtList", "format": "json",
            "maxrecords": str(RECORDS_PER_CALL), "sort": "DateDesc",
            "startdatetime": a.strftime("%Y%m%d") + "000000",
            "enddatetime": b.strftime("%Y%m%d") + "235959",
        }
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=20)
            articles = r.json().get("articles", []) if r.ok else []
        except Exception:
            continue
        for article in articles:
            e = _entry(article)
            if e and e["url"] not in seen:
                seen.add(e["url"])
                findings.append(e)
    if findings:
        log(f"      GDELT: {len(findings)} articles "
            f"({len({f['domain'] for f in findings})} outlets)")
    return findings
