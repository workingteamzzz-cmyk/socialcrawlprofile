"""Safe mode: keep Google and the team's social media accounts from being blocked.

On by default. It
- counts every page the app opens on Google Search and on each platform (search pages, post pages, background
  reads) and stops at a per-hour and per-day budget — a person does not open hundreds of pages in minutes;
- makes every pause between pages longer;
- recognises the platforms' block pages ("Try again later", "temporarily blocked", checkpoints, Google's 403) and
  then leaves that site alone for a while (Google 6 h, a platform 24 h) instead of knocking again, which only makes
  a block longer — and tells the team (Telegram);
- refuses schedules that would use the accounts too often (more often than every 6 hours).
What it costs: big searches stop earlier; what the browser could not read is left to the API (if set up).
State: APP_DATA/safety.json. Admins can switch it off, or let a resting site go early (Settings).
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

PLATFORMS = ("google", "instagram", "x", "tiktok", "facebook", "threads", "youtube")
HOSTS = {"instagram": ("instagram.com",), "x": ("x.com", "twitter.com"), "tiktok": ("tiktok.com",),
         "facebook": ("facebook.com", "fb.com"), "threads": ("threads.net", "threads.com"), "youtube": ("youtube.com",)}
PER_HOUR = {"google": 40, "instagram": 60, "x": 120, "tiktok": 100, "facebook": 40, "threads": 100, "youtube": 300}
PER_DAY = {"google": 150, "instagram": 200, "x": 400, "tiktok": 300, "facebook": 150, "threads": 300, "youtube": 1000}
REST_HOURS = {"google": 6}            # others: 24
PAUSE_FACTOR = 1.7
MIN_SCHEDULE_HOURS = 6                # schedules that use the accounts / Google's search pages
LABEL = {"google": "Google Search", "instagram": "Instagram", "x": "X", "tiktok": "TikTok", "facebook": "Facebook",
         "threads": "Threads", "youtube": "YouTube"}
# what a platform shows when it has limited / blocked the account (page text, lower case) or where it sends you (url)
BLOCK_TEXT = {
    "instagram": ("try again later", "we restrict certain activity", "please wait a few minutes before you try again",
                  "your account has been suspended", "we suspended your account"),
    "facebook": ("you're temporarily blocked", "you’re temporarily blocked", "you can't use this feature right now",
                 "you’re restricted from", "your account has been locked", "we suspended your account"),
    "threads": ("try again later", "we restrict certain activity"),
    "x": ("rate limit exceeded", "your account is suspended", "your account has been locked",
          "we've temporarily limited some of your account features"),
    "tiktok": ("too many attempts", "maximum number of attempts reached", "your account was banned"),
}
BLOCK_URL = {"instagram": ("/challenge/", "/accounts/suspended", "/accounts/disabled"),
             "facebook": ("/checkpoint/",), "x": ("/account/access", "/account/locked"),
             "threads": ("/challenge/",), "tiktok": ()}
_LOCK = threading.RLock()


class Resting(RuntimeError):
    """A site is resting (blocked, or its budget for now is used): nothing is opened on it."""


def _path() -> str:
    from .paths import APP_DATA
    return os.path.join(APP_DATA, "safety.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_path()), exist_ok=True)
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, _path())
    except OSError:
        pass


def enabled() -> bool:
    return bool(_load().get("enabled", True))


def set_enabled(on: bool) -> dict:
    with _LOCK:
        d = _load()
        d["enabled"] = bool(on)
        _save(d)
    return status()


def pause_factor() -> float:
    return PAUSE_FACTOR if enabled() else 1.0


def platform_of(url: str) -> str:
    """Which budget a page belongs to: Google's search pages, or a social platform ('' = not counted)."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if (host.startswith("www.google.") or host.startswith("google.")) and p.path.startswith(("/search", "/sorry")):
        return "google"
    for plat, hosts in HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return plat
    return ""


def _keys(now: float) -> tuple[str, str]:
    return time.strftime("%Y-%m-%d", time.localtime(now)), time.strftime("%Y-%m-%dT%H", time.localtime(now))


def resting(plat: str, now: float | None = None) -> tuple[float, str]:
    """(until, reason) when the site is resting, else (0, '')."""
    now = time.time() if now is None else now
    r = (_load().get("rest") or {}).get(plat) or {}
    until = float(r.get("until", 0) or 0)
    return (until, r.get("reason", "")) if until > now else (0.0, "")


