#!/usr/bin/env bash
# One-command setup of Post Link Collector on a fresh Ubuntu 22.04/24.04 VPS.
#
#   1. copy the app folder to the server (see README "Quick server setup")
#   2. inside that folder:   sudo bash install-server.sh
#
# It installs Docker, a firewall (only SSH, HTTP, HTTPS open), automatic security updates and swap, asks for the
# people who may use the app (each gets a username + password; the first is admin) and an address, then starts it with HTTPS.
# Safe to run again: existing settings and data are kept. Settings can also be given beforehand:
#   PEOPLE="ana,budi" DOMAIN=collector.example.com sudo -E bash install-server.sh
set -euo pipefail
cd "$(dirname "$0")"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "Run it with sudo:  sudo bash install-server.sh"
[ -f docker-compose.yml ] && [ -f serve.py ] || fail "Run this inside the post-link-collector folder (the one with serve.py)."

# ---------- 1. the server itself ----------
if [ "${SKIP_SYSTEM:-0}" != 1 ]; then
  bold "1/4  Preparing the server (Docker, firewall, security updates)..."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q curl openssl ufw unattended-upgrades >/dev/null
  if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y -q docker.io >/dev/null
  fi
  if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y -q docker-compose-v2 >/dev/null || apt-get install -y -q docker-compose-plugin >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || fail "Docker Compose could not be installed — send this screen to whoever helps you."

  # Firewall: only SSH (so you can still log in), HTTP and HTTPS.
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null
  # Automatic security updates.
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
    > /etc/apt/apt.conf.d/20auto-upgrades
  # Small servers: 2 GB of swap so Chromium never runs out of memory.
  mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
  if [ "$mem_mb" -lt 6000 ] && ! swapon --show | grep -q .; then
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

# ---------- 2. settings (.env) ----------
KEYS_FILE="${KEYS_FILE:-/root/post-link-collector-keys.txt}"
if [ -f .env ]; then
  bold "2/4  Settings already exist (.env) — keeping them."
  DOMAIN=$(grep -E '^DOMAIN=' .env | cut -d= -f2-)
else
  bold "2/4  Who may use it, and at which address"
  if [ -z "${PEOPLE:-}" ]; then
    read -rp "Names of the people who may use the app, comma separated (e.g. ana,budi,citra) [admin]: " PEOPLE
  fi
  PEOPLE=${PEOPLE:-admin}
  PEOPLE=${PEOPLE// /,}                 # "ana budi" and "ana, budi" both mean two people
  ip=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
  default_domain="${ip//./-}.sslip.io"
  if [ -z "${DOMAIN:-}" ]; then
    echo "Address of the app. Press Enter to use the free one, or type your own domain"
    echo "(your own domain must already point at this server's IP $ip)."
    read -rp "Address [$default_domain]: " DOMAIN
  fi
  DOMAIN=${DOMAIN:-$default_domain}
  DOMAIN=${DOMAIN#https://}; DOMAIN=${DOMAIN#http://}; DOMAIN=${DOMAIN%%/*}

  names_ok=""
  IFS=',' read -ra names <<< "$PEOPLE"
  for raw in "${names[@]}"; do
    name=$(echo "$raw" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
    [ -z "$name" ] && continue
    [[ "$name" =~ ^[a-z0-9][a-z0-9._-]{1,31}$ ]] || \
      fail "\"$raw\" is not a valid username — 2 to 32 letters, digits, dot, - or _ (no spaces)."
    names_ok="${names_ok:+$names_ok,}$name"
  done
  [ -n "$names_ok" ] || fail "No names given."
  emergency=$(openssl rand -hex 24)
  umask 077
  cat > .env <<EOF
DOMAIN=$DOMAIN
# Emergency admin access key (sign-in "Use an access key instead", or the X-API-Key header). Keep it safe.
COLLECTOR_API_KEYS=admin=$emergency
ALLOWED_IPS=
CAPTCHA_WAIT_MINUTES=10
CAPTCHA_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PUBLIC_URL=https://$DOMAIN
NOTIFY_WEBHOOK_URL=
RESULTS_WEBHOOK_URL=
RESULTS_WEBHOOK_TOKEN=
SESSION_DAYS=7
RETENTION_DAYS=30
DEBUG_RETENTION_DAYS=7
EOF
  echo "$names_ok" > .people_pending                # the accounts are made once the app runs (step 4)
  { echo "Post Link Collector — https://$DOMAIN"; echo
    echo "Emergency admin access key (keep it safe; for scripts, or if every password is lost):"
    echo "  $emergency"; echo; } > "$KEYS_FILE"
  chmod 600 .env "$KEYS_FILE" .people_pending
  umask 022
fi

# ---------- 3. start ----------
if [ "${SKIP_START:-0}" != 1 ]; then
  bold "3/4  Building and starting the app (the first time takes 5-15 minutes)..."
  docker compose up -d --build
  bold "4/4  Waiting for the HTTPS certificate and the app..."
  ok=0
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 5 "https://$DOMAIN/api/health" >/dev/null 2>&1; then ok=1; break; fi
    sleep 5
  done
  # First install: one account per name — the first is admin, the others operators — each with a random password.
  if [ -f .people_pending ]; then
    echo "Sign in with username + password (each person can change it under their name in the sidebar):" >> "$KEYS_FILE"
    first=1
    IFS=',' read -ra names < .people_pending
    for name in "${names[@]}"; do
      role="operator"; if [ "$first" = 1 ]; then role="admin"; fi; first=0
      pw=$(openssl rand -hex 8)                       # (no pipe: with pipefail a SIGPIPE would stop the script)
      if docker compose exec -T -e NEW_PASSWORD="$pw" collector python serve.py add-user "$name" "$role" >/dev/null; then
        printf '  %-16s %-9s %s\n' "$name" "$role" "$pw" >> "$KEYS_FILE"
      else
        echo "  (could not create $name — make it later on the Users page)" >> "$KEYS_FILE"
      fi
    done
    rm -f .people_pending
  fi
  if [ "$ok" != 1 ]; then
    echo "The app is running, but https://$DOMAIN does not answer yet."
    echo "- Using your own domain? Check that it points at this server, then wait a few minutes."
    echo "- Some providers also have a firewall in their web panel: allow ports 80 and 443 there."
    echo "- Logs:  docker compose logs --tail 50"
  fi
fi

bold "Done."
echo "Open:   https://$DOMAIN"
if [ -f "$KEYS_FILE" ]; then
  echo
  cat "$KEYS_FILE"
  echo
  echo "(Saved in $KEYS_FILE — give each person only their own username and password, privately.)"
fi
cat <<'EOF'

Next: sign in, click "Import cookies" and import the cookies of the accounts used for collecting.
More people, other roles (viewer / operator / admin), new passwords: the Users page (admins).
Useful later:
  docker compose logs --tail 50        what the app is doing
  docker compose restart               restart
  sudo bash install-server.sh          after uploading a new version (keeps settings and data)
EOF
