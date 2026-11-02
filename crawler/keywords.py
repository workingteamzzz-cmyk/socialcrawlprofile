"""Keyword search: collect post links that each platform's own search shows for a keyword,
limited to the chosen period. No API: the logged-in browser uses the platform's search page.

How complete the result is depends on each platform's search:
- X: search operators since:/until: + "Latest" tab -> chronological, good coverage.
- YouTube: search sorted by upload date, with after:/before: -> chronological, good coverage.
- Threads: "Recent" tab -> chronological, limited depth.
- Facebook: "Recent posts" filter -> roughly chronological, unstable.
- Instagram: hashtag page and keyword search show "top" posts only, not in date order.
- TikTok: video search is ranked by relevance, not date.
Dates always come from the post ID / page data, so out-of-period posts are dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import quote

from .browser import BrowserCrawler, Post, Result, Stopped
from .idtime import instagram_shortcode_time, tiktok_id_time, x_status_time

KEYWORD_PLATFORMS = ["x", "youtube", "threads", "instagram", "tiktok", "facebook"]

# Facebook search filter "Recent posts" (base64 of {"recent_posts:0":"{\"name\":\"recent_posts\",\"args\":\"\"}"})
_FB_RECENT = "eyJyZWNlbnRfcG9zdHM6MCI6IntcIm5hbWVcIjpcInJlY2VudF9wb3N0c1wiLFwiYXJnc1wiOlwiXCJ9In0%3D"
_RESERVED_X = {"i", "home", "search", "explore", "settings", "hashtag", "notifications", "messages", "compose"}


@dataclass
class SearchOptions:
    exact: bool = False                 # wrap multi-word keywords in quotes (X, YouTube, Facebook)
    exclude: list[str] = field(default_factory=list)   # words to exclude (X, YouTube)
    x_lang: str = ""                    # "", "id", "en"
    x_no_replies: bool = True
    x_no_reposts: bool = True
    ig_hashtag: bool = True             # Instagram: hashtag page (single-word keywords)
    ig_keyword: bool = True             # Instagram: keyword search page
    cap: int = 300                      # max in-period links per keyword per platform (0 = no limit)
    detail: str = "links"               # "links" or "full" (also open each post for caption + engagement)

    @classmethod
    def from_dict(cls, d: dict) -> "SearchOptions":
        d = d or {}
        excl = d.get("exclude") or []
        if isinstance(excl, str):
            excl = re.split(r"[,\n]+", excl)
        return cls(
            exact=bool(d.get("exact", False)),
            exclude=[w.strip().lstrip("-") for w in excl if w.strip()],
            x_lang=str(d.get("x_lang") or "")[:5],
            x_no_replies=bool(d.get("x_no_replies", True)),
            x_no_reposts=bool(d.get("x_no_reposts", True)),
            ig_hashtag=bool(d.get("ig_hashtag", True)),
            ig_keyword=bool(d.get("ig_keyword", True)),
            cap=max(0, int(d.get("cap") or 0)),
            detail="full" if d.get("detail") == "full" else "links",
        )


def phrase(kw: str, exact: bool) -> str:
    kw = kw.strip()
    if exact and " " in kw and not kw.startswith('"'):
        return f'"{kw}"'
    return kw


def x_query(kw: str, opt: SearchOptions, since: str, until: str) -> str:
    parts = [phrase(kw, opt.exact)]
    parts += [f"-{w}" for w in opt.exclude]
    if opt.x_lang:
        parts.append(f"lang:{opt.x_lang}")
    if opt.x_no_replies:
        parts.append("-filter:replies")
    if opt.x_no_reposts:
        parts.append("-filter:nativeretweets")
    parts += [f"since:{since}", f"until:{until}"]
    return " ".join(parts)


def youtube_query(kw: str, opt: SearchOptions, start: datetime, end: datetime) -> str:
    # YouTube search understands after:/before: date operators; results are still date-checked per video.
    after = (start - timedelta(days=1)).strftime("%Y-%m-%d")
    before = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    return " ".join([phrase(kw, opt.exact)] + [f"-{w}" for w in opt.exclude] + [f"after:{after}", f"before:{before}"])


class KeywordCrawler(BrowserCrawler):
    """Reuses the browser helpers of the profile crawler with search pages instead of profiles."""

    def _cap_trim(self, posts: list[Post], cap: int) -> list[Post]:
        inr = sorted(self._in_range(posts), key=lambda p: p.time_utc, reverse=True)
        return inr[:cap] if cap else inr

    def _done(self, posts, opt, login_markers, note="") -> Result:
        if not posts:
            url = self.page.url.lower()
            if any(m in url for m in login_markers):
                return Result("login_required", [], "Login required — use 'Log in to platforms'")
            return Result("ok", [], note or "Search returned no posts")
        kept = self._cap_trim(posts, opt.cap)
        extra = f"{len(posts) - len(self._in_range(posts))} results outside the period dropped"
        return Result("ok", kept, "; ".join(x for x in (note, extra) if x))

    # ---------- X ----------
    def search_x(self, kw: str, opt: SearchOptions) -> Result:
        since = self.start.strftime("%Y-%m-%d")
        until = (self.end + timedelta(days=1)).strftime("%Y-%m-%d")
        q = x_query(kw, opt, since, until)
        self._goto(f"https://x.com/search?q={quote(q)}&src=typed_query&f=live")
        rx = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")

        def extract():
            # Only each tweet's own permalink (the link wrapping its timestamp), not quoted tweets.
            try:
                hrefs = self.page.eval_on_selector_all(
                    "article a[href*='/status/']", "els => els.filter(e => e.querySelector('time')).map(e => e.getAttribute('href'))")
            except Exception:  # noqa: BLE001
                hrefs = []
            out = []
            for h in hrefs:
                m = rx.match(h or "")
                if m and m.group(1).lower() not in _RESERVED_X:
                    out.append(Post(f"https://x.com/{m.group(1)}/status/{m.group(2)}", x_status_time(m.group(2)), m.group(1)))
            return out

        posts = self._scroll_collect(extract, older_needed=0, idle_limit=5, max_rounds=600, cap=opt.cap)
        return self._done(posts, opt, ("/login", "/i/flow/login"), f"query: {q}")

    # ---------- YouTube ----------
    def search_youtube(self, kw: str, opt: SearchOptions) -> Result:
        q = youtube_query(kw, opt, self.start, self.end)
        self._goto(f"https://www.youtube.com/results?search_query={quote(q)}&sp=CAI%253D")   # sort: upload date
        posts = self._yt_collect("ytd-video-renderer a[href]", older_needed=4, cap=opt.cap, max_rounds=150)
        return self._done(posts, opt, ("consent.youtube.com",), f"query: {q}")

    # ---------- Threads ----------
    def search_threads(self, kw: str, opt: SearchOptions) -> Result:
        self._goto(f"https://www.threads.com/search?q={quote(kw.strip())}&serp_type=default&filter=recent")
        rx = re.compile(r"^/@([^/]+)/post/([A-Za-z0-9_-]+)/?$")

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
                if not m:
                    continue
                t = None
                if dt:
                    try:
                        t = datetime.fromisoformat(dt.replace("Z", "+00:00"))
                    except ValueError:
                        t = None
                out.append(Post(f"https://www.threads.com/@{m.group(1)}/post/{m.group(2)}",
                                t or instagram_shortcode_time(m.group(2)), m.group(1)))
            return out

        posts = self._scroll_collect(extract, older_needed=6, idle_limit=5, max_rounds=300, cap=opt.cap)
        return self._done(posts, opt, ("/login",))

    # ---------- Instagram ----------
    def search_instagram(self, kw: str, opt: SearchOptions) -> Result:
        rx = re.compile(r"^/(?:([A-Za-z0-9._]+)/)?(p|reel|tv)/([A-Za-z0-9_-]{5,})")
        found: dict[str, Post] = {}
        sources = []
        tag = kw.strip().lstrip("#")
        if opt.ig_hashtag and re.fullmatch(r"[\w]+", tag, re.UNICODE):
            sources.append(("hashtag", f"https://www.instagram.com/explore/tags/{quote(tag.lower())}/"))
        if opt.ig_keyword:
            sources.append(("keyword search", f"https://www.instagram.com/explore/search/keyword/?q={quote(kw.strip())}"))
        if not sources:
            return Result("ok", [], "No Instagram source applies (hashtag needs a single word)")

        def extract():
            out = []
            for h in self._hrefs():
                m = rx.match(h or "")
                if m:
                    kind = "reel" if m.group(2) == "reel" else "p"
                    out.append(Post(f"https://www.instagram.com/{kind}/{m.group(3)}/",
                                    instagram_shortcode_time(m.group(3)), m.group(1) or ""))
            return out

        for _name, url in sources:
            try:
                self._goto(url)
                got = self._scroll_collect(extract, older_needed=0, idle_limit=4, max_rounds=40, cap=opt.cap)
            except Stopped as e:              # keep the source already read
                raise Stopped(list(found.values()) + e.posts) from None
            for p in got:
                found.setdefault(p.url, p)
            self._pause(2, 4)
        note = "Instagram shows top posts only (not every post) — " + " + ".join(n for n, _ in sources)
        return self._done(list(found.values()), opt, ("accounts/login", "/challenge"), note)

    # ---------- TikTok ----------
    def search_tiktok(self, kw: str, opt: SearchOptions) -> Result:
        self._goto(f"https://www.tiktok.com/search/video?q={quote(kw.strip())}")
        self._wait_captcha()
        rx = re.compile(r"/@([^/?#]+)/(video|photo)/(\d+)")

        def extract():
            self._wait_captcha()
            out = []
            for h in self._hrefs():
                m = rx.search(h or "")
                if m:
                    out.append(Post(f"https://www.tiktok.com/@{m.group(1)}/{m.group(2)}/{m.group(3)}",
                                    tiktok_id_time(m.group(3)), m.group(1)))
            return out

        posts = self._scroll_collect(extract, older_needed=0, idle_limit=4, max_rounds=60, cap=opt.cap)
        return self._done(posts, opt, ("/login",), "TikTok search is ranked by relevance, not date")

    # ---------- Facebook ----------
    def search_facebook(self, kw: str, opt: SearchOptions) -> Result:
        url = f"https://www.facebook.com/search/posts?q={quote(phrase(kw, opt.exact))}&filters={_FB_RECENT}"
        posts = self._fb_collect(url, older_needed=8, idle_limit=6, max_rounds=200, cap=opt.cap)
        return self._done(posts, opt, ("/login", "checkpoint"), "Facebook search is best effort")
