"""News Stream reader + client API: every article in the server's databases (the live stream and the reporting
database — News Search / Keyword / scheduled runs, imported files), filtered (query with AND / OR / NOT, a report
topic, dates, sentiment), with sentiment counts, and one article in full.

Used by the dashboard's News Stream page and — with an API key — by a client's own dashboard:
  GET /api/articles?q=pln AND (padam OR gangguan)&start=2026-11-20&end=2026-11-20&sentiment=negative&page=1
      &source=all (default: live stream + the reporting database) | stream | reports
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


SOURCES = ("all", "stream", "reports")


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


def _has_reports(c: sqlite3.Connection) -> bool:
    try:
        return bool(c.execute("SELECT 1 FROM s.sqlite_master WHERE name='rep'").fetchone())
    except sqlite3.Error:
        return False


def _topic_query(topic: str) -> str:
    from . import archive
    t = next((x for x in archive.topics() if x.get("id") == topic or x.get("name") == topic), None)
    if not t or not t.get("keywords"):
        raise ValueError("Unknown topic.")
    return " OR ".join(f"({fts_query(k)})" for k in t["keywords"])


def articles(q: str = "", topic: str = "", start: str = "", end: str = "", sentiment: str = "",
             sort: str = "newest", page: int = 1, size: int = 24, source: str = "all", scope: str = "",
             only_key: str = "") -> dict:
    """source: "all" = the live stream + the reporting database (News Search / Keyword / scheduled runs, imported
    files), a link in both shown once; "stream" = the live stream only; "reports" = the reporting database only."""
    source = source if source in SOURCES else "all"
    parts = []
    if (scope or "").strip():                      # a client key's fixed query: its own part, so q can only narrow it
        parts.append(f"({fts_query(scope)})")
    if (q or "").strip():
        parts.append(f"({fts_query(q)})")
    if topic:
        parts.append(f"({_topic_query(topic)})")
    expr = " AND ".join(parts)
    for v in (start, end):
        if v and not re.match(r"\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("Dates as YYYY-MM-DD.")
    want_rep = source in ("all", "reports")
    c = _conn(bool(expr) or want_rep and os.path.exists(os.path.join(_dir(), "search.db")))
    try:
        selects, args = [], []
        if source in ("all", "stream"):
            w, a = ["a.status='success'"], []
            for v, op in ((start, ">="), (end, "<=")):
                if v:
                    w.append(f"a.pub_date {op} ?")
                    a.append(v)
            if only_key:
                w.append("a.key = ?")
                a.append(only_key)
            join = ""
            if expr:
                join = "JOIN s.docs d ON d.key = a.key"
                w.append("d.docs MATCH ?")
                a.append(expr)
            selects.append("SELECT a.key, a.url, a.domain, a.title, a.pub_date, a.pub_time, a.image, a.summary, "
                           "COALESCE(NULLIF(a.sentiment,''),'neutral') sentiment, a.matched, a.fetched, 'stream' origin "
                           f"FROM articles a {join} WHERE {' AND '.join(w)}")
            args += a
        if want_rep and _has_reports(c):
            w, a = [], []
            for v, op in ((start, ">="), (end, "<=")):
                if v:
                    w.append(f"r.pub_date {op} ?")
                    a.append(v)
            if only_key:
                w.append("r.skey = ?")
                a.append(only_key)
            join = ""
            if expr:
                join = "JOIN s.rdocs rd ON rd.skey = r.skey"
                w.append("rd.rdocs MATCH ?")
                a.append(expr)
            if source == "all":                            # read by the stream too: shown once, from the stream
                w.append("NOT EXISTS (SELECT 1 FROM articles x WHERE x.key = r.skey AND x.status='success')")
            selects.append("SELECT r.skey key, r.url, r.domain, r.title, r.pub_date, r.pub_time, r.image, r.summary, "
                           "r.sentiment, r.keywords matched, 0 fetched, 'reports' origin "
                           f"FROM s.rep r {join}{' WHERE ' + ' AND '.join(w) if w else ''}")
            args += a
        counts: dict = {}
        rows: list = []
        size, page = max(1, min(int(size or 24), 100)), max(1, int(page or 1))
        if selects:
            u = " UNION ALL ".join(selects)
            try:
                counts = {r["s"]: r["n"] for r in c.execute(
                    f"SELECT sentiment s, COUNT(*) n FROM ({u}) GROUP BY s", args)}
                sw, sargs = "", list(args)
                if sentiment in SENTIMENTS:
                    sw = " WHERE sentiment = ?"
                    sargs.append(sentiment)
                order = "ASC" if sort == "oldest" else "DESC"
                rows = c.execute(f"SELECT * FROM ({u}){sw} ORDER BY pub_date {order}, pub_time {order}, "
                                 f"fetched {order} LIMIT ? OFFSET ?", (*sargs, size, (page - 1) * size)).fetchall()
            except sqlite3.OperationalError as e:
                raise ValueError(f"The query could not be read: {e}")
        tiers = {}
        doms = sorted({r["domain"] for r in rows if r["domain"]})
        if doms:
            tiers = {r["domain"]: r["tier"] for r in c.execute(
                f"SELECT domain, tier FROM sources WHERE kind='sitemap' AND domain IN ({','.join('?' * len(doms))})",
                doms)}
    finally:
        c.close()
    counts = {k: n for k, n in counts.items() if k in SENTIMENTS} | {
        "neutral": sum(n for k, n in counts.items() if k not in ("positive", "negative"))}
    total = sum(counts.values())
    shown = counts.get(sentiment, 0) if sentiment in SENTIMENTS else total
    return {"total": total, "matching": shown, "page": page, "size": size, "query": expr, "source": source,
            "sentiment": {k: {"n": counts.get(k, 0), "pct": round(counts.get(k, 0) * 100 / total, 1) if total else 0}
                          for k in SENTIMENTS},
            "rows": [{**dict(r), "tier": tiers.get(r["domain"], "")} for r in rows]}


def _report_article(key: str) -> dict:
    import json
    from . import archive
    from .people_clean import clean_names
    sp = os.path.join(_dir(), "search.db")
    if not os.path.exists(sp):
        raise ValueError("Article not found.")
    s = sqlite3.connect(f"file:{sp}?mode=ro", uri=True, timeout=30)
    s.row_factory = sqlite3.Row
    try:
        r = s.execute("SELECT * FROM rep WHERE skey=?", (key,)).fetchone()
    except sqlite3.Error:
        r = None
    finally:
        s.close()
    if not r:
        raise ValueError("Article not found.")
    with archive._LOCK:
        a = archive._db()
        try:
            got = a.execute("SELECT row FROM items WHERE kind='online' AND key=?", (r["akey"],)).fetchone()
        finally:
            a.close()
    row = json.loads(got["row"]) if got else {}
    html = str(row.get("CONTENT") or "")
    paras = [re.sub(r"<[^>]+>", "", p).strip() for p in re.split(r"</p>\s*|<br\s*/?>|\n", html)]
    return {"key": key, "url": r["url"], "domain": r["domain"], "title": r["title"], "date": r["pub_date"],
            "time": r["pub_time"], "image": r["image"], "author": row.get("AUTHOR") or "", "summary": r["summary"],
            "sentiment": r["sentiment"] or "neutral", "matched": r["keywords"], "origin": "reports",
            "influencer": clean_names(row.get("INFLUENCER") or ""), "persons": clean_names(row.get("PERSON") or ""),
            "paragraphs": [p for p in paras if p]}


def article(key: str) -> dict:
    from .people_clean import clean_names
    c = _conn(False)
    try:
        r = c.execute("SELECT * FROM articles WHERE key=? AND status='success'", (key,)).fetchone()
    finally:
        c.close()
    if not r:
        return _report_article(key)
    text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    keys = r.keys()
    return {"key": r["key"], "url": r["url"], "domain": r["domain"], "title": r["title"], "date": r["pub_date"],
            "time": r["pub_time"], "image": r["image"], "author": r["author"], "summary": r["summary"],
            "sentiment": (r["sentiment"] if "sentiment" in keys else "") or "neutral", "matched": r["matched"],
            "influencer": clean_names(r["influencer"]), "persons": clean_names(r["persons"]), "paragraphs": paras,
            "origin": "stream"}
