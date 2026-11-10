"""API keys for the API scraper — kept on the server only (file 0600), never sent back to the dashboard in full."""
from __future__ import annotations

import json
import os


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
            "default_budget": int(d.get("default_budget", 200))}


def save(api_key: str | None = None, twitter_api_key: str | None = None, default_budget: int | None = None,
         clear_api_key: bool = False, clear_twitter: bool = False) -> dict:
    d = load()
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
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return public()
