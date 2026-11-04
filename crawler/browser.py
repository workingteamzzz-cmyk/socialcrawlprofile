"""Collect post links from profile pages with a real (logged-in) browser via Playwright.

No platform API is used: the browser opens the profile, scrolls, and reads the post links
that are on the page. Dates come from the post ID itself (see idtime.py) or, for Facebook,
from the data the page loads while scrolling.
"""
from __future__ import annotations

import json
import os
import sys
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote, urlparse

from .accounts import Account
from .idtime import instagram_shortcode_time, tiktok_id_time, x_status_time

LOGIN_PAGES = [
    "https://www.instagram.com/accounts/login/",
    "https://x.com/login",
    "https://www.tiktok.com/login",
    "https://www.threads.com/login",
    "https://www.facebook.com/login",
]

NOT_FOUND_TEXTS = [
    "sorry, this page isn't available", "maaf, halaman ini tidak tersedia",
    "profile isn't available", "profil tidak tersedia", "profil ini tidak tersedia",
    "this account doesn’t exist", "this account doesn't exist", "akun ini tidak ada",
    "account suspended", "akun ditangguhkan",
    "couldn't find this account", "couldn’t find this account", "tidak dapat menemukan akun ini",
    "this content isn't available", "this content isn’t available", "konten ini tidak tersedia",
    "this page isn't available", "this page isn’t available", "halaman ini tidak tersedia",
]
CAPTCHA_TEXTS = ["drag the slider", "geser penggeser", "verify to continue", "verifikasi untuk melanjutkan",
                 "select 2 objects", "pilih 2 objek"]


@dataclass
class Post:
    url: str
    time_utc: datetime | None
    author: str = ""
    details: dict | None = None      # caption / engagement, when requested
    text: str = ""                   # what the result shows (post text, title) — used to drop excluded/off-topic posts


@dataclass
class Result:
    status: str          # ok | not_found | login_required | error | invalid | stopped
    posts: list[Post]
    note: str = ""


class Stopped(Exception):
    """Raised when the user presses Stop. .posts carries what was collected for the current item."""
    def __init__(self, posts=None):
        super().__init__("stopped")
        self.posts = posts or []


BROWSER_CLOSED = "has been closed"   # Playwright error text when the user closes the browser window


def launch_context(pw, profile_dir: str, headless: bool = False):
    os.makedirs(profile_dir, exist_ok=True)
    # Stale Chrome lock files left by a crash would make the profile look "in use". Only this app uses this folder,
    # and only one job runs at a time, so it is safe to clear them.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_dir, name)
        if os.path.lexists(path):
            try:
                os.remove(path)
            except OSError:
                pass
    opts = dict(
        user_data_dir=profile_dir,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        locale="id-ID",
        # AutomationControlled off: without it TikTok (and others) spot the automated browser and show nothing.
        # Real Chrome shows a harmless "unsupported command-line flag" bar for it — just close the bar.
        # No script tweaks on top: the flag already makes navigator.webdriver false natively, while a patched getter
        # is itself a giveaway (X then shows an empty or "Something went wrong" timeline).
        args=["--disable-blink-features=AutomationControlled", "--disable-features=Translate,TranslateUI"],
        ignore_default_args=["--enable-automation"],
        # Playwright turns Chrome's sandbox off by default, which shows a "--no-sandbox" warning bar; Mac and
        # Windows run fine with it on (Linux as root does not).
        chromium_sandbox=sys.platform in ("darwin", "win32"),
    )
    exe = os.environ.get("CRAWLER_BROWSER_PATH")
    if exe:
        return pw.chromium.launch_persistent_context(executable_path=exe, **opts)
    errors = []
    # Prefer the installed Google Chrome / Edge (less often blocked), fall back to Playwright's Chromium.
    # Always reopen a profile with the browser that created it: Chrome, Edge and Chromium encrypt saved logins
    # differently, so switching between them makes the platforms ask for a login again.
    marker = os.path.join(profile_dir, ".collector_browser")
    try:
        with open(marker, encoding="utf-8") as f:
            used = f.read().strip()
    except OSError:
        used = ""
    order = ["chrome", "msedge", "chromium"]
    if used in order:
        order.remove(used)
        order.insert(0, used)
    for channel in order:
        try:
            if channel != "chromium":
                ctx = pw.chromium.launch_persistent_context(channel=channel, **opts)
            else:
                ctx = pw.chromium.launch_persistent_context(**opts)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel}: {str(e).splitlines()[0]}")
            continue
        if channel != used:
            try:
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(channel)
            except OSError:
                pass
        return ctx
    raise RuntimeError("Could not open a browser. Install Google Chrome (or Microsoft Edge), or start the app again "
                       "with internet access so it can download Chromium. Details: " + " | ".join(errors))


