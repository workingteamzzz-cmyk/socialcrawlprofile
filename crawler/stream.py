"""Live news stream: an always-on engine that reads EVERY new article from the registered media — not a search that
runs and ends.

How it works (several engines at once, all in this process):
- RSS engine: every working RSS feed of the media dictionary (the curated ones + every feed the multi-source engine
  discovered) is checked every few minutes; a feed that did not change answers "304 not modified" (almost free).
- Sitemap engine: every outlet's news sitemap is checked every ~30 minutes; outlets that publish nothing new are
  checked less often (up to every 6 h), dead ones once a day — so the busy outlets get the attention.
- Readers: a pool of threads reads the new articles (4-layer extraction, people, image, author, date), newest
  first, one page at a time per outlet with a pause between pages (polite: no outlet is hammered, nobody blocks us).
- Every article read goes into the stream database (kept `retention_days`); the ones matching a keyword (the stream's
  own keywords + every report topic) also go to the reporting database at once: Home dashboard, Reports, Excel.

State: APP_DATA/stream/ (stream.db, settings.json). Admins start/stop it and set its numbers in the dashboard.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
DEFAULTS = {"enabled": False, "threads": 8, "discovery_threads": 8, "rss_minutes": 10, "sitemap_minutes": 60,
            "catchup_days": 2, "catchup_hour": 1, "weekly_report": True, "report_weekday": 0, "report_hour": 7,
            "health_on_start": True, "health_weekday": 6, "health_hour": 2,
            "top_n": 1000, "top2_n": 2000, "active2_hours": 2, "semi_hours": 4, "rare_hours": 24, "inactive_days": 3, "dead_days": 7,
            "retention_days": 7, "max_age_hours": 48, "all_links": True, "old_per_check": 1000, "homepage": True, "domain_gap": 1.5, "rss": True, "sitemaps": True,
            "keywords": [], "robots": True, "exclude_domains": []}
LIMITS = {"threads": (2, 64), "discovery_threads": (2, 48), "rss_minutes": (2, 240), "sitemap_minutes": (10, 720),
          "old_per_check": (0, 20000),
          "catchup_days": (0, 7), "catchup_hour": (0, 23), "health_weekday": (0, 6), "health_hour": (0, 23), "report_weekday": (0, 6), "report_hour": (0, 23),
          "top_n": (50, 20000), "top2_n": (0, 20000), "active2_hours": (1, 24), "semi_hours": (1, 48), "rare_hours": (2, 168), "inactive_days": (1, 30),
          "dead_days": (1, 60),
          "retention_days": (1, 90), "max_age_hours": (6, 168)}
# Tiers — how often an outlet is checked, from how much it published (new articles found) in the last days:
#   active    the top `top_n` outlets of the last 7 days     sitemap every `sitemap_minutes` (60), RSS every `rss_minutes` (10)
#   semi      published in the last 7 days, not in the top    every `semi_hours` (4 h), RSS every hour
#   rare      nothing in 7 days, something in the last 30     every `rare_hours` (24 h)
#   inactive  nothing in 30 days (or never)                   every `inactive_days` (3 days)
#   dead      unreachable 3 times in a row                    every `dead_days` (7 days)
#   new       never checked yet                               checked once right away, then classified
# Recomputed every 10 minutes; an outlet that suddenly publishes is promoted at once (semi at least).
TIERS = ("active", "active2", "semi", "rare", "inactive", "dead", "new")
MAX_INTERVAL = 6 * 3600
DEAD_INTERVAL = 24 * 3600
SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
  key TEXT PRIMARY KEY, url TEXT, domain TEXT, title TEXT DEFAULT '', source TEXT DEFAULT '',
  feed_date TEXT DEFAULT '', feed_time TEXT DEFAULT '', discovered REAL, status TEXT DEFAULT 'new', fetched REAL,
  pub_date TEXT DEFAULT '', pub_time TEXT DEFAULT '', author TEXT DEFAULT '', summary TEXT DEFAULT '',
  content BLOB, image TEXT DEFAULT '', influencer TEXT DEFAULT '', persons TEXT DEFAULT '', matched TEXT DEFAULT '',
  error TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS a_status ON articles(status, discovered);
CREATE INDEX IF NOT EXISTS a_fetched ON articles(fetched);
CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, kind TEXT, domain TEXT, url TEXT DEFAULT '', next_check REAL, interval REAL,
  hits INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, etag TEXT DEFAULT '', modified TEXT DEFAULT '', checked REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS s_next ON sources(next_check);
CREATE INDEX IF NOT EXISTS s_domain ON sources(domain);
CREATE INDEX IF NOT EXISTS a_succ ON articles(status, fetched);
CREATE INDEX IF NOT EXISTS a_pub ON articles(status, pub_date);
CREATE INDEX IF NOT EXISTS a_disc ON articles(discovered);
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, t REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS feed_outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id TEXT, article TEXT, tries INTEGER,
  next_try REAL, created REAL);
CREATE INDEX IF NOT EXISTS fo_next ON feed_outbox(next_try);
CREATE TABLE IF NOT EXISTS commands(id INTEGER PRIMARY KEY AUTOINCREMENT, cmd TEXT, arg TEXT DEFAULT '', t REAL);
CREATE TABLE IF NOT EXISTS site_health(domain TEXT PRIMARY KEY, checked REAL, alive INTEGER, sitemaps INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS domain_days(domain TEXT, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(domain, day));
CREATE TABLE IF NOT EXISTS hourly(
  hour TEXT PRIMARY KEY, found INTEGER DEFAULT 0, read INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  matched INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0);
"""


def _dir() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "stream")


def _hour(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), WIB).strftime("%Y-%m-%d %H")


def norm_key(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"[?&](utm_[a-z]+|fbclid|gclid|amp|page)=[^&]*", "", u, flags=re.I)
    u = re.sub(r"^https?://(www\.|m\.|amp\.)?", "", u, flags=re.I)
    return u.rstrip("/?&").lower()


# ---------- settings ----------
def settings() -> dict:
    try:
        with open(os.path.join(_dir(), "settings.json"), encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        got = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (got or {}).items() if k in DEFAULTS})
    return out


def save_settings(data: dict) -> dict:
    cur = settings()
    for k, v in (data or {}).items():
        if k not in DEFAULTS or k == "enabled":
            continue
        if k in LIMITS:
            lo, hi = LIMITS[k]
            try:
                v = max(lo, min(hi, int(float(v))))
            except (TypeError, ValueError):
                raise ValueError(f"{k}: a number from {lo} to {hi}.")
        elif k == "domain_gap":
            v = max(0.5, min(30.0, float(v)))
        elif k in ("rss", "sitemaps", "robots", "weekly_report", "health_on_start", "all_links", "homepage"):
            v = bool(v)
        elif k == "exclude_domains":
            items = v if isinstance(v, list) else re.split(r"[\n,\s]+", str(v or ""))
            v = sorted({re.sub(r"^(https?://)?(www\.)?", "", x.strip().lower()).split("/")[0] for x in items if str(x).strip()})[:2000]
        elif k == "keywords":
            items = v if isinstance(v, list) else re.split(r"[\n,]", str(v or ""))
            v = list(dict.fromkeys(x.strip() for x in items if str(x).strip()))[:500]
        cur[k] = v
    _write_settings(cur)
    return cur


