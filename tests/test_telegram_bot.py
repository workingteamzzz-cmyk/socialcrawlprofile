"""The Telegram bot: only the team's chats are answered; buttons run, stop and skip; no data is sent."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def tg(monkeypatch):
    sent = []

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            sent.append((self.path.rsplit("/", 1)[-1], body))
            raw = json.dumps({"ok": True, "result": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(raw)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111,-222")
    monkeypatch.setenv("TELEGRAM_API_BASE", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("PUBLIC_URL", "https://collector.example")
    yield sent
    srv.shutdown()


def _wait(sent, n, t=3):
    end = time.time() + t
    while len(sent) < n and time.time() < end:
        time.sleep(0.02)
    return sent


def _msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "from": {"username": "ana"}, "text": text}}


def test_strangers_get_only_their_chat_id(tg):
    from crawler import telegram_bot as bot
    bot.handle(_msg(999, "/status"))
    time.sleep(0.2)
    assert tg == []                                           # not answered at all
    bot.handle(_msg(999, "/start"))
    method, body = _wait(tg, 1)[0]
    assert method == "sendMessage" and body["chat_id"] == "999" and "999" in body["text"]
    assert "running" not in body["text"].lower()


def test_status_schedules_and_buttons(tg, monkeypatch):
    from crawler import schedule
    from crawler import telegram_bot as bot

    class Sched:
        ran = []

        def list(self):
            return [{"id": "s1", "name": "PLN harian", "describe": "every day at 06:00", "last_result": "12 links"}]

        def run_now(self, sid):
            self.ran.append(sid)
            return None
    sch = Sched()
    monkeypatch.setattr(schedule, "get", lambda: sch)
    bot.handle(_msg(111, "/status"))
    method, body = _wait(tg, 1)[0]
    assert "Nothing is running" in body["text"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://collector.example"
    tg.clear()
    bot.handle(_msg(-222, "/schedules"))
    body = _wait(tg, 1)[0][1]
    assert "PLN harian" in body["text"] and body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "run:s1"
    tg.clear()
    bot.handle({"update_id": 2, "callback_query": {"id": "q1", "data": "run:s1", "from": {"username": "ana"},
                                                   "message": {"chat": {"id": -222}}}})
    assert sch.ran == ["s1"]
    assert any(m == "answerCallbackQuery" and "Started" in b["text"] for m, b in _wait(tg, 2))


def test_skip_button_skips_a_waiting_verification(tg):
    from crawler import telegram_bot as bot
    from crawler.captcha import CAPTCHA
    with CAPTCHA.lock:
        CAPTCHA.pending = True
    try:
        bot.handle({"update_id": 3, "callback_query": {"id": "q2", "data": "skip", "from": {"id": 5},
                                                       "message": {"chat": {"id": 111}}}})
        assert CAPTCHA.actions.get_nowait() == {"type": "skip"}
    finally:
        with CAPTCHA.lock:
            CAPTCHA.pending = False


def test_verification_message_has_solve_and_skip_buttons(tg):
    from crawler import captcha
    captcha._notify("Google Search")
    msgs = [b for m, b in _wait(tg, 2) if m == "sendMessage"]
    assert len(msgs) == 2 and {m["chat_id"] for m in msgs} == {"111", "-222"}
    kb = msgs[0]["reply_markup"]["inline_keyboard"]
    assert kb[0][0]["url"] == "https://collector.example" and kb[1][0]["callback_data"] == "skip"
    assert "your own Google account is not used" in msgs[0]["text"]


def test_set_from_the_dashboard(tg, tmp_path, monkeypatch):
    from crawler import paths
    from crawler import telegram_bot as bot
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert bot.token() == "" and bot.public()["token_set"] is False
    with pytest.raises(ValueError, match="not a bot token"):
        bot.save("cd ~/post-link-collector && read -rsp")            # a pasted command is refused
    pub = bot.save("  bot123456789:AAHabcdefghijklmnopqrstuvwxyz012345\\n", "-1001234, 55555", "https://app.example.id/")
    assert pub["token_tail"] == "…2345" and bot.token() == "123456789:AAHabcdefghijklmnopqrstuvwxyz012345"
    assert bot.chats() == ["-1001234", "55555"] and bot.public_url() == "https://app.example.id"
    assert oct(os.stat(tmp_path / "telegram.json").st_mode)[-3:] == "600"
    r = bot.check()
    assert r["ok"] and r["sent"] == ["-1001234", "55555"]
    with pytest.raises(ValueError, match="chat id"):
        bot.save(chat_ids="grup tim")


def test_settings_endpoint_admin_only(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from crawler import paths
    from tests.test_users import SITE, _app, _client
    monkeypatch.setattr(paths, "APP_DATA", str(tmp_path / "data"))
    app, _ = _app(tmp_path, monkeypatch)
    admin, op = _client(app, "ana"), _client(app, "budi")
    assert op.get("/api/telegram").status_code == 403
    r = admin.post("/api/telegram", json={"token": "123456789:AAHabcdefghijklmnopqrstuvwxyz012345", "chats": "-100"},
                   headers=SITE).json()
    assert r["ok"] and r["token_set"] and "AAH" not in admin.get("/api/telegram").text
