#!/usr/bin/env bash
set -euo pipefail

REPO=/opt/xko
SERVICE=xko-intent-backup.service
TIMER=xko-intent-backup.timer
BACKUP_DIR=/var/backups/xko-nautilus
ENV_FILE=/etc/xko/nautilus-bridge.env

fail() { echo "BACKUP_INSTALL_FAIL $*" >&2; exit 1; }
ok() { echo "BACKUP_INSTALL_OK $*"; }

[[ ${EUID} -eq 0 ]] || fail "run as root: sudo $0"
[[ -r "$ENV_FILE" ]] || fail "missing $ENV_FILE"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
for flag in ALLOW_ORDER_SUBMIT ALLOW_UNPROTECTED_ENTRY ALLOW_MARKET_ENTRY; do
  [[ "${!flag:-}" == "false" ]] || fail "$flag must be exactly false"
done
ok "safety_flags submit=false unprotected=false market=false"

install -d -o xko -g xko -m 0750 "$BACKUP_DIR"
install -o root -g root -m 0644 "$REPO/deploy/aws-ec2/$SERVICE" "/etc/systemd/system/$SERVICE"
install -o root -g root -m 0644 "$REPO/deploy/aws-ec2/$TIMER" "/etc/systemd/system/$TIMER"

systemctl daemon-reload
systemctl enable --now "$TIMER"

# Run once immediately so installation is validated now rather than at the first timer firing.
systemctl start "$SERVICE"
systemctl is-failed --quiet "$SERVICE" && fail "$SERVICE failed"

latest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'intents-*.db' -printf '%T@ %f\n' | sort -nr | head -1 | cut -d' ' -f2-)"
[[ -n "$latest" ]] || fail "no backup file created"
[[ "$(stat -c '%a' "$BACKUP_DIR/$latest")" == "600" ]] || fail "backup file permissions are not 600"
[[ -f "$BACKUP_DIR/$latest.sha256" ]] || fail "checksum file missing"

systemctl is-enabled --quiet "$TIMER" || fail "$TIMER not enabled"
systemctl is-active --quiet "$TIMER" || fail "$TIMER not active"

ok "initial_backup=$latest"
ok "timer_enabled=true"
systemctl list-timers "$TIMER" --no-pager
printf '%s\n' "INTENT_BACKUP_TIMER_OK"
