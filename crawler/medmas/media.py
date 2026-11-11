"""Indonesian media dictionary: domain -> outlet name, plus RSS feeds.

Supports an EXTERNAL dictionary (thousands of outlets): put a file named
media_dictionary.csv / .xlsx / .txt (or the legacy kamus_media.*) in the app
folder — or set media_dictionary_file in the config — and it is loaded and
merged automatically.
"""

import os
import re
from urllib.parse import urlparse

# Core curated outlets. Used to (1) name outlets in results,
# (2) drive default per-media queries & sitemap crawling.
MEDIA_DICT = {
    "detik.com": "Detik",
    "kompas.com": "Kompas.com",
    "kompas.id": "Kompas.id",
    "tribunnews.com": "Tribunnews",
    "cnnindonesia.com": "CNN Indonesia",
    "cnbcindonesia.com": "CNBC Indonesia",
    "liputan6.com": "Liputan6",
    "tempo.co": "Tempo",
    "antaranews.com": "Antara",
    "republika.co.id": "Republika",
    "sindonews.com": "Sindonews",
    "okezone.com": "Okezone",
    "suara.com": "Suara.com",
    "merdeka.com": "Merdeka.com",
    "kumparan.com": "Kumparan",
    "tirto.id": "Tirto",
    "viva.co.id": "Viva",
    "jpnn.com": "JPNN",
    "medcom.id": "Medcom",
    "inews.id": "iNews",
    "idntimes.com": "IDN Times",
    "bisnis.com": "Bisnis.com",
    "katadata.co.id": "Katadata",
    "beritasatu.com": "Berita Satu",
    "mediaindonesia.com": "Media Indonesia",
    "jawapos.com": "Jawa Pos",
    "pikiran-rakyat.com": "Pikiran Rakyat",
    "grid.id": "Grid.id",
    "rri.co.id": "RRI",
    "voi.id": "VOI",
    "kontan.co.id": "Kontan",
    "solopos.com": "Solopos",
    "krjogja.com": "KR Jogja",
    "radarsemarang.jawapos.com": "Radar Semarang",
    "ayosemarang.com": "Ayo Semarang",
    "gatra.com": "Gatra",
    "wartaekonomi.co.id": "Warta Ekonomi",
    "investor.id": "Investor Daily",
    "thejakartapost.com": "The Jakarta Post",
    # --- additional national ---
    "rmol.id": "RMOL",
    "elshinta.com": "Elshinta",
    "era.id": "ERA",
    "kbr.id": "KBR",
    "alinea.id": "Alinea",
    "validnews.id": "Valid News",
    "law-justice.co": "Law-Justice",
    "akurat.co": "Akurat",
    "wartakota.tribunnews.com": "Warta Kota",
    "poskota.co.id": "Pos Kota",
    "fin.co.id": "Fajar Indonesia Network",
    "disway.id": "Disway",
    # --- regional / networks ---
    "suaramerdeka.com": "Suara Merdeka",
    "harianjogja.com": "Harian Jogja",
    "beritajatim.com": "Berita Jatim",
    "timesindonesia.co.id": "Times Indonesia",
    "jatimnow.com": "JatimNow",
    "ayobandung.com": "Ayo Bandung",
    "jabarekspres.com": "Jabar Ekspres",
    "radarbogor.id": "Radar Bogor",
    "bantennews.co.id": "Banten News",
    "fajar.co.id": "Fajar",
    "serambinews.com": "Serambi Indonesia",
    "banjarmasinpost.co.id": "Banjarmasin Post",
    "pos-kupang.com": "Pos Kupang",
    "sripoku.com": "Sriwijaya Post",
    "bangkapos.com": "Bangka Pos",
    "sumeks.co": "Sumatera Ekspres",
    "prokal.co": "Prokal",
    "kaltimtoday.co": "Kaltim Today",
    "magelangekspres.com": "Magelang Ekspres",
    "betanews.id": "Beta News",
    "murianews.com": "Muria News",
}

# CORE = the curated list above. Distinguished from the external dictionary
# (thousands of outlets) when prioritizing active harvesting.
CORE_MEDIA = frozenset(MEDIA_DICT)

# Direct RSS feeds from media sites (not via Google) — the fastest route
# to BRAND-NEW articles. Format: domain -> list of feed URLs.
RSS_FEEDS = {
    "detik.com": ["https://rss.detik.com/index.php/detikcom"],
    "tempo.co": ["https://rss.tempo.co/nasional", "https://rss.tempo.co/bisnis"],
    "antaranews.com": ["https://www.antaranews.com/rss/terkini.xml"],
    "cnnindonesia.com": ["https://www.cnnindonesia.com/rss"],
    "republika.co.id": ["https://www.republika.co.id/rss"],
    "suara.com": ["https://www.suara.com/rss"],
    "sindonews.com": ["https://www.sindonews.com/feed"],
    "liputan6.com": ["https://feed.liputan6.com/rss"],
    "merdeka.com": ["https://www.merdeka.com/feed/"],
    "tirto.id": ["https://tirto.id/rss"],
    "jpnn.com": ["https://www.jpnn.com/index.php?mib=rss"],
    "voi.id": ["https://voi.id/rss"],
}


