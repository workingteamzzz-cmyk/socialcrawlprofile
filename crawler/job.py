"""Background jobs: 'login' (open login pages) and 'crawl' (collect links). One at a time."""
from __future__ import annotations

import os
import random
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .accounts import PLATFORM_LABEL, PLATFORMS, parse_accounts
from .browser import LOGIN_PAGES, BrowserCrawler, Result, Stopped, launch_context
from .output import write_outputs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(ROOT, "browser_profile")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR") or os.path.join(ROOT, "outputs")

TIMEZONES = {"WIB": 7, "WITA": 8, "WIT": 9, "UTC": 0}


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.results: list[dict] = []
        self.account_status: list[dict] = []
        self.period = None
        self.reset("idle")

    def reset(self, kind):
        self.kind = kind
        self.running = False
        self.logs: list[str] = []
        self.done = 0
        self.total = 0
        self.current = ""
        if kind in ("crawl", "idle"):
            self.files: list[str] = []
            self.links_count = 0
            self.results, self.account_status, self.period = [], [], None

    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with self.lock:
            self.logs.append(line)
            if len(self.logs) > 3000:
                self.logs = self.logs[-3000:]

    def state(self, since: int = 0) -> dict:
        with self.lock:
            return {"kind": self.kind, "running": self.running, "done": self.done, "total": self.total,
                    "links": self.links_count, "files": list(self.files), "current": self.current,
                    "period": self.period, "results_count": len(self.results) + 100000 * len(self.account_status),
                    "logs": self.logs[since:], "log_count": len(self.logs)}

    def results_view(self) -> dict:
        with self.lock:
            return {"links": list(self.results), "accounts": list(self.account_status), "period": self.period}

    def _start(self, kind, target, *args) -> str | None:
        if self.running:
            return "A job is already running. Wait for it to finish or click Stop."
        self.reset(kind)
        self.stop_flag.clear()
        self.running = True

        def wrap():
            try:
                target(*args)
            except Exception as e:  # noqa: BLE001
                self.log(f"❌ Error: {e}")
                traceback.print_exc()
            finally:
                self.running = False
        self.thread = threading.Thread(target=wrap, daemon=True)
        self.thread.start()
        return None

    def stop(self):
        self.stop_flag.set()

    # ---------------- login ----------------
    def start_login(self):
        return self._start("login", self._login)

    def _login(self):
        from playwright.sync_api import sync_playwright
        self.log("Opening the browser. Sign in on each tab (Instagram, X, TikTok, Threads, Facebook).")
        self.log("Done signing in? CLOSE the browser window — the sessions are saved for later runs.")
        with sync_playwright() as pw:
            ctx = launch_context(pw, PROFILE_DIR)
            first = ctx.pages[0] if ctx.pages else ctx.new_page()
            for i, url in enumerate(LOGIN_PAGES):
                page = first if i == 0 else ctx.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception:  # noqa: BLE001
                    pass
            while not self.stop_flag.is_set():
                try:
                    if not ctx.pages:
                        break
                    ctx.pages[0].wait_for_timeout(1000)
                except Exception:  # noqa: BLE001
                    break
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self.log("✅ Browser closed. Login sessions saved.")

    # ---------------- crawl ----------------
    def start_crawl(self, text: str, start: str, end: str, tz_name: str, platforms: list[str]):
        try:
            d_start = datetime.strptime(start, "%Y-%m-%d")
            d_end = datetime.strptime(end, "%Y-%m-%d")
        except ValueError:
            return "Invalid date."
        if d_end < d_start:
            return "The end date is before the start date."
        platforms = [p for p in platforms if p in PLATFORMS] or PLATFORMS
        accounts, bad = parse_accounts(text, platforms)
        if not accounts:
            return "No recognised accounts selected."
        return self._start("crawl", self._crawl, accounts, bad, d_start, d_end, tz_name)

    def _crawl(self, accounts, bad, d_start, d_end, tz_name):
        tz_hours = TIMEZONES.get(tz_name, 7)
        tz = timezone(timedelta(hours=tz_hours))
        start_utc = d_start.replace(tzinfo=tz).astimezone(timezone.utc)
        end_utc = (d_end.replace(tzinfo=tz) + timedelta(days=1) - timedelta(microseconds=1)).astimezone(timezone.utc)

        order = {p: i for i, p in enumerate(PLATFORMS)}
        accounts.sort(key=lambda a: order[a.platform])
        self.total = len(accounts)
        self.period = {"start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}", "tz": tz_name}
        for line in bad:
            self.log(f"⚠ Unrecognised line skipped: {line}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"post_links_{d_start:%Y%m%d}-{d_end:%Y%m%d}_{stamp}"
        xlsx = os.path.join(OUTPUT_DIR, base + ".xlsx")
        csvp = os.path.join(OUTPUT_DIR, base + ".csv")
        meta = {"tz_hours": tz_hours, "tz_name": tz_name, "start": f"{d_start:%Y-%m-%d}",
                "end": f"{d_end:%Y-%m-%d}", "created": datetime.now().strftime("%Y-%m-%d %H:%M")}

        links: list[dict] = []
        statuses: list[dict] = []

        def record(acc, res: Result):
            with self.lock:
                for p in res.posts:
                    t = p.time_utc.astimezone(tz) if p.time_utc else None
                    self.results.append({"platform": acc.platform, "unit": acc.label or acc.handle,
                                         "handle": acc.handle, "url": p.url,
                                         "date": t.strftime("%Y-%m-%d") if t else "",
                                         "time": t.strftime("%H:%M") if t else ""})
                self.account_status.append({"platform": acc.platform, "unit": acc.label or acc.handle,
                                            "handle": acc.handle, "url": acc.url, "status": res.status,
                                            "count": len(res.posts), "note": res.note})
            for p in res.posts:
                links.append({"order": order[acc.platform], "platform_label": PLATFORM_LABEL[acc.platform],
                              "handle": acc.handle, "label": acc.label, "url": p.url, "time": p.time_utc})
            statuses.append({"platform_label": PLATFORM_LABEL[acc.platform], "handle": acc.handle, "label": acc.label,
                             "url": acc.url, "status": res.status, "count": len(res.posts), "note": res.note})
            self.links_count = len(links)

        def save():
            write_outputs(xlsx, csvp, links, statuses, meta)

        self.log(f"Period {d_start:%Y-%m-%d} to {d_end:%Y-%m-%d} ({tz_name}) — {len(accounts)} accounts")
        need_browser = any(a.platform != "youtube" and not a.problem for a in accounts)
        pw = ctx = crawler = None
        stopped = False
        try:
            if need_browser:
                from playwright.sync_api import sync_playwright
                pw = sync_playwright().start()
                ctx = launch_context(pw, PROFILE_DIR)
                crawler = BrowserCrawler(ctx, self.log, self.stop_flag, start_utc, end_utc)
            for i, acc in enumerate(accounts):
                if self.stop_flag.is_set():
                    stopped = True
                    break
                label = f"{PLATFORM_LABEL[acc.platform]} @{acc.handle}"
                if acc.problem:
                    self.log(f"({i + 1}/{len(accounts)}) {label} — skipped: {acc.problem}")
                    record(acc, Result("invalid", [], acc.problem))
                    self.done = i + 1
                    continue
                self.log(f"({i + 1}/{len(accounts)}) {label} ...")
                self.current = f"{label}" + (f" — {acc.label}" if acc.label else "")
                try:
                    if acc.platform == "youtube":
                        from .youtube import crawl_youtube
                        res = crawl_youtube(acc, start_utc, end_utc, self.log, self.stop_flag)
                    else:
                        res = getattr(crawler, acc.platform)(acc)
                except Stopped:
                    stopped = True
                    record(acc, Result("stopped", [], "Stopped before finishing"))
                    break
                except Exception as e:  # noqa: BLE001
                    res = Result("error", [], str(e).splitlines()[0][:200])
                record(acc, res)
                extra = f" — {res.note}" if res.note else ""
                icon = "✅" if res.status == "ok" else "⚠"
                self.log(f"   {icon} {len(res.posts)} links{extra}")
                self.done = i + 1
                save()
                if acc.platform != "youtube" and i + 1 < len(accounts):
                    t_end = time.time() + random.uniform(3, 7)
                    while time.time() < t_end and not self.stop_flag.is_set():
                        time.sleep(0.25)
        finally:
            done_keys = {(s["platform_label"], s["handle"]) for s in statuses}
            for acc in accounts:
                if (PLATFORM_LABEL[acc.platform], acc.handle) not in done_keys:
                    statuses.append({"platform_label": PLATFORM_LABEL[acc.platform], "handle": acc.handle, "label": acc.label,
                                     "url": acc.url, "status": "skipped", "count": 0, "note": ""})
            save()
            for closer in (ctx, pw):
                try:
                    if closer is ctx and ctx:
                        ctx.close()
                    elif closer is pw and pw:
                        pw.stop()
                except Exception:  # noqa: BLE001
                    pass
        self.current = ""
        self.files = [os.path.basename(xlsx), os.path.basename(csvp)]
        self.log(("⏹ Stopped. " if stopped else "✅ Done. ") + f"Total {len(links)} links → {os.path.basename(xlsx)}")


JOB = Job()
