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


def run(keywords: list[str], start: str, end: str, opt, tz, log, stop_event: threading.Event,
        data_dir: str, out_dir: str) -> tuple[list[dict], list[str], dict]:
    """Crawl; returns (news rows, extra files written to out_dir, statistics)."""
    from .medmas import pipeline
    from .medmas.config import Config
    from .medmas.db import Database
    work = os.path.join(data_dir, "news_multi")
    os.makedirs(work, exist_ok=True)
    srcs = [s for s in (opt.multi_sources or DEFAULT_SOURCES) if s in SOURCES]
    to_dash = {engine_keyword(k, opt): k for k in keywords}
    cfg = Config(keywords=list(to_dash), start_date=start, end_date=end,
                 **{f: (k in srcs) for k, f in SOURCES.items()},
                 extra_media=list(opt.extra_media), registered_media_only=bool(opt.registered_only),
                 report_new_only=bool(opt.new_only), retry_failed=bool(opt.retry_failed),
                 media_dictionary_file=_dictionary(data_dir), max_media_harvest=int(opt.max_harvest),
                 threads=8, output_folder=os.path.join(work, "export"), db_path=os.path.join(work, "medmascrawler.db"))
    if "permedia" in srcs and "gnews" not in srcs:
        cfg.use_google_news = True                    # per-media harvest is part of the Google News step
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("Another multi-source news run is still going.")
    done = threading.Event()

    def watch():
        while not done.wait(0.5):
            if stop_event.is_set():
                pipeline.request_stop()
                return
    try:
        pipeline.clear_stop()
        threading.Thread(target=watch, daemon=True).start()
        if os.path.isdir(cfg.output_folder):
            shutil.rmtree(cfg.output_folder, ignore_errors=True)
        stat = pipeline.run(cfg, log=log)
    finally:
        done.set()
        pipeline.clear_stop()
        _RUN_LOCK.release()
    db = Database(cfg.db_path)
    try:
        hits = results(db, stat["batch"], start, end, bool(opt.include_review))
    finally:
        db.close()
    rows = [_row(h, tz) for h in hits]
    for r in rows:
        r["keyword"] = to_dash.get(r["keyword"], r["keyword"])
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
