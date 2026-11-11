"""Fill what the browser could not read in social rows (User ID, User Image, Follower, caption, engagement) with the
scraping API — the fewest credits possible:

1. free first: account data already known (profile cache, or another row of the same account) is copied;
2. a post with no caption or no engagement is fetched by its link (1 credit) — that also brings the account's
   User ID / image, which is then copied to every row of that account;
3. an account still without User ID / image / followers gets one profile call (1 credit per account, not per row);
4. stops at the per-run fill cap and at the daily / monthly limit; posts without a caption go first, newest first.
Nothing is fetched when no API key is set or auto-fill is off (API settings)."""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

from . import settings

ENGINE_PLATFORM = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube", "threads": "Threads",
                   "facebook": "Facebook", "x": "Twitter", "twitter": "Twitter"}
ACCOUNT_FIELDS = ("user_id", "user_image", "followers")
_LOCK = threading.Lock()


def _handle(r: dict) -> str:
    return str(r.get("author") or r.get("handle") or "").lstrip("@").strip()


def _empty(v) -> bool:
    return v in (None, "", 0, "0") or (isinstance(v, str) and not v.strip())


def _user(r: dict) -> str:
    from ..templates import username_from_url
    return _handle(r) or username_from_url(str(r.get("platform", "")).lower(), r.get("url") or "")


def needs(r: dict) -> set:
    """Which fields of a row are missing."""
    miss = {f for f in ACCOUNT_FIELDS if _empty(r.get(f))}
    if not _user(r):
        miss.add("username")
    if _empty(r.get("caption")) and _empty(r.get("text")):
        miss.add("text")
    if all(_empty(r.get(k)) for k in ("likes", "comments", "shares", "views")):
        miss.add("engagement")
    return miss


def _account_key(r: dict):
    return (ENGINE_PLATFORM.get(str(r.get("platform", "")).lower(), ""), _user(r).lower())


def _copy_account(r: dict, acc: dict) -> int:
    n = 0
    for f in ACCOUNT_FIELDS:
        if _empty(r.get(f)) and not _empty(acc.get(f)):
            r[f] = acc[f]
            n += 1
    return n


def plan(rows: list[dict]) -> dict:
    """What would be fetched (for the log and tests): posts by link and accounts by profile."""
    posts = [r for r in rows if ({"text", "engagement", "username"} & needs(r)) and r.get("url")
             and ENGINE_PLATFORM.get(str(r.get("platform", "")).lower())]
    posts.sort(key=lambda r: ("username" not in needs(r) and "text" not in needs(r), -(r.get("ts") or 0)))
    accounts = {_account_key(r) for r in rows if set(ACCOUNT_FIELDS) & needs(r) and _account_key(r)[1]}
    return {"posts": posts, "accounts": accounts}


