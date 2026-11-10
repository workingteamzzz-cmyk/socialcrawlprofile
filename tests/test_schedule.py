"""Scheduled searches: run times, periods, waiting for a running job, catching up, results, storage, API."""
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler import schedule as sch  # noqa: E402

WIB = timezone(timedelta(hours=7))


def at(y, m, d, hh=0, mm=0, tz=WIB):
    return datetime(y, m, d, hh, mm, tzinfo=tz).timestamp()


NEWS = {"kind": "news", "payload": {"keywords": ["tarif listrik"], "tz": "WIB", "options": {"cap": 50}},
        "repeat": "daily", "time": "07:00", "window": "yesterday"}


def test_run_times_and_periods_in_wib():
    s = sch.clean(NEWS)
    assert s["name"] == "News: tarif listrik"
    # 06:59 WIB -> today 07:00 WIB; 07:00 exactly -> tomorrow
    assert sch.next_run(s, at(2026, 11, 10, 6, 59)) == at(2026, 11, 10, 7, 0)
    assert sch.next_run(s, at(2026, 11, 10, 7, 0)) == at(2026, 11, 11, 7, 0)
    # a run at 07:00 WIB on 10 Nov (00:00 UTC) collects 9 Nov, in WIB
    assert sch.period(s, at(2026, 11, 10, 7, 0)) == ("2026-11-09", "2026-11-09")
    assert sch.period({**s, "window": "last_7_days"}, at(2026, 11, 10, 7)) == ("2026-11-03", "2026-11-09")
    assert sch.period({**s, "window": "yesterday_today"}, at(2026, 11, 1, 0, 30)) == ("2026-10-31", "2026-11-01")
    h = sch.clean({**NEWS, "repeat": "hours", "hours": 6, "window": "today"})
    assert sch.next_run(h, 1000.0) == 1000.0 + 6 * 3600
    assert "every 6 hours" in sch.describe(h) and "every day at 07:00 (WIB), collecting yesterday" == sch.describe(s)


def test_bad_schedules_are_explained():
    for bad, msg in [({**NEWS, "kind": "x"}, "what to run"), ({**NEWS, "payload": {"keywords": [" "]}}, "keyword"),
                     ({**NEWS, "time": "7"}, "HH:MM"), ({**NEWS, "repeat": "hours", "hours": 0}, "1 to 24"),
                     ({**NEWS, "window": "forever"}, "period"), ({**NEWS, "repeat": "weekly"}, "how often"),
                     ({"kind": "profiles", "payload": {"ids": []}, "repeat": "daily", "time": "07:00",
                       "window": "yesterday"}, "account")]:
        with pytest.raises(sch.ScheduleError, match=msg):
            sch.clean(bad)
    s = sch.clean({**NEWS, "payload": {**NEWS["payload"], "start": "2020-01-01", "end": "2020-01-02", "tz": "PST"}})
    assert "start" not in s["payload"] and s["payload"]["tz"] == "WIB"     # dates come from the window, tz checked


class FakeJob:
    """Behaves like crawler.job.JOB for the scheduler: starts, runs, finishes."""
    def __init__(self):
        self.running = False
        self.job_id = 0
        self.calls, self.logs = [], []
        self.error = ""
        self.count = 0

    def _start(self, *call):
        if self.running:
            return "A job is already running."
        self.calls.append(call)
        self.running, self.job_id = True, self.job_id + 1
        return None

    def start_news(self, kws, start, end, tz, options):
        return self._start("news", kws, start, end, tz, options)

    def start_keywords(self, kws, start, end, tz, plats, options):
        return self._start("keywords", kws, start, end, tz, plats, options)

    def start_crawl(self, text, start, end, tz, plats, detail, template="standard"):
        return self._start("profiles", text, start, end, tz, plats, detail)

    def finish(self, count=12, error=""):
        self.running, self.count, self.error = False, count, error

    def state(self):
        return {"job_id": self.job_id, "error": self.error,
                "modes": {"news": {"count": self.count, "files": [f"news_links_{self.job_id}.xlsx"]},
                          "keywords": {"count": self.count, "files": []}, "profiles": {"count": 0, "files": []}}}

    def log(self, m):
        self.logs.append(m)


