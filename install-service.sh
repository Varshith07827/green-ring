#!/usr/bin/env bash
#
# Installs the gateway and the bridge as systemd services, so they start on
# boot, restart on failure, and survive you logging out.
#
#   ./install-service.sh            install and start
#   ./install-service.sh --remove   stop, disable and delete both
#
# Run ./start.sh --setup-only first: this script installs nothing and builds
# nothing, it only wires up what is already there.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$ROOT/repo"
BRIDGE="$ROOT/bridge"
VENV_PY="$BRIDGE/.venv/bin/python"
RUN_USER="${SUDO_USER:-$USER}"

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

command -v systemctl >/dev/null 2>&1 || die "systemd not found - this machine uses something else."

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

if [[ "${1:-}" == "--remove" ]]; then
  for unit in openwa-bridge openwa-gateway; do
    $SUDO systemctl disable --now "$unit" 2>/dev/null || true
    $SUDO rm -f "/etc/systemd/system/$unit.service"
  done
  $SUDO systemctl daemon-reload
  ok "Removed. Nothing starts on boot any more."
  exit 0
fi

# Refuse rather than install units that point at files which are not there -
# a service that fails on every boot is worse than one that was never made.
[[ -f "$REPO/dist/main.js" ]] || die "repo/dist/main.js is missing. Run ./start.sh --setup-only first."
[[ -x "$VENV_PY" ]]          || die "the Python environment is missing. Run ./start.sh --setup-only first."
[[ -f "$ROOT/.env" ]]        || die ".env is missing. Run ./start.sh --setup-only first."

NODE_BIN="$(command -v node)" || die "node is not on PATH"

# Both services write under these. A directory left owned by root - by a first
# run under sudo, or a clone made as root - fails only once systemd starts the
# unit as $RUN_USER, and Restart=always then turns that into a boot loop whose
# error ("the media storage root is not writable") reads like bad config rather
# than bad ownership. Check it here, while there is someone watching.
as_run_user() {
  if [[ "$RUN_USER" == "$(id -un)" ]]; then "$@"; else sudo -u "$RUN_USER" "$@"; fi
}
for dir in "$REPO/data" "$REPO/data/media" "$REPO/data/sessions" "$BRIDGE"; do
  as_run_user mkdir -p "$dir" 2>/dev/null || true
  if ! as_run_user test -w "$dir"; then
    say "$RUN_USER cannot write to:"
    say "  $dir"
    say ""
    say "Fix the ownership and run this again:"
    printf '    [33msudo chown -R %s %s[0m
' "$RUN_USER" "$ROOT"
    say ""
    die "Refusing to install a service that cannot start."
  fi
done

say "installing as user: $RUN_USER"

# The gateway. Restart=always because a crashed engine takes WhatsApp with it,
# and the session data on disk means it recovers without re-pairing.
$SUDO tee /etc/systemd/system/openwa-gateway.service >/dev/null <<EOF
[Unit]
Description=OpenWA gateway (WhatsApp engine)
Documentation=https://github.com/rmyndharis/OpenWA
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$NODE_BIN dist/main
Restart=always
RestartSec=10
# Chrome is memory-hungry; without a ceiling one runaway session can take the
# whole box down rather than just itself.
MemoryMax=2G
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openwa-gateway

[Install]
WantedBy=multi-user.target
EOF

# The bridge. It registers itself against the gateway at startup, so it is
# ordered after it - though it also retries, so the order is a courtesy rather
# than a requirement.
$SUDO tee /etc/systemd/system/openwa-bridge.service >/dev/null <<EOF
[Unit]
Description=OpenWA bridge (HTTP API and MongoDB archive)
After=network-online.target openwa-gateway.service
Wants=network-online.target
PartOf=openwa-gateway.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$BRIDGE
ExecStart=$VENV_PY -m app.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=openwa-bridge

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now openwa-gateway.service
sleep 5
$SUDO systemctl enable --now openwa-bridge.service
sleep 3

printf '\n'
$SUDO systemctl --no-pager --lines=0 status openwa-gateway.service | head -4 || true
printf '\n'
$SUDO systemctl --no-pager --lines=0 status openwa-bridge.service | head -4 || true

HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST="${HOST:-localhost}"

printf '\n\033[32m  Installed and running.\033[0m\n\n'
say "They now start on boot and restart on failure."
say ""
say "  logs      journalctl -u openwa-gateway -f"
say "            journalctl -u openwa-bridge -f"
say "  restart   sudo systemctl restart openwa-bridge"
say "  stop      sudo systemctl stop openwa-gateway openwa-bridge"
say "  remove    ./install-service.sh --remove"
say ""
say "Dashboard:  http://$HOST:2785   (start the session and scan the QR once)"
say "Health:     curl http://127.0.0.1:8000/health"
printf '\n'
