"""The live news stream's own process: `python -m crawler.stream_worker` (started and watched by the web server).

It runs at a lower priority than the web server, follows the stream settings (on / off / numbers) within seconds,
takes the dashboard's requests from the `commands` table (sweep now, site check now, export a day, reload paid-media
logins, forget removed outlets) and writes its live state to live.json every 2 seconds. It ends by itself when the web
server ends (its stdin closes).
"""
from __future__ import annotations

import json
import os
import sys
import threading


def main() -> None:
    try:
        os.nice(10)                               # the dashboard always comes first
    except (AttributeError, OSError):
        pass
    parent_gone = threading.Event()

    def watch_parent():
        try:
            while sys.stdin.read(1):              # the web server holds the other end; EOF = it is gone
                pass
        except (OSError, ValueError):
            pass
        parent_gone.set()
    if not sys.stdin.isatty():
        threading.Thread(target=watch_parent, daemon=True).start()

    from crawler import insights, media_db, stream
    from crawler.paths import OUTPUT_DIR

    def log(m):
        print(m, flush=True)
    s = stream.get()
    try:
        media_db.install()
    except Exception:  # noqa: BLE001
        pass
    insights.start()
    path = os.path.join(stream._dir(), "settings.json")
    last_mt, prev = None, stream.settings()
    while not parent_gone.is_set():
        try:
            cfg = stream.settings()
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = None
            if cfg["enabled"] and not s.running:
                s.start(log)
            elif not cfg["enabled"] and s.running:
                s.stop()
            elif s.running and last_mt is not None and mt != last_mt:
                if any(prev.get(k) != cfg.get(k) for k in ("threads", "discovery_threads")):
                    s.stop()
                    s.start(log)
                else:
                    s.cfg, s.kw_cache = cfg, (0.0, [])
            last_mt, prev = mt, cfg
            for cmd, arg in stream.take_commands(s):
                if cmd == "catchup":
                    s.catchup_now.set()
                elif cmd == "health":
                    s.health_now.set()
                elif cmd == "export" and not s.export.get("running"):
                    s.export = {"running": True, "done": 0, "total": 0, "file": "", "error": "", "day": arg}
                    threading.Thread(target=stream._quiet, args=(s.export_day, arg, OUTPUT_DIR), daemon=True).start()
                elif cmd == "removed":
                    s.remove_outlets(json.loads(arg or "[]"))
                elif cmd == "subs":
                    media_db.install()
                elif cmd == "archive_export":
                    from crawler import backfill
                    threading.Thread(target=stream._quiet, args=(backfill.export, s, arg, OUTPUT_DIR), daemon=True).start()
            stream.write_live(s.live_state())
        except Exception as e:  # noqa: BLE001  the loop keeps going; the error shows on the dashboard
            s.last_error = f"worker: {str(e)[:150]}"
        parent_gone.wait(2)
    s.stop()


if __name__ == "__main__":
    main()
