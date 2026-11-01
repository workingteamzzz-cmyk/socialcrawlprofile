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


def link_rows(links, tz_hours: int):
    tz = timezone(timedelta(hours=tz_hours))
    rows = []
    for item in sorted(links, key=lambda r: (r["order"], (r.get("label") or r["handle"]).lower(), r["handle"].lower(), r["time"] or 0)):
        t = item["time"].astimezone(tz) if item["time"] else None
        rows.append([item["platform_label"], item.get("label", ""), item["handle"],
                     t.strftime("%Y-%m-%d") if t else "", t.strftime("%H:%M") if t else "", item["url"]])
    return rows


def write_outputs(path_xlsx: str, path_csv: str, links, statuses, meta: dict):
    tz_hours = meta["tz_hours"]
    tz_name = meta["tz_name"]
    rows = link_rows(links, tz_hours)

    wb = Workbook()
    ws = wb.active
    ws.title = "Post Links"
    ws.append(["No", "Platform", "Unit", "Handle", f"Date ({tz_name})", f"Time ({tz_name})", "Post Link"])
    for i, r in enumerate(rows, 1):
        ws.append([i] + r)
        cell = ws.cell(row=i + 1, column=7)
        cell.hyperlink = r[5]
        cell.font = Font(color="0563C1", underline="single")
    _autosize(ws, [6, 11, 34, 26, 16, 10, 70])

    ws2 = wb.create_sheet("Account Status")
    ws2.append(["Platform", "Unit", "Handle", "Profile URL", "Status", "Links", "Notes"])
    for s in statuses:
        ws2.append([s["platform_label"], s.get("label", ""), s["handle"], s["url"],
                    STATUS_LABEL.get(s["status"], s["status"]), s["count"], s["note"]])
    _autosize(ws2, [11, 34, 26, 50, 20, 12, 70])

    ws3 = wb.create_sheet("Info")
    for k, v in [("Period", f"{meta['start']} to {meta['end']} ({tz_name})"),
                 ("Created", meta["created"]), ("Total links", len(rows)),
                 ("Method", "Browser (no API) — dates decoded from post IDs")]:
        ws3.append([k, v])
    ws3.column_dimensions["A"].width = 14
    ws3.column_dimensions["B"].width = 70
    wb.save(path_xlsx)

    with open(path_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Platform", "Unit", "Handle", f"Date ({tz_name})", f"Time ({tz_name})", "Post Link"])
        w.writerows(rows)
