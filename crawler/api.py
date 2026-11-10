"""FastAPI version of the app, for running on a server (see serve.py and the README section "Run as an API server").

Same dashboard and the same JSON endpoints as the local app, plus:
- a key per person (COLLECTOR_API_KEYS="ana=...,budi=...", or one COLLECTOR_API_KEY) on everything except
  /api/health: the `X-API-Key` header for scripts (n8n, Power Query...), or sign in on the dashboard (an HttpOnly
  cookie that expires); removing a person's key from the settings locks them out, cookies included;
- optional allowed addresses (ALLOWED_IPS), an audit log of who did what (never cookie values), security headers;
- interactive API docs at /docs (after signing in);
- POST /api/logins/cookies to sign the server's browser in with cookies exported from your own browser.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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


def _session_value(name: str, key: str, issued: int | None = None) -> str:
    """What the sign-in cookie holds: who, when, and a signature made with that person's key — never the key
    itself. Changing or removing the key makes the cookie worthless."""
    issued = int(time.time()) if issued is None else issued
    sig = hmac.new(key.encode(), f"post-link-collector-session|{name}|{issued}".encode(), hashlib.sha256).hexdigest()
    return f"{name}.{issued}.{sig}"


class Audit:
    """Append-only record of who did what (JSON lines). Cookie values and keys are never written."""
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()

    def add(self, who: str, action: str, ip: str = "", **info):
        if not self.path:
            return
        line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "who": who, "ip": ip, "action": action, **info},
                          ensure_ascii=False)
        with self.lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                if os.path.exists(self.path) and os.path.getsize(self.path) > 5_000_000:
                    os.replace(self.path, self.path + ".1")          # keep it bounded: current + one older file
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def tail(self, n: int = 200) -> list[dict]:
        if not self.path or not os.path.exists(self.path):
            return []
        with self.lock, open(self.path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out


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
    template: Literal["standard", "medsos"] = Field("standard", description="Excel layout: the app's own, or the "
                                                                           "client's Medsos layout")


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


class CaptchaAction(BaseModel):
    type: Literal["click", "type", "key", "scroll", "refresh", "skip"]
    x: float = Field(0, description="click: position on the page (in page pixels, see width/height in /api/status)")
    y: float = 0
    text: str = Field("", max_length=200, description="type: the text")
    key: str = Field("", description="key: Enter, Tab, Backspace, Escape, Space, ArrowDown, ArrowUp")
    dy: float = Field(400, description="scroll: pixels down (negative = up)")


class ScheduleIn(BaseModel):
    """A search that runs by itself. `payload` is what the matching start endpoint takes, without start/end."""
    id: str = Field("", description="Empty = a new schedule; an id = change that schedule")
    name: str = ""
    kind: Literal["news", "keywords", "profiles"]
    payload: dict = Field(..., examples=[{"keywords": ["tarif listrik", "PLN"], "tz": "WIB",
                                          "options": {"sources": ["feed"], "cap": 300}}])
    repeat: Literal["daily", "hours"] = "daily"
    time: str = Field("07:00", description="daily: HH:MM in the payload's time zone")
    hours: int = Field(6, description="hours: run every N hours (1-24)")
    window: Literal["yesterday", "today", "yesterday_today", "last_7_days"] = "yesterday"
    enabled: bool = True
    webhook: str = Field("", description="When a run finishes, POST the results as JSON here (a client's API, n8n...)")
    webhook_token: str = Field("", description="Sent as 'Authorization: Bearer <token>' and used to sign the body "
                                               "(X-Collector-Signature: sha256=HMAC)")


class ScheduleRef(BaseModel):
    id: str


class SignIn(BaseModel):
    key: str


def create_app(api_keys: dict | str | None = None, secure_cookie: bool = False, allowed_ips: list | None = None,
               audit_path: str | None = None, session_days: float = 7) -> FastAPI:
    """api_keys: {person: key} (a single key string means {"admin": key}). None only for a copy that listens on
    127.0.0.1 — then everything is open, like the local app. allowed_ips: addresses/networks that may connect
    (None = any)."""
    from .paths import SERVER_MODE
    keys = {"admin": api_keys} if isinstance(api_keys, str) else dict(api_keys or {})
    nets = [ipaddress.ip_network(n, strict=False) for n in (allowed_ips or [])]
    audit = Audit(audit_path)
    app_state = {"audit": audit}
    failures = _Failures()
    app = FastAPI(title="Post Link Collector API", version="1.1", docs_url=None, redoc_url=None, openapi_url=None,
                  description="Collect post links (and optionally caption & engagement) from Instagram, X, TikTok, "
                              "YouTube, Threads and Facebook, and news links from Google — without platform APIs. "
                              "One job runs at a time: start it, poll `/api/status`, then read `/api/results` or "
                              "download the Excel file.")

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "?"

    def ip_allowed(ip: str) -> bool:
        if not nets:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in nets)

    def key_owner(given: str | None) -> str | None:
        if not given:
            return None
        found = None
        for name, key in keys.items():                    # compare with every key (constant time per key)
            if hmac.compare_digest(given.encode(), key.encode()):
                found = name
        return found

    def cookie_owner(value: str) -> str | None:
        try:
            name, issued, _sig = value.split(".", 2)
            issued_i = int(issued)
        except ValueError:
            return None
        key = keys.get(name)
        if not key or time.time() - issued_i > session_days * 86400 or issued_i > time.time() + 60:
            return None
        return name if hmac.compare_digest(value.encode(), _session_value(name, key, issued_i).encode()) else None

    def who(request: Request, header_key: str | None) -> str | None:
        """The person making the request, or None. Raises 403 for a cookie request sent from another site."""
        if not keys:
            return "local"
        bearer = (request.headers.get("authorization") or "")
        if bearer.lower().startswith("bearer "):
            header_key = header_key or bearer[7:].strip()
        name = key_owner(header_key)
        if name:
            return name
        name = cookie_owner(request.cookies.get(COOKIE) or "")
        if name and request.method not in ("GET", "HEAD", "OPTIONS"):
            # a signed-in browser: only this app's own page may send changes (no cross-site requests)
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if not origin or urlparse(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "forbidden: request from another site")
        return name

    def signed_in(request: Request, header_key: str | None) -> bool:
        return who(request, header_key) is not None

    def require(request: Request, key: str | None = Depends(_key_header)) -> str:
        if not keys and urlparse("//" + request.headers.get("host", "")).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(403, "forbidden")      # open local copy: only requests addressed to this computer
        ip = client_ip(request)
        if not ip_allowed(ip):
            raise HTTPException(403, "This address may not use the server.")
        if failures.blocked(ip):
            raise HTTPException(429, "Too many wrong keys — try again in a few minutes.")
        name = who(request, key)
        if name is None:
            if key or request.cookies.get(COOKIE) or request.headers.get("authorization"):
                failures.add(ip)
                audit.add("?", "rejected", ip, path=request.url.path)
            raise HTTPException(401, "Missing or wrong API key (send the X-API-Key header, or sign in on the dashboard).")
        request.state.who = name
        return name

    def log(request: Request, action: str, **info):
        audit.add(getattr(request.state, "who", "?"), action, client_ip(request), **info)

    auth = [Depends(require)]

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)

    # ---------- open ----------
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        from fastapi import Response
        return Response(status_code=204)

    @app.get("/api/health", tags=["server"], summary="Is the server up (no key needed)")
    def health():
        return {"ok": True, "running": JOB.running}

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        if nets and request.url.path != "/api/health" and not ip_allowed(client_ip(request)):
            return JSONResponse({"ok": False, "error": "This address may not use the server."}, status_code=403)
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path != "/docs":       # the docs page loads Swagger UI from a CDN
            h.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if secure_cookie:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path.startswith(("/api/", "/download/")):
            h.setdefault("Cache-Control", "no-store")
        return resp

    @app.get("/", include_in_schema=False)
    def dashboard(request: Request):
        if not ip_allowed(client_ip(request)):
            raise HTTPException(403, "This address may not use the server.")
        if keys and not signed_in(request, None):
            return HTMLResponse(_SIGN_IN_PAGE, headers={"Cache-Control": "no-store"})
        return FileResponse(os.path.join(STATIC, "index.html"), media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.post("/auth", include_in_schema=False)
    def sign_in(body: SignIn, request: Request):
        ip = client_ip(request)
        if not ip_allowed(ip):
            raise HTTPException(403, "This address may not use the server.")
        if failures.blocked(ip):
            raise HTTPException(429, "Too many wrong keys — try again in a few minutes.")
        name = key_owner(body.key.strip())
        if not name:
            failures.add(ip)
            audit.add("?", "sign-in failed", ip)
            raise HTTPException(401, "Wrong key.")
        audit.add(name, "signed in", ip)
        resp = JSONResponse({"ok": True, "who": name})
        resp.set_cookie(COOKIE, _session_value(name, keys[name]), httponly=True, samesite="strict", secure=secure_cookie,
                        max_age=int(session_days * 86400))
        return resp

    @app.post("/logout", include_in_schema=False)
    def sign_out():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/audit", tags=["server"], dependencies=auth, summary="Who did what on this server (newest first)")
    def audit_log(limit: int = 200):
        return {"entries": audit.tail(max(1, min(limit, 2000)))}

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
    def download(name: str, request: Request):
        name = os.path.basename(name)
        path = os.path.join(OUTPUT_DIR, name)
        if not name or not name.endswith((".xlsx", ".csv")) or not os.path.isfile(path):
            raise HTTPException(404, "file not found")
        log(request, "downloaded", file=name)
        return FileResponse(path, filename=name)

    # ---------- start / stop ----------
    def answer(err):
        return {"ok": err is None, "error": err}

    @app.post("/api/start", tags=["jobs"], dependencies=auth, summary="Collect post links from accounts")
    def start(body: CrawlRequest, request: Request):
        text = store.lines_for(body.list_id, body.ids) if body.ids else body.text
        err = JOB.start_crawl(text, body.start, body.end, body.tz, list(body.platforms), body.detail, body.template)
        log(request, "started account collection" if not err else "start refused", period=f"{body.start}..{body.end}",
            accounts=len([ln for ln in text.splitlines() if ln.strip()]), detail=body.detail)
        return answer(err)

    @app.post("/api/keywords/start", tags=["jobs"], dependencies=auth, summary="Keyword search on the platforms")
    def keywords(body: KeywordRequest, request: Request):
        err = JOB.start_keywords(body.keywords, body.start, body.end, body.tz, list(body.platforms), body.options)
        log(request, "started keyword search" if not err else "start refused", period=f"{body.start}..{body.end}",
            keywords=body.keywords[:20], platforms=list(body.platforms))
        return answer(err)

    @app.post("/api/news/start", tags=["jobs"], dependencies=auth, summary="News links from Google")
    def news(body: NewsRequest, request: Request):
        err = JOB.start_news(body.keywords, body.start, body.end, body.tz, body.options)
        log(request, "started news search" if not err else "start refused", period=f"{body.start}..{body.end}",
            keywords=body.keywords[:20])
        return answer(err)

    @app.post("/api/stop", tags=["jobs"], dependencies=auth, summary="Stop the running job (results so far are kept)")
    def stop(request: Request):
        JOB.stop()
        log(request, "stopped the job")
        return answer(None)

    # ---------- verification ("I'm not a robot") solved by a person from the dashboard ----------
    solving: dict = {}
    @app.get("/api/captcha/screenshot", tags=["verification"], dependencies=auth,
             summary="Live picture of the page that asks for a verification")
    def captcha_picture(view: Literal["full", "focus"] = "full"):
        """`view=focus`: a close-up of the check itself (for phones). The `X-Clip` header says which part of the page
        (x,y,w,h in page pixels) the picture shows — a click at (px, py) on the picture is page point
        (x + px/picture_width*w, y + py/picture_height*h)."""
        from fastapi import Response

        from .captcha import CAPTCHA
        png, clip = CAPTCHA.screenshot(view)
        if not png:
            raise HTTPException(404, "No picture yet")
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store",
                                                              "X-Clip": ",".join(str(int(v)) for v in clip)})

    @app.post("/api/captcha", tags=["verification"], dependencies=auth,
              summary="Click / type / press a key on that page, or skip it")
    def captcha_action(body: CaptchaAction, request: Request):
        from .captcha import CAPTCHA
        err = CAPTCHA.act(body.model_dump())
        if not err and body.type == "skip":
            log(request, "skipped a verification", site=CAPTCHA.label)
        elif not err and solving.get("since") != CAPTCHA.since:          # once per verification, not per click
            solving["since"] = CAPTCHA.since
            log(request, "worked on a verification", site=CAPTCHA.label)
        return answer(err)

    # ---------- schedules ----------
    from . import schedule as sched_mod

    def sched_call(fn):
        try:
            return fn()
        except sched_mod.ScheduleError as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/schedules", tags=["schedules"], dependencies=auth, summary="Searches that run by themselves")
    def schedules():
        return {"schedules": sched_mod.get().list(), "now": int(time.time())}

    @app.post("/api/schedules/save", tags=["schedules"], dependencies=auth, summary="Create or change a schedule")
    def schedule_save(body: ScheduleIn, request: Request):
        def go():
            s = sched_mod.get().save(body.model_dump())
            log(request, "changed a schedule" if body.id else "created a schedule", name=s["name"])
            return {"ok": True, "schedule": s}
        return sched_call(go)

    @app.post("/api/schedules/delete", tags=["schedules"], dependencies=auth)
    def schedule_delete(body: ScheduleRef, request: Request):
        ok = sched_mod.get().delete(body.id)
        if ok:
            log(request, "deleted a schedule", id=body.id)
        return {"ok": ok, "error": None if ok else "That schedule no longer exists."}

    @app.post("/api/schedules/run", tags=["schedules"], dependencies=auth, summary="Run a schedule now")
    def schedule_run(body: ScheduleRef, request: Request):
        err = sched_mod.get().run_now(body.id)
        if not err:
            log(request, "ran a schedule now", id=body.id)
        return answer(err)

    # ---------- logins ----------
    @app.get("/api/logins", tags=["logins"], dependencies=auth, summary="Which platforms the server is signed in to")
    def logins(request: Request):
        return {**logins_info(), "server": SERVER_MODE, "who": getattr(request.state, "who", "")}

    @app.post("/api/logins/cookies", tags=["logins"], dependencies=auth,
              summary="Sign the server in with cookies exported from your browser")
    def import_cookies(body: CookieImport, request: Request):
        text = body.cookies if isinstance(body.cookies, str) else json.dumps(body.cookies)
        res = JOB.import_cookies(text)
        log(request, "imported cookies" if res.get("ok") else "cookie import failed",   # never the cookie values
            count=res.get("added", 0), signed_in=[p for p, v in (res.get("platforms") or {}).items() if v])
        return res

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
    def accounts_add(body: AccountsAdd, request: Request):
        log(request, "added accounts", list=body.list_id)
        return list_change(lambda: store.add(body.list_id, body.text, body.group))

    @app.post("/api/accounts/remove", tags=["account lists"], dependencies=auth)
    def accounts_remove(body: AccountsRemove, request: Request):
        log(request, "removed accounts", list=body.list_id, count=len(body.ids))
        return list_change(lambda: {"removed": store.remove(body.list_id, body.ids)})

    @app.post("/api/lists/create", tags=["account lists"], dependencies=auth)
    def lists_create(body: ListCreate, request: Request):
        log(request, "created a list", name=body.name)
        def go():
            list_id, err = store.create_list(body.name)
            return {"ok": err is None, "error": err, "id": list_id}
        return list_change(go)

    @app.post("/api/lists/delete", tags=["account lists"], dependencies=auth)
    def lists_delete(body: ListDelete, request: Request):
        log(request, "deleted a list", list=body.id)
        return list_change(lambda: {"ok": store.delete_list(body.id)})

    app.state.audit = app_state["audit"]
    return app


_SIGN_IN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Link Collector · Sign in</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--ink:#0f172a;--mute:#64748b;--line:#e2e8f0;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0b1220;--card:#111a2e;--ink:#e2e8f0;--mute:#94a3b8;--line:#1e293b}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--ink);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:16px;box-sizing:border-box}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:28px;width:min(380px,100%);
  box-sizing:border-box}
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
