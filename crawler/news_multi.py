"""News Search, engine "Multi-source": the team's medmascrawler (crawler/medmas, its code unchanged) run from the
dashboard. No browser: Google News RSS split by date + per-media site: queries, media sitemaps, media RSS feeds,
Bing News, GDELT (and DuckDuckGo if installed); every article read with 4-layer extraction; INFLUENCER / PERSON /
ORGANIZATION detected; sentiment by lexicon. Its SQLite database (data folder) remembers every link across runs.

The results come back as ordinary news rows, so the dashboard, News Links, the Online / Standard Excel layouts,
schedules and deliveries work exactly as with the browser engine."""
from __future__ import annotations

import os
import re
import shutil
import threading
from datetime import datetime

SOURCES = {        # dashboard name -> medmascrawler Config field
    "gnews": "use_google_news", "permedia": "per_media_harvest", "sitemap": "use_sitemap",
    "rss": "use_rss", "bing": "use_bing", "gdelt": "use_gdelt", "ddg": "use_duckduckgo"}
DEFAULT_SOURCES = ["gnews", "permedia", "sitemap", "rss", "bing", "gdelt"]
_RUN_LOCK = threading.Lock()                  # the engine keeps module-level state: one run at a time


def _dictionary(data_dir: str) -> str:
    """The media dictionary: the admin's own file in the data folder if there is one, else the bundled ~12,000 outlets."""
    for name in ("media_dictionary.xlsx", "media_dictionary.csv", "media_dictionary.txt"):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(__file__), "medmas", "media_dictionary.txt")


def _row(h: dict, tz) -> dict:
    """A medmascrawler result -> a news row of this app (same keys as the browser engine's 'full' detail)."""
    from .medmas.extract import clean_title
    from .medmas.media import media_name
    d = str(h.get("published_date") or "")[:10]
    t = str(h.get("published_time") or "")[:5]
    full = f"{d} {t}" if re.match(r"\d{4}-\d{2}-\d{2}$", d) and re.match(r"\d{2}:\d{2}$", t) else ""
    ts = 0.0
    if re.match(r"\d{4}-\d{2}-\d{2}$", d):
        try:
            ts = datetime.strptime(full or d, "%Y-%m-%d %H:%M" if full else "%Y-%m-%d").replace(tzinfo=tz).timestamp()
        except ValueError:
            pass
    return {"url": h["url"], "title": clean_title(h.get("title") or "", h["url"]),
            "media": h.get("media") or media_name(h["url"]), "published": d if ts else "", "ts": ts,
            "published_full": full, "author": (h.get("author") or "").strip(),
            "text": h.get("content") or "", "description": h.get("summary") or "", "image": h.get("image") or "",
            "influencer": h.get("influencer") or "", "person": h.get("persons") or "",
            "organization": h.get("organizations") or "", "sentiment": (h.get("sentiment") or "").lower(),
            "source": h.get("source") or "", "status": h.get("status") or "", "keyword": h.get("keyword") or ""}


def results(db, batch: str, start: str, end: str, include_review: bool) -> list[dict]:
    """This run's articles, chosen the way the engine's own Upload Online export chooses them: 'success'
    (+ 'needs_review' if asked), needs-review rows whose keyword now verifies promoted, listing pages left out,
    undated ones dated by discovery, and the period enforced."""
    from .medmas.extract import looks_like_index_page
    from .medmas.filters import keyword_excluded, keyword_match, url_slug
    ok = {"success"} | ({"needs_review"} if include_review else set())
    out = []
    for h in db.all_results(batch=batch):
        st = h["status"]
        if st not in ok:
            if not (st == "needs_review" and h.get("content") and h.get("keyword")):
                continue
            slug = url_slug(h["url"])
            if keyword_excluded(h["keyword"], h["title"], h["content"], slug) or \
                    not keyword_match(h["keyword"], h["title"], h["content"], slug):
                continue
            h["status"] = "success"
        if looks_like_index_page(h.get("title") or "", h["url"]):
            continue
        d = str(h.get("published_date") or "")[:10]
        if not re.match(r"\d{4}-\d{2}-\d{2}", d):
            d = str(h.get("discovered_at") or "")[:10]
            h["published_date"] = d if re.match(r"\d{4}-\d{2}-\d{2}", d) else ""
        if start and end and not (start <= str(h["published_date"])[:10] <= end):
            continue
        out.append(h)
    return out


