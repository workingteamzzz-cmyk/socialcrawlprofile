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


class DecodeFile(BaseModel):
    name: str = Field(..., examples=["20261108_Online_Manual_24.00.xlsx"])
    data: str = Field(..., description="The file, base64 (a data: URL is fine)")


class ApiScrape(BaseModel):
    """The team's API scraper — the same options as its Colab form."""
    mode: Literal["keyword", "link", "profile", "mixed"] = "keyword"
    platform: str = Field("ALL platforms", description="keyword mode: 'ALL platforms' or one from GET /api/apiscraper/info")
    queries: str = Field("", description="keywords (per line or comma), or post / profile links (one per line)")
    limit: int = Field(30, description="Max/query")
    budget: int = Field(200, description="Credit cap per run (non-Twitter); 0 = unlimited")
    time_from: str = Field("", description="'YYYY-MM-DD HH:MM' (WIB), empty = no time filter")
    time_to: str = ""
    strict: bool = True
    lang: Literal["", "id", "en"] = ""
    neg_use: bool = False
    neg_kw: str = ""
    dedup: bool = True
    coverage: bool = False
    delta: bool = False
    comments: bool = False
    cmt_top: int = 20
    cmt_max: int = 30
    cmt_kw: bool = False
    cmt_kw_txt: str = ""
    foll: bool = False
    fillid: bool = True
    twrt: bool = True
    twrt_cap: int = 0
    twrt_tot: int = 0
    twrt_link: bool = False
    import_: str = Field("", alias="import", description="links / link<TAB>text you already have (0 credits)")
    model_config = ConfigDict(populate_by_name=True)


class Topic(BaseModel):
    id: str | None = None
    name: str = Field("", description="e.g. PLN")
    keywords: list[str] | str = Field(default_factory=list, description="the keywords that belong to this topic")


class ReportRequest(BaseModel):
    kind: str = Field("online", description="online (news) or medsos (social media)")
    start: str = Field(..., description="YYYY-MM-DD")
    end: str = Field(..., description="YYYY-MM-DD")
    topic_id: str | None = Field(None, description="a saved topic; or give name + keywords")
    name: str = ""
    keywords: list[str] | str = Field(default_factory=list)
    in_text: bool = Field(True, description="also rows whose text contains a keyword (not only the run's keyword)")


class ApiKeys(BaseModel):
    api_key: str = ""
    twitter_api_key: str = ""
    default_budget: int | None = None
    clear_api_key: bool = False
    clear_twitter: bool = False
    daily_cap: int | None = Field(None, description="max credits per day over all runs (0 = no limit)")
    monthly_cap: int | None = Field(None, description="max credits per month over all runs (0 = no limit)")
    auto_fill: bool | None = Field(None, description="fill empty Medsos data (IDs, image, followers, caption, "
                                                     "engagement) with the API after browser runs")
    fill_cap: int | None = Field(None, description="max credits auto-fill may use per run")
    comments: bool | None = Field(None, description="Medsos: add the comments of the most engaged posts (API)")
    cmt_posts: int | None = None
    cmt_per_post: int | None = None


class SignIn(BaseModel):
    username: str = ""
    password: str = ""
    key: str = Field("", description="Or an access key (COLLECTOR_API_KEYS = admin) or a personal API key")


class UserIn(BaseModel):
    username: str
    role: Literal["viewer", "operator", "admin"] = "viewer"
    password: str = Field("", description="Required for a new user; give it to change the password")
    disabled: bool = False


class UserRef(BaseModel):
    username: str


class PasswordChange(BaseModel):
    old: str
    new: str


