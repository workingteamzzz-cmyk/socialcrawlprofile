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
        self.version = 0
        self.size = (0, 0)
        self.last_view = 0.0

    # ---------- web side ----------
    def info(self) -> dict:
        with self.lock:
            return {"pending": self.pending, "label": self.label, "since": int(self.since), "until": int(self.until),
                    "version": self.version,
                    "width": self.size[0], "height": self.size[1]}

    def screenshot(self) -> bytes:
        with self.lock:
            self.last_view = time.time()
            return self.shot

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
            self.shot, self.size = b"", (0, 0)
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
        except Exception:  # noqa: BLE001  (page busy navigating)
            return
        with self.lock:
            self.shot, self.size = png, (vp["width"], vp["height"])
            self.version += 1


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
    url = os.environ.get("CAPTCHA_WEBHOOK_URL", "").strip()
    if not url:
        return
    link = os.environ.get("PUBLIC_URL", "").strip()
    body = json.dumps({"text": f"Post Link Collector: {label} is asking for a verification. "
                               f"Open {link or 'the dashboard'} to solve it."}).encode()

    def send():
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).close()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=send, daemon=True).start()


CAPTCHA = CaptchaGate()
