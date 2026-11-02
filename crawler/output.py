"""Write the collected links to Excel + CSV."""
from __future__ import annotations

import csv
from datetime import timedelta, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

STATUS_LABEL = {
    "ok": "OK",
    "not_found": "Not found",
    "login_required": "Login required",
    "invalid": "Invalid username",
    "error": "Error",
    "stopped": "Stopped",
    "skipped": "Not processed",
}


def _autosize(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


DETAIL_HEADERS = ["Caption", "Likes", "Comments", "Shares / Reposts", "Views"]
DETAIL_KEYS = ["caption", "likes", "comments", "shares", "views"]
DETAIL_WIDTHS = [60, 10, 10, 14, 12]


def _detail_values(item) -> list:
    return [item.get(k) if item.get(k) is not None else "" for k in DETAIL_KEYS]


def link_rows(links, tz_hours: int, with_details: bool = False):
    tz = timezone(timedelta(hours=tz_hours))
    rows = []
    for item in sorted(links, key=lambda r: (r["order"], (r.get("label") or r["handle"]).lower(), r["handle"].lower(), r["time"] or 0)):
        t = item["time"].astimezone(tz) if item["time"] else None
        rows.append([item["platform_label"], item.get("label", ""), item["handle"],
                     t.strftime("%Y-%m-%d") if t else "", t.strftime("%H:%M") if t else "", item["url"]]
                    + (_detail_values(item) if with_details else []))
    return rows


def write_outputs(path_xlsx: str, path_csv: str, links, statuses, meta: dict):
    tz_hours = meta["tz_hours"]
    tz_name = meta["tz_name"]
    det = bool(meta.get("details"))
    rows = link_rows(links, tz_hours, det)
    head = ["Platform", "Group", "Handle", f"Date ({tz_name})", f"Time ({tz_name})", "Post Link"] + (DETAIL_HEADERS if det else [])

    wb = Workbook()
    ws = wb.active
    ws.title = "Post Links"
    ws.append(["No"] + head)
    for i, r in enumerate(rows, 1):
        ws.append([i] + r)
        cell = ws.cell(row=i + 1, column=7)
        cell.hyperlink = r[5]
        cell.font = Font(color="0563C1", underline="single")
    _autosize(ws, [6, 11, 34, 26, 16, 10, 70] + (DETAIL_WIDTHS if det else []))

    ws2 = wb.create_sheet("Account Status")
    ws2.append(["Platform", "Group", "Handle", "Profile URL", "Status", "Links", "Notes"])
    for s in statuses:
        ws2.append([s["platform_label"], s.get("label", ""), s["handle"], s["url"],
                    STATUS_LABEL.get(s["status"], s["status"]), s["count"], s["note"]])
    _autosize(ws2, [11, 34, 26, 50, 20, 12, 70])

    ws3 = wb.create_sheet("Info")
    for k, v in [("Period", f"{meta['start']} to {meta['end']} ({tz_name})"),
                 ("Created", meta["created"]), ("Total links", len(rows)),
                 ("Output", "Links + caption & engagement (snapshot at collection time)" if det else "Links only"),
                 ("Method", "Browser (no API) — dates decoded from post IDs")]:
        ws3.append([k, v])
    ws3.column_dimensions["A"].width = 14
    ws3.column_dimensions["B"].width = 70
    wb.save(path_xlsx)

    with open(path_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        w.writerows(rows)


def write_keyword_outputs(path_xlsx: str, path_csv: str, rows, statuses, meta: dict):
    """Keyword search results: one row per unique link (keywords that found it are merged)."""
    from .accounts import PLATFORM_LABEL
    tz_name = meta["tz_name"]
    rows = sorted(rows, key=lambda r: (r["keywords"][0].lower(), PLATFORM_LABEL[r["platform"]], -r["ts"]))
    det = meta["options"].detail == "full"
    header = ["No", "Keyword", "Platform", "Author", f"Date ({tz_name})", f"Time ({tz_name})", "Post Link"] + (DETAIL_HEADERS if det else [])
    values = lambda r: [r["keyword"], PLATFORM_LABEL[r["platform"]], r["author"], r["date"], r["time"], r["url"]] + \
        (_detail_values(r) if det else [])

    wb = Workbook()
    ws = wb.active
    ws.title = "Post Links"
    ws.append(header)
    for i, r in enumerate(rows, 1):
        ws.append([i] + values(r))
        cell = ws.cell(row=i + 1, column=7)
        cell.hyperlink = r["url"]
        cell.font = Font(color="0563C1", underline="single")
    _autosize(ws, [6, 22, 11, 26, 16, 10, 70] + (DETAIL_WIDTHS if det else []))

    ws2 = wb.create_sheet("Search Status")
    ws2.append(["Keyword", "Platform", "Status", "Links", "Notes"])
    for s in statuses:
        ws2.append([s["keyword"], PLATFORM_LABEL[s["platform"]], STATUS_LABEL.get(s["status"], s["status"]),
                    s["count"], s["note"]])
    _autosize(ws2, [22, 11, 18, 10, 90])

    opt = meta["options"]
    ws3 = wb.create_sheet("Info")
    for k, v in [("Period", f"{meta['start']} to {meta['end']} ({tz_name})"),
                 ("Keywords", ", ".join(meta["keywords"])),
                 ("Exact phrase", "yes" if opt.exact else "no"),
                 ("Excluded words", ", ".join(opt.exclude) or "-"),
                 ("X language", opt.x_lang or "any"),
                 ("X replies / reposts", f"{'excluded' if opt.x_no_replies else 'included'} / "
                                         f"{'excluded' if opt.x_no_reposts else 'included'}"),
                 ("Max links per keyword per platform", opt.cap or "no limit"),
                 ("Output", "Links + caption & engagement (snapshot at collection time)" if det else "Links only"),
                 ("Created", meta["created"]), ("Total links", len(rows)),
                 ("Method", "Each platform's own search in a logged-in browser (no API); dates decoded from post IDs. "
                            "Coverage depends on the platform's search.")]:
        ws3.append([k, v])
    ws3.column_dimensions["A"].width = 34
    ws3.column_dimensions["B"].width = 90
    wb.save(path_xlsx)

    with open(path_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header[1:])
        for r in rows:
            w.writerow(values(r))


NEWS_COLUMNS = {   # detail level -> columns (key, header, width)
    "links": [("keyword", "Keyword", 22), ("url", "Link", 80)],
    "basic": [("keyword", "Keyword", 22), ("published", "Date", 12), ("media", "Media", 22), ("title", "Title", 70),
              ("url", "Link", 70)],
    "full": [("keyword", "Keyword", 22), ("published", "Date", 12), ("media", "Media", 22), ("title", "Title", 60),
             ("url", "Link", 60), ("published_full", "Published (article)", 18), ("author", "Author", 22),
             ("text", "Article Text", 90)],
}


def write_news_outputs(path_xlsx: str, path_csv: str, rows, statuses, meta: dict):
    opt = meta["options"]
    cols = NEWS_COLUMNS[opt.detail]
    rows = sorted(rows, key=lambda r: (r["keywords"][0].lower(), -(r.get("ts") or 0)))
    link_col = [k for k, _, _ in cols].index("url") + 2

    wb = Workbook()
    ws = wb.active
    ws.title = "News Links"
    ws.append(["No"] + [h for _, h, _ in cols])
    for i, r in enumerate(rows, 1):
        ws.append([i] + [r.get(k, "") or "" for k, _, _ in cols])
        cell = ws.cell(row=i + 1, column=link_col)
        cell.hyperlink = r["url"]
        cell.font = Font(color="0563C1", underline="single")
    _autosize(ws, [6] + [w for _, _, w in cols])

    ws2 = wb.create_sheet("Search Status")
    ws2.append(["Keyword", "Status", "Links", "Notes"])
    for s in statuses:
        ws2.append([s["keyword"], STATUS_LABEL.get(s["status"], s["status"]), s["count"], s["note"]])
    _autosize(ws2, [26, 18, 10, 90])

    ws3 = wb.create_sheet("Info")
    for k, v in [("Period", f"{meta['start']} to {meta['end']} ({meta['tz_name']})"),
                 ("Keywords", ", ".join(meta["keywords"])),
                 ("Source", "Google Search, News tab, custom date range, sorted by date"),
                 ("Region", "Indonesia (Indonesian)" if opt.region == "id" else "Global (English)"),
                 ("Exact phrase", "yes" if opt.exact else "no"),
                 ("Excluded words", ", ".join(opt.exclude) or "-"),
                 ("Only these sites", ", ".join(opt.sites) or "all"),
                 ("Max links per keyword", opt.cap),
                 ("Output", {"links": "Links only", "basic": "Links + title, media, date",
                             "full": "Full: + article text, author, published time"}[opt.detail]),
                 ("Created", meta["created"]), ("Total links", len(rows))]:
        ws3.append([k, v])
    ws3.column_dimensions["A"].width = 24
    ws3.column_dimensions["B"].width = 90
    wb.save(path_xlsx)

    with open(path_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([h for _, h, _ in cols])
        for r in rows:
            w.writerow([r.get(k, "") or "" for k, _, _ in cols])
