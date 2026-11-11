"""Run the app as an API server (FastAPI + uvicorn) — for a VPS/cloud machine, usually through Docker
(docker-compose.yml puts it behind Caddy with automatic HTTPS).

Environment:
  Sign-in: user accounts (username + password, role viewer/operator/admin) are managed on the dashboard's Users
  page; the first admin is made with:   python serve.py add-user <name> admin
  (in Docker: docker compose exec collector python serve.py add-user <name> admin) — it prints a password.
  COLLECTOR_API_KEYS   access keys that act as admin: "admin=<key>" (16+ random characters, `openssl rand -hex 24`);
                       for emergencies and scripts. Remove an entry and restart to cancel it.
  COLLECTOR_API_KEY    or a single key (person "admin").
  Keys or at least one user account are required unless HOST is 127.0.0.1.
  ALLOWED_IPS          optional: only these addresses/networks may connect, e.g. "203.0.113.7,198.51.100.0/24"
  SESSION_DAYS         how long a dashboard sign-in lasts (default 7)
  RETENTION_DAYS       delete result files older than this many days (default 30; 0 = keep)
  DEBUG_RETENTION_DAYS delete page copies in outputs/debug older than this (default 7; 0 = keep)
  COLLECTOR_SERVER     1 = no screen: hidden browser, logins by cookie import, verifications solved from the dashboard
  CAPTCHA_WAIT_MINUTES how long a verification waits for a person before that search is skipped (default 10)
  CAPTCHA_WEBHOOK_URL  optional: gets {"text": ...} when a verification is waiting (Slack/Teams/n8n)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  optional: the same messages to Telegram (bot from @BotFather)
  PUBLIC_URL           the dashboard's address, used in that message
  NOTIFY_WEBHOOK_URL   optional: gets {"text": ...} when a scheduled run finishes
  RESULTS_WEBHOOK_URL  optional: every finished search's links are POSTed here as JSON (a client's server, n8n...)
  RESULTS_WEBHOOK_TOKEN  optional: sent as "Authorization: Bearer ..." and used to sign the body
  HOST / PORT          default 0.0.0.0 / 8770
  COOKIE_SECURE        1 behind HTTPS (docker-compose sets it): sign-in cookie HTTPS-only, HSTS
  CRAWLER_HOME         folder for the browser profile, account lists, results and audit.log (Docker: /data)
"""
import ipaddress
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def read_keys(env=os.environ) -> dict:
    """{person: key} from COLLECTOR_API_KEYS / COLLECTOR_API_KEY. Exits with a clear message on a bad entry."""
    keys = {}
    for entry in filter(None, (e.strip() for e in env.get("COLLECTOR_API_KEYS", "").split(","))):
        name, sep, key = entry.partition("=")
        name, key = name.strip().lower(), key.strip()
        if not sep or not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
            sys.exit(f"COLLECTOR_API_KEYS: write each entry as name=key (name: letters, digits, - or _), not {entry[:12]!r}...")
        keys[name] = key
    if env.get("COLLECTOR_API_KEY", "").strip():
        keys.setdefault("admin", env["COLLECTOR_API_KEY"].strip())
    for name, key in keys.items():
        if len(key) < 16:
            sys.exit(f"The key of {name!r} is too short — use at least 16 random characters (openssl rand -hex 24).")
    if len(set(keys.values())) < len(keys):
        sys.exit("Two people have the same key — give everyone their own key.")
    return keys


def add_user(argv):
    """python serve.py add-user <name> [viewer|operator|admin]  — prints a new random password."""
    import secrets

    from crawler import paths
    from crawler.users import UserError, Users
    if len(argv) < 1:
        sys.exit("Usage: python serve.py add-user <name> [viewer|operator|admin]")
    name, role = argv[0], (argv[1] if len(argv) > 1 else "operator")
    password = os.environ.get("NEW_PASSWORD") or secrets.token_urlsafe(12)
    try:
        u = Users(os.path.join(paths.APP_DATA, "users.json")).save(name, role, password)
    except UserError as e:
        sys.exit(str(e))
    print(f"{u['username']}\t{u['role']}\t{password}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "add-user":
        return add_user(sys.argv[2:])
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8770"))
    keys = read_keys()
    local_only = host in ("127.0.0.1", "localhost", "::1")
    from crawler import paths as _paths
    users_file = os.path.join(_paths.APP_DATA, "users.json")
    has_users = os.path.exists(users_file) and os.path.getsize(users_file) > 5
    if not keys and not has_users and not local_only:
        sys.exit("No way to sign in is set up. Anyone who can reach this server could use your signed-in accounts — "
                 "make an admin (python serve.py add-user <name> admin) or set COLLECTOR_API_KEYS, "
                 "or use HOST=127.0.0.1.")
    allowed = [a.strip() for a in os.environ.get("ALLOWED_IPS", "").split(",") if a.strip()]
    for a in allowed:
        try:
            ipaddress.ip_network(a, strict=False)
        except ValueError:
            sys.exit(f"ALLOWED_IPS: {a!r} is not an IP address or network (e.g. 203.0.113.7 or 198.51.100.0/24).")

    import uvicorn

    from crawler import paths, retention
    from crawler.api import create_app
    try:
        paths.migrate()
    except Exception as e:  # noqa: BLE001
        print(f"Note: could not prepare the data folders ({e}). Continuing.")
    days = float(os.environ.get("RETENTION_DAYS", "30") or 0)
    debug_days = float(os.environ.get("DEBUG_RETENTION_DAYS", "7") or 0)
    retention.start(paths.OUTPUT_DIR, days, debug_days)
    app = create_app(keys or None, secure_cookie=os.environ.get("COOKIE_SECURE", "") in ("1", "true", "yes"),
                     allowed_ips=allowed or None, audit_path=os.path.join(paths.APP_DATA, "audit.log"),
                     session_days=float(os.environ.get("SESSION_DAYS", "7") or 7), users_path=users_file)
    from crawler import schedule
    sched = schedule.get()
    sched.audit = lambda who, action, **kw: app.state.audit.add(who, action, "", **kw)
    sched.start()
    from crawler import telegram_bot
    if telegram_bot.start():
        print("Telegram bot: answering /status, /schedules, /stop in the configured chats")
    print(f"Schedules: {len(sched.items)}")
    print(f"Data folder: {os.path.dirname(paths.LISTS_DIR)}  ·  server mode: {'on' if paths.SERVER_MODE else 'off'}")
    print(f"Access keys: {', '.join(sorted(keys)) or '-'}  ·  user accounts: {'yes' if has_users else 'none yet'}  ·  "
          f"allowed addresses: {', '.join(allowed) or 'any'}")
    print(f"Results kept {days:g} days (0 = forever), page copies {debug_days:g} days")
    print(f"API on http://{host}:{port}  ·  docs at /docs  ·  health at /api/health")
    uvicorn.run(app, host=host, port=port, proxy_headers=True, server_header=False,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
