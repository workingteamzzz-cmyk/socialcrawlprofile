"""Background jobs, one at a time:
- 'login'    open the platforms' login pages in the saved browser profile
- 'profiles' collect post links from selected profiles
- 'keywords' collect post links from the platforms' search for keywords
Results of the last profile run and the last keyword run are kept side by side."""
from __future__ import annotations

import os
import random
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from .accounts import PLATFORM_LABEL, PLATFORMS, parse_accounts
from .browser import BROWSER_CLOSED, LOGIN_PAGES, BrowserCrawler, Result, Stopped, launch_context
from .details import read_details
from .output import write_keyword_outputs, write_news_outputs, write_outputs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(ROOT, "browser_profile")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR") or os.path.join(ROOT, "outputs")

TIMEZONES = {"WIB": 7, "WITA": 8, "WIT": 9, "UTC": 0}
MODES = ("profiles", "keywords", "news")


def _bucket():
    return {"links": [], "statuses": [], "period": None, "files": [], "version": 0, "meta": {}}


def _period(start: str, end: str):
    try:
        d_start = datetime.strptime(start, "%Y-%m-%d")
        d_end = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return None, None, "Invalid date."
    if d_end < d_start:
        return None, None, "The end date is before the start date."
    return d_start, d_end, None


def _utc_range(d_start, d_end, tz):
    start_utc = d_start.replace(tzinfo=tz).astimezone(timezone.utc)
    end_utc = (d_end.replace(tzinfo=tz) + timedelta(days=1) - timedelta(microseconds=1)).astimezone(timezone.utc)
    return start_utc, end_utc


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.data = {m: _bucket() for m in MODES}
        self.kind = "idle"
        self.running = False
        self.logs: list[str] = []
        self.done = self.total = 0
        self.current = ""
        self.error = ""
        self.job_id = 0
        self.log_total = 0            # lines ever logged in this job (the kept list is capped)

    # ---------- shared ----------
    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001  (e.g. a console that cannot show emoji)
            pass
        with self.lock:
            self.logs.append(line)
            self.log_total += 1
            if len(self.logs) > 3000:
                self.logs = self.logs[-3000:]

    def state(self, since: int = 0) -> dict:
        with self.lock:
            return {"kind": self.kind, "running": self.running, "done": self.done, "total": self.total,
                    "current": self.current, "error": self.error, "job_id": self.job_id,
                    "logs": self.logs[max(0, since - (self.log_total - len(self.logs))):] if since < self.log_total else [],
                    "log_count": self.log_total,
                    "modes": {m: {"count": len(b["links"]), "files": list(b["files"]), "period": b["period"],
                                  "version": b["version"]} for m, b in self.data.items()}}

    def results_view(self, mode: str) -> dict:
        b = self.data.get(mode) or _bucket()
        with self.lock:
            return {"links": list(b["links"]), "statuses": list(b["statuses"]), "period": b["period"], "meta": b["meta"]}

    def _start(self, kind, target, *args) -> str | None:
        with self.lock:
            if self.running:
                return ("A job is already running. Wait for it to finish, click Stop, or — in login mode — "
                        "close the login browser window.")
            self.running = True
            self.job_id += 1
            self.kind = kind
            self.logs, self.log_total, self.done, self.total, self.current, self.error = [], 0, 0, 0, "", ""
            self.__dict__.pop("_save_warned", None)
            if kind in MODES:
                self.data[kind] = _bucket()
            self.stop_flag.clear()

        def wrap():
            try:
                target(*args)
            except Exception as e:  # noqa: BLE001
                first = str(e).splitlines()[0] if str(e) else e.__class__.__name__
                self.error = first[:300]
                self.log(f"❌ Error: {first}")
                traceback.print_exc()
            finally:
                self.running = False
                self.current = ""
        self.thread = threading.Thread(target=wrap, daemon=True)
        self.thread.start()
        return None

    def stop(self):
        self.stop_flag.set()

    def _pause_between(self):
        t_end = time.time() + random.uniform(3, 7)
        while time.time() < t_end and not self.stop_flag.is_set():
            time.sleep(0.25)

    def _fill_details(self, crawler, platform: str, posts, label: str) -> bool:
        """Open each post and read caption + engagement. Returns True if the user pressed Stop."""
        for j, p in enumerate(posts):
            if self.stop_flag.is_set():
                return True
            self.current = f"{label} — details {j + 1}/{len(posts)}"
            try:
                p.details = read_details(crawler, platform, p.url)
            except Stopped:
                return True
            except Exception as e:  # noqa: BLE001
                if BROWSER_CLOSED in str(e):
                    raise
                p.details = {"error": str(e).splitlines()[0][:120]}
        return False

    def _safe_save(self, fn, path) -> bool:
        """Write the result files; if Excel has one open (locked on Windows), keep going and retry later."""
        try:
            fn()
            self.__dict__.pop("_save_warned", None)
            return True
        except OSError as e:
            if not self.__dict__.get("_save_warned"):
                self.log(f"⚠ Could not save {os.path.basename(path)} ({e.__class__.__name__}) — is it open in Excel? "
                         "Close it; saving is retried after the next account.")
                self._save_warned = True
            return False

    def _finish_files(self, save, paths: list, anything: bool) -> list:
        """Last save of a run; returns the file names to offer for download.
        Nothing is written for a run that never got going (e.g. the browser could not open).
        If the files are locked (open in Excel), the results go to a *_copy file instead."""
        if not anything:
            return []
        if save():
            return [os.path.basename(p) for p in paths]
        alt = [p.replace(".xlsx", "_copy.xlsx").replace(".csv", "_copy.csv") for p in paths]
        if save(alt):
            self.log(f"⚠ The result file was locked (open in Excel?) — saved as {os.path.basename(alt[0])} instead.")
            return [os.path.basename(p) for p in alt]
        self.error = "Could not save the result files — close them in Excel. The links are still shown in the dashboard."
        self.log("❌ " + self.error)
        return []

    def _check_or_raise(self):
        if self.stop_flag.is_set():
            raise Stopped()

    @staticmethod
    def _close(ctx, pw):
        for obj, fn in ((ctx, "close"), (pw, "stop")):
            try:
                if obj:
                    getattr(obj, fn)()
            except Exception:  # noqa: BLE001
                pass

    # ---------- login ----------
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

    # ---------- profiles ----------
    def start_crawl(self, text: str, start: str, end: str, tz_name: str, platforms: list[str], detail: str = "links"):
        d_start, d_end, err = _period(start, end)
        if err:
            return err
        platforms = [p for p in platforms if p in PLATFORMS] or PLATFORMS
        accounts, bad = parse_accounts(text, platforms)
        if not accounts:
            return "No recognised accounts selected."
        return self._start("profiles", self._crawl, accounts, bad, d_start, d_end, tz_name, detail == "full")

    def _crawl(self, accounts, bad, d_start, d_end, tz_name, with_details=False):
        tz_hours = TIMEZONES.get(tz_name, 7)
        tz = timezone(timedelta(hours=tz_hours))
        start_utc, end_utc = _utc_range(d_start, d_end, tz)
        b = self.data["profiles"]
        order = {p: i for i, p in enumerate(PLATFORMS)}
        accounts.sort(key=lambda a: order[a.platform])
        self.total = len(accounts)
        b["period"] = {"start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}", "tz": tz_name}
        for line in bad:
            self.log(f"⚠ Unrecognised line skipped: {line}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        base = f"post_links_{d_start:%Y%m%d}-{d_end:%Y%m%d}_{datetime.now():%Y%m%d_%H%M%S}"
        xlsx, csvp = (os.path.join(OUTPUT_DIR, base + ext) for ext in (".xlsx", ".csv"))
        meta = {"tz_hours": tz_hours, "tz_name": tz_name, "start": f"{d_start:%Y-%m-%d}",
                "end": f"{d_end:%Y-%m-%d}", "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "details": with_details}
        b["meta"] = {"details": with_details}
        links: list[dict] = []
        statuses: list[dict] = []

        def record(acc, res: Result):
            with self.lock:
                for p in res.posts:
                    t = p.time_utc.astimezone(tz) if p.time_utc else None
                    b["links"].append({"platform": acc.platform, "group": acc.label or acc.handle,
                                       "handle": acc.handle, "url": p.url,
                                       "date": t.strftime("%Y-%m-%d") if t else "",
                                       "time": t.strftime("%H:%M") if t else "", **(p.details or {})})
                b["statuses"].append({"platform": acc.platform, "group": acc.label or acc.handle,
                                      "handle": acc.handle, "url": acc.url, "status": res.status,
                                      "count": len(res.posts), "note": res.note})
                b["version"] += 1
            for p in res.posts:
                links.append({"order": order[acc.platform], "platform_label": PLATFORM_LABEL[acc.platform],
                              "handle": acc.handle, "label": acc.label, "url": p.url, "time": p.time_utc,
                              **(p.details or {})})
            statuses.append({"platform_label": PLATFORM_LABEL[acc.platform], "handle": acc.handle, "label": acc.label,
                             "url": acc.url, "status": res.status, "count": len(res.posts), "note": res.note})

        def save(paths=None):
            x, c = paths or (xlsx, csvp)
            return self._safe_save(lambda: write_outputs(x, c, links, statuses, meta), x)

        self.log(f"Period {d_start:%Y-%m-%d} to {d_end:%Y-%m-%d} ({tz_name}) — {len(accounts)} accounts")
        need_browser = any(not a.problem for a in accounts)
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
                self.current = label + (f" — {acc.label}" if acc.label else "")
                try:
                    res = getattr(crawler, acc.platform)(acc)
                    if with_details and res.posts and self._fill_details(crawler, acc.platform, res.posts, label):
                        stopped = True
                        res.note = (res.note + "; " if res.note else "") + "stopped while reading details"
                except Stopped as e:
                    stopped = True
                    part = crawler._in_range(e.posts) if crawler else []
                    record(acc, Result("stopped", part, f"Stopped before finishing — {len(part)} links collected so far kept"))
                    break
                except Exception as e:  # noqa: BLE001
                    if BROWSER_CLOSED in str(e):
                        stopped = True
                        self.error = "The browser window was closed, so the collection stopped. Results so far are saved."
                        self.log("⏹ " + self.error)
                        record(acc, Result("stopped", [], "Browser window was closed"))
                        break
                    res = Result("error", [], str(e).splitlines()[0][:200])
                record(acc, res)
                self.log(f"   {'✅' if res.status == 'ok' else '⚠'} {len(res.posts)} links" + (f" — {res.note}" if res.note else ""))
                self.done = i + 1
                save()
                if stopped:
                    break
                if i + 1 < len(accounts):
                    self._pause_between()
        finally:
            done_keys = {(s["platform_label"], s["handle"]) for s in statuses}
            for acc in accounts:
                if (PLATFORM_LABEL[acc.platform], acc.handle) not in done_keys:
                    statuses.append({"platform_label": PLATFORM_LABEL[acc.platform], "handle": acc.handle, "label": acc.label,
                                     "url": acc.url, "status": "skipped", "count": 0, "note": ""})
            files = self._finish_files(save, [xlsx, csvp], any(st["status"] != "skipped" for st in statuses))
            self._close(ctx, pw)
        b["files"] = files
        self.log(("⏹ Stopped. " if stopped else "✅ Done. ") + f"Total {len(links)} links" + (f" → {files[0]}" if files else ""))

    # ---------- keywords ----------
    def start_keywords(self, keywords: list[str], start: str, end: str, tz_name: str,
                       platforms: list[str], options: dict):
        from .keywords import KEYWORD_PLATFORMS, SearchOptions
        d_start, d_end, err = _period(start, end)
        if err:
            return err
        kws = []
        for k in keywords:
            k = " ".join(str(k).split())[:100]
            if k and k.lower() not in [x.lower() for x in kws]:
                kws.append(k)
        if not kws:
            return "Enter at least one keyword."
        if len(kws) > 50:
            return "Up to 50 keywords per run."
        plats = [p for p in KEYWORD_PLATFORMS if p in platforms]
        if not plats:
            return "Choose at least one platform."
        try:
            opt = SearchOptions.from_dict(options)
        except (TypeError, ValueError):
            return "Invalid search options."
        return self._start("keywords", self._keywords, kws, plats, opt, d_start, d_end, tz_name)

    def _keywords(self, kws, plats, opt, d_start, d_end, tz_name):
        from .keywords import KeywordCrawler
        tz_hours = TIMEZONES.get(tz_name, 7)
        tz = timezone(timedelta(hours=tz_hours))
        start_utc, end_utc = _utc_range(d_start, d_end, tz)
        b = self.data["keywords"]
        b["period"] = {"start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}", "tz": tz_name}
        b["meta"] = {"keywords": kws, "platforms": plats, "cap": opt.cap, "details": opt.detail == "full"}
        tasks = [(k, p) for k in kws for p in plats]
        self.total = len(tasks)

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        base = f"keyword_links_{d_start:%Y%m%d}-{d_end:%Y%m%d}_{datetime.now():%Y%m%d_%H%M%S}"
        xlsx, csvp = (os.path.join(OUTPUT_DIR, base + ext) for ext in (".xlsx", ".csv"))
        meta = {"tz_hours": tz_hours, "tz_name": tz_name, "start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}",
                "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "keywords": kws, "options": opt}
        seen: dict[str, dict] = {}   # url -> row (one row per link; keywords merged)
        statuses: list[dict] = []

        def record(kw, plat, res: Result):
            with self.lock:
                for p in res.posts:
                    row = seen.get(p.url)
                    if row:
                        if kw not in row["keywords"]:
                            row["keywords"].append(kw)
                            row["keyword"] = ", ".join(row["keywords"])
                        continue
                    t = p.time_utc.astimezone(tz) if p.time_utc else None
                    row = {"keyword": kw, "keywords": [kw], "platform": plat, "author": p.author, "url": p.url,
                           "date": t.strftime("%Y-%m-%d") if t else "", "time": t.strftime("%H:%M") if t else "",
                           "ts": p.time_utc.timestamp() if p.time_utc else 0, **(p.details or {})}
                    seen[p.url] = row
                    b["links"].append(row)
                st = {"keyword": kw, "platform": plat, "status": res.status, "count": len(res.posts), "note": res.note}
                statuses.append(st)
                b["statuses"].append(st)
                b["version"] += 1

        def save(paths=None):
            x, c = paths or (xlsx, csvp)
            return self._safe_save(lambda: write_keyword_outputs(x, c, list(seen.values()), statuses, meta), x)

        self.log(f"Keyword search: {', '.join(kws)} — {len(plats)} platforms — period {d_start:%Y-%m-%d} to {d_end:%Y-%m-%d} ({tz_name})")
        pw = ctx = crawler = None
        stopped = False
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            ctx = launch_context(pw, PROFILE_DIR)
            crawler = KeywordCrawler(ctx, self.log, self.stop_flag, start_utc, end_utc)
            for i, (kw, plat) in enumerate(tasks):
                if self.stop_flag.is_set():
                    stopped = True
                    break
                label = f"{PLATFORM_LABEL[plat]} · “{kw}”"
                self.log(f"({i + 1}/{len(tasks)}) {label} ...")
                self.current = label
                try:
                    res = getattr(crawler, f"search_{plat}")(kw, opt)
                    if opt.detail == "full" and res.posts:
                        fresh = [p for p in res.posts if p.url not in seen]     # skip links already read for another keyword
                        if self._fill_details(crawler, plat, fresh, label):
                            stopped = True
                            res.note = (res.note + "; " if res.note else "") + "stopped while reading details"
                except Stopped as e:
                    stopped = True
                    part = crawler._cap_trim(e.posts, opt.cap) if crawler else []
                    record(kw, plat, Result("stopped", part, f"Stopped before finishing — {len(part)} links collected so far kept"))
                    break
                except Exception as e:  # noqa: BLE001
                    if BROWSER_CLOSED in str(e):
                        stopped = True
                        self.error = "The browser window was closed, so the search stopped. Results so far are saved."
                        self.log("⏹ " + self.error)
                        record(kw, plat, Result("stopped", [], "Browser window was closed"))
                        break
                    res = Result("error", [], str(e).splitlines()[0][:200])
                record(kw, plat, res)
                self.log(f"   {'✅' if res.status == 'ok' else '⚠'} {len(res.posts)} links" + (f" — {res.note}" if res.note else ""))
                self.done = i + 1
                save()
                if stopped:
                    break
                if i + 1 < len(tasks):
                    self._pause_between()
        finally:
            files = self._finish_files(save, [xlsx, csvp], bool(statuses))
            self._close(ctx, pw)
        b["files"] = files
        self.log(("⏹ Stopped. " if stopped else "✅ Done. ") + f"Total {len(seen)} unique links" + (f" → {files[0]}" if files else ""))


    # ---------- news (Google) ----------
    def start_news(self, keywords: list[str], start: str, end: str, tz_name: str, options: dict):
        from .news import NewsOptions
        d_start, d_end, err = _period(start, end)
        if err:
            return err
        kws = []
        for k in keywords:
            k = " ".join(str(k).split())[:150]
            if k and k.lower() not in [x.lower() for x in kws]:
                kws.append(k)
        if not kws:
            return "Enter at least one keyword."
        if len(kws) > 50:
            return "Up to 50 keywords per run."
        try:
            opt = NewsOptions.from_dict(options)
        except (TypeError, ValueError):
            return "Invalid search options."
        return self._start("news", self._news, kws, opt, d_start, d_end, tz_name)

    def _news(self, kws, opt, d_start, d_end, tz_name):
        from .news import NewsCrawler
        tz = timezone(timedelta(hours=TIMEZONES.get(tz_name, 7)))
        start_utc, end_utc = _utc_range(d_start, d_end, tz)
        b = self.data["news"]
        b["period"] = {"start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}", "tz": tz_name}
        b["meta"] = {"keywords": kws, "detail": opt.detail, "cap": opt.cap}
        self.total = len(kws)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        base = f"news_links_{d_start:%Y%m%d}-{d_end:%Y%m%d}_{datetime.now():%Y%m%d_%H%M%S}"
        xlsx, csvp = (os.path.join(OUTPUT_DIR, base + ext) for ext in (".xlsx", ".csv"))
        meta = {"tz_name": tz_name, "start": f"{d_start:%Y-%m-%d}", "end": f"{d_end:%Y-%m-%d}",
                "created": datetime.now().strftime("%Y-%m-%d %H:%M"), "keywords": kws, "options": opt}
        seen: dict[str, dict] = {}
        statuses: list[dict] = []

        def save(paths=None):
            x, c = paths or (xlsx, csvp)
            return self._safe_save(lambda: write_news_outputs(x, c, list(seen.values()), statuses, meta), x)

        def record(kw, res):
            added = 0
            with self.lock:
                for r in res.posts:
                    row = seen.get(r["url"])
                    if row:
                        if kw not in row["keywords"]:
                            row["keywords"].append(kw)
                            row["keyword"] = ", ".join(row["keywords"])
                        continue
                    r.update({"keyword": kw, "keywords": [kw]})
                    seen[r["url"]] = r
                    b["links"].append(r)
                    added += 1
                st = {"keyword": kw, "status": res.status, "count": len(res.posts), "note": res.note}
                statuses.append(st)
                b["statuses"].append(st)
                b["version"] += 1
            return added

        self.log(f"News search (Google): {', '.join(kws)} — period {d_start:%Y-%m-%d} to {d_end:%Y-%m-%d} "
                 f"({tz_name}) — detail: {opt.detail}")
        pw = ctx = None
        stopped = False
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            ctx = launch_context(pw, PROFILE_DIR)
            crawler = NewsCrawler(ctx, self.log, self.stop_flag, start_utc, end_utc, tz=tz,
                                  debug_dir=os.path.join(OUTPUT_DIR, "debug"))
            for i, kw in enumerate(kws):
                if self.stop_flag.is_set():
                    stopped = True
                    break
                label = f"Google News · “{kw}”"
                self.log(f"({i + 1}/{len(kws)}) {label} ...")
                self.current = label
                res = Result("stopped", [], "Stopped before finishing")
                try:
                    res = crawler.search(kw, opt, d_start, d_end)
                    if opt.detail == "full":
                        new = [r for r in res.posts if r["url"] not in seen]
                        for j, r in enumerate(new):
                            self._check_or_raise()
                            self.current = f"{label} — article {j + 1}/{len(new)}"
                            try:
                                r.update({k: v for k, v in crawler.article(r["url"]).items() if v})   # keep Google's title if the page gives none
                            except Stopped:
                                raise
                            except Exception as e:  # noqa: BLE001
                                if BROWSER_CLOSED in str(e):
                                    raise
                                r["error"] = str(e).splitlines()[0][:120]
                except Stopped as e:
                    stopped = True
                    if e.posts and not res.posts:
                        res = Result("stopped", e.posts, "")
                    res.note = (res.note + "; " if res.note else "") + "stopped — links collected so far kept"
                except Exception as e:  # noqa: BLE001
                    if BROWSER_CLOSED in str(e):
                        stopped = True
                        self.error = "The browser window was closed, so the search stopped. Results so far are saved."
                        self.log("⏹ " + self.error)
                        record(kw, Result("stopped", res.posts if res.status == "ok" else [], "Browser window was closed"))
                        break
                    res = Result("error", [], str(e).splitlines()[0][:200])
                added = record(kw, res)
                self.log(f"   {'✅' if res.status == 'ok' else '⚠'} {len(res.posts)} links ({added} new)"
                         + (f" — {res.note}" if res.note else ""))
                self.done = i + 1
                save()
                if stopped:
                    break
                if i + 1 < len(kws):
                    self._pause_between()
        finally:
            files = self._finish_files(save, [xlsx, csvp], bool(statuses))
            self._close(ctx, pw)
        b["files"] = files
        self.log(("⏹ Stopped. " if stopped else "✅ Done. ") + f"Total {len(seen)} unique news links" + (f" → {files[0]}" if files else ""))


JOB = Job()
