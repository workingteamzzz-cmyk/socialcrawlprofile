"""Tiny local web UI (Python standard library only). Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import store
from .job import JOB, OUTPUT_DIR

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


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

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            since = int(parse_qs(u.query).get("since", ["0"])[0] or 0)
            return self._json(JOB.state(since))
        if u.path == "/api/accounts":
            return self._json({"accounts": store.load_all()})
        if u.path == "/api/results":
            mode = parse_qs(u.query).get("mode", ["profiles"])[0]
            return self._json(JOB.results_view(mode))
        if u.path == "/api/outputs":
            files = []
            if os.path.isdir(OUTPUT_DIR):
                for name in os.listdir(OUTPUT_DIR):
                    if name.endswith(".xlsx") and name.startswith(("post_links_", "keyword_links_")):
                        st = os.stat(os.path.join(OUTPUT_DIR, name))
                        files.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
            files.sort(key=lambda f: f["mtime"], reverse=True)
            return self._json({"files": files[:40]})
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

    def do_POST(self):
        # Only accept requests from this app's own page.
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
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
            text = store.lines_for(data["ids"]) if data.get("ids") else data.get("text", "")
            err = JOB.start_crawl(text, data.get("start", ""), data.get("end", ""),
                                  data.get("tz", "WIB"), data.get("platforms") or [])
        elif u.path == "/api/keywords/start":
            err = JOB.start_keywords(data.get("keywords") or [], data.get("start", ""), data.get("end", ""),
                                     data.get("tz", "WIB"), data.get("platforms") or [], data.get("options") or {})
        elif u.path == "/api/accounts/add":
            return self._json(store.add(data.get("text", ""), data.get("unit", "")))
        elif u.path == "/api/accounts/remove":
            return self._json({"removed": store.remove(data.get("ids") or [])})
        elif u.path == "/api/stop":
            JOB.stop()
            err = None
        else:
            return self._json({"error": "not found"}, 404)
        self._json({"ok": err is None, "error": err})


def serve(port: int):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()
