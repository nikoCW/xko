from __future__ import annotations

import asyncio
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


class _Clock:
    def timestamp_ns(self) -> int:
        return 1


class _Log:
    def warning(self, *_args, **_kwargs) -> None:
        pass


class _NoNetworkOKXProbe:
    """Run OKXExecutionClient._submit_order_list while replacing the network boundary."""

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
        self._clock = _Clock()
        self._log = _Log()
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
        # This is the first real network boundary in the v1.231 attached-bracket path.
        # Capture it instead of calling OKXHttpClient.place_order.
        self.place_calls.append(
            {
                "order": order,
                "params": params,
                "attach_algo_ords": attach_algo_ords,
            }
        )


def _bracket():
    factory = OrderFactory(
        TraderId("TRADER-001"),
        StrategyId("S-001"),
        Clock.new_test(),
    )
    return factory.bracket(
        instrument_id=InstrumentId.from_str("HYPE-USDT-SWAP.OKX"),
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str("64"),
        entry_order_type=OrderType.LIMIT,
        entry_price=Price.from_str("78.951"),
        time_in_force=TimeInForce.GTC,
        entry_post_only=False,
        entry_client_order_id=ClientOrderId("XKOC3EDF8743BFD4D2C9A006839"),
        sl_order_type=OrderType.STOP_MARKET,
        sl_trigger_price=Price.from_str("78.161"),
        sl_trigger_type=TriggerType.LAST_PRICE,
        sl_client_order_id=ClientOrderId("XKSC3EDF8743BFD4D2C9A006839"),
        tp_order_type=OrderType.MARKET_IF_TOUCHED,
        tp_trigger_price=Price.from_str("80.000"),
        tp_trigger_type=TriggerType.LAST_PRICE,
        tp_post_only=False,
        tp_client_order_id=ClientOrderId("XKTC3EDF8743BFD4D2C9A006839"),
    )


def test_okx_attached_oco_payload_is_one_parent_with_market_on_trigger_children() -> None:
    bracket = _bracket()
    probe = _NoNetworkOKXProbe()

    asyncio.run(
        OKXExecutionClient._submit_order_list(
            probe,
            SimpleNamespace(order_list=bracket, params={}),
        )
    )

    assert probe.denied_events == []
    assert probe.rejected_events == []
    assert len(probe.submitted_events) == 3
    assert len(probe.place_calls) == 1

    call = probe.place_calls[0]
    parent = call["order"]
    payload = call["attach_algo_ords"]
    assert parent.client_order_id == ClientOrderId("XKOC3EDF8743BFD4D2C9A006839")
    assert parent.order_type == OrderType.LIMIT
    assert parent.side == OrderSide.BUY
    assert parent.quantity == Quantity.from_str("64")

    assert payload == [
        {
            "attach_algo_cl_ord_id": "XKSC3EDF8743BFD4D2C9A006839",
            "sl_trigger_px": "78.161",
            "sl_ord_px": "-1",
            "sl_trigger_px_type": "last",
            "tp_trigger_px": "80.000",
            "tp_ord_px": "-1",
            "tp_trigger_px_type": "last",
        }
    ]

    parent_order, stop_order, take_profit_order = probe.binding
    assert parent_order is parent
    assert stop_order.order_type == OrderType.STOP_MARKET
    assert stop_order.is_reduce_only is True
    assert stop_order.side == OrderSide.SELL
    assert take_profit_order.order_type == OrderType.MARKET_IF_TOUCHED
    assert take_profit_order.is_reduce_only is True
    assert take_profit_order.side == OrderSide.SELL
