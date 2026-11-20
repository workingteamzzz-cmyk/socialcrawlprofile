"""Insights on top of the live news stream: instant search over every article, top stories (the same story told by
many outlets, grouped), and keyword spike alerts on Telegram.

- Search index: APP_DATA/stream/search.db (SQLite FTS5) holds title + summary of every article the stream read, kept
  as long as the stream keeps its articles. Filled in the background from stream.db (read-only), a few seconds behind.
  Query syntax as everywhere in the app: words, "exact phrase", OR, NOT / -word, ( ), prefix*.
- Top stories: articles of the last hours whose titles share most of their meaningful words are one story; stories are
  ranked by how many outlets carry them.
- Spike alerts: every 15 minutes, for every stream keyword / report topic: articles in the last hour vs the hourly
  average of the 24 hours before. ≥ factor × and ≥ minimum -> one Telegram message (at most once per 3 h per keyword).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
DEFAULTS = {"spike_alerts": True, "spike_factor": 3.0, "spike_min": 5, "spike_quiet_hours": 3}
_STATE = {"thread": None, "stop": threading.Event(), "last_indexed": 0.0, "indexed": 0, "error": "",
          "alerts": [], "stories": (0.0, 0, None)}
_LOCK = threading.RLock()
STOP_WORDS = set("""
yang dan di ke dari ini itu dengan untuk pada dalam tidak akan juga atau sudah ada karena oleh saat bisa lebih
para kepada agar telah masih hingga serta bagi seperti tersebut setelah antara terhadap namun jadi kata soal
usai jelang bakal kini baru begini begitu apa siapa mengapa kenapa bagaimana berapa kapan mana ini-itu
the a an of to in on for and is are with by at from as be this that
""".split())


def _dir() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream")


def settings() -> dict:
    try:
        with open(os.path.join(_dir(), "insights.json"), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        got = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (got or {}).items() if k in DEFAULTS})
    return out


def save_settings(data: dict) -> dict:
    cur = settings()
    for k, v in (data or {}).items():
        if k not in DEFAULTS:
            continue
        if k == "spike_alerts":
            cur[k] = bool(v)
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{k}: a number.")
        lo, hi = {"spike_factor": (1.5, 50), "spike_min": (1, 10000), "spike_quiet_hours": (1, 48)}[k]
        cur[k] = max(lo, min(hi, v if k == "spike_factor" else int(v)))
    os.makedirs(_dir(), exist_ok=True)
    with open(os.path.join(_dir(), "insights.json"), "w", encoding="utf-8") as f:
        json.dump(cur, f)
    return cur


# ---------- the search index ----------
def _search_db() -> sqlite3.Connection:
    os.makedirs(_dir(), exist_ok=True)
    c = sqlite3.connect(os.path.join(_dir(), "search.db"), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(key UNINDEXED, url UNINDEXED, domain UNINDEXED, "
              "pub_date UNINDEXED, pub_time UNINDEXED, matched UNINDEXED, fetched UNINDEXED, title, summary, "
              "tokenize='unicode61 remove_diacritics 2')")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    c.commit()
    return c


def _stream_ro() -> sqlite3.Connection | None:
    p = os.path.join(_dir(), "stream.db")
    if not os.path.exists(p):
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def index_new(limit: int = 5000) -> int:
    """Articles the stream read since the last call -> the search index. Returns how many were added."""
    src = _stream_ro()
    if src is None:
        return 0
    db = _search_db()
    try:
        r = db.execute("SELECT v FROM meta WHERE k='last_fetched'").fetchone()
        last = float(r["v"]) if r else 0.0
        rows = src.execute("SELECT key, url, domain, title, summary, pub_date, pub_time, matched, fetched FROM articles "
                           "WHERE status='success' AND fetched > ? ORDER BY fetched LIMIT ?", (last, limit)).fetchall()
        if not rows:
            return 0
        keys = [x["key"] for x in rows]
        for i in range(0, len(keys), 500):                  # a re-read article replaces its old entry
            part = keys[i:i + 500]
            db.execute(f"DELETE FROM docs WHERE key IN ({','.join('?' * len(part))})", part)
        db.executemany("INSERT INTO docs(key, url, domain, pub_date, pub_time, matched, fetched, title, summary) "
                       "VALUES (?,?,?,?,?,?,?,?,?)",
                       [(x["key"], x["url"], x["domain"], x["pub_date"], x["pub_time"], x["matched"], x["fetched"],
                         x["title"] or "", x["summary"] or "") for x in rows])
        db.execute("INSERT INTO meta(k, v) VALUES ('last_fetched', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                   (str(rows[-1]["fetched"]),))
        db.commit()
        with _LOCK:
            _STATE["indexed"] += len(rows)
        return len(rows)
    finally:
        db.close()
        src.close()


def purge_index(days: int) -> None:
    db = _search_db()
    try:
        db.execute("DELETE FROM docs WHERE CAST(fetched AS REAL) < ?", (time.time() - days * 86400,))
        db.commit()
    finally:
        db.close()


def fts_query(q: str) -> str:
    """The app's query syntax -> FTS5: words, "phrases", OR, NOT / -word, ( ), prefix*. Raises ValueError."""
    q = (q or "").strip()
    if not q:
        raise ValueError("Type what to search for.")
    toks = re.findall(r'"[^"]*"|\(|\)|-?[\w*]+', q, flags=re.U)
    out, pos, neg = [], [], []
    depth = 0
    for t in toks:
        up = t.upper()
        if t == "(":
            depth += 1
            out.append("(")
        elif t == ")":
            if depth:
                depth -= 1
                out.append(")")
        elif up in ("AND", "OR"):
            out.append(up)
        elif up == "NOT":
            out.append("NOT")
        elif t.startswith("-") and len(t) > 1:
            neg.append(_term(t[1:]))
        elif t.startswith('"'):
            phrase = re.sub(r'[^\w\s]', " ", t[1:-1], flags=re.U).strip()
            if phrase:
                out.append(f'"{phrase}"')
                pos.append(phrase)
        else:
            term = _term(t)
            if term:
                out.append(term)
                pos.append(term)
    out += [")"] * depth
    s = " ".join(out)
    s = re.sub(r"\bAND\s+NOT\b", "NOT", s)
    s = re.sub(r"^\s*(AND|OR)\s+|\s+(AND|OR|NOT)\s*$", "", s)
    if re.match(r"^\s*NOT\b", s) or not pos:
        raise ValueError("Start with a word or a phrase (NOT / -word only after it).")
    for n in neg:
        s = f"({s}) NOT {n}"
    return s


