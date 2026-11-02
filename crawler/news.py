"""News links from Google (News tab), the way a person does it: open Google, search the keyword with a
custom date range, read the result cards, click "Next" page after page. Optionally open each article
and copy its title, date, author and text. No API.

Google shows a captcha when it sees too many searches too fast; the run waits so you can solve it in
the browser window."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from .browser import BrowserCrawler, Result, Stopped, _parse_iso

DETAIL_LEVELS = ("links", "basic", "full")
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
    cap: int = 100                 # max links per keyword (Google itself stops around 300)
    region: str = "id"             # "id" = Indonesia/Indonesian, "any" = global English
    exact: bool = False
    exclude: list[str] = field(default_factory=list)
    sites: list[str] = field(default_factory=list)
    detail: str = "basic"          # links | basic (title, media, date) | full (+ article text)

    @classmethod
    def from_dict(cls, d: dict) -> "NewsOptions":
        d = d or {}
        split = lambda v: [w.strip() for w in (re.split(r"[,\n]+", v) if isinstance(v, str) else (v or [])) if w.strip()]
        sites = []
        for s in split(d.get("sites")):
            host = urlparse(s if "://" in s else "https://" + s).netloc.lower().removeprefix("www.")
            if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
                sites.append(host)
        detail = d.get("detail") if d.get("detail") in DETAIL_LEVELS else "basic"
        return cls(cap=max(0, int(d.get("cap") or 0)) or 100, region="any" if d.get("region") == "any" else "id",
                   exact=bool(d.get("exact")), exclude=[w.lstrip("-") for w in split(d.get("exclude"))],
                   sites=sites[:20], detail=detail)


def google_query(kw: str, opt: NewsOptions) -> str:
    kw = kw.strip()
    parts = [f'"{kw}"' if opt.exact and " " in kw and not kw.startswith('"') else kw]
    parts += [f"-{w}" for w in opt.exclude]
    if opt.sites:
        parts.append("(" + " OR ".join(f"site:{s}" for s in opt.sites) + ")")
    return " ".join(parts)


def google_news_url(q: str, start_local: datetime, end_local: datetime, region: str) -> str:
    rng = f"cdr:1,cd_min:{start_local:%m/%d/%Y},cd_max:{end_local:%m/%d/%Y},sbd:1"
    loc = "&hl=id&gl=ID" if region == "id" else "&hl=en&gl=US"
    return f"https://www.google.com/search?q={quote(q)}&tbm=nws&tbs={quote(rng, safe=':,')}{loc}"


def parse_news_date(text: str, now: datetime) -> datetime | None:
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


class NewsCrawler(BrowserCrawler):
    def __init__(self, *a, tz=timezone.utc, debug_dir: str | None = None, **kw):
        super().__init__(*a, **kw)
        self.tz = tz
        self.debug_dir = debug_dir      # where to keep a copy of a results page that could not be read

    def _wait_google_captcha(self) -> bool:
        """True when the page is usable; waits up to ~5 minutes for the user to solve a captcha."""
        for i in range(100):
            url = self.page.url
            txt = self._body_text()
            blocked = "/sorry/" in url or "unusual traffic" in txt or "traffic yang tidak biasa" in txt or "lalu lintas" in txt and "tidak biasa" in txt
            if not blocked:
                return True
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
        q = google_query(kw, opt)
        self._goto(google_news_url(q, start_local, end_local, opt.region))
        self._accept_consent()
        found: dict[str, dict] = {}
        try:
            return self._search_pages(q, opt, found)
        except Stopped:
            raise Stopped(list(found.values())) from None

    def _search_pages(self, q: str, opt: NewsOptions, found: dict) -> Result:
        pages = cards = outside_period = 0
        snapshot = ""
        while pages < 40:
            self._check_stop()
            if not self._wait_google_captcha():
                return Result("error", list(found.values()), "Google captcha was not solved in time")
            pages += 1
            try:
                self.page.wait_for_selector("#rso a[href], #search a[href]", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            data = self.page.evaluate(_RESULTS_JS) or {}
            items = data.get("items") or []
            cards += len(items)
            if pages == 1 and not items:
                snapshot = self._save_snapshot(q)
            for item in items:
                url = item["url"]
                if url in found:
                    continue
                lines = [ln for ln in item.get("lines", []) if ln != item["title"]]
                date_txt = next((ln for ln in reversed(lines) if parse_news_date(ln, datetime.now(self.tz))), "")
                dt = parse_news_date(date_txt, datetime.now(self.tz)) if date_txt else None
                if dt and not (self.start - timedelta(days=1) <= dt <= self.end + timedelta(days=1)):
                    outside_period += 1
                    continue                                  # outside the period (Google's filter is approximate)
                media = next((ln for ln in lines if ln != date_txt and len(ln) < 60), "") or urlparse(url).netloc.removeprefix("www.")
                found[url] = {"url": url, "title": item["title"], "media": media,
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
                nxt.click()
                self.page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception as e:  # noqa: BLE001
                if "closed" in str(e):
                    raise
                break
            self._pause(1.5, 3)
        note = f"query: {q}; {pages} result page{'s' if pages != 1 else ''}; {cards} results read"
        if outside_period:
            note += f"; {outside_period} outside the period dropped"
        if not found and ("/sorry/" in self.page.url):
            return Result("error", [], "Google captcha")
        if not cards:
            hint = ("Google showed no news results for this query and period" if "did not match" in self._body_text()
                    or "tidak cocok" in self._body_text() else
                    "Google's page could not be read (its layout may have changed)")
            if snapshot:
                hint += f" — a copy of the page was saved to outputs/debug/{snapshot}; please send it to the developer"
            return Result("error" if snapshot and "layout" in hint else "ok", [], note + "; " + hint)
        return Result("ok", list(found.values()), note)

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
        self._goto(url)
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
