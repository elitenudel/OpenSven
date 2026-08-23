#!/usr/bin/env bash
# Installs the EV load balancer as a systemd service running under its own
# dedicated system account. Safe to re-run after `git pull` to deploy an
# update - config.yaml at the install destination is left alone once it
# exists, so local edits made there survive re-installs.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root: sudo $0" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/ev-balancer"
SERVICE_USER="evbalancer"
SERVICE_NAME="ev-balancer"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! -f "$SOURCE_DIR/config.yaml" ]]; then
  echo "error: $SOURCE_DIR/config.yaml not found - create/edit it before installing" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found - install it first (apt install python3 python3-venv)" >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Creating system user '$SERVICE_USER'"
  useradd --system --no-create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "Syncing app files to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# Wipe old code (but keep config.yaml and the venv), then copy the current
# source tree in fresh, so renamed/removed files don't linger.
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 \
  ! -name 'config.yaml' ! -name '.venv' \
  -exec rm -rf {} +
find "$SOURCE_DIR" -mindepth 1 -maxdepth 1 \
  ! -name '.git' ! -name '.venv' ! -name '__pycache__' ! -name 'config.yaml' \
  -exec cp -a {} "$INSTALL_DIR"/ \;

if [[ ! -f "$INSTALL_DIR/config.yaml" ]]; then
  cp "$SOURCE_DIR/config.yaml" "$INSTALL_DIR/config.yaml"
  echo "Copied initial config.yaml to $INSTALL_DIR - edit it there from now on."
else
  echo "$INSTALL_DIR/config.yaml already exists - leaving it as-is."
fi

echo "Setting up Python virtualenv"
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR"

echo "Installing systemd unit to $UNIT_PATH"
sed \
  -e "s#{{INSTALL_DIR}}#$INSTALL_DIR#g" \
  -e "s#{{SERVICE_USER}}#$SERVICE_USER#g" \
  "$SOURCE_DIR/systemd/ev-balancer.service.template" > "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo
sleep 1
systemctl --no-pager status "${SERVICE_NAME}.service" || true
echo
echo "Installed and running as system user '$SERVICE_USER', app files in $INSTALL_DIR."
echo "Autostarts on boot (system service, no login session required)."
echo
echo "Watch it live:   journalctl -u ${SERVICE_NAME}.service -f -o cat"
echo "Edit config:     $INSTALL_DIR/config.yaml, then: systemctl restart ${SERVICE_NAME}.service"
echo "Deploy an update: git pull (in $SOURCE_DIR), then re-run: sudo $0"
