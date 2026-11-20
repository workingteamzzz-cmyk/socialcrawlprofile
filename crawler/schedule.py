"""Scheduled searches: set a search up once, the app runs it by itself (e.g. news for "yesterday" every day at 07:00).

A schedule keeps what the Start button would send (keywords, platforms, options, account list) plus when to run
and which period to collect at that moment. One job runs at a time: a schedule that comes due while another job is
running waits for it. Runs that were missed while the app was off are run once when it comes back.
Each run writes its own Excel file (Exports); NOTIFY_WEBHOOK_URL (or CAPTCHA_WEBHOOK_URL) is told when one finishes.

Automation / delivery to another server (a client's API, n8n, Make, Zapier...): a schedule can have its own `webhook`
(+ `webhook_token`). When a run finishes, that URL receives the whole result as JSON (see delivery.py), retried if
the server is down.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

KINDS = ("news", "keywords", "profiles", "apiscrape")
# period collected at run time: (first day, last day) relative to the run's day
WINDOWS = {"since_last": None, "yesterday": (-1, -1), "today": (0, 0), "yesterday_today": (-1, 0),
           "last_7_days": (-7, -1)}
WINDOW_LABEL = {"since_last": "everything since the last run", "yesterday": "yesterday", "today": "today",
                "yesterday_today": "yesterday + today", "last_7_days": "the 7 days before"}
AUTO_MIN_H, AUTO_MAX_H = 1, 12          # "automatic" schedules: how often, at the most and at the least
SINCE_MAX_DAYS = 31                     # "since the last run" never reaches back further than this
HISTORY = 12                            # runs remembered per schedule (dashboard sparkline, automatic pace)
TIMEZONES = {"WIB": 7, "WITA": 8, "WIT": 9, "UTC": 0}
MAX_SCHEDULES = 50


class ScheduleError(ValueError):
    pass


def _tz(name: str) -> timezone:
    return timezone(timedelta(hours=TIMEZONES.get(name, 7)))


def next_run(s: dict, after: float) -> float:
    """The first run time strictly after `after` (epoch seconds)."""
    tz = _tz(s.get("payload", {}).get("tz", "WIB"))
    if s["repeat"] == "daily":
        hh, mm = (int(x) for x in s["time"].split(":"))
        t = datetime.fromtimestamp(after, tz).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t.timestamp() <= after:
            t += timedelta(days=1)
        return t.timestamp()
    if s["repeat"] == "auto":
        return after + float(s.get("auto_hours") or s.get("hours") or 2) * 3600
    return after + int(s["hours"]) * 3600


def since(s: dict, at: float) -> float:
    """'Since the last run': where this run starts — the start of the last run that finished well (a few minutes
    of overlap; what is already known is skipped anyway), else one day back; never more than SINCE_MAX_DAYS."""
    last = float(s.get("last_ok") or 0)
    start = (last - 600) if last else (at - 86400)
    return max(start, at - SINCE_MAX_DAYS * 86400)


def period(s: dict, at: float) -> tuple[str, str]:
    """(start, end) dates, YYYY-MM-DD in the schedule's time zone, for a run at `at`."""
    tz = _tz(s.get("payload", {}).get("tz", "WIB"))
    day = datetime.fromtimestamp(at, tz).date()
    if s["window"] == "since_last":
        return datetime.fromtimestamp(since(s, at), tz).date().isoformat(), day.isoformat()
    a, b = WINDOWS[s["window"]]
    return (day + timedelta(days=a)).isoformat(), (day + timedelta(days=b)).isoformat()


def describe(s: dict) -> str:
    if s["repeat"] == "auto":
        h = float(s.get("auto_hours") or s.get("hours") or 2)
        when = f"automatic — now every {h:g} hour{'s' if h != 1 else ''}, faster when much is new"
    else:
        when = f"every day at {s['time']}" if s["repeat"] == "daily" else \
            ("every hour" if int(s["hours"]) == 1 else f"every {s['hours']} hours")
    if s.get("new_only"):
        when += ", only new results"
    return f"{when} ({s.get('payload', {}).get('tz', 'WIB')}), collecting {WINDOW_LABEL[s['window']]}"


