"""Media archive: every article of one outlet over a period (months, years) — from its sitemaps, all of them.

An admin picks an outlet and a period. The worker:
1. finds the outlet's sitemaps (robots.txt + the usual places, WordPress / news / dated archives) and walks every
   sitemap index, skipping archives that are clearly outside the period (by their date in the URL or their lastmod);
2. queues every article link of the period (undated ones too: the page's own date decides when it is read);
3. the stream's readers read them — behind fresh news, one page at a time for that outlet (its own pause).
Articles of an archive job are kept for good (the stream's 7-day clean-up skips them), appear in News Stream /
Explore, and the whole job comes out as one Online Excel.

How complete it gets depends on the outlet: most publish every article in their sitemaps; an outlet without
sitemaps for older years cannot be read back this way (the job says how many sitemaps and links it found).
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
import zlib
from datetime import date, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill(id TEXT PRIMARY KEY, domain TEXT, start TEXT, end TEXT, status TEXT,
  maps_total INTEGER DEFAULT 0, maps_done INTEGER DEFAULT 0, found INTEGER DEFAULT 0, gap REAL DEFAULT 1.5,
  created REAL, updated REAL, error TEXT DEFAULT '', file TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS backfill_maps(job TEXT, url TEXT, done INTEGER DEFAULT 0, PRIMARY KEY(job, url));
CREATE INDEX IF NOT EXISTS a_source ON articles(source, status);
"""
EXTRA_PATHS = ("/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml", "/post-sitemap.xml",
               "/sitemap/sitemap.xml")
MAX_MAPS = 30000
_DATE_IN_URL = [re.compile(r"(20\d{2})[-/_.]?(0[1-9]|1[0-2])[-/_.]?(0[1-9]|[12]\d|3[01])(?!\d)"),
                re.compile(r"(20\d{2})[-/_.]?(0[1-9]|1[0-2])(?!\d)"), re.compile(r"(?<!\d)(20\d{2})(?!\d)")]


def ensure(stream) -> None:
    with stream.lock:
        stream._db.executescript(SCHEMA)
        stream._db.commit()


def source_of(job_id: str) -> str:
    return f"backfill:{job_id}"


def start(stream, domain: str, start_d: str, end_d: str, gap: float = 1.5) -> dict:
    from .media_db import clean_domain
    d = clean_domain(domain)
    if not d:
        raise ValueError("Which outlet? Its domain, e.g. detik.com")
    for v in (start_d, end_d):
        if not re.match(r"\d{4}-\d{2}-\d{2}$", v or ""):
            raise ValueError("Dates as YYYY-MM-DD.")
    if start_d > end_d:
        raise ValueError("The start date is after the end date.")
    ensure(stream)
    jid = uuid.uuid4().hex[:10]
    gap = max(0.5, min(10.0, float(gap or 1.5)))
    stream.x("INSERT INTO backfill(id, domain, start, end, status, gap, created, updated) VALUES (?,?,?,?,?,?,?,?)",
             (jid, d, start_d, end_d, "queued", gap, time.time(), time.time()))
    return job(stream, jid)


def cancel(stream, jid: str) -> bool:
    ensure(stream)
    n = stream.x("UPDATE backfill SET status='cancelled', updated=? WHERE id=? AND status NOT IN ('done','cancelled')",
                 (time.time(), jid))
    stream.x("DELETE FROM articles WHERE source=? AND status='new' AND source LIKE 'backfill:%'", (source_of(jid),))
    return bool(n)


def delete(stream, jid: str) -> bool:
    """Forget the job and its articles (the Excel files already made stay in Exports)."""
    ensure(stream)
    cancel(stream, jid)
    stream.x("DELETE FROM articles WHERE source=? AND source LIKE 'backfill:%'", (source_of(jid),))
    stream.x("DELETE FROM backfill_maps WHERE job=?", (jid,))
    return bool(stream.x("DELETE FROM backfill WHERE id=?", (jid,)))


def job(stream, jid: str) -> dict:
    ensure(stream)
    r = stream.rq("SELECT * FROM backfill WHERE id=?", (jid,))
    if not r:
        raise ValueError("Unknown archive job.")
    j = dict(r[0])
    counts = {x["status"]: x["n"] for x in stream.rq("SELECT status, COUNT(*) n FROM articles WHERE source=? "
                                                       "AND source LIKE 'backfill:%' GROUP BY status", (source_of(jid),))}
    j["read"], j["waiting"] = counts.get("success", 0), counts.get("new", 0)
    j["not_read"] = sum(v for k, v in counts.items() if k not in ("success", "new"))
    return j


def jobs(stream) -> list[dict]:
    ensure(stream)
    return [job(stream, r["id"]) for r in stream.rq("SELECT id FROM backfill ORDER BY created DESC LIMIT 100")]


