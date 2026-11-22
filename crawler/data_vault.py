"""Data by year: what the server holds per year and month (articles, report rows, approximate size), a month or a
year out as a ZIP, and a ZIP back in.

Two ZIP formats:
  excel   per day one Excel in the team's Online layout (stream articles + News Search rows, a link once) and one in
          the Medsos layout (social posts of that day) — for people to open; "YYYY-MM/Online_YYYY-MM-DD.xlsx".
          Undated articles go in "YYYY-MM/Online_YYYY-MM-undated.xlsx" (the month they were found).
  backup  everything as it is stored, nothing left out: "YYYY-MM/articles_YYYY-MM-DD.jsonl" (every column of the
          stream's articles, the text as plain text) and "YYYY-MM/reports_YYYY-MM.jsonl" (the reporting database's rows)
          — read back exactly.

Both upload back (restore): a ZIP of Excel files (the team's Online / Medsos layouts — also files made elsewhere) or a
backup ZIP; a single Excel as before. Rows already on the server are left as they are (nothing is doubled).

A ZIP is made in the results folder (Exports), one at a time, and only when the disk has room for it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
import zlib
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
_LOCK = threading.Lock()
_STATE: dict = {"build": {}, "overview": {"t": 0.0, "v": None, "busy": False}}
KEEP_FREE_GB = 3                                    # never fill the disk: this much stays free after a ZIP
ART_COLS = ("key", "url", "domain", "title", "source", "feed_date", "feed_time", "discovered", "status", "fetched",
            "pub_date", "pub_time", "author", "summary", "image", "influencer", "persons", "matched", "error",
            "sentiment")


def _stream():
    from . import stream
    return stream.get()


def _archive_conn():
    from . import archive
    return archive._db()


# ---------- what the server holds ----------
def _compute() -> dict:
    s = _stream()
    months: dict[str, dict] = {}

    def m(key):
        return months.setdefault(key, {"month": key, "articles": 0, "online_rows": 0, "medsos_rows": 0, "bytes": 0})
    for r in s.rq("SELECT substr(pub_date, 1, 7) mo, COUNT(*) n FROM articles WHERE status='success' AND pub_date > '' "
                  "GROUP BY mo"):
        if re.fullmatch(r"\d{4}-\d{2}", r["mo"] or ""):
            m(r["mo"])["articles"] += r["n"]
    undated = s.rq("SELECT COUNT(*) n FROM articles WHERE status='success' AND pub_date = ''")[0]["n"]
    # an average stored article, from a sample (text compressed as stored, plus its other columns and indexes)
    smp = s.rq("SELECT AVG(COALESCE(length(content),0) + length(url) + length(title) + length(summary) + "
               "length(image) + length(persons) + length(influencer) + 160) b FROM "
               "(SELECT * FROM articles WHERE status='success' ORDER BY fetched DESC LIMIT 3000)")  # a_succ index
    art_b = float((smp[0]["b"] if smp else 0) or 3500) * 1.35
    rep_b = 2500.0
    try:
        c = _archive_conn()
        try:
            for r in c.execute("SELECT kind, substr(day, 1, 7) mo, COUNT(*) n FROM items WHERE day > '' "
                               "GROUP BY kind, mo"):
                if re.fullmatch(r"\d{4}-\d{2}", r[1] or ""):
                    m(r[1])["online_rows" if r[0] == "online" else "medsos_rows"] += r[2]
            got = c.execute("SELECT AVG(length(row) + length(text) + length(key) + 120) FROM "
                            "(SELECT * FROM items ORDER BY rowid DESC LIMIT 3000)").fetchone()
            rep_b = float((got[0] if got else 0) or 2500) * 1.3
        finally:
            c.close()
    except Exception:  # noqa: BLE001  no reporting database yet
        pass
    years: dict[str, dict] = {}
    for k in sorted(months, reverse=True):
        v = months[k]
        v["bytes"] = int(v["articles"] * art_b + (v["online_rows"] + v["medsos_rows"]) * rep_b)
        y = years.setdefault(k[:4], {"year": k[:4], "articles": 0, "online_rows": 0, "medsos_rows": 0, "bytes": 0,
                                     "months": []})
        for f in ("articles", "online_rows", "medsos_rows", "bytes"):
            y[f] += v[f]
        y["months"].append(v)
    return {"years": [years[y] for y in sorted(years, reverse=True)], "undated": undated,
            "undated_bytes": int(undated * art_b), "counted": int(time.time())}


def overview(wait: float = 5.0, fresh: bool = False) -> dict:
    """Per year and month: articles, report rows (Online / Medsos), approximate size. Counted in the background and
    kept for 30 minutes — counting a large database takes a while."""
    c = _STATE["overview"]
    if fresh and not c["busy"]:
        c["t"] = 0.0
    if time.time() - c["t"] > 1800 and not c["busy"]:
        c["busy"] = True

        def run():
            try:
                c["v"] = _compute()
                c["t"] = time.time()
            except Exception as e:  # noqa: BLE001
                c["v"] = {"error": str(e)[:200]}
                c["t"] = time.time() - 1500                        # try again in 5 minutes
            finally:
                c["busy"] = False
        th = threading.Thread(target=run, daemon=True, name="vault-count")
        th.start()
        if c["v"] is None:
            th.join(wait)
    return {"overview": c["v"], "counting": c["busy"], "build": dict(_STATE["build"]), "disk": _disk()}


def _disk() -> dict:
    from .paths import OUTPUT_DIR
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        u = shutil.disk_usage(OUTPUT_DIR)
        return {"free": u.free, "total": u.total}
    except OSError:
        return {}


# ---------- a month or a year out as a ZIP ----------
def _months_of(period: str) -> list[str]:
    if re.fullmatch(r"\d{4}-\d{2}", period or ""):
        return [period]
    if re.fullmatch(r"\d{4}", period or ""):
        return [f"{period}-{i:02d}" for i in range(1, 13)]
    raise ValueError("Choose a year (2026) or a month (2026-11).")


def _days(month: str) -> list[str]:
    d = datetime.strptime(month + "-01", "%Y-%m-%d")
    out = []
    while d.strftime("%Y-%m") == month:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def start_build(period: str, fmt: str) -> dict:
    """A ZIP of a month or a year: fmt 'excel' (Online / Medsos Excel per day) or 'backup' (everything, exactly)."""
    if fmt not in ("excel", "backup"):
        raise ValueError("Choose Excel or backup.")
    months = _months_of(period)
    ov = (overview(wait=30).get("overview") or {})
    est = 0
    for y in ov.get("years") or []:
        for mo in y["months"]:
            if mo["month"] in months:
                est += mo["bytes"]
    if fmt == "backup":
        est = int(est * 0.6)                    # text compresses well in a ZIP
    free = _disk().get("free") or 0
    if free and est + KEEP_FREE_GB * 1024 ** 3 > free:
        raise ValueError(f"Not enough free disk for this ZIP (about {est / 1024 ** 3:.1f} GB needed, "
                         f"{free / 1024 ** 3:.1f} GB free, {KEEP_FREE_GB} GB must stay free) — choose one month, or "
                         "free up space first.")
    with _LOCK:
        if _STATE["build"].get("running"):
            raise ValueError("A ZIP is being made — wait for it (or cancel it) first.")
        name = f"Data_{period}_{'Excel' if fmt == 'excel' else 'Backup'}_{datetime.now(WIB):%Y%m%d-%H%M}.zip"
        _STATE["build"] = {"running": True, "period": period, "format": fmt, "file": name, "done_days": 0,
                           "days": sum(len(_days(mo)) for mo in months), "rows": 0, "error": "", "cancel": False,
                           "started": int(time.time()), "estimate": est}
    threading.Thread(target=_build, args=(months, fmt, name), daemon=True, name="vault-build").start()
    return {"ok": True, "file": name, "estimate": est}


def cancel_build() -> dict:
    _STATE["build"]["cancel"] = True
    return {"ok": True}


def _build(months: list[str], fmt: str, name: str) -> None:
    from .paths import APP_DATA, OUTPUT_DIR
    st = _STATE["build"]
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp = os.path.join(OUTPUT_DIR, "." + name + ".part")
    work = tempfile.mkdtemp(prefix="vault-", dir=os.path.join(APP_DATA))
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for mo in months:
                for day in _days(mo):
                    if st.get("cancel"):
                        raise InterruptedError("cancelled")
                    if fmt == "excel":
                        _excel_day(z, work, mo, day)
                    else:
                        _backup_day(z, mo, day)
                    st["done_days"] += 1
                if fmt == "excel":
                    _excel_day(z, work, mo, "undated")
                else:
                    _backup_day(z, mo, "undated")
                    _backup_reports(z, mo)
            z.writestr("README.txt", _readme(fmt, months))
        if not st["rows"]:
            raise ValueError("Nothing stored in this period.")
        os.replace(tmp, os.path.join(OUTPUT_DIR, name))
        st["finished"] = int(time.time())
    except Exception as e:  # noqa: BLE001
        st["error"] = "Cancelled." if isinstance(e, InterruptedError) else str(e)[:200]
        try:
            os.remove(tmp)
        except OSError:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        st["running"] = False


def _readme(fmt: str, months: list[str]) -> str:
    if fmt == "excel":
        return ("Media monitoring data, " + months[0] + (" .. " + months[-1] if len(months) > 1 else "") + ".\n"
                "Per day: Online_<date>.xlsx (news, the team's Online layout) and Medsos_<date>.xlsx (social media).\n"
                "Upload this ZIP (Exports -> Restore) to put it back on a server; rows already there are kept once.\n")
    return ("Media monitoring backup, " + months[0] + (" .. " + months[-1] if len(months) > 1 else "") + ".\n"
            "articles_<date>.jsonl: every stored news article with all its columns; reports_<month>.jsonl: the\n"
            "reporting database (News Search, Keyword Search, social media). Upload this ZIP to restore it exactly.\n")


def _stream_rows(day: str, mo: str):
    s = _stream()
    if day == "undated":
        a = datetime.strptime(mo + "-01", "%Y-%m-%d").replace(tzinfo=WIB)
        b = (a + timedelta(days=32)).replace(day=1)
        where, args = "status='success' AND pub_date = '' AND discovered >= ? AND discovered < ?", \
            [a.timestamp(), b.timestamp()]
    else:
        where, args = "status='success' AND pub_date = ?", [day]
    last = 0
    while True:
        rows = s.rq(f"SELECT rowid, * FROM articles WHERE {where} AND rowid > ? ORDER BY rowid LIMIT 1000",
                    (*args, last))
        if not rows:
            return
        for r in rows:
            last = r["rowid"]
            yield r


def _report_rows(kind: str, day: str):
    c = _archive_conn()
    try:
        for r in c.execute("SELECT kind, key, keywords, source, day, sentiment, text, row, filled, first_seen, "
                           "last_seen FROM items WHERE kind = ? AND day = ?", (kind, day)):
            yield r
    finally:
        c.close()


def _write_xlsx(path: str, columns: list[str], rows) -> int:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.styles.colors import Color
    from . import online_clean as oc
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Sheet1")
    teal, grey = PatternFill("solid", fgColor="FF15677B"), PatternFill("solid", fgColor=Color(theme=0, tint=-0.5))
    head = []
    for col in columns:
        c = WriteOnlyCell(ws, value=col)
        c.font = Font(name="Calibri", size=9, bold=True, color=Color(theme=0))
        c.fill = grey if col in ("INFLUENCER", "PERSON", "SUMMARY") else teal
        head.append(c)
    ws.append(head)
    n = 0
    for row in rows:
        ws.append([oc.safe_cell(v, columns[k]) if k < len(columns) else v for k, v in enumerate(row)])
        n += 1
    wb.save(path)
    return n


def _excel_day(z: zipfile.ZipFile, work: str, mo: str, day: str) -> None:
    from . import online_clean as oc, templates
    st = _STATE["build"]
    seen: set = set()

    def online():
        for r in _stream_rows(day, mo):
            text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
            d, t = r["pub_date"] or "", r["pub_time"] or ""
            row = templates.online_row({"url": r["url"], "title": r["title"], "author": r["author"], "text": text,
                                        "description": r["summary"], "image": r["image"], "influencer": r["influencer"],
                                        "person": r["persons"], "published": d, "sentiment": r["sentiment"] or "",
                                        "published_full": f"{d} {t}" if d and re.match(r"\d{2}:\d{2}$", t) else ""},
                                       WIB)
            if row[3] in seen or oc.is_junk_page(row[2], row[3]):
                continue
            seen.add(row[3])
            yield row
        if day != "undated":                                  # News Search rows of the day the stream does not have
            for r in _report_rows("online", day):
                data = json.loads(r[7] or "{}")
                url = oc.normalize_url(data.get("URL"))
                if not url or url in seen:
                    continue
                seen.add(url)
                yield [data.get(c, "") for c in templates.ONLINE_COLUMNS]
    tag = f"{mo}-undated" if day == "undated" else day
    p = os.path.join(work, f"Online_{tag}.xlsx")
    n = _write_xlsx(p, templates.ONLINE_COLUMNS, online())
    if n:
        z.write(p, f"{mo}/Online_{tag}.xlsx", compress_type=zipfile.ZIP_STORED)   # an xlsx is compressed already
        st["rows"] += n
    os.remove(p)
    if day == "undated":
        return
    p = os.path.join(work, f"Medsos_{day}.xlsx")
    rows = ([json.loads(r[7] or "{}").get(c, "") for c in templates.MEDSOS_COLUMNS] for r in _report_rows("medsos", day))
    n = _write_xlsx(p, templates.MEDSOS_COLUMNS, rows)
    if n:
        z.write(p, f"{mo}/Medsos_{day}.xlsx", compress_type=zipfile.ZIP_STORED)
        st["rows"] += n
    os.remove(p)


def _backup_day(z: zipfile.ZipFile, mo: str, day: str) -> None:
    st = _STATE["build"]
    tag = f"{mo}-undated" if day == "undated" else day
    lines, n = [], 0
    for r in _stream_rows(day, mo):
        d = {k: r[k] for k in ART_COLS if k in r.keys()}
        d["content"] = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
        lines.append(json.dumps(d, ensure_ascii=False))
        n += 1
    if n:
        z.writestr(f"{mo}/articles_{tag}.jsonl", "\n".join(lines) + "\n")
        st["rows"] += n


def _backup_reports(z: zipfile.ZipFile, mo: str) -> None:
    st = _STATE["build"]
    cols = ("kind", "key", "keywords", "source", "day", "sentiment", "text", "row", "filled", "first_seen", "last_seen")
    c = _archive_conn()
    try:
        lines = [json.dumps(dict(zip(cols, r)), ensure_ascii=False) for r in c.execute(
            f"SELECT {', '.join(cols)} FROM items WHERE day >= ? AND day <= ?", (mo + "-01", mo + "-31"))]
    finally:
        c.close()
    if lines:
        z.writestr(f"{mo}/reports_{mo}.jsonl", "\n".join(lines) + "\n")
        st["rows"] += len(lines)


# ---------- a ZIP back in ----------
def is_zip_of_files(path: str) -> bool:
    """A ZIP of Excel / backup files (an .xlsx is a ZIP too — told apart by its own inner layout)."""
    if not zipfile.is_zipfile(path):
        return False
    with zipfile.ZipFile(path) as z:
        return "[Content_Types].xml" not in z.namelist()


def restore_zip(path: str, st: dict, restore_excel) -> None:
    """Each Excel in the ZIP through the usual restore (restore_excel), each backup file read back exactly. One file
    at a time, so a large ZIP never needs much extra disk."""
    from .paths import APP_DATA
    work = tempfile.mkdtemp(prefix="vault-in-", dir=APP_DATA)
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith("/") and not os.path.basename(n).startswith(".")
                     and "__MACOSX" not in n]
            todo = [n for n in names if n.lower().endswith((".xlsx", ".jsonl"))]
            if not todo:
                raise ValueError("The ZIP holds no Excel (.xlsx) or backup (.jsonl) files.")
            st.update(files=len(todo), files_done=0)
            for n in todo:
                if n.lower().endswith(".xlsx"):
                    p = os.path.join(work, "part.xlsx")
                    with z.open(n) as src, open(p, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    try:
                        restore_excel(p)
                    finally:
                        os.remove(p)
                else:
                    with z.open(n) as src:
                        _restore_jsonl(src, os.path.basename(n), st)
                st["files_done"] += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _restore_jsonl(src, name: str, st: dict) -> None:
    import io
    reports = name.startswith("reports_")
    batch: list = []

    def flush():
        if not batch:
            return
        if reports:
            st["report_rows"] = st.get("report_rows", 0) + _put_reports(batch)
        else:
            st["articles"] = st.get("articles", 0) + _put_articles(batch)
        batch.clear()
    for line in io.TextIOWrapper(src, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            batch.append(json.loads(line))
        except ValueError:
            continue
        st["rows"] = st.get("rows", 0) + 1
        if len(batch) >= 2000:
            flush()
    flush()


def _put_articles(rows: list[dict]) -> int:
    s = _stream()
    cols = [c for c in ART_COLS if c != "key"]
    vals = []
    for d in rows:
        if not str(d.get("key") or "").strip() or not str(d.get("url") or "").startswith("http"):
            continue
        vals.append((d["key"], *[d.get(c) if c != "status" else (d.get(c) or "success") for c in cols],
                     zlib.compress(str(d.get("content") or "").encode("utf-8"))))
    if not vals:
        return 0
    with s.lock:
        s._db.executemany("DELETE FROM seen WHERE key=?", [(v[0],) for v in vals])
        cur = s._db.executemany(f"INSERT OR IGNORE INTO articles(key, {', '.join(cols)}, content) VALUES "
                                f"({', '.join('?' * (len(cols) + 2))})", vals)
        s._db.commit()
    return max(0, cur.rowcount)


def _put_reports(rows: list[dict]) -> int:
    from . import archive
    cols = ("kind", "key", "keywords", "source", "day", "sentiment", "text", "row", "filled", "first_seen", "last_seen")
    vals = [tuple(d.get(c) for c in cols) for d in rows if d.get("kind") in ("online", "medsos") and d.get("key")]
    if not vals:
        return 0
    with archive._LOCK:
        c = archive._db()
        try:
            cur = c.executemany(f"INSERT OR IGNORE INTO items({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                                vals)
            n = max(0, cur.rowcount)
            c.executemany("INSERT OR IGNORE INTO urls(u) VALUES (?)", [(v[1],) for v in vals if v[0] == "online"])
            c.commit()
        finally:
            c.close()
    _STATE["overview"]["t"] = 0.0                       # counted again next time
    return n
