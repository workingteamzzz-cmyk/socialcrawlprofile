"""SQLite storage: every link & article is kept permanently.

This is what makes crawling AUTOMATIC and repeatable: links seen before are
never processed twice, and results accumulate across runs.

Databases created by older (Indonesian-schema) versions are migrated
automatically: columns are renamed and status values translated.
"""

import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    url             TEXT PRIMARY KEY,
    keyword         TEXT,
    source          TEXT,          -- google_news / sitemap / rss / bing_news / import
    media           TEXT,
    title           TEXT DEFAULT '',
    published_date  TEXT DEFAULT '',
    published_time  TEXT DEFAULT '',
    discovered_at   TEXT,
    batch           TEXT DEFAULT '',    -- run that (re)touched this link
    status          TEXT DEFAULT 'new'  -- new/success/needs_review/failed/filtered
);
CREATE TABLE IF NOT EXISTS articles (
    url             TEXT PRIMARY KEY REFERENCES links(url),
    title           TEXT, media TEXT, author TEXT,
    published_date  TEXT, published_time TEXT,
    summary         TEXT, content TEXT,
    image           TEXT DEFAULT '',
    influencer      TEXT DEFAULT '',
    persons         TEXT DEFAULT '',
    sentiment       TEXT DEFAULT '',
    method          TEXT, error TEXT,
    fetched_at      TEXT
);
-- A link can belong to SEVERAL jobs (found again in a later run). Jobs are
-- historical records, so membership is many-to-many: re-processing a link
-- never empties the earlier job's export.
CREATE TABLE IF NOT EXISTS link_batch (
    url    TEXT,
    batch  TEXT,
    PRIMARY KEY (url, batch)
);
CREATE INDEX IF NOT EXISTS idx_link_batch_batch ON link_batch(batch);
CREATE TABLE IF NOT EXISTS runs (
    batch       TEXT PRIMARY KEY,   -- job id, also the export scope
    kind        TEXT,               -- crawl | import
    label       TEXT,               -- keywords used / "N pasted links"
    started_at  TEXT,
    finished_at TEXT DEFAULT ''
);
-- Outlets rejected by registered-media-only mode, with how many articles
-- they would have contributed. The review queue for growing the whitelist.
CREATE TABLE IF NOT EXISTS candidates (
    domain      TEXT PRIMARY KEY,
    hits        INTEGER DEFAULT 0,
    last_seen   TEXT
);
-- Sitemap productivity & rotation: with thousands of outlets in the user's
-- dictionary we cannot read every sitemap every run, so we remember which
-- ones pay off and which were checked least recently.
CREATE TABLE IF NOT EXISTS sitemap_stats (
    domain       TEXT PRIMARY KEY,
    last_checked TEXT DEFAULT '',
    checks       INTEGER DEFAULT 0,
    hits         INTEGER DEFAULT 0
);
-- Google News links that could not be decoded yet (Google throttles bulk
-- decoding): kept here and retried on later runs instead of being dropped,
-- so throttling never costs a clip (anti missed clip).
CREATE TABLE IF NOT EXISTS decode_queue (
    url       TEXT PRIMARY KEY,   -- the news.google.com link
    keyword   TEXT,
    source    TEXT,
    title     TEXT,
    feed_date TEXT,
    feed_time TEXT,
    attempts  INTEGER DEFAULT 1,
    batch     TEXT,               -- last run that tried it
    added_at  TEXT
);
-- Auto-discovered RSS feeds, one row per dictionary outlet probed.
-- feed_url = '' means probed but nothing found (re-probed eventually).
CREATE TABLE IF NOT EXISTS feed_stats (
    domain       TEXT PRIMARY KEY,
    feed_url     TEXT DEFAULT '',
    last_checked TEXT DEFAULT '',
    checks       INTEGER DEFAULT 0,
    hits         INTEGER DEFAULT 0
);
-- Per-media (site:) harvest rotation: which outlets were queried least
-- recently, so the whole dictionary is covered across runs.
CREATE TABLE IF NOT EXISTS harvest_stats (
    domain    TEXT PRIMARY KEY,
    last_used TEXT DEFAULT ''
);
-- Outlet liveness, learned from normal runs and the one-click health
-- check. fails = consecutive unreachable probes; any success resets to 0.
CREATE TABLE IF NOT EXISTS domain_health (
    domain       TEXT PRIMARY KEY,
    fails        INTEGER DEFAULT 0,
    last_checked TEXT DEFAULT '',
    last_ok      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS media_domain (
    domain          TEXT PRIMARY KEY,  -- auto-learned media domains
    discovered_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_status ON links(status);
CREATE INDEX IF NOT EXISTS idx_links_keyword ON links(keyword);
"""

_STATUS_MIGRATION = {"baru": "new", "berhasil": "success",
                     "perlu_cek": "needs_review", "gagal": "failed",
                     "difilter": "filtered"}
_LINKS_RENAMES = {"sumber": "source", "judul": "title",
                  "tanggal": "published_date", "ditemukan": "discovered_at"}
_ARTICLE_RENAMES = {"judul": "title", "tanggal": "published_date",
                    "jam": "published_time", "ringkasan": "summary",
                    "konten": "content", "foto": "image",
                    "narasumber": "influencer", "sentimen": "sentiment",
                    "metode": "method", "diambil": "fetched_at"}


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # timeout + WAL: the pipelined extractor, the dashboard's pollers
        # and the sentiment step all touch the database CONCURRENTLY.
        # WAL lets readers run during writes; the busy timeout makes any
        # residual contention wait instead of raising 'database is locked'.
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     timeout=30)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        self._migrate_legacy()
        self._conn.executescript(_SCHEMA)
        # light migration: new columns on databases created before they existed
        for col in ("image", "influencer", "persons", "sentiment",
                    "organizations"):
            try:
                self._conn.execute(
                    f"ALTER TABLE articles ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        for col in ("published_time", "batch"):
            try:
                self._conn.execute(
                    f"ALTER TABLE links ADD COLUMN {col} TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        try:
            self._conn.execute(
                "ALTER TABLE decode_queue ADD COLUMN domain TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()
        self._lock = threading.Lock()

    def _cols(self, table):
        try:
            return {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            return set()

    def _migrate_legacy(self):
        """Rename Indonesian-era table/columns/status values to English."""
        try:
            if "artikel" in {r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                if "articles" not in {r[0] for r in self._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")}:
                    self._conn.execute("ALTER TABLE artikel RENAME TO articles")
            for table, renames in (("links", _LINKS_RENAMES),
                                   ("articles", _ARTICLE_RENAMES)):
                cols = self._cols(table)
                for old, new in renames.items():
                    if old in cols and new not in cols:
                        self._conn.execute(
                            f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            if "links" in {r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}:
                for old, new in _STATUS_MIGRATION.items():
                    self._conn.execute(
                        "UPDATE links SET status=? WHERE status=?", (new, old))
            mdcols = self._cols("media_domain")
            if "ditemukan" in mdcols and "discovered_at" not in mdcols:
                self._conn.execute(
                    "ALTER TABLE media_domain RENAME COLUMN ditemukan TO discovered_at")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # fresh database or already migrated

    def _now(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def add_link(self, url, keyword, source, media, title="",
                 published_date="", published_time="", batch="", retag=True):
        """Store a link. Return True if it is NEW (never seen before).

        retag=True: known links are re-tagged with the current batch, so
        this run's export contains everything this run FOUND.
        retag=False ('report only new'): known links keep their old job —
        a twice-daily schedule then delivers clean increments: the evening
        report contains only what the noon report didn't."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO links "
                "(url, keyword, source, media, title, published_date, "
                "published_time, discovered_at, batch) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (url, keyword, source, media, title, published_date,
                 published_time, self._now(), batch))
            is_new = cur.rowcount > 0
            if batch and (is_new or retag):
                if not is_new:
                    self._conn.execute("UPDATE links SET batch=? WHERE url=?",
                                       (batch, url))
                self._conn.execute(
                    "INSERT OR IGNORE INTO link_batch (url, batch) VALUES (?,?)",
                    (url, batch))
            self._conn.commit()
            return is_new

    def tag_batch(self, url, batch):
        """Attach a link to a job. Used when a run extracts links that were
        queued by an earlier run, so this run's export contains them."""
        if not batch:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO link_batch (url, batch) VALUES (?,?)",
                (url, batch))
            self._conn.commit()

    def requeue(self, urls, force=False):
        """Re-extract specific links (explicit bulk import): anything not
        already successful goes back to the 'new' queue. force=True also
        re-extracts successful ones (fresh content after engine upgrades)."""
        sql = ("UPDATE links SET status='new' WHERE url=?" if force else
               "UPDATE links SET status='new' WHERE url=? AND status<>'success'")
        with self._lock:
            n = 0
            for url in urls:
                n += self._conn.execute(sql, (url,)).rowcount
            self._conn.commit()
            return n

    def start_run(self, batch, kind, label):
        """Register a job so the dashboard can list and re-download it."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (batch, kind, label, started_at) "
                "VALUES (?,?,?,?)", (batch, kind, label[:300], self._now()))
            self._conn.commit()

    def finish_run(self, batch):
        with self._lock:
            self._conn.execute("UPDATE runs SET finished_at=? WHERE batch=?",
                               (self._now(), batch))
            self._conn.commit()

    def list_runs(self, limit=15):
        """Recent jobs with their live article counts."""
        q = ("SELECT r.batch, r.kind, r.label, r.started_at, r.finished_at, "
             "(SELECT COUNT(*) FROM link_batch b WHERE b.batch=r.batch) AS links, "
             "(SELECT COUNT(*) FROM link_batch b JOIN links l ON l.url=b.url "
             " WHERE b.batch=r.batch AND l.status='success') AS success "
             "FROM runs r ORDER BY r.batch DESC LIMIT ?")
        return [dict(r) for r in self._conn.execute(q, (limit,))]

    def delete_batch(self, batch):
        """Remove one job. Links shared with another job stay — only links
        belonging exclusively to this job are deleted with their articles."""
        with self._lock:
            self._conn.execute("DELETE FROM link_batch WHERE batch=?", (batch,))
            orphan_sql = ("SELECT url FROM links WHERE url NOT IN "
                          "(SELECT url FROM link_batch)")
            orphans = [r["url"] for r in self._conn.execute(orphan_sql)]
            for url in orphans:
                self._conn.execute("DELETE FROM articles WHERE url=?", (url,))
                self._conn.execute("DELETE FROM links WHERE url=?", (url,))
            self._conn.execute("DELETE FROM runs WHERE batch=?", (batch,))
            self._conn.commit()
            return len(orphans)

    def latest_batch(self):
        row = self._conn.execute(
            "SELECT batch FROM links WHERE batch<>'' "
            "ORDER BY batch DESC LIMIT 1").fetchone()
        return row["batch"] if row else ""

    def clear_all(self, keep_learned_media=True):
        """Wipe collected links & articles — a clean slate for a new job.
        Learned media domains are kept by default (that knowledge is not
        'output' and makes future crawls better)."""
        with self._lock:
            counts = {
                "links": self._conn.execute("SELECT COUNT(*) c FROM links").fetchone()["c"],
                "articles": self._conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
            }
            self._conn.execute("DELETE FROM articles")
            self._conn.execute("DELETE FROM links")
            self._conn.execute("DELETE FROM link_batch")
            self._conn.execute("DELETE FROM runs")
            self._conn.execute("DELETE FROM decode_queue")
            if not keep_learned_media:
                self._conn.execute("DELETE FROM media_domain")
            self._conn.commit()
            self._conn.execute("VACUUM")
            return counts

    def pending_links(self, keyword=None):
        """Links with status 'new' that have not been extracted yet.
        Returns (url, keyword, feed_date, feed_time) — the feed stamps serve
        as fallbacks when the article page exposes no date/time."""
        q = ("SELECT url, keyword, published_date, published_time "
             "FROM links WHERE status='new'")
        args = ()
        if keyword:
            q += " AND keyword=?"
            args = (keyword,)
        return [(r["url"], r["keyword"], r["published_date"] or "",
                 r["published_time"] or "")
                for r in self._conn.execute(q, args)]

    def set_status(self, url, status):
        with self._lock:
            self._conn.execute("UPDATE links SET status=? WHERE url=?", (status, url))
            self._conn.commit()

    def add_domain(self, domain):
        """Record a detected media domain (auto-learn coverage)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO media_domain (domain, discovered_at) VALUES (?,?)",
                (domain, self._now()))
            self._conn.commit()

    def pick_sitemap_domains(self, all_domains, limit, first=()):
        """Choose which sitemaps to read this run.

        Order: outlets already seen carrying the keyword this run, then the
        most productive ones historically, then those checked longest ago
        (never-checked first). Over successive runs the whole dictionary
        gets covered without ever stalling a single run."""
        known = {r["domain"]: (r["hits"], r["last_checked"])
                 for r in self._conn.execute(
                     "SELECT domain, hits, last_checked FROM sitemap_stats")}
        head = [d for d in dict.fromkeys(first) if d]
        head_set = set(head)
        rest = [d for d in sorted(set(all_domains)) if d not in head_set]
        rest.sort(key=lambda d: (-known.get(d, (0, ""))[0],   # most hits first
                                 known.get(d, (0, ""))[1]))   # then oldest check
        ordered = head + rest
        return ordered if not limit else ordered[:limit]

    def record_sitemap_results(self, counts):
        """counts: {domain: articles_found_this_run} for every domain read."""
        with self._lock:
            for domain, found in counts.items():
                self._conn.execute(
                    "INSERT INTO sitemap_stats (domain, last_checked, checks, hits) "
                    "VALUES (?,?,1,?) ON CONFLICT(domain) DO UPDATE SET "
                    "last_checked=?, checks=checks+1, hits=hits+?",
                    (domain, self._now(), found, self._now(), found))
            self._conn.commit()

    def record_domain_health(self, results, weight=1):
        """results: {domain: alive_bool}. weight=2 for the dedicated health
        check (it double-probes failures, so one census is enough evidence
        to skip); normal runs use weight=1 (two bad runs before skipping)."""
        now = self._now()
        with self._lock:
            for d, alive in results.items():
                if alive:
                    self._conn.execute(
                        "INSERT INTO domain_health (domain, fails, "
                        "last_checked, last_ok) VALUES (?,0,?,?) "
                        "ON CONFLICT(domain) DO UPDATE SET fails=0, "
                        "last_checked=?, last_ok=?",
                        (d, now, now, now, now))
                else:
                    self._conn.execute(
                        "INSERT INTO domain_health (domain, fails, "
                        "last_checked) VALUES (?,?,?) "
                        "ON CONFLICT(domain) DO UPDATE SET "
                        "fails=fails+?, last_checked=?",
                        (d, weight, now, weight, now))
            self._conn.commit()

    def dead_domains(self, min_fails=2, recheck_days=14):
        """Outlets to skip: unreachable min_fails+ consecutive times, last
        checked within the recheck window. Older entries get another
        chance automatically — dead sites do come back (anti missed clip:
        nothing is skipped forever)."""
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - recheck_days * 86400))
        return {r["domain"] for r in self._conn.execute(
            "SELECT domain FROM domain_health "
            "WHERE fails >= ? AND last_checked >= ?", (min_fails, cutoff))}

    def health_summary(self):
        row = self._conn.execute(
            "SELECT SUM(fails = 0) alive, SUM(fails > 0) failing "
            "FROM domain_health").fetchone()
        return {"alive": row["alive"] or 0, "failing": row["failing"] or 0}

    def known_feeds(self):
        """{domain: feed_url} for every outlet with a working discovered feed."""
        return {r["domain"]: r["feed_url"] for r in self._conn.execute(
            "SELECT domain, feed_url FROM feed_stats WHERE feed_url != ''")}

    def pick_feed_discovery(self, pool, limit):
        """Outlets to probe for an RSS feed this run: never-probed first,
        then the ones probed longest ago (dead sites get re-checked
        eventually — outlets do add feeds over time)."""
        checked = {r["domain"]: r["last_checked"] for r in self._conn.execute(
            "SELECT domain, last_checked FROM feed_stats")}
        ordered = sorted(set(pool),
                         key=lambda d: (d in checked, checked.get(d, ""), d))
        return ordered[:limit] if limit else ordered

    def record_feed_probe(self, domain, feed_url):
        with self._lock:
            self._conn.execute(
                "INSERT INTO feed_stats (domain, feed_url, last_checked, checks) "
                "VALUES (?,?,?,1) ON CONFLICT(domain) DO UPDATE SET "
                "feed_url=?, last_checked=?, checks=checks+1",
                (domain, feed_url, self._now(), feed_url, self._now()))
            self._conn.commit()

    def record_feed_hits(self, counts):
        """counts: {domain: keyword-matching articles from its feed}."""
        with self._lock:
            for domain, n in counts.items():
                self._conn.execute(
                    "UPDATE feed_stats SET hits = hits + ? WHERE domain=?",
                    (n, domain))
            self._conn.commit()

    def harvest_last_used(self):
        return {r["domain"]: r["last_used"] for r in self._conn.execute(
            "SELECT domain, last_used FROM harvest_stats")}

    def record_harvest_used(self, domains):
        with self._lock:
            now = self._now()
            self._conn.executemany(
                "INSERT INTO harvest_stats (domain, last_used) VALUES (?,?) "
                "ON CONFLICT(domain) DO UPDATE SET last_used=excluded.last_used",
                [(d, now) for d in domains])
            self._conn.commit()

    def queue_undecoded(self, items, keyword, batch="", attempts=1):
        """Keep Google links that failed to decode, for retry in later runs.

        attempts=0 is the crash-safety path: links are parked here right
        after DISCOVERY (before any decode attempt), so killing the app
        mid-run never loses them. Parking again does not bump the failure
        count — only a real failed decode (attempts=1) does."""
        bump = ("attempts = attempts + 1" if attempts
                else "attempts = attempts")
        with self._lock:
            for t in items:
                self._conn.execute(
                    "INSERT INTO decode_queue (url, keyword, source, title, "
                    "feed_date, feed_time, attempts, batch, added_at, domain) "
                    f"VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET "
                    f"{bump}, batch = excluded.batch, "
                    "domain = COALESCE(NULLIF(excluded.domain,''), domain)",
                    (t["url"], t.get("keyword") or keyword, t.get("source", ""),
                     t.get("title", ""), t.get("date", ""), t.get("time", ""),
                     attempts, batch, self._now(), t.get("domain", "")))
            self._conn.commit()

    def purge_decode_queue(self, max_attempts=3):
        """Drop links that failed decoding max_attempts times. Returns count."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM decode_queue WHERE attempts >= ?", (max_attempts,))
            self._conn.commit()
            return cur.rowcount

    def decode_retries(self, exclude_batch="", limit=1500):
        """Google links still awaiting decoding, oldest first. Links already
        tried by the current run (exclude_batch) are skipped."""
        rows = self._conn.execute(
            "SELECT url, keyword, source, title, feed_date, feed_time, domain "
            "FROM decode_queue WHERE batch != ? ORDER BY added_at LIMIT ?",
            (exclude_batch, limit))
        return [{"url": r["url"], "keyword": r["keyword"], "source": r["source"],
                 "title": r["title"], "date": r["feed_date"],
                 "time": r["feed_time"], "domain": r["domain"]} for r in rows]

    def remove_decoded(self, urls):
        with self._lock:
            self._conn.executemany("DELETE FROM decode_queue WHERE url=?",
                                   [(u,) for u in urls])
            self._conn.commit()

    def add_candidate(self, domain, hits=1):
        """Record an outlet skipped by the whitelist (counts accumulate)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO candidates (domain, hits, last_seen) VALUES (?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET hits = hits + ?, last_seen = ?",
                (domain, hits, self._now(), hits, self._now()))
            self._conn.commit()

    def list_candidates(self, limit=40):
        return [dict(r) for r in self._conn.execute(
            "SELECT domain, hits, last_seen FROM candidates "
            "ORDER BY hits DESC, domain LIMIT ?", (limit,))]

    def drop_candidate(self, domain):
        with self._lock:
            self._conn.execute("DELETE FROM candidates WHERE domain=?", (domain,))
            self._conn.commit()

    def all_domains(self):
        return {r["domain"] for r in self._conn.execute("SELECT domain FROM media_domain")}

    def needs_review_rows(self, limit=5000):
        """needs_review articles WITH content — candidates for automatic
        re-verification when keyword rules improve."""
        return [(r["url"], r["keyword"] or "", r["title"] or "",
                 r["content"] or "") for r in self._conn.execute(
            "SELECT l.url, l.keyword, a.title, a.content FROM links l "
            "JOIN articles a ON a.url = l.url "
            "WHERE l.status='needs_review' AND a.content != '' LIMIT ?",
            (limit,))]

    def reset_failed(self):
        """Re-queue every failed link for another extraction attempt."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE links SET status='new' WHERE status='failed'")
            self._conn.commit()
            return cur.rowcount

    def save_article(self, url, data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO articles "
                "(url, title, media, author, published_date, published_time, "
                "summary, content, image, influencer, persons, sentiment, "
                "organizations, method, error, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (url, data.get("title", ""), data.get("media", ""),
                 data.get("author", ""), data.get("date", ""), data.get("time", ""),
                 data.get("summary", ""), data.get("content", ""),
                 data.get("image", ""), data.get("influencer", ""),
                 data.get("persons", ""), data.get("sentiment", ""),
                 data.get("organizations", ""),
                 data.get("method", ""), data.get("error", ""), self._now()))
            self._conn.commit()

    def articles_without_sentiment(self):
        """Successful/needs_review articles that lack a sentiment label."""
        q = ("SELECT a.url, a.title, a.summary FROM articles a "
             "JOIN links l ON l.url = a.url "
             "WHERE COALESCE(a.sentiment,'')='' AND a.content<>'' "
             "AND l.status IN ('success','needs_review')")
        return [(r["url"], r["title"] or "", r["summary"] or "")
                for r in self._conn.execute(q)]

    def set_sentiment(self, url, value):
        with self._lock:
            self._conn.execute(
                "UPDATE articles SET sentiment=? WHERE url=?", (value, url))
            self._conn.commit()

    def all_results(self, keyword=None, batch=None):
        """Join links + articles for export.
        batch: limit to one run (use latest_batch() for 'this run only')."""
        q = ("SELECT l.url, l.keyword, l.source, l.status, l.discovered_at, "
             "COALESCE(a.title, l.title) AS title, "
             "COALESCE(NULLIF(a.media,''), l.media) AS media, "
             "a.author, "
             "COALESCE(NULLIF(a.published_date,''), l.published_date) AS published_date, "
             "COALESCE(NULLIF(a.published_time,''), l.published_time) AS published_time, "
             "a.summary, a.content, a.image, a.influencer, "
             "a.persons, a.sentiment, a.organizations, a.method, a.error "
             "FROM links l LEFT JOIN articles a ON a.url = l.url")
        where, args = [], []
        if batch:
            q += " JOIN link_batch b ON b.url = l.url"
            where.append("b.batch=?")
            args.append(batch)
        if keyword:
            where.append("l.keyword=?")
            args.append(keyword)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY published_date DESC, l.discovered_at DESC"
        return [dict(r) for r in self._conn.execute(q, tuple(args))]

    def stats(self):
        s = {}
        for r in self._conn.execute("SELECT status, COUNT(*) n FROM links GROUP BY status"):
            s[r["status"]] = r["n"]
        s["total"] = sum(s.values())
        return s

    def close(self):
        self._conn.close()
