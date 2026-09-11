#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/xko/nautilus-bridge.env"
SERVICE="xko-nautilus-bridge"
BACKUP_DIR="/var/lib/xko-nautilus/safety-backups"
HEALTH_URL="https://bridge.the3111.xyz/health"
PYTHON="/opt/xko/.venv/bin/python"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 2
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "missing $ENV_FILE" >&2
  exit 2
fi

install -d -m 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$BACKUP_DIR/nautilus-bridge.env.$stamp"
install -m 600 "$ENV_FILE" "$backup"

"$PYTHON" - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
required = {
    "ALLOW_ORDER_SUBMIT": "false",
    "ALLOW_UNPROTECTED_ENTRY": "false",
    "ALLOW_MARKET_ENTRY": "false",
}
seen = set()
out = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in required:
            out.append(f"{key}={required[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in required.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
PY
chmod 600 "$ENV_FILE"

systemctl restart "$SERVICE"

for _ in {1..20}; do
  if systemctl is-active --quiet "$SERVICE" && curl -fsS "$HEALTH_URL" > /tmp/xko-kill-health.json; then
    if "$PYTHON" - /tmp/xko-kill-health.json <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    h = json.load(f)
assert h["order_submit_enabled"] is False, h
assert h["unprotected_entry_enabled"] is False, h
print("HEALTH_ORDER_SUBMIT_ENABLED", h["order_submit_enabled"])
print("HEALTH_UNPROTECTED_ENTRY_ENABLED", h["unprotected_entry_enabled"])
print("HEALTH_PROTECTION_READY", h.get("protection_ready"))
print("HEALTH_TRADING_READY", h.get("trading_ready"))
PY
    then
      rm -f /tmp/xko-kill-health.json
      echo "BACKUP_CREATED $backup"
      echo "XKO_KILL_SWITCH_OK"
      exit 0
    fi
  fi
  sleep 1
done

rm -f /tmp/xko-kill-health.json
systemctl --no-pager --full status "$SERVICE" || true
echo "FAIL: service/health did not verify safe state" >&2
exit 1