# ---------- the worker's side ----------
def _url_dates(url: str):
    """(first day, last day) a dated archive URL covers, or None when it carries no date."""
    import calendar
    for i, rx in enumerate(_DATE_IN_URL):
        m = rx.search(url)
        if not m:
            continue
        try:
            y = int(m.group(1))
            if i == 0:
                d = date(y, int(m.group(2)), int(m.group(3)))
                return d.isoformat(), d.isoformat()
            if i == 1:
                mo = int(m.group(2))
                return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
            return f"{y}-01-01", f"{y}-12-31"
        except ValueError:
            continue
    return None


def _parse(body: bytes):
    """('index', [(url, lastmod)]) or ('urlset', [(url, date, title)])."""
    from lxml import etree
    try:
        root = etree.fromstring(body, parser=etree.XMLParser(recover=True, huge_tree=True))
    except Exception:  # noqa: BLE001
        return "urlset", []
    if root is None:
        return "urlset", []
    tag = etree.QName(root).localname
    out = []
    if tag == "sitemapindex":
        for sm in root.iter("{*}sitemap"):
            loc, mod = sm.find("{*}loc"), sm.find("{*}lastmod")
            if loc is not None and loc.text:
                out.append((loc.text.strip(), (mod.text or "").strip()[:10] if mod is not None else ""))
        return "index", out
    for u in root.iter("{*}url"):
        loc = u.find("{*}loc")
        if loc is None or not loc.text:
            continue
        d = u.find(".//{*}publication_date")
        d = d if d is not None else u.find("{*}lastmod")
        t = u.find(".//{*}title")
        out.append((loc.text.strip(), (d.text or "").strip()[:10] if d is not None else "",
                    (t.text or "").strip() if t is not None else ""))
    return "urlset", out


def _discover(stream, j: dict) -> None:
    from .medmas.discovery import sitemaps as sm
    dom, jid = j["domain"], j["id"]
    roots, alive = sm._sitemaps_from_robots(dom)
    if not alive:
        stream.x("UPDATE backfill SET status='failed', error=?, updated=? WHERE id=?",
                 ("the site does not answer", time.time(), jid))
        return
    cands = list(dict.fromkeys(roots + [f"https://{dom}{p}" for p in sm.COMMON_PATHS + EXTRA_PATHS]))
    with stream.lock:
        stream._db.executemany("INSERT OR IGNORE INTO backfill_maps(job, url) VALUES (?,?)", [(jid, u) for u in cands])
        stream._db.execute("UPDATE backfill SET status='discovering', updated=? WHERE id=?", (time.time(), jid))
        stream._db.commit()


def _in_period(lo: str, hi: str, j: dict) -> bool:
    return not (hi < j["start"] or lo > j["end"])