def engine_keyword(kw: str, opt) -> str:
    """The dashboard's keyword + "Exact phrase" + "Exclude words" -> the engine's boolean syntax."""
    q = kw.strip()
    plain = not re.search(r'["()]|\b(AND|OR|NOT)\b', q)
    if opt.exact and plain and " " in q:
        q = f'"{q}"'
    for w in opt.exclude:
        q += f' NOT "{w}"' if " " in w else f" NOT {w}"
    return q


COVERAGE = {   # media checked per run: sitemaps read, outlets probed for an RSS feed, outlets searched one by one
    "normal": {"sitemaps": 400, "feeds": 300, "harvest": 0},
    "wide": {"sitemaps": 1000, "feeds": 1000, "harvest": 500},
}


def _priority_first(pipeline, Database, priority: list[str]):
    """Priority media go to the front of every rotation (and are never cut by its per-run cap) — done by wrapping
    two of the engine's choosers for this run, without changing the engine's code. Returns an undo function."""
    pri = [d for d in dict.fromkeys(priority) if d]
    if not pri:
        return lambda: None
    orig_h, orig_s, orig_f = pipeline._harvest_domains, Database.pick_sitemap_domains, Database.pick_feed_discovery

    def front(lst, limit):
        out = pri + [d for d in lst if d not in pri]
        return out[:max(limit, len(pri))] if limit else out

    def harvest(cfg, detected, log=print, db=None):
        got = orig_h(cfg, detected, log, db)
        return front(got, cfg.max_media_harvest or 0)

    def sitemaps(self, all_domains, limit, first=()):
        return front(orig_s(self, set(all_domains) | set(pri), 0, first), limit)

    def feeds(self, pool, limit):                     # pool = outlets with no known feed yet
        pool = set(pool)
        got = orig_f(self, pool, 0)
        head = [d for d in pri if d in pool]
        out = head + [d for d in got if d not in head]
        return out[:max(limit, len(head))] if limit else out
    pipeline._harvest_domains, Database.pick_sitemap_domains, Database.pick_feed_discovery = harvest, sitemaps, feeds

    def undo():
        pipeline._harvest_domains, Database.pick_sitemap_domains, Database.pick_feed_discovery = orig_h, orig_s, orig_f
    return undo


def _shared_reads(ttl: float = 15 * 60):
    """Within one run every keyword reads the same media sitemaps and RSS feeds: fetch each once and share it
    (the engine itself reads them again per keyword). Wraps the two fetchers for this run only; returns undo."""
    from .medmas.discovery import rss_feeds, sitemaps
    import time as _t
    cache: dict = {}
    lock = threading.Lock()
    orig_feed, orig_sm = rss_feeds.fetch_feed, sitemaps._fetch

    def once(key, fn):
        with lock:
            hit = cache.get(key)
        if hit and _t.time() - hit[0] < ttl:
            if isinstance(hit[1], Exception):
                raise hit[1]
            return hit[1]
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001  a dead site fails once per run, not once per keyword
            with lock:
                cache[key] = (_t.time(), e)
            raise
        with lock:
            cache[key] = (_t.time(), val)
        return val

    rss_feeds.fetch_feed = lambda url, timeout=20, fallback=True: once(("rss", url), lambda: orig_feed(url, timeout, fallback))
    sitemaps._fetch = lambda url, timeout=8: once(("sm", url), lambda: orig_sm(url, timeout))

    def undo():
        rss_feeds.fetch_feed, sitemaps._fetch = orig_feed, orig_sm
    return undo