def _write_settings(cur: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = os.path.join(_dir(), "settings.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(_dir(), "settings.json"))


# ---------- the engine ----------
class Stream:
    def __init__(self, folder: str | None = None):
        self.folder = folder or _dir()
        os.makedirs(self.folder, exist_ok=True)
        self.db_path = os.path.join(self.folder, "stream.db")
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        from .backfill import SCHEMA as ARCHIVE_SCHEMA
        self._db.executescript(ARCHIVE_SCHEMA)
        for sql in ("ALTER TABLE hourly ADD COLUMN skipped INTEGER DEFAULT 0",          # databases from v23
                    "ALTER TABLE sources ADD COLUMN tier TEXT DEFAULT 'new'",
                    "ALTER TABLE articles ADD COLUMN sentiment TEXT DEFAULT ''"):            # databases before v33
            try:
                self._db.execute(sql)
            except sqlite3.OperationalError:
                pass
        self._db.commit()
        self.lock = threading.RLock()
        self._ctl = threading.Lock()           # start / stop never overlap
        self._local = threading.local()        # per-thread read-only connections (reads without the lock)
        self.qn = 0                            # articles waiting in the in-memory queue
        self.gap_override: dict = {}           # outlet -> its own pause (a media-archive job's setting)
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.running = False
        self.started = 0.0
        self.counts = {"found": 0, "read": 0, "failed": 0, "matched": 0, "skipped": 0}   # this hour, flushed often
        self.robots: dict = {}
        self.tiers: dict = {}                  # domain -> tier (recomputed every 10 minutes)
        self.catchup = {"running": False, "done": 0, "total": 0, "found": 0, "started": 0, "finished": 0}
        self.catchup_now = threading.Event()
        self.health = {"running": False, "done": 0, "total": 0, "alive": 0, "down": 0, "started": 0, "finished": 0}
        self.health_now = threading.Event()
        self.export = {"running": False, "done": 0, "total": 0, "file": "", "error": ""}
        self.recent = deque(maxlen=50000)       # timestamps of articles read (rate per minute)
        self.inflight_sources: set = set()
        self.queued: set = set()               # article keys handed to the readers
        self.by_domain: dict[str, deque] = {}
        self.busy_domains: set = set()
        self.next_ok: dict[str, float] = {}
        self.rr = deque()                      # round-robin over domains with work
        self.kw_cache = (0.0, [])
        self.last_error = ""
        self.cfg = settings()

    # ----- small db helpers -----
    def q(self, sql: str, args=()):
        with self.lock:
            return self._db.execute(sql, args).fetchall()

    def rq(self, sql: str, args=()):
        """A read on this thread's own read-only connection: big reads never hold up the readers (WAL)."""
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.c = c
        return c.execute(sql, args).fetchall()

    def x(self, sql: str, args=()):
        with self.lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    def bump(self, key: str, n: int = 1) -> None:
        with self.lock:
            self.counts[key] += n

    def flush_counts(self) -> None:
        with self.lock:
            c, self.counts = self.counts, {k: 0 for k in self.counts}
            if any(c.values()):
                self._db.execute("INSERT INTO hourly(hour) VALUES (?) ON CONFLICT(hour) DO NOTHING", (_hour(),))
                self._db.execute("UPDATE hourly SET found=found+?, read=read+?, failed=failed+?, matched=matched+?, "
                                 "skipped=skipped+? WHERE hour=?",
                                 (c["found"], c["read"], c["failed"], c["matched"], c["skipped"], _hour()))
                self._db.commit()

    # ----- sources -----
    def sync_sources(self, log=print) -> dict:
        """Every RSS feed and every outlet of the media dictionary becomes a source (new ones only)."""
        from .medmas.media import RSS_FEEDS
        from . import media_db
        from .paths import APP_DATA
        reg = media_db.registry()                           # the registered media (admins edit it: Media Database)
        feeds = {d: list(u) for d, u in RSS_FEEDS.items() if d in reg}
        known = os.path.join(APP_DATA, "news_multi", "medmascrawler.db")
        if os.path.exists(known):
            try:
                c = sqlite3.connect(f"file:{known}?mode=ro", uri=True, timeout=10)
                for d, u in c.execute("SELECT domain, feed_url FROM feed_stats WHERE feed_url != ''"):
                    if d not in reg:
                        continue
                    feeds.setdefault(d, [])
                    if u not in feeds[d]:
                        feeds[d].append(u)
                c.close()
            except sqlite3.Error:
                pass
        now = time.time()
        rss_iv, sm_iv = self.cfg["rss_minutes"] * 60, self.cfg["sitemap_minutes"] * 60
        rows = [(f"rss:{u}", "rss", d, u, now + random.random() * rss_iv, rss_iv)
                for d, urls in feeds.items() for u in urls]
        doms = sorted(reg)
        rows += [(f"sm:{d}", "sitemap", d, "", now + random.random() * sm_iv, sm_iv) for d in doms]
        with self.lock:
            before = self._db.total_changes
            self._db.executemany("INSERT OR IGNORE INTO sources(id, kind, domain, url, next_check, interval) "
                                 "VALUES (?,?,?,?,?,?)", rows)
            self._db.commit()
            added = self._db.total_changes - before
        return {"rss": sum(len(u) for u in feeds.values()), "sitemaps": len(doms), "added": added}

    def add_outlets(self, domains: list[str]) -> None:
        """Newly registered outlets: their sitemap is looked at right away."""
        now = time.time()
        with self.lock:
            self._db.executemany("INSERT OR IGNORE INTO sources(id, kind, domain, url, next_check, interval) "
                                 "VALUES (?,?,?,?,?,?)", [(f"sm:{d}", "sitemap", d, "", now, 3600) for d in domains])
            self._db.commit()

    def remove_outlets(self, domains: list[str]) -> None:
        """Removed from the registered media: no feed, no sitemap, nothing waiting to be read."""
        with self.lock:
            for d in domains:
                self._db.execute("DELETE FROM sources WHERE domain=?", (d,))
                self._db.execute("DELETE FROM articles WHERE domain=? AND status='new'", (d,))
                q = self.by_domain.pop(d, None)
                for j in q or []:
                    self.queued.discard(j["key"])
                    self.qn -= 1
                self.tiers.pop(d, None)
            self._db.commit()

    def due_sources(self, limit: int = 64) -> list:
        kinds = [k for k, on in (("rss", self.cfg["rss"]), ("sitemap", self.cfg["sitemaps"])) if on]
        if not kinds:
            return []
        return self.q(f"SELECT * FROM sources WHERE next_check <= ? AND kind IN ({','.join('?' * len(kinds))}) "
                      "ORDER BY next_check LIMIT ?", (time.time(), *kinds, limit))

    def interval(self, kind: str, tier: str) -> float:
        c = self.cfg
        sm = {"active": c["sitemap_minutes"] * 60, "active2": c.get("active2_hours", 2) * 3600,
              "semi": c["semi_hours"] * 3600, "rare": c["rare_hours"] * 3600,
              "inactive": c["inactive_days"] * 86400, "dead": c["dead_days"] * 86400, "new": c["sitemap_minutes"] * 60}
        if kind == "rss":                                   # a feed is almost free to check (304): more often
            return {"active": c["rss_minutes"] * 60, "active2": min(sm["active2"], 1800),
                    "semi": min(sm["semi"], 3600)}.get(tier, sm[tier])
        return sm[tier]

    def tier_of(self, src) -> str:
        t = self.tiers.get(src["domain"])
        if t:
            return t
        try:
            return src["tier"] or "new"
        except (IndexError, KeyError):
            return "new"

    def reschedule(self, src, new: int, failed: bool, extra: dict | None = None) -> None:
        tier = self.tier_of(src)
        fails = (src["fails"] + 1) if failed else 0
        if failed:
            tier = "dead" if fails >= 3 else tier
            iv = self.interval(src["kind"], tier) if fails >= 3 else min(3600 * fails, self.interval(src["kind"], tier))
        else:
            if new and tier in ("rare", "inactive", "new", "dead"):
                tier = "semi"                                # it publishes again: promoted right away
            elif not new and tier == "new":
                tier = "inactive"                            # first check found nothing recent
            iv = self.interval(src["kind"], tier)
        if tier in ("semi", "inactive", "dead") and not failed:
            with self.lock:
                if self.tiers.get(src["domain"]) in (None, "new", "rare", "inactive", "dead"):
                    self.tiers[src["domain"]] = tier
        sets = {"next_check": time.time() + iv * random.uniform(0.9, 1.1), "interval": iv, "fails": fails,
                "checked": time.time(), "hits": src["hits"] + new, "tier": tier, **(extra or {})}
        self.x(f"UPDATE sources SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), src["id"]))

    def compute_tiers(self) -> dict:
        """Rank the outlets by new articles found in the last 7 days: the top `top_n` are active, the rest of the
        7-day publishers semi, 30-day publishers rare, the others inactive; unreachable ones stay dead. Sources whose
        tier now asks for an earlier check are moved forward."""
        today = datetime.now(WIB)
        d7, d30 = (today - timedelta(days=6)).strftime("%Y-%m-%d"), (today - timedelta(days=29)).strftime("%Y-%m-%d")
        c7 = {r["domain"]: r["n"] for r in self.rq("SELECT domain, SUM(n) n FROM domain_days WHERE day >= ? "
                                                     "GROUP BY domain HAVING n > 0", (d7,))}
        c30 = {r["domain"] for r in self.rq("SELECT domain FROM domain_days WHERE day >= ? GROUP BY domain "
                                             "HAVING SUM(n) > 0", (d30,))}
        ranked = sorted(c7, key=lambda d: -c7[d])
        top = set(ranked[:self.cfg["top_n"]])
        top2 = set(ranked[self.cfg["top_n"]:self.cfg["top_n"] + self.cfg.get("top2_n", 2000)])
        rows = self.rq("SELECT domain, MAX(CASE WHEN kind='sitemap' THEN fails END) f, MAX(checked) c FROM sources "
                       "GROUP BY domain")                      # a broken RSS feed alone never makes an outlet dead
        tiers = {}
        for r in rows:
            d = r["domain"]
            tiers[d] = ("active" if d in top else "active2" if d in top2 else "semi" if d in c7 else "rare" if d in c30 else
                        "dead" if (r["f"] or 0) >= 3 else "inactive" if r["c"] else "new")
        with self.lock:
            self.tiers = tiers
            for t in TIERS:
                doms = [d for d, x in tiers.items() if x == t]
                if doms:
                    self._db.executemany("UPDATE sources SET tier=? WHERE domain=?", [(t, d) for d in doms])
            for kind in ("rss", "sitemap"):                 # promoted: the next check comes sooner
                for t in ("active", "active2", "semi", "rare"):
                    self._db.execute("UPDATE sources SET next_check = MIN(next_check, checked + ?) "
                                     "WHERE kind=? AND tier=? AND checked > 0", (self.interval(kind, t), kind, t))
            self._db.commit()
        return {t: sum(1 for x in tiers.values() if x == t) for t in TIERS}

    def cutoff(self) -> str:
        """The oldest publication day the stream takes: max_age_hours, or the catch-up's days if longer."""
        hours = max(self.cfg["max_age_hours"], (self.cfg.get("catchup_days") or 0) * 24 + 24)
        return (datetime.now(WIB) - timedelta(hours=hours)).strftime("%Y-%m-%d")

    def add_found(self, items: list[dict], source: str, front: bool = True) -> int:
        """New article links -> the reading queue (front: fresh news first; catch-up links go to the back).
        Only articles, only from the days the stream covers."""
        from .medmas.filters import is_article_link
        from .online_clean import is_junk_page
        cutoff = self.cutoff()
        take_all = bool(self.cfg.get("all_links", True))
        room_old = int(self.cfg.get("old_per_check", 1000)) if take_all else 0
        now, rows = time.time(), []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http") or not is_article_link(url) or is_junk_page("", url):
                continue
            d = (it.get("date") or "")[:10]
            old = bool(d and d < cutoff) or bool(it.get("old"))
            if old and not take_all:
                continue
            rows.append(((norm_key(url), url, it.get("domain") or "", (it.get("title") or "")[:500], source, d,
                          (it.get("time") or "")[:5], now), old))
        if not rows:
            return 0
        fresh = []
        with self.lock:
            for row, old in rows:
                if old and room_old <= 0:
                    continue                                   # older links: a share per visit, the rest next time
                if self._db.execute("SELECT 1 FROM seen WHERE key=?", (row[0],)).fetchone():
                    continue                                   # read before and purged since: not again
                cur = self._db.execute("INSERT OR IGNORE INTO articles(key, url, domain, title, source, feed_date, "
                                       "feed_time, discovered) VALUES (?,?,?,?,?,?,?,?)", row)
                if cur.rowcount:
                    if old:
                        room_old -= 1
                    fresh.append((row, old))
            per_dom: dict = {}
            for row, old in fresh:
                if not old:                                   # activity = new articles, not the back catalogue
                    per_dom[row[2]] = per_dom.get(row[2], 0) + 1
            day = datetime.now(WIB).strftime("%Y-%m-%d")
            self._db.executemany("INSERT INTO domain_days(domain, day, n) VALUES (?,?,?) ON CONFLICT(domain, day) "
                                 "DO UPDATE SET n = n + excluded.n", [(d, day, n) for d, n in per_dom.items() if d])
            self._db.commit()
            self.counts["found"] += len(fresh)
            if self.running:                     # straight to the readers, at the front: newest first, no waiting
                for (key, url, dom, title, _src, fd, ft, _t), old in fresh:
                    if self.qn >= 4000 and (old or not front):
                        continue                       # the queue is full: it waits in the database (refill)
                    self._enqueue({"key": key, "url": url, "domain": dom, "title": title, "feed_date": fd,
                                   "feed_time": ft}, front=front and not old)       # older ones wait behind the news
        return len(fresh)

    def _enqueue(self, job: dict, front: bool = False) -> None:
        if job["key"] in self.queued:
            return
        self.queued.add(job["key"])
        self.qn += 1
        dom = job["domain"] or norm_key(job["url"]).split("/")[0]
        if dom not in self.by_domain:
            self.by_domain[dom] = deque()
            self.rr.appendleft(dom) if front else self.rr.append(dom)
        (self.by_domain[dom].appendleft if front else self.by_domain[dom].append)(job)

    def check_rss(self, src) -> tuple[int, bool, dict]:
        import requests
        import feedparser
        from .medmas.discovery import _HEADERS, entry_datetime
        h = dict(_HEADERS)
        if src["etag"]:
            h["If-None-Match"] = src["etag"]
        if src["modified"]:
            h["If-Modified-Since"] = src["modified"]
        try:
            r = requests.get(src["url"], headers=h, timeout=12)
        except Exception:  # noqa: BLE001
            return 0, True, {}
        if r.status_code == 304:
            return 0, False, {}
        if not r.ok or not r.content:
            return 0, True, {}
        feed = feedparser.parse(r.content)
        items = []
        for e in feed.entries:
            fd, ft = entry_datetime(e)
            items.append({"url": getattr(e, "link", ""), "title": getattr(e, "title", ""), "date": fd, "time": ft,
                          "domain": src["domain"]})
        extra = {"etag": r.headers.get("ETag", "")[:200], "modified": r.headers.get("Last-Modified", "")[:100]}
        return self.add_found(items, "rss"), False, extra

    def check_sitemap(self, src, catchup: int = 0) -> tuple[int, bool, dict]:
        """An outlet's sitemaps -> new links. catchup = N: the late-article sweep — also the archive sitemaps of the
        last N days (sitemap-2026-11-19.xml, …/2026/11/19/…), more sub-sitemaps read, links queued behind the news."""
        from .medmas.discovery import sitemaps as sm
        dom = src["domain"]
        roots = json.loads(src["url"]) if src["url"] else None
        extra = {}
        if roots is None:                                    # first visit: find the outlet's sitemaps once
            found, alive = sm._sitemaps_from_robots(dom)
            if not alive:
                return 0, True, {}
            cands = found or [f"https://{dom}{p}" for p in sm.COMMON_PATHS]
            cands.sort(key=lambda u: (0 if ("news" in u.lower() or "berita" in u.lower()) else 1))
            roots = cands[:6]
            extra["url"] = json.dumps(roots)
        now = datetime.now(WIB)
        back = [now - timedelta(days=i) for i in range(0, max(2, catchup) + 1)]
        months = {d.strftime("%Y-%m") for d in back}
        stamps = [f for d in back[1:catchup + 1] for f in (d.strftime("%Y-%m-%d"), d.strftime("%Y%m%d"),
                                                            d.strftime("%Y/%m/%d"), d.strftime("%Y/%m/") + f"{d.day}")]
        items, ok_any, queue, seen, reads = [], False, list(roots), set(), 0
        while queue and reads < (14 if catchup else 6):
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            try:
                body = sm._fetch(u, timeout=10)
            except Exception:  # noqa: BLE001
                continue
            reads += 1
            ok_any = True
            kind, data = sm._parse_sitemap(body)
            if kind == "index":
                ranked = sorted(((x, sm._sitemap_priority(x, frozenset(months))) for x in data),
                                key=lambda t: (t[1] is None, t[1] or 0))
                subs = [x for x, p in ranked if p is not None and (p < 0 or any(m in x for m in months))]
                if catchup:                                  # the day's own archive sitemaps first
                    dated = [x for x, p in ranked if p is not None and any(t in x for t in stamps)]
                    subs = dated + [x for x in subs if x not in dated]
                queue.extend((subs or [x for x, p in ranked if p is not None][-2:])[:8 if catchup else 3])
                continue
            news = "news" in u.lower() or "berita" in u.lower()
            for e in data:
                if e.get("date") or news:                    # undated entries of a news sitemap: the last 48 h by rule
                    items.append({**e, "domain": dom})
                else:                                        # undated elsewhere: taken too, behind the news
                    items.append({**e, "domain": dom, "old": True})
        if self.cfg.get("homepage", True) and not catchup:   # the front page: articles not (yet) in any sitemap
            got = self.homepage_links(dom)
            if got is not None:
                ok_any = True
                first = not (src["checked"] if "checked" in src.keys() else 0)
                items += [{**g, "old": True} for g in got] if first else got   # first visit: the menu, not news
        if not ok_any:
            if roots and src["url"]:                         # known sitemaps all failed: look for them again next time
                extra["url"] = ""
            return 0, True, extra
        return self.add_found(items, "catchup" if catchup else "sitemap", front=not catchup), False, extra

    def homepage_links(self, dom: str) -> list | None:
        """The article links on an outlet's front page (same site only). None = the page did not answer."""
        from urllib.parse import urljoin, urlparse
        from .medmas.discovery import sitemaps as sm
        try:
            html = sm._fetch(f"https://{dom}/", timeout=10).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
        out, seen = [], set()
        for m in re.finditer(r"""<a\b[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", html[:3_000_000], re.I | re.S):
            u = urljoin(f"https://{dom}/", m.group(1).strip())
            host = (urlparse(u).hostname or "").lower()
            if not (host == dom or host.endswith("." + dom)) or u in seen:
                continue
            seen.add(u)
            title = re.sub(r"<[^>]+>|\s+", " ", m.group(2)).strip()[:300]
            out.append({"url": u, "title": title, "domain": dom})
        return out

    def check_source(self, src) -> int:
        if self.excluded("https://" + (src["domain"] or "")):           # an excluded outlet: not even its feed
            self.x("UPDATE sources SET next_check=?, checked=? WHERE id=?", (time.time() + DEAD_INTERVAL, time.time(), src["id"]))
            return 0
        try:
            new, failed, extra = (self.check_rss if src["kind"] == "rss" else self.check_sitemap)(src)
        except Exception as e:  # noqa: BLE001
            new, failed, extra = 0, True, {}
            self.last_error = f"{src['id']}: {str(e)[:120]}"
        self.reschedule(src, new, failed, extra)
        return new

    # ----- readers -----
    def refill(self) -> None:
        with self.lock:
            if self.qn > 4000:
                return
            rows = self._db.execute("SELECT key, url, domain, title, feed_date, feed_time, source FROM articles "
                                    "WHERE status='new' ORDER BY discovered DESC LIMIT 3000").fetchall()
            for r in rows:                       # left-overs (e.g. after a restart), newest first
                self._enqueue(dict(r))

    def next_job(self):
        """The next article to read: round robin over outlets, one page at a time per outlet, a pause between."""
        with self.lock:
            now = time.time()
            for _ in range(len(self.rr)):
                dom = self.rr[0]
                self.rr.rotate(-1)
                q = self.by_domain.get(dom)
                if not q:
                    self.by_domain.pop(dom, None)
                    try:
                        self.rr.remove(dom)
                    except ValueError:
                        pass
                    continue
                if dom in self.busy_domains or self.next_ok.get(dom, 0) > now:
                    continue
                self.busy_domains.add(dom)
                self.qn -= 1
                return dom, q.popleft()
        return None

    def keywords(self) -> list[str]:
        if time.time() - self.kw_cache[0] < 60:
            return self.kw_cache[1]
        kws = list(self.cfg.get("keywords") or [])
        try:
            from . import archive
            for t in archive.topics():
                kws += t.get("keywords") or []
        except Exception:  # noqa: BLE001
            pass
        kws = list(dict.fromkeys(k for k in kws if k))
        self.kw_cache = (time.time(), kws)
        return kws

    def excluded(self, url: str) -> bool:
        host = norm_key(url).split("/")[0].split(":")[0]
        return any(host == d or host.endswith("." + d) for d in self.cfg.get("exclude_domains") or [])

    def robots_ok(self, url: str) -> bool:
        """The outlet's robots.txt allows this page (read once a day per outlet; no robots.txt = allowed)."""
        if not self.cfg.get("robots", True):
            return True
        from urllib import robotparser
        from urllib.parse import urlparse
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self.lock:
            hit = self.robots.get(base)
        if not hit or time.time() - hit[0] > 86400:
            rp = None
            try:
                import requests
                from .medmas.discovery import _HEADERS
                r = requests.get(base + "/robots.txt", headers=_HEADERS, timeout=8)
                if r.status_code == 200 and r.text:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:  # noqa: BLE001  unreachable robots.txt: allowed, like search engines do
                rp = None
            hit = (time.time(), rp)
            with self.lock:
                self.robots[base] = hit
        return hit[1] is None or hit[1].can_fetch("*", url)

    def read(self, job: dict) -> str:
        from .medmas.extract import extract_article
        from .medmas.filters import keyword_excluded, keyword_match, url_slug
        if self.excluded(job["url"]) or not self.robots_ok(job["url"]):
            self.x("UPDATE articles SET status='skipped', fetched=? WHERE key=?", (time.time(), job["key"]))
            self.bump("skipped")
            return "skipped"
        res = extract_article(job["url"], 20)
        d = str(res.get("date") or "")[:10] or job.get("feed_date") or ""
        t = str(res.get("time") or "")[:5] or job.get("feed_time") or ""
        from .online_clean import is_junk_page
        status = "success" if res.get("content") and res.get("is_article", True) and not res.get("is_index") else \
            ("failed" if not res.get("content") else "filtered")
        if status == "success" and is_junk_page(res.get("title") or job.get("title") or "", job["url"]):
            status = "filtered"                              # privacy / terms / a front page: not an article
        cutoff = self.cutoff()
        archive_job = (job.get("source") or "").startswith("backfill:")
        if archive_job and status == "success":               # a media-archive job: its own period decides
            bj = self.rq("SELECT start, end, status FROM backfill WHERE id=?", (job["source"][9:],))
            if not bj or bj[0]["status"] == "cancelled" or (re.match(r"\d{4}-\d{2}-\d{2}$", d)
                                                           and not bj[0]["start"] <= d <= bj[0]["end"]):
                status = "filtered"
        if status == "success" and not archive_job and not self.cfg.get("all_links", True) and \
                re.match(r"\d{4}-\d{2}-\d{2}$", d) and d < cutoff:
            status = "old"
        matched = []
        if status == "success":
            slug = url_slug(job["url"])
            for kw in self.keywords():
                if keyword_match(kw, res["title"], res["content"], slug) and \
                        not keyword_excluded(kw, res["title"], res["content"], slug):
                    matched.append(kw)
        sentiment = ""
        if status == "success":
            try:
                from .templates import analyze
                sentiment = (analyze(f"{res.get('title') or ''}. {res.get('summary') or ''}")[0] or "neutral").lower()
            except Exception:  # noqa: BLE001
                sentiment = ""
        self.x("UPDATE articles SET sentiment=? WHERE key=?", (sentiment, job["key"]))
        self.x("UPDATE articles SET status=?, fetched=?, pub_date=?, pub_time=?, title=?, author=?, summary=?, content=?,"
               " image=?, influencer=?, persons=?, matched=?, error=? WHERE key=?",
               (status, time.time(), d, t, res.get("title") or job.get("title") or "", res.get("author") or "",
                res.get("summary") or "", zlib.compress((res.get("content") or "").encode("utf-8")) if status == "success"
                else None, res.get("image") or "", res.get("influencer") or "", res.get("persons") or "",
                ", ".join(matched), (res.get("error") or "")[:200], job["key"]))
        if status == "success":
            self.bump("read")
            self.recent.append(time.time())
        elif status == "failed":
            self.bump("failed")
        if status == "success":
            try:
                from . import feeds
                feeds.match_and_queue(self, job, res, d, t, sentiment)       # client feeds: pushed within seconds
            except Exception as e:  # noqa: BLE001
                self.last_error = f"client feeds: {str(e)[:120]}"
        if matched:
            self.bump("matched")
            try:
                self.to_archive(job, res, d, t, matched)
            except Exception as e:  # noqa: BLE001  the article itself was read fine
                self.last_error = f"reports: {str(e)[:120]}"
        return status

    def to_archive(self, job, res, d, t, matched) -> None:
        from . import archive
        full = f"{d} {t}" if re.match(r"\d{4}-\d{2}-\d{2}$", d) and re.match(r"\d{2}:\d{2}$", t) else ""
        row = {"url": job["url"], "title": res.get("title") or job.get("title") or "", "media": res.get("media") or "",
               "published": d, "published_full": full, "author": res.get("author") or "", "text": res.get("content") or "",
               "description": res.get("summary") or "", "image": res.get("image") or "",
               "influencer": res.get("influencer") or "", "person": res.get("persons") or "", "sentiment": "",
               "keyword": ", ".join(matched)}
        archive.add_rows("online", [row], WIB)

    def reader_loop(self) -> None:
        ev = self.stop_event                                 # this run's event: a restart never revives old threads
        while not ev.is_set():
            got = self.next_job()
            if not got:
                ev.wait(0.3)
                continue
            dom, job = got
            try:
                self.read(job)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"read {job['url']}: {str(e)[:120]}"
                try:
                    self.x("UPDATE articles SET status='failed', fetched=?, error=? WHERE key=?",
                           (time.time(), str(e)[:200], job["key"]))
                except sqlite3.Error:
                    pass
                self.bump("failed")
            finally:
                with self.lock:
                    self.busy_domains.discard(dom)
                    self.next_ok[dom] = time.time() + float(self.gap_override.get(dom) or self.cfg["domain_gap"])
                    self.queued.discard(job["key"])

    # ----- discovery + housekeeping -----
    def discovery_loop(self) -> None:
        ev = self.stop_event
        with ThreadPoolExecutor(max_workers=self.cfg["discovery_threads"], thread_name_prefix="stream-src") as ex:
            while not ev.is_set():
                try:
                    with self.lock:
                        room = self.cfg["discovery_threads"] * 2 - len(self.inflight_sources)
                    if room > 0:
                        for src in self.due_sources(room):
                            with self.lock:
                                if src["id"] in self.inflight_sources:
                                    continue
                                self.inflight_sources.add(src["id"])
                            ex.submit(self._check_one, src, ev)
                except Exception as e:  # noqa: BLE001  a database hiccup never ends discovery
                    self.last_error = f"discovery: {str(e)[:120]}"
                ev.wait(1.0)

    def _check_one(self, src, ev=None) -> None:
        try:
            if not (ev or self.stop_event).is_set():
                self.check_source(src)
        finally:
            with self.lock:
                self.inflight_sources.discard(src["id"])

    def house_loop(self) -> None:
        ev = self.stop_event
        last_purge = last_tiers = 0.0
        while not ev.is_set():
            for name, step in (("counts", self.flush_counts), ("queue", self.refill), ("sentiment", self.fill_sentiment)):
                try:
                    step()
                except Exception as e:  # noqa: BLE001  each step on its own: one failing never stops the others
                    self.last_error = f"{name}: {str(e)[:120]}"
            if time.time() - last_tiers > 600:
                last_tiers = time.time()
                try:
                    self.compute_tiers()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"tiers: {str(e)[:120]}"
            if time.time() - last_purge > 1800:
                last_purge = time.time()
                try:
                    self.purge(ev)
                    self.cfg = settings()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"purge: {str(e)[:120]}"
            ev.wait(3.0)
        try:
            self.flush_counts()
        except Exception:  # noqa: BLE001
            pass

    def _backfill_loop(self) -> None:
        from . import backfill
        ev = self.stop_event
        backfill.ensure(self)
        while not ev.is_set():
            try:
                backfill.step(self, ev)
            except Exception as e:  # noqa: BLE001
                self.last_error = f"media archive: {str(e)[:120]}"
            ev.wait(3)

    def _feeds_loop(self) -> None:
        from . import feeds
        feeds.sender_loop(self)

    def fill_sentiment(self, n: int = 300) -> int:
        """Articles read before sentiment was kept get theirs, a few hundred at a time."""
        rows = self.rq("SELECT key, title, summary FROM articles WHERE status='success' AND "
                       "(sentiment IS NULL OR sentiment='') LIMIT ?", (n,))
        if not rows:
            return 0
        from .templates import analyze
        vals = []
        for r in rows:
            try:
                v = (analyze(f"{r['title'] or ''}. {r['summary'] or ''}")[0] or "neutral").lower()
            except Exception:  # noqa: BLE001
                v = "neutral"
            vals.append((v, r["key"]))
        with self.lock:
            self._db.executemany("UPDATE articles SET sentiment=? WHERE key=?", vals)
            self._db.commit()
        return len(vals)

    def purge(self, ev=None) -> int:
        """Articles older than retention_days leave, in small chunks (the readers never wait long); their keys stay in
        `seen` for 60 days so the same old links are not read again every week."""
        cut = time.time() - self.cfg["retention_days"] * 86400
        n = 0
        while not (ev and ev.is_set()):
            with self.lock:
                rows = self._db.execute("SELECT rowid, key, discovered FROM articles WHERE discovered < ? AND "
                                        "(source IS NULL OR source NOT LIKE 'backfill:%') LIMIT 5000",
                                        (cut,)).fetchall()
                if not rows:
                    break
                self._db.executemany("INSERT OR IGNORE INTO seen(key, t) VALUES (?,?)",
                                     [(r["key"], r["discovered"]) for r in rows])
                self._db.executemany("DELETE FROM articles WHERE rowid=?", [(r["rowid"],) for r in rows])
                self._db.commit()
            n += len(rows)
            time.sleep(0.05)
        day = (datetime.now(WIB) - timedelta(days=max(30, self.cfg["retention_days"]))).strftime("%Y-%m-%d")
        self.x("DELETE FROM hourly WHERE hour < ?", (day,))
        self.x("DELETE FROM domain_days WHERE day < ?", ((datetime.now(WIB) - timedelta(days=35)).strftime("%Y-%m-%d"),))
        self.x("DELETE FROM seen WHERE t < ?", (time.time() - 60 * 86400,))
        return n

    # ----- late articles: the daily catch-up sweep -----
    def meta(self, k: str, v: str | None = None) -> str:
        if v is not None:
            self.x("INSERT INTO meta(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            return v
        r = self.q("SELECT v FROM meta WHERE k=?", (k,))
        return r[0]["v"] if r else ""

    def run_catchup(self) -> dict:
        """Every reachable outlet's sitemaps for the last `catchup_days` days, once: links published earlier but
        listed late, or missed while an outlet was checked rarely. Known links are skipped (one row per article)."""
        ev = self.stop_event
        days = int(self.cfg.get("catchup_days") or 0)
        srcs = [r for r in self.rq("SELECT * FROM sources WHERE kind='sitemap' AND fails < 3 AND checked > 0")
                if not self.excluded("https://" + r["domain"])] if days else []
        with self.lock:
            self.catchup = {"running": True, "done": 0, "total": len(srcs), "found": 0, "started": time.time(),
                            "finished": 0, "days": days}

        def one(src):
            if ev.is_set():
                return
            try:
                n, _failed, _extra = self.check_sitemap(src, catchup=days)
            except Exception:  # noqa: BLE001
                n = 0
            with self.lock:
                self.catchup["done"] += 1
                self.catchup["found"] += n
        with ThreadPoolExecutor(max_workers=max(2, self.cfg["discovery_threads"] // 2), thread_name_prefix="stream-cu") as ex:
            list(ex.map(one, srcs))
        with self.lock:
            self.catchup.update(running=False, finished=time.time())
            out = dict(self.catchup)
        if ev.is_set():
            return out                                      # cut short: today's sweep runs again after the restart
        self.meta("catchup_last", datetime.now(WIB).strftime("%Y-%m-%d"))
        self.meta("catchup_result", json.dumps({k: out[k] for k in ("total", "found", "started", "finished")}))
        return out

    # ----- site check: is every registered outlet up? (at start + weekly) -----
    def check_site(self, domain: str) -> tuple[bool, list]:
        from .medmas.discovery import sitemaps as sm
        roots, alive = sm._sitemaps_from_robots(domain)
        return alive, roots

    def run_health(self) -> dict:
        """Every registered outlet once: does it answer, does it list sitemaps. A site that is down is not crawled
        (no feed, no sitemap) until the next site check; one that came back is put back in the rotation at once."""
        ev = self.stop_event
        doms = sorted({r["domain"] for r in self.rq("SELECT DISTINCT domain FROM sources WHERE kind='sitemap'")})
        doms = [d for d in doms if not self.excluded("https://" + d)]
        with self.lock:
            self.health = {"running": True, "done": 0, "total": len(doms), "alive": 0, "down": 0,
                           "started": time.time(), "finished": 0}
        next_check = self._next_health_time()

        results: dict = {}

        def probe(d):
            if ev.is_set():
                return
            try:
                alive, roots = self.check_site(d)
            except Exception:  # noqa: BLE001
                alive, roots = False, []
            with self.lock:
                results[d] = (alive, roots)
                self.health["done"] += 1
                self.health["alive" if alive else "down"] += 1
        workers = max(4, self.cfg["discovery_threads"])
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stream-hc") as ex:
            list(ex.map(probe, doms))
        down = [d for d, (a, _r) in results.items() if not a]
        if down and not ev.is_set():                        # a site that looked down gets a second chance
            ev.wait(60)
            with self.lock:
                self.health["down"] -= len(down)
                self.health["done"] -= len(down)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stream-hc") as ex:
                list(ex.map(probe, down))
        if ev.is_set():                                     # stopped part-way: mark nothing
            with self.lock:
                self.health.update(running=False, finished=time.time())
                return dict(self.health)
        down = [d for d, (a, _r) in results.items() if not a]
        if results and len(down) > 0.6 * len(results):
            # most of the internet "down" at once is this server's connection, not the sites: change nothing
            with self.lock:
                self.health.update(running=False, finished=time.time(), error="most sites did not answer — "
                                   "the server's internet connection looks down; nothing was marked")
                out = dict(self.health)
            self.last_error = "site check: " + out["error"]
            return out
        now = time.time()
        with self.lock:
            for d, (alive, roots) in results.items():
                self._db.execute("INSERT INTO site_health(domain, checked, alive, sitemaps) VALUES (?,?,?,?) "
                                 "ON CONFLICT(domain) DO UPDATE SET checked=excluded.checked, alive=excluded.alive, "
                                 "sitemaps=excluded.sitemaps", (d, now, int(alive), len(roots)))
                if not alive:                                 # down: left alone until the next site check
                    self._db.execute("UPDATE sources SET fails=MAX(fails, 3), tier='dead', next_check=? WHERE domain=?",
                                     (next_check, d))
                    self.tiers[d] = "dead"
                    continue
                self._db.execute("UPDATE sources SET fails=0, tier='inactive', next_check=? "
                                 "WHERE domain=? AND tier='dead'", (now, d))             # back: checked right away
                if roots:
                    self._db.execute("UPDATE sources SET url=? WHERE domain=? AND kind='sitemap' AND url=''",
                                     (json.dumps(roots[:6]), d))
                if self.tiers.get(d) == "dead":
                    self.tiers[d] = "inactive"
            self._db.commit()
            self.health.update(running=False, finished=time.time())
            out = dict(self.health)
        self.meta("health_last", str(int(time.time())))
        self.meta("health_result", json.dumps({k: out[k] for k in ("total", "alive", "down", "started", "finished")}))
        return out

    def _next_health_time(self) -> float:
        now = datetime.now(WIB)
        days = (self.cfg.get("health_weekday", 6) - now.weekday()) % 7 or 7
        nxt = (now + timedelta(days=days)).replace(hour=self.cfg.get("health_hour", 2), minute=30, second=0)
        return nxt.timestamp()

    def health_loop(self) -> None:
        ev = self.stop_event
        first = True
        while not ev.is_set():
            try:
                now = datetime.now(WIB)
                week = now.strftime("%G-W%V")
                last = float(self.meta("health_last") or 0)
                at_start = first and self.cfg.get("health_on_start", True) and time.time() - last > 20 * 3600
                weekly = (now.weekday() == self.cfg.get("health_weekday", 6)
                          and now.hour >= self.cfg.get("health_hour", 2) and self.meta("health_week") != week)
                first = False
                if at_start or weekly or self.health_now.is_set():
                    self.health_now.clear()
                    if weekly:
                        self.meta("health_week", week)
                    self.run_health()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"site check: {str(e)[:120]}"
                with self.lock:
                    self.health["running"] = False
            ev.wait(30)

    def catchup_loop(self) -> None:
        ev = self.stop_event
        while not ev.is_set():
            due = False
            try:
                now = datetime.now(WIB)
                due = bool(self.cfg.get("catchup_days") and now.hour >= self.cfg.get("catchup_hour", 1)
                           and self.meta("catchup_last") != now.strftime("%Y-%m-%d"))
                week = now.strftime("%G-W%V")
                if (self.cfg.get("weekly_report", True) and now.weekday() == self.cfg.get("report_weekday", 0)
                        and now.hour >= self.cfg.get("report_hour", 7) and self.meta("weekly_last") != week):
                    self.meta("weekly_last", week)
                    from . import media_db
                    media_db.send_report()
            except Exception as e:  # noqa: BLE001
                self.last_error = f"weekly report: {str(e)[:120]}"
            if due or self.catchup_now.is_set():
                self.catchup_now.clear()
                try:
                    self.run_catchup()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"catch-up: {str(e)[:120]}"
                    with self.lock:
                        self.catchup["running"] = False
            ev.wait(30)

    # ----- every article of a day -> the team's Online file -----
    def export_day(self, day: str, out_dir: str) -> str:
        """All articles published on `day` (YYYY-MM-DD) read by the stream -> one Online Excel (the uploader's
        cleaning rules), written row by row so even 100,000+ articles fit in memory. Returns the file name."""
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.styles.colors import Color
        from . import templates, online_clean as oc
        if not re.match(r"\d{4}-\d{2}-\d{2}$", day or ""):
            raise ValueError("Date as YYYY-MM-DD.")
        total = self.rq("SELECT COUNT(*) n FROM articles WHERE status='success' AND pub_date=?", (day,))[0]["n"]
        with self.lock:
            self.export = {"running": True, "done": 0, "total": total, "file": "", "error": "", "day": day}
        name = f"Stream_All_{day.replace('-', '')}_{datetime.now(WIB):%H%M}.xlsx"
        os.makedirs(out_dir, exist_ok=True)
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Sheet1")
        ws.sheet_view.zoomScale = 80
        for i, w in enumerate(templates.ONLINE_WIDTHS, 1):
            ws.column_dimensions[chr(64 + i)].width = w
        font = Font(name="Calibri", size=9, bold=True, color=Color(theme=0))
        teal, grey = PatternFill("solid", fgColor="FF15677B"), PatternFill("solid", fgColor=Color(theme=0, tint=-0.5))
        head = []
        for c in templates.ONLINE_COLUMNS:
            cell = WriteOnlyCell(ws, value=c)
            cell.font, cell.alignment = font, Alignment(horizontal="left", vertical="center")
            cell.fill = grey if c in ("INFLUENCER", "PERSON", "SUMMARY") else teal
            head.append(cell)
        ws.append(head)
        seen, last = set(), 0
        try:
            while True:
                rows = self.rq("SELECT rowid, * FROM articles WHERE status='success' AND pub_date=? AND rowid > ? "
                               "ORDER BY rowid LIMIT 500", (day, last))
                if not rows:
                    break
                for r in rows:
                    last = r["rowid"]
                    text = zlib.decompress(r["content"]).decode("utf-8", "ignore") if r["content"] else ""
                    t = r["pub_time"] or ""
                    row = templates.online_row({
                        "url": r["url"], "title": r["title"], "author": r["author"], "text": text,
                        "description": r["summary"], "image": r["image"], "influencer": r["influencer"],
                        "person": r["persons"], "published": day,
                        "published_full": f"{day} {t}" if re.match(r"\d{2}:\d{2}$", t) else ""}, WIB)
                    u = row[3]
                    if u in seen or not (row[10] or "").strip() or oc.is_junk_page(row[2], u):
                        continue
                    seen.add(u)
                    ws.append([oc.safe_cell(v, templates.ONLINE_COLUMNS[k]) for k, v in enumerate(row)])
                with self.lock:
                    self.export["done"] = min(total, self.export["done"] + len(rows))
            tmp = os.path.join(out_dir, name + ".tmp")
            wb.save(tmp)
            os.replace(tmp, os.path.join(out_dir, name))
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.export.update(running=False, error=str(e)[:200])
            raise
        with self.lock:
            self.export.update(running=False, done=total, file=name, rows=len(seen))
        return name

    # ----- control -----
    def start(self, log=print) -> None:
        with self._ctl:                                      # never at the same time as a stop
            with self.lock:
                if self.running:
                    return
                self.cfg = settings()
                self.stop_event = threading.Event()          # a fresh event: old threads keep seeing theirs set
                self.running, self.started = True, time.time()
            try:
                from . import media_db
                media_db.install()                            # paid media: their logins
            except Exception:  # noqa: BLE001
                pass
            info = self.sync_sources(log)
            log(f"🛰 News stream started: {info['rss']} RSS feeds + {info['sitemaps']} outlet sitemaps, "
                f"{self.cfg['threads']} readers")
            self.threads = [threading.Thread(target=self.discovery_loop, daemon=True, name="stream-discovery"),
                            threading.Thread(target=self.catchup_loop, daemon=True, name="stream-catchup"),
                            threading.Thread(target=self.health_loop, daemon=True, name="stream-health"),
                            threading.Thread(target=self.house_loop, daemon=True, name="stream-house"),
                            threading.Thread(target=self._feeds_loop, daemon=True, name="stream-feeds"),
                            threading.Thread(target=self._backfill_loop, daemon=True, name="stream-archive")]
            self.threads += [threading.Thread(target=self.reader_loop, daemon=True, name=f"stream-reader-{i}")
                             for i in range(self.cfg["threads"])]
            for t in self.threads:
                t.start()

    def stop(self) -> None:
        with self._ctl:
            with self.lock:
                if not self.running:
                    return
                self.running = False
                ev, threads = self.stop_event, self.threads
            ev.set()
            deadline = time.time() + 60                      # readers finish the page they are on
            for t in threads:
                t.join(timeout=max(0.1, deadline - time.time()))
            self.threads = []
            with self.lock:
                self.by_domain.clear()
                self.rr.clear()
                self.queued.clear()
                self.qn = 0

    def live_state(self) -> dict:
        """What only the running engine knows (the worker process writes it for the web process every 2 s)."""
        rec = list(self.recent)
        now = time.time()
        per_min = sum(1 for t in rec if t > now - 300) / 5
        with self.lock:
            return {"t": now, "running": self.running, "since": int(self.started) if self.running else 0,
                    "per_minute": round(per_min, 1), "per_day_pace": int(per_min * 60 * 24),
                    "readers_busy": len(self.busy_domains), "queue_mem": self.qn, "last_error": self.last_error,
                    "export": dict(self.export), "health": dict(self.health), "catchup": dict(self.catchup)}

    def days_with_articles(self) -> list[str]:
        """The last 30 calendar days that have articles (one indexed look per day, not a scan)."""
        now = datetime.now(WIB)
        out = []
        for k in range(31):
            d = (now - timedelta(days=k)).strftime("%Y-%m-%d")
            if self.rq("SELECT 1 FROM articles WHERE status='success' AND pub_date=? LIMIT 1", (d,)):
                out.append(d)
            if len(out) >= 14:
                break
        return out

    def status(self, latest: int = 30, q: str = "") -> dict:
        now = time.time()
        today = datetime.now(WIB).strftime("%Y-%m-%d")
        hours = [(datetime.now(WIB) - timedelta(hours=i)).strftime("%Y-%m-%d %H") for i in range(23, -1, -1)]
        got = {r["hour"]: dict(r) for r in self.rq("SELECT * FROM hourly WHERE hour >= ?", (hours[0],))}
        with self.lock:
            live = dict(self.counts)
        series = []
        for h in hours:
            r = got.get(h, {})
            series.append({"hour": h, **{k: int(r.get(k, 0)) + (live[k] if h == _hour() else 0)
                                         for k in ("found", "read", "failed", "matched", "skipped")}})
        day = {k: sum(s[k] for s in series if s["hour"].startswith(today))
               for k in ("found", "read", "failed", "matched", "skipped")}
        src = {r["kind"]: dict(r) for r in self.rq(
            "SELECT kind, COUNT(*) n, SUM(fails >= 3) dead, SUM(hits > 0) productive, SUM(checked > 0) checked "
            "FROM sources GROUP BY kind")}
        where, args = "status='success'", []
        if q:
            where += " AND (title LIKE ? OR matched LIKE ?)"
            args = [f"%{q}%", f"%{q}%"]
        rows = self.rq(f"SELECT url, domain, title, pub_date, pub_time, matched, fetched FROM articles WHERE {where} "
                      "ORDER BY fetched DESC LIMIT ?", (*args, latest))
        try:
            size = os.path.getsize(self.db_path) + (os.path.getsize(self.db_path + "-wal")
                                                    if os.path.exists(self.db_path + "-wal") else 0)
        except OSError:
            size = 0
        rec = list(self.recent)                              # readers append while we count
        per_min = sum(1 for t in rec if t > now - 300) / 5
        live = read_live() if _WORKER_PARENT else None
        out = {"running": self.running, "since": int(self.started) if self.running else 0, "settings": settings(),
                "queue": self.rq("SELECT COUNT(*) n FROM articles WHERE status='new'")[0]["n"],
                "stored": self.rq("SELECT COUNT(*) n FROM articles WHERE status='success'")[0]["n"],
                "per_minute": round(per_min, 1), "per_day_pace": int(per_min * 60 * 24), "today": day,
                "hourly": series, "sources": src, "keywords": len(self.keywords()), "db_mb": round(size / 1e6, 1),
                "tiers": {r["tier"] or "new": r["n"] for r in self.rq(
                    "SELECT tier, COUNT(DISTINCT domain) n FROM sources GROUP BY tier")},
                "health": {**dict(self.health), "last_result": json.loads(self.meta("health_result") or "{}")},
                "catchup": {**dict(self.catchup), "last": self.meta("catchup_last"),
                            "last_result": json.loads(self.meta("catchup_result") or "{}")},
                "readers_busy": len(self.busy_domains), "last_error": self.last_error, "export": dict(self.export),
                "days": self.days_with_articles(),
                "latest": [dict(r) for r in rows]}
        if live:                                             # the engine runs in its own process: its live state
            for k in ("running", "since", "per_minute", "per_day_pace", "readers_busy", "last_error", "export"):
                out[k] = live.get(k, out[k])
            out["health"].update(live.get("health") or {})
            out["catchup"].update(live.get("catchup") or {})
            out["worker"] = True
        elif _WORKER_PARENT:
            out["running"] = False
            out["last_error"] = "the stream process is starting or not running"
        return out


_STREAM: Stream | None = None
_GET_LOCK = threading.Lock()


def get() -> Stream:
    global _STREAM
    with _GET_LOCK:
        if _STREAM is None:
            _STREAM = Stream()
        return _STREAM


def set_enabled(on: bool, log=print) -> dict:
    cur = settings()
    cur["enabled"] = bool(on)
    _write_settings(cur)
    if _WORKER_PARENT:
        return {"enabled": cur["enabled"]}                # the worker process follows the setting within seconds
    s = get()
    if on:
        threading.Thread(target=s.start, args=(log,), daemon=True).start()
    else:
        threading.Thread(target=s.stop, daemon=True).start()
    return {"enabled": cur["enabled"]}


def autostart(log=print) -> None:
    """At app start: the stream runs again if an admin left it on."""
    if settings().get("enabled"):
        threading.Thread(target=get().start, args=(log,), daemon=True).start()


def apply(data: dict, log=print) -> dict:
    """Dashboard: save the numbers / keywords, switch on or off. Running with new numbers -> restarted."""
    s = get()
    out = {}
    fields = {k: v for k, v in (data or {}).items() if k in DEFAULTS and k != "enabled"}
    if fields:
        old = settings()
        new = save_settings(fields)
        s.cfg = new
        s.kw_cache = (0.0, [])
        restart = (not _WORKER_PARENT and s.running
                   and any(old.get(k) != new.get(k) for k in ("threads", "discovery_threads")))
        if restart:
            def again():
                s.stop()
                s.start(log)
            threading.Thread(target=again, daemon=True).start()
    if isinstance(data.get("enabled"), bool):
        out = set_enabled(data["enabled"], log)
    running = (read_live() or {}).get("running") if _WORKER_PARENT else s.running
    for key, cmd, ev in (("health_now", "health", s.health_now), ("catchup_now", "catchup", s.catchup_now)):
        if data.get(key):
            if not running:
                raise ValueError("Start the stream first.")
            command(cmd) if _WORKER_PARENT else ev.set()
            out[cmd] = "started"
    return {**settings(), **out}


def start_export(day: str) -> dict:
    """In the background: the dashboard shows the progress, the file lands in Exports."""
    from .paths import OUTPUT_DIR
    s = get()
    if not re.match(r"\d{4}-\d{2}-\d{2}$", day or ""):
        return {"ok": False, "error": "Date as YYYY-MM-DD."}
    if _WORKER_PARENT:
        if ((read_live() or {}).get("export") or {}).get("running"):
            return {"ok": False, "error": "An export is already running."}
        command("export", day)                             # made by the worker process (keeps the web quick)
        return {"ok": True}
    with s.lock:
        if s.export.get("running"):
            return {"ok": False, "error": "An export is already running."}
        s.export = {"running": True, "done": 0, "total": 0, "file": "", "error": "", "day": day}
    threading.Thread(target=lambda: _quiet(s.export_day, day, OUTPUT_DIR), daemon=True).start()
    return {"ok": True}


def _quiet(fn, *a):
    try:
        fn(*a)
    except Exception:  # noqa: BLE001  the error is in s.export
        pass


# ---------- the stream in its own process ----------
# The web server and the stream share nothing but the data folder: the stream runs as `python -m
# crawler.stream_worker` at a lower priority, so a busy stream (thousands of pages, parsing, indexing) never makes the
# dashboard slow. The web process writes settings and commands; the worker writes its live state (live.json).
_WORKER_PARENT = False
_WORKER = {"proc": None, "stop": threading.Event()}


def _live_path() -> str:
    return os.path.join(_dir(), "live.json")


def read_live() -> dict | None:
    try:
        with open(_live_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if time.time() - float(d.get("t", 0)) < 20 else None
    except (OSError, ValueError):
        return None


def write_live(state: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    tmp = _live_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, _live_path())


def command(cmd: str, arg: str = "") -> None:
    """A request for the worker process (run the sweep / site check now, make an export, reload logins, …)."""
    get().x("INSERT INTO commands(cmd, arg, t) VALUES (?,?,?)", (cmd, arg, time.time()))


def take_commands(s: "Stream") -> list:
    with s.lock:
        rows = s._db.execute("SELECT id, cmd, arg FROM commands ORDER BY id").fetchall()
        if rows:
            s._db.execute("DELETE FROM commands WHERE id <= ?", (rows[-1]["id"],))
            s._db.commit()
    return [(r["cmd"], r["arg"]) for r in rows]


def start_worker(log=print) -> None:
    """The web server's side: start the worker process and restart it if it ever dies."""
    import subprocess
    import sys
    global _WORKER_PARENT
    _WORKER_PARENT = True

    def keep():
        fails = 0
        while not _WORKER["stop"].is_set():
            p = _WORKER["proc"]
            if p is None or p.poll() is not None:
                if p is not None:
                    fails += 1
                    log(f"stream worker stopped (exit {p.returncode}) — starting it again")
                _WORKER["proc"] = subprocess.Popen(          # stdin stays open: the worker ends when we do
                    [sys.executable, "-m", "crawler.stream_worker"], stdin=subprocess.PIPE,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            _WORKER["stop"].wait(min(60, 5 * (1 + fails)))
    threading.Thread(target=keep, daemon=True, name="stream-supervisor").start()
