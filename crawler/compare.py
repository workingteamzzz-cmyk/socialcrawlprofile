"""Compare with another list: paste the links another monitoring service (a competitor) delivered — for each one,
whether this server has it, and if not, why not.

Reasons, from the server's own records:
  have            in the database (Live Stream or News Search / reports)
  waiting         found by the Live Stream, waiting in the queue to be read
  failed          the Live Stream tried and the site did not give the page (error shown: 403, timeout…)
  not_article     read, but filtered: not a news article (index / policy page) or outside the stream's days
  refused         robots.txt / excluded outlet
  not_found       the outlet is registered, but this article was never found (not in its sitemap / RSS yet)
  not_registered  the outlet is not in the media list
The missing links can then be read at once (Bulk links) and the missing outlets added to the list.
"""
from __future__ import annotations

import os
import sqlite3
from collections import Counter

REASONS = {"have": "In the database", "waiting": "Found — waiting to be read", "failed": "Could not be read",
           "not_article": "Filtered: not a news article / outside the days", "refused": "Refused (robots.txt / excluded)",
           "not_found": "Outlet registered — article not found", "not_registered": "Outlet not in the media list"}
MAX = 20000


def _dir() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream")


def _lookup(path: str, sql: str, keys: list[str]) -> dict:
    out: dict = {}
    if not keys or not os.path.exists(path):
        return out
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            try:
                for r in c.execute(sql.format(q=",".join("?" * len(part))), part):
                    out[r[0]] = dict(r)
            except sqlite3.Error:
                return out
    finally:
        c.close()
    return out


def compare(text) -> dict:
    from . import archive, media_db, news_multi
    from .stream import norm_key
    links = news_multi.clean_links(text)
    if not links:
        raise ValueError("Paste the other list's links (one per line, starting with http).")
    links = links[:MAX]
    keys = {u: norm_key(u) for u in links}
    ks = list(dict.fromkeys(keys.values()))
    stream_rows = _lookup(os.path.join(_dir(), "stream.db"),
                          "SELECT key, status, error, title, pub_date FROM articles WHERE key IN ({q})", ks)
    rep_rows = _lookup(os.path.join(_dir(), "search.db"), "SELECT skey, title, pub_date FROM rep WHERE skey IN ({q})",
                       ks)
    known = archive.known_urls(links)
    reg = media_db.registry()
    access = {}
    try:
        st = media_db.stats(wait=2)
        access = {d: media_db.access(x or None) for d, x in st.items()}
    except Exception:  # noqa: BLE001
        pass

    def outlet(u: str) -> str:
        d = media_db.clean_domain(u)
        parts = d.split(".")
        return next((".".join(parts[i:]) for i in range(len(parts) - 1) if ".".join(parts[i:]) in reg), "")

    rows = []
    for u in links:
        k = keys[u]
        s, r = stream_rows.get(k), rep_rows.get(k)
        dom = media_db.clean_domain(u)
        reg_dom = outlet(u)
        title, date, err = "", "", ""
        if (s and s["status"] == "success") or r or u in known:
            why = "have"
            title, date = (s or r or {}).get("title") or "", (s or r or {}).get("pub_date") or ""
        elif s and s["status"] == "new":
            why = "waiting"
        elif s and s["status"] == "failed":
            why, err = "failed", s.get("error") or ""
        elif s and s["status"] in ("filtered", "old"):
            why, title = "not_article", s.get("title") or ""
        elif s and s["status"] == "skipped":
            why = "refused"
        elif reg_dom:
            why = "not_found"
            a = access.get(reg_dom, "")
            if a in ("blocked", "unreachable", "robots", "paywall"):
                err = media_db.ACCESS_LABEL.get(a, a)
        else:
            why = "not_registered"
        sub = dom if (reg_dom and dom != reg_dom and dom not in reg and
                      dom.split(".")[0] not in ("m", "amp", "mobile", "www")) else ""
        rows.append({"url": u, "outlet": reg_dom or dom, "status": why, "reason": REASONS[why], "error": err,
                     "title": title, "date": date, "subsite": sub})
    cnt = Counter(r["status"] for r in rows)
    missing = [r for r in rows if r["status"] != "have"]
    return {"total": len(rows), "have": cnt.get("have", 0),
            "have_pct": round(cnt.get("have", 0) * 100 / len(rows), 1) if rows else 0,
            "counts": {k: {"n": cnt.get(k, 0), "label": v} for k, v in REASONS.items()},
            "missing_outlets": (Counter(r["outlet"] for r in missing if r["status"] == "not_registered")
                                + Counter(r["subsite"] for r in missing if r["status"] == "not_found" and r["subsite"])
                                ).most_common(80),
            "top_missing_outlets": Counter(r["outlet"] for r in missing).most_common(15),
            "fetchable": [r["url"] for r in missing if r["status"] in ("not_found", "not_registered", "failed",
                                                                         "waiting")],
            "rows": rows[:3000], "cut": len(rows) > 3000}


