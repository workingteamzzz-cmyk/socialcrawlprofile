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
            "retention_days": 0, "max_age_hours": 48, "all_links": True, "old_per_check": 50, "old_per_day": 30000, "old_hours": "1-6", "late_days": 7, "min_date": "", "homepage": True, "domain_gap": 1.5, "rss": True, "sitemaps": True,
            "keywords": [], "robots": True, "exclude_domains": [],
            "fetch_googlebot": True, "fetch_cloudscraper": True,     # refused pages: see fetch_policy.py
            "auto_add_media": True, "retry_failed": True,             # outlets found by News Search; retry later
            "pause_international": False,                             # non-Indonesian outlets: see intl.py
            "reader_processes": 0}                                    # 0 = automatic (CPU cores - 1, max 4)
LIMITS = {"threads": (2, 64), "reader_processes": (0, 8), "discovery_threads": (2, 48), "rss_minutes": (2, 240), "sitemap_minutes": (10, 720),
          "old_per_check": (0, 20000), "old_per_day": (0, 2_000_000), "late_days": (2, 30),
          "catchup_days": (0, 7), "catchup_hour": (0, 23), "health_weekday": (0, 6), "health_hour": (0, 23), "report_weekday": (0, 6), "report_hour": (0, 23),
          "top_n": (50, 20000), "top2_n": (0, 20000), "active2_hours": (1, 24), "semi_hours": (1, 48), "rare_hours": (2, 168), "inactive_days": (1, 30),
          "dead_days": (1, 60),
          "retention_days": (0, 3650), "max_age_hours": (6, 168)}
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
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, t REAL);
CREATE TABLE IF NOT EXISTS fetch_methods(day TEXT, method TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(day, method));
CREATE TABLE IF NOT EXISTS outlet_country(domain TEXT PRIMARY KEY, country TEXT, id_share REAL, n INTEGER DEFAULT 0,
  checked REAL, mark TEXT DEFAULT '');
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
    mark50 = os.path.join(_dir(), "oldlinks_v50.done")
    if got and got.get("old_per_check") == 1000 and not os.path.exists(mark50):
        got["old_per_check"] = 50                               # the old default flooded the queue (millions a day)
        try:
            _write_settings({**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}})
            open(mark50, "w").close()
        except OSError:
            pass
    mark = os.path.join(_dir(), "retention_v33.done")
    if got and got.get("retention_days") == 7 and not os.path.exists(mark):
        got["retention_days"] = 0                               # the old 7-day default: articles are now kept for good
        try:
            _write_settings({**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}})
            open(mark, "w").close()
        except OSError:
            pass
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
        elif k == "old_hours":                             # "1-6" = 01:00–06:00 WIB; "" = any time
            v = str(v or "").strip().replace(" ", "").replace("–", "-")
            m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", v)
            if v and not (m and 0 <= int(m[1]) <= 23 and 1 <= int(m[2]) <= 24 and int(m[1]) != int(m[2])):
                raise ValueError("Older links: hours as from-to, e.g. 1-6 (empty = any time).")
            v = f"{int(m[1])}-{int(m[2])}" if v else ""
        elif k == "min_date":                              # nothing published before this (unless asked for)
            v = str(v or "").strip()
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("Oldest publication date: YYYY-MM-DD, or empty for no limit.")
        elif k == "domain_gap":
            v = max(0.5, min(30.0, float(v)))
        elif k in ("rss", "sitemaps", "robots", "weekly_report", "health_on_start", "all_links", "homepage",
                   "fetch_googlebot", "fetch_cloudscraper", "auto_add_media", "retry_failed", "pause_international"):
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


QUEUE_MAX = 20000                          # articles held in memory at most; the rest wait in the database
FRESH_PER_CHECK = 1500                    # links per outlet visit taken as news; more at once = an archive listing
SITEMAP_READS_BUSY = 20                    # sitemap files read per visit of an active outlet (others: 6)
JUNK_VERSION = "v47"                       # bump when online_clean's page filter learns new titles


