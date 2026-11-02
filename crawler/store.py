"""Account lists. Each list is a CSV file in data/lists/ (editable in Excel):
Group, Platform, Handle, URL, Status, Source, Notes."""
from __future__ import annotations

import csv
import hashlib
import os
import re
import threading
from datetime import datetime

from .accounts import PLATFORM_LABEL, parse_line

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTS_DIR = os.path.join(ROOT, "data", "lists")
FIELDS = ["Group", "Platform", "Handle", "URL", "Status", "Source", "Notes"]
# Files from earlier versions, migrated once into data/lists/
LEGACY_BASE = os.path.join(ROOT, "data", "accounts.csv")
LEGACY_CUSTOM = os.path.join(ROOT, "data", "custom_accounts.csv")

_lock = threading.RLock()


def _id(url: str) -> str:
    return hashlib.sha1(url.lower().encode()).hexdigest()[:12]


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:                       # older files used "Unit"
        if "Group" not in r and "Unit" in r:
            r["Group"] = r.pop("Unit")
    return rows


def _write_csv(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def _migrate():
    os.makedirs(LISTS_DIR, exist_ok=True)
    if os.path.exists(LEGACY_BASE) or os.path.exists(LEGACY_CUSTOM):
        rows = _read_csv(LEGACY_BASE) + _read_csv(LEGACY_CUSTOM)
        target = os.path.join(LISTS_DIR, "Ditjen SDA.csv")
        if rows and not os.path.exists(target):
            _write_csv(target, rows)
        for p in (LEGACY_BASE, LEGACY_CUSTOM):
            if os.path.exists(p):
                os.replace(p, p + ".migrated")


def _path(list_id: str) -> str | None:
    """Resolve a list id to its file, only among the files that exist (no path tricks)."""
    _migrate()
    for name in os.listdir(LISTS_DIR):
        if name.endswith(".csv") and name[:-4] == list_id:
            return os.path.join(LISTS_DIR, name)
    return None


def _entry(row: dict):
    url = (row.get("URL") or "").strip()
    acc = parse_line(url) if url else None
    if acc is None:
        return None
    return {
        "id": _id(acc.url), "group": (row.get("Group") or "").strip() or acc.handle,
        "platform": acc.platform, "platform_label": PLATFORM_LABEL[acc.platform],
        "handle": acc.handle, "url": acc.url, "status": (row.get("Status") or "").strip(),
        "note": (row.get("Notes") or "").strip(), "problem": acc.problem,
    }


# ---------- lists ----------
def lists() -> list[dict]:
    with _lock:
        _migrate()
        out = []
        for name in sorted(os.listdir(LISTS_DIR), key=str.lower):
            if name.endswith(".csv"):
                n = sum(1 for r in _read_csv(os.path.join(LISTS_DIR, name)) if (r.get("URL") or "").strip())
                out.append({"id": name[:-4], "name": name[:-4], "count": n})
        return out


def create_list(name: str) -> tuple[str | None, str | None]:
    name = re.sub(r"[^\w .()&+-]", "", " ".join((name or "").split()), flags=re.UNICODE).strip(" .")[:60]
    if not name:
        return None, "Enter a list name (letters, numbers, spaces)."
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name += " list"                   # reserved file names on Windows
    with _lock:
        _migrate()
        if any(n[:-4].lower() == name.lower() for n in os.listdir(LISTS_DIR) if n.endswith(".csv")):
            return None, "A list with this name already exists."
        _write_csv(os.path.join(LISTS_DIR, name + ".csv"), [])
        return name, None


def delete_list(list_id: str) -> bool:
    with _lock:
        p = _path(list_id)
        if not p:
            return False
        os.replace(p, p + ".deleted")    # kept on disk as a backup
        return True


# ---------- accounts in a list ----------
def load(list_id: str) -> list[dict]:
    with _lock:
        p = _path(list_id)
        out, seen = [], set()
        for row in _read_csv(p) if p else []:
            e = _entry(row)
            if e and e["id"] not in seen and not (e["problem"] and e["status"] == "Not found"):
                seen.add(e["id"])
                out.append(e)
        return out


def add(list_id: str, text: str, group: str) -> dict:
    group = (group or "").strip()[:80]
    added, errors = [], []
    with _lock:
        p = _path(list_id)
        if not p:
            return {"added": [], "errors": ["List not found."]}
        rows = _read_csv(p)
        existing = {(e["platform"], e["handle"].lower()) for e in (_entry(r) for r in rows) if e}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line_group = group
            for sep in ("|", "\t"):          # "URL | Group" or two columns pasted from Excel
                if sep in line:
                    line, line_group = (x.strip() for x in line.split(sep, 1))
                    line_group = line_group or group
                    break
            acc = parse_line(line)
            if acc is None:
                errors.append(f"{line}: not an Instagram/X/TikTok/YouTube/Threads/Facebook profile URL")
                continue
            if acc.problem:
                errors.append(f"{line}: {acc.problem}")
                continue
            if (acc.platform, acc.handle.lower()) in existing:
                errors.append(f"{line}: already in this list")
                continue
            existing.add((acc.platform, acc.handle.lower()))
            rows.append({"Group": line_group or acc.handle, "Platform": PLATFORM_LABEL[acc.platform],
                         "Handle": acc.handle, "URL": acc.url, "Status": "Added", "Source": "manual",
                         "Notes": f"Added {datetime.now():%Y-%m-%d}"})
            added.append(acc.url)
        if added:
            _write_csv(p, rows)
    return {"added": added, "errors": errors}


def remove(list_id: str, ids: list[str]) -> int:
    ids = set(ids)
    with _lock:
        p = _path(list_id)
        if not p:
            return 0
        rows = _read_csv(p)

        def row_id(r):
            acc = parse_line((r.get("URL") or "").strip()) if (r.get("URL") or "").strip() else None
            return _id(acc.url) if acc else None
        keep = [r for r in rows if row_id(r) not in ids]
        if len(keep) != len(rows):
            _write_csv(p, keep)
        return len(rows) - len(keep)


def lines_for(list_id: str, ids: list[str]) -> str:
    wanted = set(ids)
    return "\n".join(f"{e['url']} | {e['group']}" for e in load(list_id) if e["id"] in wanted)