def test_scheduler_runs_waits_catches_up_and_reports(tmp_path):
    job, sent = FakeJob(), []
    s = sch.Scheduler(str(tmp_path / "schedules.json"), job, notify=sent.append)
    created = s.save(NEWS)
    assert created["next_run"] > 0 and len(s.list()) == 1
    first = created["next_run"]
    s.tick(first - 60)
    assert job.calls == []                                                   # not yet
    job.running = True                                                       # someone runs a search by hand
    s.tick(first + 5)
    assert job.calls == []                                                   # waits for it
    job.running = False
    s.tick(first + 30)
    assert job.calls == [("news", ["tarif listrik"], *sch.period(created, first + 30), "WIB", {"cap": 50})]
    item = s.list()[0]
    assert item["running"] and item["last_result"] == "running…" and item["next_run"] == first + 86400
    job.finish(count=37)
    s.tick(first + 60)
    item = s.list()[0]
    assert not item["running"] and item["last_result"].startswith("37 links — news_links_1.xlsx")
    assert sent and "finished — 37 links" in sent[-1]
    # stored: a restart (new Scheduler on the same file) keeps it, and a missed run is caught up once
    s2 = sch.Scheduler(str(tmp_path / "schedules.json"), job)
    late = item["next_run"] + 3 * 86400                                      # server was off for 3 days
    s2.tick(late)
    assert len(job.calls) == 2
    assert s2.list()[0]["next_run"] > late                                   # then back on its normal rhythm
    job.finish(error="The browser window was closed")
    s2.tick(late + 30)
    assert s2.list()[0]["last_result"].startswith("error:")


def test_pause_run_now_and_delete(tmp_path):
    job = FakeJob()
    s = sch.Scheduler(str(tmp_path / "schedules.json"), job)
    a = s.save(NEWS)
    s.save({**a, "enabled": False})
    s.tick(a["next_run"] + 10)
    assert job.calls == []                                                   # paused
    assert s.run_now(a["id"]) is None and len(job.calls) == 1                # but can be run by hand
    assert s.run_now(a["id"]) == "A job is already running."
    assert s.list()[0]["next_run"] == a["next_run"]                          # a manual run keeps the rhythm
    assert s.delete(a["id"]) and not s.delete(a["id"]) and s.list() == []
    assert s.run_now("nope") == "That schedule no longer exists."


def test_profiles_schedule_uses_the_saved_accounts(tmp_path, monkeypatch):
    from crawler import store
    monkeypatch.setattr(store, "lines_for", lambda lst, ids: f"{lst}:{','.join(ids)}")
    job = FakeJob()
    s = sch.Scheduler(str(tmp_path / "schedules.json"), job)
    a = s.save({"kind": "profiles", "payload": {"list": "ditjen", "ids": ["a1", "a2"], "platforms": ["x"], "detail": "full"},
                "repeat": "hours", "hours": 12, "window": "today"})
    s.run_now(a["id"])
    assert job.calls[0][0] == "profiles" and job.calls[0][1] == "ditjen:a1,a2" and job.calls[0][-2:] == (["x"], "full")


