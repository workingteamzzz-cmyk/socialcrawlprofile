"""Home dashboard: what the archive holds for a period — trend, sentiment, top media / keywords, latest items.

Everything comes from the reporting database (archive.db), so it shows every run: manual, scheduled, imported.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date, timedelta

SENTIMENTS = ("negative", "neutral", "positive")
_CACHE: dict = {}             # (days, q, day) -> (time, result): the same view again within a minute is instant
CACHE_TTL = 60


def _sent(v: str) -> str:
    v = (v or "").strip().lower()
    return v if v in SENTIMENTS else "neutral"


def _latest_row(kind: str, r) -> dict:
    try:
        row = json.loads(r["row"] or "{}")
    except ValueError:
        row = {}
    if kind == "online":
        return {"title": row.get("TITLE") or "", "source": row.get("MEDIA") or r["source"] or "",
                "url": row.get("URL") or "", "day": r["day"], "time": str(row.get("TIME") or "")[:5],
                "sentiment": _sent(r["sentiment"])}
    return {"title": (row.get("Text") or "")[:220], "source": f"@{row.get('Username')}" if row.get("Username") else "",
            "platform": row.get("Platform") or r["source"] or "", "url": row.get("Url") or "", "day": r["day"],
            "time": str(row.get("Time") or "")[:5], "sentiment": _sent(r["sentiment"])}


def build(days: int = 14, q: str = "", today: date | None = None) -> dict:
    from . import archive
    days = max(1, min(int(days or 14), 90))
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    prev_start = start - timedelta(days=days)
    span = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    q = (q or "").strip().lower()[:100]
    out = {"days": days, "start": span[0], "end": span[-1], "q": q,
           "trend": [{"day": d, "online": 0, "medsos": 0} for d in span],
           "sentiment": {k: dict.fromkeys(SENTIMENTS, 0) for k in ("online", "medsos")},
           "top_media": [], "top_platforms": [], "top_keywords": [], "latest": {"online": [], "medsos": []},
           "kpi": {}}
    idx = {d: i for i, d in enumerate(span)}
    totals, prev = Counter(), Counter()
    neg, prev_neg = Counter(), Counter()
    media, plats, kws = Counter(), Counter(), Counter()
    prev_media = set()
    key = (days, q, today.isoformat())
    hit = _CACHE.get(key)
    ttl = CACHE_TTL if days <= 7 else 300 if days <= 30 else 900    # a long view reads far more rows: kept longer
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        # only reading: the archive's write lock is not held
        c = archive._db()
        try:
            where, args = "day >= ? AND day <= ?", [prev_start.isoformat(), span[-1]]
            if q:
                where += " AND (text LIKE ? OR keywords LIKE ?)"
                args += [f"%{q}%", f"%{q}%"]
            for r in c.execute(f"SELECT kind, day, source, sentiment, keywords FROM items WHERE {where}", args):
                k, d, s = r["kind"], r["day"], _sent(r["sentiment"])
                if d < span[0]:
                    prev[k] += 1
                    prev_neg[k] += s == "negative"
                    if k == "online" and r["source"]:
                        prev_media.add(r["source"].lower())
                    continue
                totals[k] += 1
                neg[k] += s == "negative"
                out["trend"][idx[d]][k] += 1
                out["sentiment"][k][s] += 1
                if k == "online" and r["source"]:
                    media[r["source"].lower()] += 1
                elif k == "medsos" and r["source"]:
                    plats[r["source"]] += 1
                for kw in (r["keywords"] or "").strip("|").split("|"):
                    if kw:
                        kws[kw] += 1
            for kind in ("online", "medsos"):
                rows = c.execute(f"SELECT * FROM items WHERE kind=? AND {where} ORDER BY day DESC, last_seen DESC "
                                 "LIMIT 12", [kind] + [span[0], span[-1]] + args[2:]).fetchall()
                out["latest"][kind] = [_latest_row(kind, r) for r in rows]
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    out["top_media"] = media.most_common(8)
    out["top_platforms"] = plats.most_common(7)
    out["top_keywords"] = kws.most_common(8)

    def pct(n, d):
        return round(n * 100 / d) if d else 0
    all_now, all_prev = totals["online"] + totals["medsos"], prev["online"] + prev["medsos"]
    out["kpi"] = {
        "articles": totals["online"], "articles_prev": prev["online"],
        "posts": totals["medsos"], "posts_prev": prev["medsos"],
        "negative_pct": pct(neg["online"] + neg["medsos"], all_now),
        "negative_pct_prev": pct(prev_neg["online"] + prev_neg["medsos"], all_prev),
        "media": len(media), "media_prev": len(prev_media),
    }
    out["generated"] = int(time.time())
    if len(_CACHE) > 50:
        _CACHE.clear()
    _CACHE[key] = (time.time(), out)
    return out
