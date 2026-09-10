from __future__ import annotations

import queue
from datetime import timedelta
from decimal import Decimal
from typing import Any

from nautilus_trader.common import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import AccountId, ClientOrderId, Currency, InstrumentId
from nautilus_trader.model import OrderAccepted, OrderCanceled, OrderDenied, OrderFilled, OrderRejected
from nautilus_trader.model import OrderSide, TimeInForce
from nautilus_trader.risk import FixedRiskSizer
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

    def _size_intent(self, record: IntentRecord) -> dict[str, str | bool]:
        self._validate_policy(record)
        request = record.request
        instrument_id = InstrumentId.from_str(request.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Instrument {instrument_id} is not loaded in Nautilus cache")

        equity_map = self.portfolio.equity(account_id=self._account_id)
        currency = Currency.from_str(self._runtime.policy.equity_currency)
        equity = equity_map.get(currency)
        if equity is None:
            available = ", ".join(str(x) for x in equity_map.keys()) or "(none)"
            raise RuntimeError(f"No {currency} equity available from portfolio; available={available}")

        risk_fraction = request.risk_pct / Decimal("100")
        quantity = FixedRiskSizer(instrument).calculate(
            entry=instrument.make_price(str(request.entry_price)),
            stop_loss=instrument.make_price(str(request.stop_loss)),
            equity=equity,
            risk=risk_fraction,
            hard_limit=self._runtime.policy.max_order_qty,
            unit_batch_size=instrument.size_increment.as_decimal(),
            units=1,
        )
        if quantity.as_decimal() <= 0:
            raise RuntimeError("FixedRiskSizer produced zero/non-positive quantity")

        self._runtime.store.update_execution(
            record.intent_id,
            sized_quantity=str(quantity),
            equity_used=str(equity),
            risk_fraction_used=str(risk_fraction),
            last_event="PREVIEW_SIZED",
        )
        return {
            "intent_id": record.intent_id,
            "instrument_id": request.instrument_id,
            "quantity": str(quantity),
            "equity": str(equity),
            "risk_fraction": str(risk_fraction),
            "submit_enabled": self._runtime.policy.allow_order_submit,
            "warning": "V1 only sizes from stop_loss; it does not submit a protective stop/TP child.",
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
