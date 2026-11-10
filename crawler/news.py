"""News links from Google (News tab), the way a person does it: open Google, search the keyword with a
custom date range, read the result cards, click "Next" page after page. Optionally open each article
and copy its title, date, author and text. No API.

Google shows a captcha when it sees too many searches too fast; the run waits so you can solve it in
the browser window."""
from __future__ import annotations

import os
import time
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from .browser import BROWSER_CLOSED, BrowserCrawler, Result, Stopped, _parse_iso
from .media import is_media
from .textmatch import verdict

DETAIL_LEVELS = ("links", "basic", "full")
SOURCES = ("feed", "news_tab", "all_tab")
SOURCE_LABEL = {"feed": "Google News feed", "news_tab": "Google Search · News tab", "all_tab": "Google Search · All results (media only)"}
_LEGACY_SOURCE = {"news": ["feed"], "search": ["news_tab"], "both": ["feed", "news_tab"]}
_MAX_TEXT = 30000

_MONTHS = {m: i + 1 for i, names in enumerate([
    ("jan", "januari", "january"), ("feb", "februari", "february"), ("mar", "maret", "march"), ("apr", "april"),
    ("mei", "may"), ("jun", "juni", "june"), ("jul", "juli", "july"), ("agu", "agt", "agustus", "aug", "august"),
    ("sep", "sept", "september"), ("okt", "oktober", "oct", "october"), ("nov", "november", "nop", "nopember"),
    ("des", "desember", "dec", "december")]) for m in names}
_REL = {"menit": "minutes", "minute": "minutes", "min": "minutes", "mins": "minutes", "minutes": "minutes",
        "jam": "hours", "hour": "hours", "hours": "hours", "hari": "days", "day": "days", "days": "days",
        "minggu": "weeks", "week": "weeks", "weeks": "weeks", "bulan": "months", "month": "months", "months": "months",
        "tahun": "years", "year": "years", "years": "years"}


@dataclass
class NewsOptions:
    cap: int = 100                 # max links per keyword (0 = no limit; Google stops around 300 per search)
    by_day: bool = True            # search each day of a longer period separately -> far more results
    region: str = "id"             # "id" = Indonesia/Indonesian, "any" = global English
    exact: bool = False
    exclude: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    detail: str = "basic"          # links | basic (title, media, date) | full (+ article text)
    sources: list[str] = field(default_factory=lambda: list(SOURCES))   # feed / news_tab / all_tab
    resolve: bool = True           # open Google News links to get each media's own article link
    media_only: bool = True        # skip non-media links (social media, forums, shops, wikis, files)
    extra_media: list[str] = field(default_factory=list)   # more domains to treat as media

    @classmethod
    def from_dict(cls, d: dict) -> "NewsOptions":
        d = d or {}
        def split(v):
            items = re.split(r"[,\n]+", v) if isinstance(v, str) else (v if isinstance(v, list) else [])
            return [str(w).strip() for w in items if str(w).strip()]
        def hosts(v):
            out = []
            for s in split(v):
                try:
                    host = urlparse(s if "://" in s else "https://" + s).netloc.lower().removeprefix("www.")
                except ValueError:
                    continue
                if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
                    out.append(host)
            return out
        sites = hosts(d.get("sites"))
        srcs = d.get("sources")
        if isinstance(srcs, list):
            srcs = [x for x in SOURCES if x in srcs]
        else:
            srcs = _LEGACY_SOURCE.get(d.get("source"), list(SOURCES))
        detail = d.get("detail") if d.get("detail") in DETAIL_LEVELS else "basic"
        cap = d.get("cap", 100)
        return cls(cap=max(0, int(cap if cap not in (None, "") else 100)), by_day=bool(d.get("by_day", True)),
                   region="any" if d.get("region") == "any" else "id",
                   exact=bool(d.get("exact")), exclude=[w.lstrip("-") for w in split(d.get("exclude"))],
                   sites=sites[:20], detail=detail,
                   sources=srcs or ["feed"], resolve=bool(d.get("resolve", True)),
                   media_only=bool(d.get("media_only", True)), extra_media=hosts(d.get("extra_media"))[:200])


def google_query(kw: str, opt: NewsOptions) -> str:
    kw = kw.strip()
    parts = [f'"{kw}"' if opt.exact and " " in kw and not kw.startswith('"') else kw]
    parts += [f"-{w}" for w in opt.exclude]
    if opt.sites:
        parts.append("(" + " OR ".join(f"site:{s}" for s in opt.sites) + ")")
    return " ".join(parts)


