"""Is this link an article from a news medium? Used to keep only media news from Google's "All" results
(and to drop obvious non-media links from the other sources).

is_media(url) -> True  (known medium, or looks like a news site's article)
                 False (social media, video, forum, marketplace, encyclopedia, files, ... = junk)
                 None  (unknown — not clearly a medium)
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Indonesian national/regional and international news media (registrable domain or a suffix of it).
KNOWN_MEDIA = {
    # national
    "kompas.com", "kompas.id", "kompas.tv", "detik.com", "tribunnews.com", "liputan6.com", "cnnindonesia.com",
    "cnbcindonesia.com", "tempo.co", "antaranews.com", "republika.co.id", "okezone.com", "sindonews.com", "merdeka.com",
    "suara.com", "idntimes.com", "kumparan.com", "jpnn.com", "viva.co.id", "inews.id", "beritasatu.com", "bisnis.com",
    "kontan.co.id", "katadata.co.id", "mediaindonesia.com", "jawapos.com", "tirto.id", "rmol.id", "medcom.id",
    "metrotvnews.com", "tvonenews.com", "idxchannel.com", "investor.id", "thejakartapost.com", "jakartaglobe.id", "voi.id",
    "gatra.com", "inilah.com", "akurat.co", "wartaekonomi.co.id", "era.id", "hops.id", "grid.id", "rri.co.id", "tvri.go.id",
    "kbr.id", "mongabay.co.id", "theconversation.com", "bbc.com", "voaindonesia.com", "dw.com", "asumsi.co", "narasi.tv",
    "kompas.co.id", "sindonews.net", "harianjogja.com", "solopos.com", "pikiran-rakyat.com", "ayobandung.com",
    "fajar.co.id", "tribun-timur.com", "radarbogor.id", "sumutpos.co", "waspada.id", "analisadaily.com", "lampost.co",
    "timesindonesia.co.id", "suarasurabaya.net", "beritajatim.com", "jatimnow.com", "kabar24.bisnis.com", "batamnews.co.id",
    "riauonline.co.id", "goriau.com", "jambiekspres.co.id", "palembang-pos.com", "sumeks.co", "radarlampung.co.id",
    "radarsolo.jawapos.com", "radarjogja.jawapos.com", "jogjapolitan.harianjogja.com", "krjogja.com", "suaramerdeka.com",
    "wartakotalive.com", "poskota.co.id", "beritajakarta.id", "infopublik.id", "balipost.com", "nusabali.com", "pos-kupang.com", "victorynews.id", "lombokpost.jawapos.com", "insidelombok.id",
    "kaltimpost.jawapos.com", "prokal.co", "banjarmasinpost.co.id", "kalteng.co", "pontianakpost.jawapos.com",
    "manadopost.jawapos.com", "tribunmanado.co.id", "makassar.terkini.id", "gorontalopost.jawapos.com", "palu.tribunnews.com",
    "ambon.tribunnews.com", "malutpost.com", "cenderawasihpos.jawapos.com", "jubi.id", "papuabaratnews.co",
    "ekonomi.bisnis.com", "katadata.id", "dunia-energi.com", "ruangenergi.com", "petromindo.com", "energi.media",
    # international
    "reuters.com", "apnews.com", "bloomberg.com", "nytimes.com", "theguardian.com", "aljazeera.com", "cnn.com",
    "straitstimes.com", "channelnewsasia.com", "scmp.com", "nikkei.com", "asia.nikkei.com", "ft.com", "wsj.com",
    "washingtonpost.com", "abc.net.au", "smh.com.au", "thediplomat.com", "eco-business.com", "benarnews.org",
}

# Never news articles from a medium.
JUNK = {
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com", "youtu.be", "threads.net",
    "threads.com", "linkedin.com", "pinterest.com", "reddit.com", "quora.com", "kaskus.co.id", "t.me", "whatsapp.com",
    "wikipedia.org", "wikimedia.org", "brainly.co.id", "brainly.com", "scribd.com", "slideshare.net", "academia.edu",
    "researchgate.net", "tokopedia.com", "shopee.co.id", "bukalapak.com", "lazada.co.id", "blibli.com", "olx.co.id",
    "amazon.com", "play.google.com", "apps.apple.com", "maps.google.com", "google.com", "bing.com", "yahoo.com",
    "glassdoor.com", "jobstreet.co.id", "indeed.com", "linktr.ee", "spotify.com", "soundcloud.com", "medium.com",
    "blogspot.com", "kompasiana.com", "wordpress.com", "tumblr.com", "github.com", "dokumen.tips", "studocu.com", "pdfcoffee.com",
}

_NEWSY_HOST = re.compile(r"news|berita|times|post|pos\b|media|tv|radar|tribun|kabar|warta|harian|daily|online|today|"
                         r"info|pers|press|journal|jurnal|ekspres|express|sindo|metro|suara|antara|portal|katta|terkini",
                         re.I)
_ARTICLE_PATH = re.compile(r"/20\d\d/\d{1,2}/\d{1,2}/|/20\d\d\d\d\d\d|/read/|/berita/|/news/|/artikel/|/article/|/detail/|"
                           r"/baca/|/post/|-\d{5,}(?:\.html?)?/?$|/\d{6,}/|\.html?$", re.I)
_FILE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|jpg|png)$", re.I)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_media(url: str, extra: list[str] | None = None) -> bool | None:
    host = _host(url)
    if not host:
        return False
    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return False
    if _FILE.search(path):
        return False
    if extra and _matches(host, extra):
        return True
    if _matches(host, JUNK):
        return False
    if _matches(host, KNOWN_MEDIA):
        return path not in ("", "/")             # a medium's home or section page is not an article
    if host.endswith(".go.id") or host.endswith(".ac.id") or host.endswith(".sch.id"):
        return None                              # government / campus sites: not media
    name = host.rsplit(".", 2)[0] if host.count(".") >= 2 else host.split(".")[0]   # host without the TLD
    if _NEWSY_HOST.search(name) and _ARTICLE_PATH.search(path):
        return True                              # e.g. radarcirebon.com/2026/10/20/... or beritadaerah.id/read/...
    return None
