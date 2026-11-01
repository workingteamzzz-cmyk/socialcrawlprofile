"""Account store: the base list (data/accounts.csv) plus profiles added from the dashboard
(data/custom_accounts.csv)."""
from __future__ import annotations

import csv
import hashlib
import os
import threading
from datetime import datetime

from .accounts import PLATFORM_LABEL, parse_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_CSV = os.path.join(ROOT, "data", "accounts.csv")
CUSTOM_CSV = os.path.join(ROOT, "data", "custom_accounts.csv")
FIELDS = ["Unit", "Platform", "Handle", "URL", "Status", "Source", "Notes"]

_lock = threading.Lock()


def _id(url: str) -> str:
    return hashlib.sha1(url.lower().encode()).hexdigest()[:12]


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _to_entry(row: dict, source: str):
    url = (row.get("URL") or "").strip()
    if not url:
        return None
    unit = (row.get("Unit") or "").strip()
    acc = parse_line(url)
    if acc is None:
        return None
    return {
        "id": _id(acc.url),
        "unit": unit or acc.handle,
        "platform": acc.platform,
        "platform_label": PLATFORM_LABEL[acc.platform],
        "handle": acc.handle,
        "url": acc.url,
        "status": (row.get("Status") or "").strip() or ("Added" if source == "custom" else ""),
        "note": (row.get("Notes") or "").strip(),
        "source": source,
        "problem": acc.problem,
    }


def load_all() -> list[dict]:
    out, seen = [], set()
    for source, rows in (("base", _read_csv(BASE_CSV)), ("custom", _read_csv(CUSTOM_CSV))):
        for row in rows:
            e = _to_entry(row, source)
            if e and e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    return out


def add(text: str, unit: str) -> dict:
    unit = (unit or "").strip()
    added, errors = [], []
    with _lock:
        existing = {(e["platform"], e["handle"].lower()) for e in load_all()}
        new_rows = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line_unit = unit
            if "|" in line:
                line, line_unit = (x.strip() for x in line.split("|", 1))
            acc = parse_line(line)
            if acc is None:
                errors.append(f"{line}: not an Instagram/X/TikTok/YouTube/Threads/Facebook profile URL")
                continue
            if acc.problem:
                errors.append(f"{line}: {acc.problem}")
                continue
            if (acc.platform, acc.handle.lower()) in existing:
                errors.append(f"{line}: already in the list")
                continue
            existing.add((acc.platform, acc.handle.lower()))
            new_rows.append({"Unit": line_unit or acc.handle, "Platform": PLATFORM_LABEL[acc.platform],
                             "Handle": acc.handle, "URL": acc.url, "Status": "Added",
                             "Source": "manual", "Notes": f"Added {datetime.now():%Y-%m-%d}"})
            added.append(acc.url)
        if new_rows:
            os.makedirs(os.path.dirname(CUSTOM_CSV), exist_ok=True)
            rows = _read_csv(CUSTOM_CSV) + new_rows
            _write(CUSTOM_CSV, rows)
    return {"added": added, "errors": errors}


def remove(ids: list[str]) -> int:
    ids = set(ids)
    with _lock:
        rows = _read_csv(CUSTOM_CSV)
        def row_id(r):
            acc = parse_line(r.get("URL") or "")
            return _id(acc.url) if acc else None
        keep = [r for r in rows if row_id(r) not in ids]
        if len(keep) != len(rows):
            _write(CUSTOM_CSV, keep)
        return len(rows) - len(keep)


def lines_for(ids: list[str]) -> str:
    wanted = set(ids)
    return "\n".join(f"{e['url']} | {e['unit']}" for e in load_all() if e["id"] in wanted)


def _write(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