def test_schedules_api_and_audit(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from crawler import api
    job = FakeJob()
    monkeypatch.setattr(sch, "SCHEDULER", sch.Scheduler(str(tmp_path / "s.json"), job))
    key = "sched-key-0123456789abcdef"
    c = TestClient(api.create_app({"ana": key}, audit_path=str(tmp_path / "audit.log")))
    h = {"X-API-Key": key}
    r = c.post("/api/schedules/save", headers=h, json=NEWS).json()
    assert r["ok"] and r["schedule"]["name"] == "News: tarif listrik"
    sid = r["schedule"]["id"]
    assert c.post("/api/schedules/save", headers=h, json={**NEWS, "time": "25:00"}).json()["error"].startswith("Give the time")
    lst = c.get("/api/schedules", headers=h).json()["schedules"]
    assert [x["id"] for x in lst] == [sid] and lst[0]["describe"].startswith("every day at 07:00")
    assert c.post("/api/schedules/run", headers=h, json={"id": sid}).json()["ok"]
    assert c.post("/api/schedules/delete", headers=h, json={"id": sid}).json()["ok"]
    acts = [e["action"] for e in c.get("/api/audit", headers=h).json()["entries"]]
    assert acts[:3] == ["deleted a schedule", "ran a schedule now", "created a schedule"]


def test_schedules_on_the_local_app(tmp_path, monkeypatch):
    import json
    import urllib.request
    from http.server import ThreadingHTTPServer

    from crawler import server
    job = FakeJob()
    monkeypatch.setattr(sch, "SCHEDULER", sch.Scheduler(str(tmp_path / "s.json"), job))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req, timeout=10).read())
    try:
        sid = post("/api/schedules/save", NEWS)["schedule"]["id"]
        assert json.loads(urllib.request.urlopen(base + "/api/schedules", timeout=10).read())["schedules"][0]["id"] == sid
        assert post("/api/schedules/run", {"id": sid})["ok"] and job.calls
        assert post("/api/schedules/save", {**NEWS, "window": "x"})["error"] == "Choose the period to collect."
        assert post("/api/schedules/delete", {"id": sid})["ok"]
    finally:
        httpd.shutdown()


class _Receiver:
    """A client's server: records what it receives; can fail the first N requests."""
    def __init__(self, fail_first=0):
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        rec = self
        self.got, self.fail_left = [], fail_first

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if rec.fail_left:
                    rec.fail_left -= 1
                    self.send_response(503)
                    self.end_headers()
                    return
                rec.got.append((dict(self.headers), raw, json.loads(raw)))
                self.send_response(201)
                self.end_headers()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/ingest"

    def wait(self, n=1, s=10):
        import time
        for _ in range(int(s * 10)):
            if len(self.got) >= n:
                return True
            time.sleep(0.1)
        return False


def test_results_delivered_to_a_client_server(tmp_path, monkeypatch):
    """When a scheduled run finishes, the client's server receives the links as JSON, with its token and a signature
    it can check; a server that is down at first gets the data on a retry."""
    import hashlib
    import hmac

    from crawler import delivery
    monkeypatch.setattr(delivery, "RETRY_AFTER", (0.2, 0.2, 0.2))
    rx = _Receiver(fail_first=2)
    job = FakeJob()
    job.data = {"news": {"links": [{"keyword": "tarif listrik", "url": "https://news.detik.com/1", "title": "A"}]}}
    s = sch.Scheduler(str(tmp_path / "s.json"), job)
    with pytest.raises(sch.ScheduleError, match="https://"):
        s.save({**NEWS, "webhook": "ftp://x"})
    a = s.save({**NEWS, "webhook": rx.url, "webhook_token": "client-secret-123"})
    assert s.list()[0]["webhook_token"] == "••••••"                          # never shown again
    s.save({**s.list()[0]})                                                   # saving the list's copy keeps the token
    assert s.items[0]["webhook_token"] == "client-secret-123"
    s.run_now(a["id"])
    job.finish(count=1)
    monkeypatch.setattr(delivery, "send", lambda *a, **k: _real_send(*a, **{**k, "retry_after": (0.2, 0.2, 0.2)}))
    s.tick()
    assert rx.wait(), job.logs
    headers, raw, body = rx.got[0]
    assert body["event"] == "results.ready" and body["schedule"]["name"] == "News: tarif listrik" and body["kind"] == "news"
    assert body["count"] == 1 and body["file"] == "news_links_1.xlsx" and body["download_path"] == "/download/news_links_1.xlsx"
    assert body["links"][0]["url"] == "https://news.detik.com/1" and body["period"]["tz"] == "WIB"
    assert headers["Authorization"] == "Bearer client-secret-123"
    expected = "sha256=" + hmac.new(b"client-secret-123", raw, hashlib.sha256).hexdigest()
    assert headers["X-Collector-Signature"] == expected                       # the client can verify it
    assert any("failed (HTTP 503)" in m for m in job.logs) and any("delivered to" in m for m in job.logs)
    import time
    for _ in range(30):
        if "✓ delivered" in s.list()[0]["last_result"]:
            break
        time.sleep(0.1)
    assert "✓ delivered to 127.0.0.1" in s.list()[0]["last_result"]
    rx.httpd.shutdown()


