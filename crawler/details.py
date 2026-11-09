"""Optional post details: caption and engagement, read the way a person would — by opening each post
in the browser and reading what the page shows (visible text, page metadata, or the data the page
itself carries). Numbers are a snapshot at collection time. Missing values stay empty."""
from __future__ import annotations

import html as _html
import json
import re
from urllib.parse import urlparse

DETAIL_FIELDS = ["caption", "likes", "comments", "shares", "views"]
_MAX_TEXT = 30000          # an Excel cell holds 32,767 characters

_SUFFIX = {"k": 1e3, "rb": 1e3, "ribu": 1e3, "m": 1e6, "jt": 1e6, "juta": 1e6, "b": 1e9, "m.": 1e6, "mlr": 1e9, "miliar": 1e9}


def parse_count(text) -> int | None:
    """'1,234' '1.234' '1.2K' '1,2 rb' '3 jt' '12.5M' '987' -> int. Indonesian and English formats."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().lower().replace(" ", " ")
    m = re.match(r"^([\d.,]+)\s*(k|rb|ribu|jt|juta|m|b|mlr|miliar)?\b", s)
    if not m:
        return None
    num, suf = m.group(1), m.group(2)
    if suf:
        num = num.replace(",", ".")
        if num.count(".") > 1:
            num = num.replace(".", "", num.count(".") - 1)
        try:
            return int(round(float(num) * _SUFFIX[suf]))
        except ValueError:
            return None
    digits = re.sub(r"[.,](?=\d{3}(\D|$))", "", num)       # thousands separators
    digits = digits.split(",")[0].split(".")[0]
    return int(digits) if digits.isdigit() else None


def _count_near(text: str, words: list[str]) -> int | None:
    """Find '<number> <word>' in visible text, e.g. '1.234 suka', '56 comments', '12 rb kali dibagikan'."""
    for w in words:
        m = re.search(r"([\d][\d.,]*\s*(?:k|rb|ribu|jt|juta|m|b)?)\s+" + w + r"\b", text, re.I)
        if m:
            v = parse_count(m.group(1))
            if v is not None:
                return v
    return None


def _clean(text: str) -> str:
    return (text or "").strip()[:_MAX_TEXT]


_META_JS = """() => { const m = n => (document.querySelector(`meta[property="${n}"],meta[name="${n}"]`) || {}).content || '';
  return {desc: m('og:description') || m('description'), title: m('og:title')}; }"""


def read_details(crawler, platform: str, url: str) -> dict:
    """Open the post and read caption + engagement (and the author when the list did not show it).
    crawler is a BrowserCrawler (uses its page). When nothing at all could be read, one copy of such a page per
    platform is kept in outputs/debug/ so the reader can be adjusted."""
    _open(crawler, url)
    fn = globals().get(f"_read_{platform}")
    out = fn(crawler) if fn else {}
    res = _result(out)
    if not res["caption"] and all(res.get(k) is None for k in ("likes", "comments", "shares", "views")):
        saved = crawler.__dict__.setdefault("_detail_snapshots", set())
        if platform not in saved:
            saved.add(platform)
            name = crawler._snapshot(f"details_{platform}") if hasattr(crawler, "_snapshot") else ""
            if name:
                crawler.log(f"   ⚠ Could not read caption/engagement from {url} — page copy saved to outputs/debug/{name}")
    return res


def _open(crawler, url: str):
    """Open a post like a person clicking it, without idling: the readers below wait for the content they need."""
    crawler._check_stop()
    try:
        crawler.page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:  # noqa: BLE001
        if "Timeout" not in str(e):
            raise
    crawler._pause(0.6, 1.2)


def _has_numbers(d: dict | None) -> bool:
    return bool(d) and any(d.get(k) is not None for k in ("likes", "comments", "shares", "views"))


def _result(out: dict) -> dict:
    res = {k: out.get(k) for k in DETAIL_FIELDS} | {"caption": _clean(out.get("caption") or "")}
    if out.get("author"):
        res["author"] = str(out["author"]).lstrip("@")[:80]
    return res


# ---------- fast path: read many posts at once in the background ----------
# The post pages of TikTok, YouTube, Threads and Instagram carry their numbers in the page itself, so the open tab
# downloads a few of them at a time (with the user's own session, like opening them in background tabs) instead of
# showing each one. Whatever cannot be read this way is opened normally afterwards.
_FETCH_MANY_JS = r"""async ([paths, par]) => {
  const out = {};
  const one = async (p) => {
    const ctl = new AbortController(); const t = setTimeout(() => ctl.abort(), 20000);
    try {
      const r = await fetch(p, {credentials: 'include', signal: ctl.signal});
      out[p] = r.ok ? (await r.text()).slice(0, 4000000) : '';
    } catch (e) { out[p] = ''; } finally { clearTimeout(t); }
  };
  for (let i = 0; i < paths.length; i += par) await Promise.all(paths.slice(i, i + par).map(one));
  return out;
}"""

_HOME = {"tiktok": "https://www.tiktok.com/", "youtube": "https://www.youtube.com/",
         "threads": "https://www.threads.com/", "instagram": "https://www.instagram.com/"}
_PARALLEL = {"instagram": 2}          # Instagram is the quickest to say "try again later"


def prefetch_details(crawler, platform: str, urls: list[str], progress=None) -> dict:
    """{url: details} for the posts that could be read in the background; the rest are left out."""
    parse = globals().get(f"_parse_{platform}_html")
    home = _HOME.get(platform)
    if not parse or not home or not urls:
        return {}
    host = urlparse(home).hostname
    def site(u):                                           # www./m./bare host all count as the same site
        h = (urlparse(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h[2:] if h.startswith("m.") else h
    todo = [u for u in urls if site(u) == site(home)]
    if not todo:
        return {}
    if (urlparse(crawler.page.url).hostname or "") != host:
        crawler._goto(home)                               # same site, so the page may read the posts
    got = {}
    par = _PARALLEL.get(platform, 4)
    step = par * 3
    misses = 0
    for k in range(0, len(todo), step):
        crawler._check_stop()
        batch = todo[k:k + step]
        paths = [urlparse(u).path + (("?" + urlparse(u).query) if urlparse(u).query else "") for u in batch]
        try:
            pages = crawler.page.evaluate(_FETCH_MANY_JS, [paths, par]) or {}
        except Exception as e:  # noqa: BLE001
            if "closed" in str(e):
                raise
            break
        for u, pth in zip(batch, paths):
            try:
                d = _result(parse(pages.get(pth) or "", u))
            except Exception:  # noqa: BLE001
                d = {}
            if _has_numbers(d):
                got[u] = d
            else:
                misses += 1
        if progress:
            progress(min(k + step, len(todo)), len(todo))
        if k == 0 and misses == len(batch):
            break                                          # this site does not answer this way now: open them instead
        if k + step < len(todo):
            crawler._pause(0.5, 1.1)
    return got


def x_counts(group_label: str, caption: str = "", views_text: str = "") -> dict:
    """X's action bar label: '12 replies, 34 reposts, 560 likes, 7 bookmarks, 8901 views' (or Indonesian)."""
    g = group_label or ""
    return {
        "caption": caption or "",
        "comments": _count_near(g, ["replies", "reply", "balasan"]),
        "shares": _count_near(g, ["reposts", "repost", "posting ulang", "retweets"]),
        "likes": _count_near(g, ["likes", "like", "suka"]),
        "views": _count_near(g, ["views", "view", "tayangan"]) or _count_near(views_text, ["views", "tayangan"]),
    }


