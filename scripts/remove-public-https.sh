#!/usr/bin/env bash
# Stops and removes the Caddy service set up by setup-public-https.sh.
# Deleting the Caddy binary/config and the issued certificates (under
# /var/lib/caddy) are both optional and asked about interactively - the
# certs in particular are worth keeping if you're likely to re-run
# setup-public-https.sh soon, since Let's Encrypt rate-limits how often a
# given domain can request a fresh one.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root: sudo $0" >&2
  exit 1
fi

CADDY_USER="caddy"
CADDY_BIN="/usr/local/bin/caddy"
SERVICE_NAME="caddy"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl daemon-reload
echo "Service stopped, disabled, and unit removed."

if [[ -f "$CADDY_BIN" || -d /etc/caddy ]]; then
  read -r -p "Also delete the Caddy binary and /etc/caddy (config + token)? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -f "$CADDY_BIN"
    rm -rf /etc/caddy
    echo "Removed $CADDY_BIN and /etc/caddy"
  fi
fi

if [[ -d /var/lib/caddy ]]; then
  read -r -p "Also delete /var/lib/caddy (issued certificates)? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -rf /var/lib/caddy
    echo "Removed /var/lib/caddy"
  fi
fi

if id "$CADDY_USER" >/dev/null 2>&1; then
  read -r -p "Also delete the '$CADDY_USER' system user? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    userdel "$CADDY_USER" 2>/dev/null || true
    echo "Removed user $CADDY_USER"
  fi
fi
