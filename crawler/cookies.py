"""Logins for a server without a screen: import the cookies of a browser where you are signed in.

Accepted formats (paste or upload the text):
- JSON list exported by a cookie extension such as "Cookie-Editor" / "EditThisCookie"
  ([{"name", "value", "domain", "path", "expirationDate", "httpOnly", "secure", "sameSite"}, ...]);
- Playwright storage state ({"cookies": [...]});
- Netscape cookies.txt (domain, flag, path, secure, expiry, name, value — tab separated).

Only cookies of the supported platforms are kept; everything else in the file is ignored.
"""
from __future__ import annotations

import json
import time

# Sites whose cookies are kept (the platforms, and Google/YouTube for news and video pages).
KEEP_DOMAINS = ("instagram.com", "x.com", "twitter.com", "tiktok.com", "threads.com", "threads.net",
                "facebook.com", "youtube.com", "google.com")

_SAME_SITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


class CookieError(ValueError):
    pass


def _keep(domain: str) -> bool:
    d = (domain or "").lstrip(".").lower()
    return any(d == k or d.endswith("." + k) for k in KEEP_DOMAINS)


def _norm(c: dict) -> dict | None:
    name, value, domain = c.get("name"), c.get("value"), c.get("domain") or c.get("host") or ""
    if not name or value is None or not _keep(domain):
        return None
    out = {"name": str(name), "value": str(value), "domain": str(domain), "path": str(c.get("path") or "/"),
           "httpOnly": bool(c.get("httpOnly", False)), "secure": bool(c.get("secure", False))}
    exp = c.get("expirationDate", c.get("expires", c.get("expiry")))
    if not c.get("session") and isinstance(exp, (int, float)) and exp > 0:
        out["expires"] = float(exp)
    ss = _SAME_SITE.get(str(c.get("sameSite") or "").lower())
    if ss == "None" and not out["secure"]:
        ss = None                                  # browsers refuse SameSite=None without Secure
    if ss:
        out["sameSite"] = ss
    return out


def parse_cookies(text: str) -> list[dict]:
    """Playwright cookie dicts from any of the accepted formats. Raises CookieError with a readable message."""
    text = (text or "").strip()
    if not text:
        raise CookieError("Paste the exported cookies (JSON or cookies.txt).")
    raw: list = []
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise CookieError(f"That is not valid JSON ({e.msg} at line {e.lineno}).") from None
        if isinstance(data, dict):
            data = data.get("cookies", [])
        if not isinstance(data, list):
            raise CookieError("Expected a list of cookies.")
        raw = [c for c in data if isinstance(c, dict)]
    else:
        for line in text.splitlines():
            http_only = line.startswith("#HttpOnly_")
            if http_only:
                line = line[len("#HttpOnly_"):]
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, path, secure, expiry, name, value = parts[:7]
            try:
                exp = float(expiry)
            except ValueError:
                exp = 0
            raw.append({"domain": domain, "path": path, "secure": secure.upper() == "TRUE", "expires": exp,
                        "name": name, "value": value, "httpOnly": http_only})
    now = time.time()
    out = [n for n in (_norm(c) for c in raw) if n and (n.get("expires") is None or n["expires"] > now)]
    if not out:
        raise CookieError("No usable cookies for Instagram, X, TikTok, Threads, Facebook, YouTube or Google were found "
                          "(or they have expired). Export them while signed in on that site.")
    return out
