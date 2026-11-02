"""Collect post links from profile pages with a real (logged-in) browser via Playwright.

No platform API is used: the browser opens the profile, scrolls, and reads the post links
that are on the page. Dates come from the post ID itself (see idtime.py) or, for Facebook,
from the data the page loads while scrolling.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import quote

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


@dataclass
class Result:
    status: str          # ok | not_found | login_required | error | invalid | stopped
    posts: list[Post]
    note: str = ""


class Stopped(Exception):
    pass


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
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    exe = os.environ.get("CRAWLER_BROWSER_PATH")
    if exe:
        return pw.chromium.launch_persistent_context(executable_path=exe, **opts)
    errors = []
    # Prefer the installed Google Chrome / Edge (less often blocked), fall back to Playwright's Chromium.
    for channel in ("chrome", "msedge", None):
        try:
            if channel:
                return pw.chromium.launch_persistent_context(channel=channel, **opts)
            return pw.chromium.launch_persistent_context(**opts)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel or 'chromium'}: {str(e).splitlines()[0]}")
    raise RuntimeError("Could not open a browser. " + " | ".join(errors))


class BrowserCrawler:
    def __init__(self, context, log: Callable[[str], None], stop_flag, start_utc: datetime, end_utc: datetime):
        self.ctx = context
        self.log = log
        self.stop_flag = stop_flag
        self.start = start_utc
        self.end = end_utc
        self.page = context.pages[0] if context.pages else context.new_page()

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
                        idle_limit: int = 5, max_rounds: int = 400, cap: int = 0) -> list[Post]:
        """Scroll until enough posts older than the start date are seen (newest-first feeds),
        or nothing new loads. older_needed > 1 so pinned old posts at the top don't stop it early;
        older_needed=0 for feeds that are not in date order. cap>0 stops after that many in-range posts."""
        found: dict[str, Post] = {}
        idle = 0
        for _ in range(max_rounds):
            self._check_stop()
            before = len(found)
            for p in extract():
                if p.url not in found or (found[p.url].time_utc is None and p.time_utc):
                    found[p.url] = p
            older = sum(1 for p in found.values() if p.time_utc and p.time_utc < self.start)
            if older_needed and older >= older_needed:
                break
            if cap and sum(1 for p in found.values() if p.time_utc and self.start <= p.time_utc <= self.end) >= cap:
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
            return Result("ok", [], "No posts could be read from the profile")
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
        rx = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)")
        handle = acc.handle.lower()

        def extract():
            out = []
            for h in self._hrefs():
                m = rx.match(h or "")
                if m and m.group(1).lower() == handle:
                    out.append(Post(f"https://x.com/{m.group(1)}/status/{m.group(2)}", x_status_time(m.group(2))))
            return out

        posts = self._scroll_collect(extract, older_needed=1, idle_limit=4)
        if not posts:
            # Search shows nothing for both "no posts in period" and "account missing": check the profile.
            self._goto(acc.url)
            state = self._page_state(("/login", "/i/flow/login"))
            if state == "not_found":
                return Result("not_found", [], "Account not found / suspended")
            if state == "login_required":
                return Result("login_required", [], "Login to X required")
            return Result("ok", [], "")
        return self._finish(posts, ("/login", "/i/flow/login"))

    # ---------- YouTube ----------
    # Video IDs carry no date, so each new video's publish date is read from its watch page with a
    # same-origin fetch from the open YouTube tab (4 at a time). No yt-dlp, no API.
    _YT_JS = r"""async (ids) => {
      const out = {};
      const one = async (id) => {
        try {
          const r = await fetch('/watch?v=' + id, {credentials: 'include'});
          const h = await r.text();
          const m = h.match(/"publishDate":"([^"]+)"/) || h.match(/"uploadDate":"([^"]+)"/)
                 || h.match(/itemprop="datePublished" content="([^"]+)"/);
          const a = h.match(/"ownerProfileUrl":"https?:\/\/www\.youtube\.com\/@([^"\/]+)"/)
                 || h.match(/"author":"([^"]+)"/);
          out[id] = {date: m ? m[1] : '', author: a ? a[1] : '', upcoming: /"isUpcoming":true/.test(h)};
        } catch (e) { out[id] = {date: '', author: '', upcoming: false}; }
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
                info = got.get(vid) or {}
                cache[vid] = (None if info.get("upcoming") else _parse_iso(info.get("date", "")), info.get("author", ""))
        return {i: cache[i] for i in ids}

    def _yt_collect(self, selector: str, older_needed: int, cap: int = 0, max_rounds: int = 200) -> list[Post]:
        rx = re.compile(r"(?:/watch\?v=|/shorts/)([A-Za-z0-9_-]{11})")

        def extract():
            try:
                hrefs = self.page.eval_on_selector_all(selector, "els => els.map(e => e.getAttribute('href'))")
            except Exception:  # noqa: BLE001
                hrefs = []
            if not hrefs:                       # layout changed: fall back to every link on the page
                hrefs = self._hrefs()
            order, kind = [], {}
            for h in hrefs:
                m = rx.search(h or "")
                if m and m.group(1) not in kind:
                    order.append(m.group(1))
                    kind[m.group(1)] = "shorts" if "/shorts/" in h else "watch"
            dates = self._yt_dates(order)
            return [Post(f"https://www.youtube.com/shorts/{v}" if kind[v] == "shorts" else f"https://www.youtube.com/watch?v={v}",
                         dates[v][0], dates[v][1]) for v in order]

        return self._scroll_collect(extract, older_needed=older_needed, idle_limit=4, max_rounds=max_rounds, cap=cap)

    def youtube(self, acc: Account) -> Result:
        base = acc.url.rstrip("/")
        found: dict[str, Post] = {}
        tabs_seen = 0
        for tab in ("videos", "shorts", "streams"):
            self._goto(f"{base}/{tab}")
            if tabs_seen == 0 and self._page_state(("consent.youtube.com",)) == "not_found":
                return Result("not_found", [], "Channel not found")
            if f"/{tab}" not in self.page.url:   # channel has no such tab (YouTube redirects to the home tab)
                continue
            tabs_seen += 1
            for p in self._yt_collect("ytd-rich-item-renderer a[href], ytd-grid-video-renderer a[href]", older_needed=2):
                found.setdefault(p.url, p)
        if not tabs_seen:
            return self._finish([], ("consent.youtube.com",))
        posts = list(found.values())
        undated = sum(1 for p in posts if p.time_utc is None)
        return Result("ok", self._in_range(posts), f"{undated} videos without a readable date skipped" if undated else "")

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


def _parse_iso(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
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
