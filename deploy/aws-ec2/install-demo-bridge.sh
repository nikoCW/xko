#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "DEMO_INSTALL_FAIL run_as_root_required" >&2
  exit 1
fi

REPO=/opt/xko
LIVE_ENV=/etc/xko/nautilus-bridge.env
DEMO_ENV=/etc/xko/nautilus-demo.env
DEMO_ENV_TEMPLATE="$REPO/deploy/aws-ec2/nautilus-demo.env.example"
UNIT_SRC="$REPO/deploy/aws-ec2/xko-nautilus-demo.service"
UNIT_DST=/etc/systemd/system/xko-nautilus-demo.service
DEMO_DB_DIR=/var/lib/xko-nautilus-demo
DEMO_PORT=8775

require_exact() {
  local file=$1 line=$2 label=$3
  if ! grep -Fqx "$line" "$file"; then
    echo "DEMO_INSTALL_FAIL ${label}" >&2
    exit 1
  fi
}

require_nonempty_key() {
  local file=$1 key=$2
  if ! grep -Eq "^${key}=.+$" "$file"; then
    echo "DEMO_INSTALL_FAIL ${key}_missing_or_empty" >&2
    exit 1
  fi
}

if [[ ! -f "$LIVE_ENV" ]]; then
  echo "DEMO_INSTALL_FAIL live_env_missing" >&2
  exit 1
fi

# LIVE service is not modified by this installer. Require its three execution switches
# to remain hard-off before touching the separate DEMO service.
require_exact "$LIVE_ENV" 'ALLOW_ORDER_SUBMIT=false' live_submit_not_false
require_exact "$LIVE_ENV" 'ALLOW_UNPROTECTED_ENTRY=false' live_unprotected_not_false
require_exact "$LIVE_ENV" 'ALLOW_MARKET_ENTRY=false' live_market_not_false

echo "DEMO_INSTALL_OK live_safety_flags submit=false unprotected=false market=false"

if [[ ! -f "$DEMO_ENV" ]]; then
  install -o root -g xko -m 0640 "$DEMO_ENV_TEMPLATE" "$DEMO_ENV"
  echo "DEMO_ENV_CREATED $DEMO_ENV"
  echo "Edit it locally with: sudo nano $DEMO_ENV" >&2
  echo "Fill only DEMO OKX credentials and replace BRIDGE_API_TOKEN; do not paste secrets into chat." >&2
  exit 2
fi

# The DEMO service is deliberately isolated from LIVE shadow by environment, port and DB.
require_exact "$DEMO_ENV" 'OKX_DEMO=true' demo_environment_must_be_true
require_exact "$DEMO_ENV" 'NAUTILUS_BRIDGE_HOST=127.0.0.1' demo_host_must_be_loopback
require_exact "$DEMO_ENV" "NAUTILUS_BRIDGE_PORT=${DEMO_PORT}" demo_port_mismatch
require_exact "$DEMO_ENV" 'INTENT_DB_PATH=/var/lib/xko-nautilus-demo/intents.db' demo_db_path_mismatch
require_exact "$DEMO_ENV" 'ALLOW_ORDER_SUBMIT=false' demo_submit_must_start_false
require_exact "$DEMO_ENV" 'ALLOW_UNPROTECTED_ENTRY=false' demo_unprotected_must_be_false
require_exact "$DEMO_ENV" 'ALLOW_MARKET_ENTRY=false' demo_market_must_be_false
require_nonempty_key "$DEMO_ENV" OKX_API_KEY
require_nonempty_key "$DEMO_ENV" OKX_API_SECRET
require_nonempty_key "$DEMO_ENV" OKX_API_PASSPHRASE
require_nonempty_key "$DEMO_ENV" BRIDGE_API_TOKEN

if grep -Fqx 'BRIDGE_API_TOKEN=CHANGE_ME_TO_A_SEPARATE_LONG_RANDOM_TOKEN' "$DEMO_ENV"; then
  echo "DEMO_INSTALL_FAIL replace_demo_bridge_token_placeholder" >&2
  exit 1