def step(stream, ev=None, budget_s: float = 20.0) -> int:
    """One slice of work on the oldest active archive job (the worker calls this every few seconds).
    Returns how many new article links were queued."""
    from .medmas.discovery import sitemaps as sm
    from .medmas.filters import is_article_link
    from .online_clean import is_junk_page
    from .stream import norm_key
    ensure(stream)
    rows = stream.rq("SELECT * FROM backfill WHERE status IN ('queued','discovering','reading') ORDER BY created "
                     "LIMIT 1")
    if not rows:
        return 0
    j = dict(rows[0])
    jid, src = j["id"], source_of(j["id"])
    stream.gap_override[j["domain"]] = float(j["gap"] or 1.5)
    if j["status"] == "queued":
        _discover(stream, j)
        return 0
    if j["status"] == "reading":
        left = stream.rq("SELECT COUNT(*) n FROM articles WHERE source=? AND status='new' AND source LIKE 'backfill:%'", (src,))[0]["n"]
        if not left:
            stream.x("UPDATE backfill SET status='done', updated=? WHERE id=?", (time.time(), jid))
            stream.gap_override.pop(j["domain"], None)
        return 0
    added, t0 = 0, time.time()
    while time.time() - t0 < budget_s and not (ev is not None and ev.is_set()):
        todo = stream.rq("SELECT url FROM backfill_maps WHERE job=? AND done=0 LIMIT 1", (jid,))
        if not todo:
            total = stream.rq("SELECT COUNT(*) n FROM backfill_maps WHERE job=? AND done=1", (jid,))[0]["n"]
            stream.x("UPDATE backfill SET status='reading', maps_done=?, updated=? WHERE id=?", (total, time.time(), jid))
            break
        u = todo[0]["url"]
        fresh: list = []
        stream.x("UPDATE backfill_maps SET done=1 WHERE job=? AND url=?", (jid, u))
        try:
            body = sm._fetch(u, timeout=20)
        except Exception:  # noqa: BLE001  a missing guess (e.g. /post-sitemap.xml) is normal
            continue
        kind, items = _parse(body)
        if kind == "index":
            keep = []
            for child, mod in items:
                span = _url_dates(child)
                if span and not _in_period(span[0], span[1], j):
                    continue                                   # a dated archive outside the period
                if mod and re.match(r"\d{4}-\d{2}-\d{2}$", mod) and mod < j["start"] and not span:
                    continue                                   # nothing in it changed since before the period
                if any(x in child.lower() for x in ("image", "video", "tag", "category", "author", "page-sitemap")):
                    continue
                keep.append(child)
            n_maps = stream.rq("SELECT COUNT(*) n FROM backfill_maps WHERE job=?", (jid,))[0]["n"]
            keep = keep[:max(0, MAX_MAPS - n_maps)]
            with stream.lock:
                stream._db.executemany("INSERT OR IGNORE INTO backfill_maps(job, url) VALUES (?,?)",
                                       [(jid, c) for c in keep])
                stream._db.commit()
        else:
            span = _url_dates(u)
            rows_in = []
            for url, d, title in items:
                if not url.startswith("http") or not is_article_link(url) or is_junk_page(title, url):
                    continue
                if d and not (j["start"] <= d <= j["end"]):
                    continue
                if not d and span and not _in_period(span[0], span[1], j):
                    continue
                rows_in.append((norm_key(url), url, j["domain"], title[:500], src, d, "", time.time()))
            fresh = []
            with stream.lock:
                for r in rows_in:
                    if stream._db.execute("INSERT OR IGNORE INTO articles(key, url, domain, title, source, feed_date, "
                                          "feed_time, discovered) VALUES (?,?,?,?,?,?,?,?)", r).rowcount:
                        fresh.append(r)
                stream._db.commit()
                if stream.running:
                    for key, url, dom, title, _s, fd, ft, _t in fresh:
                        if stream.qn >= 4000:
                            break
                        stream._enqueue({"key": key, "url": url, "domain": dom, "title": title, "feed_date": fd,
                                         "feed_time": ft, "source": src}, front=False)
            added += len(fresh)
        tot = stream.rq("SELECT COUNT(*) n, SUM(done) d FROM backfill_maps WHERE job=?", (jid,))[0]
        stream.x("UPDATE backfill SET maps_total=?, maps_done=?, found=found+?, updated=? WHERE id=?",
                 (tot["n"], tot["d"] or 0, len(fresh) if kind != "index" else 0, time.time(), jid))
        time.sleep(min(2.0, float(j["gap"] or 1.5)))       # polite between two sitemap pages too
    return added


def export(stream, jid: str, out_dir: str) -> str:
    """Every article read for this archive job -> one Online Excel."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.styles.colors import Color
    from datetime import timedelta, timezone
    from . import online_clean as oc, templates
    j = job(stream, jid)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    for i, w in enumerate(templates.ONLINE_WIDTHS, 1):
        ws.column_dimensions[chr(64 + i)].width = w
    teal, grey = PatternFill("solid", fgColor="FF15677B"), PatternFill("solid", fgColor=Color(theme=0, tint=-0.5))
    head = []
    for col in templates.ONLINE_COLUMNS:
        c = WriteOnlyCell(ws, value=col)
        c.font = Font(name="Calibri", size=9, bold=True, color=Color(theme=0))
        c.fill = grey if col in ("INFLUENCER", "PERSON", "SUMMARY") else teal
        head.append(c)
    ws.append(head)
    wib, last, seen, n = timezone(timedelta(hours=7)), "", set(), 0
    while True:
        rows = stream.rq("SELECT * FROM articles WHERE source=? AND status='success' AND key > ? AND source LIKE 'backfill:%' ORDER BY key LIMIT 500",
                         (source_of(jid), last))
        if not rows:
            break
        for r in rows:
            last = r["key"]
            text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
            d, t = r["pub_date"] or "", r["pub_time"] or ""
            row = templates.online_row({"url": r["url"], "title": r["title"], "author": r["author"], "text": text,
                                        "description": r["summary"], "image": r["image"], "influencer": r["influencer"],
                                        "person": r["persons"], "published": d, "sentiment": r["sentiment"] or "",
                                        "published_full": f"{d} {t}" if d and re.match(r"\d{2}:\d{2}$", t) else ""}, wib)
            if row[3] in seen or not (row[10] or "").strip() or oc.is_junk_page(row[2], row[3]):
                continue
            seen.add(row[3])
            ws.append([oc.safe_cell(v, templates.ONLINE_COLUMNS[k]) for k, v in enumerate(row)])
            n += 1
    os.makedirs(out_dir, exist_ok=True)
    name = f"Media_{j['domain'].replace('.', '_')}_{j['start'].replace('-', '')}-{j['end'].replace('-', '')}_" \
           f"{datetime.now():%H%M}.xlsx"
    tmp = os.path.join(out_dir, name + ".tmp")
    wb.save(tmp)
    os.replace(tmp, os.path.join(out_dir, name))
    stream.x("UPDATE backfill SET file=?, updated=? WHERE id=?", (name, time.time(), jid))
    return name


def status_json(stream) -> str:
    return json.dumps(jobs(stream))
