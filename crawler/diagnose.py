"""How the server is doing, in one look — read only, changes nothing:

    sudo docker compose exec collector python -m crawler.diagnose

Memory in real use, database sizes, and how long the dashboard's main reads take right now (News Stream, a keyword,
each report topic, the media figures, a link check) — each line starts with its seconds.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime


def _t(label, fn):
    a = time.time()
    try:
        r = fn()
    except Exception as e:  # noqa: BLE001
        r = "ERROR " + str(e)[:160]
    print(f"{time.time() - a:6.1f}s  {label}  {'' if isinstance(r, dict) else r}")
    return r


def main() -> None:
    from . import archive, media_db, reader, stream
    m = stream.mem_used()
    print(f"memory in real use: {'unknown' if m is None else f'{m * 100:.0f}%'} of the limit"
          f"{' — nearly full: background extras resting' if stream.mem_tight() else ''}; "
          f"load {tuple(round(x, 1) for x in os.getloadavg())} on {os.cpu_count()} CPUs")
    d = stream._dir()
    print("  ".join(f"{f} {os.path.getsize(os.path.join(d, f)) / 1e9:.2f} GB" for f in
                    ("stream.db", "stream.db-wal", "search.db", "search.db-wal") if os.path.exists(os.path.join(d, f))))
    from .paths import APP_DATA
    sizes = []
    for root, _dirs, files in os.walk(APP_DATA):
        for f in files:
            try:
                sizes.append((os.path.getsize(os.path.join(root, f)), os.path.relpath(os.path.join(root, f), APP_DATA)))
            except OSError:
                pass
    st = os.statvfs(APP_DATA)
    print(f"disk: {st.f_bavail * st.f_frsize / 1e9:.1f} GB free; app data {sum(x[0] for x in sizes) / 1e9:.1f} GB — "
          "largest: " + ", ".join(f"{n} {b / 1e9:.2f} GB" for b, n in sorted(sizes, reverse=True)[:8]))
    cfg = stream.settings()
    print({k: cfg.get(k) for k in ("reader_processes", "threads", "discovery_threads", "sitemap_minutes")})
    day = datetime.now(stream.WIB).strftime("%Y-%m-%d")
    r = _t("News Stream, today", lambda: reader.articles(start=day, end=day, size=24))
    if isinstance(r, dict):
        print("        total", r["total"])
    r = _t("News Stream, keyword pln", lambda: reader.articles(q="pln", start=day, end=day, size=24))
    if isinstance(r, dict):
        print("        total", r["total"], "| searched:", r["query"], "|", [x["title"][:40] for x in r["rows"][:3]])
    for tp in archive.topics()[:10]:
        kws = tp.get("keywords") or []
        r = _t(f"topic {tp['name']} ({len(kws)} keywords; short ones: {[k for k in kws if len(k) <= 3][:10]})",
               lambda tp=tp: reader.articles(topic=tp["id"], start=day, end=day, size=24))
        if isinstance(r, dict):
            print("        total", r["total"], "|", [x["title"][:40] for x in r["rows"][:2]])
    _t("media figures", lambda: f"{len(media_db.stats(wait=0))} outlets")
    _t("200 links checked (Compare)", lambda: f"{len(archive.known_urls([f'https://x{i}.id/b/{i}' for i in range(200)]))} known")
    p = os.path.join(d, "search.db")
    if os.path.exists(p):
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
        try:
            print("report rows still to be checked for doubles:",
                  c.execute("SELECT COUNT(*) FROM rep WHERE dup IS NULL").fetchone()[0], "of",
                  c.execute("SELECT COUNT(*) FROM rep").fetchone()[0])
        finally:
            c.close()


if __name__ == "__main__":
    main()
