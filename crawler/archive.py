"""Reporting: every result kept in one database, topics, and reports in the team's templates.

- archive.db (data folder): one row per article (Online) / post or comment (Medsos), in the team's columns, with the
  keyword(s) that found it. Every News / Keyword / Profile / API-scraper run adds to it; a link found again is
  merged (keywords added, the fuller version kept). Old Excel files in Exports can be imported.
- topics.json: a topic = a name + its keywords ("PLN": pln, listrik padam, pemadaman, …).
- report(): a topic (or keywords) + period + Online / Medsos -> the team's Excel (Sheet1) + a Summary sheet
  (per day, per media/platform, sentiment, top accounts/outlets), and the numbers for the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime

_LOCK = threading.RLock()
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                                      "Dec"], 1)}
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    kind      TEXT,             -- online | medsos
    key       TEXT,             -- normalized URL (+ comment id)
    keywords  TEXT DEFAULT '',  -- '|pln|listrik padam|'
    source    TEXT DEFAULT '',  -- media domain / platform
    day       TEXT DEFAULT '',  -- YYYY-MM-DD
    sentiment TEXT DEFAULT '',
    text      TEXT DEFAULT '',  -- title + text, lower case, for matching
    row       TEXT,             -- JSON: the layout's columns
    filled    INTEGER DEFAULT 0,
    first_seen TEXT, last_seen TEXT,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_items_day ON items(kind, day);
"""


def _dir() -> str:
    from .paths import APP_DATA
    return APP_DATA


