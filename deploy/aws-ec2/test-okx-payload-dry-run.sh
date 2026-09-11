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

# This probe deliberately executes the Nautilus v1.231 OKX order-list serializer,
# but replaces _place_order_http before the network boundary. Never run it with
# order submission enabled.
if [[ "${ALLOW_ORDER_SUBMIT:-}" != "false" ]]; then
  echo "REFUSING TEST: ALLOW_ORDER_SUBMIT must be exactly false" >&2
  exit 3
fi
if [[ "${ALLOW_UNPROTECTED_ENTRY:-}" != "false" ]]; then
  echo "REFUSING TEST: ALLOW_UNPROTECTED_ENTRY must be exactly false" >&2
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

"$PYTHON" - "$TMP_DIR/before.json" <<'PY'
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

from nautilus_trader.adapters.okx.execution import OKXExecutionClient
from nautilus_trader.common import Clock, OrderFactory
from nautilus_trader.model import ClientOrderId
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import OrderType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.model import StrategyId
from nautilus_trader.model import TimeInForce
from nautilus_trader.model import TraderId
from nautilus_trader.model import TriggerType


class ProbeClock:
    def timestamp_ns(self) -> int:
        return 1


class ProbeLog:
    def warning(self, *_args, **_kwargs) -> None:
        pass


class NoNetworkOKXProbe:
    """Use the real v1.231 adapter serializer but replace the network boundary."""

    _OKX_CONDITIONAL_ORDER_TYPES = OKXExecutionClient._OKX_CONDITIONAL_ORDER_TYPES
    _is_conditional_order = OKXExecutionClient._is_conditional_order
    _extract_attached_bracket_parent = OKXExecutionClient._extract_attached_bracket_parent
    _validate_attached_bracket_child = staticmethod(
        OKXExecutionClient._validate_attached_bracket_child
    )
    _assign_attached_bracket_child = staticmethod(
        OKXExecutionClient._assign_attached_bracket_child
    )
    _extract_attached_bracket_orders = OKXExecutionClient._extract_attached_bracket_orders
    _okx_trigger_type_str = staticmethod(OKXExecutionClient._okx_trigger_type_str)
    _attached_oco_attach_client_order_id = staticmethod(
        OKXExecutionClient._attached_oco_attach_client_order_id
    )
    _build_attach_algo_ords = OKXExecutionClient._build_attach_algo_ords
    _merge_attach_algo_ords = staticmethod(OKXExecutionClient._merge_attach_algo_ords)

    def __init__(self) -> None:
        self._clock = ProbeClock()
        self._log = ProbeLog()
        self.place_calls: list[dict] = []
        self.submitted_events: list[dict] = []
        self.rejected_events: list[dict] = []
        self.denied_events: list[dict] = []
        self.binding = None

    def _is_spread_instrument_id(self, _instrument_id) -> bool:
        return False

    def _register_attached_oco_binding(self, parent_order, sl_order, tp_order) -> None:
        self.binding = (parent_order, sl_order, tp_order)

    def _clear_attached_oco_binding(self, _client_order_id) -> None:
        self.binding = None

    def generate_order_submitted(self, **kwargs) -> None:
        self.submitted_events.append(kwargs)

    def generate_order_rejected(self, **kwargs) -> None:
        self.rejected_events.append(kwargs)

    def generate_order_denied(self, **kwargs) -> None:
        self.denied_events.append(kwargs)

    async def _place_order_http(self, *, order, params, attach_algo_ords=None) -> None:
        # Hard stop before OKXHttpClient.place_order: capture only, never network.
        self.place_calls.append(
            {
                "order": order,
                "params": params,
                "attach_algo_ords": attach_algo_ords,
            }
        )


def child_id(parent: str, prefix: str) -> str:
    if not parent.startswith("XKO"):
        raise SystemExit(f"FAIL: unexpected XKO parent client order ID: {parent}")
    return prefix + parent[3:]


with open(sys.argv[1]) as f:
    record = json.load(f)

request = record["request"]
if record.get("sized_quantity") in (None, ""):
    raise SystemExit("FAIL: intent has no sized_quantity; run preview first")
if request.get("take_profit") in (None, ""):
    raise SystemExit("FAIL: protected payload dry-run requires take_profit")

parent_id = str(record["client_order_id"])
stop_id = child_id(parent_id, "XKS")
tp_id = child_id(parent_id, "XKT")
side = OrderSide.BUY if request["side"] == "BUY" else OrderSide.SELL
quantity = Quantity.from_str(str(record["sized_quantity"]))

factory = OrderFactory(
    TraderId("TRADER-001"),
    StrategyId("S-001"),
    Clock.new_test(),
)

kwargs = {
    "instrument_id": InstrumentId.from_str(request["instrument_id"]),
    "order_side": side,
    "quantity": quantity,
    "entry_client_order_id": ClientOrderId(parent_id),
    "tp_order_type": OrderType.MARKET_IF_TOUCHED,
    "tp_trigger_price": Price.from_str(str(request["take_profit"])),
    "tp_trigger_type": TriggerType.LAST_PRICE,
    "tp_post_only": False,
    "tp_client_order_id": ClientOrderId(tp_id),
    "sl_order_type": OrderType.STOP_MARKET,
    "sl_trigger_price": Price.from_str(str(request["stop_loss"])),
    "sl_trigger_type": TriggerType.LAST_PRICE,
    "sl_client_order_id": ClientOrderId(stop_id),
}
if request["entry_type"] == "LIMIT":
    kwargs.update(
        entry_order_type=OrderType.LIMIT,
        entry_price=Price.from_str(str(request["entry_price"])),
        time_in_force=TimeInForce.GTC,
        entry_post_only=False,
    )
