"""API keys for the API scraper — kept on the server only (file 0600), never sent back to the dashboard in full."""
from __future__ import annotations

import json
import os
import time


def _path() -> str:
    from ..paths import APP_DATA
    return os.path.join(APP_DATA, "api_scraper.json")


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(v: str) -> str:
    return ("…" + v[-4:]) if v else ""


def public() -> dict:
    """What the dashboard may see: whether each key is set, its last 4 characters, the default credit cap."""
    d = load()
    return {"api_key_set": bool(d.get("api_key")), "api_key_tail": _tail(d.get("api_key", "")),
            "twitter_set": bool(d.get("twitter_api_key")), "twitter_tail": _tail(d.get("twitter_api_key", "")),
            "default_budget": int(d.get("default_budget", 200)),
            "daily_cap": int(d.get("daily_cap", 0)), "monthly_cap": int(d.get("monthly_cap", 0))}


def save(api_key: str | None = None, twitter_api_key: str | None = None, default_budget: int | None = None,
         clear_api_key: bool = False, clear_twitter: bool = False,
         daily_cap: int | None = None, monthly_cap: int | None = None) -> dict:
    d = load()
    for k, v in (("daily_cap", daily_cap), ("monthly_cap", monthly_cap)):
        if v is not None:
            d[k] = max(0, int(v))
    if api_key:
        d["api_key"] = api_key.strip()
    if twitter_api_key:
        d["twitter_api_key"] = twitter_api_key.strip()
    if clear_api_key:
        d.pop("api_key", None)
    if clear_twitter:
        d.pop("twitter_api_key", None)
    if default_budget is not None:
        d["default_budget"] = max(0, int(default_budget))
    _write(_path(), d)
    return public()


def _write(p: str, d) -> None:
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ---------- credits used (for the daily / monthly limits) ----------
def _usage_path() -> str:
    return os.path.join(os.path.dirname(_path()), "api_usage.json")


def _usage() -> dict:
    try:
        with open(_usage_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record_usage(credits: int, twitter: int = 0, now: float | None = None) -> None:
    """Add a run's requests to today's count (1 request ~ 1 credit). Days older than 400 are dropped."""
    if credits <= 0 and twitter <= 0:
        return
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    d = _usage()
    e = d.setdefault(day, {"credits": 0, "twitter": 0, "runs": 0})
    e["credits"] += int(credits); e["twitter"] += int(twitter); e["runs"] += 1
    for k in sorted(d)[:-400]:
        d.pop(k, None)
    _write(_usage_path(), d)


def usage(now: float | None = None) -> dict:
    """Credits used today and this month (non-Twitter, which the limits and the credit cap count)."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    d = _usage()
    return {"today": int(d.get(day, {}).get("credits", 0)),
            "month": sum(int(v.get("credits", 0)) for k, v in d.items() if k[:7] == day[:7]),
            "twitter_today": int(d.get(day, {}).get("twitter", 0))}


def credits_left(now: float | None = None) -> int | None:
    """What the daily / monthly limits still allow (None = no limit set)."""
    pub, u = public(), usage(now)
    left = [cap - used for cap, used in ((pub["daily_cap"], u["today"]), (pub["monthly_cap"], u["month"])) if cap]
    return max(0, min(left)) if left else None