def check(url: str, n: int = 1, now: float | None = None) -> None:
    """Before opening n pages on a site: raises Resting when it rests or its budget for now is used."""
    plat = platform_of(url)
    if not plat or not enabled():
        return
    now = time.time() if now is None else now
    until, reason = resting(plat, now)
    if until:
        raise Resting(f"{LABEL[plat]} is resting until {time.strftime('%d %b %H:%M', time.localtime(until))} "
                      f"(safe mode — {reason})")
    day, hour = _keys(now)
    d = _load()
    used_d = int(((d.get("day") or {}).get(day) or {}).get(plat, 0))
    used_h = int(((d.get("hour") or {}).get(hour) or {}).get(plat, 0))
    if used_d + n > PER_DAY[plat]:
        raise Resting(f"{LABEL[plat]}: today's safe-mode limit of {PER_DAY[plat]} pages is used — "
                      f"continues tomorrow (what is missing is filled with the API if it is set up)")
    if used_h + n > PER_HOUR[plat]:
        raise Resting(f"{LABEL[plat]}: this hour's safe-mode limit of {PER_HOUR[plat]} pages is used — continues next hour")


def spend(url: str, n: int = 1, now: float | None = None) -> None:
    plat = platform_of(url)
    if not plat:
        return
    now = time.time() if now is None else now
    day, hour = _keys(now)
    with _LOCK:
        d = _load()
        for key, k in (("day", day), ("hour", hour)):
            bucket = d.setdefault(key, {})
            for old in [x for x in bucket if x < k][:-1]:      # keep the previous day / hour, drop older ones
                bucket.pop(old, None)
            bucket.setdefault(k, {})[plat] = int(bucket.setdefault(k, {}).get(plat, 0)) + n
        _save(d)


def rest(plat: str, reason: str, hours: float | None = None) -> float:
    """Leave a site alone for a while (and tell the team)."""
    hours = REST_HOURS.get(plat, 24) if hours is None else hours
    until = time.time() + hours * 3600
    with _LOCK:
        d = _load()
        d.setdefault("rest", {})[plat] = {"until": until, "reason": reason[:160], "since": time.time()}
        _save(d)
    try:
        from .captcha import notify
        notify(f"⛔ Safe mode: {LABEL.get(plat, plat)} showed a block ({reason[:120]}). It is left alone until "
               f"{time.strftime('%d %b %H:%M', time.localtime(until))} so the block ends sooner. "
               "Please check that account in the app on your phone.")
    except Exception:  # noqa: BLE001
        pass
    return until


def release(plat: str) -> dict:
    with _LOCK:
        d = _load()
        (d.get("rest") or {}).pop(plat, None)
        _save(d)
    if plat == "google":
        try:
            from .news import clear_google_block
            clear_google_block()
        except Exception:  # noqa: BLE001
            pass
    return status()


def detect(url: str, body_text: str) -> str:
    """The platform's block / limit page? Returns what was seen ('' = fine)."""
    plat = platform_of(url)
    if not plat or plat == "google":
        return ""
    low_url = (url or "").lower()
    for m in BLOCK_URL.get(plat, ()):
        if m in low_url:
            return f"sent to {m.strip('/')}"
    txt = (body_text or "")[:4000].lower()
    for t in BLOCK_TEXT.get(plat, ()):
        if t in txt:
            return f'"{t}"'
    return ""


def min_schedule_hours(kind: str, payload: dict) -> int:
    """Safe mode: how often a schedule may run at most (0 = no limit). Searches with the accounts or on Google's
    search pages: every 6 hours at most; the multi-source news engine (no accounts, no search pages): any."""
    if not enabled():
        return 0
    opt = (payload or {}).get("options") or {}
    if kind == "news" and opt.get("engine") == "multi" and opt.get("target") != "social":
        return 0
    return MIN_SCHEDULE_HOURS


def status() -> dict:
    now = time.time()
    day, hour = _keys(now)
    d = _load()
    out = {"enabled": bool(d.get("enabled", True)), "platforms": []}
    for p in PLATFORMS:
        until, reason = resting(p, now)
        if p == "google":
            from .news import google_blocked_until
            g = google_blocked_until()
            if g > until:
                until, reason = g, "Google answered 403 (this connection searched too much)"
        out["platforms"].append({
            "id": p, "label": LABEL[p], "today": int(((d.get("day") or {}).get(day) or {}).get(p, 0)),
            "per_day": PER_DAY[p], "this_hour": int(((d.get("hour") or {}).get(hour) or {}).get(p, 0)),
            "per_hour": PER_HOUR[p], "resting_until": int(until), "reason": reason})
    return out
