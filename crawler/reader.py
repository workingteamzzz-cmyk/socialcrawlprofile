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
import time
import zlib
from datetime import datetime, timedelta, timezone

from .insights import fts_query

SENTIMENTS = ("positive", "negative", "neutral")


def _dir() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream")


SOURCES = ("all", "stream", "reports")


def _count_now(c, ck, u, args) -> dict:
    counts = {r["s"]: r["n"] for r in c.execute(f"SELECT sentiment s, COUNT(*) n FROM ({u}) GROUP BY s", args)}
    if len(_COUNTS) > 200:
        _COUNTS.clear()
    _COUNTS[ck] = (time.time(), dict(counts))
    _LAST[ck] = dict(counts)
    if len(_LAST) > 200:
        _LAST.clear()
    return counts


def _count_beside(ck, u, args, with_search) -> dict | None:
    import threading
    if ck not in _BUSY:
        _BUSY.add(ck)

        def run():
            try:
                c2 = _conn(with_search)
                try:
                    _count_now(c2, ck, u, args)
                finally:
                    c2.close()
            except Exception:  # noqa: BLE001
                pass
            finally:
                _BUSY.discard(ck)
        th = threading.Thread(target=run, daemon=True, name="reader-count")
        th.start()
        th.join(0.3)                                    # a small database is counted at once; a big one follows
    hit = _COUNTS.get(ck)
    if hit:
        return dict(hit[1])
    last = _LAST.get(ck)
    return dict(last) if last is not None else None


def forget_counts() -> None:
    """Data was deleted or restored: the totals are counted again at once (new articles may take up to a minute)."""
    _COUNTS.clear()
    _LAST.clear()


_IDX = [" INDEXED BY a_succ_pub"]      # emptied when the database has no such index yet
_LAST: dict = {}              # (filter) -> the last totals counted, however old (shown while counting again)
_BUSY: set = set()            # filters being counted in the background
_COUNTS: dict = {}            # (filter) -> (time, sentiment counts): the totals of a filter, kept for a minute
COUNT_TTL = 60


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


WIB = timezone(timedelta(hours=7))


def _moment(v: str, what: str) -> tuple[str, str]:
    """'2026-11-20' or '2026-11-20 06:00' (also 2026-11-20T06:00) -> (date, time or '')."""
    v = (v or "").strip().replace("T", " ")
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2})(?::\d{2})?)?", v, flags=re.ASCII)
    if not m:
        raise ValueError(f"{what}: a date as YYYY-MM-DD, or a date and time as YYYY-MM-DD HH:MM (WIB).")
    return m.group(1), m.group(2) or ""


def received_epoch(v) -> float:
    """received_since: epoch seconds, or a date / date-time (WIB unless it carries its own offset)."""
    v = str(v or "").strip()
    if not v:
        return 0.0
    if re.fullmatch(r"\d{9,11}(\.\d+)?", v):
        return float(v)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        raise ValueError("received_since: epoch seconds, or a date-time like 2026-11-20 06:00 (WIB) / "
                         "2026-11-20T06:00:00+07:00 — the server_time of the previous answer works as is.")
    return (dt if dt.tzinfo else dt.replace(tzinfo=WIB)).timestamp()


def _period_sql(p: str, start: str, end: str) -> tuple[list[str], list]:
    """Published-at limits; a time on the start / end day narrows to the minute (an article without a time
    counts as inside its day)."""
    w, a = [], []
    if start:
        d, t = _moment(start, "start")
        if t:
            w.append(f"({p}.pub_date > ? OR ({p}.pub_date = ? AND ({p}.pub_time >= ? OR {p}.pub_time = '')))")
            a += [d, d, t]
        else:
            w.append(f"{p}.pub_date >= ?")
            a.append(d)
    if end:
        d, t = _moment(end, "end")
        if t:
            w.append(f"({p}.pub_date < ? OR ({p}.pub_date = ? AND ({p}.pub_time <= ? OR {p}.pub_time = '')))")
            a += [d, d, t]
        else:
            w.append(f"{p}.pub_date <= ?")
            a.append(d)
    return w, a