def _meta(html: str, name: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"', html) or \
        re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(name)}"', html)
    return _html.unescape(m.group(1)) if m else ""


def _parse_tiktok_html(html: str, url: str = "") -> dict:
    m = re.search(r'<script[^>]+id="(?:__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)"[^>]*>(.*?)</script>', html, re.S)
    return _tiktok_item(m.group(1) if m else "")


def _tiktok_item(raw: str) -> dict:
    try:
        data = json.loads(raw) if raw else {}
        item = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct", {})
        if not item and "ItemModule" in data:
            item = next(iter(data["ItemModule"].values()), {})
    except (ValueError, AttributeError, StopIteration):
        item = {}
    if not item:
        return {}
    st = item.get("stats") or {}
    return {"caption": item.get("desc", ""), "likes": parse_count(st.get("diggCount")),
            "comments": parse_count(st.get("commentCount")), "shares": parse_count(st.get("shareCount")),
            "views": parse_count(st.get("playCount")), "author": (item.get("author") or {}).get("uniqueId")}


def _parse_youtube_html(html: str, url: str = "") -> dict:
    def rx(p):
        m = re.search(p, html)
        return m.group(1) if m else ""
    title = _unescape(rx(r'"videoDetails":\{.*?"title":"((?:[^"\\]|\\.)*)"'))
    desc = _unescape(rx(r'"shortDescription":"((?:[^"\\]|\\.)*)"'))
    likes = rx(r'"label":"([\d.,]+\s*(?:rb|jt|K|M)?)\s*(?:likes|suka|orang lainnya)') or \
        rx(r'like this video along with ([\d.,]+) other') or rx(r'bersama ([\d.,]+) orang lainnya')
    comments = rx(r'"commentCount":\{"simpleText":"([^"]+)"') or rx(r'"contextualInfo":\{"runs":\[\{"text":"([^"]+)"')
    return {"caption": (title + ("\n\n" + desc if desc else "")).strip(),
            "views": parse_count(rx(r'"viewCount":"(\d+)"')), "likes": parse_count(likes), "comments": parse_count(comments)}


def _parse_threads_html(html: str, url: str = "") -> dict:
    code = (url or "").rstrip("/").rsplit("/", 1)[-1]
    i = html.find(f'"code":"{code}"') if code else -1
    win = html[max(0, i - 6000): i + 6000] if i >= 0 else ""

    def num(key):
        m = re.search(rf'"{key}":(\d+)', win)
        return int(m.group(1)) if m else None
    cap = re.search(r'"caption":\{[^{}]*?"text":"((?:[^"\\]|\\.)*)"', win)
    reposts = num("repost_count")
    return {"caption": _unescape(cap.group(1)) if cap else _meta(html, "og:description"),
            "likes": num("like_count"), "comments": num("direct_reply_count"),
            "shares": (reposts or 0) + (num("quote_count") or 0) if reposts is not None else None}


def _parse_instagram_html(html: str, url: str = "") -> dict:
    out = _instagram_from_desc(_meta(html, "og:description") or _meta(html, "description"))
    code = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", url or "")
    if code:
        out.update({k: v for k, v in _instagram_from_json(html, code.group(1), out).items() if v is not None})
    return out


def _instagram_from_desc(desc: str) -> dict:
    """'1,234 likes, 56 comments - user on October 5, 2026: "caption"' / '1.234 suka, 56 komentar - user pada ...'"""
    out = {}
    m = re.search(r':\s*"(.*)"\s*\.?\s*$', desc or "", re.S)
    if m:
        out["caption"] = m.group(1)
    out["likes"] = _count_near(desc or "", ["likes", "like", "suka"])
    out["comments"] = _count_near(desc or "", ["comments", "comment", "komentar"])
    m = re.search(r"-\s*([A-Za-z0-9._]{1,30})\s+(?:on|pada)\s", desc or "")
    if m:
        out["author"] = m.group(1)
    return out


def _instagram_from_json(html: str, code: str, have: dict) -> dict:
    i = max(html.find(f'"code":"{code}"'), html.find(f'"shortcode":"{code}"'))
    win = html[max(0, i - 15000): i + 15000] if i >= 0 else ""
    if not win:
        return {}

    def num(*keys):
        for k in keys:
            mm = re.search(rf'"{k}":\s*(\d+)', win)
            if mm:
                return int(mm.group(1))
        return None
    out = {"likes": have.get("likes") if have.get("likes") is not None else num("like_count"),
           "comments": have.get("comments") if have.get("comments") is not None else num("comment_count"),
           "views": num("play_count", "ig_play_count", "view_count", "video_view_count")}
    if not have.get("caption"):
        mm = re.search(r'"caption":\{[^{}]*?"text":"((?:[^"\\]|\\.)*)"', win)
        out["caption"] = _unescape(mm.group(1)) if mm else None
    if not have.get("author"):
        mm = re.search(r'"(?:owner|user)":\{[^{}]*?"username":"([A-Za-z0-9._]{1,30})"', win)
        out["author"] = mm.group(1) if mm else None
    return out


# ---------- per platform (post opened in the tab) ----------
def _read_x(c) -> dict:
    page = c.page
    try:
        page.wait_for_selector("article", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    data = page.evaluate("""() => {
      const art = document.querySelector('article[data-testid="tweet"]') || document.querySelector('article');
      if (!art) return {};
      const txt = art.querySelector('[data-testid="tweetText"]');
      const grp = art.querySelector('[role="group"][aria-label]');
      const views = [...art.querySelectorAll('a[href$="/analytics"], span')].map(e => e.innerText).find(t => /views|tayangan/i.test(t)) || '';
      return {caption: txt ? txt.innerText : '', group: grp ? grp.getAttribute('aria-label') : '', views};
    }""") or {}
    return x_counts(data.get("group", ""), data.get("caption", ""), data.get("views", ""))


def _read_instagram(c) -> dict:
    """Three ways, like a person: the page summary (meta), the post data the page carries, then what is on screen."""
    page = c.page
    try:
        page.wait_for_selector("article, main h1, main ul", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    meta = page.evaluate(_META_JS) or {}
    out = _instagram_from_desc(meta.get("desc", "") or "")
    out.setdefault("likes", None)
    out.setdefault("comments", None)
    # 2. the post's own data in the page (logged-in pages often have no summary)
    code = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", page.url)
    if code:
        out.update({k: v for k, v in _instagram_from_json(page.content(), code.group(1), out).items() if v is not None})
    # 3. what the page shows
    dom = page.evaluate(r"""() => {
      const main = document.querySelector('article') || document.querySelector('main') || document.body;
      const h1 = main.querySelector('h1');
      const head = main.querySelector('header a[href^="/"]:not([href*="/explore/"])');
      return {caption: h1 ? h1.innerText : '', author: head ? head.getAttribute('href').split('/').filter(Boolean)[0] : '',
              text: (main.innerText || '').slice(0, 6000)};
    }""") or {}
    if not out.get("caption"):
        out["caption"] = dom.get("caption", "")
    if not out.get("author") and dom.get("author"):
        out["author"] = dom["author"]
    text = dom.get("text", "")
    if out["likes"] is None:
        mm = re.search(r"(?:dan|and)\s+([\d.,]+\s*(?:rb|jt|K|M)?)\s+(?:lainnya|others)", text)   # "Disukai oleh x dan 1.234 lainnya"
        out["likes"] = (parse_count(mm.group(1)) + 1) if mm else _count_near(text, ["suka", "likes", "like"])
    if out["comments"] is None:
        out["comments"] = _count_near(text, ["komentar", "comments"])
    if out.get("views") is None:
        out["views"] = _count_near(text, ["views", "plays", "tayangan", "kali diputar"])
    return out


def _read_tiktok(c) -> dict:
    c._wait_captcha()
    raw = c.page.evaluate("""() => { const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')
      || document.getElementById('SIGI_STATE'); return s ? s.textContent : ''; }""") or ""
    item = _tiktok_item(raw)
    if item:
        return item
    dom = c.page.evaluate("""() => { const t = s => (document.querySelector(s) || {}).innerText || '';
      return {caption: t('[data-e2e="browse-video-desc"]') || t('[data-e2e="video-desc"]'),
              likes: t('[data-e2e="like-count"]'), comments: t('[data-e2e="comment-count"]'),
              shares: t('[data-e2e="share-count"]')}; }""") or {}
    return {"caption": dom.get("caption", ""), "likes": parse_count(dom.get("likes")),
            "comments": parse_count(dom.get("comments")), "shares": parse_count(dom.get("shares"))}


def _read_youtube(c) -> dict:
    return _parse_youtube_html(c.page.content())


def _read_threads(c) -> dict:
    return _parse_threads_html(c.page.content(), c.page.url)


def _read_facebook(c) -> dict:
    meta = c.page.evaluate(_META_JS) or {}
    body = _body(c, 20000)
    reactions = None
    m = re.search(r"(?:All reactions|Semua reaksi)[:\s]*([\d.,]+\s*(?:rb|jt|K|M)?)", body, re.I)
    if m:
        reactions = parse_count(m.group(1))
    return {"caption": meta.get("desc", "") or meta.get("title", ""),
            "likes": reactions,
            "comments": _count_near(body, ["comments", "comment", "komentar"]),
            "shares": _count_near(body, ["shares", "share", "kali dibagikan", "dibagikan"]),
            "views": _count_near(body, ["views", "tayangan", "kali ditonton"])}


def _body(c, limit=8000) -> str:
    try:
        return (c.page.inner_text("body", timeout=5000) or "")[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _unescape(s: str) -> str:
    if not s:
        return ""
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s