elif request["entry_type"] == "MARKET":
    kwargs.update(
        entry_order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
    )
else:
    raise SystemExit(f"FAIL: unsupported entry_type={request['entry_type']}")

bracket = factory.bracket(**kwargs)
probe = NoNetworkOKXProbe()
asyncio.run(
    OKXExecutionClient._submit_order_list(
        probe,
        SimpleNamespace(order_list=bracket, params={}),
    )
)

if probe.denied_events:
    raise SystemExit(f"FAIL: adapter denied bracket: {probe.denied_events}")
if probe.rejected_events:
    raise SystemExit(f"FAIL: adapter rejected bracket: {probe.rejected_events}")
if len(probe.place_calls) != 1:
    raise SystemExit(f"FAIL: expected one captured parent place call, got {len(probe.place_calls)}")
if len(probe.submitted_events) != 3:
    raise SystemExit(
        f"FAIL: expected three local OrderSubmitted events, got {len(probe.submitted_events)}"
    )

call = probe.place_calls[0]
parent_order = call["order"]
attach_algo_ords = call["attach_algo_ords"]
parent, stop_order, tp_order = probe.binding

if parent_order.client_order_id != ClientOrderId(parent_id):
    raise SystemExit("FAIL: adapter place call was not for the parent entry")
if parent is not parent_order:
    raise SystemExit("FAIL: captured attached-OCO binding parent mismatch")
if stop_order is None or tp_order is None:
    raise SystemExit("FAIL: adapter did not resolve both protective children")

expected_payload = [
    {
        "attach_algo_cl_ord_id": stop_id,
        "sl_trigger_px": str(stop_order.trigger_price),
        "sl_ord_px": "-1",
        "sl_trigger_px_type": "last",
        "tp_trigger_px": str(tp_order.trigger_price),
        "tp_ord_px": "-1",
        "tp_trigger_px_type": "last",
    }
]
if attach_algo_ords != expected_payload:
    raise SystemExit(
        "FAIL: unexpected OKX attach_algo_ords payload\n"
        f"actual={json.dumps(attach_algo_ords, sort_keys=True)}\n"
        f"expected={json.dumps(expected_payload, sort_keys=True)}"
    )

if stop_order.order_type != OrderType.STOP_MARKET or not bool(stop_order.is_reduce_only):
    raise SystemExit("FAIL: stop-loss leg is not reduce-only STOP_MARKET")
if tp_order.order_type != OrderType.MARKET_IF_TOUCHED or not bool(tp_order.is_reduce_only):
    raise SystemExit("FAIL: take-profit leg is not reduce-only MARKET_IF_TOUCHED")
if stop_order.side == parent_order.side or tp_order.side == parent_order.side:
    raise SystemExit("FAIL: protective child side does not oppose parent")

stop_trigger_raw = getattr(getattr(stop_order, "trigger_type", None), "value", getattr(stop_order, "trigger_type", None))
tp_trigger_raw = getattr(getattr(tp_order, "trigger_type", None), "value", getattr(tp_order, "trigger_type", None))

print("INTENT_ID", record["intent_id"])
print("INTENT_STATUS", record["status"])
print("PARENT_CLIENT_ORDER_ID", parent_id)
print("PARENT_ORDER_TYPE", getattr(parent_order.order_type, "name", str(parent_order.order_type)))
print("PARENT_SIDE", getattr(parent_order.side, "name", str(parent_order.side)))
print("PARENT_QUANTITY", str(parent_order.quantity))
print("STOP_CLIENT_ORDER_ID", stop_id)
print("STOP_TRIGGER_TYPE_RAW", stop_trigger_raw)
print("TP_CLIENT_ORDER_ID", tp_id)
print("TP_TRIGGER_TYPE_RAW", tp_trigger_raw)
print("ATTACH_ALGO_ORDS", json.dumps(attach_algo_ords, separators=(",", ":"), sort_keys=True))
print("LOCAL_ORDER_SUBMITTED_EVENTS", len(probe.submitted_events))
print("CAPTURED_PARENT_PLACE_CALLS", len(probe.place_calls))
print("CAPTURED_CHILD_PLACE_CALLS", 0)
print("REAL_NETWORK_CALLS", 0)
print("OKX_PAYLOAD_DRY_RUN_OK")
PY

curl -fsS \
  -H "$AUTH_HEADER" \
  "$BASE_URL/intents/$INTENT_ID" \
  > "$TMP_DIR/after.json"

"$PYTHON" - "$TMP_DIR/before.json" "$TMP_DIR/after.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as f:
    before = json.load(f)
with open(sys.argv[2]) as f:
    after = json.load(f)

unchanged = before == after
print("INTENT_UNCHANGED", unchanged)
if not unchanged:
    raise SystemExit("FAIL: intent mutated during OKX payload dry-run")
PY
