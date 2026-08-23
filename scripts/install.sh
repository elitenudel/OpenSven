#!/usr/bin/env bash
# Installs the EV load balancer as a systemd service running under its own
# dedicated system account. Safe to re-run after `git pull` to deploy an
# update - config.yaml at the install destination is always replaced with
# the one from the source checkout (so new config options actually reach
# existing deployments), but the previous copy is backed up first rather
# than discarded, since it likely has host-specific settings (IPs, fuse
# rating, etc) that need re-applying by hand after each update.
set -euo pipefail

YELLOW='\033[1;33m'
RESET='\033[0m'

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

config_backup=""
if [[ -f "$INSTALL_DIR/config.yaml" ]]; then
  config_backup="$INSTALL_DIR/config.yaml.bak.$(date +%Y%m%d-%H%M%S)"
  mv "$INSTALL_DIR/config.yaml" "$config_backup"
  echo -e "${YELLOW}config.yaml was replaced with the repo's version - your previous one is backed up at:${RESET}"
  echo -e "${YELLOW}  $config_backup${RESET}"
  echo -e "${YELLOW}Re-apply any host-specific settings (IPs, fuse rating, etc.) from it into the new config.yaml.${RESET}"
fi
cp "$SOURCE_DIR/config.yaml" "$INSTALL_DIR/config.yaml"

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
if [[ -n "$config_backup" ]]; then
  echo -e "${YELLOW}>>> config.yaml was reset to the repo's version this run.${RESET}"
  echo -e "${YELLOW}>>> Re-apply your host-specific settings from: $config_backup${RESET}"
  echo
fi
echo "Watch it live:   journalctl -u ${SERVICE_NAME}.service -f -o cat"
echo "Edit config:     $INSTALL_DIR/config.yaml, then: systemctl restart ${SERVICE_NAME}.service"
echo "Deploy an update: git pull (in $SOURCE_DIR), then re-run: sudo $0"
echo "                  (config.yaml gets reset to the repo's version every time - reapply your"
echo "                  settings from the config.yaml.bak.* file it leaves behind)"