def _term(t: str) -> str:
    star = t.endswith("*")
    t = re.sub(r"[^\w]", "", t, flags=re.U)
    return (t + ("*" if star else "")) if t else ""


def search(q: str, days: int = 7, page: int = 1, size: int = 50, domain: str = "") -> dict:
    expr = fts_query(q)
    db = _search_db()
    try:
        since = time.time() - max(1, min(int(days or 7), 90)) * 86400
        where = "docs MATCH ? AND CAST(fetched AS REAL) >= ?"
        args: list = [expr, since]
        if domain:
            where += " AND domain = ?"
            args.append(domain)
        try:
            total = db.execute(f"SELECT COUNT(*) n FROM docs WHERE {where}", args).fetchone()["n"]
            size, page = max(10, min(int(size or 50), 200)), max(1, int(page or 1))
            rows = db.execute(f"SELECT url, domain, pub_date, pub_time, matched, "
                              f"highlight(docs, 7, '<mark>', '</mark>') title, "
                              f"snippet(docs, 8, '<mark>', '</mark>', '…', 24) snip FROM docs WHERE {where} "
                              f"ORDER BY CAST(fetched AS REAL) DESC LIMIT ? OFFSET ?",
                              (*args, size, (page - 1) * size)).fetchall()
            per_day = {r["d"]: r["n"] for r in db.execute(
                f"SELECT pub_date d, COUNT(*) n FROM docs WHERE {where} GROUP BY pub_date", args)}
            media = db.execute(f"SELECT domain, COUNT(*) n FROM docs WHERE {where} GROUP BY domain ORDER BY n DESC "
                               "LIMIT 10", args).fetchall()
        except sqlite3.OperationalError as e:
            raise ValueError(f"The search could not be read: {e}")
        return {"q": q, "query": expr, "total": total, "page": page, "size": size,
                "rows": [dict(r) for r in rows], "per_day": per_day, "media": [[r["domain"], r["n"]] for r in media]}
    finally:
        db.close()


