"""Main pipeline: discovery -> decode -> dedup (DB) -> extract -> export.

Built for AUTOMATIC crawling: every run only processes links never seen
before (SQLite dedup), so it is safe to run repeatedly via watch mode,
cron, or any scheduler.
"""

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Cooperative stop: the dashboard's Stop button sets this. Every long phase
# checks it, so a run can be interrupted and STILL export what it collected.
STOP = threading.Event()


def request_stop():
    STOP.set()


def clear_stop():
    STOP.clear()

from .config import Config
from .db import Database
from .decoder import decode_all, resolve_by_search
from .discovery import (bing_news, duckduckgo, feed_finder, gdelt,
                        google_news, rss_feeds, sitemaps)
from .extract import extract_article
from .filters import (describe_keyword, is_article_link, keyword_excluded,
                      keyword_match, keyword_queries, keyword_query, url_slug,
                      validate_keyword)
from .media import (CORE_MEDIA, MEDIA_DICT, in_dictionary,
                    load_external_dictionary, media_name, set_extra_media,
                    url_domain)
from .sentiment import run_sentiment


def _harvest_domains(cfg: Config, detected, log=print, db=None):
    """Order domains for active harvesting (site: queries) by priority:
    1) media already seen reporting the keyword (most productive),
    2) curated core dictionary, 3) external dictionary (can be thousands) —
    ROTATED across runs (least recently queried first), so with a cap the
    whole dictionary still gets covered over successive runs."""
    external = sorted(MEDIA_DICT)
    if db is not None:
        dead = db.dead_domains()
        if dead:
            external = [d for d in external if d not in dead]
        last = db.harvest_last_used()
        external.sort(key=lambda d: last.get(d, ""))   # never-used first
    ordered = list(dict.fromkeys(
        sorted(d for d in detected if d)
        + sorted(CORE_MEDIA)
        + external))
    if cfg.max_media_harvest and len(ordered) > cfg.max_media_harvest:
        log(f"   ⚖️ Active harvest capped at {cfg.max_media_harvest} of "
            f"{len(ordered)} domains (priority: detected > core > external, "
            f"external rotated across runs so everyone gets a turn; "
            f"0 = all).")
        ordered = ordered[:cfg.max_media_harvest]
    if db is not None:
        db.record_harvest_used(ordered)
    return ordered


