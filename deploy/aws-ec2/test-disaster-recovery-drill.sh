#!/usr/bin/env bash
set -euo pipefail

REPO="/opt/xko"
ENV_FILE="/etc/xko/nautilus-bridge.env"
SERVICE="xko-nautilus-bridge"
PYTHON="/opt/xko/.venv/bin/python"
DR_PY="$REPO/deploy/aws-ec2/disaster_recovery_drill.py"
HEALTH_URL="http://127.0.0.1:8765/health"
FREEZE_COMMIT="97956abf51f567c1d87b20cb297a1d72bbbf07d7"

fail() { echo "DR_FAIL $*" >&2; exit 1; }
ok() { echo "DR_OK $*"; }

if [[ "${EUID}" -ne 0 ]]; then
  fail "run as root: sudo $0"
fi
[[ -r "$ENV_FILE" ]] || fail "missing $ENV_FILE"
[[ -x "$PYTHON" ]] || fail "missing $PYTHON"
[[ -f "$DR_PY" ]] || fail "missing $DR_PY"

# Hard safety lock: this drill is never allowed to run with any entry/submit path enabled.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
for flag in ALLOW_ORDER_SUBMIT ALLOW_UNPROTECTED_ENTRY ALLOW_MARKET_ENTRY; do
  if [[ "${!flag:-}" != "false" ]]; then
    fail "$flag must be exactly false"
  fi
done
ok "safety_flags submit=false unprotected=false market=false"

DB_PATH="${INTENT_DB_PATH:-/var/lib/xko-nautilus/intents.db}"
[[ -f "$DB_PATH" ]] || fail "intent database missing at configured path"
[[ ! -L "$DB_PATH" ]] || fail "intent database must not be a symlink"

# Refuse the restart portion if runtime files have drifted since the signed pre-live freeze.
# Only this DR tooling/documentation may differ from the freeze commit.
sudo -u xko git -C "$REPO" merge-base --is-ancestor "$FREEZE_COMMIT" HEAD \
  || fail "HEAD does not descend from pre-live freeze $FREEZE_COMMIT"
[[ -z "$(sudo -u xko git -C "$REPO" status --porcelain)" ]] \
  || fail "repository worktree is dirty"
mapfile -t changed_since_freeze < <(sudo -u xko git -C "$REPO" diff --name-only "$FREEZE_COMMIT"..HEAD)
for path in "${changed_since_freeze[@]}"; do
  case "$path" in
    deploy/aws-ec2/disaster_recovery_drill.py|deploy/aws-ec2/test-disaster-recovery-drill.sh|DISASTER_RECOVERY.md)
      ;;
    *) fail "runtime drift since freeze: $path" ;;
  esac
done
ok "runtime_unchanged_since_prelive_freeze"

# Root-only temporary recovery workspace. It is removed on exit and never replaces live files.
DRILL_DIR="$(mktemp -d /var/tmp/xko-dr-drill.XXXXXX)"
chmod 700 "$DRILL_DIR"
cleanup() {
  rm -rf "$DRILL_DIR"
}
trap cleanup EXIT
mkdir -p "$DRILL_DIR/config" "$DRILL_DIR/db"
chmod 700 "$DRILL_DIR/config" "$DRILL_DIR/db"

# Back up configuration into the isolated recovery workspace without printing secrets.
install -m 600 "$ENV_FILE" "$DRILL_DIR/config/nautilus-bridge.env"
install -m 600 /etc/caddy/Caddyfile "$DRILL_DIR/config/Caddyfile"
systemctl cat "$SERVICE" > "$DRILL_DIR/config/xko-nautilus-bridge.service"
chmod 600 "$DRILL_DIR/config/xko-nautilus-bridge.service"
cmp -s "$ENV_FILE" "$DRILL_DIR/config/nautilus-bridge.env" \
  || fail "env recovery copy mismatch"
cmp -s /etc/caddy/Caddyfile "$DRILL_DIR/config/Caddyfile" \
  || fail "Caddy recovery copy mismatch"
systemctl cat "$SERVICE" | cmp -s - "$DRILL_DIR/config/xko-nautilus-bridge.service" \
  || fail "systemd recovery copy mismatch"
ok "config_recovery_copies_verified"

# Online SQLite backup uses a read-only source connection, then restores only to a temp path.
PYTHONPATH="$REPO/nautilus_bridge" "$PYTHON" "$DR_PY" \
  --source "$DB_PATH" \
  --backup "$DRILL_DIR/db/intents.backup.db" \
  --restore "$DRILL_DIR/db/intents.restored.db"

# Confirm restored files remain private.
[[ "$(stat -c '%a' "$DRILL_DIR/db/intents.backup.db")" == "600" ]] \
  || fail "backup database permissions are not 600"
[[ "$(stat -c '%a' "$DRILL_DIR/db/intents.restored.db")" == "600" ]] \
  || fail "restored database permissions are not 600"
