"""International (non-Indonesian) outlets: recognised, paused when the admin wants the stream's effort on Indonesian
news, and caught up later — everything they published while paused, or a chosen period.

Which outlets are international, decided in this order:
  1. the admin's own marks (Media Database: Mark Indonesian / Mark international) — always win;
  2. the address: .id (co.id, go.id, ac.id, my.id…) = Indonesian; another country's ending (.my, .sg, .co.uk, .com.au,
     .in, .ph…) = international;
  3. for .com / .net / .org and the like: the language of the titles the stream read from it (Indonesian function words
     like "yang", "dan", "di", "untuk"): at least 8 titles and fewer than a quarter in Indonesian = international.
     Too few titles = not decided (never paused).
The decision is redone once a day by the stream (and on demand); it is kept in stream.db (outlet_country).

Pause: their RSS feeds and sitemaps are not checked (sources.paused = 1); links already waiting are still read.
Catch-up after the pause: up to 7 days — the late-article sweep, for those outlets only; longer — a media-archive job
per outlet for the period (Media Database → Media archive shows them).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
# second-level endings that stay "generic" (used by Indonesian outlets too)
GENERIC_CC = {"co", "tv", "me", "io", "ws", "fm", "am", "ly", "to", "cc", "news", "in.net"}
ID_WORDS = set("""yang dan di ke dari untuk dengan ini itu tidak akan pada dalam oleh jadi bisa ada kata warga pemerintah
polisi kota kabupaten provinsi presiden jakarta indonesia ri rp sebut minta soal usai jelang bakal resmi hingga lagi baru
tak belum sudah karena agar atau juga masih saat jalan harga gelar rumah sakit bupati gubernur wali menteri dpr dprd
pemkab pemkot pemprov polres polda kapolres tni pln bumn ojk bi ihsg rupiah warganet viral lebaran ramadan""".split())


def _words(t: str) -> list[str]:
    return re.findall(r"[a-z]+", (t or "").lower())


def is_indonesian_title(t: str) -> bool:
    w = _words(t)
    return bool(w) and sum(1 for x in w if x in ID_WORDS) >= (1 if len(w) <= 6 else 2)


def by_address(domain: str) -> str:
    """'id', 'intl' or '' (decided by the titles)."""
    d = (domain or "").lower().strip(".")
    parts = d.split(".")
    if len(parts) < 2:
        return ""
    if parts[-1] == "id":
        return "id"
    tld = parts[-1]
    if len(tld) == 2 and tld not in GENERIC_CC:
        return "intl"                                           # .my .sg .au .uk (co.uk) .in .ph .jp …
    return ""


def classify(stream, days: int = 21, per_domain: int = 30) -> dict:
    """Decide every outlet the stream knows; returns the counts. Kept in stream.db (outlet_country)."""
    ensure(stream)
    since = time.time() - days * 86400
    titles: dict[str, list[str]] = {}
    for r in stream.rq("SELECT domain, title FROM articles WHERE status='success' AND fetched > ? AND title != ''",
                       (since,)):
        lst = titles.setdefault(r["domain"], [])
        if len(lst) < per_domain:
            lst.append(r["title"])
    domains = {r["domain"] for r in stream.rq("SELECT DISTINCT domain FROM sources")} | set(titles)
    marks = {r["domain"]: r["mark"] for r in stream.rq("SELECT domain, mark FROM outlet_country WHERE mark != ''")}
    rows, cnt = [], {"id": 0, "intl": 0, "unknown": 0}
    now = time.time()
    for d in domains:
        if not d:
            continue
        ts = titles.get(d) or []
        share = round(sum(1 for t in ts if is_indonesian_title(t)) / len(ts), 2) if ts else None
        country = marks.get(d) or by_address(d)
        if not country:
            country = "unknown" if share is None or len(ts) < 8 else ("intl" if share < 0.25 else "id")
        cnt[country] = cnt.get(country, 0) + 1
        rows.append((d, country, share, len(ts), now))
    with stream.lock:
        stream._db.executemany("INSERT INTO outlet_country(domain, country, id_share, n, checked) VALUES (?,?,?,?,?) "
                               "ON CONFLICT(domain) DO UPDATE SET country=excluded.country, id_share=excluded.id_share, "
                               "n=excluded.n, checked=excluded.checked", rows)
        stream._db.commit()
    apply_pause(stream)
    stream.meta("intl_classified", datetime.now(WIB).strftime("%Y-%m-%d %H:%M"))
    return cnt


def ensure(stream) -> None:
    """The table and column come with the stream's schema (stream.py); nothing to do for an up-to-date database."""
    return None


