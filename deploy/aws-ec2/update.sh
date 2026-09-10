#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/aws-ec2/update.sh" >&2
  exit 1
fi

APP_USER="${APP_USER:-xko}"
APP_DIR="${APP_DIR:-/opt/xko}"
BRANCH="${BRANCH:-main}"
RUNTIME_REQUIREMENTS="deploy/aws-ec2/requirements-runtime.txt"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "${APP_DIR} is not a git checkout. Run bootstrap.sh first." >&2
  exit 1
fi

# Keep the checkout owned by the service user and run Git as that same user.
# This preserves Git's ownership safety checks instead of disabling them.
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
sudo -u "${APP_USER}" git -C "${APP_DIR}" fetch origin "${BRANCH}"
sudo -u "${APP_USER}" git -C "${APP_DIR}" checkout "${BRANCH}"
sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only origin "${BRANCH}"

sudo -u "${APP_USER}" "${APP_DIR}/.venv/bin/pip" install --no-cache-dir \
  -r "${APP_DIR}/${RUNTIME_REQUIREMENTS}"
rm -rf "/home/${APP_USER}/.cache/pip" /root/.cache/pip

install -o root -g root -m 0644 \
  "${APP_DIR}/deploy/aws-ec2/xko-nautilus-bridge.service" \
  /etc/systemd/system/xko-nautilus-bridge.service

systemctl daemon-reload
systemctl restart xko-nautilus-bridge.service
systemctl --no-pager --full status xko-nautilus-bridge.service
