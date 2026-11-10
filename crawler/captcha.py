"""A person solves a verification ("I'm not a robot") from the dashboard while the browser runs on a server.

When a page asks for a verification and there is no screen, the job pauses: the dashboard shows a live picture of
the page; a click on the picture is a click on the same spot of the page, typed text goes to the page, and the job
continues on its own once the page is through. Nobody in time → that search is skipped, as before.
Optional: CAPTCHA_WEBHOOK_URL gets {"text": ...} when a verification is waiting (Slack/Teams/n8n/Telegram bridge),
with PUBLIC_URL as the link to open.

Browser objects belong to the job's thread, so the web requests only queue actions; the job thread performs them
and takes the pictures.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request


class CaptchaGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.actions: queue.Queue = queue.Queue()
        self.pending = False
        self.label = ""
        self.since = 0.0
        self.until = 0.0
        self.shot = b""
        self.focus_shot = b""
        self.focus = None            # (x, y, w, h) of the check itself on the page, when it can be found
        self.version = 0
        self.size = (0, 0)
        self.last_view = 0.0

    # ---------- web side ----------
    def info(self) -> dict:
        with self.lock:
            return {"pending": self.pending, "label": self.label, "since": int(self.since), "until": int(self.until),
                    "version": self.version,
                    "width": self.size[0], "height": self.size[1],
                    "focus": list(self.focus) if self.focus else None}

    def screenshot(self, view: str = "full") -> tuple[bytes, tuple]:
        """The latest picture and the part of the page it shows (x, y, w, h) — "focus" is a close-up of the check
        itself (for phones), "full" the whole page."""
        with self.lock:
            self.last_view = time.time()
            if view == "focus" and self.focus_shot and self.focus:
                return self.focus_shot, self.focus
            return self.shot, (0, 0, self.size[0], self.size[1])

    def act(self, action: dict) -> str | None:
        if not self.pending:
            return "No verification is waiting."
        kind = action.get("type")
        if kind not in ("click", "type", "key", "skip", "refresh", "scroll"):
            return "Unknown action."
        self.last_view = time.time()
        self.actions.put(action)
        return None

    # ---------- job side ----------
    def solve(self, crawler, is_solved, label: str, wait_s: float | None = None) -> bool:
        """Pause the job until is_solved() — a person solving it from the dashboard — or the time runs out.
        Raises Stopped if the user presses Stop."""
        wait_s = float(os.environ.get("CAPTCHA_WAIT_MINUTES", "10")) * 60 if wait_s is None else wait_s
        page = crawler.page
        with self.lock:
            self.pending, self.label, self.since = True, label, time.time()
            self.until = self.since + wait_s
            self.shot, self.focus_shot, self.focus, self.size = b"", b"", None, (0, 0)
        while not self.actions.empty():
            self.actions.get_nowait()
        crawler.log(f"   🧩 {label} is asking for a verification — open the dashboard to solve it "
                    f"(waiting up to {wait_s / 60:.0f} min; the job continues by itself afterwards)")
        _notify(label)
        last_shot, dirty = 0.0, True
        try:
            end = time.time() + wait_s
            while time.time() < end:
                crawler._check_stop()
                if is_solved():
                    crawler.log("   ✅ Verification done — continuing")
                    return True
                try:
                    act = self.actions.get(timeout=0.4)
                except queue.Empty:
                    act = None
                if act:
                    if act["type"] == "skip":
                        crawler.log("   Verification skipped from the dashboard")
                        return False
                    _perform(page, act)
                    dirty = True
                    page.wait_for_timeout(700)               # let the page react before the next picture
                watched = time.time() - self.last_view < 15
                if dirty or (watched and time.time() - last_shot > 1.5) or not self.shot:
                    self._picture(page)
                    last_shot, dirty = time.time(), False
            crawler.log("   ⚠ Nobody solved the verification in time — this search is skipped")
            return False
        finally:
            with self.lock:
                self.pending, self.label = False, ""

    def _picture(self, page):
        try:
            png = page.screenshot(type="png")
            vp = page.viewport_size or {"width": 1280, "height": 900}
            focus = _find_check(page, vp["width"], vp["height"])
            close = page.screenshot(type="png", clip=dict(zip(("x", "y", "width", "height"), focus))) if focus else b""
        except Exception:  # noqa: BLE001  (page busy navigating)
            return
        with self.lock:
            self.shot, self.size = png, (vp["width"], vp["height"])
            self.focus_shot, self.focus = close, focus
            self.version += 1


_FIND_CHECK_JS = r"""() => {
  // Where the check is: the image challenge first, then the tick box, then any captcha box (Google, reCAPTCHA,
  // hCaptcha, Cloudflare, TikTok's slider).
  const groups = [
    'iframe[src*="recaptcha"][src*="bframe"], iframe[title*="challenge" i]',
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare"]',
    '#captcha-form, #captcha_container, .captcha_verify_container, .captcha-verify-container',
    '[id*="captcha" i], [class*="captcha" i], [class*="verify" i][class*="container" i]'];
  const W = innerWidth, H = innerHeight;
  const seen = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    return r.width >= 40 && r.height >= 30 && r.bottom > 0 && r.top < H && r.right > 0 && r.left < W
      && st.visibility !== 'hidden' && st.display !== 'none' && +st.opacity > 0.05 ? r : null; };
  for (const g of groups) {
    const rs = [...document.querySelectorAll(g)].map(seen).filter(Boolean);
    if (!rs.length) continue;
    const r = rs.reduce((a, b) => (a.width * a.height >= b.width * b.height ? a : b));
    return [r.left, r.top, r.width, r.height];
  }
  return null;
}"""


def _find_check(page, vw: int, vh: int):
    """(x, y, w, h) around the check, with some room around it; None if there is none or it fills the page."""
    try:
        r = page.evaluate(_FIND_CHECK_JS)
    except Exception:  # noqa: BLE001
        return None
    if not r:
        return None
    x, y, w, h = r
    w2, h2 = max(w + 48, 360), max(h + 48, 260)           # some room around it, and not too small
    if w2 * h2 > vw * vh * 0.6:
        return None                                         # the check is most of the page anyway
    x2 = min(max(0, x + w / 2 - w2 / 2), max(0, vw - w2))
    y2 = min(max(0, y + h / 2 - h2 / 2), max(0, vh - h2))
    return (round(x2), round(y2), round(min(w2, vw)), round(min(h2, vh)))


def _perform(page, act: dict):
    try:
        if act["type"] == "click":
            page.mouse.click(float(act["x"]), float(act["y"]))
        elif act["type"] == "type":
            page.keyboard.type(str(act.get("text", ""))[:200], delay=40)
        elif act["type"] == "key":
            key = str(act.get("key", ""))
            if key in ("Enter", "Tab", "Backspace", "Escape", "Space", "ArrowDown", "ArrowUp"):
                page.keyboard.press(key)
        elif act["type"] == "scroll":
            page.mouse.wheel(0, max(-2000, min(2000, float(act.get("dy", 400)))))
    except Exception:  # noqa: BLE001  a click during navigation is simply lost; the person clicks again
        pass


def _notify(label: str):
    link = os.environ.get("PUBLIC_URL", "").strip()
    mins = os.environ.get("CAPTCHA_WAIT_MINUTES", "10")
    notify(f"🧩 Post Link Collector: {label} is asking for a verification (\"I'm not a robot\"). "
           f"Open {link or 'the dashboard'} within {mins} minutes and tap the picture to solve it.",
           os.environ.get("CAPTCHA_WEBHOOK_URL", "") or None)


def notify(text: str, url: str | None = None):
    """Tell people, in the background: a verification is waiting, or a scheduled search finished.
    - Telegram (free, to your phone): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (one or more ids, comma separated);
    - a webhook that takes {"text": ...} (Slack/Teams incoming webhook, n8n/Make): NOTIFY_WEBHOOK_URL,
      else CAPTCHA_WEBHOOK_URL (or the url given).
    Nothing is sent when none is set."""
    url = (url or os.environ.get("NOTIFY_WEBHOOK_URL") or os.environ.get("CAPTCHA_WEBHOOK_URL") or "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    posts = []
    if url:
        posts.append((url, {"text": text}))
    if token and chats:
        base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        for chat in chats:
            posts.append((f"{base}/bot{token}/sendMessage", {"chat_id": chat, "text": text,
                                                               "disable_web_page_preview": True}))
    if not posts:
        return

    def send():
        for u, payload in posts:
            try:
                req = urllib.request.Request(u, data=json.dumps(payload).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=send, daemon=True).start()


CAPTCHA = CaptchaGate()
