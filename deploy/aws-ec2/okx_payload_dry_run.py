from __future__ import annotations

import asyncio
import json
import os
import sys
from importlib.metadata import version
from types import SimpleNamespace
from urllib.request import Request, urlopen

from nautilus_trader.adapters.okx.execution import OKXExecutionClient
from nautilus_trader.common.component import TestClock
from nautilus_trader.common.factories import OrderFactory
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, TriggerType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId, StrategyId, TraderId
from nautilus_trader.model.objects import Price, Quantity


BASE_URL = "http://127.0.0.1:8765"
EXPECTED_NAUTILUS_VERSION = "1.231.0"


class ProbeClock:
    def timestamp_ns(self) -> int:
        return 1


class ProbeLog:
    def warning(self, *_args, **_kwargs) -> None:
        pass


class NoNetworkOKXProbe:
    """Execute the real v1.231 bracket serializer, replacing the network boundary."""

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
        # First real network boundary in the v1.231 attached-bracket path.
        # Capture the parent + serialized payload and never call OKXHttpClient.place_order.
        self.place_calls.append(
            {
                "order": order,
                "params": params,
                "attach_algo_ords": attach_algo_ords,
            }
        )


def fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def enum_raw(value) -> str:
    raw = getattr(value, "value", value)
    try:
        return str(int(raw))
    except (TypeError, ValueError):
        return str(raw)


def child_id(parent: str, prefix: str) -> str:
    if not parent.startswith("XKO"):
        fail(f"unexpected XKO parent client order ID: {parent}")
    return prefix + parent[3:]


def get_intent(intent_id: str, token: str) -> dict:
    req = Request(
        f"{BASE_URL}/intents/{intent_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urlopen(req, timeout=5) as response:
        return json.load(response)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: okx_payload_dry_run.py <intent_id>")

    intent_id = sys.argv[1]
    token = os.environ.get("BRIDGE_API_TOKEN", "")
    if not token:
        fail("BRIDGE_API_TOKEN is missing")

    installed_version = version("nautilus_trader")
    if installed_version != EXPECTED_NAUTILUS_VERSION:
        fail(
            f"NautilusTrader version mismatch: installed={installed_version} "
            f"expected={EXPECTED_NAUTILUS_VERSION}"
        )

    before = get_intent(intent_id, token)
    request = before["request"]
    sized_quantity = before.get("sized_quantity")
    if sized_quantity in (None, ""):
        fail("intent has no sized_quantity; run preview first")
    if request.get("take_profit") in (None, ""):
        fail("protected payload dry-run requires take_profit")

    parent_id = str(before["client_order_id"])
    stop_id = child_id(parent_id, "XKS")
    tp_id = child_id(parent_id, "XKT")
    side = OrderSide.BUY if request["side"] == "BUY" else OrderSide.SELL
    quantity = Quantity.from_str(str(sized_quantity))

    factory = OrderFactory(
        trader_id=TraderId("TRADER-001"),
        strategy_id=StrategyId("S-001"),
        clock=TestClock(),
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
        fail(f"unsupported entry_type={request['entry_type']}")

    bracket = factory.bracket(**kwargs)
    probe = NoNetworkOKXProbe()
    asyncio.run(
        OKXExecutionClient._submit_order_list(
            probe,
            SimpleNamespace(order_list=bracket, params={}),
        )
    )

    if probe.denied_events:
        fail(f"adapter denied bracket: {probe.denied_events}")
    if probe.rejected_events:
        fail(f"adapter rejected bracket: {probe.rejected_events}")
    if len(probe.place_calls) != 1:
        fail(f"expected one captured parent place call, got {len(probe.place_calls)}")
    if len(probe.submitted_events) != 3:
        fail(f"expected three local OrderSubmitted events, got {len(probe.submitted_events)}")
    if probe.binding is None:
        fail("adapter did not register attached-OCO binding")

    call = probe.place_calls[0]
    parent_order = call["order"]
    attach_algo_ords = call["attach_algo_ords"]
    parent, stop_order, tp_order = probe.binding

    if parent_order.client_order_id != ClientOrderId(parent_id):
        fail("adapter place call was not for the parent entry")
    if parent is not parent_order:
        fail("captured attached-OCO binding parent mismatch")
    if stop_order is None or tp_order is None:
        fail("adapter did not resolve both protective children")

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
        fail(
            "unexpected OKX attach_algo_ords payload\n"
            f"actual={json.dumps(attach_algo_ords, sort_keys=True)}\n"
            f"expected={json.dumps(expected_payload, sort_keys=True)}"
        )

    if stop_order.order_type != OrderType.STOP_MARKET or not bool(stop_order.is_reduce_only):
        fail("stop-loss leg is not reduce-only STOP_MARKET")
    if tp_order.order_type != OrderType.MARKET_IF_TOUCHED or not bool(tp_order.is_reduce_only):
        fail("take-profit leg is not reduce-only MARKET_IF_TOUCHED")
    if stop_order.side == parent_order.side or tp_order.side == parent_order.side:
        fail("protective child side does not oppose parent")

    after = get_intent(intent_id, token)
    if before != after:
        fail("intent mutated during OKX payload dry-run")

    print("NAUTILUS_VERSION", installed_version)
    print("INTENT_ID", before["intent_id"])
    print("INTENT_STATUS", before["status"])
    print("PARENT_CLIENT_ORDER_ID", parent_id)
    print("PARENT_ORDER_TYPE", str(parent_order.order_type))
    print("PARENT_SIDE", str(parent_order.side))
    print("PARENT_QUANTITY", str(parent_order.quantity))
    print("STOP_CLIENT_ORDER_ID", stop_id)
    print("STOP_TRIGGER_TYPE_RAW", enum_raw(stop_order.trigger_type))
    print("TP_CLIENT_ORDER_ID", tp_id)
    print("TP_TRIGGER_TYPE_RAW", enum_raw(tp_order.trigger_type))
    print(
        "ATTACH_ALGO_ORDS",
        json.dumps(attach_algo_ords, separators=(",", ":"), sort_keys=True),
    )
    print("LOCAL_ORDER_SUBMITTED_EVENTS", len(probe.submitted_events))
    print("CAPTURED_PARENT_PLACE_CALLS", len(probe.place_calls))
    print("CAPTURED_CHILD_PLACE_CALLS", 0)
    print("REAL_NETWORK_CALLS", 0)
    print("INTENT_UNCHANGED", True)
    print("OKX_PAYLOAD_DRY_RUN_OK")


if __name__ == "__main__":
    main()
