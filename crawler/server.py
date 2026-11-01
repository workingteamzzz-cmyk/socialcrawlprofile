"""Tiny local web UI (Python standard library only). Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .job import JOB, OUTPUT_DIR, ROOT

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_LIST = os.path.join(ROOT, "daftar_akun.txt")


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
        if u.path == "/api/default-accounts":
            text = ""
            if os.path.exists(DEFAULT_LIST):
                with open(DEFAULT_LIST, encoding="utf-8") as f:
                    text = f.read()
            return self._json({"text": text})
        if u.path.startswith("/download/"):
            name = os.path.basename(unquote(u.path[len("/download/"):]))
            path = os.path.join(OUTPUT_DIR, name)
            if not name or not os.path.isfile(path):
                return self._json({"error": "file tidak ada"}, 404)
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
            err = JOB.start_crawl(data.get("text", ""), data.get("start", ""), data.get("end", ""),
                                  data.get("tz", "WIB"), data.get("platforms") or [])
        elif u.path == "/api/stop":
            JOB.stop()
            err = None
        elif u.path == "/api/save-accounts":
            with open(DEFAULT_LIST, "w", encoding="utf-8") as f:
                f.write(data.get("text", ""))
            err = None
        else:
            return self._json({"error": "not found"}, 404)
        self._json({"ok": err is None, "error": err})


def serve(port: int):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.serve_forever()