def google_news_url(q: str, start_local: datetime, end_local: datetime, region: str, tab: str = "news_tab") -> str:
    """Google Search with a custom date range. tab="news_tab" = the News tab (sorted by date); "all_tab" = All results."""
    rng = f"cdr:1,cd_min:{start_local:%m/%d/%Y},cd_max:{end_local:%m/%d/%Y}" + (",sbd:1" if tab == "news_tab" else "")
    loc = "&hl=id&gl=ID" if region == "id" else "&hl=en&gl=US"
    nws = "&tbm=nws" if tab == "news_tab" else ""
    # filter=0: include the results Google hides as "very similar to the ones shown"
    return f"https://www.google.com/search?q={quote(q)}{nws}&tbs={quote(rng, safe=':,')}{loc}&filter=0"


def parse_news_date(text: str, now: datetime) -> datetime | None:
    try:
        return _parse_news_date(text, now)
    except (ValueError, OverflowError):     # e.g. "45 Okt 2026" in a snippet is not a date
        return None


def _parse_news_date(text: str, now: datetime) -> datetime | None:
    """'3 jam lalu', '2 days ago', '5 Okt 2026', 'Oct 5, 2026', '2026-10-05' -> aware datetime."""
    t = (text or "").strip().lower()
    m = re.search(r"(\d+)\s*(menit|jam|hari|minggu|bulan|tahun|mins?|minutes?|hours?|days?|weeks?|months?|years?)\s*(yang\s*)?(lalu|ago)", t)
    if m:
        n, unit = int(m.group(1)), _REL[m.group(2)]
        days = {"minutes": n / 1440, "hours": n / 24, "days": n, "weeks": 7 * n, "months": 30 * n, "years": 365 * n}[unit]
        return now - timedelta(days=days)
    m = re.search(r"(\d{1,2})\s+([a-z]{3,9})\.?\s+(\d{4})", t) or None
    if m and m.group(2) in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), 12, tzinfo=now.tzinfo)
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", t)
    if m and m.group(1) in _MONTHS:
        return datetime(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)), 12, tzinfo=now.tzinfo)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 12, tzinfo=now.tzinfo)
    return None


_RESULTS_JS = r"""() => {
  // Read Google's news result cards without depending on one exact layout: any link to an outside site
  // with a title-like text counts. Google redirect links (/url?q=...) are unwrapped to the real article.
  const root = document.querySelector('#rso') || document.querySelector('#search') || document.querySelector('#center_col') || document.body;
  const skipHost = /(^|\.)(google\.[a-z.]+|googleusercontent\.com|gstatic\.com|googleadservices\.com|youtube\.com|blogger\.com)$/;
  const real = href => {
    try {
      let u = new URL(href, location.href);
      if (/(^|\.)google\.[a-z.]+$/.test(u.hostname) && /^\/(url|aclk|imgres)$/.test(u.pathname)) {
        const q = u.searchParams.get('q') || u.searchParams.get('url') || u.searchParams.get('adurl');
        if (!q) return null;
        u = new URL(q);
      }
      if (!/^https?:$/.test(u.protocol) || skipHost.test(u.hostname)) return null;
      return u.href;
    } catch (e) { return null; }
  };
  const textLines = el => ((el && el.innerText) || '').split('\n').map(s => s.trim()).filter(Boolean);
  const out = [], seen = new Set();
  let outside = 0;
  for (const a of root.querySelectorAll('a[href]')) {
    const url = real(a.getAttribute('href'));
    if (!url) continue;
    outside++;
    if (seen.has(url)) continue;
    const card = a.closest('[data-news-doc-id], .SoaBEf, g-card, article, [data-hveid]') || a;
    const head = a.querySelector('[role="heading"], h3, [aria-level="3"]') || card.querySelector('[role="heading"], h3, [aria-level="3"]');
    let title = head ? head.innerText.trim() : '';
    if (!title) title = textLines(a).filter(t => t.length >= 20).sort((x, y) => y.length - x.length)[0] || '';
    if (!title || title.length < (head ? 3 : 12)) continue;      // icons, "more results" links, etc.
    seen.add(url);
    out.push({url, title, lines: textLines(card).slice(0, 12)});
  }
  return {items: out, outside};
}"""

_FETCH_JS = r"""async (url) => {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 20000);   // never hang on a stalled request
  try { const r = await fetch(url, {credentials: 'include', signal: ctl.signal}); return r.ok ? await r.text() : ''; }
  catch (e) { return ''; } finally { clearTimeout(t); }
}"""


