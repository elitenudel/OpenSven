#!/usr/bin/env bash
# Sets up public HTTPS access to the dashboard at https://power.kjellner.net:8443/,
# reverse-proxying to the dashboard already running (via install.sh) on
# 127.0.0.1:8080. Uses Caddy for TLS termination + automatic Let's Encrypt
# certs, built with the Cloudflare DNS module so it can complete the ACME
# challenge via DNS-01 - no inbound port 80 is needed for that (only the
# custom public port itself needs forwarding on your router).
#
# Requires CLOUDFLARE_API_TOKEN in the environment: a token scoped to
# "Zone / DNS / Edit" on the kjellner.net zone only (Cloudflare dashboard ->
# My Profile -> API Tokens -> Create Token). It's stored on this machine at
# /etc/caddy/caddy.env (mode 600, owned by the dedicated caddy user) for
# Caddy to read on renewal - a compromise of this host means that token is
# exposed, so scope it tightly.
#
# Safe to re-run: re-downloads the Caddy binary, rewrites the Caddyfile
# (backing up the previous one) and the systemd unit, then restarts. Already
# -issued certificates are cached under /var/lib/caddy and untouched.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: run as root: sudo $0" >&2
  exit 1
fi

DOMAIN="power.kjellner.net"
PUBLIC_PORT=8443
APP_PORT=8080   # must match web.port in config.yaml
CADDY_USER="caddy"
CADDY_BIN="/usr/local/bin/caddy"
CADDYFILE="/etc/caddy/Caddyfile"
ENV_FILE="/etc/caddy/caddy.env"
SERVICE_NAME="caddy"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" ]]; then
  echo "error: CLOUDFLARE_API_TOKEN is not set." >&2
  echo "  Create one at https://dash.cloudflare.com/profile/api-tokens with" >&2
  echo "  \"Zone / DNS / Edit\" permission scoped to the kjellner.net zone," >&2
  echo "  then re-run as:" >&2
  echo "    sudo CLOUDFLARE_API_TOKEN=xxxx $0" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "error: curl not found - install it first (apt install curl)" >&2
  exit 1
fi

case "$(uname -m)" in
  aarch64) CADDY_ARCH_QS="arch=arm64" ;;
  armv7l|armv6l) CADDY_ARCH_QS="arch=arm&goarm=7" ;;
  x86_64) CADDY_ARCH_QS="arch=amd64" ;;
  *)
    echo "error: unrecognized architecture $(uname -m) - add a case for it in this script" >&2
    exit 1
    ;;
esac

if ! id "$CADDY_USER" >/dev/null 2>&1; then
  echo "Creating system user '$CADDY_USER'"
  useradd --system --no-create-home --home-dir /var/lib/caddy --shell /usr/sbin/nologin "$CADDY_USER"
fi

echo "Downloading Caddy (with the Cloudflare DNS module) for $(uname -m)"
curl -fsSL "https://caddyserver.com/api/download?os=linux&${CADDY_ARCH_QS}&p=github.com/caddy-dns/cloudflare" \
  -o "${CADDY_BIN}.new"
chmod 755 "${CADDY_BIN}.new"
mv "${CADDY_BIN}.new" "$CADDY_BIN"
"$CADDY_BIN" version

echo "Writing Caddyfile"
install -d -m 755 /etc/caddy
sed \
  -e "s#__DOMAIN__#${DOMAIN}#g" \
  -e "s#__PUBLIC_PORT__#${PUBLIC_PORT}#g" \
  -e "s#__APP_PORT__#${APP_PORT}#g" \
  "$SOURCE_DIR/deploy/Caddyfile.snippet" > "${CADDYFILE}.new"
if [[ -f "$CADDYFILE" ]] && ! diff -q "$CADDYFILE" "${CADDYFILE}.new" >/dev/null 2>&1; then
  cp "$CADDYFILE" "${CADDYFILE}.bak.$(date +%Y%m%d-%H%M%S)"
fi
mv "${CADDYFILE}.new" "$CADDYFILE"

printf 'CLOUDFLARE_API_TOKEN=%s\n' "$CLOUDFLARE_API_TOKEN" > "$ENV_FILE"
chmod 600 "$ENV_FILE"

install -d -m 700 -o "$CADDY_USER" -g "$CADDY_USER" /var/lib/caddy
# Recursive, so this also takes over caddy.env - readable by the caddy
# user (who owns the process) and by root (systemd's EnvironmentFile is
# read by the manager itself, which always runs as root), nobody else.
chown -R "$CADDY_USER":"$CADDY_USER" /etc/caddy /var/lib/caddy
chmod 600 "$ENV_FILE"

echo "Installing systemd unit to $UNIT_PATH"
cp "$SOURCE_DIR/deploy/caddy.service.template" "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl restart "${SERVICE_NAME}.service"

echo
sleep 1
systemctl --no-pager status "${SERVICE_NAME}.service" || true
echo
echo "-- local dashboard (http://127.0.0.1:${APP_PORT}/):"
curl -fsS "http://127.0.0.1:${APP_PORT}/" | head -c 150 || echo "(not reachable - is ev-balancer.service running? see: systemctl status ev-balancer)"
echo
echo
echo "Public site: https://${DOMAIN}:${PUBLIC_PORT}/"
echo "First load can take a few extra seconds while Caddy issues the cert via Cloudflare DNS-01."
echo "That needs your router forwarding UDP+TCP port ${PUBLIC_PORT} to this machine - the"
echo "certificate issuance itself needs no inbound port at all (DNS-01, not HTTP-01)."
echo
echo "Watch it live:    journalctl -u ${SERVICE_NAME}.service -f -o cat"
echo "Remove it again:  sudo ./scripts/remove-public-https.sh"
