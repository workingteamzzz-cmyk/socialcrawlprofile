"""FastAPI version of the app, for running on a server (see serve.py and the README section "Run as an API server").

Same dashboard and the same JSON endpoints as the local app, plus:
- an API key (COLLECTOR_API_KEY) on everything except /api/health: send it as the `X-API-Key` header (scripts,
  n8n, Power Query...) or sign in once on the dashboard (an HttpOnly cookie);
- interactive API docs at /docs (after signing in);
- POST /api/logins/cookies to sign the server's browser in with cookies exported from your own browser.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from . import store
from .job import JOB, OUTPUT_DIR
from .server import STATIC, list_outputs, logins_info

COOKIE = "plc_session"
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="The server's COLLECTOR_API_KEY")


def _session_value(key: str) -> str:
    """What the sign-in cookie holds: derived from the key, never the key itself."""
    return hmac.new(key.encode(), b"post-link-collector-session", hashlib.sha256).hexdigest()


class _Failures:
    """Slow down key guessing: after 10 wrong keys from one address in 10 minutes, refuse for a while."""
    def __init__(self):
        self.lock = threading.Lock()
        self.seen: dict[str, list[float]] = {}

    def blocked(self, ip: str) -> bool:
        now = time.time()
        with self.lock:
            hits = [t for t in self.seen.get(ip, []) if now - t < 600]
            self.seen[ip] = hits
            return len(hits) >= 10

    def add(self, ip: str):
        with self.lock:
            self.seen.setdefault(ip, []).append(time.time())


# ---------- request bodies (shown in /docs) ----------
Tz = Literal["WIB", "WITA", "WIT", "UTC"]
Platform = Literal["instagram", "x", "tiktok", "youtube", "threads", "facebook"]


class CrawlRequest(BaseModel):
    """Collect post links from accounts: either a saved list (+ the ids of its accounts) or pasted profile links."""
    model_config = ConfigDict(populate_by_name=True)
    start: str = Field(..., examples=["2026-11-01"], description="First day, YYYY-MM-DD")
    end: str = Field(..., examples=["2026-11-09"], description="Last day, YYYY-MM-DD")
    tz: Tz = "WIB"
    platforms: list[Platform] = Field(default_factory=list, description="Empty = all platforms")
    detail: Literal["links", "full"] = Field("links", description="'full' adds caption, likes, comments, shares, views")
    list_id: str = Field("", alias="list", description="Account list id (GET /api/lists)")
    ids: list[str] = Field(default_factory=list, description="Account ids in that list (GET /api/accounts?list=...)")
    text: str = Field("", description="Or: profile links, one per line ('link | group' is fine)")


class KeywordRequest(BaseModel):
    keywords: list[str] = Field(..., examples=[["tarif listrik", "PLN"]])
    start: str = Field(..., examples=["2026-11-01"])
    end: str = Field(..., examples=["2026-11-09"])
    tz: Tz = "WIB"
    platforms: list[Platform] = Field(default_factory=list)
    options: dict = Field(default_factory=dict, examples=[{"detail": "links", "cap": 300, "exclude": ["promo"],
                                                           "must_mention": True, "exact": False}])


class NewsRequest(BaseModel):
    keywords: list[str] = Field(..., examples=[["tarif listrik"]])
    start: str = Field(..., examples=["2026-11-01"])
    end: str = Field(..., examples=["2026-11-09"])
    tz: Tz = "WIB"
    options: dict = Field(default_factory=dict, examples=[{"detail": "links", "cap": 300,
                                                           "sources": ["feed", "news_tab", "all_tab"], "exclude": []}])


class CookieImport(BaseModel):
    cookies: str | list | dict = Field(..., description="Exported cookies: Cookie-Editor JSON, Playwright storage "
                                                        "state or a cookies.txt file's text")


class AccountsAdd(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    list_id: str = Field(..., alias="list")
    text: str
    group: str = ""


class AccountsRemove(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    list_id: str = Field(..., alias="list")
    ids: list[str]


class ListCreate(BaseModel):
    name: str


class ListDelete(BaseModel):
    id: str


class SignIn(BaseModel):
    key: str


def create_app(api_key: str | None = None, secure_cookie: bool = False) -> FastAPI:
    """api_key=None only for a copy that listens on 127.0.0.1 (then everything is open, like the local app)."""
    from .paths import SERVER_MODE
    failures = _Failures()
    app = FastAPI(title="Post Link Collector API", version="1.0", docs_url=None, redoc_url=None, openapi_url=None,
                  description="Collect post links (and optionally caption & engagement) from Instagram, X, TikTok, "
                              "YouTube, Threads and Facebook, and news links from Google — without platform APIs. "
                              "One job runs at a time: start it, poll `/api/status`, then read `/api/results` or "
                              "download the Excel file.")

    def signed_in(request: Request, header_key: str | None) -> bool:
        if not api_key:
            return True
        bearer = (request.headers.get("authorization") or "")
        if bearer.lower().startswith("bearer "):
            header_key = header_key or bearer[7:].strip()
        if header_key and hmac.compare_digest(header_key.encode(), api_key.encode()):
            return True
        cookie = request.cookies.get(COOKIE) or ""
        if cookie and hmac.compare_digest(cookie.encode(), _session_value(api_key).encode()):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                # a signed-in browser: only this app's own page may send changes (no cross-site requests)
                origin = request.headers.get("origin") or request.headers.get("referer") or ""
                if not origin or urlparse(origin).netloc != request.headers.get("host", ""):
                    raise HTTPException(403, "forbidden: request from another site")
            return True
        return False

    def require(request: Request, key: str | None = Depends(_key_header)):
        ip = request.client.host if request.client else "?"
        if failures.blocked(ip):
            raise HTTPException(429, "Too many wrong keys — try again in a few minutes.")
        if not signed_in(request, key):
            if key or request.cookies.get(COOKIE):
                failures.add(ip)
            raise HTTPException(401, "Missing or wrong API key (send the X-API-Key header, or sign in on the dashboard).")

    auth = [Depends(require)]

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

    # ---------- open ----------
    @app.get("/api/health", tags=["server"], summary="Is the server up (no key needed)")
    def health():
        return {"ok": True, "running": JOB.running}

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request):
        if api_key and not signed_in(request, None):
            return HTMLResponse(_SIGN_IN_PAGE)
        return FileResponse(os.path.join(STATIC, "index.html"), media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.post("/auth", include_in_schema=False)
    def sign_in(body: SignIn, request: Request):
        ip = request.client.host if request.client else "?"
        if failures.blocked(ip):
            raise HTTPException(429, "Too many wrong keys — try again in a few minutes.")
        if not api_key or not hmac.compare_digest(body.key.strip().encode(), api_key.encode()):
            failures.add(ip)
            raise HTTPException(401, "Wrong key.")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _session_value(api_key), httponly=True, samesite="strict", secure=secure_cookie,
                        max_age=30 * 24 * 3600)
        return resp

    @app.post("/logout", include_in_schema=False)
    def sign_out():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    # ---------- docs (signed in) ----------
    @app.get("/openapi.json", include_in_schema=False, dependencies=auth)
    def openapi():
        return app.openapi()

    @app.get("/docs", include_in_schema=False, dependencies=auth)
    def docs():
        return get_swagger_ui_html(openapi_url="/openapi.json", title="Post Link Collector API")

    # ---------- status & results ----------
    @app.get("/api/status", tags=["jobs"], dependencies=auth, summary="Current job, progress and new log lines")
    def status(since: int = 0):
        return JOB.state(since)

    @app.get("/api/results", tags=["jobs"], dependencies=auth, summary="Links of the last run of a mode")
    def results(mode: Literal["profiles", "keywords", "news"] = "profiles"):
        return JOB.results_view(mode)

    @app.get("/api/outputs", tags=["files"], dependencies=auth, summary="Excel files that can be downloaded")
    def outputs():
        return {"files": list_outputs()}

    @app.get("/download/{name}", tags=["files"], dependencies=auth, summary="Download an Excel/CSV result file")
    def download(name: str):
        name = os.path.basename(name)
        path = os.path.join(OUTPUT_DIR, name)
        if not name or not name.endswith((".xlsx", ".csv")) or not os.path.isfile(path):
            raise HTTPException(404, "file not found")
        return FileResponse(path, filename=name)

    # ---------- start / stop ----------
    def answer(err):
        return {"ok": err is None, "error": err}

    @app.post("/api/start", tags=["jobs"], dependencies=auth, summary="Collect post links from accounts")
    def start(body: CrawlRequest):
        text = store.lines_for(body.list_id, body.ids) if body.ids else body.text
        return answer(JOB.start_crawl(text, body.start, body.end, body.tz, list(body.platforms), body.detail))

    @app.post("/api/keywords/start", tags=["jobs"], dependencies=auth, summary="Keyword search on the platforms")
    def keywords(body: KeywordRequest):
        return answer(JOB.start_keywords(body.keywords, body.start, body.end, body.tz, list(body.platforms), body.options))

    @app.post("/api/news/start", tags=["jobs"], dependencies=auth, summary="News links from Google")
    def news(body: NewsRequest):
        return answer(JOB.start_news(body.keywords, body.start, body.end, body.tz, body.options))

    @app.post("/api/stop", tags=["jobs"], dependencies=auth, summary="Stop the running job (results so far are kept)")
    def stop():
        JOB.stop()
        return answer(None)

    # ---------- logins ----------
    @app.get("/api/logins", tags=["logins"], dependencies=auth, summary="Which platforms the server is signed in to")
    def logins():
        return {**logins_info(), "server": SERVER_MODE}

    @app.post("/api/logins/cookies", tags=["logins"], dependencies=auth,
              summary="Sign the server in with cookies exported from your browser")
    def import_cookies(body: CookieImport):
        text = body.cookies if isinstance(body.cookies, str) else json.dumps(body.cookies)
        return JOB.import_cookies(text)

    @app.post("/api/login", include_in_schema=False, dependencies=auth)
    def login():
        return answer(JOB.start_login())

    if not SERVER_MODE:                      # these look at folders on the computer itself — not on a server
        @app.get("/api/old-profiles", include_in_schema=False, dependencies=auth)
        def old_profiles():
            from .paths import find_old_profiles
            return {"profiles": find_old_profiles()[:10]}

        @app.post("/api/logins/import", include_in_schema=False, dependencies=auth)
        def import_profile(body: dict):
            if JOB.running:
                return answer("Wait until the current job has finished (or Stop it) and try again.")
            from .paths import import_profile as do_import
            return answer(do_import(body.get("path", ""), JOB.log) or None)

    # ---------- account lists ----------
    def list_change(fn):
        try:
            return fn()
        except store.ListFileError as e:
            return {"ok": False, "error": str(e)}
        except OSError:
            return {"ok": False, "error": "Could not change the list file — is it open somewhere? Try again."}

    @app.get("/api/lists", tags=["account lists"], dependencies=auth)
    def lists():
        return {"lists": store.lists()}

    @app.get("/api/accounts", tags=["account lists"], dependencies=auth)
    def accounts(list: str = ""):  # noqa: A002  (the dashboard's parameter name)
        return {"accounts": store.load(list)}

    @app.post("/api/accounts/add", tags=["account lists"], dependencies=auth)
    def accounts_add(body: AccountsAdd):
        return list_change(lambda: store.add(body.list_id, body.text, body.group))

    @app.post("/api/accounts/remove", tags=["account lists"], dependencies=auth)
    def accounts_remove(body: AccountsRemove):
        return list_change(lambda: {"removed": store.remove(body.list_id, body.ids)})

    @app.post("/api/lists/create", tags=["account lists"], dependencies=auth)
    def lists_create(body: ListCreate):
        def go():
            list_id, err = store.create_list(body.name)
            return {"ok": err is None, "error": err, "id": list_id}
        return list_change(go)

    @app.post("/api/lists/delete", tags=["account lists"], dependencies=auth)
    def lists_delete(body: ListDelete):
        return list_change(lambda: {"ok": store.delete_list(body.id)})

    return app


_SIGN_IN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link Collector · Sign in</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#0f172a;--mute:#64748b;--line:#e2e8f0;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1220;--card:#111a2e;--ink:#e2e8f0;--mute:#94a3b8;--line:#1e293b}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;width:min(380px,100%)}
h1{font-size:20px;margin:0 0 4px}p{color:var(--mute);margin:0 0 18px}
input{width:100%;box-sizing:border-box;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
  background:transparent;color:inherit;font:inherit}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:10px;background:var(--accent);color:#fff;
  font:600 15px system-ui,sans-serif;cursor:pointer}
#e{color:#dc2626;min-height:1.5em;margin:10px 0 0;font-size:14px}
</style></head><body>
<form id="f"><h1>Link Collector</h1><p>Enter the server's access key.</p>
<input id="k" type="password" autocomplete="current-password" placeholder="Access key" autofocus required>
<button>Sign in</button><div id="e"></div></form>
<script>
document.getElementById('f').onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch('/auth', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify({key: document.getElementById('k').value})});
  if (r.ok) location.reload();
  else document.getElementById('e').textContent = (await r.json()).error || 'Wrong key.';
};
</script></body></html>"""