def _db() -> sqlite3.Connection:
    os.makedirs(_dir(), exist_ok=True)
    c = sqlite3.connect(os.path.join(_dir(), "archive.db"), timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _iso_day(v) -> str:
    """'10-Nov-2026' / '2026-11-10' / datetime -> '2026-11-10'."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if m and m.group(2).title() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).title()]:02d}-{int(m.group(1)):02d}"
    return ""


def _norm_url(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"[?#].*$", "", u) if not re.search(r"(story_fbid|v)=", u) else u
    return u.rstrip("/").lower()


def _keyify(kind: str, row: dict) -> str:
    if kind == "online":
        return _norm_url(row.get("URL"))
    url = _norm_url(row.get("Url"))
    if row.get("Sub Type") == "comment":
        return f"{url}#c{row.get('Post ID') or row.get('Text', '')[:40]}"
    return f"{(row.get('Platform') or '').lower()}:{row.get('Post ID') or url}"


def _filled(row: dict) -> int:
    return sum(1 for v in row.values() if v not in (None, ""))


def add(kind: str, items: list[tuple[str, dict]]) -> int:
    """items: (keyword, row in the layout's columns). Returns how many were new."""
    if kind not in ("online", "medsos") or not items:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    new = 0
    with _LOCK:
        c = _db()
        try:
            for kw, row in items:
                row = {k: (v if not isinstance(v, datetime) else v.isoformat()) for k, v in row.items() if k != "No"}
                key = _keyify(kind, row)
                if not key or key in ("online:", "medsos:"):
                    continue
                kws = [k.strip().lower() for k in str(kw or "").split(",") if k.strip()]
                if kind == "online":
                    day, src, sent = _iso_day(row.get("DATE")), row.get("MEDIA") or "", row.get("SENTIMENT") or ""
                    text = f"{row.get('TITLE') or ''} {row.get('SUMMARY') or ''} {row.get('CONTENT') or ''}"
                else:
                    day, src, sent = _iso_day(row.get("Date")), row.get("Platform") or "", row.get("Sentiment") or ""
                    text = f"{row.get('Username') or ''} {row.get('Text') or ''}"
                text = re.sub(r"<[^>]+>", " ", text).lower()[:20000]
                old = c.execute("SELECT keywords, row, filled FROM items WHERE kind=? AND key=?", (kind, key)).fetchone()
                if old is None:
                    c.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              (kind, key, "|" + "|".join(kws) + "|" if kws else "", src, day, sent, text,
                               json.dumps(row, ensure_ascii=False, default=str), _filled(row), now, now))
                    new += 1
                    continue
                merged = [k for k in old["keywords"].strip("|").split("|") if k]
                merged += [k for k in kws if k not in merged]
                if _filled(row) >= old["filled"]:
                    c.execute("UPDATE items SET keywords=?, source=?, day=?, sentiment=?, text=?, row=?, filled=?, "
                              "last_seen=? WHERE kind=? AND key=?",
                              ("|" + "|".join(merged) + "|" if merged else "", src, day or "", sent, text,
                               json.dumps(row, ensure_ascii=False, default=str), _filled(row), now, kind, key))
                else:
                    c.execute("UPDATE items SET keywords=?, last_seen=? WHERE kind=? AND key=?",
                              ("|" + "|".join(merged) + "|" if merged else "", now, kind, key))
            c.commit()
        finally:
            c.close()
    return new


def _row_keys(kind: str, rows: list[dict], tz) -> list[str]:
    from . import templates
    out = []
    for r in rows:
        try:
            if kind == "online":
                row = dict(zip(templates.ONLINE_COLUMNS, templates.online_row(r, tz)))
            else:
                row = dict(zip(templates.MEDSOS_COLUMNS, templates.medsos_row(0, r, tz)))
            out.append(_keyify(kind, row))
        except Exception:  # noqa: BLE001
            out.append("")
    return out


def only_new(kind: str, rows: list[dict], tz) -> list[dict]:
    """The rows not yet in the database (= not delivered by an earlier run): for hourly schedules."""
    rows = list(rows)
    keys = _row_keys(kind, rows, tz)
    known: set = set()
    with _LOCK:
        c = _db()
        try:
            want = [k for k in set(keys) if k]
            for i in range(0, len(want), 500):
                part = want[i:i + 500]
                known |= {r["key"] for r in c.execute(
                    f"SELECT key FROM items WHERE kind=? AND key IN ({','.join('?' * len(part))})", (kind, *part))}
        finally:
            c.close()
    return [r for r, k in zip(rows, keys) if not k or k not in known]


def add_rows(kind: str, rows: list[dict], tz, keyword_of=None) -> int:
    """Raw job rows -> the layout's columns -> archive (never raises)."""
    from . import templates
    try:
        if kind == "online":
            cols = templates.ONLINE_COLUMNS
            items = [((keyword_of or _kw)(r), dict(zip(cols, templates.online_row(r, tz)))) for r in rows]
        else:
            cols = templates.MEDSOS_COLUMNS
            items = [((keyword_of or _kw)(r), dict(zip(cols, templates.medsos_row(0, r, tz)))) for r in rows]
        return add(kind, items)
    except Exception:  # noqa: BLE001  reporting must never cost a run its file
        return 0


def _kw(r: dict) -> str:
    return r.get("keyword") or ", ".join(r.get("keywords") or []) or r.get("label") or r.get("handle") or ""


def import_file(path: str) -> tuple[str, int]:
    """An Excel file in the team's Online or Medsos layout (also the API scraper's 'Data …xlsx') -> archive."""
    from openpyxl import load_workbook

    from . import templates
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h or "").strip() for h in next(it, [])]
        if head[:len(templates.ONLINE_COLUMNS)] == templates.ONLINE_COLUMNS:
            kind = "online"
        elif head[:5] == templates.MEDSOS_COLUMNS[:5]:
            kind = "medsos"
        else:
            return "", 0
        items = []
        for vals in it:
            row = dict(zip(head, vals))
            if not any(v not in (None, "") for v in row.values()):
                continue
            if kind == "medsos" and "ID" in row and "Post ID" not in row:
                row["Post ID"] = row.pop("ID")
            items.append((str(row.pop("Query", "") or "").replace("(by link)", "").replace("(comment)", ""), row))
        return kind, add(kind, items)
    finally:
        wb.close()


