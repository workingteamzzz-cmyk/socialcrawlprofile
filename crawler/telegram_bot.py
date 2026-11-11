"""Telegram bot: tells the team what happens and lets them tap a button — no data leaves the server.

Messages (to every chat in TELEGRAM_CHAT_ID):
- a verification ("I'm not a robot") is waiting -> buttons "Solve it" (opens the dashboard) and "Skip";
- a run finished / failed -> how many results + "Open dashboard";
Commands (only from the chats in TELEGRAM_CHAT_ID; anyone else is told their chat id and nothing more):
  /status  what runs now, progress, verification waiting, API credits used today
  /schedules  the schedules, each with a "Run now" button
  /stop  stop the current run (asks to confirm)
  /help
Files and results are NOT sent to Telegram — only counts and the dashboard link, which needs a sign-in.

Setup: create a bot with @BotFather -> TELEGRAM_BOT_TOKEN; send /start to it and it answers with your chat id ->
TELEGRAM_CHAT_ID (several: comma separated; a group's id starts with -). PUBLIC_URL = the dashboard address.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

_STATE = {"thread": None, "offset": 0, "last_error": ""}


def token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def chats() -> list[str]:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]


def _base() -> str:
    return os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/") + f"/bot{token()}"


def call(method: str, payload: dict, timeout: float = 15):
    req = urllib.request.Request(f"{_base()}/{method}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def keyboard(buttons) -> dict | None:
    """[[("Text", "https://…" | "cb:data"), …], …] -> Telegram inline keyboard (URL buttons only for http(s))."""
    rows = []
    for row in buttons or []:
        out = []
        for text, target in row:
            if str(target).startswith(("http://", "https://")):
                out.append({"text": text, "url": target})
            elif target:
                out.append({"text": text, "callback_data": str(target)[:60]})
        if out:
            rows.append(out)
    return {"inline_keyboard": rows} if rows else None


def dashboard_button(label: str = "🔗 Open dashboard"):
    link = os.environ.get("PUBLIC_URL", "").strip()
    return [(label, link)] if link.startswith(("http://", "https://")) else []


def send(text: str, buttons=None, chat: str | None = None, background: bool = True):
    """Send to one chat or to every configured chat. Never raises."""
    if not token():
        return
    targets = [chat] if chat else chats()

    def go():
        for c in targets:
            body = {"chat_id": c, "text": text[:4000], "disable_web_page_preview": True}
            kb = keyboard(buttons)
            if kb:
                body["reply_markup"] = kb
            try:
                call("sendMessage", body)
            except Exception:  # noqa: BLE001
                pass
    if background:
        threading.Thread(target=go, daemon=True).start()
    else:
        go()


# ---------- what the bot answers ----------
def _status_text() -> str:
    from .captcha import CAPTCHA
    from .job import JOB
    s = JOB.state(since=10**9) if hasattr(JOB, "state") else {}
    names = {"profiles": "Profile collection", "keywords": "Keyword search", "news": "News search",
             "apiscrape": "API scraper", "decode": "Fixing Google News links", "login": "Login mode"}
    if s.get("running"):
        line = f"▶️ {names.get(s.get('kind'), s.get('kind') or 'A job')} is running"
        if s.get("total"):
            line += f" — {s.get('done', 0)}/{s['total']}"
        if s.get("current"):
            line += f"\n   {s['current']}"
    else:
        line = "✅ Nothing is running."
        if s.get("error"):
            line += f"\nLast error: {s['error'][:200]}"
    cap = CAPTCHA.info()
    if cap.get("pending"):
        line += f"\n🧩 A verification is waiting: {cap.get('label')}"
    try:
        from .apiscraper import settings
        u = settings.usage()
        line += f"\n💳 API credits today: {u['today']}" + (f" (left: {settings.credits_left()})"
                                                          if settings.credits_left() is not None else "")
    except Exception:  # noqa: BLE001
        pass
    return line


def handle(update: dict) -> None:
    """One update from Telegram: a command message or a button press."""
    from .captcha import CAPTCHA
    from .job import JOB
    msg = update.get("message") or {}
    cbq = update.get("callback_query") or {}
    chat = str(((msg or cbq.get("message") or {}).get("chat") or {}).get("id", ""))
    who = (msg.get("from") or cbq.get("from") or {})
    name = who.get("username") or who.get("first_name") or str(who.get("id", "?"))
    if not chat:
        return
    if chat not in chats():
        if msg.get("text", "").startswith("/start"):
            send(f"This bot only answers the team's chats. Your chat id is {chat} — an admin adds it to "
                 f"TELEGRAM_CHAT_ID on the server.", chat=chat, background=False)
        return
    if cbq:
        data = cbq.get("data", "")
        answer = "OK"
        if data == "skip":
            answer = CAPTCHA.act({"type": "skip"}) or "Skipped — the run continues without it"
        elif data == "stop?":
            send("Stop the current run? What was collected is kept.", [[("⏹ Yes, stop", "stop!"), ("Cancel", "noop")]],
                 chat=chat)
            answer = ""
        elif data == "stop!":
            if JOB.running:
                JOB.stop_flag.set()
                JOB.log(f"⏹ Stop requested from Telegram by {name}")
                answer = "Stopping — the results so far are saved"
            else:
                answer = "Nothing is running"
        elif data.startswith("run:"):
            from . import schedule
            err = schedule.get().run_now(data[4:])
            if not err:
                JOB.log(f"▶️ Schedule started from Telegram by {name}")
            answer = err or "Started ✅"
        try:
            call("answerCallbackQuery", {"callback_query_id": cbq.get("id"), "text": answer[:190]})
        except Exception:  # noqa: BLE001
            pass
        if answer and answer != "OK":
            send(answer, chat=chat)
        return
    text = (msg.get("text") or "").strip()
    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd in ("/start", "/help"):
        send("Post Link Collector bot\n/status — what runs now\n/schedules — run a schedule now\n/stop — stop the run\n"
             "I also message you when a verification is waiting or a run finishes.", [dashboard_button()], chat=chat)
    elif cmd == "/status":
        from .captcha import CAPTCHA as C
        btns = [dashboard_button()]
        if C.info().get("pending"):
            btns = [dashboard_button("🧩 Solve it"), [("⏭ Skip", "skip")]]
        elif JOB.running:
            btns.append([("⏹ Stop", "stop?")])
        send(_status_text(), btns, chat=chat)
    elif cmd == "/schedules":
        from . import schedule
        items = schedule.get().list()
        if not items:
            send("No schedules yet — make one on the dashboard.", [dashboard_button()], chat=chat)
            return
        lines, btns = [], []
        for s in items[:15]:
            lines.append(f"• {s['name']} — {s.get('describe') or ''}".rstrip(" —")
                         + (f"\n   last: {s['last_result']}" if s.get("last_result") else ""))
            btns.append([(f"▶️ Run “{s['name'][:30]}” now", f"run:{s['id']}")])
        send("Schedules:\n" + "\n".join(lines), btns, chat=chat)
    elif cmd == "/stop":
        send("Stop the current run? What was collected is kept." if JOB.running else "Nothing is running.",
             [[("⏹ Yes, stop", "stop!"), ("Cancel", "noop")]] if JOB.running else None, chat=chat)


def poll_once(timeout: int = 50) -> int:
    """Read the updates since the last call and handle them; returns how many."""
    res = call("getUpdates", {"offset": _STATE["offset"], "timeout": timeout,
                              "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)
    n = 0
    for u in res.get("result") or []:
        _STATE["offset"] = max(_STATE["offset"], int(u.get("update_id", 0)) + 1)
        try:
            handle(u)
        except Exception:  # noqa: BLE001
            pass
        n += 1
    return n


def start() -> bool:
    """Start answering commands in the background (when TELEGRAM_BOT_TOKEN is set). Safe to call twice."""
    if not token() or _STATE["thread"]:
        return False

    def loop():
        while True:
            try:
                poll_once()
                _STATE["last_error"] = ""
            except urllib.error.HTTPError as e:
                _STATE["last_error"] = f"HTTP {e.code}"      # 409: another copy polls the same bot
                time.sleep(30 if e.code == 409 else 10)
            except Exception as e:  # noqa: BLE001
                _STATE["last_error"] = str(e)[:120]
                time.sleep(10)
    _STATE["thread"] = threading.Thread(target=loop, daemon=True, name="telegram-bot")
    _STATE["thread"].start()
    return True