def fill(rows: list[dict], log, stop_event: threading.Event | None = None, data_dir: str | None = None) -> dict:
    """Fill rows in place; returns {"credits", "posts", "profiles", "filled_rows"}."""
    out = {"credits": 0, "posts": 0, "profiles": 0, "filled_rows": 0}
    keys = settings.load()
    pub = settings.public()
    if not rows or not keys.get("api_key") or not pub.get("auto_fill", True):
        return out
    todo = [r for r in rows if needs(r)]
    if not todo:
        return out
    cap = int(pub.get("fill_cap", 100) or 0)
    left = settings.credits_left()
    if left is not None:
        cap = min(cap, left) if cap else left
    if cap <= 0:
        log("💳 API auto-fill skipped: the credit limit is used up")
        return out
    from . import runner
    eng = runner.engine()
    if not _LOCK.acquire(blocking=False):
        return out
    try:
        eng.API_KEY = keys["api_key"]
        eng.HEADERS["x-api-key"] = keys["api_key"]
        eng.TWITTER_API_KEY = keys.get("twitter_api_key", "")
        eng.STOP["on"] = False
        eng.BUDGET["max"], eng.BUDGET["warned"] = cap, True
        for k in ("total", "twitter", "other"):
            eng._REQN[k] = 0
        eng._REQN["fase"] = {}
        data_dir = data_dir or os.path.dirname(settings._path())
        prof_path = os.path.join(data_dir, "api_profiles.json")
        eng._prof_cache.clear()
        runner._load_profiles(eng, prof_path, followers=True)

        before = {id(r): len(needs(r)) for r in todo}
        # 1) free: known profiles and other rows of the same account
        known: dict = {}
        for (plat, user), v in eng._prof_cache.items():
            if v:
                known[(plat, user.lower())] = {"user_id": v.get("user_id"), "user_image": v.get("profile_image"),
                                               "followers": v.get("followers")}
        for r in rows:
            k = _account_key(r)
            acc = known.setdefault(k, {})
            for f in ACCOUNT_FIELDS:
                if _empty(acc.get(f)) and not _empty(r.get(f)):
                    acc[f] = r[f]
        for r in todo:
            _copy_account(r, known.get(_account_key(r), {}))

        p = plan(todo)
        log(f"🔌 API auto-fill: {len(todo)} row(s) with empty data — {len(p['posts'])} post(s) by link, "
            f"up to {len(p['accounts'])} account(s) by profile; cap {cap} credit(s)")

        def stopped():
            return bool(stop_event and stop_event.is_set())

        # 2) posts by link
        def one(r):
            if stopped():
                return r, None
            try:
                return r, eng._fetch_link(r["url"], False, lambda m: None)
            except eng.BudgetExceeded:
                return r, None
            except Exception:  # noqa: BLE001
                return r, None
        with ThreadPoolExecutor(max_workers=6) as ex:
            for r, got in ex.map(one, p["posts"]):
                if not got:
                    continue
                out["posts"] += 1
                if not _user(r) and got.get("username"):
                    r["author"] = str(got["username"]).lstrip("@")
                if _empty(r.get("caption")) and _empty(r.get("text")) and got.get("text"):
                    r["text"] = got["text"]
                for k in ("likes", "comments", "shares", "views"):
                    if _empty(r.get(k)) and not _empty(got.get(k)):
                        r[k] = got[k]
                acc = {"user_id": got.get("user_id"), "user_image": got.get("profile_image"),
                       "followers": got.get("followers")}
                _copy_account(r, acc)
                cur = known.setdefault(_account_key(r), {})
                for f in ACCOUNT_FIELDS:
                    if _empty(cur.get(f)) and not _empty(acc.get(f)):
                        cur[f] = acc[f]
        for r in todo:
            _copy_account(r, known.get(_account_key(r), {}))

        # 3) accounts still missing something: one profile call each
        need_acc = sorted({_account_key(r) for r in todo if set(ACCOUNT_FIELDS) & needs(r)
                           and _account_key(r)[1] and eng.PROFILE_EP.get(_account_key(r)[0])})
        orig = {(_account_key(r)): _user(r) for r in todo}

        def prof(k):
            if stopped():
                return k, None
            try:
                return k, eng.fetch_profile(k[0], orig.get(k) or k[1], lambda m: None)
            except eng.BudgetExceeded:
                return k, None
            except Exception:  # noqa: BLE001
                return k, None
        with ThreadPoolExecutor(max_workers=8) as ex:
            for k, v in ex.map(prof, need_acc):
                if v:
                    out["profiles"] += 1
                    cur = known.setdefault(k, {})
                    for f, src in (("user_id", "user_id"), ("user_image", "profile_image"), ("followers", "followers")):
                        if _empty(cur.get(f)) and not _empty(v.get(src)):
                            cur[f] = v[src]
        for r in todo:
            _copy_account(r, known.get(_account_key(r), {}))

        out["filled_rows"] = sum(1 for r in todo if len(needs(r)) < before[id(r)])
        out["credits"] = int(eng._REQN["other"]) + int(eng._REQN["twitter"])
        settings.record_usage(eng._REQN["other"], eng._REQN["twitter"])
        try:
            runner._save_profiles(eng, prof_path)
        except OSError:
            pass
        still = sum(1 for r in todo if needs(r))
        log(f"   ✅ API auto-fill: {out['filled_rows']} row(s) completed, {out['credits']} credit(s) used"
            + (f"; {still} row(s) still have empty fields (cap reached, private/deleted, or not offered by the API)"
               if still else ""))
        return out
    finally:
        eng.BUDGET["max"] = 0
        _LOCK.release()


