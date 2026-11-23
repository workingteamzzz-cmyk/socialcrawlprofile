"""Free up the server by period: everything of a period (e.g. 1 Oct - 1 Nov 2026) out as one Excel to keep elsewhere
(Google Drive), then deleted from the server — the stream's articles (and their search entries), the reporting
database's rows and the result files made in that period.

Deleted stream articles are remembered as "seen" for good, so the stream never reads them again from a sitemap.
SQLite reuses the freed space for new articles (the database file itself does not shrink at once).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
STATUSES = ("success", "new", "failed", "filtered", "skipped", "old")
FOREVER = 4102444800.0                     # "seen" entries the 60-day clean-up never removes (year 2100)
_STATE: dict = {"export": {}, "delete": {}, "restore": {}}
_LOCK = threading.Lock()


def _check(start: str, end: str) -> tuple[float, float]:
    for v in (start, end):
        if not re.match(r"\d{4}-\d{2}-\d{2}$", v or ""):
            raise ValueError("Dates as YYYY-MM-DD.")
    if start > end:
        raise ValueError("The start date is after the end date.")
    a = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=WIB).timestamp()
    b = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=WIB).timestamp()
    return a, b


def _where() -> str:
    # by publication day; an article without one counts by the day it was found
    return (f"status IN ({','.join('?' * len(STATUSES))}) AND (pub_date BETWEEN ? AND ? OR "
            f"((pub_date IS NULL OR pub_date = '') AND discovered >= ? AND discovered < ?))")


def _files(start: str, end: str) -> list[dict]:
    from .server import list_outputs
    a, b = _check(start, end)
    return [f for f in list_outputs() if a <= f["mtime"] < b]


def preview(start: str, end: str) -> dict:
    from . import archive, stream
    a, b = _check(start, end)
    s = stream.get()
    n = s.rq(f"SELECT COUNT(*) n FROM articles WHERE {_where()}", (*STATUSES, start, end, a, b))[0]["n"]
    total = s.rq("SELECT COUNT(*) n FROM articles")[0]["n"] or 1
    try:
        size = os.path.getsize(s.db_path)
    except OSError:
        size = 0
    rep = 0
    try:
        with archive._LOCK:
            c = archive._db()
            try:
                rep = c.execute("SELECT COUNT(*) FROM items WHERE day BETWEEN ? AND ?", (start, end)).fetchone()[0]
            finally:
                c.close()
    except Exception:  # noqa: BLE001
        pass
    files = _files(start, end)
    return {"start": start, "end": end, "articles": n, "articles_bytes": int(size * n / total),
            "report_rows": rep, "files": len(files), "files_bytes": sum(f["size"] for f in files),
            "readable": s.rq("SELECT COUNT(*) n FROM articles WHERE status='success' AND pub_date BETWEEN ? AND ?",
                             (start, end))[0]["n"]}


# ---------- 1. the period out as Excel ----------
def _export(start: str, end: str, out_dir: str) -> None:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.styles.colors import Color
    from . import online_clean as oc, stream, templates
    s = stream.get()
    st = _STATE["export"]
    st["total"] = s.rq("SELECT COUNT(*) n FROM articles WHERE status='success' AND pub_date BETWEEN ? AND ?",
                       (start, end))[0]["n"]
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
    last, seen, n = 0, set(), 0
    while True:
        rows = s.rq("SELECT rowid, * FROM articles WHERE status='success' AND pub_date BETWEEN ? AND ? AND rowid > ? "
                    "ORDER BY rowid LIMIT 500", (start, end, last))
        if not rows:
            break
        for r in rows:
            last = r["rowid"]
            text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
            d, t = r["pub_date"] or "", r["pub_time"] or ""
            row = templates.online_row({"url": r["url"], "title": r["title"], "author": r["author"], "text": text,
                                        "description": r["summary"], "image": r["image"], "influencer": r["influencer"],
                                        "person": r["persons"], "published": d, "sentiment": r["sentiment"] or "",
                                        "published_full": f"{d} {t}" if d and re.match(r"\d{2}:\d{2}$", t) else ""}, WIB)
            if row[3] in seen or not (row[10] or "").strip() or oc.is_junk_page(row[2], row[3]):
                continue
            seen.add(row[3])
            ws.append([oc.safe_cell(v, templates.ONLINE_COLUMNS[k]) for k, v in enumerate(row)])
            n += 1
            if n >= 1_048_000:                         # one Excel sheet's limit
                break
        st["done"] = min(st["total"], st.get("done", 0) + len(rows))
        if n >= 1_048_000:
            st["note"] = "Excel's limit of 1,048,576 rows reached — export a shorter period for the rest."
            break
    os.makedirs(out_dir, exist_ok=True)
    name = f"All_articles_{start.replace('-', '')}-{end.replace('-', '')}_{datetime.now(WIB):%H%M}.xlsx"
    tmp = os.path.join(out_dir, name + ".tmp")
    wb.save(tmp)
    os.replace(tmp, os.path.join(out_dir, name))
    st.update(file=name, rows=n)


def start_export(start: str, end: str) -> dict:
    from .paths import OUTPUT_DIR
    _check(start, end)
    with _LOCK:
        if _STATE["export"].get("running") or _STATE["delete"].get("running"):
            return {"ok": False, "error": "Another export or delete is running."}
        _STATE["export"] = {"running": True, "start": start, "end": end, "done": 0, "total": 0, "file": "", "error": ""}

    def work():
        try:
            _export(start, end, OUTPUT_DIR)
        except Exception as e:  # noqa: BLE001
            _STATE["export"]["error"] = str(e)[:200]
        _STATE["export"]["running"] = False
    threading.Thread(target=work, daemon=True, name="cleanup-export").start()
    return {"ok": True}


# ---------- 2. delete the period ----------
def _delete(start: str, end: str, parts: dict) -> None:
    from . import archive, stream
    from .server import delete_outputs
    st = _STATE["delete"]
    a, b = _check(start, end)
    s = stream.get()
    if parts.get("articles", True):
        sp = os.path.join(os.path.dirname(s.db_path), "search.db")
        while True:
            rows = s.rq(f"SELECT rowid, key FROM articles WHERE {_where()} LIMIT 5000", (*STATUSES, start, end, a, b))
            if not rows:
                break
            keys = [r["key"] for r in rows]
            with s.lock:
                s._db.executemany("INSERT OR REPLACE INTO seen(key, t) VALUES (?,?)", [(k, FOREVER) for k in keys])
                s._db.executemany("DELETE FROM articles WHERE rowid=?", [(r["rowid"],) for r in rows])
                s._db.commit()
            if os.path.exists(sp):
                import sqlite3
                c = sqlite3.connect(sp, timeout=60)
                try:
                    from .insights import _fts_drop
                    _fts_drop(c, "docs", keys)                 # by the key table: never a read of the whole index
                    c.commit()
                except sqlite3.Error:
                    pass
                finally:
                    c.close()
            st["articles"] = st.get("articles", 0) + len(rows)
            time.sleep(0.05)                           # the readers get the database between chunks
    if parts.get("reports", True):
        with archive._LOCK:
            c = archive._db()
            try:
                st["report_rows"] = c.execute("DELETE FROM items WHERE day BETWEEN ? AND ?", (start, end)).rowcount
                c.commit()
            finally:
                c.close()
        try:
            from . import insights
            insights.prune_reports()                   # the News Stream reader forgets them too
        except Exception:  # noqa: BLE001
            pass
    if parts.get("files", True):
        keep = set(parts.get("keep_files") or [])
        st["files"] = len(delete_outputs([f["name"] for f in _files(start, end) if f["name"] not in keep
                                          and not re.fullmatch(r"Data_.+\.zip", f["name"])]))   # backups stay


def start_delete(start: str, end: str, parts: dict | None = None) -> dict:
    _check(start, end)
    p0 = dict(parts or {})
    if p0.get("articles", True) or p0.get("reports", True):    # data goes only when a full backup of it exists
        from . import data_vault
        name, why = data_vault.covering_backup(start, end)
        if not name:
            return {"ok": False, "error": why}
    with _LOCK:
        if _STATE["export"].get("running") or _STATE["delete"].get("running"):
            return {"ok": False, "error": "Another export or delete is running."}
        _STATE["delete"] = {"running": True, "start": start, "end": end, "articles": 0, "report_rows": 0, "files": 0,
                            "error": ""}
    p = dict(parts or {})
    ex = _STATE["export"]
    if ex.get("file") and ex.get("start") == start and ex.get("end") == end:
        p.setdefault("keep_files", [ex["file"]])        # the backup just made is not deleted with the period's files

    def work():
        try:
            _delete(start, end, p)
        except Exception as e:  # noqa: BLE001
            _STATE["delete"]["error"] = str(e)[:200]
        _STATE["delete"]["running"] = False
        _STATE["delete"]["finished"] = int(time.time())
        from . import reader
        reader.forget_counts()
    threading.Thread(target=work, daemon=True, name="cleanup-delete").start()
    return {"ok": True}


def status() -> dict:
    return json.loads(json.dumps(_STATE))


# ---------- 3. restore a period from its Excel ----------
def upload_chunk(upload_id: str, offset: int, data: bytes, final: bool) -> dict:
    """A backup Excel comes in pieces (a big month can be hundreds of MB); the last piece starts the restore."""
    from .paths import APP_DATA
    if not re.fullmatch(r"[a-f0-9]{8,40}", upload_id or ""):
        raise ValueError("Bad upload id.")
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("A piece is larger than 16 MB.")
    folder = os.path.join(APP_DATA, "uploads")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, upload_id + ".upload")         # an Excel or a ZIP of Excel / backup files
    if offset == 0 and os.path.exists(path):
        os.remove(path)
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if offset != size:
        raise ValueError(f"Pieces out of order (expected {size}, got {offset}).")
    with open(path, "ab") as f:
        f.write(data)
    if not final:
        return {"ok": True, "received": size + len(data)}
    return start_restore(path)


def _plain(html: str) -> str:
    paras = re.findall(r"<p>(.*?)</p>", html or "", flags=re.S) or [html or ""]
    return "\n\n".join(re.sub(r"<[^>]+>", "", p).strip() for p in paras if p.strip())


def _restore(path: str) -> None:
    from openpyxl import load_workbook
    from . import archive, stream
    from .stream import norm_key
    st = _STATE["restore"]
    kind, rep = archive.import_file(path)                      # the reporting database first (Online or Medsos)
    st["report_rows"] = st.get("report_rows", 0) + rep          # added up: a ZIP brings many files
    st["kind"] = kind or st.get("kind", "")
    if not kind:                                                # not the team's Online / Medsos layout: said, not hidden
        st.setdefault("skipped", []).append(st.get("current") or os.path.basename(path))
        return
    if kind != "online":
        return                                                 # a Medsos file: reports only
    s = stream.get()
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it, [])]
        batch, now = [], time.time()

        def flush():
            if not batch:
                return
            with s.lock:
                s._db.executemany("DELETE FROM seen WHERE key=?", [(b[0],) for b in batch])
                cur = s._db.executemany(
                    "INSERT OR IGNORE INTO articles(key, url, domain, title, source, discovered, status, fetched, pub_date,"
                    " pub_time, author, summary, content, image, influencer, persons, sentiment) "
                    "VALUES (?,?,?,?, 'restore', ?, 'success', ?, ?,?,?,?,?,?,?,?,?)", batch)
                st["articles"] = st.get("articles", 0) + max(0, cur.rowcount)
                s._db.commit()
            batch.clear()
        for vals in it:
            r = dict(zip(head, vals))
            url = str(r.get("URL") or "").strip()
            if not url.startswith("http"):
                continue
            d = archive._iso_day(r.get("DATE"))
            t = str(r.get("TIME") or "")[:5]
            text = _plain(str(r.get("CONTENT") or ""))
            batch.append((norm_key(url), url, str(r.get("MEDIA") or "").strip(), str(r.get("TITLE") or "")[:500], now,
                          now, d, t if re.match(r"\d{2}:\d{2}$", t) else "", str(r.get("AUTHOR") or ""),
                          str(r.get("SUMMARY") or ""), zlib.compress(text.encode("utf-8")), str(r.get("IMAGE") or ""),
                          str(r.get("INFLUENCER") or ""), str(r.get("PERSON") or ""),
                          str(r.get("SENTIMENT") or "").lower()))
            st["rows"] = st.get("rows", 0) + 1
            if len(batch) >= 2000:
                flush()
        flush()
    finally:
        wb.close()


def start_restore(path: str) -> dict:
    with _LOCK:
        if _STATE.get("restore", {}).get("running"):
            return {"ok": False, "error": "A restore is running."}
        _STATE["restore"] = {"running": True, "file": os.path.basename(path), "rows": 0, "articles": 0, "report_rows": 0,
                             "error": ""}

    def work():
        nonlocal path
        from . import data_vault
        try:
            if data_vault.is_zip_of_files(path):
                data_vault.restore_zip(path, _STATE["restore"], _restore)
            else:
                if not path.endswith(".xlsx"):               # the Excel reader goes by the file's ending
                    xl = os.path.splitext(path)[0] + ".xlsx"
                    os.replace(path, xl)
                    path = xl
                _restore(path)
        except Exception as e:  # noqa: BLE001
            _STATE["restore"]["error"] = str(e)[:200]
        _STATE["restore"]["running"] = False
        _STATE["restore"]["finished"] = int(time.time())
        from . import reader
        reader.forget_counts()
        try:
            os.remove(path)
        except OSError:
            pass
    threading.Thread(target=work, daemon=True, name="cleanup-restore").start()
    return {"ok": True, "restoring": True}
