"""Article extraction: 4 fallback layers.

normal -> Googlebot UA -> cloudscraper (beats HTTP 403 / anti-bot) -> AMP
version. Metadata (title, author, date+time, summary) comes from meta tags,
JSON-LD and text patterns. Content is cleaned of "Baca juga"/ads junk, the
main image is captured, and people (speakers + all mentions) are extracted.
"""

import datetime as _dt
import json
import re
import time
from urllib.parse import urljoin

import requests
import trafilatura
from bs4 import BeautifulSoup

from .media import media_name, url_domain
from .organizations import extract_orgs
from .persons import extract_people

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
GOOGLEBOT = {
    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Accept-Language": "id-ID,id;q=0.9",
}
SUMMARY_SENTENCES = 3

# Junk lines that leak into extracted content: "Baca juga" link blocks,
# ads, scroll prompts, follow-us calls. Removed for clean report content —
# they also poison keyword verification and person extraction ("PLN" in a
# "Baca juga" headline must never count as a PLN mention).
JUNK_LINE_RE = re.compile(
    r"^\s*(?:baca\s*juga|baca\s*:|simak\s*juga|simak\s*video|simak\s*breaking|"
    r"lihat\s*juga|saksikan\s*video|tonton\s*(?:juga|video)|artikel\s*terkait|"
    r"berita\s*terkait|ringkasan\s*berita|advertisement|advertisment|"
    r"scroll\s*to\s*continue|pilihan\s*editor|video\s*pilihan|"
    r"berita\s*populer|rekomendasi\s*untuk|konten\s*promosi|"
    r"halaman\s*selanjutnya|halaman\s*[:：]|tags?\s*[:：]|penulis\s*[:：]|"
    r"editor\s*[:：]|reporter\s*[:：]|sumber\s*[:：]|kontributor\s*[:：]|"
    r"\(?\s*(?:foto|video|gambar|dok)\s*[.:：]|copyright|hak\s*cipta|"
    r"artikel\s*ini\s*telah|berita\s*ini\s*telah|telah\s*tayang\s*di|"
    r"disclaimer|"
    r"ikuti\s*(?:berita|kami|update)|gabung\s*(?:channel|grup)|"
    r"follow\s*(?:whatsapp|instagram)|dapatkan\s*(?:berita|update|info)|"
    r"download\s*aplikasi|install\s*aplikasi|klik\s*di\s*sini|"
    r"live\s*update|update\s*terus|jangan\s*lewatkan|kunjungi\s*laman)",
    re.IGNORECASE)

# "Baca juga: Some Headline Title" glued INSIDE a line (newlines lost):
# remove from the marker up to the end of the embedded headline (the next
# sentence terminator or end of line).
JUNK_INLINE_RE = re.compile(
    r"(?:baca|simak|lihat|tonton)\s+juga\s*:?\s*[^.\n!?]*[.!?]?",
    re.IGNORECASE)

# "(Sumber: yt otorider)" / "Sumber: xyz" glued at the end of a line
SOURCE_INLINE_RE = re.compile(
    r"\(?\s*sumber\s*[:：][^.\n)]{0,60}\)?\s*[.!?]?\s*$", re.IGNORECASE)

# Video/photo credit fragments glued to a caption or paragraph end:
# "...fitur yang cukup lengkap.–Yt Otorider–Yt Otorider"
CREDIT_TAIL_RE = re.compile(
    r"(?:\s*[-–—]{1,2}\s*(?:yt|youtube|ig|instagram|tiktok|fb|facebook|"
    r"foto|video|dok|sumber|source|ist|antara)\b[^-–—\n]{0,40})+\s*$",
    re.IGNORECASE)

# Site dateline prefix: "TRIBUNKALTIM.CO - ", "KOMPAS.com -",
# "JAKARTA, DISWAYMALANG.ID –", "BOGOR, viva.co.id -". The dateline can sit
# on any of the first few paragraphs (captions often come first).
DATELINE_RE = re.compile(
    r"^\s*(?:[A-Z][A-Z0-9 .,()'\-]{0,35}?,\s*)?"       # optional "JAKARTA, "
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]{2,10})+"          # domain-like token
    r"\s*[-–—,]{1,2}\s*")
# Plain city dateline: "JAKARTA - Pemerintah ..." (all-caps city + dash)
CITY_DASH_RE = re.compile(
    r"^\s*[A-Z][A-Z .,']{1,30}\s*[-–—]{1,2}\s+(?=[A-Z\"“0-9])")

_DATELINE_SCAN_LINES = 3  # datelines can hide below a leading caption