def clean(data: dict) -> dict:
    """Validate a schedule sent by the dashboard/API. Raises ScheduleError with a readable message."""
    kind = data.get("kind")
    if kind not in KINDS:
        raise ScheduleError("Choose what to run: news, keywords, profiles or the API scraper.")
    payload = dict(data.get("payload") or {})
    payload.pop("start", None)
    payload.pop("end", None)
    if kind in ("news", "keywords") and not [k for k in payload.get("keywords") or [] if str(k).strip()]:
        raise ScheduleError("Add at least one keyword.")
    if kind == "profiles" and not (payload.get("ids") or str(payload.get("text") or "").strip()):
        raise ScheduleError("Select at least one account.")
    if kind == "apiscrape":
        cfg = payload.get("cfg") or {}
        if not str(cfg.get("queries") or "").strip() and not str(cfg.get("import") or "").strip():
            raise ScheduleError("Type at least one keyword, post link or profile link.")
        if not int(cfg.get("budget") or 0):
            raise ScheduleError("A scheduled API run needs a credit budget per run (Max credits), so it never spends "
                                "more than you planned.")
    payload["tz"] = payload.get("tz") if payload.get("tz") in TIMEZONES else "WIB"
    repeat = data.get("repeat")
    s = {"kind": kind, "payload": payload, "repeat": repeat, "window": data.get("window"),
         "name": str(data.get("name") or "").strip()[:80], "enabled": bool(data.get("enabled", True)),
         "webhook": str(data.get("webhook") or "").strip()[:500],
         "webhook_token": str(data.get("webhook_token") or "").strip()[:500],
         "new_only": bool(data.get("new_only", False))}
    if s["webhook"] and not re.fullmatch(r"https?://[^\s]+", s["webhook"]):
        raise ScheduleError("The results webhook must be a web address starting with https:// (e.g. an n8n Webhook URL).")
    if repeat == "daily":
        t = str(data.get("time") or "")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ScheduleError("Give the time as HH:MM, e.g. 07:00.")
        s["time"] = t
    elif repeat == "auto":
        from . import safety
        least = safety.min_schedule_hours(kind, payload) or AUTO_MIN_H
        try:
            h = int(data.get("hours") or 2)
        except (TypeError, ValueError):
            h = 2
        s["hours"] = max(least, min(AUTO_MAX_H, h))          # where the pace starts
    elif repeat == "hours":
        try:
            h = int(data.get("hours"))
        except (TypeError, ValueError):
            h = 0
        if not 1 <= h <= 24:
            raise ScheduleError("Repeat every 1 to 24 hours.")
        from . import safety
        least = safety.min_schedule_hours(kind, payload)
        if least and h < least:
            raise ScheduleError(f"Safe mode: searches with the social media accounts or on Google's search pages run "
                                f"at most every {least} hours (so the accounts and this connection are not blocked). "
                                "Choose 6 hours or more — or, for news, the Multi-source engine, which may run hourly.")
        s["hours"] = h
    else:
        raise ScheduleError("Choose how often: daily, every few hours or automatic.")
    if s["window"] not in WINDOWS:
        raise ScheduleError("Choose the period to collect.")
    if not s["name"]:
        what = ", ".join(payload.get("keywords") or [])[:40] or \
            " ".join(str((payload.get("cfg") or {}).get("queries") or "").split())[:40] or "accounts"
        s["name"] = f"{'API scraper' if kind == 'apiscrape' else kind.capitalize()}: {what}"
    return s


def pace(s: dict, new: int) -> float:
    """Automatic schedules: much new → run sooner (down to the safe minimum), nothing new → later (up to 12 h).
    'Much' is judged against the schedule's own recent runs, so a busy keyword and a quiet one each find a pace."""
    from . import safety
    least = max(AUTO_MIN_H, safety.min_schedule_hours(s["kind"], s.get("payload") or {}) or 0)
    h = float(s.get("auto_hours") or s.get("hours") or 2)
    past = [x.get("new") or 0 for x in (s.get("history") or [])[:-1]][-6:]
    usual = (sum(past) / len(past)) if past else 0
    if new == 0:
        h *= 2
    elif new >= max(20, 2 * usual):          # a surge (a spike in the news): check far more often
        h /= 2
    elif new < max(3, usual / 2):
        h *= 1.5
    elif new > usual * 1.3:
        h /= 1.5
    h = max(least, min(AUTO_MAX_H, h))
    return round(h * 2) / 2                  # half hours


