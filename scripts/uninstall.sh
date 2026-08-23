#!/usr/bin/env bash
# Stops and removes the ev-balancer systemd service. Deleting the app
# files/config and the dedicated system user are both optional and asked
# about interactively, since they're destructive.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root: sudo $0" >&2
  exit 1
fi

INSTALL_DIR="/opt/ev-balancer"
SERVICE_USER="evbalancer"
SERVICE_NAME="ev-balancer"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
systemctl disable "${SERVICE_NAME}.service" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl daemon-reload
echo "Service stopped, disabled, and unit removed."

if [[ -d "$INSTALL_DIR" ]]; then
  read -r -p "Also delete $INSTALL_DIR, including config.yaml? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    rm -rf "$INSTALL_DIR"
    echo "Removed $INSTALL_DIR"
  fi
fi

if id "$SERVICE_USER" >/dev/null 2>&1; then
  read -r -p "Also delete the '$SERVICE_USER' system user? [y/N] " reply
  if [[ "$reply" =~ ^[Yy]$ ]]; then
    userdel "$SERVICE_USER" 2>/dev/null || true
    echo "Removed user $SERVICE_USER"
  fi
fi