def _key(url: str) -> str:
    """Same article = same key: ignore http/https, www., m./amp variants, tracking parameters, fragments, trailing /."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return url
    host = (u.hostname or "").lower()
    for p in ("www.", "m.", "amp."):
        if host.startswith(p):
            host = host[len(p):]
    path = re.sub(r"/(amp)/?$", "", u.path or "").rstrip("/")
    query = "&".join(sorted(q for q in (u.query or "").split("&")
                            if q and not re.match(r"(utm_[a-z]+|fbclid|gclid|ocid|oc|ref|source)=", q, re.I)))
    return f"{host}{path}" + (f"?{query}" if query else "")


def _gnews_id(url: str) -> str:
    m = re.search(r"/(?:rss/)?articles/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def gnews_open_url(url: str) -> str:
    """The form of a Google News link that forwards to the media when opened: /rss/articles/ID only shows a blank
    page, /articles/ID sends the browser on to the article."""
    return re.sub(r"(news\.google\.[a-z.]+)/rss/articles/", r"\1/articles/", url or "")


def decode_gnews_offline(url: str) -> str:
    """Older Google News ids are base64 of a small record that contains the article URL itself."""
    import base64
    gid = _gnews_id(url)
    if not gid:
        return ""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except (ValueError, TypeError):
        return ""
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m or raw[4:10] == b"AU_yqL":
        return ""
    found = m.group(0).decode("ascii", "ignore")
    # the record may continue straight after the URL with another length-prefixed field
    found = re.split(r"[\x00-\x1f]|\xd2\x01", found)[0]
    return found if "." in urlparse(found).netloc else ""


def _decode_failure_reason(failures) -> str:
    if not failures:
        return "no answer"
    f = failures[0]
    if f.get("error"):
        return "request failed: " + str(f["error"])[:80]
    pages = f.get("pages") or []
    if pages and not any(p.get("sig") for p in pages):
        st = ",".join(str(p.get("status")) for p in pages)
        return f"article page gave no signature (HTTP {st})"
    if f.get("status") and f.get("status") != 200:
        return f"Google refused the request (HTTP {f.get('status')})"
    return "Google's answer had no link"


def _parse_batchexecute(text: str) -> str:
    """Pull the article URL out of Google's batchexecute answer: )]}'  [["wrb.fr","Fbv4je","[\"garturlres\",\"URL\",1]",...]]"""
    import json as _json
    if not text:
        return ""
    rest = text.split("\n", 1)[1] if text.startswith(")]}'") else text
    chunks = [rest.strip()] + text.splitlines()
    for line in chunks:
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            data = _json.loads(line)
        except ValueError:
            continue
        for el in data if isinstance(data, list) else []:
            if isinstance(el, list) and len(el) > 2 and el[0] == "wrb.fr" and isinstance(el[2], str):
                try:
                    inner = _json.loads(el[2])
                except ValueError:
                    continue
                if isinstance(inner, list) and len(inner) > 1 and isinstance(inner[1], str) and inner[1].startswith("http"):
                    return inner[1]
    m = re.search(r'garturlres\\",\\"(https?://[^"\\]+)', text)
    return m.group(1) if m else ""


_GNEWS_DECODE_JS = r"""async (args) => {
  // Same two steps Google News's own redirect page takes: read the article's signature, then ask for its URL.
  // creds 'omit': ask as an anonymous visitor (works whether or not the browser is signed in to Google);
  // 'include': ask with the Google session, then the page's own security token ("at") must go along.
  const [ids, loc, creds] = args;
  const out = {};
  const at = (window.WIZ_global_data || {}).SNlM0e || '';
  const grab = (h, name) => (h.match(new RegExp(name + '="([^"]+)"')) || h.match(new RegExp(name + '\\\\?":\\\\?"([^"\\\\]+)')) || [])[1];
  const one = async (id) => {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 25000);
    const info = {id, pages: [], body: ''};
    try {
      let sg, ts;
      for (const path of ['/rss/articles/' + id + '?' + loc, '/articles/' + id + '?' + loc, '/rss/articles/' + id, '/articles/' + id]) {
        const r = await fetch(path, {credentials: creds || 'omit', signal: ctl.signal});
        const h = await r.text();
        sg = grab(h, 'data-n-a-sg'); ts = grab(h, 'data-n-a-ts');
        info.pages.push({path, status: r.status, size: h.length, sig: !!(sg && ts), head: sg && ts ? '' : h.slice(0, 1500)});
        if (sg && ts) break;
      }
      if (!sg || !ts) { out[id] = info; return; }
      const inner = JSON.stringify(["garturlreq", [["X", "X", ["X", "X"], null, null, 1, 1, "US:en", null, 1, null, null, null,
        null, null, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, null, 0, 0, null, 0], id, Number(ts), sg]);
      let body = 'f.req=' + encodeURIComponent(JSON.stringify([[["Fbv4je", inner, null, "generic"]]]));
      if (creds === 'include' && at) body += '&at=' + encodeURIComponent(at);
      const r = await fetch('/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je', {method: 'POST', credentials: creds || 'omit',
        signal: ctl.signal, headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}, body});
      info.status = r.status; info.body = await r.text(); info.creds = creds;
    } catch (e) { info.error = String(e); } finally { clearTimeout(t); }
    out[id] = info;
  };
  for (let i = 0; i < ids.length; i += 4) await Promise.all(ids.slice(i, i + 4).filter(Boolean).map(one));
  return out;
}"""


def _parse_rss(xml: str) -> list[dict]:
    """Items of a Google News RSS feed: link, title (without the ' - Media' suffix), source, publish time."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        if not link:
            continue
        source = (it.findtext("source") or "").strip()
        title = (it.findtext("title") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "")
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            dt = None
        out.append({"link": link, "title": title, "source": source, "dt": dt})
    return out