def international(stream) -> list[str]:
    ensure(stream)
    return [r["domain"] for r in stream.rq("SELECT domain FROM outlet_country WHERE country='intl' ORDER BY domain")]


def apply_pause(stream) -> int:
    """sources.paused follows the setting and the current list. Pausing notes the moment; resuming makes the
    outlets due at once (the missed period is caught up separately)."""
    from .stream import settings
    on = bool(settings().get("pause_international"))
    with stream.lock:
        if on:
            n = stream._db.execute("UPDATE sources SET paused = CASE WHEN domain IN (SELECT domain FROM outlet_country "
                                   "WHERE country='intl') THEN 1 ELSE 0 END").rowcount
            if not stream.meta("intl_paused_since"):
                stream._db.execute("INSERT INTO meta(k, v) VALUES ('intl_paused_since', ?) ON CONFLICT(k) DO UPDATE "
                                   "SET v=excluded.v", (datetime.now(WIB).strftime("%Y-%m-%d"),))
        else:
            n = stream._db.execute("UPDATE sources SET paused = 0, next_check = MIN(next_check, ?) WHERE paused = 1",
                                   (time.time(),)).rowcount
            since = stream.meta("intl_paused_since")
            if since:                                   # what the catch-up should cover
                stream._db.execute("INSERT INTO meta(k, v) VALUES ('intl_missed', ?) ON CONFLICT(k) DO UPDATE SET "
                                   "v=excluded.v", (json.dumps({"start": since, "end": datetime.now(WIB).strftime(
                                       "%Y-%m-%d")}),))
                stream._db.execute("DELETE FROM meta WHERE k='intl_paused_since'")
        stream._db.commit()
    return n


def mark(stream, domains: list[str], country: str) -> int:
    """The admin's own decision for outlets ('id', 'intl', or '' = back to automatic)."""
    from .media_db import clean_domain
    if country not in ("id", "intl", ""):
        raise ValueError("Mark as Indonesian or international.")
    ensure(stream)
    ds = [d for d in (clean_domain(x) for x in domains or []) if d]
    with stream.lock:
        for d in ds:
            stream._db.execute("INSERT INTO outlet_country(domain, country, mark, checked) VALUES (?,?,?,?) "
                               "ON CONFLICT(domain) DO UPDATE SET mark=excluded.mark, country=CASE WHEN excluded.mark "
                               "!= '' THEN excluded.mark ELSE outlet_country.country END",
                               (d, country or "unknown", country, time.time()))
        stream._db.commit()
    apply_pause(stream)
    return len(ds)


def status(stream) -> dict:
    ensure(stream)
    cnt = {r["country"]: r["n"] for r in stream.rq("SELECT country, COUNT(*) n FROM outlet_country GROUP BY country")}
    missed = stream.meta("intl_missed")
    from .stream import settings
    return {"paused": bool(settings().get("pause_international")), "paused_since": stream.meta("intl_paused_since"),
            "missed": json.loads(missed) if missed else None, "international": cnt.get("intl", 0),
            "indonesian": cnt.get("id", 0), "undecided": cnt.get("unknown", 0),
            "classified": stream.meta("intl_classified"),
            "sample": [r["domain"] for r in stream.rq("SELECT domain FROM outlet_country WHERE country='intl' "
                                                      "ORDER BY n DESC LIMIT 12")]}


def catch_up(stream, start: str, end: str) -> dict:
    """Everything the international outlets published in a period: up to 7 days back — the late-article sweep for
    them (a command to the stream's worker); longer — a media-archive job per outlet."""
    from . import backfill
    for v in (start, end):
        if not re.match(r"\d{4}-\d{2}-\d{2}$", v or ""):
            raise ValueError("Dates as YYYY-MM-DD.")
    if start > end:
        raise ValueError("The start date is after the end date.")
    doms = international(stream)
    if not doms:
        raise ValueError("No international outlets are known yet.")
    days_back = (datetime.now(WIB).date() - datetime.strptime(start, "%Y-%m-%d").date()).days
    if days_back <= 7 and end >= (datetime.now(WIB) - timedelta(days=1)).strftime("%Y-%m-%d"):
        from .stream import command
        command("catchup_domains", json.dumps({"days": max(1, days_back), "domains": doms}))
        how, jobs = "sweep", 0
    else:
        jobs = 0
        for d in doms:
            backfill.start(stream, d, start, end)
            jobs += 1
        how = "archive"
    stream.x("DELETE FROM meta WHERE k='intl_missed'")
    return {"outlets": len(doms), "how": how, "jobs": jobs, "start": start, "end": end}