from crawler.delivery import send as _real_send  # noqa: E402


def test_every_finished_search_can_go_to_the_client(monkeypatch):
    """RESULTS_WEBHOOK_URL: every search, also those started by hand, is delivered; a server that never answers
    is reported, not retried forever."""
    from crawler import delivery
    rx = _Receiver()
    monkeypatch.setenv("RESULTS_WEBHOOK_URL", rx.url)
    monkeypatch.setenv("RESULTS_WEBHOOK_TOKEN", "")
    job = FakeJob()
    job.data = {"keywords": {"links": [{"keyword": "PLN", "platform": "x", "url": "https://x.com/a/status/1"}]}}
    job.start_keywords(["PLN"], "2026-11-09", "2026-11-09", "WIB", ["x"], {})
    job.finish(count=1)
    delivery.deliver_finished_job(job, "keywords")
    assert rx.wait()
    headers, _raw, body = rx.got[0]
    assert body["kind"] == "keywords" and body["schedule"] is None and body["links"][0]["platform"] == "x"
    assert "Authorization" not in headers
    rx.httpd.shutdown()
    logs, outcome = [], []
    delivery.send("http://127.0.0.1:9/nothing-here", {"count": 0}, "", logs.append, retry_after=(0.1,),
                  done=lambda ok, msg: outcome.append((ok, msg)))
    import time
    for _ in range(100):
        if outcome:
            break
        time.sleep(0.1)
    assert outcome and outcome[0][0] is False and any("could not be delivered" in m for m in logs)


def test_the_real_job_delivers_when_it_finishes(tmp_path, monkeypatch):
    from crawler import job as job_mod
    rx = _Receiver()
    monkeypatch.setenv("RESULTS_WEBHOOK_URL", rx.url)
    j = job_mod.Job()
    assert j._start("news", lambda: None) is None
    j.thread.join(5)
    assert rx.wait() and rx.got[0][2]["kind"] == "news"
    rx.httpd.shutdown()


def test_telegram_messages_for_verifications_and_finished_schedules(tmp_path, monkeypatch):
    """TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID: a message to each chat (via the Bot API's sendMessage)."""
    import json
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from crawler import captcha
    got = []

    class TG(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), TG)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("TELEGRAM_API_BASE", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, 222")
    monkeypatch.setenv("PUBLIC_URL", "https://collector.example")
    for k in ("NOTIFY_WEBHOOK_URL", "CAPTCHA_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)

    def wait(n):
        for _ in range(50):
            if len(got) >= n:
                return
            time.sleep(0.1)
    captcha._notify("Google Search")
    wait(2)
    assert sorted(b["chat_id"] for _, b in got) == ["111", "222"] and got[0][0] == "/bot123:ABC/sendMessage"
    assert "Google Search" in got[0][1]["text"] and "https://collector.example" in got[0][1]["text"]
    job = FakeJob()
    s = sch.Scheduler(str(tmp_path / "s.json"), job, notify=captcha.notify)
    s.run_now(s.save(NEWS)["id"])
    job.finish(count=42)
    s.tick()
    wait(4)
    assert any("finished — 42 links" in b["text"] for _, b in got[2:])
    httpd.shutdown()