# ---------- topics ----------
def _topics_path() -> str:
    return os.path.join(_dir(), "topics.json")


def topics() -> list[dict]:
    try:
        with open(_topics_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def save_topic(name: str, keywords, tid: str | None = None) -> dict:
    name = " ".join(str(name or "").split())[:80]
    kws = keywords if isinstance(keywords, list) else re.split(r"[,\n]+", str(keywords or ""))
    kws = list(dict.fromkeys(k.strip().lower() for k in kws if k and k.strip()))[:100]
    if not name or not kws:
        raise ValueError("A topic needs a name and at least one keyword.")
    with _LOCK:
        items = topics()
        t = next((x for x in items if x["id"] == tid), None) if tid else None
        if t is None:
            t = {"id": uuid.uuid4().hex[:10], "created": time.strftime("%Y-%m-%d")}
            items.append(t)
        t.update(name=name, keywords=kws)
        tmp = _topics_path() + ".tmp"
        os.makedirs(_dir(), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _topics_path())
        return t


def delete_topic(tid: str) -> bool:
    with _LOCK:
        items = topics()
        keep = [t for t in items if t["id"] != tid]
        if len(keep) == len(items):
            return False
        with open(_topics_path(), "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, indent=1)
        return True


# ---------- reports ----------
def query(kind: str, start: str, end: str, keywords: list[str], in_text: bool = True) -> list[dict]:
    """Rows of a period whose keyword is one of the topic's keywords — or, with in_text, whose text contains one."""
    kws = [k.strip().lower() for k in keywords if k and k.strip()]
    with _LOCK:
        c = _db()
        try:
            got = c.execute("SELECT keywords, text, row, day FROM items WHERE kind=? AND day>=? AND day<=? "
                            "ORDER BY day DESC", (kind, start, end)).fetchall()
        finally:
            c.close()
    out = []
    for g in got:
        tagged = [k for k in g["keywords"].strip("|").split("|") if k]
        hit = [k for k in kws if k in tagged or any(k in t for t in tagged)]
        if not hit and in_text:
            hit = [k for k in kws if _contains(g["text"], k)]
        if hit or not kws:
            row = json.loads(g["row"])
            row["_keywords"] = ", ".join(hit or tagged)
            out.append(row)
    return out


def _contains(text: str, kw: str) -> bool:
    words = [w for w in re.split(r'\s+', kw.replace('"', "")) if w]
    return bool(words) and all(re.search(r"(?<!\w)" + re.escape(w), text) for w in words)


def summary(kind: str, rows: list[dict]) -> dict:
    day_k, src_k, sent_k = ("DATE", "MEDIA", "SENTIMENT") if kind == "online" else ("Date", "Platform", "Sentiment")
    days = Counter(_iso_day(r.get(day_k)) for r in rows)
    sources = Counter((r.get(src_k) or "-") for r in rows)
    sents = Counter((r.get(sent_k) or "-").lower() for r in rows)
    out = {"total": len(rows), "per_day": sorted(days.items()), "top_sources": sources.most_common(15),
           "sentiment": dict(sents), "per_keyword": Counter(k.strip() for r in rows
                                                            for k in r.get("_keywords", "").split(",") if k.strip()).most_common(20)}
    if kind == "medsos":
        out["top_accounts"] = Counter(r.get("Username") or "-" for r in rows).most_common(15)
        out["comments"] = sum(1 for r in rows if r.get("Sub Type") == "comment")
        out["engagement"] = sum(int(r.get(k) or 0) for r in rows for k in ("Likes", "Reply", "Shares")
                                if str(r.get(k) or "0").lstrip("-").isdigit())
    else:
        out["top_people"] = Counter(p for r in rows for p in str(r.get("PERSON") or "").split(",") if p).most_common(15)
    return out


def report(kind: str, start: str, end: str, keywords: list[str], name: str, out_dir: str,
           in_text: bool = True) -> dict:
    """Write Report_<name>_<Online|Medsos>_<start>_<end>.xlsx: Sheet1 in the team's layout + a Summary sheet."""
    from . import online_clean as oc
    from . import templates
    rows = query(kind, start, end, keywords, in_text)
    if kind == "online":
        cols = templates.ONLINE_COLUMNS
        values = [[r.get(c) for c in cols] for r in rows]
        values, _st = oc.finalize(values, cols, False)
    else:
        cols = templates.MEDSOS_COLUMNS
        values = [[i] + [r.get(c) for c in cols[1:]] for i, r in enumerate(rows, 1)]
    summ = summary(kind, rows)
    safe = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40] or "Report"
    fname = f"Report_{safe}_{'Online' if kind == 'online' else 'Medsos'}_{start}_{end}.xlsx"

    def add_summary(wb):
        from openpyxl.styles import Font
        ws = wb.create_sheet("Summary")
        bold = Font(bold=True)
        ws.append([f"Report: {name}", f"{'Online news' if kind == 'online' else 'Social media'}", f"{start} to {end}"])
        ws["A1"].font = bold
        ws.append(["Keywords", ", ".join(keywords)])
        ws.append(["Total", summ["total"]])
        for title, pairs in (("Per day", summ["per_day"]), ("Sentiment", sorted(summ["sentiment"].items())),
                             ("Top " + ("media" if kind == "online" else "platforms"), summ["top_sources"]),
                             ("Per keyword", summ["per_keyword"]),
                             ("Top accounts" if kind == "medsos" else "Top people",
                              summ.get("top_accounts") or summ.get("top_people") or [])):
            ws.append([])
            ws.append([title])
            ws.cell(row=ws.max_row, column=1).font = bold
            for k, v in pairs:
                ws.append([k, v])
        ws.column_dimensions["A"].width = 40
        ws.column_dimensions["B"].width = 16
    os.makedirs(out_dir, exist_ok=True)
    templates.write_values(kind, os.path.join(out_dir, fname), None, cols, values, extra=add_summary)
    return {"file": fname, **summ}


def stats() -> dict:
    with _LOCK:
        c = _db()
        try:
            got = {r["kind"]: (r["n"], r["a"], r["b"]) for r in c.execute(
                "SELECT kind, COUNT(*) n, MIN(day) a, MAX(day) b FROM items WHERE day<>'' GROUP BY kind")}
        finally:
            c.close()
    return {k: {"items": v[0], "from": v[1], "to": v[2]} for k, v in got.items()}


# ---------- shared by both servers ----------
def report_from(data: dict, out_dir: str) -> dict:
    kind = data.get("kind", "online")
    start, end = str(data.get("start", "")), str(data.get("end", ""))
    if kind not in ("online", "medsos"):
        return {"ok": False, "error": "kind is online or medsos"}
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)):
        return {"ok": False, "error": "Choose the period (YYYY-MM-DD)."}
    name, kws = data.get("name") or "", data.get("keywords") or []
    if data.get("topic_id"):
        t = next((x for x in topics() if x["id"] == data["topic_id"]), None)
        if not t:
            return {"ok": False, "error": "Topic not found."}
        name, kws = t["name"], t["keywords"]
    kws = kws if isinstance(kws, list) else str(kws).split(",")
    kws = [str(k).strip() for k in kws if k and str(k).strip()]
    if not kws:
        return {"ok": False, "error": "Choose a topic or type keywords."}
    return {"ok": True, **report(kind, start, end, kws, name or kws[0], out_dir, bool(data.get("in_text", True)))}


def import_folder(folder: str) -> dict:
    done = {"online": 0, "medsos": 0, "files": 0}
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.endswith(".xlsx") and not name.startswith(("Report_", "~$")):
            try:
                kind, n = import_file(os.path.join(folder, name))
            except Exception:  # noqa: BLE001
                continue
            if kind:
                done[kind] += n
                done["files"] += 1
    return {"ok": True, **done, "stats": stats()}