fi

chmod 0640 "$DEMO_ENV"
chown root:xko "$DEMO_ENV"
install -d -o xko -g xko -m 0700 "$DEMO_DB_DIR"
install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"

live_pid_before=$(systemctl show -p MainPID --value xko-nautilus-bridge.service 2>/dev/null || true)

systemctl daemon-reload
systemctl enable --now xko-nautilus-demo.service

health=''
for _ in $(seq 1 60); do
  if health=$(curl -fsS --max-time 2 "http://127.0.0.1:${DEMO_PORT}/health" 2>/dev/null); then
    if HEALTH="$health" python3 - <<'PY'
import json, os, sys
h=json.loads(os.environ['HEALTH'])
required=(
    h.get('status') == 'ok',
    h.get('ready') is True,
    h.get('reconciliation_ready') is True,
    h.get('execution_connected') is True,
    h.get('data_connected') is True,
    h.get('protection_ready') is True,
    h.get('preview_ready') is True,
    h.get('okx_environment') == 'DEMO',
    h.get('order_submit_enabled') is False,
    h.get('unprotected_entry_enabled') is False,
)
sys.exit(0 if all(required) else 1)
PY
    then
      break
    fi
  fi
  sleep 1
done

if [[ -z "$health" ]]; then
  echo "DEMO_INSTALL_FAIL demo_health_unreachable" >&2
  journalctl -u xko-nautilus-demo.service --no-pager -n 60 -l >&2 || true
  exit 1
fi

if ! HEALTH="$health" python3 - <<'PY'
import json, os, sys
h=json.loads(os.environ['HEALTH'])
required=(
    h.get('status') == 'ok',
    h.get('ready') is True,
    h.get('reconciliation_ready') is True,
    h.get('execution_connected') is True,
    h.get('data_connected') is True,
    h.get('protection_ready') is True,
    h.get('preview_ready') is True,
    h.get('okx_environment') == 'DEMO',
    h.get('order_submit_enabled') is False,
    h.get('unprotected_entry_enabled') is False,
)
sys.exit(0 if all(required) else 1)
PY
then
  echo "DEMO_INSTALL_FAIL demo_health_gates_not_green" >&2
  printf '%s\n' "$health" >&2
  journalctl -u xko-nautilus-demo.service --no-pager -n 60 -l >&2 || true
  exit 1
fi

if ! ss -ltn | awk '$4 ~ /127\.0\.0\.1:8775$/ {found=1} END {exit found?0:1}'; then
  echo "DEMO_INSTALL_FAIL demo_port_not_loopback" >&2
  exit 1
fi
if ss -ltn | awk '$4 ~ /(0\.0\.0\.0|\[::\]):8775$/ {found=1} END {exit found?0:1}'; then
  echo "DEMO_INSTALL_FAIL demo_port_publicly_bound" >&2
  exit 1
fi

live_pid_after=$(systemctl show -p MainPID --value xko-nautilus-bridge.service 2>/dev/null || true)
if [[ -n "$live_pid_before" && "$live_pid_before" != "0" && "$live_pid_before" != "$live_pid_after" ]]; then
  echo "DEMO_INSTALL_FAIL live_bridge_pid_changed" >&2
  exit 1
fi

echo "DEMO_INSTALL_OK environment=DEMO"
echo "DEMO_INSTALL_OK health_all_gates_green"
echo "DEMO_INSTALL_OK submit_enabled=false"
echo "DEMO_INSTALL_OK port_8775_loopback_only"
echo "DEMO_INSTALL_OK separate_db=/var/lib/xko-nautilus-demo/intents.db"
echo "DEMO_INSTALL_OK live_bridge_pid_unchanged=${live_pid_after:-unknown}"
echo "DEMO_BRIDGE_READY_PHASE1"