def clean_content(text):
    """Drop 'Baca juga' / ads / datelines / source & video credits from
    extracted text — whole lines and fragments glued inside a line."""
    if not text:
        return text
    lines = []
    seen_content = 0
    for line in str(text).splitlines():
        stripped = line.strip()
        if JUNK_LINE_RE.match(stripped):
            continue
        stripped = JUNK_INLINE_RE.sub(" ", stripped)
        stripped = CREDIT_TAIL_RE.sub("", stripped)
        stripped = SOURCE_INLINE_RE.sub("", stripped)
        if stripped and seen_content < _DATELINE_SCAN_LINES:
            cut = DATELINE_RE.sub("", stripped, count=1)
            if cut == stripped:
                cut = CITY_DASH_RE.sub("", stripped, count=1)
            stripped = cut
            seen_content += 1
        stripped = re.sub(r"\s{2,}", " ", stripped).strip()
        if stripped:
            lines.append(stripped)
    return "\n".join(lines).strip()


# Site taglines that sit behind the headline in <title>:
# "... - Palpres Online - Menyajikan berita terkini nasional ..."
_TAGLINE_RE = re.compile(
    r"(menyajikan\s+berita|portal\s+berita|media\s+informasi|situs\s+berita|"
    r"berita\s+(indonesia|terkini|terbaru|terupdate|hari\s+ini)|"
    r"kabar\s+terkini|informasi\s+seputar|terlengkap\s+seputar|"
    r"berita\s+dan\s+informasi|news\s+portal|latest\s+news)", re.IGNORECASE)
_TITLE_SPLIT_RE = re.compile(r"\s+[|｜–—]\s+|\s+-\s+|\s+::\s+")


def clean_title(title, url=""):
    """Strip the site identity that most CMSs append to <title>:
    'PLN Bangun Gardu - Palpres Online - Menyajikan berita terkini ...'
    -> 'PLN Bangun Gardu'. Only trailing segments that are clearly the
    outlet's own name or tagline are removed, never the headline."""
    if not title:
        return ""
    parts = [p.strip() for p in _TITLE_SPLIT_RE.split(str(title).strip()) if p.strip()]
    if not parts:
        return ""
    host = url_domain(url) if url else ""
    root = host.split(".")[0] if host else ""
    outlet = media_name(url).lower() if url else ""
    while len(parts) > 1:
        last = parts[-1]
        squashed = re.sub(r"[^a-z0-9]", "", last.lower())
        if (_TAGLINE_RE.search(last)
                or (root and root in squashed)
                or (outlet and outlet.replace(".", "").replace(" ", "") in squashed)):
            parts.pop()
            continue
        break
    return " - ".join(parts).strip(" -|–—")


def looks_like_index_page(title, url="", content=""):
    """True when a page is an index/tag/search listing rather than an
    article. Signature seen in the wild: the <title> STARTS with the site's
    own domain — 'PALPRES.COM bentuk dukungan - Palpres Online - ...',
    'disway.id isu identitas - ...'. Real headlines never do that."""
    t = (title or "").strip().lower()
    if not t:
        return False
    host = url_domain(url) if url else ""
    if host:
        if t.startswith(host) or t.startswith("www." + host):
            return True
        root = host.split(".")[0]
        # 'palpres.com ...' even when the URL host carries a subdomain
        if root and re.match(rf"^{re.escape(root)}\.[a-z]{{2,}}\b", t):
            return True
    # nothing left once the site identity is stripped -> a listing page
    if len(clean_title(title, url)) < 12 and _TAGLINE_RE.search(title or ""):
        return True
    return False


MIN_ARTICLE_CHARS = 150     # absolute floor; short news briefs are real news
PARAGRAPH_CHARS = 120       # one flowing paragraph proves it is a story


def looks_like_article(content, title="", url=""):
    """True only for pages that really carry a news story.

    Judged by STRUCTURE, not just length: listing pages (home, tag, index,
    category) also produce text — but as a pile of short headline stubs.
    A real article has at least one flowing paragraph, or enough prose to
    be one. Short news briefs must still pass, so length alone never
    decides (that would create missed clips)."""
    text = (content or "").strip()
    if len(text) < MIN_ARTICLE_CHARS:
        return False
    if looks_like_index_page(title, url, text):
        return False
    # at least two sentence endings: listings rarely have them
    if len(re.findall(r"[.!?](?:\s|$)", text)) < 2:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 6:
        short = sum(1 for ln in lines if len(ln) < 60)
        if short / len(lines) > 0.8:      # a wall of headline stubs
            return False
    longest = max((len(ln) for ln in lines), default=0)
    # a real paragraph, or plenty of prose spread over several sentences
    return longest >= PARAGRAPH_CHARS or len(text) >= 400


def main_image(soup):
    """Main article image URL (og:image / twitter:image / first img)."""
    for attr, val in (("property", "og:image"), ("name", "twitter:image"),
                      ("property", "og:image:url"), ("itemprop", "image")):
        tag = soup.find("meta", {attr: val})
        if tag and tag.get("content", "").startswith("http"):
            return tag["content"]
    article = soup.find("article")
    img = (article.find("img") if article else None) or soup.find("img")
    if img and img.get("src", "").startswith("http"):
        return img["src"]
    return ""


