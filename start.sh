#!/usr/bin/env bash
#
# Starts the whole thing on Linux: the OpenWA gateway and the bridge.
# No Docker - everything runs directly on the host.
#
#   ./start.sh              install what is missing, then run both in this shell
#   ./start.sh --setup-only install and configure, start nothing
#
# Then open http://<server>:2785, start the session once and scan the QR.
# After that, POST {"id": "...", "msg": "..."} to http://<server>:8000/send
#
# For unattended running, use ./install-service.sh instead - systemd keeps both
# alive across reboots and logouts, which this script cannot.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$ROOT/repo"
BRIDGE="$ROOT/bridge"
ROOT_ENV="$ROOT/.env"
REPO_ENV="$REPO/.env"
VENV_PY="$BRIDGE/.venv/bin/python"

SETUP_ONLY=false
[[ "${1:-}" == "--setup-only" ]] && SETUP_ONLY=true

say()  { printf '  %s\n' "$*"; }
step() { printf '\n\033[36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

# Only used for installing packages. Running the app itself needs no root.
SUDO=""
if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 && SUDO="sudo"
fi

# --------------------------------------------------------------------------
# Prerequisites
# --------------------------------------------------------------------------
# Without this the first sign of trouble is a bare "npm: command not found"
# three steps later, which says nothing about what to do.

if ! command -v apt-get >/dev/null 2>&1; then
  warn "This script installs packages with apt (Debian/Ubuntu)."
  warn "On another distro, install these yourself and re-run:"
  warn "  node 22+, python3 3.10+ with venv, and the Chrome runtime libraries"
  warn "  (nss, atk, gtk3, gbm, drm, alsa, cups, xcomposite, xdamage, xrandr)."
fi

node_major() { command -v node >/dev/null 2>&1 && node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || true; }
py_minor()   { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || true; }

NODE_MAJOR="$(node_major)"
PY_MINOR="$(py_minor)"

need_node=false
need_python=false
[[ -z "$NODE_MAJOR" || "$NODE_MAJOR" -lt 22 ]] && need_node=true
[[ -z "$PY_MINOR"  || "$PY_MINOR"  -lt 10 ]] && need_python=true

if [[ "$need_node" == true || "$need_python" == true ]] && command -v apt-get >/dev/null 2>&1; then
  step "Installing prerequisites"
  [[ -z "$NODE_MAJOR" ]] && say "node is not installed" || { [[ "$need_node" == true ]] && say "node v$NODE_MAJOR is too old, needs 22+"; }
  [[ -z "$PY_MINOR" ]] && say "python3 is not installed" || { [[ "$need_python" == true ]] && say "python 3.$PY_MINOR is too old, needs 3.10+"; }

  $SUDO apt-get update -qq

  if [[ "$need_node" == true ]]; then
    # Debian and Ubuntu ship Node far older than 22 in their own repos, so the
    # distro package is not an option here - NodeSource is.
    say "adding the NodeSource repository for Node 22"
    $SUDO apt-get install -y -qq --no-install-recommends ca-certificates curl gnupg
    curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO -E bash - >/dev/null
    $SUDO apt-get install -y -qq nodejs
  fi

  if [[ "$need_python" == true ]]; then
    $SUDO apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip
  fi
fi

NODE_MAJOR="$(node_major)"
PY_MINOR="$(py_minor)"
[[ -n "$NODE_MAJOR" && "$NODE_MAJOR" -ge 22 ]] || die "node 22+ is required (found: ${NODE_MAJOR:-none})"
[[ -n "$PY_MINOR"  && "$PY_MINOR"  -ge 10 ]] || die "python 3.10+ is required (found: ${PY_MINOR:-none})"

# Chrome's shared libraries, plus `patch` for the upstream engine patches and
# ffmpeg for voice-note conversion. Taken from the dependency list upstream
# tests against, minus anything only a container needs.
if command -v apt-get >/dev/null 2>&1 && [[ ! -f "$ROOT/.deps-installed" ]]; then
  step "Installing the Chrome runtime libraries"

  # Refresh the index BEFORE probing: on a fresh server the cache is empty and
  # every name below would otherwise look missing.
  $SUDO apt-get update -qq || warn "apt-get update reported a problem; continuing with the cache as it is"

  # Ubuntu 24.04's 64-bit time_t transition renamed a number of these with a
  # `t64` suffix (libasound2, libatk1.0-0, libatk-bridge2.0-0, libcups2,
  # libgtk-3-0 ...). Probe for the suffixed name first and fall back, rather
  # than hardcoding either spelling - which release renamed what is a moving
  # target, and guessing wrong kills the whole install.
  have_pkg() {
    local candidate
    candidate="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/{print $2; exit}')"
    [[ -n "$candidate" && "$candidate" != "(none)" ]]
  }

  PKGS=()
  UNKNOWN=()
  for base in fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 \
              libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 \
              libnss3 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
              xdg-utils patch curl ca-certificates sqlite3 ffmpeg; do
    if have_pkg "${base}t64"; then
      PKGS+=("${base}t64")
    elif have_pkg "$base"; then
      PKGS+=("$base")
    else
      UNKNOWN+=("$base")
    fi
  done

  # One batch install, quiet unless it fails. If it does, retry individually so
  # a single unavailable package cannot take the other twenty down with it.
  FAILED=()
  APT_LOG="$(mktemp)"
  if [[ ${#PKGS[@]} -gt 0 ]]; then
    if ! $SUDO apt-get install -y -q --no-install-recommends "${PKGS[@]}" >"$APT_LOG" 2>&1; then
      warn "the batch install failed; retrying one package at a time"
      for pkg in "${PKGS[@]}"; do
        $SUDO apt-get install -y -q --no-install-recommends "$pkg" >>"$APT_LOG" 2>&1 || FAILED+=("$pkg")
      done
    fi
  fi

  if [[ ${#UNKNOWN[@]} -gt 0 || ${#FAILED[@]} -gt 0 ]]; then
    warn "some system packages could not be installed:"
    [[ ${#UNKNOWN[@]} -gt 0 ]] && say "  not in any configured repository: ${UNKNOWN[*]}"
    [[ ${#FAILED[@]} -gt 0 ]]  && say "  failed to install: ${FAILED[*]}"
    say "  full apt output: $APT_LOG"
    say ""
    say "Setup continues. If Chrome later refuses to start, this is the place to"
    say "look - check which library it names and install that package by hand."
  else
    rm -f "$APT_LOG"
    touch "$ROOT/.deps-installed"
    ok "system libraries installed"
  fi
fi

# --------------------------------------------------------------------------
# Configuration - one file, at the root
# --------------------------------------------------------------------------

rand() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

if [[ ! -f "$ROOT_ENV" ]]; then
  step "First run - setting up configuration"
  read -rp "  MongoDB URI [mongodb://localhost:27017]: " MONGO
  MONGO="${MONGO:-mongodb://localhost:27017}"

  BRIDGE_KEY="$(rand 24)"

  # The gateway takes API_MASTER_KEY only on its FIRST boot, then authenticates
  # against its own database. If it has already booted, that seeded key - not
  # this file - is the one it accepts, so adopt it rather than inventing one
  # the gateway has never heard of.
  if [[ -f "$REPO/data/.api-key" ]]; then
    GATEWAY_KEY="$(tr -d '[:space:]' < "$REPO/data/.api-key")"
    say "adopting the gateway key it already seeded"
  else
    GATEWAY_KEY="$(rand 32)"
  fi

  cat > "$ROOT_ENV" <<EOF
# ===========================================================================
# Configuration - this is the only file you edit.
#
# repo/.env is generated from this one by start.sh on every run. Don't edit
# that; your changes get overwritten.
# ===========================================================================

# --- Set these -------------------------------------------------------------

# Where every message is archived, both directions.
MONGO_URI=$MONGO

# --- Generated once - keep them secret, no need to change them -------------

# What you send from Postman:  Authorization: Bearer <this>
BRIDGE_API_KEY=$BRIDGE_KEY

# Shared with the WhatsApp gateway. start.sh copies it into repo/.env.
OPENWA_API_KEY=$GATEWAY_KEY

# Signs the gateway's internal event deliveries to the bridge.
EVENTS_SECRET=$(rand 24)

# --- Optional --------------------------------------------------------------
# Everything else has a working default. The common ones:
#
#   BRIDGE_PORT=8000            the port you POST to
#   BRIDGE_HOST=0.0.0.0         127.0.0.1 to refuse everything but this machine
#   MEDIA_DIR=data/media        where photos and voice notes are written
#   DEFAULT_COUNTRY_CODE=       e.g. 91 to accept bare 10-digit numbers
EOF
  ok "wrote .env"
  printf '\n  Your key for sending messages:\n  \033[33m%s\033[0m\n\n' "$BRIDGE_KEY"
fi

# Read it back for everything below.
get_env() { grep -E "^$1=" "$ROOT_ENV" 2>/dev/null | head -1 | cut -d= -f2- || true; }
GATEWAY_KEY="$(get_env OPENWA_API_KEY)"
BRIDGE_KEY="$(get_env BRIDGE_API_KEY)"
MONGO_URI="$(get_env MONGO_URI)"
[[ -n "$GATEWAY_KEY" ]] || die "OPENWA_API_KEY is missing from .env"

# The seeded key wins, always. A mismatch here is a silent 401 on every call.
if [[ -f "$REPO/data/.api-key" ]]; then
  SEEDED="$(tr -d '[:space:]' < "$REPO/data/.api-key")"
  if [[ -n "$SEEDED" && "$SEEDED" != "$GATEWAY_KEY" ]]; then
    warn "OPENWA_API_KEY in .env is not the key this gateway accepts."
    say ".env has           : ${GATEWAY_KEY:0:12}..."
    say "the gateway seeded : ${SEEDED:0:12}..."
    say ""
    say "The gateway takes API_MASTER_KEY only on its FIRST boot, then uses its"
    say "own database. Yours has already booted, so .env is being ignored."
    say ""
    say "Fix it by putting the seeded key in .env:"
    printf '    \033[33mOPENWA_API_KEY=%s\033[0m\n' "$SEEDED"
    say ""
    say "Or delete repo/data to start the gateway over - which also unpairs"
    say "WhatsApp and means scanning the QR again."
    die "Refusing to start with a key the gateway will reject."
  fi
fi

# --------------------------------------------------------------------------
# MongoDB
# --------------------------------------------------------------------------
# Only worth offering when the URI points at this machine. A remote or Atlas
# URI is somebody else's server.

if [[ "$MONGO_URI" =~ ^mongodb(\+srv)?://(localhost|127\.0\.0\.1) ]]; then
  PORT=27017
  [[ "$MONGO_URI" =~ :([0-9]{2,5}) ]] && PORT="${BASH_REMATCH[1]}"
  if ! (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    warn "MONGO_URI points at this machine, but nothing is listening on port $PORT."
    say "Install MongoDB, start it, or point MONGO_URI at another server."
    say "  https://www.mongodb.com/docs/manual/administration/install-on-linux/"
    say "The bridge will not start until it can reach one."
  fi
fi

# --------------------------------------------------------------------------
# Install and build
# --------------------------------------------------------------------------

if [[ ! -d "$REPO/node_modules" ]]; then
  step "Installing gateway dependencies (first run, a few minutes)"
  cd "$REPO"
  # --ignore-scripts is required: npm otherwise runs node-gyp for
  # better-sqlite3, which needs a full C++ toolchain, while the package already
  # ships a prebuilt binary its loader prefers. postinstall is then run by hand
  # for the upstream whatsapp-web.js/baileys patches, which do matter.
  npm ci --ignore-scripts
  node scripts/postinstall.js
  cd "$ROOT"
fi

# Chrome. Puppeteer's own download is skipped by --ignore-scripts, so fetch it
# explicitly. On arm64 there is no Chrome for Testing build, so the distro's
# chromium is used instead.
CHROME_BIN=""
ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
  if ! command -v chromium >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    step "Installing chromium (arm64 has no Chrome for Testing build)"
    $SUDO apt-get install -y -qq --no-install-recommends chromium
  fi
  CHROME_BIN="$(command -v chromium || true)"
else
  export PUPPETEER_CACHE_DIR="$ROOT/.cache/puppeteer"
  CHROME_BIN="$(find "$PUPPETEER_CACHE_DIR" -type f -name chrome -perm -u+x 2>/dev/null | head -1 || true)"
  if [[ -z "$CHROME_BIN" ]]; then
    step "Downloading Chrome for Puppeteer (first run only)"
    (cd "$REPO" && ./node_modules/.bin/puppeteer browsers install chrome >/dev/null)
    CHROME_BIN="$(find "$PUPPETEER_CACHE_DIR" -type f -name chrome -perm -u+x 2>/dev/null | head -1 || true)"
  fi
fi
[[ -n "$CHROME_BIN" ]] || die "No Chrome found. Install chromium, or set PUPPETEER_EXECUTABLE_PATH in repo/.env."

# --------------------------------------------------------------------------
# Regenerate the gateway's config, every run
# --------------------------------------------------------------------------

cat > "$REPO_ENV" <<EOF
# OpenWA gateway - generated by start.sh from ../.env. Do not edit.
PORT=2785
NODE_ENV=production
AUTO_START_SESSIONS=true

DATABASE_TYPE=sqlite
DATABASE_NAME=./data/openwa.sqlite
DATABASE_SYNCHRONIZE=true
DATABASE_LOGGING=false

ENGINE_TYPE=whatsapp-web.js
SESSION_DATA_PATH=./data/sessions
PUPPETEER_HEADLESS=true
PUPPETEER_ARGS=--no-sandbox,--disable-setuid-sandbox,--disable-dev-shm-usage
PUPPETEER_EXECUTABLE_PATH=$CHROME_BIN

# The full contact record on inbound messages, and a real number for privacy-id
# (@lid) senders, so the archive records who sent what.
WEBHOOK_CONTACT_DETAILS=true
RESOLVE_LID_TO_PHONE=true
CHAT_MEDIA_ARCHIVE_ENABLED=true

WEBHOOK_TIMEOUT=10000
WEBHOOK_RETRY_DELAY=5000
WEBHOOK_SHUTDOWN_DRAIN_MS=15000
# The bridge listens on loopback, which the SSRF guard blocks by default.
# Without this allowlist the gateway cannot deliver events to it at all.
SSRF_ALLOWED_HOSTS=localhost,127.0.0.1

STORAGE_TYPE=local
STORAGE_LOCAL_PATH=./data/media

REDIS_ENABLED=false
QUEUE_ENABLED=false
CACHE_ENABLED=false

API_MASTER_KEY=$GATEWAY_KEY
CSP_UPGRADE_INSECURE_REQUESTS=false
EOF

if [[ ! -f "$REPO/dist/main.js" ]]; then
  step "Building the gateway (first run only)"
  (cd "$REPO" && npm run build)
fi

if [[ ! -d "$REPO/dashboard/dist" ]]; then
  step "Building the dashboard (first run only)"
  (cd "$REPO" && npm run dashboard:build) || warn "dashboard build failed - the API still works, the web UI will not"
fi

# The venv is only built once, so a pull that adds a dependency would leave it
# missing until someone deleted .venv by hand - and the failure surfaces at
# request time, deep in a library, saying nothing about an install step. Stamp
# the requirements and reinstall when they move.
REQ_FILE="$BRIDGE/requirements.txt"
REQ_STAMP="$BRIDGE/.venv/.requirements-sha256"
req_hash() { sha256sum "$REQ_FILE" 2>/dev/null | cut -d' ' -f1; }

if [[ ! -x "$VENV_PY" ]]; then
  step "Creating the Python environment (first run only)"
  python3 -m venv "$BRIDGE/.venv"
  "$VENV_PY" -m pip install --upgrade pip --quiet
  "$VENV_PY" -m pip install -r "$REQ_FILE"
  req_hash > "$REQ_STAMP"
elif [[ "$(cat "$REQ_STAMP" 2>/dev/null)" != "$(req_hash)" ]]; then
  step "Python dependencies changed - installing"
  "$VENV_PY" -m pip install -r "$REQ_FILE"
  req_hash > "$REQ_STAMP"
fi

if [[ "$SETUP_ONLY" == true ]]; then
  ok "Setup complete. Start it with ./start.sh, or install services with ./install-service.sh"
  exit 0
fi

# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

step "Starting"

# A leftover gateway or bridge from an earlier run still owns its port, and the
# copy started here would exit with EADDRINUSE seconds later - after this script
# had already printed "Running" and a set of instructions that cannot work.
BRIDGE_PORT="$(get_env BRIDGE_PORT)"
BRIDGE_PORT="${BRIDGE_PORT:-8000}"
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
for p in 2785 "$BRIDGE_PORT"; do
  if port_busy "$p"; then
    warn "port $p is already in use - something from an earlier run is still going."
    say "  see what:  sudo ss -lntp | grep :$p"
    say "  stop it:   sudo fuser -k $p/tcp"
    say "  or, if it was installed as a service:"
    say "             sudo systemctl stop openwa-gateway openwa-bridge"
    die "Refusing to start a second copy on port $p."
  fi
done

cd "$REPO"
node dist/main &
GATEWAY_PID=$!
cd "$ROOT"

# Both die together: a bridge with no gateway sends nothing, and leaving one
# behind means the next run cannot bind its port.
cleanup() {
  trap - INT TERM EXIT
  kill "$GATEWAY_PID" 2>/dev/null || true
  [[ -n "${BRIDGE_PID:-}" ]] && kill "$BRIDGE_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# The bridge registers itself against the gateway at startup, so it goes second.
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:2785/api/sessions" 2>/dev/null && break
  # 401 means it is up and asking for a key, which is all we need to know.
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:2785/api/sessions" 2>/dev/null)" == "401" ]] && break
  sleep 2
done

cd "$BRIDGE"
"$VENV_PY" -m app.main &
BRIDGE_PID=$!
cd "$ROOT"

sleep 3

# Never announce success for a process that has already died. Both of these can
# exit during startup - the gateway on a port clash, the bridge when MongoDB
# refuses it - and printing "Running" over the top of that error sends people
# looking in the wrong place.
kill -0 "$GATEWAY_PID" 2>/dev/null || die "the gateway exited during startup - its error is above."
kill -0 "$BRIDGE_PID"  2>/dev/null || die "the bridge exited during startup - its error is above.
  A MongoDB 'requires authentication' there means MONGO_URI in .env needs a
  username and password, e.g. mongodb://user:pass@localhost:27017/?authSource=admin"

HOSTNAME_GUESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOSTNAME_GUESS="${HOSTNAME_GUESS:-localhost}"

printf '\n\033[32m  Running.\033[0m\n\n'
say "1. Open the dashboard:  http://$HOSTNAME_GUESS:2785"
say "   Start the 'default' session and scan the QR. Once only."
say ""
say "2. Then send messages:  POST http://$HOSTNAME_GUESS:8000/send"
say "   body    {\"id\": \"919876543210\", \"msg\": \"hello\"}"
printf '     header  \033[33mAuthorization: Bearer %s\033[0m\n' "$BRIDGE_KEY"
say ""
say "Check readiness:  curl http://127.0.0.1:8000/health"
say "Ctrl-C stops both. For unattended running: ./install-service.sh"
printf '\n'

wait