def articles(q: str = "", topic: str = "", start: str = "", end: str = "", sentiment: str = "",
             sort: str = "newest", page: int = 1, size: int = 24, source: str = "all", scope: str = "",
             only_key: str = "", last_hours: float = 0, received_since="", quick: bool = False) -> dict:
    """start / end: published from / to (a date, or a date + time to the minute, WIB); last_hours: published in the
    last N hours (instead of start); received_since: what reached this server after that moment — fresh news and
    late arrivals alike, whatever their publication date (use the previous answer's server_time).
    sort: newest / oldest (by publication) or received (newest arrival first)."""
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
    try:
        last_hours = float(last_hours or 0)
    except (TypeError, ValueError):
        raise ValueError("last_hours: a number of hours, e.g. 6 or 48.")
    if last_hours > 0:
        start = (datetime.now(WIB) - timedelta(hours=min(last_hours, 24 * 3650))).strftime("%Y-%m-%d %H:%M")
    since = received_epoch(received_since)
    now = time.time()
    want_rep = source in ("all", "reports")
    c = _conn(bool(expr) or want_rep and os.path.exists(os.path.join(_dir(), "search.db")))
    try:
        selects, args = [], []
        # the newest (or oldest) stored articles straight from their own index — no sort of the whole collection.
        # Only for a plain list (no words, no single article); an older database without the index reads as before.
        use_idx = bool(_IDX[0]) and not expr and not only_key and not since and sort in ("newest", "oldest")
        if source in ("all", "stream"):
            w, a = ["a.status='success'"], []
            pw, pa = _period_sql("a", start, end)
            w += pw
            a += pa
            if since:
                w.append("a.fetched >= ?")
                a.append(since)
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
                           f"FROM articles a{_IDX[0] if use_idx else ''} {join} WHERE {' AND '.join(w)}")
            args += a
        if want_rep and _has_reports(c):
            w, a = _period_sql("r", start, end)
            if since:
                w.append("COALESCE(r.added, 0) >= ?")
                a.append(since)
            if only_key:
                w.append("r.skey = ?")
                a.append(only_key)
            join = ""
            if expr:
                join = "JOIN s.rdocs rd ON rd.skey = r.skey"
                w.append("rd.rdocs MATCH ?")
                a.append(expr)
            if source == "all":                            # read by the stream too: shown once, from the stream
                # marked once when indexed (insights.dup) — no lookup into the stream's database per row and view;
                # a row not marked yet is shown (rather twice than missing)
                w.append("COALESCE(r.dup, 0) = 0")
            selects.append("SELECT r.skey key, r.url, r.domain, r.title, r.pub_date, r.pub_time, r.image, r.summary, "
                           "r.sentiment, r.keywords matched, COALESCE(r.added, 0) fetched, 'reports' origin "
                           f"FROM s.rep r {join}{' WHERE ' + ' AND '.join(w) if w else ''}")
            args += a
        counts: dict = {}
        rows: list = []
        size, page = max(1, min(int(size or 24), 100)), max(1, min(int(page or 1), 100000))
        if selects:
            u = " UNION ALL ".join(selects)
            try:
                ck = (u, tuple(args))
                hit = _COUNTS.get(ck)
                with_search = bool(expr) or want_rep and os.path.exists(os.path.join(_dir(), "search.db"))
                if hit and time.time() - hit[0] < COUNT_TTL and not only_key:
                    counts = dict(hit[1])                   # the same filter a moment ago: its totals again
                elif quick and not only_key:
                    # the dashboard: the articles at once — the totals are counted beside it (a second at most is
                    # waited for); until then the last ones known, or "counting"
                    counts = _count_beside(ck, u, args, with_search)
                else:
                    counts = _count_now(c, ck, u, args)
                sw, sargs = "", list(args)
                if sentiment in SENTIMENTS:
                    sw = " WHERE sentiment = ?"
                    sargs.append(sentiment)
                order = "ASC" if sort == "oldest" else "DESC"
                by = "fetched DESC, pub_date DESC" if sort == "received" else \
                    f"pub_date {order}, pub_time {order}, fetched {order}"
                if not sw:
                    # only the first page*size of each part can be on this page: each part is cut there (its own
                    # index) before they are merged — the whole collection is never sorted for 24 rows
                    cut = page * size
                    u = " UNION ALL ".join(f"SELECT * FROM (SELECT * FROM ({q}) ORDER BY {by} LIMIT {int(cut)})"
                                           for q in selects)
                rows = c.execute(f"SELECT * FROM ({u}){sw} ORDER BY {by} LIMIT ? OFFSET ?",
                                 (*sargs, size, (page - 1) * size)).fetchall()
            except sqlite3.OperationalError as e:
                if "no such index" in str(e) and _IDX[0]:
                    _IDX[0] = ""                            # not built yet (the stream builds it once): read without
                    c.close()
                    return articles(q, topic, start, end, sentiment, sort, page, size, source, scope, only_key,
                                    last_hours, received_since, quick=quick)
                raise ValueError(f"The query could not be read: {e}")
        tiers = {}
        doms = sorted({r["domain"] for r in rows if r["domain"]})
        if doms:
            tiers = {r["domain"]: r["tier"] for r in c.execute(
                f"SELECT domain, tier FROM sources WHERE kind='sitemap' AND domain IN ({','.join('?' * len(doms))})",
                doms)}
    finally:
        c.close()
    if counts is None:                                  # still being counted (dashboard): the rows, no totals yet
        out_rows = [{**dict(r), "tier": tiers.get(r["domain"], "")} for r in rows]
        for r in out_rows:
            if r.get("pub_date") and not r.get("pub_time"):
                r["pub_time"] = "00:00"
            r["received_at"] = datetime.fromtimestamp(r["fetched"], WIB).isoformat(timespec="seconds") \
                if r.get("fetched") else ""
        return {"total": None, "matching": None, "page": page, "size": size, "query": expr, "source": source,
                "server_time": datetime.fromtimestamp(now, WIB).isoformat(timespec="milliseconds"), "pages": None,
                "sentiment": None, "counting": True, "more": len(out_rows) >= size, "rows": out_rows}
    counts = {k: n for k, n in counts.items() if k in SENTIMENTS} | {
        "neutral": sum(n for k, n in counts.items() if k not in ("positive", "negative"))}
    out_rows = [{**dict(r), "tier": tiers.get(r["domain"], "")} for r in rows]
    total = sum(counts.values())
    shown = counts.get(sentiment, 0) if sentiment in SENTIMENTS else total
    for r in out_rows:
        if r.get("pub_date") and not r.get("pub_time"):
            r["pub_time"] = "00:00"                    # no time known: 00:00 (the team's rule, as in the Excel)
        r["received_at"] = datetime.fromtimestamp(r["fetched"], WIB).isoformat(timespec="seconds") \
            if r.get("fetched") else ""
    return {"total": total, "matching": shown, "page": page, "size": size, "query": expr, "source": source,
            "server_time": datetime.fromtimestamp(now, WIB).isoformat(timespec="milliseconds"),
            "pages": (shown + size - 1) // size,
            "sentiment": {k: {"n": counts.get(k, 0), "pct": round(counts.get(k, 0) * 100 / total, 1) if total else 0}
                          for k in SENTIMENTS},
            "rows": out_rows}


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
            "time": r["pub_time"] or ("00:00" if r["pub_date"] else ""), "image": r["image"], "author": row.get("AUTHOR") or "", "summary": r["summary"],
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
            "time": r["pub_time"] or ("00:00" if r["pub_date"] else ""), "image": r["image"], "author": r["author"], "summary": r["summary"],
            "sentiment": (r["sentiment"] if "sentiment" in keys else "") or "neutral", "matched": r["matched"],
            "influencer": clean_names(r["influencer"]), "persons": clean_names(r["persons"]), "paragraphs": paras,
            "origin": "stream", "headline_only": not paras and (r["error"] or "").startswith("Headline only")}