def _extract_content(html):
    try:
        return trafilatura.extract(html, include_comments=False,
                                   include_tables=False, favor_precision=True)
    except Exception:
        return None


def _fetch_html(url, timeout):
    """Return (html, content, method, error)."""
    error, last_html = "", ""

    def via_requests(hdr):
        return requests.get(url, headers=hdr, timeout=timeout)

    def via_cloudscraper(_):
        import cloudscraper
        # close the session or its connection pool leaks file descriptors
        # (macOS caps a process at 256 open files by default)
        s = cloudscraper.create_scraper()
        try:
            return s.get(url, timeout=timeout)
        finally:
            s.close()

    layers = [("normal", via_requests, HEADERS),
              ("googlebot", via_requests, GOOGLEBOT),
              ("cloudscraper", via_cloudscraper, None)]

    # Network-level failures (timeout / connection refused) will not be
    # fixed by a different User-Agent — skip the disguise layer and go
    # straight to cloudscraper (different TLS stack), single attempt.
    # This cuts the worst case per dead link from ~2 minutes to ~40s.
    net_fail = False
    for method, call, hdr in layers:
        if net_fail and method == "googlebot":
            continue
        attempts = 1 if net_fail else 2
        for attempt in range(attempts):
            try:
                r = call(hdr)
                if r.status_code >= 400:
                    error = f"HTTP {r.status_code}"
                    last_html = r.text or last_html
                    break
                last_html = r.text
                content = _extract_content(last_html)
                if content:
                    return last_html, content, method, ""
                error = "Empty content"
                break
            except requests.exceptions.Timeout:
                error = "Timeout"
                break
            except requests.exceptions.ConnectionError:
                error = "ConnectionError"
                if attempt + 1 < attempts:
                    time.sleep(2)
                    continue
                break
            except ImportError:
                error = error or "Empty content"
                break
            except Exception as e:
                error = type(e).__name__
                break
        if error in ("Timeout", "ConnectionError"):
            net_fail = True

    # layer 4: AMP version
    if last_html:
        m = (re.search(r'rel=["\']amphtml["\'][^>]*href=["\']([^"\']+)', last_html)
             or re.search(r'href=["\']([^"\']+)["\'][^>]*rel=["\']amphtml', last_html))
        if m:
            try:
                r = requests.get(urljoin(url, m.group(1)), headers=HEADERS,
                                 timeout=timeout)
                if r.ok:
                    content = _extract_content(r.text)
                    if content:
                        return r.text, content, "amp", ""
            except Exception:
                pass
    return last_html, "", "", error


def _detect_author(soup, meta, html):
    if meta and meta.author:
        return meta.author
    for sel in [{"name": "author"}, {"property": "article:author"},
                {"name": "content_author"}]:
        tag = soup.find("meta", sel)
        if tag and tag.get("content") and not tag["content"].startswith("http"):
            return tag["content"].strip()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                a = item.get("author")
                if isinstance(a, dict) and a.get("name"):
                    return a["name"].strip()
                if isinstance(a, list) and a and isinstance(a[0], dict) and a[0].get("name"):
                    return ", ".join(x.get("name", "") for x in a if isinstance(x, dict))
                if isinstance(a, str) and a.strip():
                    return a.strip()
        except Exception:
            continue
    m = re.search(
        r"(?:Penulis|Reporter|Pewarta|Jurnalis|Editor|Kontributor|Oleh)\s*[:\-–]\s*"
        r"([A-Z][A-Za-z .,'\-]{2,60})", html)
    return m.group(1).strip().rstrip(",.") if m else ""


_MONTHS_ID = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maret": 3, "mar": 3,
    "april": 4, "apr": 4, "mei": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "agustus": 8, "agu": 8, "ags": 8, "agst": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "des": 12, "dec": 12,
}
# "Kamis, 30 Juli 2026 18:03 WIB" / "30 Jul 2026, 18.03 WITA" — the visible
# date line most Indonesian news sites print even when meta tags lack a time
_TEXT_DT_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})[^0-9]{0,8}"
    r"(\d{1,2})[:.](\d{2})(?:\s*(WIB|WITA|WIT))?", re.IGNORECASE)