def _live(Database, on_hit, start: str, end: str, include_review: bool):
    """Every article the engine accepts is handed over the moment it has been read — not at the end of the run —
    so it reaches the reporting database, the dashboard and the Excel file while the search goes on. Done by
    wrapping three of the engine's database calls for this run (its code unchanged). Returns an undo function."""
    from .medmas.extract import looks_like_index_page
    if on_hit is None:
        return lambda: None
    orig_p, orig_a, orig_s = Database.pending_links, Database.save_article, Database.set_status
    kw_of: dict = {}
    art: dict = {}
    lock = threading.Lock()
    ok = {"success"} | ({"needs_review"} if include_review else set())

    def pending(self, *a, **k):
        rows = orig_p(self, *a, **k)
        with lock:
            for r in rows:
                kw_of[r[0]] = r[1]
        return rows

    def save_article(self, url, data):
        orig_a(self, url, data)
        with lock:
            art[url] = data

    def set_status(self, url, status):
        orig_s(self, url, status)
        with lock:
            data = art.pop(url, None) if status in ok else None
            kw = kw_of.get(url, "")
        if not data or not kw:
            return
        d = str(data.get("date") or "")[:10]
        if start and end and not (start <= d <= end):
            return
        if looks_like_index_page(data.get("title") or "", url):
            return
        h = {"url": url, "title": data.get("title") or "", "published_date": d,
             "published_time": data.get("time") or "", "author": data.get("author") or "",
             "content": data.get("content") or "", "summary": data.get("summary") or "", "image": data.get("image") or "",
             "influencer": data.get("influencer") or "", "persons": data.get("persons") or "",
             "organizations": data.get("organizations") or "", "media": data.get("media") or "", "sentiment": "",
             "source": "", "status": status, "keyword": kw}
        try:
            on_hit(h)
        except Exception:  # noqa: BLE001  the live copy never stops the engine
            pass
    Database.pending_links, Database.save_article, Database.set_status = pending, save_article, set_status

    def undo():
        Database.pending_links, Database.save_article, Database.set_status = orig_p, orig_a, orig_s
    return undo


def _fast_first(pipeline, cfg, log):
    """Quick sources first for EVERY keyword (Google News, the known RSS feeds, Bing, GDELT — seconds to a few minutes),
    then the slow sweep (media sitemaps, looking for new RSS feeds) keyword by keyword. Articles start arriving within
    minutes instead of after the first keyword's 10-minute sitemap sweep; new feeds are looked for once per run, not
    once per keyword. Wraps the engine's collect_links for this run; returns undo."""
    from dataclasses import replace
    if not (cfg.use_sitemap or cfg.feed_discovery_per_run) or not (
            cfg.use_google_news or cfg.use_rss or cfg.use_bing or cfg.use_gdelt):
        return lambda: None
    orig = pipeline.collect_links
    fast = replace(cfg, use_sitemap=False, feed_discovery_per_run=0)
    slow = replace(cfg, use_google_news=False, per_media_harvest=False, use_bing=False, use_gdelt=False,
                   use_duckduckgo=False)
    slow_later = replace(slow, feed_discovery_per_run=0)
    st = {"first": True, "detected": {}}

    def collect(c, keyword, log=log, learned=None, db=None, batch=""):
        if st["first"]:
            st["first"] = False
            log(f"⚡ Quick sources first for all {len(c.keywords)} keyword(s) (Google News, RSS, Bing, GDELT) — "
                "articles are read and saved while the slower media-sitemap sweep follows.")
            for kw in c.keywords:
                if pipeline.STOP.is_set():
                    break
                log(f"\n⚡ Quick sources: {kw}")
                found, det = orig(fast, kw, log, learned, db=db, batch=batch)
                st["detected"][kw] = det
                if db is not None and found:
                    new = pipeline.save_findings(db, kw, found, log, batch=batch, retag=not c.report_new_only)
                    log(f"   💾 {new} NEW links queued for reading")
            log("\n🗺️ Slow sweep now: media sitemaps" + (" + looking for new RSS feeds (once this run)"
                                                          if cfg.feed_discovery_per_run else ""))
            use = slow
        else:
            use = slow_later
        if pipeline.STOP.is_set():
            return [], set()
        det = st["detected"].get(keyword, set())
        found, d2 = orig(use, keyword, log, set(learned or ()) | det, db=db, batch=batch)
        return found, set(d2) | det
    pipeline.collect_links = collect

    def undo():
        pipeline.collect_links = orig
    return undo