def collect_links(cfg: Config, keyword: str, log=print, learned=None, db=None,
                  batch=""):
    """Run every discovery source for one keyword.
    Return (findings, detected_domains)."""
    start, end = cfg.date_range()
    findings = []
    detected = set(cfg.extra_media) | set(learned or ())
    # search engines get the query form: NOT terms as minus operators
    query = keyword_query(keyword)

    def absorb(items):
        """Take a source's findings and persist them IMMEDIATELY
        (crash-safe): direct article links go into the links table, Google
        links are parked in the decode queue with attempts=0 — closing or
        killing the app mid-run never loses a discovery again."""
        items = list(items)
        findings.extend(items)
        if db is None or not items:
            return
        direct = [t for t in items if "news.google.com" not in t["url"]
                  and t["url"].startswith("http") and is_article_link(t["url"])]
        google = [t for t in items if "news.google.com" in t["url"]]
        if direct:
            save_findings(db, keyword, direct, log, batch=batch,
                          retag=not cfg.report_new_only)
        if google:
            db.queue_undecoded(google, keyword, batch=batch, attempts=0)

    # A top-level OR is fanned out into one query per alternative: engines
    # cap results PER QUERY (~100 at Google News), so N variants can yield
    # up to N times the links. Dedupe happens downstream as usual.
    queries = keyword_queries(keyword)

    if cfg.use_google_news and not STOP.is_set():
        if len(queries) > 1:
            log(f"   🔍 Google News: {len(queries)} OR-variant queries "
                f"(beats the ~100-per-query cap), each with recursive "
                f"date split...")
        else:
            log("   🔍 Google News (recursive date split)...")
        results, doms = [], set()
        for q in queries:
            if STOP.is_set():
                break
            r, d = google_news.search_full(q, start, end, log)
            results.extend(r)
            doms |= d
        log(f"      Google News general: {len(results)} results, "
            f"{len(doms)} outlets detected")
        if not results:
            log("      ⚠️ 0 results from Google News — the network may be "
                "blocking/rate-limiting the feed. Check connectivity, or rely "
                "on sitemap/RSS/Bing sources this run.")
        absorb(results)
        detected |= doms
        if cfg.per_media_harvest:
            absorb(google_news.per_media_harvest(
                query, start, end, _harvest_domains(cfg, detected, log, db),
                log, should_stop=STOP.is_set))

    if cfg.use_sitemap and not STOP.is_set():
        # Sitemaps come from the USER'S media dictionary (+ core + extras),
        # rotated so thousands of outlets are covered across runs.
        pool = set(MEDIA_DICT) | set(CORE_MEDIA) | set(cfg.extra_media)
        if db is not None:
            dead = db.dead_domains()
            skipped_dead = len(pool & dead)
            if skipped_dead:
                pool -= dead
                log(f"   🩺 {skipped_dead} outlets known unreachable are "
                    f"skipped (auto re-checked after 14 days).")
            sm_domains = db.pick_sitemap_domains(
                pool, cfg.sitemap_domains_per_run, first=sorted(detected))
        else:
            sm_domains = sorted(pool)[:cfg.sitemap_domains_per_run or None]
        log(f"   🗺️ Sitemaps from your media dictionary: reading "
            f"{len(sm_domains)} of {len(pool)} outlets this run "
            f"(rotating; productive ones first).")
        sm_health = {}
        sm_results = sitemaps.harvest(keyword, start, end, sm_domains, log,
                                      threads=min(32, max(16, cfg.threads * 2)),
                                      should_stop=STOP.is_set,
                                      health=sm_health)
        absorb(sm_results)
        if db is not None:
            counts = {d: 0 for d in sm_domains}
            for r in sm_results:
                d = r.get("domain")
                if d in counts:
                    counts[d] += 1
            db.record_sitemap_results(counts)
            if sm_health:
                db.record_domain_health(sm_health)

    if cfg.use_rss and not STOP.is_set():
        # Curated feeds + every working feed auto-discovered from the
        # user's dictionary in earlier runs + a fresh discovery slice.
        feeds = {d: list(u) for d, u in rss_feeds.RSS_FEEDS.items()}
        if db is not None:
            known = db.known_feeds()
            for d, u in known.items():
                if u not in feeds.get(d, ()):
                    feeds.setdefault(d, []).append(u)
            pool = (set(MEDIA_DICT) | set(CORE_MEDIA) | set(cfg.extra_media))
            pool -= db.dead_domains()   # skip known-unreachable outlets
            todo = (db.pick_feed_discovery(pool - set(known),
                                           cfg.feed_discovery_per_run)
                    if cfg.feed_discovery_per_run else [])
            if todo:
                log(f"   📡 Probing {len(todo)} dictionary outlets for RSS "
                    f"feeds ({len(known)} working feeds known so far; "
                    f"rotating over the whole dictionary)...")
                fd_health = {}
                found = feed_finder.discover_many(todo, log,
                                                  should_stop=STOP.is_set,
                                                  health=fd_health)
                if fd_health:
                    db.record_domain_health(fd_health)
                fresh = 0
                for d, u in found.items():
                    db.record_feed_probe(d, u)
                    if u and u not in feeds.get(d, ()):
                        feeds.setdefault(d, []).append(u)
                        fresh += 1
                if fresh:
                    log(f"      ➕ {fresh} new working feeds saved — they are "
                        f"read on every future run.")
        rss_findings = rss_feeds.harvest(
            keyword, log, feeds=feeds,
            threads=min(32, max(16, cfg.threads * 2)))
        absorb(rss_findings)
        if db is not None and rss_findings:
            counts = {}
            for r in rss_findings:
                d = r.get("domain")
                if d:
                    counts[d] = counts.get(d, 0) + 1
            db.record_feed_hits(counts)

    if cfg.use_bing and not STOP.is_set():
        for q in queries:                    # same per-query cap logic
            if STOP.is_set():
                break
            absorb(bing_news.harvest(q, log))

    if cfg.use_gdelt and not STOP.is_set():
        absorb(gdelt.harvest(query, start, end, log))

    if cfg.use_duckduckgo and not STOP.is_set():
        absorb(duckduckgo.harvest(query, start, log=log))

    if db is not None and findings:
        n_direct = sum(1 for t in findings
                       if "news.google.com" not in t["url"])
        log(f"   💾 Every finding was saved the moment its source finished "
            f"({n_direct} direct links in the database, Google links parked "
            f"in the decode queue) — stopping or closing loses nothing.")

    # Decode Google News links -> real URLs. Links that failed to decode in
    # EARLIER runs are retried first; whatever still fails is queued again —
    # Google's throttling never permanently costs a clip.
    retries = []
    if db is not None:
        gave_up = db.purge_decode_queue(max_attempts=3)
        if gave_up:
            log(f"   🗑️ {gave_up} Google links gave up after 3 decode attempts "
                f"across runs (those articles usually also arrive via "
                f"sitemap/RSS/Bing).")
        retries = db.decode_retries(exclude_batch=batch)
        if retries:
            log(f"   ♻️ {len(retries)} Google links from earlier runs are "
                f"still undecoded — retrying them this run.")
            findings.extend(retries)
    findings, undecoded = decode_all(findings, log,
                                     budget_minutes=cfg.decode_budget_minutes,
                                     should_stop=STOP.is_set)
    # Last line of defense against Google's decode throttling: search Bing
    # for each stuck link's TITLE and take the matching outlet's URL.
    if undecoded and not STOP.is_set():
        rescued, undecoded = resolve_by_search(undecoded, log,
                                               should_stop=STOP.is_set)
        findings.extend(rescued)
    if db is not None:
        # every decoded link leaves the queue (parked-at-discovery + retries)
        decoded_orig = [t["decoded_from"] for t in findings
                        if t.get("decoded_from")]
        if decoded_orig:
            db.remove_decoded(decoded_orig)
        if retries:
            undecoded_urls = {t["url"] for t in undecoded}
            solved = sum(1 for r in retries if r["url"] not in undecoded_urls)
            if solved:
                log(f"   🎉 {solved} previously stuck links decoded this "
                    f"time — recovered clips.")
        if undecoded:
            db.queue_undecoded(undecoded, keyword, batch=batch)
            log(f"   📌 {len(undecoded)} undecoded Google links queued for "
                f"automatic retry on the next run — nothing is dropped.")

    # Drop non-article links + in-run dedup
    unique, seen = [], set()
    skipped = {}
    for t in findings:
        url = t["url"].strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        if not is_article_link(url):
            continue
        if cfg.registered_media_only and not in_dictionary(url):
            host = url_domain(url)
            skipped[host] = skipped.get(host, 0) + 1
            continue
        t["url"] = url
        t["domain"] = t.get("domain") or url_domain(url)
        detected.add(t["domain"])
        unique.append(t)
    if skipped:
        total = sum(skipped.values())
        top = sorted(skipped.items(), key=lambda x: -x[1])[:5]
        log(f"   🚫 Registered-media-only: {total} links from "
            f"{len(skipped)} unregistered outlets skipped "
            f"({', '.join(f'{d} ×{n}' for d, n in top)}"
            f"{', …' if len(skipped) > 5 else ''}).")
        log("      → Review them under 'Unregistered outlets' and add the "
            "legitimate ones with one click.")
        if db is not None:
            for host, n in skipped.items():
                db.add_candidate(host, n)
    log(f"   ➜ {len(unique)} unique, valid links (from {len(findings)} raw findings)")
    return unique, detected