# Cookies that exist only while signed in.
LOGIN_COOKIES = {"instagram": ("instagram.com", "sessionid"), "x": ("x.com", "auth_token"),
                 "tiktok": ("tiktok.com", "sessionid"), "threads": ("threads.", "sessionid"),
                 "facebook": ("facebook.com", "c_user")}


def login_status(ctx) -> dict:
    """{platform: True/False} from the browser profile's cookies (YouTube needs no login)."""
    try:
        cookies = ctx.cookies()
    except Exception:  # noqa: BLE001
        return {}
    now = time.time()
    out = {}
    for plat, (domain, name) in LOGIN_COOKIES.items():
        out[plat] = any(c.get("name") == name and domain in (c.get("domain") or "") and c.get("value")
                        and (c.get("expires", -1) in (-1, None) or c.get("expires", 0) > now) for c in cookies)
    return out


class BrowserCrawler:
    def __init__(self, context, log: Callable[[str], None], stop_flag, start_utc: datetime, end_utc: datetime):
        self.ctx = context
        self.log = log
        self.stop_flag = stop_flag
        self.start = start_utc
        self.end = end_utc
        self.page = context.pages[0] if context.pages else context.new_page()
        self.debug_dir: str | None = None     # set by the job: where to keep a copy of a page that gave no links

    def _snapshot(self, tag: str) -> str:
        """Save the current page's HTML for diagnosis (returns the file name, or "")."""
        if not self.debug_dir:
            return ""
        try:
            os.makedirs(self.debug_dir, exist_ok=True)
            name = re.sub(r"[^\w-]+", "_", tag)[:60] + f"_{datetime.now():%H%M%S}.html"
            with open(os.path.join(self.debug_dir, name), "w", encoding="utf-8") as f:
                f.write(f"<!-- {self.page.url} -->\n" + self.page.content())
            return name
        except Exception:  # noqa: BLE001
            return ""

    def _empty_note(self, base: str, tag: str) -> str:
        name = self._snapshot(tag)
        return base + (f" — a copy of the page was saved to outputs/debug/{name} (check it, then send it in if this "
                       "account does have posts)" if name else "")

    # ---------- helpers ----------
    def _check_stop(self):
        if self.stop_flag.is_set():
            raise Stopped()

    def _pause(self, lo=1.2, hi=2.6):
        end = time.time() + random.uniform(lo, hi)
        while time.time() < end:
            self._check_stop()
            self.page.wait_for_timeout(250)

    def _goto(self, url: str):
        self._check_stop()
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:  # noqa: BLE001
            if "Timeout" not in str(e):
                raise
        self._pause(2.5, 4.0)

    def _body_text(self) -> str:
        try:
            return (self.page.inner_text("body", timeout=5000) or "")[:4000].lower()
        except Exception:  # noqa: BLE001
            return ""

    def _wait_captcha(self):
        for i in range(60):  # up to ~3 minutes
            txt = self._body_text()
            if not any(t in txt for t in CAPTCHA_TEXTS):
                return
            if i == 0:
                self.log("   ⚠ Captcha/verification shown — solve it in the browser window; waiting...")
            self._pause(3, 3)

    def _page_state(self, login_markers: tuple[str, ...]) -> str:
        url = self.page.url.lower()
        if any(m in url for m in login_markers):
            return "login_required"
        txt = self._body_text()
        if any(t in txt for t in NOT_FOUND_TEXTS):
            return "not_found"
        return "ok"

    def _hrefs(self) -> list[str]:
        try:
            return self.page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
        except Exception:  # noqa: BLE001
            return []

    def _scroll(self):
        try:
            self.page.mouse.wheel(0, random.randint(2200, 3200))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.evaluate("window.scrollBy(0, Math.max(800, window.innerHeight))")
        except Exception:  # noqa: BLE001
            pass

    def _scroll_collect(self, extract: Callable[[], list[Post]], older_needed: int = 4,
                        idle_limit: int = 5, max_rounds: int = 400, cap: int = 0, keep=None) -> list[Post]:
        """Scroll until enough posts older than the start date are seen (newest-first feeds),
        or nothing new loads. older_needed > 1 so pinned old posts at the top don't stop it early;
        older_needed=0 for feeds that are not in date order. cap>0 stops after that many in-range posts."""
        found: dict[str, Post] = {}
        try:
            return self._scroll_loop(found, extract, older_needed, idle_limit, max_rounds, cap, keep)
        except Stopped as e:
            raise Stopped(list(found.values()) or e.posts) from None

    def _scroll_loop(self, found, extract, older_needed, idle_limit, max_rounds, cap, keep=None):
        idle = 0
        for _ in range(max_rounds):
            self._check_stop()
            before = len(found)
            for p in extract():
                old = found.get(p.url)
                if old is None or (old.time_utc is None and p.time_utc) or len(p.text) > len(old.text):
                    if old is not None and p.time_utc is None:
                        p.time_utc = old.time_utc
                    found[p.url] = p
            older = sum(1 for p in found.values() if p.time_utc and p.time_utc < self.start)
            if older_needed and older >= older_needed:
                break
            if cap and sum(1 for p in found.values() if p.time_utc and self.start <= p.time_utc <= self.end
                           and (keep is None or keep(p))) >= cap:      # count only posts that will be kept
                break
            idle = idle + 1 if len(found) == before else 0
            if idle >= idle_limit:
                break
            self._scroll()
            self._pause()
        return list(found.values())

    def _in_range(self, posts: list[Post]) -> list[Post]:
        return [p for p in posts if p.time_utc and self.start <= p.time_utc <= self.end]

    def _finish(self, posts: list[Post], login_markers: tuple[str, ...]) -> Result:
        if not posts:
            state = self._page_state(login_markers)
            if state == "login_required":
                return Result("login_required", [], "Login required — click 'Log in to platforms' and sign in to this platform")
            if state == "not_found":
                return Result("not_found", [], "Account not found / unavailable")
            return Result("ok", [], self._empty_note("No posts could be read from the profile",
                                                     urlparse(self.page.url).netloc + urlparse(self.page.url).path))
        notes = []
        undated = sum(1 for p in posts if p.time_utc is None)
        if undated:
            notes.append(f"{undated} links without a date were skipped")
        if any(m in self.page.url.lower() for m in login_markers):
            notes.append("a login wall appeared — results may be incomplete; use 'Log in to platforms'")
        return Result("ok", self._in_range(posts), "; ".join(notes))

    # ---------- platforms ----------
    def instagram(self, acc: Account) -> Result:
        self._goto(acc.url)
        rx = re.compile(r"/(p|reel|tv)/([A-Za-z0-9_-]{5,})")

        def extract():
            out = []
            for h in self._hrefs():
                m = rx.search(h or "")
                if m:
                    kind = "reel" if m.group(1) == "reel" else "p"
                    out.append(Post(f"https://www.instagram.com/{kind}/{m.group(2)}/",
                                    instagram_shortcode_time(m.group(2))))
            return out

        posts = self._scroll_collect(extract)
        return self._finish(posts, ("accounts/login", "/challenge"))

    def threads(self, acc: Account) -> Result:
        self._goto(acc.url)
        rx = re.compile(r"^/@([^/]+)/post/([A-Za-z0-9_-]+)/?$")
        handle = acc.handle.lower()

        def extract():
            try:
                pairs = self.page.eval_on_selector_all(
                    "a[href*='/post/']",
                    "els => els.map(e => [e.getAttribute('href'), (e.querySelector('time') || {}).dateTime || ''])")
            except Exception:  # noqa: BLE001
                pairs = []
            out = []
            for href, dt in pairs:
                m = rx.match(href or "")
                if not m or m.group(1).lower() != handle:
                    continue
                t = None
                if dt:
                    try:
                        t = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    except ValueError:
                        t = None
                out.append(Post(f"https://www.threads.com/@{m.group(1)}/post/{m.group(2)}",
                                t or instagram_shortcode_time(m.group(2))))
            return out

        posts = self._scroll_collect(extract)
        return self._finish(posts, ("/login",))

    def tiktok(self, acc: Account) -> Result:
        self._goto(acc.url)
        self._wait_captcha()
        rx = re.compile(r"/@([^/?#]+)/(video|photo)/(\d+)")
        handle = acc.handle.lower()

        def extract():
            self._wait_captcha()
            out = []
            for h in self._hrefs():
                m = rx.search(h or "")
                if m and m.group(1).lower() == handle:
                    out.append(Post(f"https://www.tiktok.com/@{m.group(1)}/{m.group(2)}/{m.group(3)}",
                                    tiktok_id_time(m.group(3))))
            return out

        posts = self._scroll_collect(extract)
        return self._finish(posts, ("/login",))

    def x(self, acc: Account) -> Result:
        # X's own search narrows to the period: from:user since:YYYY-MM-DD until:YYYY-MM-DD (until is exclusive).
        since = self.start.strftime("%Y-%m-%d")
        until = (self.end + timedelta(days=1)).strftime("%Y-%m-%d")
        q = f"from:{acc.handle} since:{since} until:{until}"
        self._goto(f"https://x.com/search?q={quote(q)}&src=typed_query&f=live")
        self._x_wait()
        rx = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)")
        handle = acc.handle.lower()

        def extract():
            out = []
            for h, _txt in self._x_rows():      # each tweet's own permalink; quoted tweets are not taken
                m = rx.match(h or "")
                if m and m.group(1).lower() == handle:
                    out.append(Post(f"https://x.com/{m.group(1)}/status/{m.group(2)}", x_status_time(m.group(2))))
            return out

        posts = self._scroll_collect(extract, older_needed=1, idle_limit=4)
        if not posts:
            # Search shows nothing for "no posts in period", "account missing" and when X's search is having a bad
            # day: read the profile itself (newest first, scroll until older posts show up).
            self._goto(acc.url)
            self._x_wait()
            state = self._page_state(("/login", "/i/flow/login"))
            if state == "not_found":
                return Result("not_found", [], "Account not found / suspended")
            if state == "login_required":
                return Result("login_required", [], "Login to X required")
            posts = self._scroll_collect(extract, older_needed=3, idle_limit=4, max_rounds=150)
            if not posts:
                why = self._x_reason()
                if why:
                    self.log(f"   ⚠ X: {why}")
                    return Result("login_required" if "log in" in why.lower() else "ok", [],
                                  self._empty_note(why, "x_" + acc.handle))
                return self._finish(posts, ("/login", "/i/flow/login"))
            res = self._finish(posts, ("/login", "/i/flow/login"))
            res.note = "; ".join(n for n in ("read from the profile (X search showed nothing)", res.note) if n)
            return res
        return self._finish(posts, ("/login", "/i/flow/login"))

    # Every tweet on the page as [own permalink, text] — for X's older layout (one <article> per tweet) and its newer
    # one: there the tweets are the blocks directly under the lowest element that holds all of the timeline's status
    # links. A tweet's own permalink is the one around its timestamp, or else its first one; quoted tweets inside
    # a tweet are not taken. The sidebar (trends, "who to follow") is ignored.
    _X_JS = r"""() => {
      const rx = /^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)/;
      const ok = a => rx.test(a.getAttribute('href') || '') && !a.closest('aside, [data-testid="sidebarColumn"]');
      const links = [...document.querySelectorAll("a[href*='/status/']")].filter(ok);
      if (!links.length) return [];
      let cards = [...new Set(links.map(a => a.closest('article, [data-testid="tweet"]')).filter(Boolean))];
      cards = cards.filter(c => !cards.some(o => o !== c && o.contains(c)));
      const loose = links.filter(a => !a.closest('article, [data-testid="tweet"]'));
      if (loose.length) {
        let lca = loose[0].parentElement;
        while (lca && !loose.every(l => lca.contains(l))) lca = lca.parentElement;
        const ids = new Set(loose.map(a => a.getAttribute('href').match(rx)[2]));
        const kids = lca ? [...lca.children].filter(ch => ch.matches("a[href*='/status/']") || ch.querySelector("a[href*='/status/']")) : [];
        if (ids.size > 1 && kids.length > 1) cards.push(...kids.filter(k => !cards.some(c => c.contains(k) || k.contains(c))));
        else if (lca) cards.push(lca.closest('[data-testid="cellInnerDiv"]') || lca);
      }
      return cards.map(c => {
        const ls = [...(c.matches("a[href*='/status/']") ? [c] : []), ...c.querySelectorAll("a[href*='/status/']")].filter(ok);
        if (!ls.length) return null;
        const own = ls.find(l => l.querySelector('time')) || ls[0];
        const t = c.querySelector('[data-testid="tweetText"]');
        return [own.getAttribute('href'), (t ? t.innerText : c.innerText) || ''];
      }).filter(Boolean);
    }"""

    def _x_rows(self) -> list:
        try:
            return self.page.evaluate(self._X_JS) or []
        except Exception:  # noqa: BLE001
            return []

    def _x_reason(self) -> str:
        """Why X showed no tweets, in plain words ("" when the page looks normal)."""
        url = self.page.url.lower()
        txt = self._body_text()
        if "/account/access" in url or "account is locked" in txt or "akun anda dikunci" in txt:
            return ("X has locked/limited your account for a check — open x.com in the app's browser window, "
                    "finish X's check, then run again")
        if any(m in url for m in ("/login", "/i/flow/login")) or ("sign in to x" in txt) or ("masuk ke x" in txt):
            return "X is asking to log in — use 'Log in to platforms' and sign in to X again"
        if "rate limit" in txt or "batas" in txt and "tercapai" in txt:
            return "X says 'rate limited' (too many views in a short time) — wait 15–30 minutes and run again"
        if "something went wrong" in txt or "terjadi kesalahan" in txt:
            return ("X keeps showing 'Something went wrong' — X is refusing this browser for now; "
                    "wait a while, or open x.com in the app's window and scroll a bit by hand, then run again")
        if not txt.strip():
            return "X's page stayed blank (it did not load) — check the internet connection / open x.com in the window"
        return ""

    def _x_wait(self, timeout_s: float = 15):
        """Wait until X has drawn its tweets (it loads them after the page); press X's own "Retry" when it shows
        "Something went wrong" — up to three times, like a person would."""
        end = time.time() + timeout_s
        retries = 0
        while time.time() < end:
            self._check_stop()
            try:
                st = self.page.evaluate("""() => {
                    if (document.querySelector('article') || [...document.querySelectorAll("a[href*='/status/']")]
                        .some(a => /^\\/[A-Za-z0-9_]{1,15}\\/status\\/\\d+/.test(a.getAttribute('href') || '')
                                   && !a.closest('aside, [data-testid="sidebarColumn"]'))) return 'ok';
                    const b = [...document.querySelectorAll('[role=button], button')].find(e =>
                        /^(retry|coba lagi|ulangi|muat ulang)$/i.test((e.innerText || '').trim()));
                    if (b) { b.click(); return 'retry'; }
                    const t = document.body ? document.body.innerText : '';
                    return /no results for|tidak ada hasil|hasil tidak ditemukan/i.test(t) ? 'empty' : 'wait'; }""")
            except Exception:  # noqa: BLE001  (page still navigating)
                st = "wait"
            if st in ("ok", "empty"):
                return
            if st == "retry":
                retries += 1
                self.log("  X showed 'Something went wrong' — pressing Retry")
                if retries >= 3:
                    return
                self._pause(2, 4)
                continue
            self.page.wait_for_timeout(500)

    # ---------- YouTube ----------
    # Video IDs carry no date, so each new video's publish date is read from its watch page with a
    # same-origin fetch from the open YouTube tab (4 at a time). No yt-dlp, no API.
    _YT_JS = r"""async (ids) => {
      const out = {};
      const one = async (id) => {
        const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 20000);   // never hang
        try {
          const r = await fetch('/watch?v=' + id, {credentials: 'include', signal: ctl.signal});
          if (!r.ok) { out[id] = {failed: true}; return; }
          const h = await r.text();
          const m = h.match(/"publishDate":"([^"]+)"/) || h.match(/"uploadDate":"([^"]+)"/)
                 || h.match(/itemprop="datePublished" content="([^"]+)"/);
          const a = h.match(/"ownerProfileUrl":"https?:\/\/www\.youtube\.com\/@([^"\/]+)"/)
                 || h.match(/"author":"([^"]+)"/);
          out[id] = {date: m ? m[1] : '', author: a ? a[1] : '', upcoming: /"isUpcoming":true/.test(h)};
        } catch (e) { out[id] = {failed: true}; } finally { clearTimeout(t); }
      };
      for (let i = 0; i < ids.length; i += 4) await Promise.all(ids.slice(i, i + 4).map(one));
      return out;
    }"""

    def _yt_dates(self, ids: list[str]) -> dict:
        cache = self.__dict__.setdefault("_yt_cache", {})
        todo = [i for i in ids if i not in cache]
        for k in range(0, len(todo), 12):
            self._check_stop()
            try:
                got = self.page.evaluate(self._YT_JS, todo[k:k + 12])
            except Exception as e:  # noqa: BLE001
                if BROWSER_CLOSED in str(e):
                    raise
                got = {}
            for vid in todo[k:k + 12]:
                info = got.get(vid) or {"failed": True}
                entry = (None if info.get("upcoming") else _parse_iso(info.get("date", "")), info.get("author", ""))
                if info.get("failed"):
                    self.__dict__.setdefault("_yt_fail", {})[vid] = entry    # retried on the next scroll round
                else:
                    cache[vid] = entry
        fails = self.__dict__.get("_yt_fail", {})
        return {i: cache.get(i) or fails.pop(i, (None, "")) for i in ids}

    def _yt_collect(self, selector: str, older_needed: int, cap: int = 0, max_rounds: int = 200, keep=None) -> list[Post]:
        rx = re.compile(r"(?:/watch\?v=|/shorts/)([A-Za-z0-9_-]{11})")

        def extract():
            try:
                pairs = self.page.eval_on_selector_all(selector, """els => els.map(e => [e.getAttribute('href'),
                    ((e.closest('ytd-video-renderer, ytd-rich-item-renderer, ytd-grid-video-renderer') || {}).innerText || '')])""")
            except Exception:  # noqa: BLE001
                pairs = []
            if not pairs:                       # layout changed: fall back to every link on the page
                pairs = [[h, ""] for h in self._hrefs()]
            order, kind, texts = [], {}, {}
            for h, txt in pairs:
                m = rx.search(h or "")
                if m and m.group(1) not in kind:
                    order.append(m.group(1))
                    kind[m.group(1)] = "shorts" if "/shorts/" in h else "watch"
                if m and txt and len(txt) > len(texts.get(m.group(1), "")):
                    texts[m.group(1)] = txt[:2000]
            dates = self._yt_dates(order)
            return [Post(f"https://www.youtube.com/shorts/{v}" if kind[v] == "shorts" else f"https://www.youtube.com/watch?v={v}",
                         dates[v][0], dates[v][1], text=texts.get(v, "")) for v in order]

        return self._scroll_collect(extract, older_needed=older_needed, idle_limit=4, max_rounds=max_rounds, cap=cap, keep=keep)

    _YT_CHANNEL_JS = r"""() => {
      const m = (document.querySelector('meta[itemprop="identifier"]') || {}).content
             || ((document.querySelector('link[rel="canonical"]') || {}).href || '').split('/channel/')[1]
             || (document.documentElement.innerHTML.match(/"(?:externalId|browseId|channelId)":"(UC[\w-]{22})"/) || [])[1];
      return m || '';
    }"""

    def _yt_feed(self) -> dict:
        """YouTube's own channel feed: the ~15 newest uploads with exact publish times. Reliable even when the
        channel page layout changes. {video_id: Post}"""
        try:
            cid = self.page.evaluate(self._YT_CHANNEL_JS) or ""
            if not re.fullmatch(r"UC[\w-]{22}", cid):
                return {}
            xml = self.page.evaluate(_FETCH_TEXT_JS, f"/feeds/videos.xml?channel_id={cid}") or ""
        except Exception as e:  # noqa: BLE001
            if BROWSER_CLOSED in str(e):
                raise
            return {}
        import xml.etree.ElementTree as ET
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError:
            return {}
        out = {}
        for e in root.findall("a:entry", ns):
            vid = (e.findtext("yt:videoId", "", ns) or "").strip()
            dt = _parse_iso(e.findtext("a:published", "", ns) or "")
            link = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "") or ""
            if not re.fullmatch(r"[\w-]{11}", vid):
                continue
            url = f"https://www.youtube.com/shorts/{vid}" if "/shorts/" in link else f"https://www.youtube.com/watch?v={vid}"
            author = (e.findtext("a:author/a:name", "", ns) or "").strip()
            out[vid] = Post(url, dt, author)
        return out

    def youtube(self, acc: Account) -> Result:
        base = acc.url.rstrip("/")
        found: dict[str, Post] = {}            # by video id
        tabs_seen = 0
        feed = {}
        for tab in ("videos", "shorts", "streams"):
            self._goto(f"{base}/{tab}")
            if tabs_seen == 0 and not feed and self._page_state(("consent.youtube.com",)) == "not_found":
                return Result("not_found", [], "Channel not found")
            if not feed:
                feed = self._yt_feed()
                cache = self.__dict__.setdefault("_yt_cache", {})
                for vid, p in feed.items():        # exact dates without opening those videos
                    cache.setdefault(vid, (p.time_utc, p.author))
            if f"/{tab}" not in self.page.url:   # channel has no such tab (YouTube redirects to the home tab)
                continue
            tabs_seen += 1
            try:
                tab_posts = self._yt_collect("ytd-rich-item-renderer a[href], ytd-grid-video-renderer a[href]", older_needed=2)
            except Stopped as e:              # keep the tabs already read
                raise Stopped(list(found.values()) + e.posts + list(feed.values())) from None
            for p in tab_posts:
                found.setdefault(_yt_id(p.url), p)
        for vid, p in feed.items():
            found.setdefault(vid, p)
        posts = list(found.values())
        if not posts:
            if not tabs_seen:
                return self._finish([], ("consent.youtube.com",))
            return Result("ok", [], self._empty_note("No videos could be read from the channel", "youtube_" + acc.handle))
        undated = sum(1 for p in posts if p.time_utc is None)
        notes = [f"{len(feed)} newest videos from the channel feed"] if feed else ["channel feed not available"]
        if undated:
            notes.append(f"{undated} videos without a readable date skipped")
        return Result("ok", self._in_range(posts), "; ".join(notes))

    def facebook(self, acc: Account) -> Result:
        posts = self._fb_collect(acc.url, older_needed=3, idle_limit=6)
        return self._finish(posts, ("/login", "checkpoint"))

    def _fb_collect(self, url: str, older_needed: int, idle_limit: int, max_rounds: int = 400, cap: int = 0):
        """Open a Facebook page and read posts (url + creation_time) from the data it loads while scrolling."""
        responses = []

        def on_response(r):
            if "/api/graphql" in r.url:
                responses.append(r)

        self.page.on("response", on_response)
        try:
            self._goto(url)
            found: dict[str, Post] = {}

            def absorb(obj):
                for u, ts in _fb_walk(obj):
                    if u not in found:
                        found[u] = Post(u, datetime.fromtimestamp(ts, tz=timezone.utc), _fb_author(u))

            # Posts already embedded in the first HTML
            try:
                html = self.page.content()
                for blob in re.findall(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
                    try:
                        absorb(json.loads(blob))
                    except ValueError:
                        pass
            except Exception:  # noqa: BLE001
                pass

            def extract():
                while responses:
                    r = responses.pop(0)
                    try:
                        body = r.text()
                    except Exception:  # noqa: BLE001
                        continue
                    for line in body.replace("for (;;);", "").splitlines():
                        line = line.strip()
                        if line.startswith("{"):
                            try:
                                absorb(json.loads(line))
                            except ValueError:
                                pass
                return list(found.values())

            return self._scroll_collect(extract, older_needed=older_needed, idle_limit=idle_limit,
                                        max_rounds=max_rounds, cap=cap)
        finally:
            try:
                self.page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass


_FETCH_TEXT_JS = r"""async (url) => {
  const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 20000);
  try { const r = await fetch(url, {credentials: 'include', signal: ctl.signal}); return r.ok ? await r.text() : ''; }
  catch (e) { return ''; } finally { clearTimeout(t); }
}"""


def _yt_id(url: str) -> str:
    m = re.search(r"(?:v=|/shorts/)([\w-]{11})", url)
    return m.group(1) if m else url


def _parse_iso(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    value = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value)          # +0700 -> +07:00 (Python 3.10)
    value = re.sub(r"(\.\d{1,6})\d*(?=[+-]|Z|$)", r"\1", value)     # trim fractions beyond microseconds
    try:
        if len(value) == 10:          # date only
            return datetime.strptime(value, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fb_author(url: str) -> str:
    m = re.match(r"https?://[^/]*facebook\.com/([^/?#]+)/(?:posts|videos|photos)/", url)
    if m and m.group(1) not in ("permalink.php", "story.php", "watch", "reel", "photo"):
        return m.group(1)
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else ""


_FB_POST_RX = re.compile(r"facebook\.com/.*(/posts/|/videos/|/reel/|/photos?/|permalink\.php|story\.php|/watch/|fbid=)")


def _fb_walk(obj, depth: int = 0):
    """Yield (url, creation_time) for every feed story in Facebook's page data.
    Shared stories (attached_story) are skipped so only the page's own posts are kept."""
    if depth > 60:
        return
    if isinstance(obj, dict):
        ct = obj.get("creation_time")
        if isinstance(ct, int) and ct > 1_000_000_000:
            for k in ("url", "permalink_url", "wwwURL"):
                v = obj.get(k)
                if isinstance(v, str) and _FB_POST_RX.search(v):
                    yield v, ct
                    break
        for k, v in obj.items():
            if k == "attached_story":
                continue
            if isinstance(v, (dict, list)):
                yield from _fb_walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                yield from _fb_walk(v, depth + 1)
