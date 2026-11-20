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
        st = media_db.stats(wait=5)
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
        rows.append({"url": u, "outlet": reg_dom or dom, "status": why, "reason": REASONS[why], "error": err,
                     "title": title, "date": date})
    cnt = Counter(r["status"] for r in rows)
    missing = [r for r in rows if r["status"] != "have"]
    return {"total": len(rows), "have": cnt.get("have", 0),
            "have_pct": round(cnt.get("have", 0) * 100 / len(rows), 1) if rows else 0,
            "counts": {k: {"n": cnt.get(k, 0), "label": v} for k, v in REASONS.items()},
            "missing_outlets": Counter(r["outlet"] for r in missing if r["status"] == "not_registered").most_common(50),
            "top_missing_outlets": Counter(r["outlet"] for r in missing).most_common(15),
            "fetchable": [r["url"] for r in missing if r["status"] in ("not_found", "not_registered", "failed",
                                                                         "waiting")],
            "rows": rows[:3000], "cut": len(rows) > 3000}
