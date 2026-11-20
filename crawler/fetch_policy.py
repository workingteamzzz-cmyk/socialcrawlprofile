"""How articles may be fetched when a site refuses the normal request — set by an admin (Live Stream → settings),
applied in every process that reads articles (the stream's worker, News Search, Bulk links).

The team's article reader (medmas, used as is) tries, in order: a normal browser request → the same request as
"Googlebot" → cloudscraper (a tool made to get past Cloudflare's bot check) → the page's AMP version. The second
and third are switched here without changing the engine:
- googlebot off: that step sends the normal browser headers again (a plain retry) instead of claiming to be Google;
- cloudscraper off: the engine finds no cloudscraper module and moves on to AMP.
Both default to on (what the app always did). Live Stream counts which step got each article, per day, so the
admin can see what turning one off would cost before doing it.
"""
from __future__ import annotations

import sys
import threading

_LOCK = threading.Lock()
_ORIG: dict = {}


def apply(googlebot: bool = True, cloudscraper: bool = True) -> None:
    from .medmas import extract
    with _LOCK:
        if "gb" not in _ORIG:
            _ORIG["gb"] = dict(extract.GOOGLEBOT)
        extract.GOOGLEBOT = dict(_ORIG["gb"]) if googlebot else dict(extract.HEADERS)
        if cloudscraper:
            if sys.modules.get("cloudscraper", 0) is None:
                del sys.modules["cloudscraper"]          # importable again
        else:
            sys.modules["cloudscraper"] = None           # "import cloudscraper" fails -> the engine skips that step


def apply_settings(cfg: dict | None = None) -> None:
    if cfg is None:
        try:
            from . import stream
            cfg = stream.settings()
        except Exception:  # noqa: BLE001
            cfg = {}
    apply(bool(cfg.get("fetch_googlebot", True)), bool(cfg.get("fetch_cloudscraper", True)))
