#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/aws-ec2/bootstrap.sh" >&2
  exit 1
fi

APP_USER="${APP_USER:-xko}"
APP_DIR="${APP_DIR:-/opt/xko}"
REPO_URL="${REPO_URL:-https://github.com/nikoCW/xko.git}"
BRANCH="${BRANCH:-main}"
ENV_DIR="/etc/xko"
ENV_FILE="${ENV_DIR}/nautilus-bridge.env"
DATA_DIR="/var/lib/xko-nautilus"
SYSTEMD_UNIT="/etc/systemd/system/xko-nautilus-bridge.service"
RUNTIME_REQUIREMENTS="deploy/aws-ec2/requirements-runtime.txt"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git gnupg openssl \
  python3 python3-venv python3-pip \
  debian-keyring debian-archive-keyring apt-transport-https
apt-get clean
rm -rf /var/lib/apt/lists/*

# Install Caddy from its official Debian/Ubuntu repository when not already present.
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y --no-install-recommends caddy
  apt-get clean
  rm -rf /var/lib/apt/lists/*
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/home/${APP_USER}" --shell /usr/sbin/nologin "${APP_USER}"
fi

install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 "${DATA_DIR}"
install -d -o root -g "${APP_USER}" -m 0750 "${ENV_DIR}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  git clone --branch "${BRANCH}" --single-branch "${REPO_URL}" "${APP_DIR}"
else
  git -C "${APP_DIR}" fetch origin "${BRANCH}"
  git -C "${APP_DIR}" checkout "${BRANCH}"
  git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  sudo -u "${APP_USER}" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --no-cache-dir --upgrade pip wheel
sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --no-cache-dir \
  -r "${APP_DIR}/${RUNTIME_REQUIREMENTS}"
rm -rf "/home/${APP_USER}/.cache/pip" /root/.cache/pip

if [[ ! -f "${ENV_FILE}" ]]; then
  install -o root -g "${APP_USER}" -m 0640 \
    "${APP_DIR}/deploy/aws-ec2/nautilus-bridge.env.example" "${ENV_FILE}"
  TOKEN="$(openssl rand -hex 32)"
  sed -i "s/^BRIDGE_API_TOKEN=.*/BRIDGE_API_TOKEN=${TOKEN}/" "${ENV_FILE}"
fi

install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/aws-ec2/xko-nautilus-bridge.service" "${SYSTEMD_UNIT}"

systemctl daemon-reload
systemctl enable xko-nautilus-bridge.service

cat <<'EOF'

Bootstrap complete.

Next:
  1. sudo nano /etc/xko/nautilus-bridge.env
     Fill ONLY your OKX DEMO API key/secret/passphrase. Keep safety switches false.
  2. Point a DNS A record (for example bridge.example.com) to this EC2 Elastic IP.
  3. Copy deploy/aws-ec2/Caddyfile.example to /etc/caddy/Caddyfile and replace bridge.example.com.
  4. sudo systemctl restart caddy
  5. sudo systemctl start xko-nautilus-bridge
  6. sudo journalctl -u xko-nautilus-bridge -f

Do not paste API secrets into chat or commit them to GitHub.
EOF