def create_app(api_keys: dict | str | None = None, secure_cookie: bool = False, allowed_ips: list | None = None,
               audit_path: str | None = None, session_days: float = 7, users_path: str | None = None) -> FastAPI:
    """api_keys: {person: key} (a single key string means {"admin": key}). None only for a copy that listens on
    127.0.0.1 — then everything is open, like the local app. allowed_ips: addresses/networks that may connect
    (None = any)."""
    from .paths import SERVER_MODE
    keys = {"admin": api_keys} if isinstance(api_keys, str) else dict(api_keys or {})
    from .users import Users, UserError, allowed
    users = Users(users_path) if users_path else None

    def protected() -> bool:
        """Sign-in needed: there are access keys or user accounts. Otherwise this is an open copy on 127.0.0.1."""
        if users:
            users._fresh()
        return bool(keys) or bool(users and users.data)
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

    def identify(request: Request, header_key: str | None) -> tuple[str, str] | None:
        """(person, role) making the request, or None. Raises 403 for a cookie request sent from another site.
        Access keys (COLLECTOR_API_KEYS) are admins; user accounts have their own role."""
        if not protected():
            return ("local", "admin")
        bearer = (request.headers.get("authorization") or "")
        if bearer.lower().startswith("bearer "):
            header_key = header_key or bearer[7:].strip()
        if header_key:
            name = key_owner(header_key)
            if name:
                return (name, "admin")
            hit = users.by_api_key(header_key) if users else None
            if hit:
                return hit
        value = request.cookies.get(COOKIE) or ""
        ident = None
        if value.startswith("u.") and users:
            ident = users.from_session(value, session_days * 86400)
        elif value:
            name = cookie_owner(value)
            ident = (name, "admin") if name else None
        if ident and request.method not in ("GET", "HEAD", "OPTIONS"):
            # a signed-in browser: only this app's own page may send changes (no cross-site requests)
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if not origin or urlparse(origin).netloc != request.headers.get("host", ""):
                raise HTTPException(403, "forbidden: request from another site")
        return ident

    def signed_in(request: Request, header_key: str | None) -> bool:
        return identify(request, header_key) is not None

    def need(role: str):
        """Dependency: signed in, with at least this role (viewer < operator < admin)."""
        def check(request: Request, key: str | None = Depends(_key_header)) -> str:
            if not protected() and urlparse("//" + request.headers.get("host", "")).hostname not in (
                    "127.0.0.1", "localhost", "::1"):
                raise HTTPException(403, "forbidden")      # open local copy: only requests addressed to this computer
            ip = client_ip(request)
            if not ip_allowed(ip):
                raise HTTPException(403, "This address may not use the server.")
            if failures.blocked(ip):
                raise HTTPException(429, "Too many wrong keys — try again in a few minutes.")
            ident = identify(request, key)
            if ident is None:
                if key or request.cookies.get(COOKIE) or request.headers.get("authorization"):
                    failures.add(ip)
                    audit.add("?", "rejected", ip, path=request.url.path)
                raise HTTPException(401, "Please sign in (or send your API key in the X-API-Key header).")
            request.state.who, request.state.role = ident
            if not allowed(ident[1], role):
                raise HTTPException(403, f"Your account ({ident[1]}) may not do this — it needs {role} rights.")
            return ident[0]
        return [Depends(check)]

    def log(request: Request, action: str, **info):
        audit.add(getattr(request.state, "who", "?"), action, client_ip(request), **info)

    auth = need("viewer")            # see results, lists, schedules, download
    op = need("operator")            # run searches, schedules, lists, file fixer, verifications
    admin_only = need("admin")       # users, platform cookies, audit log

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
        if protected() and not signed_in(request, None):
            return HTMLResponse(_SIGN_IN_PAGE, headers={"Cache-Control": "no-store"})
        return FileResponse(os.path.join(STATIC, "index.html"), media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.post("/auth", include_in_schema=False)
    def sign_in(body: SignIn, request: Request):
        ip = client_ip(request)
        if not ip_allowed(ip):
            raise HTTPException(403, "This address may not use the server.")
        if failures.blocked(ip):
            raise HTTPException(429, "Too many wrong attempts — try again in a few minutes.")
        cookie = name = role = None
        if body.username and users:
            if users.locked(body.username.strip().lower()):
                raise HTTPException(429, "Too many wrong passwords for this user — try again in 15 minutes.")
            name = users.check_login(body.username, body.password)
            if name:
                role, cookie = users.data[name]["role"], users.session(name)
        elif body.key.strip():
            name = key_owner(body.key.strip())
            if name:
                role, cookie = "admin", _session_value(name, keys[name])
        if not cookie:
            failures.add(ip)
            audit.add(body.username.strip().lower()[:32] or "?", "sign-in failed", ip)
            raise HTTPException(401, "Wrong username or password." if body.username else "Wrong key.")
        audit.add(name, "signed in", ip, role=role)
        resp = JSONResponse({"ok": True, "who": name, "role": role})
        resp.set_cookie(COOKIE, cookie, httponly=True, samesite="strict", secure=secure_cookie,
                        max_age=int(session_days * 86400))
        return resp

    @app.post("/logout", include_in_schema=False)
    def sign_out():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/api/audit", tags=["server"], dependencies=admin_only, summary="Who did what on this server (newest first)")
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
        if not name or not name.endswith((".xlsx", ".csv", ".txt")) or not os.path.isfile(path):
            raise HTTPException(404, "file not found")
        log(request, "downloaded", file=name)
        return FileResponse(path, filename=name)

    # ---------- start / stop ----------
    def answer(err):
        return {"ok": err is None, "error": err}

    @app.post("/api/start", tags=["jobs"], dependencies=op, summary="Collect post links from accounts")
    def start(body: CrawlRequest, request: Request):
        text = store.lines_for(body.list_id, body.ids) if body.ids else body.text
        err = JOB.start_crawl(text, body.start, body.end, body.tz, list(body.platforms), body.detail, body.template)
        log(request, "started account collection" if not err else "start refused", period=f"{body.start}..{body.end}",
            accounts=len([ln for ln in text.splitlines() if ln.strip()]), detail=body.detail)
        return answer(err)

    @app.post("/api/keywords/start", tags=["jobs"], dependencies=op, summary="Keyword search on the platforms")
    def keywords(body: KeywordRequest, request: Request):
        err = JOB.start_keywords(body.keywords, body.start, body.end, body.tz, list(body.platforms), body.options)
        log(request, "started keyword search" if not err else "start refused", period=f"{body.start}..{body.end}",
            keywords=body.keywords[:20], platforms=list(body.platforms))
        return answer(err)

    @app.post("/api/news/start", tags=["jobs"], dependencies=op, summary="News links from Google")
    def news(body: NewsRequest, request: Request):
        err = JOB.start_news(body.keywords, body.start, body.end, body.tz, body.options)
        log(request, "started news search" if not err else "start refused", period=f"{body.start}..{body.end}",
            keywords=body.keywords[:20])
        return answer(err)

    @app.post("/api/tools/decode", tags=["jobs"], dependencies=op,
              summary="Fix the Google News links in a file (.xlsx/.csv/.txt, base64); the result appears in Exports")
    def decode_file(body: DecodeFile, request: Request):
        import base64
        try:
            raw = base64.b64decode(body.data.split(",", 1)[-1], validate=False)
        except ValueError:
            raw = b""
        err = JOB.start_decode(body.name, raw)
        log(request, "started fixing Google News links in a file" if not err else "file fix refused", file=body.name[:80])
        return answer(err)

    @app.post("/api/stop", tags=["jobs"], dependencies=op, summary="Stop the running job (results so far are kept)")
    def stop(request: Request):
        JOB.stop()
        log(request, "stopped the job")
        return answer(None)

    # ---------- verification ("I'm not a robot") solved by a person from the dashboard ----------
    solving: dict = {}
    @app.get("/api/captcha/screenshot", tags=["verification"], dependencies=op,
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

    @app.post("/api/captcha", tags=["verification"], dependencies=op,
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

    @app.post("/api/schedules/save", tags=["schedules"], dependencies=op, summary="Create or change a schedule")
    def schedule_save(body: ScheduleIn, request: Request):
        def go():
            s = sched_mod.get().save(body.model_dump())
            log(request, "changed a schedule" if body.id else "created a schedule", name=s["name"])
            return {"ok": True, "schedule": s}
        return sched_call(go)

    @app.post("/api/schedules/delete", tags=["schedules"], dependencies=op)
    def schedule_delete(body: ScheduleRef, request: Request):
        ok = sched_mod.get().delete(body.id)
        if ok:
            log(request, "deleted a schedule", id=body.id)
        return {"ok": ok, "error": None if ok else "That schedule no longer exists."}

    @app.post("/api/schedules/run", tags=["schedules"], dependencies=op, summary="Run a schedule now")
    def schedule_run(body: ScheduleRef, request: Request):
        err = sched_mod.get().run_now(body.id)
        if not err:
            log(request, "ran a schedule now", id=body.id)
        return answer(err)

    # ---------- logins ----------
    @app.get("/api/logins", tags=["logins"], dependencies=auth, summary="Which platforms the server is signed in to")
    def logins(request: Request):
        return {**logins_info(), "server": SERVER_MODE, "who": getattr(request.state, "who", "")}

    @app.post("/api/logins/cookies", tags=["logins"], dependencies=admin_only,
              summary="Sign the server in with cookies exported from your browser")
    def import_cookies(body: CookieImport, request: Request):
        text = body.cookies if isinstance(body.cookies, str) else json.dumps(body.cookies)
        res = JOB.import_cookies(text)
        log(request, "imported cookies" if res.get("ok") else "cookie import failed",   # never the cookie values
            count=res.get("added", 0), signed_in=[p for p, v in (res.get("platforms") or {}).items() if v])
        return res

    @app.post("/api/login", include_in_schema=False, dependencies=admin_only)
    def login():
        return answer(JOB.start_login())

    if not SERVER_MODE:                      # these look at folders on the computer itself — not on a server
        @app.get("/api/old-profiles", include_in_schema=False, dependencies=admin_only)
        def old_profiles():
            from .paths import find_old_profiles
            return {"profiles": find_old_profiles()[:10]}

        @app.post("/api/logins/import", include_in_schema=False, dependencies=admin_only)
        def import_profile(body: dict):
            if JOB.running:
                return answer("Wait until the current job has finished (or Stop it) and try again.")
            from .paths import import_profile as do_import
            return answer(do_import(body.get("path", ""), JOB.log) or None)

    # ---------- the team's API scraper ----------
    @app.get("/api/apiscraper/info", tags=["api scraper"], dependencies=op,
             summary="Whether the API scraper is ready, and its platforms")
    def apiscraper_info():
        from .apiscraper import runner
        return runner.info()

    @app.get("/api/apiscraper/balance", tags=["api scraper"], dependencies=op, summary="Credit balance of the API key")
    def apiscraper_balance():
        from .apiscraper import runner
        return runner.balance()

    @app.post("/api/apiscraper/start", tags=["api scraper"], dependencies=op, summary="Run the API scraper")
    def apiscraper_start(body: ApiScrape, request: Request):
        cfg = body.model_dump(by_alias=True)
        err = JOB.start_apiscrape(cfg)
        log(request, "started the API scraper" if not err else "API scraper refused", mode=body.mode,
            platform=body.platform, budget=body.budget)
        return answer(err)

    @app.get("/api/apiscraper/settings", tags=["api scraper"], dependencies=admin_only,
             summary="API keys (only whether they are set and their last 4 characters)")
    def apiscraper_settings():
        from .apiscraper import settings as api_settings
        return api_settings.public()

    @app.post("/api/apiscraper/settings", tags=["api scraper"], dependencies=admin_only, summary="Set the API keys")
    def apiscraper_settings_save(body: ApiKeys, request: Request):
        from .apiscraper import settings as api_settings
        pub = api_settings.save(body.api_key or None, body.twitter_api_key or None, body.default_budget,
                                body.clear_api_key, body.clear_twitter, body.daily_cap, body.monthly_cap,
                                body.auto_fill, body.fill_cap, body.comments, body.cmt_posts, body.cmt_per_post)
        log(request, "changed the API scraper keys", api_key=bool(body.api_key), twitter=bool(body.twitter_api_key),
            daily_cap=body.daily_cap, monthly_cap=body.monthly_cap,
            cleared=[k for k, v in (("api", body.clear_api_key), ("twitter", body.clear_twitter)) if v])
        return {"ok": True, **pub}

    # ---------- reporting (database + topics) ----------
    @app.get("/api/report/topics", tags=["reporting"], dependencies=auth, summary="Topics and what the database holds")
    def report_topics():
        from . import archive
        return {"topics": archive.topics(), "stats": archive.stats()}

    @app.post("/api/report/topics", tags=["reporting"], dependencies=op, summary="Add or change a topic")
    def report_topic_save(body: Topic, request: Request):
        from . import archive
        try:
            t = archive.save_topic(body.name, body.keywords, body.id)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        log(request, "saved a topic", name=t["name"], keywords=len(t["keywords"]))
        return {"ok": True, "topic": t}

    @app.delete("/api/report/topics/{tid}", tags=["reporting"], dependencies=op, summary="Delete a topic")
    def report_topic_delete(tid: str, request: Request):
        from . import archive
        ok = archive.delete_topic(tid)
        log(request, "deleted a topic", id=tid)
        return {"ok": ok}

    @app.post("/api/report/topics/delete", tags=["reporting"], dependencies=op, summary="Delete a topic")
    def report_topic_delete_post(body: Topic, request: Request):
        from . import archive
        ok = archive.delete_topic(body.id or "")
        log(request, "deleted a topic", id=body.id)
        return {"ok": ok}

    @app.post("/api/report", tags=["reporting"], dependencies=auth,
              summary="A report: the team's Excel (+ Summary sheet) for a topic and period, from the database")
    def report_make(body: ReportRequest, request: Request):
        from . import archive
        r = archive.report_from(body.model_dump(), OUTPUT_DIR)
        log(request, "made a report", kind=body.kind, period=f"{body.start}..{body.end}", rows=r.get("total"))
        return r

    @app.post("/api/report/import", tags=["reporting"], dependencies=op,
              summary="Put the Online / Medsos Excel files already in Exports into the database")
    def report_import(request: Request):
        from . import archive
        r = archive.import_folder(OUTPUT_DIR)
        log(request, "imported Exports into the reporting database", files=r["files"])
        return r

    # ---------- users (sign-in accounts and their rights) ----------
    def user_call(fn):
        if not users:
            return {"ok": False, "error": "User accounts are not enabled on this copy."}
        try:
            return fn()
        except UserError as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/me", tags=["users"], dependencies=auth, summary="Who is signed in, and their role")
    def me(request: Request):
        return {"who": request.state.who, "role": request.state.role, "server": SERVER_MODE,
                "accounts": bool(users), "password": bool(users and request.state.who in users.data)}

    @app.post("/api/me/password", tags=["users"], dependencies=auth, summary="Change my own password")
    def my_password(body: PasswordChange, request: Request):
        def go():
            users.change_password(request.state.who, body.old, body.new)
            log(request, "changed own password")
            return {"ok": True}
        return user_call(go)

    @app.post("/api/me/apikey", tags=["users"], dependencies=auth,
              summary="A new personal API key for me (shown once; the old one stops working)")
    def my_api_key(request: Request):
        def go():
            key = users.new_api_key(request.state.who)
            log(request, "made a new personal API key")
            return {"ok": True, "api_key": key}
        return user_call(go)

    @app.get("/api/users", tags=["users"], dependencies=admin_only)
    def users_list():
        return {"users": users.list() if users else []}

    @app.post("/api/users/save", tags=["users"], dependencies=admin_only, summary="Create a user or change role/password")
    def users_save(body: UserIn, request: Request):
        def go():
            new = body.username.strip().lower() not in users.data
            u = users.save(body.username, body.role, body.password, body.disabled, by=request.state.who)
            log(request, "created a user" if new else "changed a user", user=u["username"], role=u["role"],
                disabled=u["disabled"], password_set=bool(body.password))
            return {"ok": True, "user": u}
        return user_call(go)

    @app.post("/api/users/delete", tags=["users"], dependencies=admin_only)
    def users_delete(body: UserRef, request: Request):
        def go():
            users.delete(body.username, by=request.state.who)
            log(request, "deleted a user", user=body.username)
            return {"ok": True}
        return user_call(go)

    @app.post("/api/users/apikey", tags=["users"], dependencies=admin_only,
              summary="A new API key for a user (shown once; their old one stops working)")
    def users_api_key(body: UserRef, request: Request):
        def go():
            key = users.new_api_key(body.username)
            log(request, "made a new API key for a user", user=body.username)
            return {"ok": True, "api_key": key}
        return user_call(go)

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

    @app.post("/api/accounts/add", tags=["account lists"], dependencies=op)
    def accounts_add(body: AccountsAdd, request: Request):
        log(request, "added accounts", list=body.list_id)
        return list_change(lambda: store.add(body.list_id, body.text, body.group))

    @app.post("/api/accounts/remove", tags=["account lists"], dependencies=op)
    def accounts_remove(body: AccountsRemove, request: Request):
        log(request, "removed accounts", list=body.list_id, count=len(body.ids))
        return list_change(lambda: {"removed": store.remove(body.list_id, body.ids)})

    @app.post("/api/lists/create", tags=["account lists"], dependencies=op)
    def lists_create(body: ListCreate, request: Request):
        log(request, "created a list", name=body.name)
        def go():
            list_id, err = store.create_list(body.name)
            return {"ok": err is None, "error": err, "id": list_id}
        return list_change(go)

    @app.post("/api/lists/delete", tags=["account lists"], dependencies=op)
    def lists_delete(body: ListDelete, request: Request):
        log(request, "deleted a list", list=body.id)
        return list_change(lambda: {"ok": store.delete_list(body.id)})

    app.state.audit = app_state["audit"]
    return app


_SIGN_IN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Working Teamz · Sign in</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPGRlZnM+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9Ind0ZyIgeDE9IjYiIHkxPSI0IiB4Mj0iNTgiIHkyPSI2MCIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPgogICAgICA8c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM2MzY2ZjEiLz48c3RvcCBvZmZzZXQ9Ii41NSIgc3RvcC1jb2xvcj0iIzNiODJmNiIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzA2YjZkNCIvPgogICAgPC9saW5lYXJHcmFkaWVudD4KICAgIDxsaW5lYXJHcmFkaWVudCBpZD0id3RzIiB4MT0iMCIgeTE9IjAiIHgyPSIwIiB5Mj0iMSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii41NSIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iI2ZmZiIgc3RvcC1vcGFjaXR5PSIwIi8+PC9saW5lYXJHcmFkaWVudD4KICA8L2RlZnM+CiAgPHJlY3QgeD0iMiIgeT0iMiIgd2lkdGg9IjYwIiBoZWlnaHQ9IjYwIiByeD0iMTgiIGZpbGw9InVybCgjd3RnKSIvPgogIDxyZWN0IHg9IjIiIHk9IjIiIHdpZHRoPSI2MCIgaGVpZ2h0PSIzMCIgcng9IjE4IiBmaWxsPSJ1cmwoI3d0cykiIG9wYWNpdHk9Ii41Ii8+CiAgPHBhdGggZD0iTTEzIDIybDYuNSAyMUwyNiAyOGw2LjUgMTVMMzkgMjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KICA8cGF0aCBkPSJNMzkgMjJoMTNNNDUuNSAyMnYyMSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxjaXJjbGUgY3g9IjUxIiBjeT0iMTMiIHI9IjMuMiIgZmlsbD0iI2E1ZjNmYyIvPgogIDxwYXRoIGQ9Ik00NS42IDExLjJhNy41IDcuNSAwIDAgMSA3LjItNC4xIiBmaWxsPSJub25lIiBzdHJva2U9IiNhNWYzZmMiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBvcGFjaXR5PSIuOSIvPgo8L3N2Zz4="><meta name="theme-color" content="#4f46e5">
<style>
:root{--ink:#0f172a;--mute:#5b6478;--line:rgba(255,255,255,.75);--grad:linear-gradient(135deg,#6366f1,#3b82f6 55%,#06b6d4)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;color:var(--ink);padding:16px;
  font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;background:#eef2fb;overflow:hidden}
body::before{content:"";position:fixed;inset:-20%;z-index:-1;background:
  radial-gradient(40% 45% at 15% 20%,rgba(129,140,248,.6),transparent 70%),
  radial-gradient(35% 40% at 85% 15%,rgba(34,211,238,.5),transparent 70%),
  radial-gradient(45% 50% at 75% 90%,rgba(167,139,250,.5),transparent 72%),
  linear-gradient(160deg,#f5f7ff,#eef6ff 50%,#f3f0ff)}
form{background:rgba(255,255,255,.58);border:1px solid var(--line);border-radius:24px;padding:32px 28px;
  width:min(390px,100%);backdrop-filter:blur(24px) saturate(170%);-webkit-backdrop-filter:blur(24px) saturate(170%);
  box-shadow:0 30px 60px -25px rgba(30,41,90,.4)}
.brand{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.brand img{width:48px;height:48px;filter:drop-shadow(0 6px 14px rgba(79,70,229,.4))}
.brand b{display:block;font-size:20px;font-weight:780;letter-spacing:-.4px;background:var(--grad);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.brand small{color:var(--mute);font-size:13px}
p{color:var(--mute);margin:0 0 16px;font-size:14px}
input{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:rgba(255,255,255,.75);
  color:inherit;font:inherit;outline:none}
input:focus{border-color:rgba(99,102,241,.7);box-shadow:0 0 0 4px rgba(99,102,241,.15)}
button{margin-top:14px;width:100%;padding:12px;border:0;border-radius:12px;background:var(--grad);color:#fff;
  font:650 15px Inter,system-ui,sans-serif;cursor:pointer;box-shadow:0 12px 24px -10px rgba(79,70,229,.7)}
button:hover{filter:brightness(1.07)}
#e{color:#dc2626;min-height:1.5em;margin:10px 0 0;font-size:14px}
#sw{display:block;margin-top:12px;font-size:13px;color:var(--mute);text-decoration:none}
@media (prefers-color-scheme:dark){
  :root{--ink:#e6e9f5;--mute:#a3abc4;--line:rgba(148,163,184,.2);color-scheme:dark}
  body{background:#0b1020}
  body::before{background:radial-gradient(40% 45% at 15% 20%,rgba(99,102,241,.4),transparent 70%),
    radial-gradient(35% 40% at 85% 15%,rgba(6,182,212,.28),transparent 70%),
    radial-gradient(45% 50% at 75% 90%,rgba(139,92,246,.32),transparent 72%),linear-gradient(160deg,#0b1020,#0d1328 50%,#120f26)}
  form{background:rgba(22,27,48,.6)}
  input{background:rgba(30,36,62,.65)}
  .brand b{background:linear-gradient(135deg,#a5b4fc,#67e8f9);-webkit-background-clip:text;background-clip:text}
}
</style></head><body>
<form id="f"><div class="brand"><img alt="" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCA2NCA2NCI+CiAgPGRlZnM+CiAgICA8bGluZWFyR3JhZGllbnQgaWQ9Ind0ZyIgeDE9IjYiIHkxPSI0IiB4Mj0iNTgiIHkyPSI2MCIgZ3JhZGllbnRVbml0cz0idXNlclNwYWNlT25Vc2UiPgogICAgICA8c3RvcCBvZmZzZXQ9IjAiIHN0b3AtY29sb3I9IiM2MzY2ZjEiLz48c3RvcCBvZmZzZXQ9Ii41NSIgc3RvcC1jb2xvcj0iIzNiODJmNiIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iIzA2YjZkNCIvPgogICAgPC9saW5lYXJHcmFkaWVudD4KICAgIDxsaW5lYXJHcmFkaWVudCBpZD0id3RzIiB4MT0iMCIgeTE9IjAiIHgyPSIwIiB5Mj0iMSI+PHN0b3Agb2Zmc2V0PSIwIiBzdG9wLWNvbG9yPSIjZmZmIiBzdG9wLW9wYWNpdHk9Ii41NSIvPjxzdG9wIG9mZnNldD0iMSIgc3RvcC1jb2xvcj0iI2ZmZiIgc3RvcC1vcGFjaXR5PSIwIi8+PC9saW5lYXJHcmFkaWVudD4KICA8L2RlZnM+CiAgPHJlY3QgeD0iMiIgeT0iMiIgd2lkdGg9IjYwIiBoZWlnaHQ9IjYwIiByeD0iMTgiIGZpbGw9InVybCgjd3RnKSIvPgogIDxyZWN0IHg9IjIiIHk9IjIiIHdpZHRoPSI2MCIgaGVpZ2h0PSIzMCIgcng9IjE4IiBmaWxsPSJ1cmwoI3d0cykiIG9wYWNpdHk9Ii41Ii8+CiAgPHBhdGggZD0iTTEzIDIybDYuNSAyMUwyNiAyOGw2LjUgMTVMMzkgMjIiIGZpbGw9Im5vbmUiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSI1IiBzdHJva2UtbGluZWNhcD0icm91bmQiIHN0cm9rZS1saW5lam9pbj0icm91bmQiLz4KICA8cGF0aCBkPSJNMzkgMjJoMTNNNDUuNSAyMnYyMSIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgogIDxjaXJjbGUgY3g9IjUxIiBjeT0iMTMiIHI9IjMuMiIgZmlsbD0iI2E1ZjNmYyIvPgogIDxwYXRoIGQ9Ik00NS42IDExLjJhNy41IDcuNSAwIDAgMSA3LjItNC4xIiBmaWxsPSJub25lIiBzdHJva2U9IiNhNWYzZmMiIHN0cm9rZS13aWR0aD0iMiIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIiBvcGFjaXR5PSIuOSIvPgo8L3N2Zz4="><div><b>Working Teamz</b><small>Media Monitoring</small></div></div>
<p id="sub">Sign in to continue.</p>
<div id="up"><input id="u" autocomplete="username" placeholder="Username" autofocus>
<input id="p" type="password" autocomplete="current-password" placeholder="Password" style="margin-top:10px"></div>
<div id="kb" style="display:none"><input id="k" type="password" autocomplete="off" placeholder="Access key"></div>
<button>Sign in</button><div id="e"></div>
<a href="#" id="sw">Use an access key instead</a></form>
<script>
let useKey = false;
document.getElementById('sw').onclick = ev => { ev.preventDefault(); useKey = !useKey;
  document.getElementById('up').style.display = useKey ? 'none' : '';
  document.getElementById('kb').style.display = useKey ? '' : 'none';
  document.getElementById('sw').textContent = useKey ? 'Use username and password' : 'Use an access key instead';
  document.getElementById(useKey ? 'k' : 'u').focus(); };
document.getElementById('f').onsubmit = async ev => {
  ev.preventDefault();
  const v = id => document.getElementById(id).value;
  const body = useKey ? {key: v('k')} : {username: v('u'), password: v('p')};
  const r = await fetch('/auth', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r.ok) location.reload();
  else document.getElementById('e').textContent = (await r.json()).error || 'Could not sign in.';
};
</script></body></html>"""