_ARTICLE_JS = r"""() => {
  const meta = n => (document.querySelector(`meta[property="${n}"],meta[name="${n}"],meta[itemprop="${n}"]`) || {}).content || '';
  let ld = {};
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const j = JSON.parse(s.textContent);
      const arr = Array.isArray(j) ? j : (j['@graph'] || [j]);
      for (const o of arr) { const t = [].concat(o && o['@type'] || []).join(' ');
        if (/Article|Report|BlogPosting/.test(t)) { ld = o; break; } }
    } catch (e) {}
    if (ld.headline) break;
  }
  const skip = /^(baca juga|baca:|simak|lihat juga|read also|read more|advertisement|scroll to continue|gabung|ikuti)/i;
  let text = (ld.articleBody || '').trim();
  if (text.length < 300) {
    const sels = 'article, [itemprop="articleBody"], .detail__body-text, .read__content, .detail-text, .entry-content, .post-content, .article-content, .content-detail, main';
    const grab = c => [...c.querySelectorAll('p')].map(p => p.innerText.trim()).filter(t => t.length > 40 && !skip.test(t)).join('\n\n');
    let best = '';
    for (const c of document.querySelectorAll(sels)) { const t = grab(c); if (t.length > best.length) best = t; }
    if (best.length < 200) { const t = grab(document.body); if (t.length > best.length) best = t; }   // no article container
    if (best.length > text.length) text = best;
  }
  let author = ld.author || meta('author') || '';
  if (typeof author === 'object') author = [].concat(author).map(a => a && a.name || '').filter(Boolean).join(', ');
  return {title: meta('og:title') || ld.headline || document.title, site: meta('og:site_name'),
          published: meta('article:published_time') || ld.datePublished || meta('datePublished') || meta('pubdate') || '',
          author: String(author), text};
}"""


def _with_cards(res: Result, cards: int) -> Result:
    res.cards = cards
    return res


