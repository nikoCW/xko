#!/usr/bin/env bash
set -euo pipefail

REPO="/opt/xko"
ENV_FILE="/etc/xko/nautilus-bridge.env"
SERVICE="xko-nautilus-bridge"
RUNTIME_BASELINE="4e76f932f79bdfad2258c863e7c3fdac64656c75"
HEALTH_URL="https://bridge.the3111.xyz/health"
PYTHON="/opt/xko/.venv/bin/python"

fail() { echo "AUDIT_FAIL $*" >&2; exit 1; }
ok() { echo "AUDIT_OK $*"; }

if [[ "${EUID}" -ne 0 ]]; then
  fail "run as root: sudo $0"
fi
[[ -r "$ENV_FILE" ]] || fail "missing $ENV_FILE"
[[ -x "$PYTHON" ]] || fail "missing $PYTHON"

# Repository must include the fully dry-run-tested runtime baseline and have no local edits.
if ! sudo -u xko git -C "$REPO" merge-base --is-ancestor "$RUNTIME_BASELINE" HEAD; then
  fail "HEAD does not contain tested runtime baseline $RUNTIME_BASELINE"
fi
head_sha="$(sudo -u xko git -C "$REPO" rev-parse HEAD)"
[[ -z "$(sudo -u xko git -C "$REPO" status --porcelain)" ]] || fail "repository worktree is dirty"
ok "repo_clean head=$head_sha runtime_baseline=$RUNTIME_BASELINE"

# Safety env values are checked without printing any credentials.
declare -A vals=()
while IFS='=' read -r raw_key raw_value; do
  key="${raw_key//[[:space:]]/}"
  case "$key" in
    ALLOW_ORDER_SUBMIT|ALLOW_UNPROTECTED_ENTRY|ALLOW_MARKET_ENTRY|OKX_DEMO)
      value="${raw_value%%#*}"
      value="${value//[[:space:]]/}"
      vals["$key"]="$value"
      ;;
  esac
done < "$ENV_FILE"
[[ "${vals[ALLOW_ORDER_SUBMIT]:-}" == "false" ]] || fail "ALLOW_ORDER_SUBMIT must be false"
[[ "${vals[ALLOW_UNPROTECTED_ENTRY]:-}" == "false" ]] || fail "ALLOW_UNPROTECTED_ENTRY must be false"
[[ "${vals[ALLOW_MARKET_ENTRY]:-}" == "false" ]] || fail "ALLOW_MARKET_ENTRY must be false"
[[ "${vals[OKX_DEMO]:-}" == "false" ]] || fail "OKX_DEMO expected false for current read-only LIVE setup"
ok "safety_env submit=false unprotected=false market=false okx_demo=false"

perm="$(stat -c '%a' "$ENV_FILE")"
case "$perm" in
  600|640) ok "env_permissions=$perm" ;;
  *) fail "unsafe env permissions=$perm expected 600 or 640" ;;
esac

systemctl is-active --quiet "$SERVICE" || fail "$SERVICE not active"
systemctl is-active --quiet caddy || fail "caddy not active"
systemctl cat "$SERVICE" | grep -Fq 'EnvironmentFile=/etc/xko/nautilus-bridge.env' || fail "unexpected systemd EnvironmentFile"
ok "services_active"

# 8765 must be loopback-only. Any wildcard or non-loopback listener is a hard failure.
listeners="$(ss -ltnH '( sport = :8765 )' || true)"
[[ -n "$listeners" ]] || fail "no listener on 8765"
if echo "$listeners" | awk '{print $4}' | grep -Evq '^(127\.0\.0\.1|\[::1\]):8765$'; then
  echo "$listeners" >&2
  fail "8765 has a non-loopback listener"
fi
ok "bridge_port_loopback_only"

grep -Eq '^bridge\.the3111\.xyz[[:space:]]*\{' /etc/caddy/Caddyfile || fail "Caddy hostname missing"
grep -Eq 'reverse_proxy[[:space:]]+127\.0\.0\.1:8765' /etc/caddy/Caddyfile || fail "Caddy reverse proxy target unexpected"
caddy validate --config /etc/caddy/Caddyfile >/dev/null || fail "Caddy config invalid"
ok "caddy_config_valid"

version="$($PYTHON - <<'PY'
import nautilus_trader
print(nautilus_trader.__version__)
PY
)"
[[ "$version" == "1.231.0" ]] || fail "unexpected NautilusTrader version=$version"
ok "nautilus_version=$version"

health_tmp="$(mktemp)"
trap 'rm -f "$health_tmp"' EXIT
curl -fsS "$HEALTH_URL" > "$health_tmp" || fail "health request failed"
"$PYTHON" - "$health_tmp" <<'PY'
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
    "protection_scope": h.get("protection_scope") == "xko_owned_positions_only",
    "target_position_policy": h.get("target_instrument_position_policy") == "target_instrument_must_be_flat_before_submit",
}
failed = [k for k, v in checks.items() if not v]
if failed:
    raise SystemExit("AUDIT_FAIL health:" + ",".join(failed))
print("AUDIT_OK health_all_gates_expected submit=false")
PY

# There must be no evidence of a real submit path in the current boot journal.
if journalctl -u "$SERVICE" -b --no-pager | grep -Eq 'PROTECTED_BRACKET_SUBMITTED_TO_NAUTILUS|OrderSubmitted'; then
  fail "submit-path activity found in current boot journal"
fi
ok "no_submit_path_activity_current_boot"

echo "PRELIVE_AUDIT_OK"