def url_domain(url: str) -> str:
    """Clean host (without www.) from a URL — subdomains preserved:
    https://bogor.viva.co.id/x -> bogor.viva.co.id"""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


_GENERIC_SUBDOMAINS = ("www", "m", "amp", "mobile", "wap", "beta")


def media_outlet(url: str) -> str:
    """The MEDIA outlet, taken straight from the news URL: full host with
    meaningful subdomains preserved, technical prefixes removed.
    www.kompas.com/x -> kompas.com | bogor.viva.co.id -> bogor.viva.co.id
    m.detik.com -> detik.com | amp.tirto.id -> tirto.id"""
    parts = url_domain(url).split(".")
    while len(parts) > 2 and parts[0] in _GENERIC_SUBDOMAINS:
        parts.pop(0)
    return ".".join(parts)


def media_name(url: str) -> str:
    """Outlet name from a URL; falls back to a tidied domain name.
    Direct lookup per subdomain level — fast even with 13,000+ entries."""
    host = url_domain(url)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        name = MEDIA_DICT.get(".".join(parts[i:]))
        if name:
            return name
    return parts[-2].capitalize() if len(parts) >= 2 else host


# ============================================================
#            EXTERNAL DICTIONARY (user-owned file)
# ============================================================
DICTIONARY_FILENAMES = ("media_dictionary.csv", "media_dictionary.xlsx",
                        "media_dictionary.txt",
                        "kamus_media.csv", "kamus_media.xlsx", "kamus_media.txt")


def _clean_domain(value) -> str:
    """Normalize a cell into a domain: strip http://, www., paths, spaces."""
    v = str(value).strip().lower()
    if not v or v in ("nan", "none"):
        return ""
    v = re.sub(r"^https?://", "", v)
    v = v.split("/")[0].split("?")[0].strip()
    if v.startswith("www."):
        v = v[4:]
    if "." not in v or " " in v or len(v) < 4:
        return ""
    if re.fullmatch(r"[\d.:]+", v):   # bare IP address — not an outlet
        return ""
    return v


def _load_table(path):
    """Read CSV/XLSX/TXT -> list of (domain, name). Columns auto-detected."""
    import pandas as pd
    if path.endswith(".txt"):
        rows = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = re.split(r"[,;\t]", line.strip(), maxsplit=1)
                d = _clean_domain(parts[0])
                if d:
                    rows.append((d, parts[1].strip() if len(parts) > 1 else ""))
        return rows
    df = pd.read_csv(path, dtype=str) if path.endswith(".csv") else \
        pd.read_excel(path, dtype=str)
    if df.empty:
        return []
    # domain column = the column with the most valid-domain cells
    score = {k: df[k].map(_clean_domain).astype(bool).sum() for k in df.columns}
    domain_col = max(score, key=score.get)
    if score[domain_col] == 0:
        raise ValueError("No column containing media domains/URLs was found.")

    def looks_like_name(value):
        """A cell counts as a media name if it is text, not a number/domain."""
        v = str(value).strip()
        if not v or v.lower() in ("nan", "none"):
            return False
        if re.fullmatch(r"[\d .,\-/]+", v):
            return False
        return not _clean_domain(v)
    name_score = {k: df[k].map(looks_like_name).sum()
                  for k in df.columns if k != domain_col}
    name_col = max(name_score, key=name_score.get) if name_score else None
    if name_col is not None and name_score[name_col] == 0:
        name_col = None
    rows = []
    for _, r in df.iterrows():
        d = _clean_domain(r[domain_col])
        if d:
            name = str(r[name_col]).strip() if name_col and str(r[name_col]) != "nan" else ""
            rows.append((d, name))
    return rows


# Bare platform/hosting domains: NEVER media outlets by themselves — a bare
# "blogspot.com" entry would make EVERY blog match the dictionary.
PLATFORM_DOMAINS = frozenset("""
blogspot.com wordpress.com medium.com my.id web.id co.id or.id go.id
eu.org home.blog html-5.me esy.es hol.es pe.hu rf.gd 22web.org byethost15.com
byethost32.com heliohost.org totalh.net is-great.net is-great.org is-best.net
mywebcommunity.org medianewsonline.com atwebpages.com 000webhostapp.com
mypressonline.com getenjoyment.net synergize.co social-networking.me
my.vg id.vg uk.tn hk.tn dx.am 66ghz.com 96.lt wixsite.com weebly.com
""".split())

# Truncated Indonesian second-level TLDs (a common export artifact:
# "kemnaker.go" is really "kemnaker.go.id")
_TRUNCATED_TLDS = {".go": ".go.id", ".or": ".or.id", ".ac": ".ac.id",
                   ".web": ".web.id", ".sch": ".sch.id", ".mil": ".mil.id"}
_REAL_FOREIGN_MIL = frozenset({"af.mil", "pacom.mil"})


