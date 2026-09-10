#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/aws-ec2/update.sh" >&2
  exit 1
fi

APP_USER="${APP_USER:-xko}"
APP_DIR="${APP_DIR:-/opt/xko}"
BRANCH="${BRANCH:-main}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "${APP_DIR} is not a git checkout. Run bootstrap.sh first." >&2
  exit 1
fi

git -C "${APP_DIR}" fetch origin "${BRANCH}"
git -C "${APP_DIR}" checkout "${BRANCH}"
git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/nautilus_bridge/requirements.txt"
install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/aws-ec2/xko-nautilus-bridge.service" \
  /etc/systemd/system/xko-nautilus-bridge.service

systemctl daemon-reload
systemctl restart xko-nautilus-bridge.service
systemctl --no-pager --full status xko-nautilus-bridge.service
