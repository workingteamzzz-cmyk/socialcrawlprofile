"""Client feeds: every article the live stream reads that matches a client's query is pushed to the client's server
within seconds (a webhook), one small batch at a time.

A feed: name, what it gets — EVERY article ("all"), a query (AND / OR / NOT, "phrase", ( ) — the same syntax as
everywhere) or a plain keyword list (comma / one per line = any of them) — webhook URL, optional token.
Delivery: POST <url>, JSON
  {"event": "articles", "feed": {"id", "name"}, "sent_at": "...", "count": 3,
   "articles": [{"key", "url", "media", "title", "date", "time", "image", "summary", "content" (<p>…</p>),
                 "author", "influencer", "person", "sentiment", "read_at"}, ...]}
With a token: `Authorization: Bearer <token>` and `X-Collector-Signature: sha256=<HMAC-SHA256 of the body>`.
An article waits in an outbox (stream.db) until the client answers 2xx: retried after 30 s, 2 min, 10 min, 30 min,
then every hour for a day — so a client server that is down for a while loses nothing.
Feeds are kept in APP_DATA/stream/feeds.json (file mode 600: it holds the tokens). Admins manage them (Live Stream).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

BATCH = 50
RETRY = (30, 120, 600, 1800) + (3600,) * 24
_CACHE = {"t": 0.0, "mt": 0.0, "feeds": []}
_LOCK = threading.RLock()


def _path() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream", "feeds.json")


def load() -> list[dict]:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write(feeds: list[dict]) -> None:
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump(feeds, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path())
    _CACHE["t"] = 0


def public() -> list[dict]:
    """Feeds as the dashboard shows them: the token is never shown again (only whether one is set)."""
    out = []
    for f in load():
        g = {k: v for k, v in f.items() if k != "token"}
        g["token_set"] = bool(f.get("token"))
        out.append(g)
    return out


def normalize_query(q: str) -> str:
    """A plain keyword list ("pln, listrik padam" or one per line) -> any of them: pln OR "listrik padam".
    A real query (AND / OR / NOT, quotes, brackets) is kept as typed."""
    q = (q or "").strip()
    if re.search(r'\b(AND|OR|NOT)\b|["()]', q) or not re.search(r"[,\n;]", q):
        return q
    parts = [p.strip() for p in re.split(r"[,\n;]+", q) if p.strip()]
    return " OR ".join(f'"{p}"' if " " in p else p for p in parts)


def save(data: dict) -> dict:
    from .medmas.filters import validate_keyword
    name = str(data.get("name") or "").strip()[:80]
    every = bool(data.get("all")) or str(data.get("query") or "").strip() == "*"
    query = "" if every else normalize_query(str(data.get("query") or ""))[:1000]
    url = str(data.get("url") or "").strip()[:500]
    if not name:
        raise ValueError("Give the feed a name (e.g. the client).")
    if not every:
        if not query:
            raise ValueError("Type the query or keywords — or choose every article.")
        problem = validate_keyword(query)
        if problem:
            raise ValueError(f"Query: {problem}")
    if not re.fullmatch(r"https?://[^\s]+", url):
        raise ValueError("The client's webhook must be a web address starting with https://")
    with _LOCK:
        feeds = load()
        fid = str(data.get("id") or "")
        cur = next((f for f in feeds if f["id"] == fid), None) if fid else None
        if cur is None:
            cur = {"id": uuid.uuid4().hex[:10], "created": int(time.time()), "sent": 0, "failed": 0,
                   "last_ok": 0, "last_error": "", "token": ""}
            feeds.append(cur)
        cur.update(name=name, query=query, all=every, url=url, enabled=bool(data.get("enabled", True)))
        if data.get("clear_token"):
            cur["token"] = ""
        elif str(data.get("token") or "").strip():
            cur["token"] = str(data["token"]).strip()[:300]
        _write(feeds)
        return {k: v for k, v in cur.items() if k != "token"} | {"token_set": bool(cur["token"])}


def delete(fid: str) -> bool:
    with _LOCK:
        feeds = load()
        left = [f for f in feeds if f["id"] != fid]
        _write(left)
    return len(left) != len(feeds)


def _stat(fid: str, ok: bool, n: int, err: str = "") -> None:
    with _LOCK:
        feeds = load()
        for f in feeds:
            if f["id"] == fid:
                if ok:
                    f["sent"] = f.get("sent", 0) + n
                    f["last_ok"], f["last_error"] = int(time.time()), ""
                else:
                    f["last_error"] = err[:200]
                    if n:
                        f["failed"] = f.get("failed", 0) + n
        _write(feeds)


def active() -> list[dict]:
    """Enabled feeds, re-read when the file changes (checked at most every 5 s)."""
    if time.time() - _CACHE["t"] > 5:
        try:
            mt = os.path.getmtime(_path())
        except OSError:
            mt = 0.0
        if mt != _CACHE["mt"]:
            _CACHE["feeds"] = [f for f in load() if f.get("enabled", True)]
            _CACHE["mt"] = mt
        _CACHE["t"] = time.time()
    return _CACHE["feeds"]


# ---------- the stream's side ----------
def article_body(job: dict, res: dict, d: str, t: str, sentiment: str) -> dict:
    from datetime import timedelta, timezone
    from . import templates
    full = f"{d} {t}" if re.match(r"\d{4}-\d{2}-\d{2}$", d or "") and re.match(r"\d{2}:\d{2}$", t or "") else ""
    row = templates.online_row({"url": job["url"], "title": res.get("title") or job.get("title") or "",
                                "text": res.get("content") or "", "description": res.get("summary") or "",
                                "image": res.get("image") or "", "author": res.get("author") or "",
                                "influencer": res.get("influencer") or "", "person": res.get("persons") or "",
                                "published": d, "published_full": full, "sentiment": sentiment},
                               timezone(timedelta(hours=7)))
    c = dict(zip(templates.ONLINE_COLUMNS, row))
    return {"key": job["key"], "url": c["URL"], "media": c["MEDIA"], "title": c["TITLE"], "date": d, "time": t,
            "image": c["IMAGE"], "summary": c["SUMMARY"], "content": c["CONTENT"], "author": c["AUTHOR"],
            "influencer": c["INFLUENCER"], "person": c["PERSON"], "sentiment": c["SENTIMENT"],
            "read_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def match_and_queue(stream, job: dict, res: dict, d: str, t: str, sentiment: str) -> int:
    """Called by the stream for every article it read: matching feeds get it in their outbox."""
    feeds = active()
    if not feeds:
        return 0
    from .medmas.filters import keyword_excluded, keyword_match, url_slug
    title, content, slug = res.get("title") or "", res.get("content") or "", url_slug(job["url"])
    hits = [f for f in feeds if f.get("all") or (f.get("query") and keyword_match(f["query"], title, content, slug)
                                                 and not keyword_excluded(f["query"], title, content, slug))]
    if not hits:
        return 0
    body = json.dumps(article_body(job, res, d, t, sentiment), ensure_ascii=False, default=str)
    now = time.time()
    with stream.lock:
        stream._db.executemany("INSERT INTO feed_outbox(feed_id, article, tries, next_try, created) VALUES (?,?,0,?,?)",
                               [(f["id"], body, now, now) for f in hits])
        stream._db.commit()
    return len(hits)


def send_due(stream, ev=None) -> int:
    """One round: for each feed, up to BATCH due articles in one POST. Returns how many were delivered."""
    from .delivery import _send_once
    feeds = {f["id"]: f for f in load()}
    rows = stream.rq("SELECT id, feed_id, article, tries FROM feed_outbox WHERE next_try <= ? ORDER BY id LIMIT 2000",
                     (time.time(),))
    by: dict = {}
    for r in rows:
        by.setdefault(r["feed_id"], []).append(r)
    sent = 0
    for fid, items in by.items():
        if ev is not None and ev.is_set():
            break
        f = feeds.get(fid)
        if not f:                                              # feed deleted: its outbox goes too
            stream.x("DELETE FROM feed_outbox WHERE feed_id=?", (fid,))
            continue
        if not f.get("enabled", True):
            continue
        for i in range(0, len(items), BATCH):
            part = items[i:i + BATCH]
            body = {"event": "articles", "feed": {"id": fid, "name": f["name"]}, "sent_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z"), "count": len(part), "articles": [json.loads(r["article"]) for r in part]}
            err = _send_once(f["url"], json.dumps(body, ensure_ascii=False, default=str).encode(), f.get("token", ""))
            ids = [r["id"] for r in part]
            if err is None:
                with stream.lock:
                    stream._db.executemany("DELETE FROM feed_outbox WHERE id=?", [(x,) for x in ids])
                    stream._db.commit()
                _stat(fid, True, len(part))
                sent += len(part)
                continue
            dropped = 0
            with stream.lock:
                for r in part:
                    tries = r["tries"] + 1
                    if tries > len(RETRY):
                        stream._db.execute("DELETE FROM feed_outbox WHERE id=?", (r["id"],))
                        dropped += 1
                    else:
                        stream._db.execute("UPDATE feed_outbox SET tries=?, next_try=? WHERE id=?",
                                           (tries, time.time() + RETRY[tries - 1], r["id"]))
                stream._db.commit()
            _stat(fid, False, dropped, err)
            break                                              # this client is down: its next batches wait too
    return sent


def sender_loop(stream) -> None:
    ev = stream.stop_event
    while not ev.is_set():
        try:
            send_due(stream, ev)
        except Exception as e:  # noqa: BLE001
            stream.last_error = f"client feeds: {str(e)[:120]}"
        ev.wait(3)


def waiting(stream) -> dict:
    try:
        return {r["feed_id"]: r["n"] for r in stream.rq("SELECT feed_id, COUNT(*) n FROM feed_outbox GROUP BY feed_id")}
    except Exception:  # noqa: BLE001
        return {}


def test(fid: str) -> dict:
    """Send one sample article now (not through the outbox): does the client's server answer?"""
    from .delivery import _send_once
    f = next((x for x in load() if x["id"] == fid), None)
    if not f:
        raise ValueError("Unknown feed.")
    body = {"event": "test", "feed": {"id": fid, "name": f["name"]}, "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "count": 1, "articles": [{"key": "test", "url": "https://example.com/berita/contoh", "media": "example.com",
                                      "title": "Contoh berita (test from the media monitoring server)",
                                      "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%H:%M"),
                                      "summary": "This is a test delivery.", "content": "<p>This is a test delivery.</p>",
                                      "sentiment": "neutral"}]}
    err = _send_once(f["url"], json.dumps(body).encode(), f.get("token", ""))
    return {"ok": err is None, "error": err or ""}
