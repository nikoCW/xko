#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <intent_id>" >&2
  exit 2
fi

ENV_FILE="/etc/xko/nautilus-bridge.env"
PYTHON="/opt/xko/.venv/bin/python"
PROBE="/opt/xko/deploy/aws-ec2/okx_payload_dry_run.py"

if [[ ! -r "$ENV_FILE" ]]; then
  echo "cannot read $ENV_FILE; run with sudo" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ "${ALLOW_ORDER_SUBMIT:-}" != "false" ]]; then
  echo "REFUSING TEST: ALLOW_ORDER_SUBMIT must be exactly false" >&2
  exit 3
fi
if [[ "${ALLOW_UNPROTECTED_ENTRY:-}" != "false" ]]; then
  echo "REFUSING TEST: ALLOW_UNPROTECTED_ENTRY must be exactly false" >&2
  exit 3
fi
if [[ "${ALLOW_MARKET_ENTRY:-}" != "false" ]]; then
  echo "REFUSING TEST: ALLOW_MARKET_ENTRY must be exactly false" >&2
  exit 3
fi
if [[ -z "${BRIDGE_API_TOKEN:-}" ]]; then
  echo "REFUSING TEST: BRIDGE_API_TOKEN is missing" >&2
  exit 3
fi
if [[ ! -r "$PROBE" ]]; then
  echo "cannot read $PROBE" >&2
  exit 2
fi

exec "$PYTHON" "$PROBE" "$1"
