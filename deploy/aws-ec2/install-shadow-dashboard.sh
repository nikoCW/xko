#!/usr/bin/env bash
set -euo pipefail

REPO="/opt/xko"
ENV_FILE="/etc/xko/nautilus-bridge.env"
SERVICE="xko-shadow-dashboard"
UNIT_SRC="$REPO/deploy/aws-ec2/xko-shadow-dashboard.service"
UNIT_DST="/etc/systemd/system/xko-shadow-dashboard.service"
PYTHON="/opt/xko/.venv/bin/python"
DASHBOARD_PY="$REPO/deploy/aws-ec2/shadow_dashboard.py"
DASHBOARD_HTML="$REPO/deploy/aws-ec2/shadow_dashboard.html"
FREEZE_COMMIT="97956abf51f567c1d87b20cb297a1d72bbbf07d7"
HEALTH_URL="http://127.0.0.1:8766/health"
STATE_URL="http://127.0.0.1:8766/api/state"

fail() { echo "SHADOW_DASHBOARD_INSTALL_FAIL $*" >&2; exit 1; }
ok() { echo "SHADOW_DASHBOARD_INSTALL_OK $*"; }

if [[ "${EUID}" -ne 0 ]]; then
  fail "run as root: sudo $0"
fi
[[ -r "$ENV_FILE" ]] || fail "missing $ENV_FILE"
[[ -x "$PYTHON" ]] || fail "missing $PYTHON"
[[ -f "$DASHBOARD_PY" ]] || fail "missing $DASHBOARD_PY"
[[ -f "$DASHBOARD_HTML" ]] || fail "missing $DASHBOARD_HTML"
[[ -f "$UNIT_SRC" ]] || fail "missing $UNIT_SRC"

# This dashboard is allowed only while every execution/entry safety switch is hard-off.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
for flag in ALLOW_ORDER_SUBMIT ALLOW_UNPROTECTED_ENTRY ALLOW_MARKET_ENTRY; do
  [[ "${!flag:-}" == "false" ]] || fail "$flag must be exactly false"
done
ok "safety_flags submit=false unprotected=false market=false"

# Preserve the signed bridge runtime. The dashboard is intentionally a separate process.
sudo -u xko git -C "$REPO" merge-base --is-ancestor "$FREEZE_COMMIT" HEAD \
  || fail "HEAD does not descend from pre-live freeze $FREEZE_COMMIT"
[[ -z "$(sudo -u xko git -C "$REPO" status --porcelain)" ]] \
  || fail "repository worktree is dirty"
if sudo -u xko git -C "$REPO" diff --name-only "$FREEZE_COMMIT"..HEAD -- \
    nautilus_bridge server.py requirements.txt Procfile | grep -q .; then
  sudo -u xko git -C "$REPO" diff --name-only "$FREEZE_COMMIT"..HEAD -- \
    nautilus_bridge server.py requirements.txt Procfile >&2
  fail "core bridge/runtime drift detected since pre-live freeze"
fi
ok "core_bridge_runtime_unchanged_since_prelive_freeze"

"$PYTHON" -m py_compile "$DASHBOARD_PY"
ok "python_compile_ok"

# Static safety assertion: the dashboard implementation must have no bridge order-submit route.
if grep -Eq 'intents/.+/(submit|approve)|/submit|/approve' "$DASHBOARD_PY"; then
  fail "dashboard source contains an approve/submit bridge route"
fi
ok "no_approve_or_submit_bridge_route"

install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT_DST"
install -d -o xko -g xko -m 0750 /var/lib/xko-nautilus
systemctl daemon-reload
systemctl enable --now "$SERVICE"

ready=false
for _ in $(seq 1 30); do
  if systemctl is-active --quiet "$SERVICE" && curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
[[ "$ready" == "true" ]] || {
  systemctl --no-pager --full status "$SERVICE" >&2 || true
  journalctl -u "$SERVICE" -n 50 --no-pager >&2 || true
  fail "dashboard service did not become healthy"
}
ok "service_active"

listeners="$(ss -ltnH '( sport = :8766 )' || true)"
[[ -n "$listeners" ]] || fail "no listener on 8766"
if echo "$listeners" | awk '{print $4}' | grep -Evq '^(127\.0\.0\.1|\[::1\]):8766$'; then
  echo "$listeners" >&2
  fail "8766 has a non-loopback listener"
fi
ok "port_8766_loopback_only"

state_tmp="$(mktemp)"
trap 'rm -f "$state_tmp"' EXIT
curl -fsS --max-time 10 "$STATE_URL" > "$state_tmp" || fail "dashboard state request failed"
"$PYTHON" - "$state_tmp" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    s = json.load(f)
h = s.get("health", {})
caps = s.get("capabilities", {})
checks = {
    "mode_shadow": s.get("mode") in {"LIVE_SHADOW", "DEMO_SHADOW"},
    "submit_disabled": h.get("order_submit_enabled") is False,
    "unprotected_disabled": h.get("unprotected_entry_enabled") is False,
    "preview_capability": caps.get("preview") is True,
    "review_capability": caps.get("record_review") is True,
    "approve_capability_absent": caps.get("approve") is False,
    "submit_capability_absent": caps.get("submit") is False,
    "go_live_not_authorized": s.get("go_live_ready") is False,
}
failed = [k for k, v in checks.items() if not v]
if failed:
    raise SystemExit("SHADOW_DASHBOARD_INSTALL_FAIL state:" + ",".join(failed))
print("SHADOW_DASHBOARD_INSTALL_OK state_shadow_submit_capability=false")
PY

review_perm="$(stat -c '%a' /var/lib/xko-nautilus/shadow-reviews.db)"
[[ "$review_perm" == "600" ]] || fail "shadow review DB permissions=$review_perm expected 600"
ok "review_db_permissions=600"

echo
systemctl --no-pager --full status "$SERVICE" | sed -n '1,10p'
echo
cat <<'EOF'
SHADOW_DASHBOARD_INSTALL_OK
Access remains local-only. From your own computer, open an SSH tunnel:
  ssh -L 8766:127.0.0.1:8766 ubuntu@<EC2_PUBLIC_IP>
Then browse:
  http://127.0.0.1:8766/
EOF