class NewsCrawler(BrowserCrawler):
    def __init__(self, *a, tz=timezone.utc, debug_dir: str | None = None, **kw):
        super().__init__(*a, **kw)
        self.tz = tz
        self.debug_dir = debug_dir      # where to keep a copy of a results page that could not be read

    def _wait_google_captcha(self) -> bool:
        """True when the page is usable; waits up to ~5 minutes for the user to solve a captcha."""
        from .paths import SERVER_MODE
        for i in range(1 if SERVER_MODE else 100):      # a server cannot solve a captcha: report it, don't wait
            blocked = "/sorry/" in self.page.url
            if not blocked:      # judged from the page itself, not its text ("lalu lintas tidak biasa" is ordinary news)
                try:
                    blocked = bool(self.page.query_selector("form#captcha-form, #recaptcha, iframe[src*='recaptcha']"))
                except Exception:  # noqa: BLE001
                    blocked = False
            if not blocked:
                return True
            if SERVER_MODE:
                self.log("   ⚠ Google is asking for a captcha — a server cannot solve it; this search is skipped. "
                         "The Google News feed source usually still works.")
                return False
            if i == 0:
                self.log("   ⚠ Google is asking for a captcha — solve it in the browser window; waiting...")
            self._pause(3, 3)
        return False

    def _accept_consent(self):
        try:
            btn = self.page.query_selector("button:has-text('Accept all'), button:has-text('Terima semua'), button:has-text('Setuju')")
            if btn and "consent" in self.page.url:
                btn.click()
                self._pause(1.5, 2.5)
        except Exception:  # noqa: BLE001
            pass

    def search(self, kw: str, opt: NewsOptions, start_local: datetime, end_local: datetime) -> Result:
        """Collect from the chosen source(s) and merge (one row per article link)."""
        found: dict[str, dict] = {}
        notes, statuses = [], []
        try:
            for src in opt.sources:
                if opt.cap and len(found) >= opt.cap:
                    break
                if src == "feed":
                    res = self._search_feed(kw, opt, start_local, end_local, found)
                else:
                    res = self._search_google(kw, opt, start_local, end_local, found, tab=src)
                notes.append(f"{SOURCE_LABEL[src]}: {res.note}")
                statuses.append(res.status)
        except Stopped:
            raise Stopped(list(found.values())) from None
        except Exception as e:  # noqa: BLE001  keep what was found before the problem
            if BROWSER_CLOSED in str(e):
                raise Stopped(list(found.values())) from e
            notes.append("problem: " + str(e).splitlines()[0][:150])
            statuses.append("error")
        status = "ok" if "ok" in statuses or found else (statuses[0] if statuses else "ok")
        return Result(status, list(found.values()), " | ".join(notes))

    # ---------- Google News feed ----------
    def _search_feed(self, kw: str, opt: NewsOptions, start_local: datetime, end_local: datetime, found: dict) -> Result:
        """Google News RSS: up to 100 newest articles per search, exact publish times. One search per day for
        longer periods; links are then opened once to get the media's own URL."""
        days = (end_local.date() - start_local.date()).days + 1
        windows = [(end_local - timedelta(days=i), end_local - timedelta(days=i)) for i in range(days)] \
            if opt.by_day and days > 1 else [(start_local, end_local)]
        loc = {"hl": "id", "gl": "ID", "ceid": "ID:id"} if opt.region == "id" else {"hl": "en-US", "gl": "US", "ceid": "US:en"}
        self._gnews_loc = f"hl={loc['hl']}&gl={loc['gl']}&ceid={loc['ceid']}"
        self._goto(f"https://news.google.com/?{self._gnews_loc}")
        self._accept_consent()
        q = google_query(kw, opt)
        fresh = []
        seen_links = set()
        try:
            return self._feed_windows(q, opt, windows, loc, found, fresh, seen_links)
        except Stopped:
            for r in fresh:                          # keep what was collected (resolved or not)
                found.setdefault(_key(r["url"]), r)
            raise

    def _feed_windows(self, q, opt, windows, loc, found, fresh, seen_links) -> Result:
        read = outside = failed = 0
        for i, (a, b) in enumerate(windows):
            self._check_stop()
            if opt.cap and len(found) + len(fresh) >= opt.cap:
                break
            if i:
                self._pause(1.5, 3)
            # after:/before: are day-granular in Google's own time zone, so ask a little wider and filter exactly below
            qq = f"{q} after:{(a - timedelta(days=2)):%Y-%m-%d} before:{(b + timedelta(days=1)):%Y-%m-%d}"
            url = (f"https://news.google.com/rss/search?q={quote(qq)}&hl={loc['hl']}&gl={loc['gl']}&ceid={quote(loc['ceid'])}")
            xml = self.page.evaluate(_FETCH_JS, url) or ""
            if "<rss" not in xml[:1000] and "<channel" not in xml[:2000]:   # captcha/error page instead of a feed
                failed += 1
                continue
            for item in _parse_rss(xml):
                read += 1
                dt = item["dt"]
                if dt and not (self.start <= dt <= self.end):
                    outside += 1
                    continue
                if item["link"] in seen_links or _key(item["link"]) in found:
                    continue
                if opt.exclude and verdict(item["title"], "", opt.exclude, False) == "excluded":
                    continue                                   # Google ignored the -word for this one
                seen_links.add(item["link"])
                fresh.append({"url": item["link"], "google_url": item["link"], "title": item["title"], "media": item["source"],
                              "published": dt.astimezone(self.tz).strftime("%Y-%m-%d") if dt else "",
                              "ts": dt.timestamp() if dt else 0})
        fresh.sort(key=lambda r: -r["ts"])
        if opt.cap:
            del fresh[max(0, opt.cap - len(found)):]
        n_fresh = len(fresh)
        resolved = 0
        if opt.resolve or opt.detail == "full":      # full mode needs the media's link too, to open the article
            resolved = self._resolve_all(fresh)
        junk = 0
        for r in fresh:
            if opt.media_only and is_media(r["url"], opt.extra_media) is False and "news.google." not in r["url"]:
                junk += 1
                continue
            found.setdefault(_key(r["url"]), r)
        fresh.clear()
        note = f"query: {q}; {len(windows)} search{'es' if len(windows) != 1 else ''}; {read} articles read"
        if outside:
            note += f"; {outside} outside the period dropped"
        if (opt.resolve or opt.detail == "full") and n_fresh:
            note += f"; {resolved}/{n_fresh} media links resolved"
        if failed:
            note += f"; {failed} feed request(s) failed"
        if junk:
            note += f"; {junk} non-media links skipped"
        return Result("error" if failed == len(windows) else "ok", [], note)

    def _resolve_all(self, rows: list) -> int:
        """Turn Google News article links into the media's own links, without opening them:
        1. older links carry the article URL inside their id (decoded offline);
        2. newer ids ("AU_yqL...") are translated by Google News itself — the same request its own redirect page
           makes — sent from the open news.google.com tab, a few at a time;
        3. only if both fail, open the link briefly and catch where it goes."""
        done = 0
        todo = []
        for r in rows:
            real = decode_gnews_offline(r["google_url"])
            if real:
                r["url"] = real
                done += 1
            else:
                todo.append(r)
        loc = getattr(self, "_gnews_loc", "hl=id&gl=ID&ceid=ID:id")
        failures = []
        for creds in ("omit", "include"):          # anonymous first; then with the Google session (+ its token)
            if not todo:
                break
            ok_before = done
            for k in range(0, len(todo), 8):
                self._check_stop()
                batch = todo[k:k + 8]
                ids = [_gnews_id(r["google_url"]) for r in batch]
                try:
                    raw = self.page.evaluate(_GNEWS_DECODE_JS, [ids, loc, creds]) or {}
                except Exception as e:  # noqa: BLE001
                    if BROWSER_CLOSED in str(e):
                        raise
                    raw = {}
                    failures.append({"error": str(e)[:300], "creds": creds})
                for r, gid in zip(batch, ids):
                    info = raw.get(gid) or {}
                    real = _parse_batchexecute(info.get("body", "")) if gid else ""
                    if real:
                        r["url"] = real
                        done += 1
                        r["_ok"] = True
                    elif info:
                        failures.append(info)
                if k == 0 and done == ok_before and len(todo) > 8:
                    break                          # this way gives nothing at all now: don't spend time on the rest
                if k + 8 < len(todo):
                    self._pause(0.5, 1.0)
            todo = [r for r in todo if not r.pop("_ok", False)]
        if todo:
            why = _decode_failure_reason(failures)
            self.log(f"   ⚠ Google News: {len(todo)} links could not be decoded in the background ({why}) — "
                     "opening them instead, several at a time")
            self._save_decode_debug(failures)
            done += self._resolve_many(todo)
        return done

    def _resolve_many(self, rows: list, tabs: int = 4, wait_s: float = 12) -> int:
        """Last resort, still quick: open the links (in the /articles/ form, which forwards) in a few extra tabs at once
        and catch where each one goes — the media page itself is not loaded."""
        rx = re.compile(r"(^|\.)(google\.[a-z.]+|gstatic\.com|googleusercontent\.com|doubleclick\.net|"
                        r"googlesyndication\.com|googleadservices\.com|youtube\.com)$")
        pages, done = [], 0
        try:
            for _ in range(min(tabs, len(rows))):
                pg = self.ctx.new_page()
                slot = {"page": pg, "row": None, "t": 0.0}

                def make_handler(slot):              # (a 2-argument handler would get the request as 2nd argument)
                    def handler(route):
                        req = route.request
                        host = (urlparse(req.url).hostname or "").lower()
                        if req.is_navigation_request() and req.frame == slot["page"].main_frame and not rx.search(host):
                            slot.setdefault("target", req.url)
                            return route.abort()
                        return route.fallback()
                    return handler
                pg.route("**/*", make_handler(slot))
                pages.append(slot)
            queue = list(rows)
            total, last_log = len(rows), time.time()
            while queue or any(sl["row"] for sl in pages):
                self._check_stop()
                for sl in pages:
                    if sl["row"] is not None:
                        if "target" in sl:
                            sl["row"]["url"] = sl.pop("target")
                            done += 1
                            sl["row"] = None
                        elif time.time() - sl["t"] > wait_s:
                            sl["row"] = None                     # gave up on this one: keep the Google link
                    if sl["row"] is None and queue:
                        sl["row"], sl["t"] = queue.pop(0), time.time()
                        sl.pop("target", None)
                        try:                                    # start the navigation without waiting for it
                            sl["page"].evaluate("u => { location.href = u; }", gnews_open_url(sl["row"]["google_url"]))
                        except Exception as e:  # noqa: BLE001
                            if BROWSER_CLOSED in str(e):
                                raise
                if time.time() - last_log > 15:
                    last_log = time.time()
                    self.log(f"   opening Google News links: {total - len(queue)}/{total}")
                pages[0]["page"].wait_for_timeout(200)
        finally:
            for sl in pages:
                try:
                    sl["page"].close()
                except Exception:  # noqa: BLE001
                    pass
        return done

    def _save_decode_debug(self, failures):
        if not failures or not self.debug_dir or self.__dict__.get("_decode_debug_saved"):
            return
        self._decode_debug_saved = True
        try:
            import json as _json
            os.makedirs(self.debug_dir, exist_ok=True)
            name = f"google_news_decode_{datetime.now():%H%M%S}.json"
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as f:
                _json.dump(failures[:3], f, ensure_ascii=False, indent=1)
            self.log(f"   (details saved to outputs/debug/{name} — please send it in)")
        except OSError:
            pass

    def _resolve(self, gnews_url: str, wait_s: float = 10) -> str:
        """Open a news.google.com article link and catch where it sends the browser (the media's own URL),
        without loading the whole article page."""
        target = {}

        def handler(route):
            req = route.request
            host = (urlparse(req.url).hostname or "").lower()
            if (req.is_navigation_request() and req.frame == self.page.main_frame
                    and not re.search(r"(^|\.)(google\.[a-z.]+|gstatic\.com|googleusercontent\.com|doubleclick\.net|"
                                      r"googlesyndication\.com|googleadservices\.com|youtube\.com)$", host)):
                target.setdefault("url", req.url)
                return route.abort()
            return route.fallback()
        self.page.route("**/*", handler)
        try:
            try:
                self.page.goto(gnews_open_url(gnews_url), wait_until="commit", timeout=20000)
            except Exception as e:  # noqa: BLE001
                if BROWSER_CLOSED in str(e):
                    raise
            for _ in range(int(wait_s * 4)):          # the Google page redirects with a short delay
                if "url" in target:
                    break
                self._check_stop()
                self.page.wait_for_timeout(250)
        finally:
            try:
                self.page.unroute("**/*", handler)
            except Exception:  # noqa: BLE001
                pass
        return target.get("url", "")

    # ---------- Google Search, News tab ----------
    def _search_google(self, kw: str, opt: NewsOptions, start_local: datetime, end_local: datetime, found: dict,
                       tab: str = "news_tab") -> Result:
        """Google stops at ~300 results per search, so a longer period is searched day by day (newest first)
        when opt.by_day is on; results are merged and capped at opt.cap."""
        days = (end_local.date() - start_local.date()).days + 1
        windows = [(end_local - timedelta(days=i), end_local - timedelta(days=i)) for i in range(days)] \
            if opt.by_day and days > 1 else [(start_local, end_local)]
        notes, errors, cards = [], [], 0
        q = google_query(kw, opt)
        try:
            for i, (a, b) in enumerate(windows):
                if opt.cap and len(found) >= opt.cap:
                    break
                if len(windows) > 1:
                    self.log(f"   · {a:%d %b %Y} ({i + 1}/{len(windows)})")
                    if i:
                        self._pause(3, 6)        # a person moving on to the next day
                self._goto(google_news_url(q, a, b, opt.region, tab))
                self._accept_consent()
                res = self._search_pages(q, opt, found, tab)
                cards += res.cards
                if res.status == "error":
                    errors.append(res.note)
                    if "captcha" in res.note.lower():
                        break
                notes.append(res.note)
        except Stopped:
            raise
        if len(windows) == 1:
            return Result(("error" if errors else "ok"), list(found.values()), notes[0] if notes else "")
        note = f"query: {q}; searched day by day ({len(windows)} days); {cards} results read"
        if errors:
            note += f"; {len(errors)} day(s) with problems: " + errors[0].split("; ", 1)[-1]
        status = "error" if errors and not found else "ok"
        return Result(status, list(found.values()), note)

    def _search_pages(self, q: str, opt: NewsOptions, found: dict, tab: str = "news_tab") -> Result:
        pages = cards = outside_period = junk = excluded = 0
        snapshot = ""
        start_count = len(found)
        while pages < (60 if tab == "all_tab" else 40):     # until Google's last page
            self._check_stop()
            if not self._wait_google_captcha():
                return _with_cards(Result("error", list(found.values()), "Google captcha was not solved in time"), cards)
            pages += 1
            try:
                self.page.wait_for_selector("#rso a[href], #search a[href]", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            data = self.page.evaluate(_RESULTS_JS) or {}
            items = data.get("items") or []
            cards += len(items)
            if pages == 1 and not items and (data.get("outside") or 0) >= 3 and not self._no_results_page():
                snapshot = self._save_snapshot(q)          # links to other sites, yet no readable result
            for item in items:
                url = item["url"]
                if _key(url) in found:
                    continue
                medium = is_media(url, opt.extra_media)
                if (tab == "all_tab" and medium is not True) or (opt.media_only and medium is False):
                    junk += 1                                 # not an article from a news medium
                    continue
                lines = [ln for ln in item.get("lines", []) if ln != item["title"]]
                if opt.exclude and verdict(item["title"] + " " + " ".join(lines), "", opt.exclude, False) == "excluded":
                    excluded += 1                             # Google ignored the -word for this one
                    continue
                date_txt = next((ln for ln in reversed(lines) if parse_news_date(ln, datetime.now(self.tz))), "")
                dt = parse_news_date(date_txt, datetime.now(self.tz)) if date_txt else None
                if dt and not self._in_period(dt, date_txt):
                    outside_period += 1
                    continue                                  # outside the period (Google's filter is approximate)
                media = next((ln for ln in lines if ln != date_txt and len(ln) < 60 and not ln.lower().startswith("http")
                              and "›" not in ln), "") or urlparse(url).netloc.removeprefix("www.")
                found[_key(url)] = {"url": url, "title": item["title"], "media": media,
                              "published": dt.astimezone(self.tz).strftime("%Y-%m-%d") if dt else "", "ts": dt.timestamp() if dt else 0}
                if opt.cap and len(found) >= opt.cap:
                    break
            if opt.cap and len(found) >= opt.cap:
                break
            nxt = self.page.query_selector("a#pnnext, a[aria-label='Next page'], a[aria-label='Halaman berikutnya']")
            if not nxt:
                break
            self._pause(2.5, 5.5)                             # read the page like a person before moving on
            try:
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
                    nxt.click()
            except Exception as e:  # noqa: BLE001
                if "closed" in str(e):
                    raise
                break
            self._pause(1.5, 3)
        note = f"query: {q}; {pages} result page{'s' if pages != 1 else ''}; {cards} results read"
        if outside_period:
            note += f"; {outside_period} outside the period dropped"
        if junk:
            note += f"; {junk} non-media results skipped"
        if excluded:
            note += f"; {excluded} with excluded words dropped"
        if len(found) == start_count and ("/sorry/" in self.page.url):
            return _with_cards(Result("error", [], "Google captcha"), cards)
        if not cards:
            hint = ("Google showed no news results for this query and period" if self._no_results_page() or not snapshot
                    else "Google's page could not be read (its layout may have changed)")
            if snapshot:
                hint += f" — a copy of the page was saved to outputs/debug/{snapshot}; please send it to the developer"
            return _with_cards(Result("error" if snapshot and "layout" in hint else "ok", [], note + "; " + hint), cards)
        return _with_cards(Result("ok", list(found.values()), note), cards)

    def _in_period(self, dt: datetime, date_txt: str) -> bool:
        """Absolute card dates ("19 Okt 2026") are whole days: compare the local date exactly.
        Relative ones ("3 hari lalu" = 3 to 4 days ago) get one day of slack on the older side only."""
        if re.search(r"lalu|ago", date_txt, re.I):
            return self.start - timedelta(days=1) <= dt <= self.end + timedelta(hours=1)
        d = dt.astimezone(self.tz).date()
        return self.start.astimezone(self.tz).date() <= d <= self.end.astimezone(self.tz).date()

    def _no_results_page(self) -> bool:
        t = self._body_text()
        return any(x in t for x in ("did not match any", "tidak cocok dengan", "no results found", "tidak ada hasil"))

    def _save_snapshot(self, q: str) -> str:
        """Keep the HTML of a results page we could not read, so the reader can be fixed."""
        if not self.debug_dir:
            return ""
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            name = "google_" + re.sub(r"[^\w-]+", "_", q)[:40] + f"_{datetime.now():%H%M%S}.html"
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as f:
                f.write(f"<!-- {self.page.url} -->\n" + self.page.content())
            return name
        except Exception:  # noqa: BLE001
            return ""

    def article(self, url: str) -> dict:
        self._check_stop()
        try:                                        # a slow media site must not hold the run: 25 s, then read what loaded
            self.page.goto(gnews_open_url(url), wait_until="domcontentloaded", timeout=25000)
        except Exception as e:  # noqa: BLE001
            if "Timeout" not in str(e):
                raise
        self._pause(0.8, 1.6)
        if "news.google." in self.page.url:          # Google News link: wait for it to send us on to the media
            try:
                self.page.wait_for_url(lambda u: "news.google." not in u and "consent.google." not in u, timeout=12000)
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
                self._pause(1, 2)
            except Exception as e:  # noqa: BLE001
                if BROWSER_CLOSED in str(e):
                    raise
        try:
            a = self.page.evaluate(_ARTICLE_JS) or {}
        except Exception as e:  # noqa: BLE001
            if "closed" in str(e):
                raise
            a = {}
        pub = _parse_iso(str(a.get("published", "")))
        return {"title": (a.get("title") or "").strip(), "site": (a.get("site") or "").strip(),
                "published_full": pub.astimezone(self.tz).strftime("%Y-%m-%d %H:%M") if pub else "",
                "author": (a.get("author") or "").strip()[:200], "text": (a.get("text") or "").strip()[:_MAX_TEXT],
                "final_url": self.page.url}