ok "recovery_artifact_permissions=600"

# Verify installed service wiring is reconstructable from the repo/config bundle.
systemctl cat "$SERVICE" | grep -Fq 'EnvironmentFile=/etc/xko/nautilus-bridge.env' \
  || fail "unexpected systemd EnvironmentFile"
systemctl cat "$SERVICE" | grep -Fq 'WorkingDirectory=/opt/xko/nautilus_bridge' \
  || fail "unexpected systemd WorkingDirectory"
systemctl cat "$SERVICE" | grep -Fq 'ExecStart=/opt/xko/.venv/bin/python -m nautilus_service.main' \
  || fail "unexpected systemd ExecStart"
[[ -f "$REPO/deploy/aws-ec2/xko-nautilus-bridge.service" ]] \
  || fail "repo systemd unit missing"
[[ -f "$REPO/deploy/aws-ec2/nautilus-bridge.env.example" ]] \
  || fail "repo env template missing"
ok "service_rebuild_inputs_present"

# Restart the unchanged bridge to prove startup reconciliation/protection recovery.
DRILL_START="$(date --iso-8601=seconds)"
systemctl restart "$SERVICE"

health_file="$DRILL_DIR/health.json"
ready=false
for _ in $(seq 1 60); do
  if systemctl is-active --quiet "$SERVICE" && curl -fsS --max-time 3 "$HEALTH_URL" > "$health_file" 2>/dev/null; then
    if "$PYTHON" - "$health_file" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1]) as f:
    h = json.load(f)
ok = (
    h.get("status") == "ok"
    and h.get("strategy_ready") is True
    and h.get("portfolio_ready") is True
    and h.get("reconciliation_ready") is True
    and h.get("execution_connected") is True
    and h.get("data_connected") is True
    and h.get("protection_ready") is True
    and h.get("preview_ready") is True
    and h.get("trading_ready") is True
    and h.get("reconciliation_invalidated") is False
    and h.get("protection_invalidated") is False
    and h.get("order_submit_enabled") is False
    and h.get("unprotected_entry_enabled") is False
)
raise SystemExit(0 if ok else 1)
PY
    then
      ready=true
      break
    fi
  fi
  sleep 2
done
[[ "$ready" == "true" ]] || {
  journalctl -u "$SERVICE" --since "$DRILL_START" --no-pager >&2 || true
  fail "bridge did not return to reconciled/protected submit-disabled health"
}

"$PYTHON" - "$health_file" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    h = json.load(f)
checks = {
    "status": h.get("status") == "ok",
    "ready": h.get("ready") is True,
    "strategy_ready": h.get("strategy_ready") is True,
    "portfolio_ready": h.get("portfolio_ready") is True,
    "reconciliation_ready": h.get("reconciliation_ready") is True,
    "execution_connected": h.get("execution_connected") is True,
    "data_connected": h.get("data_connected") is True,
    "protection_ready": h.get("protection_ready") is True,
    "preview_ready": h.get("preview_ready") is True,
    "trading_ready": h.get("trading_ready") is True,
    "reconciliation_invalidated": h.get("reconciliation_invalidated") is False,
    "protection_invalidated": h.get("protection_invalidated") is False,
    "order_submit_enabled": h.get("order_submit_enabled") is False,
    "unprotected_entry_enabled": h.get("unprotected_entry_enabled") is False,
    "protected_submit_only": h.get("protected_submit_only") is True,
    "protective_bracket_mode": h.get("protective_bracket_mode") == "OKX_ATTACHED_OCO",
}
failed = [key for key, value in checks.items() if not value]
if failed:
    raise SystemExit("DR_FAIL post_restart_health:" + ",".join(failed))
print("DR_OK post_restart_reconciliation_and_protection")
print("DR_OK post_restart_submit_enabled=false")
PY

# Port remains private after restart.
listeners="$(ss -ltnH '( sport = :8765 )' || true)"
[[ -n "$listeners" ]] || fail "no listener on 8765 after restart"
if echo "$listeners" | awk '{print $4}' | grep -Evq '^(127\.0\.0\.1|\[::1\]):8765$'; then
  echo "$listeners" >&2
  fail "8765 has a non-loopback listener after restart"
fi
ok "bridge_port_loopback_only_after_restart"

# A restart performs normal authenticated read/reconciliation traffic to OKX, but this drill
# must never enter the order-submit path.
if journalctl -u "$SERVICE" --since "$DRILL_START" --no-pager \
  | grep -Eq 'PROTECTED_BRACKET_SUBMITTED_TO_NAUTILUS|OrderSubmitted'; then
  fail "submit-path activity found during DR drill"
fi
ok "no_order_submit_path_activity_during_drill"
ok "live_database_replaced=false"
ok "live_configuration_replaced=false"
echo "DISASTER_RECOVERY_DRILL_OK"