# ---------- the engine ----------
class Stream:
    def __init__(self, folder: str | None = None):
        self.folder = folder or _dir()
        os.makedirs(self.folder, exist_ok=True)
        self.db_path = os.path.join(self.folder, "stream.db")
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self.schema_busy = False
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            from .backfill import SCHEMA as ARCHIVE_SCHEMA
            self._db.executescript(ARCHIVE_SCHEMA)
        except sqlite3.OperationalError:
            # the other process holds the database (e.g. building an index once after an update): the tables are
            # there already — the dashboard reads on instead of failing until it is done
            self.schema_busy = True
        for sql in ("ALTER TABLE hourly ADD COLUMN skipped INTEGER DEFAULT 0",          # databases from v23
                    "ALTER TABLE sources ADD COLUMN tier TEXT DEFAULT 'new'",
                    "ALTER TABLE articles ADD COLUMN sentiment TEXT DEFAULT ''",             # databases before v33
                    "ALTER TABLE articles ADD COLUMN tries INTEGER DEFAULT 0",                # before v47
                    "ALTER TABLE sources ADD COLUMN paused INTEGER DEFAULT 0"):               # before v49
            if self.schema_busy:
                break
            try:
                self._db.execute(sql)
            except sqlite3.OperationalError:
                pass
        try:
            self._db.commit()
        except sqlite3.OperationalError:
            pass
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
        self.rr = deque()                      # round-robin over domains with work (fresh news lane)
        self.by_domain_old: dict[str, deque] = {}   # the backlog lane: older articles and media-archive jobs
        self.rr_old = deque()
        self.qn_old = 0                        # of qn, how many wait in the backlog lane
        self._cut = (0.0, "")                  # cutoff date, cached for a minute
        self.disk_brake = False                # the disk is nearly full: no older links taken in
        self.shard = (0, 1)                    # (this process, processes): each outlet is read by one process only
        try:
            self._db.create_function("shard", 2, _shard_of, deterministic=True)
        except sqlite3.Error:
            pass
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
                for j in self.by_domain_old.pop(d, None) or []:
                    self.queued.discard(j["key"])
                    self.qn -= 1
                    self.qn_old -= 1
                self.tiers.pop(d, None)
            self._db.commit()

    def due_sources(self, limit: int = 64) -> list:
        kinds = [k for k, on in (("rss", self.cfg["rss"]), ("sitemap", self.cfg["sitemaps"])) if on]
        if not kinds:
            return []
        return self.q(f"SELECT * FROM sources WHERE next_check <= ? AND kind IN ({','.join('?' * len(kinds))}) "
                      "AND COALESCE(paused, 0) = 0 ORDER BY next_check LIMIT ?", (time.time(), *kinds, limit))

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
        take_all = bool(self.cfg.get("all_links", True)) and not self.disk_brake
        day = datetime.now(WIB).strftime("%Y-%m-%d")
        taken = self._old_taken(day)
        room_old = min(int(self.cfg.get("old_per_check", 50)),
                       max(0, int(self.cfg.get("old_per_day", 30000)) - taken)) if take_all else 0
        late_cut = (datetime.now(WIB) - timedelta(days=int(self.cfg.get("late_days", 7)))).strftime("%Y-%m-%d")
        now, rows, n_fresh = time.time(), [], 0
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http") or not is_article_link(url) or is_junk_page("", url):
                continue
            d = (it.get("date") or "")[:10]
            if self.too_old(d, url):
                continue                                   # published before the oldest date wanted
            old = bool(d and d < cutoff) or bool(it.get("old"))
            if not old:
                n_fresh += 1
                old = n_fresh > FRESH_PER_CHECK            # thousands "new" at once: an archive, not today's news
            if old and not take_all:
                continue
            late = old and bool(d) and d >= late_cut and n_fresh <= FRESH_PER_CHECK   # late news: no daily budget
            rows.append(((norm_key(url), url, it.get("domain") or "", (it.get("title") or "")[:500],
                          f"{source}:old" if old else source, d, (it.get("time") or "")[:5], now), old, late))
        if not rows:
            return 0
        fresh = []
        with self.lock:
            for row, old, late in rows:
                if old and not late and room_old <= 0:
                    continue                                   # older links: a share per visit, the rest next time
                if self._db.execute("SELECT 1 FROM seen WHERE key=?", (row[0],)).fetchone():
                    continue                                   # read before and purged since: not again
                cur = self._db.execute("INSERT OR IGNORE INTO articles(key, url, domain, title, source, feed_date, "
                                       "feed_time, discovered) VALUES (?,?,?,?,?,?,?,?)", row)
                if cur.rowcount:
                    if old and not late:
                        room_old -= 1
                    fresh.append((row, old, late))
            per_dom: dict = {}
            for row, old, _late in fresh:
                if not old:                                   # activity = new articles, not the back catalogue
                    per_dom[row[2]] = per_dom.get(row[2], 0) + 1
            self._db.executemany("INSERT INTO domain_days(domain, day, n) VALUES (?,?,?) ON CONFLICT(domain, day) "
                                 "DO UPDATE SET n = n + excluded.n", [(d, day, n) for d, n in per_dom.items() if d])
            n_old = sum(1 for _r, o, lt in fresh if o and not lt)
            if n_old:                                     # older links: a budget per day (the readers' pace)
                self._oldday = [day, taken + n_old]
                self._db.execute("INSERT INTO meta(k, v) VALUES ('old_taken', ?) ON CONFLICT(k) DO UPDATE SET "
                                 "v=excluded.v", (json.dumps(self._oldday),))
            self._db.commit()
            self.counts["found"] += len(fresh)
            if self.running:                     # straight to the readers, at the front: newest first, no waiting
                for (key, url, dom, title, src_, fd, ft, _t), old, _late in fresh:
                    if self.qn >= QUEUE_MAX or (self.qn >= 4000 and (old or not front)):
                        continue                       # the queue is full: it waits in the database (refill)
                    self._enqueue({"key": key, "url": url, "domain": dom, "title": title, "feed_date": fd,
                                   "feed_time": ft, "source": src_}, front=front and not old)  # older ones: behind
        return len(fresh)

    def _old_taken(self, day: str) -> int:
        """Older links stored today (kept in meta, so a restart does not reset the day's budget)."""
        cur = getattr(self, "_oldday", None)
        if cur is None:
            try:
                cur = json.loads(self.meta("old_taken") or "null") or ["", 0]
            except (ValueError, sqlite3.Error):
                cur = ["", 0]
            self._oldday = cur
        return int(cur[1]) if cur[0] == day else 0

    def _fresh_cutoff(self) -> str:
        if time.time() - self._cut[0] > 60:
            self._cut = (time.time(), self.cutoff())
        return self._cut[1]

    def too_old(self, d: str, url: str = "") -> bool:
        """Published before min_date (default: none — every year is taken; older links wait for the night hours,
        newest first): not taken, except by a media-archive job. Undated links are judged by a year in their address
        (/2019/05/…)."""
        lim = str(self.cfg.get("min_date", "") or "")
        if not lim:
            return False
        if d and re.match(r"\d{4}-\d{2}-\d{2}$", d):
            return d < lim
        m = re.search(r"/((?:19|20)\d{2})/(?:[01]?\d)/", url or "")
        return bool(m) and m[1] < lim[:4]

    def _title_wanted(self, title: str) -> bool:
        """The link's title (from the sitemap / RSS) already matches a keyword or report topic."""
        if not title:
            return False
        from .medmas.filters import keyword_match
        try:
            return any(keyword_match(kw, title) for kw in self.keywords())
        except Exception:  # noqa: BLE001
            return False

    def is_late(self, job: dict) -> bool:
        """Backlog links worth reading by day when a reader is free: published in the last `late_days` days
        (news listed late), or a media-archive job."""
        src = job.get("source") or ""
        if src.startswith("backfill:"):
            return True
        d = (job.get("feed_date") or "")[:10]
        lim = (datetime.now(WIB) - timedelta(days=int(self.cfg.get("late_days", 7)))).strftime("%Y-%m-%d")
        return bool(d) and d >= lim

    def is_fresh(self, job: dict) -> bool:
        """The fresh-news lane: published within the stream's days (or no date known) and not a media-archive job —
        or a link whose title already matches a client's keywords, whatever its date."""
        src = job.get("source") or ""
        if src.startswith("backfill:"):
            return False                               # a media-archive job
        if self._title_wanted(job.get("title") or ""):
            return True
        if src.endswith(":old"):
            return False                               # a link known to be older
        d = (job.get("feed_date") or "")[:10]
        if not d:                                      # undated: fresh only when just found (2 days)
            return float(job.get("discovered") or time.time()) > time.time() - 2 * 86400
        return d >= self._fresh_cutoff()

    def mine(self, dom: str) -> bool:
        k, n = self.shard
        return n <= 1 or _shard_of(dom, n) == k

    def _enqueue(self, job: dict, front: bool = False) -> None:
        if job["key"] in self.queued:
            return
        if not self.mine(job.get("domain") or norm_key(job["url"]).split("/")[0]):
            return                                     # another reader process's outlet: it waits in the database
        self.queued.add(job["key"])
        self.qn += 1
        dom = job["domain"] or norm_key(job["url"]).split("/")[0]
        if self.is_fresh(job):
            by, rr = self.by_domain, self.rr
        else:
            by, rr = self.by_domain_old, self.rr_old
            self.qn_old += 1
        if dom not in by:
            by[dom] = deque()
            rr.appendleft(dom) if front else rr.append(dom)
        (by[dom].appendleft if front else by[dom].append)(job)

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
        busy = self.tier_of(src) in ("active", "active2")    # big, busy outlets: read deeper (news sitemaps first)
        limit = 14 if catchup else (SITEMAP_READS_BUSY if busy else 6)
        while queue and reads < limit:
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
                subs.sort(key=lambda x: 0 if ("news" in x.lower() or "berita" in x.lower()) else 1)
                queue.extend((subs or [x for x, p in ranked if p is not None][-2:])[:8 if catchup else (SITEMAP_READS_BUSY if busy else 3)])
                continue
            news = "news" in u.lower() or "berita" in u.lower()
            if not news and len(data) > 50 and len({(e.get("date") or "")[:10] for e in data}) == 1:
                data = [{**e, "date": ""} for e in data]     # one date on every entry = when the file was built
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
        """Articles waiting in the database -> the lanes: fresh news first (by date, then the undated, newest
        found first); the backlog lane is topped up separately, so millions of older links never crowd the news."""
        cols = "key, url, domain, title, feed_date, feed_time, source, discovered"
        k, n = self.shard
        sh = f" AND shard(domain, {int(n)}) = {int(k)}" if n > 1 else ""   # this process's outlets only
        with self.lock:
            fresh_n = self.qn - self.qn_old
            if fresh_n < 2000:
                cut = self._fresh_cutoff()
                rows = self._db.execute(f"SELECT {cols} FROM articles WHERE status='new' AND feed_date >= ?{sh} "
                                        "ORDER BY feed_date DESC LIMIT 3000", (cut,)).fetchall()
                rows += self._db.execute(f"SELECT {cols} FROM articles WHERE status='new' AND feed_date = '' AND "
                                         f"discovered > ? AND source NOT LIKE '%:old'{sh} ORDER BY discovered DESC "
                                         "LIMIT 1500", (time.time() - 2 * 86400,)).fetchall()
                for r in rows:
                    self._enqueue(dict(r))
            if self.qn_old < 1500:
                cut = self._fresh_cutoff()
                rows = self._db.execute(f"SELECT {cols} FROM articles WHERE status='new' AND feed_date > '' AND "
                                        f"feed_date < ?{sh} ORDER BY feed_date DESC LIMIT 1500", (cut,)).fetchall()
                rows += self._db.execute(f"SELECT {cols} FROM articles WHERE status='new' AND feed_date = ''{sh} "
                                         "ORDER BY discovered DESC LIMIT 1500").fetchall()   # undated backlog
                for r in rows:
                    if self.qn >= QUEUE_MAX:
                        break
                    self._enqueue(dict(r))

    def backlog_open(self, now: float | None = None) -> bool:
        """Older links are read only in the night hours (old_hours, WIB) — by day every reader is on fresh news.
        Media-archive jobs (asked for by hand) are read at any time."""
        span = str(self.cfg.get("old_hours", "1-6") or "")
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", span)
        if not m:
            return True
        a, b = int(m[1]), int(m[2])
        h = datetime.fromtimestamp(now or time.time(), WIB).hour
        return (a <= h < b) if a < b else (h >= a or h < b)

    def _take(self, by: dict, rr: deque, now: float, only_archive: bool = False):
        """only_archive (by day, backlog lane): only late news (last late_days days) and media-archive jobs."""
        for _ in range(len(rr)):
            dom = rr[0]
            rr.rotate(-1)
            q = by.get(dom)
            if not q:
                by.pop(dom, None)
                try:
                    rr.remove(dom)
                except ValueError:
                    pass
                continue
            if dom in self.busy_domains or self.next_ok.get(dom, 0) > now:
                continue
            if only_archive and not self.is_late(q[0]):
                continue                                   # by day: the archive waits for the night hours
            self.busy_domains.add(dom)
            self.qn -= 1
            return dom, q.popleft()
        return None

    def next_job(self, backlog_first: bool = False):
        """The next article to read: round robin over outlets, one page at a time per outlet, a pause between.
        Fresh news always first; the backlog lane only when no fresh page can be read right now — except for the
        one backlog reader (backlog_first), which keeps the archive moving. Outside the night hours (old_hours) the
        backlog lane gives only media-archive jobs: every reader is on fresh news."""
        with self.lock:
            now = time.time()
            lanes = [(self.by_domain_old, self.rr_old, True), (self.by_domain, self.rr, False)] if backlog_first else \
                [(self.by_domain, self.rr, False), (self.by_domain_old, self.rr_old, True)]
            night = self.backlog_open(now)
            if not night:
                lanes = [(self.by_domain, self.rr, False), (self.by_domain_old, self.rr_old, True)]
            for by, rr, old in lanes:
                got = self._take(by, rr, now, only_archive=old and not night)
                if got:
                    if old:
                        self.qn_old -= 1
                    return got
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
        if status == "success" and not archive_job and self.too_old(d if re.match(r"\d{4}-\d{2}-\d{2}$", d) else ""):
            status = "old"                                   # published before the oldest date wanted
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
            self.x("INSERT INTO fetch_methods(day, method, n) VALUES (?,?,1) ON CONFLICT(day, method) DO UPDATE SET "
                   "n = n + 1", (datetime.now(WIB).strftime("%Y-%m-%d"), res.get("method") or "normal"))
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

    def reader_loop(self, backlog_first: bool = False) -> None:
        ev = self.stop_event                                 # this run's event: a restart never revives old threads
        while not ev.is_set():
            got = self.next_job(backlog_first)
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
        try:                                          # here, not in start(): a big list never delays the start
            info = self.sync_sources(getattr(self, "_log", print))
            getattr(self, "_log", print)(f"🛰 Sources: {info['rss']} RSS feeds + {info['sitemaps']} outlet sitemaps")
        except Exception as e:  # noqa: BLE001
            self.last_error = f"sources: {str(e)[:120]}"
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

    def fetch_methods(self, days: int = 7) -> dict:
        """Articles read per method over the last days: normal, amp, googlebot, cloudscraper (+ share in %)."""
        since = (datetime.now(WIB) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        try:
            got = {r["method"]: r["n"] for r in self.rq("SELECT method, SUM(n) n FROM fetch_methods WHERE day >= ? "
                                                         "GROUP BY method", (since,))}
        except sqlite3.Error:
            got = {}                                   # the table comes with the worker's next start
        total = sum(got.values())
        return {"days": days, "total": total,
                "methods": {m: {"n": n, "pct": round(n * 100 / total, 2) if total else 0} for m, n in got.items()}}

    def trim_backlog(self, chunk: int = 20000) -> dict:
        """Older links waiting to be read (older than the stream's days, or undated and found days ago) are taken
        out of the queue, a chunk at a time, so the database and its indexes stay light. Nothing is lost for good:
        such links are not marked as seen — an outlet's sitemap lists them again, and the media archive can fetch
        any period."""
        cut = (datetime.now(WIB) - timedelta(days=int(self.cfg.get("late_days", 7)))).strftime("%Y-%m-%d")
        st = self.__dict__.setdefault("trim", {})                 # late news (last late_days days) stays
        st.update(running=True, removed=0, started=time.time(), finished=0, error="")
        ev = self.stop_event
        try:
            while not ev.is_set():
                with self.lock:
                    n = self._db.execute(
                        "DELETE FROM articles WHERE rowid IN (SELECT rowid FROM articles WHERE status='new' AND "
                        "((feed_date > '' AND feed_date < ?) OR source LIKE '%:old' OR "
                        "(feed_date = '' AND discovered < ?)) LIMIT ?)", (cut, time.time() - 2 * 86400, chunk)).rowcount
                    self._db.commit()
                st["removed"] += n
                if n < chunk:
                    break
                ev.wait(0.5)                                  # readers and finders get the database in between
            with self.lock:
                for d in list(self.by_domain_old):            # what was queued in memory from them
                    for j in self.by_domain_old.pop(d):
                        self.queued.discard(j["key"])
                self.rr_old.clear()
                self.qn -= self.qn_old
                self.qn_old = 0
            self.__dict__.pop("_sc", None)                    # the counts again, at once
        except Exception as e:  # noqa: BLE001
            st["error"] = str(e)[:150]
        st.update(running=False, finished=time.time())
        return dict(st)

    TEMPORARY = ("Timeout", "ConnectionError", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
                 "HTTP 520", "HTTP 521", "HTTP 522", "HTTP 523", "HTTP 524", "Empty content")

    def retry_failed(self, after_h: float = 3, max_tries: int = 2, days: int = 3) -> int:
        """Articles that failed for a passing reason (timeout, connection, a busy server: 5xx / 429, an empty
        answer) go back to the queue after a few hours — at most twice, only from the last days."""
        if not self.cfg.get("retry_failed", True):
            return 0
        now = time.time()
        q = ','.join('?' * len(self.TEMPORARY))
        keys = [r["key"] for r in self.rq(
            f"SELECT key FROM articles WHERE status='failed' AND discovered > ? AND fetched < ? AND "
            f"COALESCE(tries, 0) < ? AND error IN ({q}) LIMIT 5000",
            (now - days * 86400, now - after_h * 3600, max_tries, *self.TEMPORARY))]
        if not keys:
            return 0
        with self.lock:
            for i in range(0, len(keys), 500):
                part = keys[i:i + 500]
                self._db.execute(f"UPDATE articles SET status='new', tries=COALESCE(tries, 0) + 1 WHERE key IN "
                                 f"({','.join('?' * len(part))})", part)
            self._db.commit()
            self.counts["retried"] = self.counts.get("retried", 0) + len(keys)
        return len(keys)

    DISK_WARN = (75, 90)            # % used: a Telegram message once per level (again after it went back down)
    DISK_BRAKE_GB = 5               # free space below this: older links are not taken in (news still is)

    def disk_guard(self, notify=None) -> dict:
        """Warns before the disk fills and, when it nearly has, stops taking in older links (the backlog), so today's
        news keeps being stored. The brake lifts by itself once there is room again."""
        from .overview import disk
        d = disk()
        if not d:
            return {}
        pct, free_gb = d["used_pct"], d["free"] / 1e9
        level = max([w for w in self.DISK_WARN if pct >= w], default=0)
        sent = int(self.meta("disk_warned") or 0)
        if level > sent:
            msg = (f"💽 Server disk {pct:.0f}% full — {free_gb:.1f} GB left. "
                   + ("Make the disk bigger soon, or free up space (Exports → Free up space by period)." if level < 90
                      else "Make the disk bigger now — older links stop being taken in at "
                           f"{self.DISK_BRAKE_GB} GB left."))
            try:
                if notify:
                    notify(msg)
                else:
                    from .captcha import notify as _notify
                    _notify(msg)
            except Exception:  # noqa: BLE001
                pass
            self.meta("disk_warned", str(level))
        elif level < sent:
            self.meta("disk_warned", str(level))
        brake = free_gb < self.DISK_BRAKE_GB
        if brake != self.disk_brake:
            self.disk_brake = brake
            self.last_error = (f"disk: {free_gb:.1f} GB left — older links paused, news still read" if brake else "")
        return {"used_pct": pct, "free_gb": round(free_gb, 1), "brake": brake, "warned": max(level, 0)}

    def drop_junk_titles(self) -> int:
        """Articles read before the filter knew their title was no news ("Banner Link", "Berita Terkini"…): taken out
        of News Stream, the search and the exports (status 'filtered'). Once per filter version."""
        from .online_clean import is_junk_page
        if (self.rq("SELECT v FROM meta WHERE k='junk_titles'") or [{"v": ""}])[0]["v"] == JUNK_VERSION:
            return 0
        bad = [r["key"] for r in self.rq("SELECT key, title, url FROM articles WHERE status='success' AND "
                                          "length(COALESCE(title,'')) <= 90") if is_junk_page(r["title"], r["url"])]
        with self.lock:
            for i in range(0, len(bad), 500):
                part = bad[i:i + 500]
                self._db.execute(f"UPDATE articles SET status='filtered', error='not a news article' WHERE key IN "
                                 f"({','.join('?' * len(part))})", part)
            self._db.execute("INSERT INTO meta(k, v) VALUES ('junk_titles', ?) ON CONFLICT(k) DO UPDATE SET "
                             "v=excluded.v", (JUNK_VERSION,))
            self._db.commit()
        return len(bad)

    def ensure_indexes(self) -> None:
        """Indexes the dashboard's counts read without touching the articles themselves (Media Database: activity
        and access per outlet over 7 days — a million rows counted in seconds, not minutes). Built once, here in the
        stream's own process, never while a page waits."""
        have = {r["name"] for r in self.rq("SELECT name FROM sqlite_master WHERE type='index'")}
        if "a_disc" in have and "a_disc_ds" in have:        # covered by a_disc_ds: one index less to keep up
            with self.lock:
                self._db.execute("DROP INDEX IF EXISTS a_disc")
                self._db.commit()
        if {"a_disc_ds", "a_fail_err", "a_new_fd"} <= have:
            return
        self.meta("maintenance", f"building indexes since {datetime.now(WIB):%H:%M} (once after an update)")
        with self.lock:
            self._db.execute("CREATE INDEX IF NOT EXISTS a_disc_ds ON articles(discovered, domain, status)")
            self._db.execute("CREATE INDEX IF NOT EXISTS a_fail_err ON articles(discovered, domain, error) "
                             "WHERE status='failed'")
            self._db.execute("CREATE INDEX IF NOT EXISTS a_new_fd ON articles(feed_date, discovered) "
                             "WHERE status='new'")      # the fresh lane's refill: news by date, never a scan
            self._db.commit()
        self.meta("maintenance", "")

    def house_loop(self) -> None:
        ev = self.stop_event
        last_purge = last_tiers = 0.0
        last_retry = time.time()
        last_disk = 0.0
        last_intl = time.time() - 86400 + 900                # first decision 15 min after the start
        try:
            self.ensure_indexes()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"indexes: {str(e)[:120]}"
        try:
            self.drop_junk_titles()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"junk titles: {str(e)[:120]}"
        while not ev.is_set():
            for name, step in (("counts", self.flush_counts), ("queue", self.refill), ("sentiment", self.fill_sentiment)):
                try:
                    step()
                except Exception as e:  # noqa: BLE001  each step on its own: one failing never stops the others
                    self.last_error = f"{name}: {str(e)[:120]}"
            if time.time() - last_disk > 600:
                last_disk = time.time()
                try:
                    self.disk_guard()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"disk check: {str(e)[:120]}"
            if time.time() - last_intl > 86400:
                last_intl = time.time()
                try:
                    from . import intl
                    intl.classify(self)
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"countries: {str(e)[:120]}"
            if time.time() - last_retry > 1800:
                last_retry = time.time()
                try:
                    self.retry_failed()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"retry: {str(e)[:120]}"
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
        n = 0
        if not self.cfg.get("retention_days"):                 # 0 = articles are kept for good (the default)
            self.x("DELETE FROM hourly WHERE hour < ?", ((datetime.now(WIB) - timedelta(days=400)).strftime("%Y-%m-%d"),))
            return 0
        cut = time.time() - self.cfg["retention_days"] * 86400
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

    def run_catchup(self, days: int | None = None, domains: list | None = None) -> dict:
        """Every reachable outlet's sitemaps for the last `catchup_days` days, once: links published earlier but
        listed late, or missed while an outlet was checked rarely. Known links are skipped (one row per article).
        days / domains: a one-off sweep of some outlets (e.g. international ones after a pause) — paused outlets
        are skipped by the daily sweep."""
        ev = self.stop_event
        one_off = domains is not None
        days = int(days if days is not None else (self.cfg.get("catchup_days") or 0))
        want = set(domains or [])
        srcs = [r for r in self.rq("SELECT * FROM sources WHERE kind='sitemap' AND fails < 3 AND checked > 0")
                if not self.excluded("https://" + r["domain"])
                and ((r["domain"] in want) if one_off else not (r["paused"] if "paused" in r.keys() else 0))] \
            if days else []
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
        if ev.is_set() or one_off:
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
                self.shard = (0, reader_processes(self.cfg) if _IN_WORKER else 1)
                self.stop_event = threading.Event()          # a fresh event: old threads keep seeing theirs set
                self.running, self.started = True, time.time()
            try:
                from . import media_db
                media_db.install()                            # paid media: their logins
            except Exception:  # noqa: BLE001
                pass
            self._log = log
            log(f"🛰 News stream starting: {self.cfg['threads']} readers × {self.shard[1]} process(es)")
            self.threads = [threading.Thread(target=self.discovery_loop, daemon=True, name="stream-discovery"),
                            threading.Thread(target=self.catchup_loop, daemon=True, name="stream-catchup"),
                            threading.Thread(target=self.health_loop, daemon=True, name="stream-health"),
                            threading.Thread(target=self.house_loop, daemon=True, name="stream-house"),
                            threading.Thread(target=self._feeds_loop, daemon=True, name="stream-feeds"),
                            threading.Thread(target=self._backfill_loop, daemon=True, name="stream-archive")]
            self.threads += [threading.Thread(target=self.reader_loop, args=(i == 0 and self.cfg["threads"] >= 4,),
                                              daemon=True, name=f"stream-reader-{i}")      # reader 0: the backlog's
                             for i in range(self.cfg["threads"])]
            for t in self.threads:
                t.start()

    def start_readers(self, k: int, n: int, log=print) -> None:
        """A reader process (k of n): only reads — its share of the outlets — and keeps its counts; finding links,
        tiers, sweeps and checks stay with the main stream process."""
        with self._ctl:
            with self.lock:
                if self.running:
                    return
                self.cfg = settings()
                self.shard = (k, n)
                self.stop_event = threading.Event()
                self.running, self.started = True, time.time()
            try:
                from . import fetch_policy, media_db
                media_db.install()
                fetch_policy.apply_settings(self.cfg)
            except Exception:  # noqa: BLE001
                pass
            self.threads = [threading.Thread(target=self._reader_house, daemon=True, name=f"stream-r{k}-house")]
            self.threads += [threading.Thread(target=self.reader_loop, args=(i == 0 and self.cfg["threads"] >= 4,),
                                              daemon=True, name=f"stream-r{k}-{i}") for i in range(self.cfg["threads"])]
            for t in self.threads:
                t.start()
            log(f"stream reader process {k + 1} of {n}: {self.cfg['threads']} readers")

    def _reader_house(self) -> None:
        ev = self.stop_event
        while not ev.is_set():
            for name, step in (("counts", self.flush_counts), ("queue", self.refill)):
                try:
                    step()
                except Exception as e:  # noqa: BLE001
                    self.last_error = f"{name}: {str(e)[:120]}"
            ev.wait(3.0)
        try:
            self.flush_counts()
        except Exception:  # noqa: BLE001
            pass

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
                self.by_domain_old.clear()
                self.rr_old.clear()
                self.queued.clear()
                self.qn = self.qn_old = 0

    def _intl_status(self) -> dict:
        try:
            from . import intl
            return intl.status(self)
        except Exception:  # noqa: BLE001
            return {}

    def slow_counts(self) -> dict:
        """Waiting / recent news waiting / stored: counting millions of rows takes seconds, so it is done in the
        background at most every minute and the dashboard gets the last numbers at once (None the very first time)."""
        c = self.__dict__.setdefault("_sc", {"t": 0.0, "v": {"queue": None, "queue_fresh": None, "stored": None},
                                             "busy": False})
        if time.time() - c["t"] > 60 and not c["busy"]:
            c["busy"] = True

            def run():
                try:
                    cut = self.cutoff()
                    one = lambda sql, a=(): self.rq(sql, a)[0]["n"]  # noqa: E731
                    c["v"] = {"queue": one("SELECT COUNT(*) n FROM articles WHERE status='new'"),
                              "queue_fresh": one("SELECT COUNT(*) n FROM articles WHERE status='new' AND feed_date >= ?",
                                                 (cut,)) + one("SELECT COUNT(*) n FROM articles WHERE status='new' AND "
                                                               "feed_date = '' AND discovered > ? AND source NOT LIKE "
                                                               "'%:old'", (time.time() - 2 * 86400,)),
                              "stored": one("SELECT COUNT(*) n FROM articles WHERE status='success'")}
                    c["t"] = time.time()
                except Exception:  # noqa: BLE001  busy: try again shortly
                    c["t"] = time.time() - 45
                finally:
                    c["busy"] = False
            th = threading.Thread(target=run, daemon=True, name="stream-counts")
            th.start()
            if not c["t"]:
                th.join(3)                             # the very first time: wait a moment (small databases: done)
        return dict(c["v"])

    def live_state(self) -> dict:
        """What only the running engine knows (the worker process writes it for the web process every 2 s)."""
        rec = list(self.recent)
        now = time.time()
        per_min = sum(1 for t in rec if t > now - 300) / 5
        with self.lock:
            return {"t": now, "running": self.running, "since": int(self.started) if self.running else 0,
                    "per_minute": round(per_min, 1), "per_day_pace": int(per_min * 60 * 24),
                    "readers_busy": len(self.busy_domains), "queue_mem": self.qn, "queue_backlog_mem": self.qn_old,
                    "trim": dict(self.__dict__.get("trim") or {}),
                    "last_error": self.last_error,
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

    def lag_minutes(self) -> dict:
        """How fast news arrives: minutes from publication to stored, for today's articles read in the last hour
        (those with a publication time). Median and the share within 30 minutes."""
        now = time.time()
        c = self.__dict__.get("_lag")
        if c and now - c[0] < 60:
            return c[1]
        today = datetime.now(WIB).strftime("%Y-%m-%d")
        lags = []
        try:
            for r in self.rq("SELECT pub_time, fetched FROM articles WHERE status='success' AND fetched > ? AND "
                             "pub_date = ? AND pub_time > '00:00' LIMIT 5000", (now - 3600, today)):
                try:
                    t = datetime.strptime(f"{today} {r['pub_time'][:5]}", "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
                except ValueError:
                    continue
                m = (r["fetched"] - t.timestamp()) / 60
                if -5 <= m <= 24 * 60:
                    lags.append(max(0.0, m))
        except sqlite3.Error:
            pass
        lags.sort()
        out = {"n": len(lags), "median": round(lags[len(lags) // 2]) if lags else None,
               "within30": round(100 * sum(1 for x in lags if x <= 30) / len(lags)) if lags else None}
        self.__dict__["_lag"] = (now, out)
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
                **self.slow_counts(),
                "per_minute": round(per_min, 1), "per_day_pace": int(per_min * 60 * 24), "today": day,
                "hourly": series, "sources": src, "keywords": len(self.keywords()), "db_mb": round(size / 1e6, 1),
                "tiers": {r["tier"] or "new": r["n"] for r in self.rq(
                    "SELECT tier, COUNT(DISTINCT domain) n FROM sources GROUP BY tier")},
                "health": {**dict(self.health), "last_result": json.loads(self.meta("health_result") or "{}")},
                "catchup": {**dict(self.catchup), "last": self.meta("catchup_last"),
                            "last_result": json.loads(self.meta("catchup_result") or "{}")},
                "disk": __import__("crawler.overview", fromlist=["disk"]).disk(),
                "readers_busy": len(self.busy_domains), "last_error": self.last_error, "export": dict(self.export),
                "days": self.days_with_articles(), "fetch_methods": self.fetch_methods(7),
                "maintenance": self.meta("maintenance"), "international": self._intl_status(),
                "latest": [dict(r) for r in rows], "old_hours": str(settings().get("old_hours", "1-6") or ""),
                "late_days": int(settings().get("late_days", 7)), "lag": self.lag_minutes()}
        if live:                                             # the engine runs in its own process: its live state
            for k in ("running", "since", "per_minute", "per_day_pace", "readers_busy", "last_error", "export"):
                out[k] = live.get(k, out[k])
            out["processes"] = live.get("processes", 1)
            ex = [x for x in _WORKER["exits"] if x[0] > time.time() - 86400]
            out["restarts"] = {"n": len(ex), "last": ex[-1][0] if ex else 0, "code": ex[-1][1] if ex else None}
            out["trim"] = live.get("trim") or {}
            out["health"].update(live.get("health") or {})
            out["catchup"].update(live.get("catchup") or {})
            out["worker"] = True
        elif _WORKER_PARENT:
            out["running"] = False
            out["last_error"] = "the stream process is starting or not running"
        return out


_STREAM: Stream | None = None
_GET_LOCK = threading.Lock()


def _shard_of(domain, n) -> int:
    """Which reader process reads an outlet: the same one always (one page at a time per outlet holds across
    processes)."""
    import zlib as _z
    return _z.crc32((domain or "").encode()) % max(1, int(n or 1))


def reader_processes(cfg: dict | None = None) -> int:
    """Processes that read articles: the setting, or automatic — one per CPU core but one (the web server and the
    discovery need a core), at most 4."""
    cfg = cfg or settings()
    n = int(cfg.get("reader_processes") or 0)
    if n <= 0:
        n = max(1, min(4, (os.cpu_count() or 1) - 1))
    return max(1, min(8, n))


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
_IN_WORKER = False                         # set by stream_worker: this process is the stream's main process
_WORKER = {"proc": None, "stop": threading.Event(), "exits": []}   # exits: (time, exit code) of the stream process


def _live_path() -> str:
    return os.path.join(_dir(), "live.json")


def read_live() -> dict | None:
    try:
        with open(_live_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() - float(d.get("t", 0)) >= 20:
        return None
    import glob
    procs = 1
    for p in glob.glob(os.path.join(_dir(), "live_r*.json")):        # the reader processes add their pace
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        if time.time() - float(r.get("t", 0)) < 20 and r.get("running"):
            procs += 1
            for k in ("per_minute", "per_day_pace", "readers_busy"):
                d[k] = round(float(d.get(k) or 0) + float(r.get(k) or 0), 1) if k == "per_minute" else \
                    int(d.get(k) or 0) + int(r.get(k) or 0)
    d["processes"] = procs
    return d


def write_reader_live(k: int, state: dict) -> None:
    os.makedirs(_dir(), exist_ok=True)
    p = os.path.join(_dir(), f"live_r{k}.json")
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(p + ".tmp", p)


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


HUNG_AFTER = 300          # the worker writes its live state every 2 s: 5 minutes without it = hung
STALL_AFTER = 1200        # stream on, links waiting, nothing read for 20 minutes = stalled


def _stuck(started: float, idle_since: float) -> tuple[str, float]:
    """(reason or '', idle_since): the worker hung (its live state stopped being written — only once it has
    written one since it started, so a long start-up is never mistaken for a hang) or the stream stalled."""
    now = time.time()
    try:
        mt = os.path.getmtime(_live_path())
    except OSError:
        return "", 0.0
    if mt < started:                                      # not written since this start yet: still starting up
        return "", 0.0
    if now - mt > HUNG_AFTER:
        return "stopped answering (hung)", 0.0
    try:
        with open(_live_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return "", idle_since
    fresh = int(d.get("queue_mem") or 0) - int(d.get("queue_backlog_mem") or 0)   # by day the archive may wait
    if not settings().get("enabled") or not d.get("running") or fresh <= 0 or \
            float(d.get("per_minute") or 0) > 0 or (d.get("trim") or {}).get("running"):
        return "", 0.0
    if not idle_since:
        return "", now
    return ("read nothing for 20 minutes (stalled)" if now - idle_since > STALL_AFTER else ""), idle_since


def _alert(text: str) -> None:
    """A Telegram message to the admins, when Telegram is set up (never fails)."""
    try:
        from . import telegram_bot
        telegram_bot.send(text)
    except Exception:  # noqa: BLE001
        pass


def start_worker(log=print) -> None:
    """The web server's side: start the worker process and restart it if it ever dies."""
    import subprocess
    import sys
    global _WORKER_PARENT
    _WORKER_PARENT = True

    def keep():
        fails, started, idle_since = 0, 0.0, 0.0
        while not _WORKER["stop"].is_set():
            p = _WORKER["proc"]
            if p is not None and p.poll() is None:
                why = _stuck(started, idle_since)
                idle_since = why[1]
                if why[0]:                                   # alive but hung / not reading: start it afresh
                    log(f"stream worker {why[0]} — restarting it")
                    _alert(f"⚠️ Live Stream {why[0]} — restarted automatically.")
                    p.kill()
                    try:
                        p.wait(30)
                    except Exception:  # noqa: BLE001
                        pass
                    p.returncode = "hung" if why[0].startswith("stopped") else "stalled"
            if p is None or p.poll() is not None:
                if p is not None:
                    fails += 1
                    _WORKER["exits"] = [x for x in _WORKER["exits"] if x[0] > time.time() - 86400][-50:] + \
                        [(time.time(), p.returncode)]
                    log(f"stream worker stopped (exit {p.returncode}) — starting it again")
                    if p.returncode not in ("hung", "stalled"):
                        _alert(f"⚠️ Live Stream stopped (exit {p.returncode}"
                               f"{', out of memory' if p.returncode == -9 else ''}) — restarted automatically.")
                _WORKER["proc"] = subprocess.Popen(          # stdin stays open: the worker ends when we do
                    [sys.executable, "-m", "crawler.stream_worker"], stdin=subprocess.PIPE,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                started, idle_since = time.time(), 0.0
            elif time.time() - started > 3600:
                fails = 0                                    # an hour without trouble: quick restarts again
            _WORKER["stop"].wait(min(60, 5 * (1 + fails)))
    threading.Thread(target=keep, daemon=True, name="stream-supervisor").start()
