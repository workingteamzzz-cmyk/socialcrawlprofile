"""The registered media (the media dictionary) as a database the admins edit: add / remove outlets, see how often each
publishes (tier) and whether the app can read it (access), a weekly access report, and logins for paid media.

- The list: APP_DATA/media_dictionary.txt ("domain,name" per line). Until an admin edits it, the bundled dictionary
  (~12,400 outlets) + the engine's core outlets are used; the first edit writes the full list there.
- Activity and access come from the live stream's last 7 days (stream.db): new articles found, articles read,
  failed, refused by robots.txt, too short (often a paywall), and the last error.
- Paid media: an admin pastes the cookies of a logged-in browser for that outlet (APP_DATA/stream/subscriptions.json,
  file mode 600, never shown again); every article of that outlet is then read with them — stream and News Search.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))
_LOCK = threading.RLock()
_CACHE: dict = {"t": 0.0, "stats": {}}
TIER_LABEL = {"active": "Active", "semi": "Semi-active", "rare": "Rare", "inactive": "Inactive", "dead": "Unreachable",
              "new": "Not checked yet"}
ACCESS_LABEL = {"ok": "Readable", "partial": "Partly readable", "blocked": "Cannot be read", "paywall": "Paywall?",
                "robots": "Refused by robots.txt", "unreachable": "Site unreachable", "quiet": "No new articles", "pending": "Not read yet",
                "excluded": "Excluded", "unknown": "Not checked yet"}


def _data() -> str:
    from .paths import APP_DATA
    return APP_DATA


def _file() -> str:
    return os.path.join(_data(), "media_dictionary.txt")


def clean_domain(v: str) -> str:
    v = re.sub(r"^https?://", "", str(v or "").strip().lower()).split("/")[0].split("?")[0].strip()
    v = v[4:] if v.startswith("www.") else v
    return v if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", v) else ""


# ---------- the list ----------
def registry() -> dict[str, str]:
    """{domain: name} — the admin's list if there is one, else the bundled dictionary + core outlets."""
    with _LOCK:
        out: dict[str, str] = {}
        path = _file()
        if not os.path.exists(path):
            from .medmas.media import CORE_MEDIA, MEDIA_DICT
            path = os.path.join(os.path.dirname(__file__), "medmas", "media_dictionary.txt")
            for d in CORE_MEDIA:
                out[d] = MEDIA_DICT.get(d, "")
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = re.split(r"[,;\t]", line.strip(), maxsplit=1)
                    d = clean_domain(parts[0])
                    if d:
                        out[d] = (parts[1].strip() if len(parts) > 1 else "") or out.get(d, "")
        except OSError:
            pass
        return out


def _write(reg: dict[str, str]) -> None:
    os.makedirs(_data(), exist_ok=True)
    tmp = _file() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for d in sorted(reg):
            f.write(f"{d},{reg[d]}\n" if reg[d] else f"{d}\n")
    os.replace(tmp, _file())


def add(text: str) -> dict:
    """Lines of "domain" or "domain, name" (URLs are fine). Returns what was added / already there / invalid."""
    added, known, bad = [], [], []
    with _LOCK:
        reg = registry()
        for line in str(text or "").splitlines()[:5000]:
            if not line.strip():
                continue
            parts = re.split(r"[,;\t|]", line.strip(), maxsplit=1)
            d = clean_domain(parts[0])
            if not d:
                bad.append(line.strip()[:80])
                continue
            if d in reg:
                known.append(d)
                if len(parts) > 1 and parts[1].strip():
                    reg[d] = parts[1].strip()[:80]
                continue
            reg[d] = parts[1].strip()[:80] if len(parts) > 1 else ""
            added.append(d)
        _write(reg)
    try:
        from . import stream
        stream.get().add_outlets(added)
    except Exception:  # noqa: BLE001
        pass
    _CACHE["t"] = 0
    return {"added": added, "already": known, "invalid": bad, "total": len(reg)}


def remove(domains: list[str]) -> dict:
    gone = []
    with _LOCK:
        reg = registry()
        for d in domains or []:
            d = clean_domain(d)
            if d in reg:
                reg.pop(d)
                gone.append(d)
        _write(reg)
    try:
        from . import stream
        stream.get().remove_outlets(gone)
    except Exception:  # noqa: BLE001
        pass
    _CACHE["t"] = 0
    return {"removed": gone, "total": len(reg)}


