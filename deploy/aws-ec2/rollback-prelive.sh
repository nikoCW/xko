#!/usr/bin/env bash
set -euo pipefail

REPO="/opt/xko"
SERVICE="xko-nautilus-bridge"
DEFAULT_TARGET="4e76f932f79bdfad2258c863e7c3fdac64656c75"
TARGET="${1:-$DEFAULT_TARGET}"
PYTHON="/opt/xko/.venv/bin/python"
HEALTH_URL="https://bridge.the3111.xyz/health"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo $0 [commit]" >&2
  exit 2
fi

if [[ -n "$(sudo -u xko git -C "$REPO" status --porcelain)" ]]; then
  echo "REFUSING ROLLBACK: repository worktree is dirty" >&2
  exit 3
fi

# First force all submission-related policy flags off and prove the restarted service sees them.
bash "$REPO/deploy/aws-ec2/emergency-disable-submit.sh"

sudo -u xko git -C "$REPO" fetch origin main --tags
sudo -u xko git -C "$REPO" cat-file -e "$TARGET^{commit}" 2>/dev/null || {
  echo "REFUSING ROLLBACK: target is not a commit: $TARGET" >&2
  exit 3
}
if ! sudo -u xko git -C "$REPO" merge-base --is-ancestor "$TARGET" origin/main; then
  echo "REFUSING ROLLBACK: target is not an ancestor of origin/main" >&2
  exit 3
fi

previous="$(sudo -u xko git -C "$REPO" rev-parse HEAD)"
echo "ROLLBACK_FROM $previous"
echo "ROLLBACK_TO $TARGET"

sudo -u xko git -C "$REPO" reset --hard "$TARGET"

sudo -u xko env PYTHONPATH="$REPO/nautilus_bridge" \
  "$PYTHON" -m py_compile \
  "$REPO/nautilus_bridge/nautilus_service/bridge_strategy.py" \
  "$REPO/nautilus_bridge/nautilus_service/protection_coverage_strategy.py" \
  "$REPO/nautilus_bridge/nautilus_service/main.py"

systemctl restart "$SERVICE"

health_tmp="$(mktemp)"
trap 'rm -f "$health_tmp"' EXIT
for _ in {1..20}; do
  if systemctl is-active --quiet "$SERVICE" && curl -fsS "$HEALTH_URL" > "$health_tmp"; then
    if "$PYTHON" - "$health_tmp" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    h = json.load(f)
assert h.get("order_submit_enabled") is False, h
assert h.get("unprotected_entry_enabled") is False, h
assert h.get("status") == "ok", h
print("ROLLBACK_HEALTH_OK submit=false")
PY
    then
      echo "ROLLBACK_HEAD $(sudo -u xko git -C "$REPO" rev-parse HEAD)"
      echo "XKO_ROLLBACK_OK"
      exit 0
    fi
  fi
  sleep 1
done

systemctl --no-pager --full status "$SERVICE" || true
echo "ROLLBACK_FAIL: service/health verification failed; submission flags remain forced false in env" >&2
exit 1
