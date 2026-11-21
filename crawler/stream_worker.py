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
import time


def _readers_wanted(stream, s) -> int:
    """Extra reader processes to run now: none while the stream is off."""
    return stream.reader_processes() - 1 if s.running else 0


def _manage_readers(stream, s, procs: dict) -> None:
    """Keeps the extra reader processes (shards 1 … n-1) running while the stream runs; restarts a dead one; stops
    them all when the stream stops or the number changes (the main process re-shards on its restart)."""
    import subprocess
    want = _readers_wanted(stream, s)
    n = want + 1
    for k, (p, pn) in list(procs.items()):
        if p.poll() is not None or k > want or pn != n:
            if p.poll() is None:
                try:
                    p.stdin.close()                       # EOF: the reader stops by itself
                    p.wait(timeout=70)
                except Exception:  # noqa: BLE001
                    p.kill()
            procs.pop(k, None)
    for k in range(1, want + 1):
        if k not in procs:
            procs[k] = (subprocess.Popen([sys.executable, "-m", "crawler.stream_worker", "--reader", str(k), str(n)],
                                         stdin=subprocess.PIPE,
                                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), n)


def reader_main(k: int, n: int) -> None:
    """A reader process: reads its share of the outlets until the stream stops or the main process is gone."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    gone = threading.Event()

    def watch_parent():
        try:
            while sys.stdin.read(1):
                pass
        except (OSError, ValueError):
            pass
        gone.set()
    threading.Thread(target=watch_parent, daemon=True).start()
    from crawler import stream

    def log(m):
        print(m, flush=True)
    s = stream.Stream()
    s.start_readers(k, n, log)
    while not gone.is_set():
        cfg = stream.settings()
        if not cfg.get("enabled") or stream.reader_processes(cfg) != n:
            break
        try:
            st = s.live_state()
            stream.write_reader_live(k, {"t": time.time(), "running": True, "per_minute": st["per_minute"],
                                         "per_day_pace": st["per_day_pace"], "readers_busy": st["readers_busy"]})
        except Exception:  # noqa: BLE001
            pass
        gone.wait(2)
    s.stop()
    try:
        stream.write_reader_live(k, {"t": time.time(), "running": False})
    except Exception:  # noqa: BLE001
        pass


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
    stream._IN_WORKER = True
    readers: dict = {}

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
    from crawler import fetch_policy
    while not parent_gone.is_set():
        try:
            cfg = stream.settings()
            fetch_policy.apply_settings(cfg)              # the admin's choice, within seconds
            try:
                mt = os.path.getmtime(path)
            except OSError:
                mt = None
            if cfg["enabled"] and not s.running:
                s.start(log)
            elif not cfg["enabled"] and s.running:
                s.stop()
            elif s.running and last_mt is not None and mt != last_mt:
                if any(prev.get(k) != cfg.get(k) for k in ("threads", "discovery_threads", "reader_processes")):
                    s.stop()
                    s.start(log)
                else:
                    s.cfg, s.kw_cache = cfg, (0.0, [])
                if prev.get("pause_international") != cfg.get("pause_international"):
                    from crawler import intl
                    intl.apply_pause(s)                   # international outlets paused / back (within seconds)
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
                elif cmd == "catchup_domains":
                    a = json.loads(arg or "{}")
                    threading.Thread(target=stream._quiet, args=(s.run_catchup, int(a.get("days") or 1),
                                                                 list(a.get("domains") or [])), daemon=True).start()
                elif cmd == "classify":
                    from crawler import intl
                    threading.Thread(target=stream._quiet, args=(intl.classify, s), daemon=True).start()
                elif cmd == "archive_export":
                    from crawler import backfill
                    threading.Thread(target=stream._quiet, args=(backfill.export, s, arg, OUTPUT_DIR), daemon=True).start()
            _manage_readers(stream, s, readers)
            stream.write_live(s.live_state())
        except Exception as e:  # noqa: BLE001  the loop keeps going; the error shows on the dashboard
            s.last_error = f"worker: {str(e)[:150]}"
        parent_gone.wait(2)
    s.stop()
    for p, _n in readers.values():
        try:
            p.stdin.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--reader":
        reader_main(int(sys.argv[2]), int(sys.argv[3]))
    else:
        main()
