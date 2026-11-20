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
DEFAULTS = {"enabled": False, "threads": 16, "discovery_threads": 12, "rss_minutes": 10, "sitemap_minutes": 30,
            "retention_days": 7, "max_age_hours": 48, "domain_gap": 1.5, "rss": True, "sitemaps": True,
            "keywords": []}
LIMITS = {"threads": (2, 64), "discovery_threads": (2, 48), "rss_minutes": (2, 240), "sitemap_minutes": (10, 720),
          "retention_days": (1, 90), "max_age_hours": (6, 168)}
MAX_INTERVAL = 6 * 3600              # a quiet source is still checked at least every 6 h
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
CREATE TABLE IF NOT EXISTS hourly(
  hour TEXT PRIMARY KEY, found INTEGER DEFAULT 0, read INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
  matched INTEGER DEFAULT 0);
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
        elif k in ("rss", "sitemaps"):
            v = bool(v)
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
        self._db.commit()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.running = False
        self.started = 0.0
        self.counts = {"found": 0, "read": 0, "failed": 0, "matched": 0}   # this hour, flushed every few seconds
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
                self._db.execute("UPDATE hourly SET found=found+?, read=read+?, failed=failed+?, matched=matched+? "
                                 "WHERE hour=?", (c["found"], c["read"], c["failed"], c["matched"], _hour()))
                self._db.commit()

    # ----- sources -----
    def sync_sources(self, log=print) -> dict:
        """Every RSS feed and every outlet of the media dictionary becomes a source (new ones only)."""
        from .medmas.media import CORE_MEDIA, MEDIA_DICT, RSS_FEEDS, load_external_dictionary
        from . import news_multi
        from .paths import APP_DATA
        try:
            load_external_dictionary(news_multi._dictionary(APP_DATA), lambda m: None)
        except Exception as e:  # noqa: BLE001
            log(f"stream: media dictionary not loaded ({e})")
        feeds = {d: list(u) for d, u in RSS_FEEDS.items()}
        known = os.path.join(APP_DATA, "news_multi", "medmascrawler.db")
        if os.path.exists(known):
            try:
                c = sqlite3.connect(f"file:{known}?mode=ro", uri=True, timeout=10)
                for d, u in c.execute("SELECT domain, feed_url FROM feed_stats WHERE feed_url != ''"):
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
        doms = sorted(set(MEDIA_DICT) | set(CORE_MEDIA))
        rows += [(f"sm:{d}", "sitemap", d, "", now + random.random() * sm_iv, sm_iv) for d in doms]
        with self.lock:
            before = self._db.total_changes
            self._db.executemany("INSERT OR IGNORE INTO sources(id, kind, domain, url, next_check, interval) "
                                 "VALUES (?,?,?,?,?,?)", rows)
            self._db.commit()
            added = self._db.total_changes - before
        return {"rss": sum(len(u) for u in feeds.values()), "sitemaps": len(doms), "added": added}

    def due_sources(self, limit: int = 64) -> list:
        kinds = [k for k, on in (("rss", self.cfg["rss"]), ("sitemap", self.cfg["sitemaps"])) if on]
        if not kinds:
            return []
        return self.q(f"SELECT * FROM sources WHERE next_check <= ? AND kind IN ({','.join('?' * len(kinds))}) "
                      "ORDER BY next_check LIMIT ?", (time.time(), *kinds, limit))

    def reschedule(self, src, new: int, failed: bool, extra: dict | None = None) -> None:
        base = (self.cfg["rss_minutes"] if src["kind"] == "rss" else self.cfg["sitemap_minutes"]) * 60
        fails = (src["fails"] + 1) if failed else 0
        if failed:
            iv = DEAD_INTERVAL if fails >= 3 else min(MAX_INTERVAL, base * 2 ** fails)
        elif new:
            iv = base
        else:
            iv = min(MAX_INTERVAL, max(base, (src["interval"] or base) * 1.5))     # quiet: check less often
        sets = {"next_check": time.time() + iv * random.uniform(0.9, 1.1), "interval": iv, "fails": fails,
                "checked": time.time(), "hits": src["hits"] + new, **(extra or {})}
        self.x(f"UPDATE sources SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), src["id"]))

    def add_found(self, items: list[dict], source: str) -> int:
        """New article links -> the reading queue. Only articles, only recent ones (max_age_hours)."""
        from .medmas.filters import is_article_link
        cutoff = (datetime.now(WIB) - timedelta(hours=self.cfg["max_age_hours"])).strftime("%Y-%m-%d")
        now, rows = time.time(), []
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http") or not is_article_link(url):
                continue
            d = (it.get("date") or "")[:10]
            if d and d < cutoff:
                continue
            rows.append((norm_key(url), url, it.get("domain") or "", (it.get("title") or "")[:500], source, d,
                         (it.get("time") or "")[:5], now))
        if not rows:
            return 0
        fresh = []
        with self.lock:
            for row in rows:
                cur = self._db.execute("INSERT OR IGNORE INTO articles(key, url, domain, title, source, feed_date, "
                                       "feed_time, discovered) VALUES (?,?,?,?,?,?,?,?)", row)
                if cur.rowcount:
                    fresh.append(row)
            self._db.commit()
            self.counts["found"] += len(fresh)
            if self.running:                     # straight to the readers, at the front: newest first, no waiting
                for key, url, dom, title, _src, fd, ft, _t in fresh:
                    self._enqueue({"key": key, "url": url, "domain": dom, "title": title, "feed_date": fd,
                                   "feed_time": ft}, front=True)
        return len(fresh)

    def _enqueue(self, job: dict, front: bool = False) -> None:
        if job["key"] in self.queued:
            return
        self.queued.add(job["key"])
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

    def check_sitemap(self, src) -> tuple[int, bool, dict]:
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
        months = {datetime.now(WIB).strftime("%Y-%m"), (datetime.now(WIB) - timedelta(days=2)).strftime("%Y-%m")}
        items, ok_any, queue, seen, reads = [], False, list(roots), set(), 0
        while queue and reads < 6:
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
                queue.extend((subs or [x for x, p in ranked if p is not None][-2:])[:3])
                continue
            news = "news" in u.lower() or "berita" in u.lower()
            for e in data:
                if e.get("date") or news:                    # undated entries only from news sitemaps (48 h by rule)
                    items.append({**e, "domain": dom})
        if not ok_any:
            if roots and src["url"]:                         # known sitemaps all failed: look for them again next time
                extra["url"] = ""
            return 0, True, extra
        return self.add_found(items, "sitemap"), False, extra

    def check_source(self, src) -> int:
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
            if sum(len(d) for d in self.by_domain.values()) > 4000:
                return
            rows = self._db.execute("SELECT key, url, domain, title, feed_date, feed_time FROM articles "
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

    def read(self, job: dict) -> str:
        from .medmas.extract import extract_article
        from .medmas.filters import keyword_excluded, keyword_match, url_slug
        res = extract_article(job["url"], 20)
        d = str(res.get("date") or "")[:10] or job.get("feed_date") or ""
        t = str(res.get("time") or "")[:5] or job.get("feed_time") or ""
        status = "success" if res.get("content") and res.get("is_article", True) and not res.get("is_index") else \
            ("failed" if not res.get("content") else "filtered")
        cutoff = (datetime.now(WIB) - timedelta(hours=self.cfg["max_age_hours"])).strftime("%Y-%m-%d")
        if status == "success" and re.match(r"\d{4}-\d{2}-\d{2}$", d) and d < cutoff:
            status = "old"
        matched = []
        if status == "success":
            slug = url_slug(job["url"])
            for kw in self.keywords():
                if keyword_match(kw, res["title"], res["content"], slug) and \
                        not keyword_excluded(kw, res["title"], res["content"], slug):
                    matched.append(kw)
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
        if matched:
            self.bump("matched")
            self.to_archive(job, res, d, t, matched)
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
        while not self.stop_event.is_set():
            got = self.next_job()
            if not got:
                self.stop_event.wait(0.3)
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
                    self.next_ok[dom] = time.time() + float(self.cfg["domain_gap"])
                    self.queued.discard(job["key"])

    # ----- discovery + housekeeping -----
    def discovery_loop(self) -> None:
        with ThreadPoolExecutor(max_workers=self.cfg["discovery_threads"]) as ex:
            while not self.stop_event.is_set():
                with self.lock:
                    room = self.cfg["discovery_threads"] * 2 - len(self.inflight_sources)
                if room > 0:
                    for src in self.due_sources(room):
                        with self.lock:
                            if src["id"] in self.inflight_sources:
                                continue
                            self.inflight_sources.add(src["id"])
                        ex.submit(self._check_one, src)
                self.stop_event.wait(1.0)

    def _check_one(self, src) -> None:
        try:
            if not self.stop_event.is_set():
                self.check_source(src)
        finally:
            with self.lock:
                self.inflight_sources.discard(src["id"])

    def house_loop(self) -> None:
        last_purge = 0.0
        while not self.stop_event.is_set():
            self.flush_counts()
            self.refill()
            if time.time() - last_purge > 1800:
                last_purge = time.time()
                self.purge()
                self.cfg = settings()
            self.stop_event.wait(3.0)
        self.flush_counts()

    def purge(self) -> int:
        cut = time.time() - self.cfg["retention_days"] * 86400
        n = self.x("DELETE FROM articles WHERE discovered < ?", (cut,))
        day = (datetime.now(WIB) - timedelta(days=max(30, self.cfg["retention_days"]))).strftime("%Y-%m-%d")
        self.x("DELETE FROM hourly WHERE hour < ?", (day,))
        return n

    # ----- control -----
    def start(self, log=print) -> None:
        with self.lock:
            if self.running:
                return
            self.cfg = settings()
            self.stop_event.clear()
            self.running, self.started = True, time.time()
        info = self.sync_sources(log)
        log(f"🛰 News stream started: {info['rss']} RSS feeds + {info['sitemaps']} outlet sitemaps, "
            f"{self.cfg['threads']} readers")
        self.threads = [threading.Thread(target=self.discovery_loop, daemon=True, name="stream-discovery"),
                        threading.Thread(target=self.house_loop, daemon=True, name="stream-house")]
        self.threads += [threading.Thread(target=self.reader_loop, daemon=True, name=f"stream-reader-{i}")
                         for i in range(self.cfg["threads"])]
        for t in self.threads:
            t.start()

    def stop(self) -> None:
        with self.lock:
            if not self.running:
                return
            self.running = False
        self.stop_event.set()
        for t in self.threads:
            t.join(timeout=25)
        self.threads = []
        with self.lock:
            self.by_domain.clear()
            self.rr.clear()
            self.queued.clear()
            self.busy_domains.clear()

    def status(self, latest: int = 30, q: str = "") -> dict:
        now = time.time()
        today = datetime.now(WIB).strftime("%Y-%m-%d")
        hours = [(datetime.now(WIB) - timedelta(hours=i)).strftime("%Y-%m-%d %H") for i in range(23, -1, -1)]
        got = {r["hour"]: dict(r) for r in self.q("SELECT * FROM hourly WHERE hour >= ?", (hours[0],))}
        with self.lock:
            live = dict(self.counts)
        series = []
        for h in hours:
            r = got.get(h, {})
            series.append({"hour": h, **{k: int(r.get(k, 0)) + (live[k] if h == _hour() else 0)
                                         for k in ("found", "read", "failed", "matched")}})
        day = {k: sum(s[k] for s in series if s["hour"].startswith(today)) for k in ("found", "read", "failed", "matched")}
        src = {r["kind"]: dict(r) for r in self.q(
            "SELECT kind, COUNT(*) n, SUM(fails >= 3) dead, SUM(hits > 0) productive, SUM(checked > 0) checked "
            "FROM sources GROUP BY kind")}
        where, args = "status='success'", []
        if q:
            where += " AND (title LIKE ? OR matched LIKE ?)"
            args = [f"%{q}%", f"%{q}%"]
        rows = self.q(f"SELECT url, domain, title, pub_date, pub_time, matched, fetched FROM articles WHERE {where} "
                      "ORDER BY fetched DESC LIMIT ?", (*args, latest))
        try:
            size = os.path.getsize(self.db_path) + (os.path.getsize(self.db_path + "-wal")
                                                    if os.path.exists(self.db_path + "-wal") else 0)
        except OSError:
            size = 0
        per_min = sum(1 for t in self.recent if t > now - 300) / 5
        return {"running": self.running, "since": int(self.started) if self.running else 0, "settings": settings(),
                "queue": self.q("SELECT COUNT(*) n FROM articles WHERE status='new'")[0]["n"],
                "stored": self.q("SELECT COUNT(*) n FROM articles WHERE status='success'")[0]["n"],
                "per_minute": round(per_min, 1), "per_day_pace": int(per_min * 60 * 24), "today": day,
                "hourly": series, "sources": src, "keywords": len(self.keywords()), "db_mb": round(size / 1e6, 1),
                "readers_busy": len(self.busy_domains), "last_error": self.last_error,
                "latest": [dict(r) for r in rows]}


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
        restart = s.running and any(old.get(k) != new.get(k) for k in ("threads", "discovery_threads"))
        if restart:
            def again():
                s.stop()
                s.start(log)
            threading.Thread(target=again, daemon=True).start()
    if isinstance(data.get("enabled"), bool):
        out = set_enabled(data["enabled"], log)
    return {**settings(), **out}