def why(url: str, budget_s: float = 45) -> dict:
    """One missing link, looked into now: does the site give the page, which of the outlet's sitemaps and RSS feeds
    list it (and how deep), how old it is — so the reason it was missed is known, not guessed."""
    import time
    import requests
    from urllib.parse import urlparse
    from . import media_db, stream
    from .medmas.discovery import sitemaps as sm
    from .medmas.extract import HEADERS
    t0 = time.time()
    host = media_db.clean_domain(url)
    if not host:
        raise ValueError("Not a link.")
    want = stream.norm_key(url)
    out = {"url": url, "host": host, "page": "", "found_in": "", "depth": 0, "date": "", "checked": 0, "notes": []}
    try:
        r = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        out["page"] = f"HTTP {r.status_code}" + (" (blocked for this server)" if r.status_code in (401, 403, 429) else "")
    except Exception as e:  # noqa: BLE001
        out["page"] = f"no answer ({type(e).__name__})"
    st = stream.get()
    src = (st.rq("SELECT * FROM sources WHERE domain=? AND kind='sitemap'", (host,)) or [None])[0]
    reg = media_db.registry()
    parts = host.split(".")
    parent = next((".".join(parts[i:]) for i in range(len(parts) - 1) if ".".join(parts[i:]) in reg), "")
    if src is None and parent and parent != host:
        out["notes"].append(f"{host} is a regional / section site of {parent} and has no source of its own yet — "
                            "adding it lets its own sitemaps be read.")
        src = (st.rq("SELECT * FROM sources WHERE domain=? AND kind='sitemap'", (parent,)) or [None])[0]
    if src is not None:
        out["tier"], out["last_check"] = st.tier_of(src), src["checked"]
    roots = []
    for h in dict.fromkeys([host] + ([parent] if parent and parent != host else [])):
        found, _alive = sm._sitemaps_from_robots(h)
        roots += found or [f"https://{h}{p}" for p in sm.COMMON_PATHS[:4]]
    queue, seen = list(dict.fromkeys(roots)), set()
    while queue and time.time() - t0 < budget_s and len(seen) < 60:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            kind, data = sm._parse_sitemap(sm._fetch(u, timeout=8))
        except Exception:  # noqa: BLE001
            continue
        if kind == "index":
            months = {time.strftime("%Y-%m")}
            ranked = sorted(data, key=lambda x: 0 if ("news" in x.lower() or any(m in x for m in months)) else 1)
            queue.extend(ranked[:50])
            continue
        for e in data:
            if stream.norm_key(e.get("url") or "") == want:
                out.update(found_in=u, depth=len(seen), date=e.get("date") or "")
                break
        if out["found_in"]:
            break
    out["checked"] = len(seen)
    if not out["found_in"]:
        for r in st.rq("SELECT url FROM sources WHERE domain IN (?, ?) AND kind='rss'", (host, parent or host)):
            if time.time() - t0 > budget_s:
                break
            try:
                body = requests.get(r["url"], headers=HEADERS, timeout=8).text
            except Exception:  # noqa: BLE001
                continue
            if urlparse(url).path and urlparse(url).path in body:
                out["found_in"] = r["url"] + " (RSS)"
                break
    cut = st.cutoff()
    if out["found_in"]:
        deep = out["depth"] > (stream.SITEMAP_READS_BUSY if (out.get("tier") in ("active", "active2")) else 6)
        out["verdict"] = ("Listed deep in the outlet's sitemaps (file #%d) — deeper than one visit reads; the stream "
                          "now reads up to %d files for busy outlets." % (out["depth"], stream.SITEMAP_READS_BUSY)
                          if deep else "Listed in the outlet's sitemaps / RSS — it should be found on the next visit"
                          + (" (the outlet is checked rarely: %s)" % out.get("tier") if out.get("tier") in
                             ("semi", "rare", "inactive", "dead") else "") + ".")
        if out["date"] and out["date"][:10] < cut:
            out["verdict"] += f" Published {out['date'][:10]}: older than the stream's days (from {cut}) — taken " \
                              "in the backlog lane."
    else:
        out["verdict"] = ("Not in the outlet's sitemaps or RSS feeds (checked %d) — the site publishes it only on its "
                          "pages; the front-page reading or News Search finds such articles." % out["checked"])
    if "blocked" in out["page"]:
        out["verdict"] += " The site refuses this server (" + out["page"] + ")."
    return out
