from __future__ import annotations

import queue
from datetime import timedelta
from decimal import Decimal
from typing import Any

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderFilled,
    OrderRejected,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency
from nautilus_trader.trading import Strategy

from bridge_runtime import BridgeCommand, BridgeRuntime, CommandKind
from trading_models import CommandResult, EntryType, IntentRecord, IntentStatus, Side


class AIIntentStrategy(Strategy):
    """Consume externally-created TradeIntents on the Nautilus event thread."""

    POLL_TIMER = "xko.ai_intent.poll"

    def __init__(self, config: StrategyConfig, runtime: BridgeRuntime, account_id: AccountId) -> None:
        super().__init__(config)
        self._runtime = runtime
        self._account_id = account_id
        self._intent_by_client_order_id: dict[str, str] = {}

    def on_start(self) -> None:
        self.clock.set_timer(
            self.POLL_TIMER,
            timedelta(milliseconds=250),
            callback=self._on_poll,
        )
        self._runtime.ready.set()
        self.log.info("AIIntentStrategy ready; external intent bridge enabled")

    def on_stop(self) -> None:
        self._runtime.ready.clear()
        try:
            self.clock.cancel_timer(self.POLL_TIMER)
        except Exception:
            pass

    def _on_poll(self, _event: TimeEvent) -> None:
        for _ in range(20):
            try:
                cmd = self._runtime.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_command(cmd)
            except Exception as exc:
                self.log.error(f"Bridge command failed: {exc}")
                self._runtime.store.update_execution(
                    cmd.intent_id,
                    status=IntentStatus.ERROR,
                    error=str(exc),
                    last_event="BRIDGE_COMMAND_ERROR",
                )
                if cmd.reply is not None:
                    cmd.reply.put_nowait(CommandResult(ok=False, error=str(exc)).model_dump(mode="json"))

    def _handle_command(self, cmd: BridgeCommand) -> None:
        record = self._runtime.store.get(cmd.intent_id)
        if cmd.kind == CommandKind.PREVIEW:
            data = self._size_intent(record)
            if cmd.reply is not None:
                cmd.reply.put_nowait(CommandResult(ok=True, data=data).model_dump(mode="json"))
            return
        if cmd.kind == CommandKind.SUBMIT:
            self._submit_intent(record)
            return
        raise ValueError(f"Unknown command kind: {cmd.kind}")

    def _validate_policy(self, record: IntentRecord) -> None:
        request = record.request
        policy = self._runtime.policy
        if request.instrument_id not in policy.allowed_instruments:
            raise ValueError(f"Instrument {request.instrument_id} is not in ALLOWED_INSTRUMENTS")
        if request.risk_pct > policy.max_risk_pct:
            raise ValueError(
                f"risk_pct {request.risk_pct}% exceeds MAX_RISK_PCT {policy.max_risk_pct}%"
            )
        if request.entry_type == EntryType.MARKET and not policy.allow_market_entry:
            raise ValueError("MARKET entry disabled by ALLOW_MARKET_ENTRY=false")

    @staticmethod
    def _decimal_attr(value: Any, name: str) -> Decimal | None:
        attr = getattr(value, name, None)
        if attr is None:
            return None
        return attr.as_decimal()

    def _size_intent(self, record: IntentRecord) -> dict[str, str | bool]:
        """Size only linear OKX perpetual swaps in native contract units.

        For a linear contract, approximate stop risk per contract as:
            abs(entry - stop) * instrument.multiplier

        Contract count is rounded down to the venue size increment and capped by:
        - MAX_ORDER_QTY
        - MAX_NOTIONAL_PER_ORDER_USDT
        - venue max quantity, when present
        """
        self._validate_policy(record)
        request = record.request
        instrument_id = InstrumentId.from_str(request.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Instrument {instrument_id} is not loaded in Nautilus cache")

        # V2 sizing is deliberately narrow: only OKX linear SWAP contracts are
        # accepted. Reject unsupported derivative shapes rather than guessing.
        if not request.instrument_id.endswith("-SWAP.OKX"):
            raise RuntimeError("Contract-aware sizing currently supports OKX SWAP instruments only")
        if not isinstance(instrument, CryptoPerpetual):
            raise RuntimeError(
                f"Expected CryptoPerpetual for {instrument_id}, got {type(instrument).__name__}"
            )
        if bool(instrument.is_inverse):
            raise RuntimeError("Inverse OKX SWAP sizing is not supported; refusing to guess quantity")

        equity_map = self.portfolio.equity(account_id=self._account_id)
        currency = Currency.from_str(self._runtime.policy.equity_currency)
        equity = equity_map.get(currency)
        if equity is None:
            available = ", ".join(str(x) for x in equity_map.keys()) or "(none)"
            raise RuntimeError(f"No {currency} equity available from portfolio; available={available}")

        if str(instrument.quote_currency) != str(currency):
            raise RuntimeError(
                f"Linear SWAP quote currency {instrument.quote_currency} does not match "
                f"EQUITY_CURRENCY {currency}; FX-aware sizing is not implemented"
            )

        multiplier = self._decimal_attr(instrument, "multiplier")
        if multiplier is None or multiplier <= 0:
            raise RuntimeError(f"Invalid/missing contract multiplier for {instrument_id}: {multiplier}")

        size_increment = instrument.size_increment.as_decimal()
        if size_increment <= 0:
            raise RuntimeError(f"Invalid size_increment for {instrument_id}: {size_increment}")

        entry = Decimal(str(request.entry_price))
        stop = Decimal(str(request.stop_loss))
        stop_distance = abs(entry - stop)
        if stop_distance <= 0:
            raise RuntimeError("entry_price and stop_loss must differ")

        equity_decimal = equity.as_decimal()
        if equity_decimal <= 0:
            raise RuntimeError(f"Account equity must be positive, got {equity}")

        risk_fraction = request.risk_pct / Decimal("100")
        target_risk_money = equity_decimal * risk_fraction
        risk_per_contract = stop_distance * multiplier
        if risk_per_contract <= 0:
            raise RuntimeError("Calculated risk per contract is non-positive")

        raw_quantity = target_risk_money / risk_per_contract

        policy = self._runtime.policy
        if policy.max_order_qty <= 0:
            raise RuntimeError(f"MAX_ORDER_QTY must be positive, got {policy.max_order_qty}")
        if policy.max_notional_per_order_usdt <= 0:
            raise RuntimeError(
                "MAX_NOTIONAL_PER_ORDER_USDT must be positive, got "
                f"{policy.max_notional_per_order_usdt}"
            )

        # For a linear contract quoted in the equity currency, notional per
        # contract is entry * multiplier. This makes Preview honor the same
        # absolute notional ceiling configured on the Nautilus RiskEngine.
        notional_per_contract = entry * multiplier
        if notional_per_contract <= 0:
            raise RuntimeError("Calculated notional per contract is non-positive")
        notional_quantity_limit = policy.max_notional_per_order_usdt / notional_per_contract

        effective_limit = min(policy.max_order_qty, notional_quantity_limit)
        cap_reasons: list[str] = []
        if raw_quantity > policy.max_order_qty:
            cap_reasons.append("MAX_ORDER_QTY")
        if raw_quantity > notional_quantity_limit:
            cap_reasons.append("MAX_NOTIONAL_PER_ORDER_USDT")

        venue_max_quantity = self._decimal_attr(instrument, "max_quantity")
        if venue_max_quantity is not None and venue_max_quantity > 0:
            if raw_quantity > venue_max_quantity:
                cap_reasons.append("VENUE_MAX_QUANTITY")
            effective_limit = min(effective_limit, venue_max_quantity)
        if effective_limit <= 0:
            raise RuntimeError(f"Effective max order quantity must be positive, got {effective_limit}")

        capped_quantity = min(raw_quantity, effective_limit)
        rounded_quantity = (capped_quantity // size_increment) * size_increment
        if rounded_quantity <= 0:
            raise RuntimeError(
                "Contract-aware sizing produced zero quantity after lot-size rounding; "
                f"raw={raw_quantity} capped={capped_quantity} size_increment={size_increment}"
            )

        min_quantity = self._decimal_attr(instrument, "min_quantity")
        if min_quantity is not None and min_quantity > 0 and rounded_quantity < min_quantity:
            raise RuntimeError(
                f"Sized quantity {rounded_quantity} is below venue min_quantity {min_quantity}"
            )

        quantity = instrument.make_qty(rounded_quantity)
        estimated_risk_money = rounded_quantity * risk_per_contract
        estimated_risk_fraction = estimated_risk_money / equity_decimal
        estimated_risk_pct = estimated_risk_fraction * Decimal("100")
        estimated_notional = rounded_quantity * notional_per_contract
        lot_size = self._decimal_attr(instrument, "lot_size")

        self._runtime.store.update_execution(
            record.intent_id,
            sized_quantity=str(quantity),
            equity_used=str(equity),
            risk_fraction_used=str(risk_fraction),
            last_event="PREVIEW_SIZED_LINEAR_SWAP",
        )
        return {
            "intent_id": record.intent_id,
            "instrument_id": request.instrument_id,
            "sizing_model": "okx_linear_swap_contracts_v2",
            "quantity": str(quantity),
            "equity": str(equity),
            "risk_fraction": str(risk_fraction),
            "target_risk_money": str(target_risk_money),
            "estimated_risk_money": str(estimated_risk_money),
            "estimated_risk_pct": str(estimated_risk_pct),
            "estimated_notional": str(estimated_notional),
            "raw_quantity": str(raw_quantity),
            "stop_distance": str(stop_distance),
            "risk_per_contract": str(risk_per_contract),
            "notional_per_contract": str(notional_per_contract),
            "contract_multiplier": str(multiplier),
            "size_increment": str(size_increment),
            "lot_size": str(lot_size) if lot_size is not None else "",
            "min_quantity": str(min_quantity) if min_quantity is not None else "",
            "max_quantity": str(venue_max_quantity) if venue_max_quantity is not None else "",
            "max_order_qty": str(policy.max_order_qty),
            "max_notional_per_order_usdt": str(policy.max_notional_per_order_usdt),
            "notional_quantity_limit": str(notional_quantity_limit),
            "effective_quantity_limit": str(effective_limit),
            "capped_by": ",".join(cap_reasons),
            "is_inverse": bool(instrument.is_inverse),
            "submit_enabled": policy.allow_order_submit,
            "warning": (
                "Linear OKX SWAP contract sizing only. V1 still does not submit a protective "
                "stop/TP child; keep order submission disabled."
            ),
        }

    def _submit_intent(self, record: IntentRecord) -> None:
        if record.status != IntentStatus.QUEUED:
            raise ValueError(f"Expected QUEUED before execution, got {record.status}")
        if not self._runtime.policy.allow_order_submit:
            raise PermissionError("ALLOW_ORDER_SUBMIT=false")
        if not self._runtime.policy.allow_unprotected_entry:
            raise PermissionError(
                "ALLOW_UNPROTECTED_ENTRY=false; refusing entry without protective child order"
            )

        sizing = self._size_intent(record)
        request = record.request
        instrument_id = InstrumentId.from_str(request.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Instrument disappeared from cache: {instrument_id}")

        quantity = instrument.make_qty(sizing["quantity"])
        client_order_id = ClientOrderId(record.client_order_id)
        order_side = OrderSide.BUY if request.side == Side.BUY else OrderSide.SELL

        if request.entry_type == EntryType.LIMIT:
            order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=order_side,
                quantity=quantity,
                price=instrument.make_price(str(request.entry_price)),
                time_in_force=TimeInForce.GTC,
                post_only=False,
                reduce_only=False,
                client_order_id=client_order_id,
                tags=["AI_INTENT", record.intent_id],
            )
        elif request.entry_type == EntryType.MARKET:
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=order_side,
                quantity=quantity,
                time_in_force=TimeInForce.IOC,
                reduce_only=False,
                client_order_id=client_order_id,
                tags=["AI_INTENT", record.intent_id],
            )
        else:
            raise ValueError(f"Unsupported entry type: {request.entry_type}")

        self._intent_by_client_order_id[str(order.client_order_id)] = record.intent_id
        self.submit_order(order)
        self._runtime.store.update_execution(
            record.intent_id,
            status=IntentStatus.SUBMITTED,
            nautilus_order_id=str(order.client_order_id),
            last_event="ORDER_SUBMITTED_TO_NAUTILUS",
            mark_submitted=True,
        )

    def _intent_for_event(self, event: Any) -> str | None:
        client_order_id = getattr(event, "client_order_id", None)
        if client_order_id is None:
            return None
        key = str(client_order_id)
        intent_id = self._intent_by_client_order_id.get(key)
        if intent_id is not None:
            return intent_id
        for record in self._runtime.store.list_recent(limit=200):
            if record.client_order_id == key:
                self._intent_by_client_order_id[key] = record.intent_id
                return record.intent_id
        return None

    def on_order_accepted(self, event: OrderAccepted) -> None:
        intent_id = self._intent_for_event(event)
        if intent_id:
            self._runtime.store.update_execution(intent_id, status=IntentStatus.ACCEPTED, last_event="OrderAccepted")

    def on_order_rejected(self, event: OrderRejected) -> None:
        intent_id = self._intent_for_event(event)
        if intent_id:
            self._runtime.store.update_execution(
                intent_id, status=IntentStatus.REJECTED, last_event="OrderRejected", error=str(event)
            )

    def on_order_denied(self, event: OrderDenied) -> None:
        intent_id = self._intent_for_event(event)
        if intent_id:
            self._runtime.store.update_execution(
                intent_id, status=IntentStatus.REJECTED, last_event="OrderDenied", error=str(event)
            )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        intent_id = self._intent_for_event(event)
        if intent_id:
            self._runtime.store.update_execution(intent_id, status=IntentStatus.CANCELED, last_event="OrderCanceled")

    def on_order_filled(self, event: OrderFilled) -> None:
        intent_id = self._intent_for_event(event)
        if intent_id is None:
            return
        order = self.cache.order(event.client_order_id)
        status = IntentStatus.FILLED if order is not None and order.is_closed() else IntentStatus.PARTIALLY_FILLED
        self._runtime.store.update_execution(intent_id, status=status, last_event="OrderFilled")
