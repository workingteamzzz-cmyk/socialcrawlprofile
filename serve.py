"""Run the app as an API server (FastAPI + uvicorn) — for a VPS/cloud machine, usually through Docker.

    COLLECTOR_API_KEY=<long random key> COLLECTOR_SERVER=1 python serve.py

Environment:
  COLLECTOR_API_KEY  required unless HOST is 127.0.0.1 — at least 16 characters (e.g. `openssl rand -hex 24`)
  COLLECTOR_SERVER   1 = no screen: hidden browser, logins by cookie import, captchas reported instead of waited for
  HOST / PORT        default 0.0.0.0 / 8770
  COOKIE_SECURE      1 when the server is behind HTTPS (recommended), so the dashboard's sign-in cookie is HTTPS-only
  CRAWLER_HOME       folder for the browser profile, account lists and results (Docker: /data)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8770"))
    key = os.environ.get("COLLECTOR_API_KEY", "").strip() or None
    local_only = host in ("127.0.0.1", "localhost", "::1")
    if not key and not local_only:
        sys.exit("COLLECTOR_API_KEY is not set. Anyone who can reach this server could use your signed-in accounts — "
                 "set a long random key (e.g. `openssl rand -hex 24`), or use HOST=127.0.0.1.")
    if key and len(key) < 16:
        sys.exit("COLLECTOR_API_KEY is too short — use at least 16 random characters.")

    import uvicorn

    from crawler import paths
    from crawler.api import create_app
    try:
        paths.migrate()
    except Exception as e:  # noqa: BLE001
        print(f"Note: could not prepare the data folders ({e}). Continuing.")
    app = create_app(key, secure_cookie=os.environ.get("COOKIE_SECURE", "") in ("1", "true", "yes"))
    print(f"Data folder: {os.path.dirname(paths.LISTS_DIR)}  ·  server mode: {'on' if paths.SERVER_MODE else 'off'}")
    print(f"API on http://{host}:{port}  ·  docs at /docs  ·  health at /api/health")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