def clean_dictionary_file(src, dest="media_dictionary.txt", log=print,
                          drop_redundant_subdomains=True):
    """Clean a raw media list (one domain/URL per line) into a tidy
    media_dictionary.txt the engine auto-loads. Fixes truncated TLDs,
    lowercases, dedupes, drops bare platform domains and (optionally)
    subdomains whose parent domain is already listed. Returns stats."""
    stats = {"read": 0, "kept": 0, "fixed_tld": 0, "dropped_platform": 0,
             "dropped_invalid": 0, "dropped_redundant": 0, "duplicates": 0}

    def _raw_values(path):
        """Yield raw cells: text files line by line, CSV/Excel via pandas
        (picking the column that contains the most domains)."""
        pl = str(path).lower()
        if pl.endswith((".csv", ".xlsx", ".xls")):
            import pandas as pd
            df = (pd.read_csv(path, dtype=str) if pl.endswith(".csv")
                  else pd.read_excel(path, dtype=str))
            if df.empty:
                return
            score = {k: df[k].map(_clean_domain).astype(bool).sum()
                     for k in df.columns}
            col = max(score, key=score.get)
            if score[col] == 0:
                raise ValueError("No column containing media domains/URLs "
                                 "was found in " + str(path))
            for v in df[col].tolist():
                yield "" if v is None else str(v)
        else:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    yield line.split(",")[0].split(";")[0].split("\t")[0]

    domains = set()
    for value in _raw_values(src):
        stats["read"] += 1
        d = _clean_domain(value)
        if not d:
            if value.strip() and value.strip().lower() not in ("nan", "none"):
                stats["dropped_invalid"] += 1
            continue
        for trunc, full in _TRUNCATED_TLDS.items():
            if d.endswith(trunc) and d not in _REAL_FOREIGN_MIL:
                d = d[: -len(trunc)] + full
                stats["fixed_tld"] += 1
                break
        if d in PLATFORM_DOMAINS:
            stats["dropped_platform"] += 1
            continue
        if d in domains:
            stats["duplicates"] += 1
            continue
        domains.add(d)
    if drop_redundant_subdomains:
        keep = set()
        for d in domains:
            parts = d.split(".")
            redundant = any(".".join(parts[i:]) in domains
                            for i in range(1, len(parts) - 1))
            if redundant:
                stats["dropped_redundant"] += 1
            else:
                keep.add(d)
        domains = keep
    stats["kept"] = len(domains)
    with open(dest, "w", encoding="utf-8") as f:
        for d in sorted(domains):
            f.write(d + "\n")
    log(f"📚 Dictionary cleaned: {stats['read']} lines read -> "
        f"{stats['kept']} domains kept ({dest})")
    log(f"   fixed truncated TLDs: {stats['fixed_tld']} | "
        f"platform domains dropped: {stats['dropped_platform']} | "
        f"invalid dropped: {stats['dropped_invalid']} | "
        f"duplicates: {stats['duplicates']} | "
        f"redundant subdomains merged: {stats['dropped_redundant']}")
    return stats


# Ad-hoc additions on top of the dictionaries (config: extra_media).
# Kept separate so the curated dictionary file stays untouched and the
# additions are visible/editable straight from the dashboard.
EXTRA_MEDIA = set()


def set_extra_media(domains):
    """Replace the ad-hoc whitelist additions (list of domains/URLs)."""
    EXTRA_MEDIA.clear()
    for d in domains or ():
        cleaned = _clean_domain(d)
        if cleaned:
            EXTRA_MEDIA.add(cleaned)
    return len(EXTRA_MEDIA)


def in_dictionary(url: str) -> bool:
    """True if the URL's host (at any subdomain level) is a known outlet:
    core dictionary, the user's external dictionary, or the ad-hoc
    extra_media additions."""
    parts = url_domain(url).split(".")
    return any(".".join(parts[i:]) in MEDIA_DICT
               or ".".join(parts[i:]) in EXTRA_MEDIA
               for i in range(len(parts) - 1))


def load_external_dictionary(path="", log=print) -> int:
    """Load the user's media dictionary and merge it into MEDIA_DICT.
    Empty path -> auto-detect media_dictionary.* / kamus_media.* in the
    working folder. Returns the number of new domains. Safe to call again."""
    if not path:
        path = next((n for n in DICTIONARY_FILENAMES if os.path.exists(n)), "")
        if not path:
            return 0
    if not os.path.exists(path):
        log(f"⚠️ Media dictionary file not found: {path}")
        return 0
    try:
        rows = _load_table(path)
    except Exception as e:
        log(f"⚠️ Failed to read media dictionary {path}: {type(e).__name__}: {e}")
        return 0
    added = 0
    for domain, name in rows:
        if domain not in MEDIA_DICT:
            parts = domain.split(".")
            MEDIA_DICT[domain] = name or (parts[-2].capitalize()
                                          if len(parts) >= 2 else domain)
            added += 1
        elif name and domain not in CORE_MEDIA:
            MEDIA_DICT[domain] = name
    log(f"📚 External media dictionary '{path}': {len(rows)} rows read, "
        f"{added} new domains (dictionary total: {len(MEDIA_DICT)}).")
    return added
