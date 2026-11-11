"""Crawler configuration: loaded from a YAML file, with sensible defaults.

Old Indonesian config keys (tgl_mulai, pakai_sitemap, ...) are still
accepted and mapped automatically — existing config.yaml files keep working.
"""

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

try:
    import yaml
except ImportError:  # pyyaml is optional — Config can be built in code
    yaml = None


@dataclass
class Config:
    # --- search ---
    keywords: list = field(default_factory=list)
    start_date: str = ""           # YYYY-MM-DD; empty = last 30 days
    end_date: str = ""             # YYYY-MM-DD; empty = today
    # --- discovery sources ---
    use_google_news: bool = True
    per_media_harvest: bool = True  # site:domain queries (breaks the ~100 cap)
    use_sitemap: bool = True        # news sitemaps straight from media sites
    use_rss: bool = True            # official media RSS feeds (freshest news)
    use_bing: bool = True           # Bing News RSS (anti missed clip)
    use_gdelt: bool = True          # GDELT global index — free HISTORICAL
                                    # reach (backfill windows), no API key
    use_duckduckgo: bool = False    # optional, often rate-limited
    extra_media: list = field(default_factory=list)  # extra domains to harvest
    auto_learn_media: bool = True   # newly seen domains join future harvests
    # --- external media dictionary (thousands of outlets) ---
    media_dictionary_file: str = ""  # empty = auto-detect kamus_media/media_dictionary.*
    max_media_harvest: int = 200     # site: query domains per keyword (0 = all)
    sitemap_domains_per_run: int = 400  # sitemaps read per run, rotated over
                                        # the whole dictionary (0 = all)
    feed_discovery_per_run: int = 300   # outlets probed for an RSS feed per
                                        # run (rotating); working feeds are
                                        # remembered and read every run
                                        # (0 = disable discovery)
    registered_media_only: bool = False  # only accept articles from dictionary outlets
    # --- filters ---
    strict_date_filter: bool = True       # drop articles outside the date range
    verify_keyword_in_content: bool = True  # unverified keyword -> needs_review
    report_new_only: bool = False   # true = this run's report contains ONLY
                                    # articles never delivered before —
                                    # ideal for a twice-daily schedule where
                                    # the evening file must skip the noon one
    # --- extraction & output ---
    extract_content: bool = True
    sentiment_engine: str = "lexicon"  # lexicon | transformer | off
    threads: int = 10
    timeout: int = 20
    decode_budget_minutes: int = 25  # cap on Google-link decoding per keyword
                                     # (0 = no cap; heavy throttling can make
                                     # decoding take hours)
    output_folder: str = "output"
    db_path: str = "output/medmascrawler.db"
    # --- automation ---
    watch_interval_minutes: int = 0  # >0 = re-crawl every N minutes
    retry_failed: bool = False       # re-queue previously failed links
    schedule_times: list = field(default_factory=list)
    # daily crawl times, e.g. ["12:00", "18:00", "21:00"] — fires whenever
    # the dashboard/server is running (the 'serve' mode's autopilot)
    # --- server ---
    dashboard_password: str = ""     # non-empty = the dashboard requires
                                     # login (MANDATORY on a public server)

    def date_range(self):
        """Return (start, end) as ISO strings, defaults filled in."""
        end = self.end_date or date.today().isoformat()
        start = self.start_date or (date.today() - timedelta(days=30)).isoformat()
        return start, end


# Old Indonesian keys -> new English field names (backward compatibility)
_LEGACY_KEYS = {
    "tgl_mulai": "start_date", "tgl_akhir": "end_date",
    "pakai_google_news": "use_google_news", "panen_per_media": "per_media_harvest",
    "pakai_sitemap": "use_sitemap", "pakai_rss": "use_rss",
    "pakai_bing": "use_bing", "pakai_duckduckgo": "use_duckduckgo",
    "media_tambahan": "extra_media",
    "file_kamus_media": "media_dictionary_file",
    "maks_media_panen": "max_media_harvest",
    "filter_ketat_tanggal": "strict_date_filter",
    "keyword_di_konten": "verify_keyword_in_content",
    "ekstrak_konten": "extract_content",
    "thread": "threads",
    "folder_output": "output_folder",
    "watch_interval_menit": "watch_interval_minutes",
    "ulang_gagal": "retry_failed",
}