# ---------- paid media: logins ----------
def _subs_path() -> str:
    return os.path.join(_data(), "stream", "subscriptions.json")


def subscriptions() -> dict:
    try:
        with open(_subs_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_cookies(text: str, domain: str) -> str:
    """A browser's cookies in any common form -> one "a=b; c=d" header: the Cookie header itself, a cookie export
    (JSON list of {name, value, domain}) or a Netscape cookies.txt. Only cookies of this outlet are kept."""
    t = (text or "").strip()
    if not t:
        return ""
    pairs: list[tuple[str, str]] = []
    if t.startswith("[") or t.startswith("{"):
        try:
            data = json.loads(t)
            data = data.get("cookies", data) if isinstance(data, dict) else data
            for c in data:
                host = str(c.get("domain") or c.get("host") or domain).lstrip(".").lower()
                if host == domain or host.endswith("." + domain) or domain.endswith("." + host):
                    pairs.append((str(c["name"]), str(c["value"])))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("That cookie export could not be read.")
    elif "\t" in t:
        for line in t.splitlines():
            f = line.split("\t")
            if len(f) >= 7 and not line.startswith("#"):
                host = f[0].lstrip(".").lower()
                if host == domain or host.endswith("." + domain) or domain.endswith("." + host):
                    pairs.append((f[5], f[6]))
    else:
        t = re.sub(r"^cookie:\s*", "", t, flags=re.I)
        for part in t.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    pairs.append((k.strip(), v.strip()))
    if not pairs:
        raise ValueError(f"No cookies of {domain} were found in what you pasted.")
    return "; ".join(f"{k}={v}" for k, v in dict(pairs).items())[:16000]


def set_subscription(domain: str, cookies: str | None, note: str = "", clear: bool = False) -> dict:
    d = clean_domain(domain)
    if not d:
        raise ValueError("Which outlet? (its domain, e.g. kompas.id)")
    with _LOCK:
        subs = subscriptions()
        if clear:
            subs.pop(d, None)
        else:
            subs[d] = {"cookie": parse_cookies(cookies or "", d), "added": int(time.time()), "note": note[:120]}
        os.makedirs(os.path.dirname(_subs_path()), exist_ok=True)
        tmp = _subs_path() + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(subs, f)
        os.replace(tmp, _subs_path())
    install()
    return {"domain": d, "subscribed": d in subs}


def cookie_for(url: str) -> str:
    host = re.sub(r"^https?://", "", url or "").split("/")[0].split(":")[0].lower()
    for d, v in _SUBS.items():
        if host == d or host.endswith("." + d):
            return v
    return ""


_SUBS: dict[str, str] = {}


class _CookieRequests:
    """Stands in for `requests` inside the article reader: a page of a subscribed outlet is fetched with its login
    cookies; everything else is passed through untouched."""

    def __init__(self, real):
        self._real = real

    def get(self, url, *a, **k):
        c = cookie_for(url) if _SUBS else ""
        if c:
            h = dict(k.get("headers") or {})
            h["Cookie"] = c
            k["headers"] = h
        return self._real.get(url, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install() -> None:
    """Load the logins and put the cookie-aware stand-in into the article reader (idempotent)."""
    global _SUBS
    _SUBS = {d: v.get("cookie", "") for d, v in subscriptions().items() if v.get("cookie")}
    try:
        from .medmas import extract
        if not isinstance(extract.requests, _CookieRequests):
            extract.requests = _CookieRequests(extract.requests)
    except Exception:  # noqa: BLE001
        pass


# ---------- activity + access per outlet (last 7 days of the stream) ----------
def _stream_db() -> str:
    return os.path.join(_data(), "stream", "stream.db")


def stats(force: bool = False) -> dict:
    """{domain: {...}} — cached 5 minutes (the stream database can hold a million articles)."""
    if not force and time.time() - _CACHE["t"] < 300:
        return _CACHE["stats"]
    out: dict = {}
    path = _stream_db()
    if os.path.exists(path):
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            d7 = (datetime.now(WIB) - timedelta(days=6)).strftime("%Y-%m-%d")
            since = time.time() - 7 * 86400

            def row(d):
                return out.setdefault(d, {"found7": 0, "last_day": "", "success": 0, "failed": 0, "filtered": 0,
                                          "skipped": 0, "error": "", "tier": "new", "fails": 0, "checked": 0,
                                          "has_sitemap": False, "rss": 0})
            for r in c.execute("SELECT domain, SUM(n) n, MAX(day) last FROM domain_days WHERE day >= ? GROUP BY domain",
                               (d7,)):
                x = row(r["domain"])
                x["found7"], x["last_day"] = r["n"] or 0, r["last"] or ""
            for r in c.execute("SELECT domain, status, COUNT(*) n FROM articles WHERE discovered >= ? "
                               "GROUP BY domain, status", (since,)):
                if r["status"] in ("success", "failed", "filtered", "skipped"):
                    row(r["domain"])[r["status"]] = r["n"]
            errs: dict = {}
            for r in c.execute("SELECT domain, error, COUNT(*) n FROM articles WHERE discovered >= ? AND "
                               "status='failed' AND error != '' GROUP BY domain, error", (since,)):
                errs.setdefault(r["domain"], Counter())[r["error"]] += r["n"]
            for d, cnt in errs.items():
                row(d)["error"] = cnt.most_common(1)[0][0]
            for r in c.execute("SELECT domain, kind, tier, fails, checked, url FROM sources"):
                x = row(r["domain"])
                if r["kind"] == "sitemap":
                    x["tier"] = r["tier"] or "new"
                    x["fails"], x["checked"] = r["fails"] or 0, r["checked"] or 0
                    x["has_sitemap"] = bool(r["url"])
                else:
                    x["rss"] += 1
        except sqlite3.Error:
            pass
        finally:
            c.close()
    _CACHE.update(t=time.time(), stats=out)
    return out


def access(x: dict | None, excluded: bool = False) -> str:
    if excluded:
        return "excluded"
    if not x or not x.get("checked") and not x.get("found7"):
        return "unknown"
    ok, bad, short, robots = x["success"], x["failed"], x["filtered"], x["skipped"]
    tried = ok + bad + short
    if robots and not ok:
        return "robots"
    if x.get("tier") == "dead" and not ok:
        return "unreachable"
    if tried == 0:
        return "pending" if x.get("found7") else "quiet"
    if ok / tried >= 0.8:
        return "ok"
    if short / tried >= 0.5 and ok / tried < 0.5:
        return "paywall"
    if ok / tried >= 0.2:
        return "partial"
    return "blocked"


def listing(q: str = "", tier: str = "", acc: str = "", page: int = 1, size: int = 50, sort: str = "activity") -> dict:
    reg, st = registry(), stats()
    try:
        from . import stream
        excl = set(stream.settings().get("exclude_domains") or [])
    except Exception:  # noqa: BLE001
        excl = set()
    subs = subscriptions()
    rows = []
    for d, name in reg.items():
        x = st.get(d) or {}
        a = access(x or None, d in excl)
        t = x.get("tier", "new") if x else "new"
        rows.append({"domain": d, "name": name, "tier": t, "access": a, "found7": x.get("found7", 0),
                     "per_day": round(x.get("found7", 0) / 7, 1), "last_day": x.get("last_day", ""),
                     "read": x.get("success", 0), "failed": x.get("failed", 0), "short": x.get("filtered", 0),
                     "robots": x.get("skipped", 0), "error": x.get("error", ""), "rss": x.get("rss", 0),
                     "sitemap": x.get("has_sitemap", False), "subscribed": d in subs})
    counts = {"total": len(rows), "tiers": Counter(r["tier"] for r in rows), "access": Counter(r["access"] for r in rows)}
    ql = (q or "").strip().lower()
    if ql:
        rows = [r for r in rows if ql in r["domain"] or ql in (r["name"] or "").lower()]
    if tier:
        rows = [r for r in rows if r["tier"] == tier]
    if acc:
        rows = [r for r in rows if r["access"] == acc]
    if sort == "name":
        rows.sort(key=lambda r: (r["domain"] != ql, r["domain"]))
    else:
        rows.sort(key=lambda r: (r["domain"] != ql, -r["found7"], r["domain"]))       # the exact domain first
    size = max(10, min(int(size or 50), 500))
    page = max(1, int(page or 1))
    return {"total": len(rows), "page": page, "size": size, "counts": counts,
            "rows": rows[(page - 1) * size: page * size]}


# ---------- the weekly access report ----------
def report(out_dir: str) -> dict:
    """Every registered outlet: readable or not, how often it publishes, the error seen — one Excel (sheets: Summary,
    Cannot be read, Readable, All media) + a short text for Telegram."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    _CACHE["t"] = 0
    allrows, p = [], 1
    while True:
        part = listing(size=500, page=p)
        allrows += part["rows"]
        if p * 500 >= part["total"]:
            break
        p += 1
    cnt = Counter(r["access"] for r in allrows)
    tiers = Counter(r["tier"] for r in allrows)
    wb = Workbook()
    head_fill, head_font = PatternFill("solid", fgColor="FF15677B"), Font(bold=True, color="FFFFFFFF")
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Media access report", datetime.now(WIB).strftime("%d %b %Y %H:%M WIB"), "last 7 days of the stream"])
    ws.append([])
    ws.append(["Access", "Outlets"])
    for k in ("ok", "partial", "blocked", "paywall", "robots", "unreachable", "pending", "quiet", "excluded", "unknown"):
        ws.append([ACCESS_LABEL[k], cnt.get(k, 0)])
    ws.append([])
    ws.append(["Activity", "Outlets"])
    for k in ("active", "semi", "rare", "inactive", "dead", "new"):
        ws.append([TIER_LABEL[k], tiers.get(k, 0)])
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 28, 14
    cols = ["Domain", "Name", "Activity", "New articles (7 days)", "Per day", "Last article", "Access", "Read",
            "Could not read", "Too short (paywall?)", "Refused by robots.txt", "Most common error", "RSS feeds",
            "Sitemap found", "Paid login set"]

    def sheet(title, rows):
        w = wb.create_sheet(title)
        w.append(cols)
        for c in w[1]:
            c.fill, c.font = head_fill, head_font
        for r in rows:
            w.append([r["domain"], r["name"], TIER_LABEL.get(r["tier"], r["tier"]), r["found7"], r["per_day"],
                      r["last_day"], ACCESS_LABEL.get(r["access"], r["access"]), r["read"], r["failed"], r["short"],
                      r["robots"], r["error"], r["rss"], "yes" if r["sitemap"] else "", "yes" if r["subscribed"] else ""])
        for i, wd in enumerate([28, 24, 14, 12, 9, 12, 20, 8, 10, 12, 12, 22, 9, 9, 10], 1):
            w.column_dimensions[w.cell(row=1, column=i).column_letter].width = wd
        w.freeze_panes = "A2"
        w.auto_filter.ref = f"A1:O{max(1, w.max_row)}"
    bad = [r for r in allrows if r["access"] in ("blocked", "paywall", "robots", "partial")]
    sheet("Cannot be read", sorted(bad, key=lambda r: -r["found7"]))
    sheet("Readable", [r for r in allrows if r["access"] == "ok"])
    sheet("All media", allrows)
    os.makedirs(out_dir, exist_ok=True)
    name = f"Media_Access_Report_{datetime.now(WIB):%Y%m%d_%H%M}.xlsx"
    tmp = os.path.join(out_dir, name + ".tmp")
    wb.save(tmp)
    os.replace(tmp, os.path.join(out_dir, name))
    top_bad = [r["domain"] for r in sorted(bad, key=lambda r: -r["found7"]) if r["found7"]][:10]
    text = (f"📰 Weekly media access report\n"
            f"✅ Readable: {cnt.get('ok', 0)}  ·  ⚠ Partly: {cnt.get('partial', 0)}\n"
            f"⛔ Cannot be read: {cnt.get('blocked', 0)}  ·  💳 Paywall?: {cnt.get('paywall', 0)}  ·  "
            f"🤖 robots.txt: {cnt.get('robots', 0)}\n"
            f"📴 Unreachable: {cnt.get('unreachable', 0)}  ·  💤 No new articles: {cnt.get('quiet', 0)}\n"
            f"Active {tiers.get('active', 0)} · Semi {tiers.get('semi', 0)} · Rare {tiers.get('rare', 0)} · "
            f"Inactive {tiers.get('inactive', 0)}"
            + (f"\nBusiest outlets we cannot read: {', '.join(top_bad)}" if top_bad else ""))
    return {"file": name, "text": text, "access": dict(cnt), "tiers": dict(tiers)}


def send_report() -> dict:
    """Make the report, keep it in Exports and tell the team on Telegram (with the download link if the dashboard
    address is set)."""
    from .paths import OUTPUT_DIR
    r = report(OUTPUT_DIR)
    try:
        from . import telegram_bot
        from .captcha import notify
        url = telegram_bot.public_url()
        notify(r["text"] + (f"\n⬇ {url.rstrip('/')}/download/{r['file']}" if url else f"\nFile: {r['file']} (Exports)"))
    except Exception:  # noqa: BLE001
        pass
    return r
