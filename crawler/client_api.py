"""Client API: a key per client that reads only that client's news — no sign-in to the dashboard.

An admin adds a client (Client API page): a name, what the client may see (a query with AND / OR / NOT, or a report
topic), which databases (everything / Live Stream / News Search & reports), optionally from which date, a rate limit
and an expiry date. The key is shown once; only its SHA-256 is kept (APP_DATA/client_api.json, file mode 600).

The client calls, with `X-Client-Key: <key>` (or `Authorization: Bearer <key>`):
  GET /api/client/articles?q=&start=&end=&sentiment=&sort=&page=&size=   their articles (q narrows further)
  GET /api/client/articles/one?key=…                                    one of their articles in full
  GET /api/client/me                                                    what the key gives
The key reaches nothing else: no files, no dashboard, no other client's data. Calls from a browser are allowed
(CORS, no cookies) — from any website, or only the client's own addresses when set.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
_LOCK = threading.RLock()
_HITS: dict[str, deque] = {}
SOURCES = ("all", "stream", "reports")
PUBLIC_FIELDS = ("key", "url", "domain", "title", "pub_date", "pub_time", "image", "summary", "sentiment", "origin",
                 "received_at")


class ClientError(ValueError):
    pass


def _path() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "client_api.json")


def _load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _public(c: dict) -> dict:
    out = {k: v for k, v in c.items() if k != "key_hash"}
    out["expired"] = bool(c.get("expires") and c["expires"] < datetime.now(WIB).strftime("%Y-%m-%d"))
    return out


def public() -> list[dict]:
    return [_public(c) for c in _load()]


def _clean(data: dict) -> dict:
    from .feeds import normalize_query
    from .insights import fts_query
    name = str(data.get("name") or "").strip()[:80]
    if not name:
        raise ClientError("Give the client a name (e.g. PLN UID Jabar).")
    query = normalize_query(str(data.get("query") or ""))[:1000]
    topic = str(data.get("topic") or "").strip()[:100]
    if not query and not topic:
        raise ClientError("Type the client's query or keywords, or choose a report topic — the key shows only that.")
    if query:
        try:
            fts_query(query)
        except ValueError as e:
            raise ClientError(f"Query: {e}")
    if topic:
        from . import archive
        if not any(t.get("id") == topic for t in archive.topics()):
            raise ClientError("That report topic no longer exists.")
    source = data.get("source") if data.get("source") in SOURCES else "all"
    since = str(data.get("since") or "").strip()
    expires = str(data.get("expires") or "").strip()
    for v, what in ((since, "Data from"), (expires, "Expires")):
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ClientError(f"{what}: a date as YYYY-MM-DD, or empty.")
    try:
        rate = max(1, min(600, int(data.get("rate") or 60)))
    except (TypeError, ValueError):
        rate = 60
    origins = [o.strip().rstrip("/") for o in re.split(r"[,\s]+", str(data.get("origins") or "")) if o.strip()]
    for o in origins:
        if not re.fullmatch(r"https?://[^/\s]+", o):
            raise ClientError("Websites: addresses like https://dashboard.client.id (no path), comma separated.")
    return {"name": name, "query": query, "topic": topic, "source": source, "since": since, "expires": expires,
            "rate": rate, "origins": origins[:10], "enabled": bool(data.get("enabled", True))}


def save(data: dict) -> tuple[dict, str]:
    """Add or change a client. Returns (client, key) — the key only for a new client (else "")."""
    c = _clean(data)
    with _LOCK:
        items = _load()
        cid = str(data.get("id") or "")
        if cid:
            cur = next((x for x in items if x["id"] == cid), None)
            if cur is None:
                raise ClientError("That client no longer exists.")
            cur.update(c)
            _write(items)
            return _public(cur), ""
        if len(items) >= 200:
            raise ClientError("At most 200 clients.")
        key = "wtc_" + secrets.token_urlsafe(30)
        cur = {"id": uuid.uuid4().hex[:10], **c, "key_hash": _hash(key), "key_tail": key[-4:],
               "created": int(time.time()), "calls": 0, "last_used": 0}
        items.append(cur)
        _write(items)
        return _public(cur), key


def rotate(cid: str) -> str:
    """A new key for the client; the old one stops working at once."""
    with _LOCK:
        items = _load()
        cur = next((x for x in items if x["id"] == cid), None)
        if cur is None:
            raise ClientError("That client no longer exists.")
        key = "wtc_" + secrets.token_urlsafe(30)
        cur.update(key_hash=_hash(key), key_tail=key[-4:], rotated=int(time.time()))
        _write(items)
        return key


def delete(cid: str) -> bool:
    with _LOCK:
        items = _load()
        left = [x for x in items if x["id"] != cid]
        _write(left)
    return len(left) != len(items)


def identify(key: str) -> dict | None:
    """The client of a key — None when unknown, paused or expired."""
    key = (key or "").strip()
    if not key.startswith("wtc_"):
        return None
    h = _hash(key)
    c = next((x for x in _load() if secrets.compare_digest(x.get("key_hash", ""), h)), None)
    if not c or not c.get("enabled", True):
        return None
    if c.get("expires") and c["expires"] < datetime.now(WIB).strftime("%Y-%m-%d"):
        return None
    return c


def allow(c: dict) -> bool:
    """Rate limit: at most c['rate'] calls per minute per client."""
    now = time.time()
    with _LOCK:
        q = _HITS.setdefault(c["id"], deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= int(c.get("rate") or 60):
            return False
        q.append(now)
    return True


def touch(c: dict) -> None:
    """Calls and last use, written at most every 30 s per client (no file write per call)."""
    now = time.time()
    with _LOCK:
        pend = _PENDING.setdefault(c["id"], [0, 0.0])
        pend[0] += 1
        if now - pend[1] < 30:
            return
        n, pend[0], pend[1] = pend[0], 0, now
        items = _load()
        for x in items:
            if x["id"] == c["id"]:
                x["calls"] = int(x.get("calls") or 0) + n
                x["last_used"] = int(now)
        _write(items)


_PENDING: dict[str, list] = {}


def _scope(c: dict, start: str) -> tuple[str, str, str]:
    """The client's fixed filter: (query, topic, start) — a start before the client's 'data from' is moved up."""
    since = c.get("since") or ""
    if since and (not start or start[:10] < since):
        start = since
    return c.get("query") or "", c.get("topic") or "", start


def articles(c: dict, q: str = "", start: str = "", end: str = "", sentiment: str = "", sort: str = "newest",
             page: int = 1, size: int = 24, last_hours: float = 0, received_since="") -> dict:
    from . import reader
    if last_hours and float(last_hours or 0) > 0:
        start = (datetime.now(WIB) - timedelta(hours=min(float(last_hours), 24 * 3650))).strftime("%Y-%m-%d %H:%M")
    query, topic, start = _scope(c, start)
    r = reader.articles(q or "", topic, start, end, sentiment, sort, page, size, c.get("source") or "all",
                        scope=query, received_since=received_since)
    return {"client": c["name"], "server_time": r["server_time"], "total": r["total"], "matching": r["matching"],
            "page": r["page"], "pages": r["pages"], "size": r["size"], "sentiment": r["sentiment"],
            "rows": [{k: row.get(k) for k in PUBLIC_FIELDS} for row in r["rows"]]}


def article(c: dict, key: str) -> dict:
    """One article — only when it is one of the client's (same filter as the list)."""
    from . import reader
    query, topic, start = _scope(c, "")
    if not reader.articles("", topic, start, "", "", "newest", 1, 1, c.get("source") or "all", scope=query,
                           only_key=key)["total"]:
        raise ValueError("Article not found.")
    a = reader.article(key)
    a.pop("matched", None)                       # other clients' topics never show
    return a


def me(c: dict) -> dict:
    return {"client": c["name"], "query": c.get("query") or "", "topic": c.get("topic") or "",
            "source": c.get("source") or "all", "data_from": c.get("since") or "", "expires": c.get("expires") or "",
            "rate_per_minute": c.get("rate") or 60}
