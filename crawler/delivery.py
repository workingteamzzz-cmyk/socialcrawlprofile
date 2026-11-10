"""Deliver results to another server (a client's API, n8n, Make, Zapier...) as JSON when a search finishes.

- Per schedule: its `webhook` (+ optional `webhook_token`).
- Every finished search, scheduled or by hand: RESULTS_WEBHOOK_URL (+ RESULTS_WEBHOOK_TOKEN).

The request: POST <url>, Content-Type: application/json, body:
  {"event": "results.ready", "id": "<unique per delivery>", "sent_at": "...", "source": "post-link-collector",
   "kind": "news|keywords|profiles", "schedule": {...} or null, "period": {"start", "end", "tz"},
   "ok": true, "error": null, "count": 39, "file": "news_links_....xlsx", "download_path": "/download/...",
   "links": [ {...one row per link, the same columns as the Excel...} ]}
With a token: `Authorization: Bearer <token>` and `X-Collector-Signature: sha256=<HMAC-SHA256 of the body with the
token>`, so the receiver can check the data really comes from this app. Failed deliveries are retried
(after 30 s, 2 min and 10 min); any 2xx answer counts as delivered.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid

RETRY_AFTER = (30, 120, 600)
MAX_LINKS = 5000


def payload(job, kind: str, schedule: dict | None = None) -> dict:
    """The result of the job's last `kind` run, ready to send."""
    st = job.state()
    mode = (st.get("modes") or {}).get(kind) or {}
    files = [f for f in mode.get("files") or [] if f.endswith(".xlsx")]
    period = dict(mode.get("period") or {})
    try:
        links = list((getattr(job, "data", {}).get(kind) or {}).get("links") or [])[:MAX_LINKS]
    except Exception:  # noqa: BLE001
        links = []
    return {"event": "results.ready", "id": uuid.uuid4().hex, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "post-link-collector", "kind": kind, "schedule": schedule, "period": period or None,
            "ok": not st.get("error"), "error": st.get("error") or None, "count": mode.get("count", len(links)),
            "file": files[0] if files else None, "download_path": f"/download/{files[0]}" if files else None,
            "dashboard": os.environ.get("PUBLIC_URL", "").strip() or None, "links": links}


def _send_once(url: str, data: bytes, token: str) -> str | None:
    headers = {"Content-Type": "application/json", "User-Agent": "post-link-collector"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Collector-Signature"] = "sha256=" + hmac.new(token.encode(), data, hashlib.sha256).hexdigest()
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return None if 200 <= r.status < 300 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return str(e)[:120] or e.__class__.__name__


def send(url: str, body: dict, token: str = "", log=print, label: str = "", retry_after=RETRY_AFTER, done=None):
    """Deliver in the background, retrying. done(ok, message) is called at the end."""
    data = json.dumps(body, ensure_ascii=False, default=str).encode()

    def run():
        from urllib.parse import urlparse
        where = urlparse(url).netloc or url
        err = None
        for attempt, wait in enumerate((0, *retry_after)):
            if wait:
                time.sleep(wait)
            err = _send_once(url, data, token)
            if err is None:
                log(f"📤 Results {label}delivered to {where} ({body.get('count', 0)} links)")
                if done:
                    done(True, f"delivered to {where}")
                return
            if attempt < len(retry_after):
                log(f"⚠ Delivering results to {where} failed ({err}) — trying again in {retry_after[attempt]} s")
        log(f"❌ Results could not be delivered to {where} ({err}). They are still in Exports and /api/results.")
        if done:
            done(False, f"delivery to {where} failed: {err}")
    threading.Thread(target=run, daemon=True, name="delivery").start()


def deliver_finished_job(job, kind: str):
    """Called when any search finishes: RESULTS_WEBHOOK_URL gets its results (if set)."""
    url = os.environ.get("RESULTS_WEBHOOK_URL", "").strip()
    if url:
        send(url, payload(job, kind), os.environ.get("RESULTS_WEBHOOK_TOKEN", "").strip(), job.log)