def load_config(path: str) -> Config:
    """Read config from YAML. Unknown fields are ignored; legacy keys mapped."""
    if yaml is None:
        raise RuntimeError("pyyaml is not installed: pip install pyyaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    mapped = {}
    for k, v in data.items():
        mapped[_LEGACY_KEYS.get(k, k)] = v
    # legacy sentiment flag: analisis_sentimen true -> transformer engine
    if "analisis_sentimen" in mapped:
        if "sentiment_engine" not in data:
            mapped["sentiment_engine"] = ("transformer" if mapped["analisis_sentimen"]
                                          else "lexicon")
        mapped.pop("analisis_sentimen", None)
    valid = {f.name for f in Config.__dataclass_fields__.values()}
    cfg = Config(**{k: v for k, v in mapped.items() if k in valid})
    if isinstance(cfg.keywords, str):
        cfg.keywords = [cfg.keywords]
    for attr in ("start_date", "end_date"):
        v = getattr(cfg, attr)
        if isinstance(v, date):
            setattr(cfg, attr, v.isoformat())
    return cfg


SAMPLE_CONFIG = """\
# medmascrawler configuration — keyword-based news crawler engine
# Run: python -m medmascrawler crawl -c config.yaml

keywords:
  - "Pemkot Magelang"              # quotes = exact phrase
  - banjir magelang                # no quotes = all words must appear (AND)
  # Boolean: AND / OR / NOT (UPPERCASE) and ( ) groups; -word also excludes
  - '"PLN" AND (padam OR byarpet) NOT promo'
  - '"Pemkot Magelang" OR "Pemerintah Kota Magelang"'

start_date: 2026-07-01       # leave empty for "last 30 days"
end_date: 2026-08-01

# Link discovery sources
use_google_news: true
per_media_harvest: true      # per-media queries -> breaks Google News ~100 cap
use_sitemap: true            # news sitemaps straight from media sites
use_rss: true                # official media RSS feeds (freshest news)
use_bing: true               # Bing News (complements Google, anti missed clip)
use_gdelt: true              # GDELT global news index — free, historical
                             # date-range queries: the backfill workhorse
use_duckduckgo: false
extra_media: []              # outlets accepted ON TOP of the dictionary and
                             # harvested too, e.g. [magelangekspres.disway.id]
                             # (the dashboard can add these with one click)
auto_learn_media: true       # newly detected media join future harvests

# Your own media dictionary (optional, supports thousands of outlets):
# put media_dictionary.xlsx / .csv / .txt (or kamus_media.*) in the app folder.
media_dictionary_file: ""    # or an explicit path, e.g. data/media_13000.xlsx
max_media_harvest: 200       # site: query domains per keyword (0 = all;
                             # priority: detected > core > external dictionary)
sitemap_domains_per_run: 400 # sitemaps read per run, taken FROM YOUR MEDIA
                             # DICTIONARY and rotated: outlets that already
                             # carried the keyword first, then the most
                             # productive, then the least recently checked —
                             # so the whole list gets covered over time
                             # (0 = read every outlet every run: very slow)
feed_discovery_per_run: 300  # outlets probed for an RSS feed per run
                             # (rotating over the dictionary). Working feeds
                             # are remembered and read on EVERY later run —
                             # the fastest route to brand-new articles.
registered_media_only: false # true = ONLY accept articles from outlets in the
                             # dictionary PLUS extra_media (classic media-
                             # monitoring whitelist). Rejected outlets are
                             # listed in the dashboard so you can add them.

# Filters — articles with unverified keywords are NOT dropped,
# they get status 'needs_review' for manual checking (anti missed clip)
strict_date_filter: true
verify_keyword_in_content: true
report_new_only: false       # true = each report contains ONLY articles never
                             # delivered before. For twice-daily crawling the
                             # evening file then skips the noon file's clips.

# Extraction & output
extract_content: true
sentiment_engine: lexicon    # lexicon (instant, default) | transformer
                             # (more accurate, needs: pip install transformers torch)
                             # | off
threads: 10
decode_budget_minutes: 25    # max minutes spent turning Google News links into
                             # real URLs, per keyword. Google throttles bulk
                             # decoding hard; when the cap is hit the run moves
                             # on (those articles usually also arrive via
                             # sitemap/RSS/Bing). 0 = no cap.
output_folder: output
db_path: output/medmascrawler.db

# Automation: >0 repeats the crawl every N minutes (Ctrl+C to stop)
watch_interval_minutes: 0

# Autopilot: crawl daily at these times while the dashboard/server runs
# (python -m medmascrawler serve). Perfect for a 2-3x daily schedule.
schedule_times: []           # e.g. ["12:00", "18:00", "21:00"]

# Server mode: set a password before exposing the dashboard beyond your
# own computer — every page then requires login.
dashboard_password: ""
"""


def write_sample_config(path: str = "config.yaml"):
    if os.path.exists(path):
        raise FileExistsError(f"{path} already exists — not overwriting.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(SAMPLE_CONFIG)
    return path
