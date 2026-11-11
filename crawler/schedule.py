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

KINDS = ("news", "keywords", "profiles")
# period collected at run time: (first day, last day) relative to the run's day
WINDOWS = {"yesterday": (-1, -1), "today": (0, 0), "yesterday_today": (-1, 0), "last_7_days": (-7, -1)}
WINDOW_LABEL = {"yesterday": "yesterday", "today": "today", "yesterday_today": "yesterday + today",
                "last_7_days": "the 7 days before"}
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
    return after + int(s["hours"]) * 3600


def period(s: dict, at: float) -> tuple[str, str]:
    """(start, end) dates, YYYY-MM-DD in the schedule's time zone, for a run at `at`."""
    day = datetime.fromtimestamp(at, _tz(s.get("payload", {}).get("tz", "WIB"))).date()
    a, b = WINDOWS[s["window"]]
    return (day + timedelta(days=a)).isoformat(), (day + timedelta(days=b)).isoformat()


def describe(s: dict) -> str:
    when = f"every day at {s['time']}" if s["repeat"] == "daily" else \
        ("every hour" if int(s["hours"]) == 1 else f"every {s['hours']} hours")
    if s.get("new_only"):
        when += ", only new results"
    return f"{when} ({s.get('payload', {}).get('tz', 'WIB')}), collecting {WINDOW_LABEL[s['window']]}"


def clean(data: dict) -> dict:
    """Validate a schedule sent by the dashboard/API. Raises ScheduleError with a readable message."""
    kind = data.get("kind")
    if kind not in KINDS:
        raise ScheduleError("Choose what to run: news, keywords or profiles.")
    payload = dict(data.get("payload") or {})
    payload.pop("start", None)
    payload.pop("end", None)
    if kind in ("news", "keywords") and not [k for k in payload.get("keywords") or [] if str(k).strip()]:
        raise ScheduleError("Add at least one keyword.")
    if kind == "profiles" and not (payload.get("ids") or str(payload.get("text") or "").strip()):
        raise ScheduleError("Select at least one account.")
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
    elif repeat == "hours":
        try:
            h = int(data.get("hours"))
        except (TypeError, ValueError):
            h = 0
        if not 1 <= h <= 24:
            raise ScheduleError("Repeat every 1 to 24 hours.")
        s["hours"] = h
    else:
        raise ScheduleError("Choose how often: daily or every few hours.")
    if s["window"] not in WINDOWS:
        raise ScheduleError("Choose the period to collect.")
    if not s["name"]:
        what = ", ".join(payload.get("keywords") or [])[:40] or "accounts"
        s["name"] = f"{kind.capitalize()}: {what}"
    return s


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
        self.job._next_origin = "Manual" if manual else "Auto"         # file names: 20261108_Online_Auto_24.00
        if s.get("new_only"):
            self.job._next_new_only = True                              # each file: only what is new since last run
        if s["kind"] == "news":
            opts = dict(p.get("options") or {})
            if s.get("new_only"):
                opts["new_only"] = True                                 # the multi-source engine's own memory too
            err = self.job.start_news(p.get("keywords") or [], start, end, p["tz"], opts)
        elif s["kind"] == "keywords":
            err = self.job.start_keywords(p.get("keywords") or [], start, end, p["tz"], p.get("platforms") or [],
                                          p.get("options") or {})
        else:
            from . import store
            text = store.lines_for(p.get("list", ""), p["ids"]) if p.get("ids") else p.get("text", "")
            err = self.job.start_crawl(text, start, end, p["tz"], p.get("platforms") or [], p.get("detail", "links"),
                                       p.get("template", "standard"))
        if err:
            self.job.__dict__.pop("_next_origin", None)
            self.job.__dict__.pop("_next_new_only", None)
            return err                                       # a scheduled one stays due and is tried again
        with self.lock:
            self.running_id = (s["id"], self.job.job_id, s["kind"], start, end)
            s["last_run"] = now
            s["last_result"] = "running…"
            if not manual:
                s["next_run"] = next_run(s, now)
            self._save()
        self.job.log(f"⏰ Schedule “{s['name']}” started — period {start} to {end}")
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
        same_job = job_id == st.get("job_id")
        if not same_job:
            result = "finished"
        elif st.get("error"):
            result = "error: " + st["error"][:120]
        else:
            result = f"{mode.get('count', 0)} links" + (f" — {files[0]}" if files else "")
        with self.lock:
            self.running_id = None
            s = next((x for x in self.items if x["id"] == sid), None)
            if s:
                s["last_result"] = result
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


def get() -> Scheduler:
    """The app's scheduler (created on first use; started by run.py / serve.py)."""
    global SCHEDULER
    if SCHEDULER is None:
        from .captcha import notify
        from .job import JOB
        from .paths import APP_DATA
        SCHEDULER = Scheduler(os.path.join(APP_DATA, "schedules.json"), JOB, notify=notify)
    return SCHEDULER
