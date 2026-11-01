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


@dataclass
class Result:
    status: str          # ok | not_found | login_required | error | invalid | stopped
    posts: list[Post]
    note: str = ""


class Stopped(Exception):
    pass


def launch_context(pw, profile_dir: str, headless: bool = False):
    os.makedirs(profile_dir, exist_ok=True)
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
    raise RuntimeError("Browser tidak bisa dibuka. " + " | ".join(errors))


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
                self.log("   ⚠ Captcha/verifikasi muncul — selesaikan di jendela browser, proses menunggu...")
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
                        idle_limit: int = 5, max_rounds: int = 400) -> list[Post]:
        """Scroll until enough posts older than the start date are seen (newest-first feeds),
        or nothing new loads. older_needed > 1 so pinned old posts at the top don't stop it early."""
        found: dict[str, Post] = {}
        idle = 0
        for _ in range(max_rounds):
            self._check_stop()
            before = len(found)
            for p in extract():
                if p.url not in found or (found[p.url].time_utc is None and p.time_utc):
                    found[p.url] = p
            older = sum(1 for p in found.values() if p.time_utc and p.time_utc < self.start)
            if older >= older_needed:
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
                return Result("login_required", [], "Perlu login — klik 'Login dulu' lalu login ke platform ini")
            if state == "not_found":
                return Result("not_found", [], "Akun tidak ditemukan / tidak tersedia")
            return Result("ok", [], "Tidak ada unggahan yang terbaca di profil")
        undated = sum(1 for p in posts if p.time_utc is None)
        note = f"{undated} link tanpa tanggal dilewati" if undated else ""
        return Result("ok", self._in_range(posts), note)

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
                return Result("not_found", [], "Akun tidak ditemukan / ditangguhkan")
            if state == "login_required":
                return Result("login_required", [], "Perlu login ke X")
            return Result("ok", [], "")
        return self._finish(posts, ("/login", "/i/flow/login"))

    def facebook(self, acc: Account) -> Result:
        responses = []

        def on_response(r):
            if "/api/graphql" in r.url:
                responses.append(r)

        self.page.on("response", on_response)
        try:
            self._goto(acc.url)
            found: dict[str, Post] = {}

            def absorb(obj):
                for url, ts in _fb_walk(obj):
                    if url not in found:
                        found[url] = Post(url, datetime.fromtimestamp(ts, tz=timezone.utc))

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

            posts = self._scroll_collect(extract, older_needed=3, idle_limit=6)
        finally:
            try:
                self.page.remove_listener("response", on_response)
            except Exception:  # noqa: BLE001
                pass
        return self._finish(posts, ("/login", "checkpoint"))


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
