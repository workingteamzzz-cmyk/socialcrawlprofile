"""Tiny local web UI (Python standard library only). Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import store
from .job import JOB, OUTPUT_DIR

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def list_outputs() -> list[dict]:
    """The result files that can be downloaded, newest first."""
    files = []
    if os.path.isdir(OUTPUT_DIR):
        for name in os.listdir(OUTPUT_DIR):
            if name.endswith(".xlsx") and name.startswith(("post_links_", "keyword_links_", "news_links_")):
                st = os.stat(os.path.join(OUTPUT_DIR, name))
                files.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return files[:40]


def logins_info() -> dict:
    from .paths import LISTS_DIR, LOGINS_FILE, OUTPUT_DIR as OUT
    info = {}
    try:
        with open(LOGINS_FILE, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    return {**info, "lists_dir": LISTS_DIR, "outputs_dir": OUT}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _local_host(self) -> bool:
        """Only answer requests addressed to this computer by name. A web page that points its own domain at
        127.0.0.1 ("DNS rebinding") would otherwise be able to read the results."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1")

    def do_GET(self):
        if not self._local_host():
            return self._json({"error": "forbidden"}, 403)
        try:
            self._get()
        except Exception as e:  # noqa: BLE001  never leave the dashboard without an answer
            self._json({"error": f"{e.__class__.__name__}: {e}"}, 500)

    def do_POST(self):
        if not self._local_host():
            return self._json({"ok": False, "error": "forbidden"}, 403)
        try:
            self._post()
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": f"{e.__class__.__name__}: {e}"}, 500)

    def _get(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
            return self._json(JOB.state(since))
        if u.path == "/api/logins":
            from .paths import SERVER_MODE
            return self._json({**logins_info(), "server": SERVER_MODE})
        if u.path == "/api/old-profiles":
            from .paths import find_old_profiles
            return self._json({"profiles": find_old_profiles()[:10]})
        if u.path == "/api/lists":
            return self._json({"lists": store.lists()})
        if u.path == "/api/accounts":
            return self._json({"accounts": store.load(parse_qs(u.query).get("list", [""])[0])})
        if u.path == "/api/results":
            mode = parse_qs(u.query).get("mode", ["profiles"])[0]
            return self._json(JOB.results_view(mode))
        if u.path == "/api/outputs":
            return self._json({"files": list_outputs()})
        if u.path.startswith("/download/"):
            name = os.path.basename(unquote(u.path[len("/download/"):]))
            path = os.path.join(OUTPUT_DIR, name)
            if not name or not os.path.isfile(path):
                return self._json({"error": "file not found"}, 404)
            with open(path, "rb") as f:
                data = f.read()
            ctype = "text/csv" if name.endswith(".csv") else \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{name}"'})
        self._json({"error": "not found"}, 404)

    def _post(self):
        # Only accept requests from this app's own page.
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._json({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            data = {}
        u = urlparse(self.path)
        if u.path == "/api/login":
            err = JOB.start_login()
        elif u.path == "/api/start":
            text = store.lines_for(data.get("list", ""), data["ids"]) if data.get("ids") else data.get("text", "")
            err = JOB.start_crawl(text, data.get("start", ""), data.get("end", ""),
                                  data.get("tz", "WIB"), data.get("platforms") or [], data.get("detail", "links"))
        elif u.path == "/api/news/start":
            err = JOB.start_news(data.get("keywords") or [], data.get("start", ""), data.get("end", ""),
                                 data.get("tz", "WIB"), data.get("options") or {})
        elif u.path == "/api/keywords/start":
            err = JOB.start_keywords(data.get("keywords") or [], data.get("start", ""), data.get("end", ""),
                                     data.get("tz", "WIB"), data.get("platforms") or [], data.get("options") or {})
        elif u.path in ("/api/accounts/add", "/api/accounts/remove", "/api/lists/create", "/api/lists/delete"):
            try:
                if u.path == "/api/accounts/add":
                    return self._json(store.add(data.get("list", ""), data.get("text", ""), data.get("group", "")))
                if u.path == "/api/accounts/remove":
                    return self._json({"removed": store.remove(data.get("list", ""), data.get("ids") or [])})
                if u.path == "/api/lists/create":
                    list_id, err = store.create_list(data.get("name", ""))
                    return self._json({"ok": err is None, "error": err, "id": list_id})
                return self._json({"ok": store.delete_list(data.get("id", ""))})
            except store.ListFileError as e:
                return self._json({"ok": False, "error": str(e)})
            except OSError:
                return self._json({"ok": False, "error": "Could not change the list file — is it open in Excel? Close it and try again."})
        elif u.path == "/api/logins/import":
            if JOB.running:
                return self._json({"ok": False, "error": "Wait until the current job has finished (or Stop it) and try again."})
            from .paths import import_profile
            err = import_profile(data.get("path", ""), JOB.log) or None
        elif u.path == "/api/logins/cookies":
            c = data.get("cookies", "")
            return self._json(JOB.import_cookies(c if isinstance(c, str) else json.dumps(c)))
        elif u.path == "/api/stop":
            JOB.stop()
            err = None
        else:
            return self._json({"error": "not found"}, 404)
        self._json({"ok": err is None, "error": err})


def make_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(port: int):
    make_server(port).serve_forever()
