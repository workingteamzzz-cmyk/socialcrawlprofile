"""Home page: is everything set up and healthy? A checklist (like a security page) and today's numbers."""
from __future__ import annotations

import json
import os
import time


def _logins() -> dict:
    from .paths import LOGINS_FILE
    try:
        with open(LOGINS_FILE, encoding="utf-8") as f:
            return json.load(f).get("platforms") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _today_counts() -> dict:
    """Rows saved today in the reporting database, per kind."""
    try:
        from . import archive
        day = time.strftime("%Y-%m-%d")
        with archive._LOCK:
            c = archive._db()
            try:
                got = {r["kind"]: r["n"] for r in c.execute(
                    "SELECT kind, COUNT(*) n FROM items WHERE first_seen >= ? GROUP BY kind", (day,))}
            finally:
                c.close()
        return got
    except Exception:  # noqa: BLE001
        return {}


def build(server_mode: bool) -> dict:
    from . import safety, telegram_bot
    from .apiscraper import settings as api_settings
    checks = []

    def add(state, title, detail=""):
        checks.append({"state": state, "title": title, "detail": detail})

    url = telegram_bot.public_url() or os.environ.get("PUBLIC_URL", "")
    if server_mode:
        add("ok" if url.startswith("https://") else "bad", "HTTPS dashboard address" if url.startswith("https://")
            else "HTTPS not set", url or "Set the dashboard address (Settings → Telegram bot) / PUBLIC_URL")
    else:
        add("ok", "Runs on this computer only", "http://127.0.0.1 — not reachable from other devices")

    logins = _logins()
    signed = [p for p, v in logins.items() if v]
    add("ok" if signed else "warn", f"Social media signed in: {len(signed)} of {len(logins) or 6}",
        ", ".join(signed) if signed else "Log in / import cookies for the platforms you collect from")

    pub = api_settings.public()
    add("ok" if pub["api_key_set"] else "warn", "Scraping API key set" if pub["api_key_set"] else "No scraping API key",
        "Fills empty IDs / followers, the API Scraper" if pub["api_key_set"] else "Optional — Settings → API")
    if pub["api_key_set"]:
        left = api_settings.credits_left()
        add("ok" if pub["daily_cap"] or pub["monthly_cap"] else "warn",
            "API credit limit set" if pub["daily_cap"] or pub["monthly_cap"] else "No daily / monthly credit limit",
            f"{left} credits left today" if left is not None else "Settings → API")

    tg = telegram_bot.public()
    add("ok" if tg["token_set"] and tg["chats"] else "warn",
        "Telegram bot connected" if tg["token_set"] and tg["chats"] else "Telegram bot not connected",
        " · ".join(x for x in (tg.get("bot") or "", f"chats {tg['chats']}" if tg["chats"] else "Settings → Telegram") if x))

    sf = safety.status()
    add("ok" if sf["enabled"] else "bad", f"Safe mode on · {sf['level']}" if sf["enabled"] else "Safe mode is OFF",
        "budgets per site, block pages rested")
    resting = [p for p in sf["platforms"] if p["resting_until"]]
    add("warn" if resting else "ok", "Resting: " + ", ".join(p["label"] for p in resting) if resting
        else "No site is blocked or resting",
        "; ".join(f"{p['label']} until {time.strftime('%d %b %H:%M', time.localtime(p['resting_until']))}"
                  for p in resting) or "Google and every platform answer normally")

    try:
        from . import schedule
        items = schedule.get().list()
    except Exception:  # noqa: BLE001
        items = []
    active = [s for s in items if s.get("enabled")]
    nxt = min((s.get("next_run") or 0 for s in active if s.get("next_run")), default=0)
    add("ok" if active else "warn", f"{len(active)} schedule(s) running" if active else "No schedule yet",
        f"next at {time.strftime('%d %b %H:%M', time.localtime(nxt))}" if nxt else "Search pages → Schedule it")

    days = float(os.environ.get("RETENTION_DAYS", "30") or 0)
    add("ok", f"Result files kept {days:g} days" if days else "Result files kept forever",
        "the reporting database keeps everything")

    today = _today_counts()
    try:
        from .apiscraper import settings as s2
        used = s2.usage()
    except Exception:  # noqa: BLE001
        used = {"today": 0}
    return {"checks": checks, "passed": sum(1 for c in checks if c["state"] == "ok"), "total": len(checks),
            "stats": {"articles_today": today.get("online", 0), "posts_today": today.get("medsos", 0),
                      "credits_today": used.get("today", 0), "credits_cap": pub["daily_cap"],
                      "schedules": len(active), "next_run": int(nxt)}}