def run(keywords: list[str], start: str, end: str, opt, tz, log, stop_event: threading.Event,
        data_dir: str, out_dir: str, on_row=None) -> tuple[list[dict], list[str], dict]:
    """Crawl; returns (news rows, extra files written to out_dir, statistics). on_row(row) gets each accepted
    article the moment it has been read (live), before the run ends."""
    from .medmas import pipeline
    from .medmas.config import Config
    from .medmas.db import Database
    work = os.path.join(data_dir, "news_multi")
    os.makedirs(work, exist_ok=True)
    srcs = [s for s in (opt.multi_sources or DEFAULT_SOURCES) if s in SOURCES]
    to_dash = {engine_keyword(k, opt): k for k in keywords}
    cfg = Config(keywords=list(to_dash), start_date=start, end_date=end,
                 **{f: (k in srcs) for k, f in SOURCES.items()},
                 extra_media=list(dict.fromkeys(list(opt.priority_media) + list(opt.extra_media))),
                 registered_media_only=bool(opt.registered_only),
                 report_new_only=bool(opt.new_only), retry_failed=bool(opt.retry_failed),
                 media_dictionary_file=_dictionary(data_dir), max_media_harvest=int(opt.max_harvest),
                 threads=8, output_folder=os.path.join(work, "export"), db_path=os.path.join(work, "medmascrawler.db"))
    if "permedia" in srcs and "gnews" not in srcs:
        cfg.use_google_news = True                    # per-media harvest is part of the Google News step
    cov = COVERAGE.get(getattr(opt, "coverage", "normal"), COVERAGE["normal"])
    cfg.sitemap_domains_per_run, cfg.feed_discovery_per_run = cov["sitemaps"], cov["feeds"]
    if cov["harvest"] and cfg.max_media_harvest:
        cfg.max_media_harvest = max(cfg.max_media_harvest, cov["harvest"])
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("Another multi-source news run is still going.")
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            if stop_event.is_set():
                pipeline.request_stop()
                return
    try:
        from . import media_db
        media_db.install()                            # paid media: articles read with their logins
    except Exception:  # noqa: BLE001
        pass
    undo_p = _priority_first(pipeline, Database, list(opt.priority_media))
    undo_r = _shared_reads()
    undo_f = _fast_first(pipeline, cfg, log) if len(keywords) and getattr(opt, "quick_first", True) else (lambda: None)

    def hit(h):
        r = _row(h, tz)
        r["keyword"] = to_dash.get(r["keyword"], keywords[0] if len(keywords) == 1 else r["keyword"])
        on_row(r)
    undo_l = _live(Database, hit if on_row else None, start, end, bool(opt.include_review))

    def undo():
        undo_l()
        undo_f()
        undo_r()
        undo_p()
    if opt.priority_media:
        log(f"⭐ Priority media checked first in every run: {', '.join(opt.priority_media[:10])}"
            + (f" (+{len(opt.priority_media) - 10} more)" if len(opt.priority_media) > 10 else ""))
    try:
        pipeline.clear_stop()
        threading.Thread(target=watch, daemon=True).start()
        if os.path.isdir(cfg.output_folder):
            shutil.rmtree(cfg.output_folder, ignore_errors=True)
        stat = pipeline.run(cfg, log=log)
    finally:
        done.set()
        undo()
        pipeline.clear_stop()
        _RUN_LOCK.release()
    db = Database(cfg.db_path)
    try:
        hits = results(db, stat["batch"], start, end, bool(opt.include_review))
    finally:
        db.close()
    from .medmas.filters import keyword_match, url_slug
    rows = []
    for h in hits:
        r = _row(h, tz)
        kw = to_dash.get(r["keyword"])
        if kw is None:          # found before under another keyword: credit it to this run's keyword that matches
            kw = next((k for q, k in to_dash.items()
                       if keyword_match(q, h.get("title") or "", h.get("content") or "", url_slug(h["url"]))),
                      keywords[0] if len(keywords) == 1 else r["keyword"])
        r["keyword"] = kw
        rows.append(r)
    extra = []
    for name in sorted(os.listdir(cfg.output_folder)) if os.path.isdir(cfg.output_folder) else []:
        if name.startswith("crawl_results_") and name.endswith(".xlsx"):   # Data + Needs Review + Recap: for checking
            dest = f"news_links_review_{stat['batch']}.xlsx"
            shutil.copyfile(os.path.join(cfg.output_folder, name), os.path.join(out_dir, dest))
            extra.append(dest)
    stat["needs_review_left"] = sum(1 for h in hits if h["status"] == "needs_review")
    return rows, extra, stat


def check() -> str | None:
    """None when the engine's libraries are installed, else what is missing."""
    missing = []
    for mod in ("feedparser", "trafilatura", "googlenewsdecoder", "bs4", "lxml", "pandas"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return ("Multi-source engine needs: " + ", ".join(missing) + " (update the app)") if missing else None
