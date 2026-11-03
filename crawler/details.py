"""Optional post details: caption and engagement, read the way a person would — by opening each post
in the browser and reading what the page shows (visible text, page metadata, or the data the page
itself carries). Numbers are a snapshot at collection time. Missing values stay empty."""
from __future__ import annotations

import json
import re

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
    crawler._goto(url)
    fn = globals().get(f"_read_{platform}")
    out = fn(crawler) if fn else {}
    res = {k: out.get(k) for k in DETAIL_FIELDS} | {"caption": _clean(out.get("caption") or "")}
    if out.get("author"):
        res["author"] = str(out["author"]).lstrip("@")[:80]
    if not res["caption"] and all(res.get(k) is None for k in ("likes", "comments", "shares", "views")):
        saved = crawler.__dict__.setdefault("_detail_snapshots", set())
        if platform not in saved:
            saved.add(platform)
            name = crawler._snapshot(f"details_{platform}") if hasattr(crawler, "_snapshot") else ""
            if name:
                crawler.log(f"   ⚠ Could not read caption/engagement from {url} — page copy saved to outputs/debug/{name}")
    return res


# ---------- per platform ----------
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
    g = data.get("group", "")
    return {
        "caption": data.get("caption", ""),
        "comments": _count_near(g, ["replies", "reply", "balasan"]),
        "shares": _count_near(g, ["reposts", "repost", "posting ulang", "retweets"]),
        "likes": _count_near(g, ["likes", "like", "suka"]),
        "views": _count_near(g, ["views", "view", "tayangan"]) or _count_near(data.get("views", ""), ["views", "tayangan"]),
    }


def _read_instagram(c) -> dict:
    """Three ways, like a person: the page summary (meta), the post data the page carries, then what is on screen."""
    page = c.page
    try:
        page.wait_for_selector("article, main h1, main ul", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    meta = page.evaluate(_META_JS) or {}
    desc = meta.get("desc", "") or ""
    out = {}
    # 1. '1,234 likes, 56 comments - user on October 5, 2026: "caption"' / '1.234 suka, 56 komentar - user pada ...: "..."'
    m = re.search(r':\s*"(.*)"\s*\.?\s*$', desc, re.S)
    if m:
        out["caption"] = m.group(1)
    out["likes"] = _count_near(desc, ["likes", "like", "suka"])
    out["comments"] = _count_near(desc, ["comments", "comment", "komentar"])
    m = re.search(r"-\s*([A-Za-z0-9._]{1,30})\s+(?:on|pada)\s", desc)
    if m:
        out["author"] = m.group(1)
    # 2. the post's own data in the page (logged-in pages often have no summary)
    code = re.search(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)", page.url)
    html = page.content()
    win = ""
    if code:
        i = max(html.find(f'"code":"{code.group(1)}"'), html.find(f'"shortcode":"{code.group(1)}"'))
        win = html[max(0, i - 15000): i + 15000] if i >= 0 else ""

    def num(*keys):
        for k in keys:
            mm = re.search(rf'"{k}":\s*(\d+)', win)
            if mm:
                return int(mm.group(1))
        return None
    if win:
        out["likes"] = out["likes"] if out["likes"] is not None else num("like_count")
        out["comments"] = out["comments"] if out["comments"] is not None else num("comment_count")
        out["views"] = num("play_count", "ig_play_count", "view_count", "video_view_count")
        if not out.get("caption"):
            mm = re.search(r'"caption":\{[^{}]*?"text":"((?:[^"\\]|\\.)*)"', win)
            out["caption"] = _unescape(mm.group(1)) if mm else ""
        if not out.get("author"):
            mm = re.search(r'"(?:owner|user)":\{[^{}]*?"username":"([A-Za-z0-9._]{1,30})"', win)
            out["author"] = mm.group(1) if mm else None
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
    try:
        data = json.loads(raw) if raw else {}
        item = data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {}).get("itemInfo", {}).get("itemStruct", {})
        if not item and "ItemModule" in data:
            item = next(iter(data["ItemModule"].values()), {})
    except (ValueError, AttributeError, StopIteration):
        item = {}
    st = item.get("stats") or {}
    if item:
        return {"caption": item.get("desc", ""), "likes": parse_count(st.get("diggCount")),
                "comments": parse_count(st.get("commentCount")), "shares": parse_count(st.get("shareCount")),
                "views": parse_count(st.get("playCount"))}
    dom = c.page.evaluate("""() => { const t = s => (document.querySelector(s) || {}).innerText || '';
      return {caption: t('[data-e2e="browse-video-desc"]') || t('[data-e2e="video-desc"]'),
              likes: t('[data-e2e="like-count"]'), comments: t('[data-e2e="comment-count"]'),
              shares: t('[data-e2e="share-count"]')}; }""") or {}
    return {"caption": dom.get("caption", ""), "likes": parse_count(dom.get("likes")),
            "comments": parse_count(dom.get("comments")), "shares": parse_count(dom.get("shares"))}


def _read_youtube(c) -> dict:
    html = c.page.content()

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


def _read_threads(c) -> dict:
    html = c.page.content()
    code = c.page.url.rstrip("/").rsplit("/", 1)[-1]
    i = html.find(f'"code":"{code}"')
    win = html[max(0, i - 6000): i + 6000] if i >= 0 else ""

    def num(key):
        m = re.search(rf'"{key}":(\d+)', win)
        return int(m.group(1)) if m else None
    cap = re.search(r'"caption":\{[^{}]*?"text":"((?:[^"\\]|\\.)*)"', win)
    meta = c.page.evaluate(_META_JS) or {}
    return {"caption": _unescape(cap.group(1)) if cap else meta.get("desc", ""),
            "likes": num("like_count"), "comments": num("direct_reply_count"),
            "shares": (num("repost_count") or 0) + (num("quote_count") or 0) if (num("repost_count") is not None) else None}


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