COMMENT_PLATFORMS = ("TikTok", "YouTube", "Instagram", "Facebook", "Threads")


def comments(rows: list[dict], log, stop_event: threading.Event | None = None) -> list[dict]:
    """Comments of the most engaged posts (API, the team's v58 comment reader): top-N posts by likes + comments,
    at most M comments each (~1 credit per post), capped by the daily / monthly limit. Returns new Medsos rows."""
    from ..templates import post_id
    keys, pub = settings.load(), settings.public()
    if not rows or not keys.get("api_key") or not pub.get("comments"):
        return []
    top_n, per_post = int(pub.get("cmt_posts", 10) or 0), int(pub.get("cmt_per_post", 20) or 20)
    left = settings.credits_left()
    if left is not None:
        top_n = min(top_n, left)
    posts = [r for r in rows if r.get("sub_type") != "comment" and r.get("url")
             and ENGINE_PLATFORM.get(str(r.get("platform", "")).lower()) in COMMENT_PLATFORMS
             and not _empty(r.get("comments"))]
    posts.sort(key=lambda r: -((_num(r.get("likes")) or 0) + (_num(r.get("comments")) or 0)))
    posts = posts[:max(0, top_n)]
    if not posts:
        return []
    from . import runner
    eng = runner.engine()
    if not _LOCK.acquire(blocking=False):
        return []
    out: list[dict] = []
    try:
        eng.HEADERS["x-api-key"] = keys["api_key"]
        eng.API_KEY = keys["api_key"]
        eng.STOP["on"] = False
        eng.BUDGET["max"], eng.BUDGET["warned"] = max(top_n * max(1, (per_post + 24) // 25), 1), True
        for k in ("total", "twitter", "other"):
            eng._REQN[k] = 0
        log(f"💬 Comments (API): the {len(posts)} most engaged post(s), up to {per_post} comment(s) each")

        def one(r):
            if stop_event and stop_event.is_set():
                return r, []
            plat = ENGINE_PLATFORM[str(r["platform"]).lower()]
            pid = post_id(str(r["platform"]).lower(), r["url"], str(r.get("user_id") or ""))
            try:
                return r, eng.fetch_comments(plat, r["url"], pid, False, lambda m: None, cap=per_post)
            except Exception:  # noqa: BLE001  (credit cap, network)
                return r, []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r, got in ex.map(one, posts):
                parent = post_id(str(r["platform"]).lower(), r["url"], str(r.get("user_id") or ""))
                for c in got or []:
                    d, t = c.get("date") or "", (c.get("time") or "")[:5]
                    out.append({"platform": r["platform"], "author": c.get("username") or "", "url": c.get("url") or r["url"],
                                "text": c.get("text") or "", "date": d, "time": t, "ts": 0,
                                "sort_ts": (r.get("ts") or 0) - 0.001,
                                "likes": c.get("likes"), "comments": c.get("comments"), "shares": None, "views": None,
                                "user_id": c.get("user_id") or "", "user_image": c.get("profile_image") or "",
                                "followers": c.get("followers"), "sub_type": "comment", "post_id": str(c.get("id") or ""),
                                "parent_id": parent, "user_id_parent": str(r.get("user_id") or ""),
                                "keyword": r.get("keyword", ""), "keywords": list(r.get("keywords") or [])})
        settings.record_usage(eng._REQN["other"], eng._REQN["twitter"])
        log(f"   ✅ {len(out)} comment(s) added, {eng._REQN['other']} credit(s) used")
        return out
    finally:
        eng.BUDGET["max"] = 0
        _LOCK.release()


def _num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