class Scheduler:
    def __init__(self, path: str, job, notify=None, audit=None):
        self.path = path
        self.job = job
        self.notify = notify or (lambda text: None)
        self.audit = audit or (lambda *a, **k: None)
        self.lock = threading.Lock()
        self.items: list[dict] = self._load()
        self.running_id = None          # (schedule id, job id) of the run in progress
        self._thread = None

    # ---------- storage ----------
    def _load(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return [s for s in data if isinstance(s, dict) and s.get("id")]
        except (OSError, ValueError):
            return []

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- dashboard / API ----------
    def list(self) -> list[dict]:
        with self.lock:
            out = []
            for s in self.items:
                d = dict(s)
                d["describe"] = describe(s)
                d["running"] = bool(self.running_id and self.running_id[0] == s["id"])
                d["webhook_token"] = "••••••" if s.get("webhook_token") else ""     # never sent back out
                out.append(d)
            return out

    def save(self, data: dict) -> dict:
        s = clean(data)
        with self.lock:
            now = time.time()
            if data.get("id"):
                old = next((x for x in self.items if x["id"] == data["id"]), None)
                if not old:
                    raise ScheduleError("That schedule no longer exists.")
                if s["webhook_token"] == "••••••":
                    s["webhook_token"] = old.get("webhook_token", "")       # unchanged (the list only shows dots)
                old.update(s)
                old["next_run"] = next_run(old, now)
                self._save()
                return dict(old)
            if len(self.items) >= MAX_SCHEDULES:
                raise ScheduleError(f"At most {MAX_SCHEDULES} schedules.")
            s.update({"id": uuid.uuid4().hex[:12], "created": now, "last_run": None, "last_result": ""})
            s["next_run"] = next_run(s, now)
            self.items.append(s)
            self._save()
            return dict(s)

    def delete(self, sid: str) -> bool:
        with self.lock:
            n = len(self.items)
            self.items = [s for s in self.items if s["id"] != sid]
            if len(self.items) != n:
                self._save()
                return True
            return False

    def run_now(self, sid: str) -> str | None:
        with self.lock:
            s = next((x for x in self.items if x["id"] == sid), None)
        if not s:
            return "That schedule no longer exists."
        return self._run(s, time.time(), manual=True)

    # ---------- running ----------
    def _run(self, s: dict, now: float, manual: bool = False) -> str | None:
        start, end = period(s, now)
        p = s["payload"]
        from . import safety
        least = safety.min_schedule_hours(s["kind"], p)
        if (not manual and least and s.get("repeat") == "hours" and int(s.get("hours", 24)) < least
                and s.get("last_run") and now - s["last_run"] < least * 3600):
            with self.lock:                                   # an older, too-frequent schedule: safe mode spaces it out
                s["next_run"] = s["last_run"] + least * 3600
                s["last_result"] = f"postponed by safe mode (at most every {least} h)"
                self._save()
            return None
        self.job._next_origin = "Manual" if manual else "Auto"         # file names: 20261108_Online_Auto_24.00
        if s.get("new_only"):
            self.job._next_new_only = True                              # each file: only what is new since last run
        smart = s.get("window") == "since_last" or bool(s.get("new_only"))
        since_txt = datetime.fromtimestamp(since(s, now), _tz(p["tz"])).strftime("%Y-%m-%d %H:%M") \
            if s.get("window") == "since_last" else ""
        self.job._next_smart = smart                                    # skip / stop at what is already known
        if s["kind"] == "news":
            opts = dict(p.get("options") or {})
            if s.get("new_only"):
                opts["new_only"] = True                                 # the multi-source engine's own memory too
            err = self.job.start_news(p.get("keywords") or [], start, end, p["tz"], opts)
        elif s["kind"] == "keywords":
            opts = dict(p.get("options") or {})
            if smart:
                opts["stop_known"] = True                               # stop scrolling at what earlier runs have
            if since_txt:
                opts["since"] = since_txt                               # the API's time filter: to the minute
            err = self.job.start_keywords(p.get("keywords") or [], start, end, p["tz"], p.get("platforms") or [],
                                          opts)
        elif s["kind"] == "apiscrape":
            cfg = dict(p.get("cfg") or {})
            if cfg.get("mode", "keyword") == "keyword":          # the schedule's period becomes the time filter
                cfg["time_from"], cfg["time_to"] = (since_txt or f"{start} 00:00"), f"{end} 23:55"
            if s.get("new_only"):
                cfg["delta"] = True                             # the engine skips posts it already delivered
            if smart:
                cfg["skip_known"] = True                        # known posts: no paid look-ups, a known page ends paging
            err = self.job.start_apiscrape(cfg)
        else:
            from . import store
            text = store.lines_for(p.get("list", ""), p["ids"]) if p.get("ids") else p.get("text", "")
            err = self.job.start_crawl(text, start, end, p["tz"], p.get("platforms") or [], p.get("detail", "links"),
                                       p.get("template", "standard"))
        if err:
            self.job.__dict__.pop("_next_origin", None)
            self.job.__dict__.pop("_next_new_only", None)
            self.job.__dict__.pop("_next_smart", None)
            return err                                       # a scheduled one stays due and is tried again
        with self.lock:
            self.running_id = (s["id"], self.job.job_id, s["kind"], start, end)
            s["last_run"] = now
            s["last_result"] = "running…"
            if not manual:
                s["next_run"] = next_run(s, now)
            self._save()
        self.job.log(f"⏰ Schedule “{s['name']}” started — "
                     + (f"since the last run ({since_txt})" if since_txt else f"period {start} to {end}")
                     + (" · already-collected results are skipped" if smart else ""))
        self.audit("schedule", "started a scheduled run", name=s["name"], period=f"{start}..{end}")
        return None

    def _check_finished(self):
        with self.lock:
            rid = self.running_id
        if not rid or self.job.running:
            return
        sid, job_id, kind, start, end = rid
        st = self.job.state()
        mode = (st.get("modes") or {}).get(kind) or {}
        files = [f for f in mode.get("files") or [] if f.endswith(".xlsx")]
        if kind == "apiscrape":
            files = [f for f in getattr(self.job, "api_files", None) or [] if f.endswith(".xlsx")]
            mode = {"count": len(files)}
        same_job = job_id == st.get("job_id")
        if not same_job:
            result = "finished"
        elif st.get("error"):
            result = "error: " + st["error"][:120]
        else:
            result = (f"{len(files)} file(s)" if kind == "apiscrape" else f"{mode.get('count', 0)} links") + \
                (f" — {files[0]}" if files else "")
        stats = dict(getattr(self.job, "run_stats", None) or {}) if same_job else {}
        with self.lock:
            self.running_id = None
            s = next((x for x in self.items if x["id"] == sid), None)
            if s:
                ok = same_job and not st.get("error") and not stats.get("stopped")
                if stats.get("new") is not None and same_job:
                    result += f" · {stats['new']} new" + (f", {stats['known']} already known" if stats.get("known")
                                                         else "")
                s["last_result"] = result
                if ok:
                    s["last_ok"] = s.get("last_run") or time.time()
                if same_job and stats:
                    s["history"] = (s.get("history") or [])[-(HISTORY - 1):] + [
                        {"t": int(s.get("last_run") or time.time()), **{k: stats.get(k) for k in
                                                                        ("found", "new", "known", "reused",
                                                                         "credits", "secs")}}]
                if s.get("repeat") == "auto" and ok:
                    old_h = float(s.get("auto_hours") or s.get("hours") or 2)
                    s["auto_hours"] = pace(s, stats.get("new") or 0)
                    s["next_run"] = (s.get("last_run") or time.time()) + s["auto_hours"] * 3600
                    if s["auto_hours"] != old_h:
                        self.job.log(f"⏱ “{s['name']}”: {stats.get('new') or 0} new → next run in "
                                     f"{s['auto_hours']:g} h (was {old_h:g} h)")
                self._save()
        name = s["name"] if s else "schedule"
        from .telegram_bot import public_url
        link = public_url()
        self.notify(f"Post Link Collector: “{name}” finished — {result}." + (f" Open {link}" if link else ""))
        if s and s.get("webhook") and same_job:
            from . import delivery
            body = delivery.payload(self.job, kind, {"id": s["id"], "name": name})
            body["period"] = body["period"] or {"start": start, "end": end, "tz": s["payload"].get("tz", "WIB")}

            def delivered(ok, msg, sid=sid):
                with self.lock:
                    x = next((y for y in self.items if y["id"] == sid), None)
                    if x:
                        x["last_result"] = (x.get("last_result") or "") + (" · ✓ " if ok else " · ✗ ") + msg
                        self._save()
            delivery.send(s["webhook"], body, s.get("webhook_token", ""), self.job.log, f"of “{name}” ", done=delivered)

    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        self._check_finished()
        if self.job.running:
            return
        stopped = float(getattr(self.job, "stopped_at", 0) or 0)
        if now - stopped < HOLD_AFTER_STOP:              # someone pressed Stop: don't start the next run right away
            return
        with self.lock:
            due = sorted((s for s in self.items if s.get("enabled") and s.get("next_run") and s["next_run"] <= now),
                         key=lambda s: s["next_run"])
        if due:
            self._run(due[0], now)                    # one at a time; the others wait for the next tick

    def start(self, every_s: float = 20):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001  never let the scheduler die
                    try:
                        self.job.log(f"⚠ Scheduler: {e}")
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(every_s)
        self._thread = threading.Thread(target=loop, daemon=True, name="scheduler")
        self._thread.start()


SCHEDULER: Scheduler | None = None
HOLD_AFTER_STOP = 600                                  # seconds scheduled runs wait after the Stop button


def get() -> Scheduler:
    """The app's scheduler (created on first use; started by run.py / serve.py)."""
    global SCHEDULER
    if SCHEDULER is None:
        from .captcha import notify
        from .job import JOB
        from .paths import APP_DATA
        SCHEDULER = Scheduler(os.path.join(APP_DATA, "schedules.json"), JOB, notify=notify)
    return SCHEDULER