def _parse_dt_string(value):
    """Parse one date(/time) candidate string. Return (date, time) —
    either may be ''. Timezone-aware values are converted to WIB."""
    s = str(value).strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})"
                 r"(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?", s)
    if m:
        y, mo, d, h, mi = (int(m.group(i)) for i in range(1, 6))
        off = m.group(6)
        try:
            dt = _dt.datetime(y, mo, d, h, mi)
            if off:  # convert to WIB (UTC+7)
                if off in ("Z", "z"):
                    minutes = 0
                else:
                    sign = 1 if off[0] == "+" else -1
                    hh, mm = off[1:].replace(":", "")[:2], off[1:].replace(":", "")[2:4]
                    minutes = sign * (int(hh) * 60 + int(mm or 0))
                dt = dt - _dt.timedelta(minutes=minutes) + _dt.timedelta(hours=7)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
        except ValueError:
            return "", ""
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", ""
    m = re.match(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})(?:[ ,]+(\d{1,2})[:.](\d{2}))?", s)
    if m:  # DD/MM/YYYY [HH:MM]
        date = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        time = f"{int(m.group(4)):02d}:{m.group(5)}" if m.group(4) else ""
        return date, time
    m = _TEXT_DT_RE.match(s)
    if m:
        return _from_text_match(m)
    return "", ""


def _from_text_match(m):
    """Turn a _TEXT_DT_RE match into (date, time) in WIB."""
    month = _MONTHS_ID.get(m.group(2).lower())
    if not month:
        return "", ""
    try:
        h, mi = int(m.group(4)), int(m.group(5))
        dt = _dt.datetime(int(m.group(3)), month, int(m.group(1)), h, mi)
    except ValueError:
        return "", ""
    zone = (m.group(6) or "WIB").upper()
    dt -= _dt.timedelta(hours={"WIB": 0, "WITA": 1, "WIT": 2}[zone])
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


def _detect_datetime(soup):
    candidates = []
    for sel in [{"property": "article:published_time"}, {"itemprop": "datePublished"},
                {"name": "publishdate"}, {"name": "pubdate"},
                {"name": "DC.date.issued"}, {"name": "date"},
                {"property": "og:article:published_time"}]:
        tag = soup.find("meta", sel)
        if tag and tag.get("content"):
            candidates.append(tag["content"])
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for item in (data if isinstance(data, list) else [data]):
                if isinstance(item, dict) and item.get("datePublished"):
                    candidates.append(item["datePublished"])
        except Exception:
            continue
    for tag_time in soup.find_all("time"):
        if tag_time.get("datetime"):
            candidates.append(tag_time["datetime"])
        else:
            candidates.append(tag_time.get_text(" ", strip=True))
    # modified/updated stamps: lowest priority, better than nothing
    for sel in [{"property": "article:modified_time"},
                {"property": "og:updated_time"}, {"itemprop": "dateModified"}]:
        tag = soup.find("meta", sel)
        if tag and tag.get("content"):
            candidates.append(tag["content"])

    best_date = ""
    for c in candidates:
        d, t = _parse_dt_string(c)
        if not d or d.startswith(("1970", "0001")):
            continue
        if t:
            return d, t
        best_date = best_date or d

    # last resort: the visible "Kamis, 30 Juli 2026 18:03 WIB" line
    m = _TEXT_DT_RE.search(soup.get_text(" ", strip=True)[:4000])
    if m:
        d2, t2 = _from_text_match(m)
        if t2 and (not best_date or d2 == best_date or not d2):
            return best_date or d2, t2
    return best_date, ""


def _summary(text, count=SUMMARY_SENTENCES):
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z\"“0-9])", clean)
    return " ".join(sentences[:count]).strip()


def extract_article(url, timeout=20):
    """Fetch and dissect one article. Returns a complete dict."""
    result = {"url": url, "media": media_name(url), "title": "", "author": "",
              "date": "", "time": "", "summary": "", "content": "",
              "image": "", "influencer": "", "persons": "", "organizations": "",
              "method": "", "error": "", "is_index": False, "is_article": False}
    html, content, method, error = _fetch_html(url, timeout)
    result["method"], result["error"] = method, error
    content = clean_content(content)
    if html:
        try:
            meta = trafilatura.extract_metadata(html)
        except Exception:
            meta = None
        soup = BeautifulSoup(html, "lxml")
        raw_title = (meta.title if meta and meta.title else
                     (soup.title.get_text(strip=True) if soup.title else ""))
        # tag/search/index listings must never reach the report as articles
        result["is_index"] = looks_like_index_page(raw_title, url, content or "")
        result["title"] = clean_title(raw_title, url)
        result["is_article"] = looks_like_article(content or "", raw_title, url)
        result["author"] = _detect_author(soup, meta, html)
        date, time_ = _detect_datetime(soup)
        result["date"] = date or (meta.date if meta and meta.date else "")
        result["time"] = time_
        result["content"] = content or ""
        result["summary"] = _summary(content)
        result["image"] = main_image(soup)
        speakers, persons = extract_people(content)
        result["influencer"] = ", ".join(speakers)
        result["persons"] = ", ".join(persons)
        result["organizations"] = ", ".join(extract_orgs(content))
    return result
