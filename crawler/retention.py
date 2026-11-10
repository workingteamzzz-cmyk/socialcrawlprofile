"""Keep less data around on a server: result files and page copies older than a set number of days are deleted.

RETENTION_DAYS        result files (Excel/CSV) — default 30; 0 keeps them forever
DEBUG_RETENTION_DAYS  page copies in outputs/debug (they can show account names) — default 7; 0 keeps them
"""
from __future__ import annotations

import os
import threading
import time


def purge(output_dir: str, days: float, debug_days: float, now: float | None = None) -> int:
    """Delete old files; returns how many were deleted."""
    now = time.time() if now is None else now
    gone = 0
    for folder, keep in ((output_dir, days), (os.path.join(output_dir, "debug"), debug_days)):
        if not keep or not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > keep * 86400:
                    os.remove(path)
                    gone += 1
            except OSError:
                pass
    return gone


def start(output_dir: str, days: float, debug_days: float, log=print, every_s: float = 3600) -> None:
    """Purge now and then every hour, in the background."""
    def loop():
        while True:
            n = purge(output_dir, days, debug_days)
            if n:
                log(f"Retention: deleted {n} old result/debug file(s)")
            time.sleep(every_s)
    if days or debug_days:
        threading.Thread(target=loop, daemon=True, name="retention").start()
