#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/xko/nautilus-bridge.env"
PYTHON="/opt/xko/.venv/bin/python"
SCRIPT="/opt/xko/deploy/aws-ec2/fail_closed_fault_injection.py"

if [[ ! -r "$ENV_FILE" ]]; then
  echo "cannot read $ENV_FILE; run with sudo" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

for flag in ALLOW_ORDER_SUBMIT ALLOW_UNPROTECTED_ENTRY ALLOW_MARKET_ENTRY; do
  if [[ "${!flag:-}" != "false" ]]; then
    echo "REFUSING TEST: $flag must be exactly false" >&2
    exit 3
  fi
done

PYTHONPATH=/opt/xko/nautilus_bridge \
  "$PYTHON" "$SCRIPT"
