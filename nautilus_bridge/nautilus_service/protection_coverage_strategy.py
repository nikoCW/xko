from __future__ import annotations

from decimal import Decimal
from typing import Any

from nautilus_trader.model.enums import OrderSide, OrderType

from nautilus_service.bridge_strategy import AIIntentStrategy as BaseAIIntentStrategy


class AIIntentStrategy(BaseAIIntentStrategy):
    """Strengthen XKO-owned protection checks with side and full-quantity coverage.

    The base strategy already scopes protection ownership to persisted XKO intents and
    ignores unrelated manual/grid positions globally. This subclass keeps that ownership
    model, but requires the expected XKO stop child to be reduce-only, on the closing side,
    and to have enough remaining quantity to cover the current reconciled position.
    """

    @staticmethod
    def _quantity_decimal(value: Any) -> Decimal:
        if value is None:
            raise ValueError("quantity_missing")
        as_decimal = getattr(value, "as_decimal", None)
        if callable(as_decimal):
            return Decimal(str(as_decimal()))
        return Decimal(str(value))

    def _xko_stop_coverage_issue(self, intent_id: str, position: Any) -> str | None:
        instrument_id = position.instrument_id
        try:
            record = self._runtime.store.get(intent_id)
        except KeyError:
            return "intent_missing"

        expected_stop_id = record.stop_loss_order_id
        if not expected_stop_id:
            return "stop_id_missing"

        open_orders = self.cache.orders_open(
            instrument_id=instrument_id,
            account_id=self._account_id,
        )
        stop_order = next(
            (
                order
                for order in open_orders
                if str(getattr(order, "client_order_id", "")) == expected_stop_id
            ),
            None,
        )
        if stop_order is None:
            return "stop_not_open"

        if stop_order.order_type not in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT):
            return f"stop_wrong_type:{stop_order.order_type}"
        if not bool(stop_order.is_reduce_only):
            return "stop_not_reduce_only"

        if bool(getattr(position, "is_long", False)):
            expected_side = OrderSide.SELL
        elif bool(getattr(position, "is_short", False)):
            expected_side = OrderSide.BUY
        else:
            return f"position_side_unknown:{getattr(position, 'side', None)}"

        if stop_order.side != expected_side:
            return f"stop_wrong_side:actual={stop_order.side}:expected={expected_side}"

        try:
            position_qty = self._quantity_decimal(getattr(position, "quantity", None))
            stop_leaves_qty = self._quantity_decimal(getattr(stop_order, "leaves_qty", None))
        except Exception as exc:
            return f"coverage_quantity_unreadable:{type(exc).__name__}:{exc}"

        if position_qty <= 0:
            return f"position_quantity_invalid:{position_qty}"
        if stop_leaves_qty < position_qty:
            return f"stop_undercovered:leaves={stop_leaves_qty}:position={position_qty}"

        return None

    def _has_live_xko_stop(self, intent_id: str, instrument_id: Any) -> bool:
        positions = [
            position
            for position in self._open_positions(instrument_id)
            if self._intent_id_for_position(position) == intent_id
        ]
        return bool(positions) and all(
            self._xko_stop_coverage_issue(intent_id, position) is None
            for position in positions
        )

    def _refresh_protection_gate(self) -> None:
        """Fail closed if any XKO-owned position lacks complete stop coverage."""
        try:
            positions = self.cache.positions_open(account_id=self._account_id)
            invalid: list[str] = []
            allowed = self._runtime.policy.allowed_instruments

            for position in positions:
                instrument_id = position.instrument_id
                if str(instrument_id) not in allowed:
                    continue

                intent_id = self._intent_id_for_position(position)
                if intent_id is None:
                    continue

                issue = self._xko_stop_coverage_issue(intent_id, position)
                if issue is not None:
                    invalid.append(f"{instrument_id}:{intent_id}:{issue}")

            if invalid:
                self._runtime.update_protection_readiness(
                    False,
                    "xko_position_protection_invalid:" + ",".join(sorted(set(invalid))),
                )
            else:
                self._runtime.update_protection_readiness(True)
        except Exception as exc:
            self._runtime.update_protection_readiness(
                False,
                f"protection_scan_error:{type(exc).__name__}:{exc}",
            )
