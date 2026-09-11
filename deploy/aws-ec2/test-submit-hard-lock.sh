#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <intent_id>" >&2
  exit 2
fi

INTENT_ID="$1"
ENV_FILE="/etc/xko/nautilus-bridge.env"
BASE_URL="http://127.0.0.1:8765"
PYTHON="/opt/xko/.venv/bin/python"

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

if [[ -z "${BRIDGE_API_TOKEN:-}" ]]; then
  echo "REFUSING TEST: BRIDGE_API_TOKEN is missing" >&2
  exit 3
fi

AUTH_HEADER="Authorization: Bearer ${BRIDGE_API_TOKEN}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fsS \
  -H "$AUTH_HEADER" \
  "$BASE_URL/intents/$INTENT_ID" \
  > "$TMP_DIR/before.json"

HTTP_CODE="$(curl -sS \
  -o "$TMP_DIR/submit.json" \
  -w '%{http_code}' \
  -X POST \
  -H "$AUTH_HEADER" \
  "$BASE_URL/intents/$INTENT_ID/submit")"

curl -fsS \
  -H "$AUTH_HEADER" \
  "$BASE_URL/intents/$INTENT_ID" \
  > "$TMP_DIR/after.json"

"$PYTHON" - "$HTTP_CODE" "$TMP_DIR/before.json" "$TMP_DIR/submit.json" "$TMP_DIR/after.json" <<'PY'
import json
import sys

http_code, before_path, submit_path, after_path = sys.argv[1:]
with open(before_path) as f:
    before = json.load(f)
with open(submit_path) as f:
    submit = json.load(f)
with open(after_path) as f:
    after = json.load(f)

expected = {"detail": "Order submission disabled by ALLOW_ORDER_SUBMIT=false"}

print("HTTP_CODE", http_code)
print("RESPONSE", json.dumps(submit, separators=(",", ":"), sort_keys=True))
print("INTENT_STATUS_BEFORE", before.get("status"))
print("INTENT_STATUS_AFTER", after.get("status"))
print("INTENT_UNCHANGED", before == after)

if http_code != "403":
    raise SystemExit("FAIL: submit endpoint did not return HTTP 403")
if submit != expected:
    raise SystemExit("FAIL: unexpected hard-lock response body")
if before != after:
    raise SystemExit("FAIL: intent mutated despite submit hard lock")

print("SUBMIT_HARD_LOCK_OK")
PY