def save_findings(db: Database, keyword: str, findings, log=print, batch="",
                  retag=True):
    """Insert findings into the DB. Return the number of NEW links."""
    new = 0
    for t in findings:
        # links recovered from the decode-retry queue carry their own keyword
        if db.add_link(t["url"], t.get("keyword") or keyword, t["source"],
                       media_name(t["url"]),
                       t.get("title", ""), t.get("date", ""), t.get("time", ""),
                       batch=batch, retag=retag):
            new += 1
    return new


import requests as _requests


def raise_fd_limit(target=4096):
    """macOS caps a process at 256 open files by default — far too low for
    a parallel crawler (downloads + sitemaps + feeds + the dashboard all
    hold sockets). Raise the soft limit as high as the OS allows."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        wanted = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < wanted:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    except Exception:
        pass          # Windows has no such limit; never fail a run over this

# Offline detection: when the WiFi drops mid-run, downloads must WAIT for
# the connection instead of burning the whole queue as failures.
_ONLINE = {"t": 0.0, "ok": True, "was_offline": False}


def _is_online(timeout=4):
    now = time.time()
    if now - _ONLINE["t"] < 10:          # probe at most every 10s
        return _ONLINE["ok"]
    ok = False
    for probe in ("https://www.gstatic.com/generate_204",
                  "https://one.one.one.one"):
        try:
            _requests.head(probe, timeout=timeout)
            ok = True
            break
        except Exception:
            continue
    _ONLINE.update(t=now, ok=ok)
    return ok


def _wait_for_connection(log):
    """Block (all download threads) until the internet returns or Stop."""
    while not STOP.is_set() and not _is_online():
        if not _ONLINE["was_offline"]:
            _ONLINE["was_offline"] = True
            log("📴 No internet connection — downloads PAUSED. They resume "
                "automatically the moment the connection returns. "
                "(Nothing is lost; feel free to move locations.)")
        time.sleep(10)
    if _ONLINE["was_offline"] and not STOP.is_set():
        _ONLINE["was_offline"] = False
        log("📶 Connection restored — continuing right where we paused.")


def extract_pending(cfg: Config, db: Database, log=print, batch="",
                    quiet=False):
    """Extract every link with status 'new' in parallel (resumable).
    quiet=True: say nothing when the queue is empty (used by the pipelined
    background extractor that polls during discovery)."""
    pending = db.pending_links()
    if not pending:
        if not quiet:
            log("   No new links to extract.")
        return 0, 0
    start, end = cfg.date_range()
    log(f"\n📥 Extracting {len(pending)} articles ({cfg.threads} threads)...")
    t0, done, ok = time.time(), 0, 0

    def process(url, keyword, feed_date, feed_time):
        _wait_for_connection(log)     # offline -> pause, never burn the queue
        result = extract_article(url, cfg.timeout)
        # The article page often hides its date/time from parsers — the feed
        # that discovered the link carried the publish stamp; use it as
        # fallback so DATE/TIME rarely stay empty.
        if not result["date"] and feed_date:
            result["date"] = feed_date
        if not result["time"] and feed_time:
            result["time"] = feed_time
        status = "success" if result["content"] else "failed"
        if status == "success" and not result.get("is_article", True):
            # home / tag / index / category listing — text but no news story
            status = "filtered"
        # Imported links (empty keyword) skip date/keyword filters —
        # the user hand-picked those links.
        if status == "success" and keyword:
            d = str(result["date"] or "")[:10]
            if cfg.strict_date_filter and not re.match(r"\d{4}-\d{2}-\d{2}", d):
                # Undated article: cannot prove it is inside the timeframe.
                # Never dropped (anti missed clip) — flagged for review.
                status = "needs_review"
            elif cfg.strict_date_filter and not (start <= d <= end):
                status = "filtered"  # outside the date range
            elif cfg.verify_keyword_in_content and keyword_excluded(
                    keyword, result["title"], result["content"], url_slug(url)):
                # a NOT term appears — the user explicitly excluded this
                status = "filtered"
            elif cfg.verify_keyword_in_content and not keyword_match(
                    keyword, result["title"], result["content"], url_slug(url)):
                # Anti missed clip: NEVER drop — flag for manual review.
                status = "needs_review"
        db.save_article(url, result)
        db.set_status(url, status)
        # Links queued by an earlier run belong to this run's output too —
        # otherwise resuming a queue would produce an empty file.
        db.tag_batch(url, batch)
        return status, result.get("error", "")

    stopped = False
    net_errors, net_warned = 0, False
    with ThreadPoolExecutor(max_workers=cfg.threads) as ex:
        futures = [ex.submit(process, url, kw, fd, ft)
                   for url, kw, fd, ft in pending]
        for f in as_completed(futures):
            if STOP.is_set() and not stopped:
                stopped = True
                cancelled = sum(1 for x in futures if x.cancel())
                log(f"   ⛔ Stop requested — {cancelled} queued articles left "
                    f"for the next run; everything extracted is already saved.")
            try:
                status, err = f.result()
            except Exception:
                status, err = "failed", ""
            done += 1
            if status == "success":
                ok += 1
            if err in ("Timeout", "ConnectionError"):
                net_errors += 1
            # Early alarm: when downloads fail at the NETWORK level en
            # masse, the crawler cannot outrun it — tell the user exactly
            # what is wrong instead of grinding for hours.
            if not net_warned and done >= 40 and net_errors / done >= 0.7:
                net_warned = True
                log(f"   🚨 {net_errors} of the first {done} article "
                    f"downloads failed at the NETWORK level "
                    f"(timeout/connection refused). This machine, its "
                    f"antivirus or its firewall is very likely blocking "
                    f"Python's access to news sites.")
                log("      → Allow/whitelist 'python.exe' in the antivirus "
                    "& firewall, or try another network (e.g. phone "
                    "hotspot), then use 'Retry links failed in earlier "
                    "runs'. Continuing anyway...")
            if done % 25 == 0 or done == len(pending):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (len(pending) - done) / rate if rate > 0 else 0
                log(f"   ⏳ {done}/{len(pending)} "
                    f"({done / len(pending) * 100:.0f}%) | "
                    f"✅ {ok} | ⚡ {rate:.1f}/s | ~{remaining / 60:.1f} min left")
    if done and net_errors / done >= 0.5:
        log(f"   🚨 Summary: {net_errors}/{done} downloads failed at the "
            f"network level — fix the antivirus/firewall/network before "
            f"the next run, then tick 'Retry links failed in earlier runs'.")
    return done, ok


def run(cfg: Config, log=print):
    """One full crawl cycle for every keyword. Return DB statistics."""
    if not cfg.keywords:
        raise ValueError("The config contains no keywords.")
    raise_fd_limit()
    db = Database(cfg.db_path)
    start, end = cfg.date_range()
    batch = time.strftime("%Y%m%d_%H%M%S")   # this run's output scope
    db.start_run(batch, "crawl", " | ".join(cfg.keywords))
    log(f"🚀 Crawling {len(cfg.keywords)} keyword(s) | range {start} to {end}")
    for kw in cfg.keywords:
        problem = validate_keyword(kw)
        if problem:
            log(f"   ⚠️ Keyword '{kw}': {problem}")
        else:
            log(f"   🔤 '{kw}' → {describe_keyword(kw)}")

    # External media dictionary (thousands of user outlets) — loaded every
    # run so file changes apply immediately; idempotent.
    load_external_dictionary(cfg.media_dictionary_file, log)
    # Ad-hoc whitelist additions on top of the dictionaries
    if cfg.extra_media:
        n = set_extra_media(cfg.extra_media)
        log(f"➕ Extra media: {n} additional outlet(s) accepted alongside "
            f"the dictionary.")
    else:
        set_extra_media([])
    if cfg.registered_media_only:
        log(f"📋 Registered-media-only mode: only dictionary "
            f"({len(MEDIA_DICT)}) + extra ({len(cfg.extra_media)}) outlets "
            f"are accepted.")

    if cfg.retry_failed:
        n = db.reset_failed()
        if n:
            log(f"🔁 {n} failed links re-queued for another attempt.")

    # Keyword rules improve over time (e.g. the curly-quote fix): articles
    # stuck in needs_review whose keyword NOW verifies are promoted to
    # success PERMANENTLY, so every view and export agrees.
    promoted = 0
    for url, kw, title, content in db.needs_review_rows():
        if not kw:
            continue
        slug = url_slug(url)
        if (not keyword_excluded(kw, title, content, slug)
                and keyword_match(kw, title, content, slug)):
            db.set_status(url, "success")
            promoted += 1
    if promoted:
        log(f"🔁 {promoted} needs-review articles re-verified under current "
            f"rules and promoted to success.")

    # Auto-learn: media seen in previous runs join the harvest
    learned = db.all_domains() if cfg.auto_learn_media else set()
    if learned:
        log(f"🧠 Auto-learn: {len(learned)} media domains from previous runs "
            f"join the harvest.")

    # PIPELINED extraction: articles download IN PARALLEL with discovery.
    # Every source persists its links the moment it finishes (crash-safe
    # absorb), so the extractor can start on the first batch while the
    # sitemap/feed sweeps are still running — often halving the wall clock.
    discovery_done = threading.Event()

    def _extract_worker():
        while not STOP.is_set():
            if db.pending_links():
                extract_pending(cfg, db, log, batch=batch, quiet=True)
            elif discovery_done.is_set():
                break
            else:
                time.sleep(4)

    extractor = None
    if cfg.extract_content:
        extractor = threading.Thread(target=_extract_worker, daemon=True)
        extractor.start()

    total_new = 0
    try:
        for kw in cfg.keywords:
            if STOP.is_set():
                log("⛔ Stop requested — skipping the remaining keywords.")
                break
            log(f"\n🔎 Keyword: {kw}")
            findings, detected = collect_links(cfg, kw, log, learned, db=db,
                                               batch=batch)
            new = save_findings(db, kw, findings, log, batch=batch,
                                retag=not cfg.report_new_only)
            total_new += new
            if cfg.auto_learn_media:
                for d in detected:
                    if d:
                        db.add_domain(d)
            log(f"   💾 {new} NEW links stored "
                f"({len(findings) - new} were already known)")
    finally:
        discovery_done.set()
        if extractor is not None:
            extractor.join()   # wait for the tail of the download queue

    if not STOP.is_set():
        try:
            run_sentiment(db, cfg.sentiment_engine, log)
        except Exception as e:
            # never let an analysis step cost the deliverable
            log(f"⚠️ Sentiment step failed ({type(e).__name__}: {e}) — "
                f"continuing to export.")

    stat = db.stats()
    log(f"\n📊 Database: total {stat.get('total', 0)} links | "
        f"success {stat.get('success', 0)} | "
        f"needs_review {stat.get('needs_review', 0)} | "
        f"failed {stat.get('failed', 0)} | filtered {stat.get('filtered', 0)} | "
        f"new(queued) {stat.get('new', 0)}")

    # Exports cover THIS RUN only — the database keeps the full archive,
    # but the deliverable must not repeat previous runs' results.
    # They run even after a Stop, so an interrupted job still hands over a file.
    if STOP.is_set():
        log("⛔ Stopped early — exporting what this run already collected.")
    from .export import export, export_upload
    export(db, cfg.output_folder, log=log, batch=batch)
    # Upload Online must strictly match the requested timeframe
    export_upload(db, cfg.output_folder, log=log, batch=batch,
                  start=start if cfg.strict_date_filter else None,
                  end=end if cfg.strict_date_filter else None)
    db.finish_run(batch)
    db.close()
    return {"new_links": total_new, "batch": batch, **stat}


def import_links(cfg: Config, link_list, log=print, label="", force=False):
    """Process the user's own BULK LINKS: store them (source 'import'),
    extract, then export. No date/keyword filters — the user hand-picked
    the links. Return statistics."""
    cleaned = []
    for line in link_list:
        for u in str(line).replace(",", " ").replace(";", " ").split():
            u = u.strip()
            if u.startswith("http") and u not in cleaned:
                cleaned.append(u)
    if not cleaned:
        raise ValueError("No valid links (must start with http/https).")

    db = Database(cfg.db_path)
    load_external_dictionary(cfg.media_dictionary_file, log)
    set_extra_media(cfg.extra_media)
    batch = time.strftime("%Y%m%d_%H%M%S")   # this import's output scope
    db.start_run(batch, "import", f"{len(cleaned)} pasted links")
    new, known = 0, []
    for u in cleaned:
        if db.add_link(u, label, "import", media_name(u), batch=batch):
            new += 1
        else:
            known.append(u)
    # The user pasted these links on purpose: previously seen ones that are
    # not already extracted successfully go back into the queue, even if an
    # earlier run filed them as failed/filtered/needs_review.
    # force=True also re-extracts successful ones (fresh content, e.g. after
    # an engine upgrade that cleans articles better).
    requeued = db.requeue(known, force=force)
    log(f"📥 Bulk import: {len(cleaned)} valid links — {new} new, "
        f"{requeued} known links re-queued"
        f"{' (forced)' if force else ''}, "
        f"{len(known) - requeued} already extracted (reused).")

    if cfg.extract_content:
        extract_pending(cfg, db, log, batch=batch)
    run_sentiment(db, cfg.sentiment_engine, log)

    stat = db.stats()
    # Export only this import batch — not the whole archive.
    from .export import export, export_upload
    export(db, cfg.output_folder, log=log, batch=batch)
    export_upload(db, cfg.output_folder, log=log, batch=batch)
    db.finish_run(batch)
    db.close()
    return {"new_links": new, "batch": batch, **stat}


def run_watch(cfg: Config, log=print):
    """Automation mode: repeat the crawl every N minutes until stopped."""
    interval = max(1, cfg.watch_interval_minutes)
    log(f"👁️ WATCH mode on: crawling every {interval} minutes. Ctrl+C to stop.\n")
    cycle = 0
    while True:
        cycle += 1
        log(f"{'=' * 55}\n⏰ Cycle #{cycle} — {time.strftime('%Y-%m-%d %H:%M:%S')}\n{'=' * 55}")
        try:
            stat = run(cfg, log)
            log(f"✅ Cycle #{cycle} done: {stat['new_links']} new links.")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log(f"❌ Cycle #{cycle} error: {type(e).__name__}: {e} — continuing.")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            log("\n⛔ Watch stopped.")
            break