POST_FIELDS = ("key", "platform", "username", "url", "date", "time", "text", "sentiment", "emotion", "likes", "replies",
               "shares", "views", "followers", "type", "received_at")


def posts(q: str = "", start: str = "", end: str = "", platform: str = "", sentiment: str = "", sort: str = "newest",
          page: int = 1, size: int = 24, scope: str = "", last_hours: float = 0, received_since="",
          topic: str = "") -> dict:
    """Social media posts and comments of the reporting database (Keyword Search, Collect, API scraper): the same
    filters as articles(), plus the platform (Instagram, TikTok, X, YouTube, Threads, Facebook)."""
    parts = [f"({fts_query(x)})" for x in (scope, q) if (x or "").strip()]
    if topic:
        parts.append(f"({_topic_query(topic)})")
    expr = " AND ".join(parts)
    try:
        last_hours = float(last_hours or 0)
    except (TypeError, ValueError):
        raise ValueError("last_hours: a number of hours, e.g. 6 or 48.")
    if last_hours > 0:
        start = (datetime.now(WIB) - timedelta(hours=min(last_hours, 24 * 3650))).strftime("%Y-%m-%d %H:%M")
    since = received_epoch(received_since)
    now = time.time()
    sp = os.path.join(_dir(), "search.db")
    empty = {"total": 0, "matching": 0, "page": 1, "pages": 0, "size": max(1, min(int(size or 24), 100)),
             "sentiment": {}, "rows": [],
             "server_time": datetime.fromtimestamp(now, WIB).isoformat(timespec="milliseconds")}
    if not os.path.exists(sp):
        return empty
    c = sqlite3.connect(f"file:{sp}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='med'").fetchone():
            return empty
        w, a = _period_sql("m", start, end)
        if since:
            w.append("COALESCE(m.added, 0) >= ?")
            a.append(since)
        if platform:
            w.append("lower(m.platform) LIKE ?")
            a.append(platform.strip().lower()[:20] + "%")
        join = ""
        if expr:
            join = "JOIN mdocs d ON d.mkey = m.mkey"
            w.append("d.mdocs MATCH ?")
            a.append(expr)
        where = (" WHERE " + " AND ".join(w)) if w else ""
        size, page = max(1, min(int(size or 24), 100)), max(1, min(int(page or 1), 100000))
        try:
            counts = {r["s"]: r["n"] for r in c.execute(
                f"SELECT m.sentiment s, COUNT(*) n FROM med m {join}{where} GROUP BY s", a)}
            sw, sa = where, list(a)
            if sentiment in SENTIMENTS:
                sw = (sw + " AND " if sw else " WHERE ") + "m.sentiment = ?"
                sa.append(sentiment)
            order = "ASC" if sort == "oldest" else "DESC"
            by = "m.added DESC" if sort == "received" else f"m.pub_date {order}, m.pub_time {order}, m.added {order}"
            rows = c.execute(f"SELECT m.* FROM med m {join}{sw} ORDER BY {by} LIMIT ? OFFSET ?",
                             (*sa, size, (page - 1) * size)).fetchall()
        except sqlite3.OperationalError as e:
            raise ValueError(f"The query could not be read: {e}")
    finally:
        c.close()
    total = sum(counts.values())
    shown = counts.get(sentiment, 0) if sentiment in SENTIMENTS else total
    out = [{"key": r["mkey"], "platform": r["platform"], "username": r["username"], "url": r["url"],
            "date": r["pub_date"], "time": r["pub_time"] or ("00:00" if r["pub_date"] else ""), "text": r["text"], "sentiment": r["sentiment"] or "neutral",
            "emotion": r["emotion"], "likes": r["likes"], "replies": r["replies"], "shares": r["shares"],
            "views": r["views"], "followers": r["followers"], "type": r["sub_type"] or "post",
            "received_at": datetime.fromtimestamp(r["added"], WIB).isoformat(timespec="seconds") if r["added"] else ""}
           for r in rows]
    return {"total": total, "matching": shown, "page": page, "pages": (shown + size - 1) // size, "size": size,
            "server_time": datetime.fromtimestamp(now, WIB).isoformat(timespec="milliseconds"),
            "sentiment": {k: {"n": counts.get(k, 0), "pct": round(counts.get(k, 0) * 100 / total, 1) if total else 0}
                          for k in SENTIMENTS},
            "rows": out}