def export(q: str, days: int, out_dir: str) -> dict:
    """Every article matching the search -> one Online Excel (full articles from the stream database)."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.styles.colors import Color
    from . import templates, online_clean as oc
    expr = fts_query(q)
    db, src = _search_db(), _stream_ro()
    if src is None:
        raise ValueError("The stream has no articles yet.")
    try:
        since = time.time() - max(1, min(int(days or 7), 90)) * 86400
        keys = [r["key"] for r in db.execute("SELECT key FROM docs WHERE docs MATCH ? AND CAST(fetched AS REAL) >= ? "
                                             "ORDER BY CAST(fetched AS REAL) DESC LIMIT 200000", (expr, since))]
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Sheet1")
        for i, w in enumerate(templates.ONLINE_WIDTHS, 1):
            ws.column_dimensions[chr(64 + i)].width = w
        teal, grey = PatternFill("solid", fgColor="FF15677B"), PatternFill("solid", fgColor=Color(theme=0, tint=-0.5))
        head = []
        for col in templates.ONLINE_COLUMNS:
            cell = WriteOnlyCell(ws, value=col)
            cell.font = Font(name="Calibri", size=9, bold=True, color=Color(theme=0))
            cell.fill = grey if col in ("INFLUENCER", "PERSON", "SUMMARY") else teal
            head.append(cell)
        ws.append(head)
        seen, n = set(), 0
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for r in src.execute(f"SELECT * FROM articles WHERE key IN ({','.join('?' * len(part))})", part):
                text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
                d, t = r["pub_date"] or "", r["pub_time"] or ""
                row = templates.online_row({"url": r["url"], "title": r["title"], "author": r["author"], "text": text,
                                            "description": r["summary"], "image": r["image"],
                                            "influencer": r["influencer"], "person": r["persons"], "published": d,
                                            "published_full": f"{d} {t}" if re.match(r"\d{2}:\d{2}$", t) and d else ""},
                                           WIB)
                if row[3] in seen or not (row[10] or "").strip() or oc.is_junk_page(row[2], row[3]):
                    continue
                seen.add(row[3])
                ws.append([oc.safe_cell(v, templates.ONLINE_COLUMNS[k]) for k, v in enumerate(row)])
                n += 1
        os.makedirs(out_dir, exist_ok=True)
        slug = re.sub(r"[^\w]+", "_", q)[:40].strip("_") or "search"
        name = f"Search_{slug}_{datetime.now(WIB):%Y%m%d_%H%M}.xlsx"
        tmp = os.path.join(out_dir, name + ".tmp")
        wb.save(tmp)
        os.replace(tmp, os.path.join(out_dir, name))
        return {"file": name, "rows": n}
    finally:
        db.close()
        src.close()


# ---------- top stories ----------
def _words(title: str) -> set:
    t = re.sub(r"[^\w\s]", " ", (title or "").lower(), flags=re.U)
    return {w for w in t.split() if len(w) > 2 and w not in STOP_WORDS and not w.isdigit()}


def stories(hours: int = 6, limit: int = 20) -> dict:
    """The same story told by several outlets, grouped: titles sharing ≥ 60 % of their meaningful words (and ≥ 3)."""
    hours = max(1, min(int(hours or 6), 72))
    cached = _STATE["stories"]
    if cached[1] == hours and cached[2] is not None and time.time() - cached[0] < 300:
        return cached[2]
    src = _stream_ro()
    if src is None:
        return {"hours": hours, "stories": [], "articles": 0}
    try:
        rows = src.execute("SELECT url, domain, title, pub_date, pub_time, matched, fetched FROM articles "
                           "WHERE status='success' AND fetched >= ? ORDER BY fetched DESC LIMIT 60000",
                           (time.time() - hours * 3600,)).fetchall()
    finally:
        src.close()
    words = [_words(r["title"]) for r in rows]
    df = Counter(w for ws in words for w in ws)
    common = {w for w, n in df.items() if n > max(200, len(rows) // 20)}        # too frequent to tell stories apart
    index: dict = defaultdict(list)
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, ws in enumerate(words):
        ws = ws - common
        words[i] = ws
        if len(ws) < 3:
            continue
        cand = Counter()
        for w in ws:
            for j in index[w][-300:]:
                cand[j] += 1
        for j, shared in cand.items():
            if shared >= 3 and shared / min(len(ws), len(words[j])) >= 0.6 and rows[i]["domain"] != rows[j]["domain"] \
                    or shared >= 4 and shared / max(len(ws), len(words[j])) >= 0.75:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
        for w in ws:
            index[w].append(i)
    groups: dict = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    out = []
    for members in groups.values():
        outlets = {rows[i]["domain"] for i in members}
        if len(outlets) < 2:
            continue
        first = min(members, key=lambda i: rows[i]["fetched"])
        lead = Counter(w for i in members for w in words[i]).most_common(6)
        out.append({"title": rows[first]["title"], "outlets": len(outlets), "articles": len(members),
                    "media": sorted(outlets)[:12], "first_seen": int(rows[first]["fetched"]),
                    "latest": int(max(rows[i]["fetched"] for i in members)), "url": rows[first]["url"],
                    "keywords": sorted({k for i in members for k in (rows[i]["matched"] or "").split(", ") if k}),
                    "words": [w for w, _n in lead]})
    out.sort(key=lambda s: (-s["outlets"], -s["latest"]))
    res = {"hours": hours, "articles": len(rows), "stories": out[:limit]}
    _STATE["stories"] = (time.time(), hours, res)
    return res


# ---------- spike alerts ----------
def spikes(now: float | None = None) -> list[dict]:
    """Keywords whose last hour is far above their usual hour (the 24 hours before)."""
    cfg = settings()
    now = now or time.time()
    src = _stream_ro()
    if src is None:
        return []
    try:
        rows = src.execute("SELECT matched, fetched FROM articles WHERE status='success' AND matched != '' "
                           "AND fetched >= ?", (now - 25 * 3600,)).fetchall()
    finally:
        src.close()
    last, before = Counter(), Counter()
    for r in rows:
        for k in r["matched"].split(", "):
            if k:
                (last if r["fetched"] >= now - 3600 else before)[k] += 1
    out = []
    for k, n in last.items():
        usual = before[k] / 24
        if n >= cfg["spike_min"] and n >= cfg["spike_factor"] * max(usual, 0.5):
            out.append({"keyword": k, "last_hour": n, "usual": round(usual, 1),
                        "factor": round(n / max(usual, 0.5), 1)})
    return sorted(out, key=lambda x: -x["factor"])


def alert_spikes() -> list[dict]:
    cfg = settings()
    if not cfg["spike_alerts"]:
        return []
    sent = []
    meta_p = os.path.join(_dir(), "alerts.json")
    try:
        with open(meta_p, encoding="utf-8") as f:
            last_sent = json.load(f)
    except (OSError, ValueError):
        last_sent = {}
    for s in spikes():
        if time.time() - last_sent.get(s["keyword"], 0) < cfg["spike_quiet_hours"] * 3600:
            continue
        last_sent[s["keyword"]] = time.time()
        s["at"] = int(time.time())
        sent.append(s)
        try:
            from . import telegram_bot
            from .captcha import notify
            url = telegram_bot.public_url()
            notify(f"📈 Spike: “{s['keyword']}” — {s['last_hour']} articles in the last hour "
                   f"(usually ~{s['usual']}/hour, ×{s['factor']})." + (f"\n🔎 {url.rstrip('/')}/" if url else ""))
        except Exception:  # noqa: BLE001
            pass
    if sent:
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(last_sent, f)
        with _LOCK:
            _STATE["alerts"] = (sent + _STATE["alerts"])[:50]
    return sent


# ---------- background ----------
def _loop() -> None:
    last_spike = last_purge = 0.0
    while not _STATE["stop"].is_set():
        try:
            while index_new() >= 5000 and not _STATE["stop"].is_set():
                pass
            if time.time() - last_spike > 900:
                last_spike = time.time()
                alert_spikes()
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                try:
                    from . import stream
                    days = int(stream.settings().get("retention_days") or 0)
                    if days:                                      # 0 = kept for good, like the articles
                        purge_index(days)
                except Exception:  # noqa: BLE001
                    pass
            _STATE["error"] = ""
        except Exception as e:  # noqa: BLE001  never let the background die
            _STATE["error"] = str(e)[:200]
        _STATE["stop"].wait(15)


def start() -> None:
    with _LOCK:
        t = _STATE["thread"]
        if t and t.is_alive():
            return
        _STATE["stop"].clear()
        _STATE["thread"] = threading.Thread(target=_loop, daemon=True, name="insights")
        _STATE["thread"].start()


def status() -> dict:
    n = _STATE.get("count", (0, 0))
    if time.time() - n[0] > 120:
        try:
            db = _search_db()
            n = (time.time(), db.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"])
            db.close()
            _STATE["count"] = n
        except sqlite3.Error:
            pass
    n = n[1]
    return {"indexed": n, "settings": settings(), "alerts": list(_STATE["alerts"]), "error": _STATE["error"],
            "export": dict(_STATE.get("export") or {}),
            "spikes_now": spikes()}


def start_export(q: str, days: int) -> dict:
    """In the background (a search can match 100,000+ articles): the file lands in Exports."""
    from .paths import OUTPUT_DIR
    fts_query(q)
    with _LOCK:
        if _STATE.get("export", {}).get("running"):
            return {"ok": False, "error": "An export is already running."}
        _STATE["export"] = {"running": True, "q": q, "file": "", "rows": 0, "error": ""}

    def work():
        try:
            _STATE["export"].update(export(q, days, OUTPUT_DIR), running=False)
        except Exception as e:  # noqa: BLE001
            _STATE["export"].update(running=False, error=str(e)[:200])
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}
