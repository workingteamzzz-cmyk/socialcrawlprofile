"""News Stream reader + client API: the articles the live stream read, filtered (query with AND / OR / NOT, a report
topic, dates, sentiment), with sentiment counts, and one article in full.

Used by the dashboard's News Stream page and — with an API key — by a client's own dashboard:
  GET /api/articles?q=pln AND (padam OR gangguan)&start=2026-11-20&end=2026-11-20&sentiment=negative&page=1
  GET /api/articles/one?key=...
"""
from __future__ import annotations

import os
import re
import sqlite3
import zlib

from .insights import fts_query

SENTIMENTS = ("positive", "negative", "neutral")


def _dir() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream")


def _conn(with_search: bool) -> sqlite3.Connection:
    p = os.path.join(_dir(), "stream.db")
    if not os.path.exists(p):
        raise ValueError("The live stream has no articles yet — start it under Live Stream.")
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    if with_search:
        sp = os.path.join(_dir(), "search.db")
        if not os.path.exists(sp):
            c.close()
            raise ValueError("The search index is still being built — try again in a minute.")
        c.execute("ATTACH DATABASE ? AS s", (f"file:{sp}?mode=ro",))
    return c


def _topic_query(topic: str) -> str:
    from . import archive
    t = next((x for x in archive.topics() if x.get("id") == topic or x.get("name") == topic), None)
    if not t or not t.get("keywords"):
        raise ValueError("Unknown topic.")
    return " OR ".join(f"({fts_query(k)})" for k in t["keywords"])


def articles(q: str = "", topic: str = "", start: str = "", end: str = "", sentiment: str = "",
             sort: str = "newest", page: int = 1, size: int = 24) -> dict:
    parts = []
    if (q or "").strip():
        parts.append(f"({fts_query(q)})")
    if topic:
        parts.append(f"({_topic_query(topic)})")
    expr = " AND ".join(parts)
    where, args = ["a.status='success'"], []
    for v, op in ((start, ">="), (end, "<=")):
        if v:
            if not re.match(r"\d{4}-\d{2}-\d{2}$", v):
                raise ValueError("Dates as YYYY-MM-DD.")
            where.append(f"a.pub_date {op} ?")
            args.append(v)
    join = ""
    if expr:
        join = "JOIN s.docs d ON d.key = a.key"
        where.append("d.docs MATCH ?")
        args.append(expr)
    c = _conn(bool(expr))
    try:
        w = " AND ".join(where)
        try:
            counts = {r["s"] or "neutral": r["n"] for r in c.execute(
                f"SELECT COALESCE(NULLIF(a.sentiment,''),'neutral') s, COUNT(*) n FROM articles a {join} WHERE {w} "
                "GROUP BY s", args)}
            sw, sargs = w, list(args)
            if sentiment in SENTIMENTS:
                sw += " AND COALESCE(NULLIF(a.sentiment,''),'neutral') = ?"
                sargs.append(sentiment)
            order = "ASC" if sort == "oldest" else "DESC"
            size, page = max(1, min(int(size or 24), 100)), max(1, int(page or 1))
            rows = c.execute(f"SELECT a.key, a.url, a.domain, a.title, a.pub_date, a.pub_time, a.image, a.summary, "
                             f"COALESCE(NULLIF(a.sentiment,''),'neutral') sentiment, a.matched, a.fetched "
                             f"FROM articles a {join} WHERE {sw} ORDER BY a.pub_date {order}, a.pub_time {order}, "
                             f"a.fetched {order} LIMIT ? OFFSET ?", (*sargs, size, (page - 1) * size)).fetchall()
            tiers = {}
            doms = sorted({r["domain"] for r in rows if r["domain"]})
            if doms:
                tiers = {r["domain"]: r["tier"] for r in c.execute(
                    f"SELECT domain, tier FROM sources WHERE kind='sitemap' AND domain IN ({','.join('?' * len(doms))})",
                    doms)}
        except sqlite3.OperationalError as e:
            raise ValueError(f"The query could not be read: {e}")
    finally:
        c.close()
    total = sum(counts.values())
    shown = counts.get(sentiment, 0) if sentiment in SENTIMENTS else total
    return {"total": total, "matching": shown, "page": page, "size": size, "query": expr,
            "sentiment": {k: {"n": counts.get(k, 0), "pct": round(counts.get(k, 0) * 100 / total, 1) if total else 0}
                          for k in SENTIMENTS},
            "rows": [{**dict(r), "tier": tiers.get(r["domain"], "")} for r in rows]}


def article(key: str) -> dict:
    from .people_clean import clean_names
    c = _conn(False)
    try:
        r = c.execute("SELECT * FROM articles WHERE key=? AND status='success'", (key,)).fetchone()
    finally:
        c.close()
    if not r:
        raise ValueError("Article not found.")
    text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    keys = r.keys()
    return {"key": r["key"], "url": r["url"], "domain": r["domain"], "title": r["title"], "date": r["pub_date"],
            "time": r["pub_time"], "image": r["image"], "author": r["author"], "summary": r["summary"],
            "sentiment": (r["sentiment"] if "sentiment" in keys else "") or "neutral", "matched": r["matched"],
            "influencer": clean_names(r["influencer"]), "persons": clean_names(r["persons"]), "paragraphs": paras}
