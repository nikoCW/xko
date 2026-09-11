from __future__ import annotations

import queue
from datetime import timedelta
from decimal import Decimal
from typing import Any

from nautilus_trader.common.events import TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.enums import OrderSide, OrderType, TimeInForce, TriggerType
from nautilus_trader.model.events import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderFilled,
    OrderRejected,
    OrderTriggered,
)
from nautilus_trader.model.identifiers import AccountId, ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency
from nautilus_trader.trading import Strategy

from bridge_runtime import BridgeCommand, BridgeRuntime, CommandKind
from trading_models import CommandResult, EntryType, IntentRecord, IntentStatus, Side


class AIIntentStrategy(Strategy):
    """Consume externally-created TradeIntents on the Nautilus event thread.

    Real execution is deliberately narrow: OKX linear SWAP only, submitted as a
    Nautilus bracket order list. NautilusTrader 1.231 translates the representable
    bracket into one OKX parent order with venue-native attached TP/SL (`attachAlgoOrds`).
    No separate post-fill reduce-only algo submission is used.
    """

    POLL_TIMER = "xko.ai_intent.poll"
    PROTECTION_MODE = "OKX_ATTACHED_OCO"

    def __init__(self, config: StrategyConfig, runtime: BridgeRuntime, account_id: AccountId) -> None:
        super().__init__(config)
        self._runtime = runtime
        self._account_id = account_id
        self._intent_by_client_order_id: dict[str, str] = {}
        self._role_by_client_order_id: dict[str, str] = {}

    def on_start(self) -> None:
        self.clock.set_timer(
            self.POLL_TIMER,
            timedelta(milliseconds=250),
            callback=self._on_poll,
        )
        self._restore_order_bindings()
        # NautilusTrader 1.231 starts strategies only after execution startup
        # reconciliation and portfolio initialization complete successfully.
        self._runtime.mark_strategy_started_after_reconciliation()
        self._refresh_protection_gate()
        self.log.info(
            "AIIntentStrategy ready; startup reconciliation complete and protection scan initialized"
        )

    def on_stop(self) -> None:
        self._runtime.mark_strategy_stopped()
        try:
            self.clock.cancel_timer(self.POLL_TIMER)
        except Exception:
            pass

    def _on_poll(self, _event: TimeEvent) -> None:
        # Probe Nautilus state only on its event thread. A post-start loss of
        # connectivity/portfolio readiness latches reconciliation closed until restart.
        self._runtime.refresh_readiness()
        self._refresh_protection_gate()

        for _ in range(20):
            try:
                cmd = self._runtime.commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_command(cmd)
            except Exception as exc:
                self.log.error(f"Bridge command failed: {exc}")
                # Preview is read-only. A sizing/validation failure must not mutate the
                # execution lifecycle into ERROR. Submission failures do.
                if cmd.kind == CommandKind.SUBMIT:
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

    def _require_preview_ready(self) -> None:
        self._runtime.refresh_readiness()
        if not self._runtime.preview_ready.is_set():
            reason = self._runtime.readiness_reason() or "not_ready"
            raise RuntimeError(f"Preview readiness gate closed: {reason}")

    def _require_trading_ready(self) -> None:
        self._runtime.refresh_readiness()
        self._refresh_protection_gate()
        if not self._runtime.trading_ready.is_set():
            reason = self._runtime.readiness_reason() or "not_ready"
            raise PermissionError(f"Trading readiness gate closed: {reason}")

    @staticmethod
    def _child_client_order_id(parent: str, role: str) -> str:
        """Derive deterministic short OKX-compatible child IDs from the parent ID."""
        if not parent.startswith("XKO"):
            raise ValueError(f"Unexpected XKO parent client order ID: {parent}")
        prefix = "XKS" if role == "STOP_LOSS" else "XKT"
        return prefix + parent[3:]

    def _register_order_binding(self, intent_id: str, client_order_id: str, role: str) -> None:
        self._intent_by_client_order_id[client_order_id] = intent_id
        self._role_by_client_order_id[client_order_id] = role

    def _restore_order_bindings(self) -> None:
        for record in self._runtime.store.list_recent(limit=200):
            self._register_order_binding(record.intent_id, record.client_order_id, "ENTRY")
            if record.stop_loss_order_id:
                self._register_order_binding(record.intent_id, record.stop_loss_order_id, "STOP_LOSS")
            if record.take_profit_order_id:
                self._register_order_binding(record.intent_id, record.take_profit_order_id, "TAKE_PROFIT")

    def _binding_for_event(self, event: Any) -> tuple[str | None, str | None]:
        client_order_id = getattr(event, "client_order_id", None)
        if client_order_id is None:
            return None, None
        key = str(client_order_id)
        intent_id = self._intent_by_client_order_id.get(key)
        role = self._role_by_client_order_id.get(key)
        if intent_id is not None:
            return intent_id, role

        for record in self._runtime.store.list_recent(limit=200):
            candidates = {
                record.client_order_id: "ENTRY",
                record.stop_loss_order_id: "STOP_LOSS",
                record.take_profit_order_id: "TAKE_PROFIT",
            }
            matched_role = candidates.get(key)
            if matched_role:
                self._register_order_binding(record.intent_id, key, matched_role)
                return record.intent_id, matched_role
        return None, None

    def _has_open_position(self, instrument_id: InstrumentId) -> bool:
        return bool(
            self.cache.positions_open(
                instrument_id=instrument_id,
                account_id=self._account_id,
            )
        )

    def _refresh_protection_gate(self) -> None:
        """Require every open allowed position to have a live reduce-only stop.

        This intentionally includes manual/external positions in the allowed instruments:
        if the account is already exposed without a protective stop, XKO must not open
        another position. Persistent failure is latched by BridgeRuntime until restart.
        """
        try:
            positions = self.cache.positions_open(account_id=self._account_id)
            missing: list[str] = []
            allowed = self._runtime.policy.allowed_instruments
            for position in positions:
                instrument_id = position.instrument_id
                if str(instrument_id) not in allowed:
                    continue
                open_orders = self.cache.orders_open(
                    instrument_id=instrument_id,
                    account_id=self._account_id,
                )
                has_protective_stop = any(
                    order.order_type in (OrderType.STOP_MARKET, OrderType.STOP_LIMIT)
                    and bool(order.is_reduce_only)
                    for order in open_orders
                )
                if not has_protective_stop:
                    missing.append(str(instrument_id))

            if missing:
                self._runtime.update_protection_readiness(
                    False,
                    "open_position_without_reduce_only_stop:" + ",".join(sorted(set(missing))),
                )
            else:
                self._runtime.update_protection_readiness(True)
        except Exception as exc:
            self._runtime.update_protection_readiness(
                False,
                f"protection_scan_error:{type(exc).__name__}:{exc}",
            )

    def _size_intent(self, record: IntentRecord) -> dict[str, Any]:
        """Size only linear OKX perpetual swaps in native contract units."""
        self._require_preview_ready()
        self._validate_policy(record)
        request = record.request
        instrument_id = InstrumentId.from_str(request.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Instrument {instrument_id} is not loaded in Nautilus cache")

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
        protected_submit_ready = request.take_profit is not None

        self._runtime.store.update_execution(
            record.intent_id,
            sized_quantity=str(quantity),
            equity_used=str(equity),
            risk_fraction_used=str(risk_fraction),
            last_event="PREVIEW_SIZED_LINEAR_SWAP",
            clear_error=True,
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
            "preview_ready": self._runtime.preview_ready.is_set(),
            "trading_ready": self._runtime.trading_ready.is_set(),
            "protection_ready": self._runtime.protection_ready.is_set(),
            "protected_submit_ready": protected_submit_ready,
            "protection_mode": self.PROTECTION_MODE if protected_submit_ready else "",
            "protective_stop_type": "STOP_MARKET",
            "take_profit_type": "MARKET_IF_TOUCHED" if protected_submit_ready else "",
            "trigger_type": "LAST_PRICE",
            "submit_enabled": policy.allow_order_submit,
            "warning": (
                "Protected execution uses a Nautilus bracket translated by OKX adapter 1.231 "
                "to venue-native attached TP/SL. Submission remains policy-gated."
                if protected_submit_ready
                else "take_profit is missing; protected submit requires both SL and TP and will refuse execution."
            ),
        }

    def _build_protected_bracket(
        self,
        record: IntentRecord,
        instrument: CryptoPerpetual,
        quantity: Any,
    ) -> tuple[Any, str, str]:
        request = record.request
        if request.take_profit is None:
            raise ValueError("Protected submit requires take_profit; refusing SL-only/unprotected entry")

        parent_client_id = ClientOrderId(record.client_order_id)
        stop_client_id = ClientOrderId(self._child_client_order_id(record.client_order_id, "STOP_LOSS"))
        tp_client_id = ClientOrderId(self._child_client_order_id(record.client_order_id, "TAKE_PROFIT"))
        order_side = OrderSide.BUY if request.side == Side.BUY else OrderSide.SELL

        kwargs: dict[str, Any] = {
            "instrument_id": instrument.id,
            "order_side": order_side,
            "quantity": quantity,
            "entry_client_order_id": parent_client_id,
            "tp_order_type": OrderType.MARKET_IF_TOUCHED,
            "tp_trigger_price": instrument.make_price(str(request.take_profit)),
            "tp_trigger_type": TriggerType.LAST_PRICE,
            "tp_post_only": False,
            "tp_client_order_id": tp_client_id,
            "sl_order_type": OrderType.STOP_MARKET,
            "sl_trigger_price": instrument.make_price(str(request.stop_loss)),
            "sl_trigger_type": TriggerType.LAST_PRICE,
            "sl_client_order_id": stop_client_id,
            "entry_tags": ["AI_INTENT", record.intent_id, "ENTRY"],
            "tp_tags": ["AI_INTENT", record.intent_id, "TAKE_PROFIT"],
            "sl_tags": ["AI_INTENT", record.intent_id, "STOP_LOSS"],
        }
        if request.entry_type == EntryType.LIMIT:
            kwargs.update(
                entry_order_type=OrderType.LIMIT,
                entry_price=instrument.make_price(str(request.entry_price)),
                time_in_force=TimeInForce.GTC,
                entry_post_only=False,
            )
        elif request.entry_type == EntryType.MARKET:
            kwargs.update(
                entry_order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
            )
        else:
            raise ValueError(f"Unsupported entry type: {request.entry_type}")

        bracket = self.order_factory.bracket(**kwargs)
        return bracket, str(stop_client_id), str(tp_client_id)

    def _submit_intent(self, record: IntentRecord) -> None:
        if record.status != IntentStatus.QUEUED:
            raise ValueError(f"Expected QUEUED before execution, got {record.status}")
        if not self._runtime.policy.allow_order_submit:
            raise PermissionError("ALLOW_ORDER_SUBMIT=false")
        if self._runtime.policy.allow_unprotected_entry:
            raise PermissionError(
                "ALLOW_UNPROTECTED_ENTRY must remain false; this bridge permits protected brackets only"
            )
        if record.request.take_profit is None:
            raise ValueError("Protected submit requires take_profit; refusing execution")
        self._require_trading_ready()

        sizing = self._size_intent(record)
        request = record.request
        instrument_id = InstrumentId.from_str(request.instrument_id)
        instrument = self.cache.instrument(instrument_id)
        if instrument is None:
            raise RuntimeError(f"Instrument disappeared from cache: {instrument_id}")
        if not isinstance(instrument, CryptoPerpetual) or bool(instrument.is_inverse):
            raise RuntimeError("Protected submit supports linear CryptoPerpetual SWAP only")

        quantity = instrument.make_qty(sizing["quantity"])
        bracket, stop_order_id, tp_order_id = self._build_protected_bracket(
            record,
            instrument,
            quantity,
        )

        self._register_order_binding(record.intent_id, record.client_order_id, "ENTRY")
        self._register_order_binding(record.intent_id, stop_order_id, "STOP_LOSS")
        self._register_order_binding(record.intent_id, tp_order_id, "TAKE_PROFIT")
        self._runtime.store.update_execution(
            record.intent_id,
            nautilus_order_id=record.client_order_id,
            stop_loss_order_id=stop_order_id,
            take_profit_order_id=tp_order_id,
            protection_mode=self.PROTECTION_MODE,
            protection_status="ATTACHED_SUBMIT_PENDING",
            protection_verified=False,
            clear_protection_error=True,
            last_event="PROTECTED_BRACKET_BUILT",
        )

        # Recheck immediately before the single Nautilus submit call. In OKX adapter
        # 1.231 this representable bracket is translated to one parent order with
        # venue-native attachAlgoOrds, eliminating the post-fill unprotected gap.
        self._require_trading_ready()
        self.submit_order_list(bracket)
        self._runtime.store.update_execution(
            record.intent_id,
            status=IntentStatus.SUBMITTED,
            protection_status="ATTACHED_SUBMITTED",
            last_event="PROTECTED_BRACKET_SUBMITTED_TO_NAUTILUS",
            mark_submitted=True,
        )

    def _invalidate_if_open_position(self, intent_id: str, reason: str) -> None:
        try:
            record = self._runtime.store.get(intent_id)
            instrument_id = InstrumentId.from_str(record.request.instrument_id)
            if self._has_open_position(instrument_id):
                self._runtime.invalidate_protection(reason)
        except Exception as exc:
            self._runtime.invalidate_protection(f"protection_state_check_failed:{exc}")

    def on_order_accepted(self, event: OrderAccepted) -> None:
        intent_id, role = self._binding_for_event(event)
        if not intent_id:
            return
        if role == "ENTRY":
            self._runtime.store.update_execution(
                intent_id,
                status=IntentStatus.ACCEPTED,
                last_event="EntryOrderAccepted",
            )
        elif role in {"STOP_LOSS", "TAKE_PROFIT"}:
            self._runtime.store.update_execution(
                intent_id,
                protection_status="ATTACHED_ACTIVE",
                protection_verified=True if role == "STOP_LOSS" else None,
                clear_protection_error=True,
                last_event=f"{role}_ACCEPTED",
            )

    def on_order_rejected(self, event: OrderRejected) -> None:
        intent_id, role = self._binding_for_event(event)
        if not intent_id:
            return
        if role == "ENTRY":
            self._runtime.store.update_execution(
                intent_id,
                status=IntentStatus.REJECTED,
                protection_status="ATTACHED_REJECTED",
                protection_verified=False,
                last_event="EntryOrderRejected",
                error=str(event),
                protection_error=str(event),
            )
        else:
            self._runtime.store.update_execution(
                intent_id,
                protection_status="PROTECTION_REJECTED",
                protection_verified=False if role == "STOP_LOSS" else None,
                protection_error=str(event),
                last_event=f"{role or 'CHILD'}_REJECTED",
            )
            self._invalidate_if_open_position(
                intent_id,
                f"protective_order_rejected:{role or 'unknown'}",
            )

    def on_order_denied(self, event: OrderDenied) -> None:
        intent_id, role = self._binding_for_event(event)
        if not intent_id:
            return
        if role == "ENTRY":
            self._runtime.store.update_execution(
                intent_id,
                status=IntentStatus.REJECTED,
                protection_status="ATTACHED_DENIED",
                protection_verified=False,
                last_event="EntryOrderDenied",
                error=str(event),
                protection_error=str(event),
            )
        else:
            self._runtime.store.update_execution(
                intent_id,
                protection_status="PROTECTION_DENIED",
                protection_verified=False if role == "STOP_LOSS" else None,
                protection_error=str(event),
                last_event=f"{role or 'CHILD'}_DENIED",
            )
            self._invalidate_if_open_position(
                intent_id,
                f"protective_order_denied:{role or 'unknown'}",
            )

    def on_order_canceled(self, event: OrderCanceled) -> None:
        intent_id, role = self._binding_for_event(event)
        if not intent_id:
            return
        if role == "ENTRY":
            self._runtime.store.update_execution(
                intent_id,
                status=IntentStatus.CANCELED,
                last_event="EntryOrderCanceled",
            )
        else:
            self._runtime.store.update_execution(
                intent_id,
                protection_status=f"{role or 'CHILD'}_CANCELED",
                protection_verified=False if role == "STOP_LOSS" else None,
                last_event=f"{role or 'CHILD'}_CANCELED",
            )
            # Do not latch here: an OCO sibling is expected to cancel when the other
            # protective leg executes. The periodic position/stop scan decides safety.

    def on_order_triggered(self, event: OrderTriggered) -> None:
        intent_id, role = self._binding_for_event(event)
        if intent_id and role in {"STOP_LOSS", "TAKE_PROFIT"}:
            self._runtime.store.update_execution(
                intent_id,
                protection_status=f"{role}_TRIGGERED",
                last_event=f"{role}_TRIGGERED",
            )

    def on_order_filled(self, event: OrderFilled) -> None:
        intent_id, role = self._binding_for_event(event)
        if intent_id is None:
            return
        if role == "ENTRY":
            order = self.cache.order(event.client_order_id)
            status = (
                IntentStatus.FILLED
                if order is not None and order.is_closed()
                else IntentStatus.PARTIALLY_FILLED
            )
            self._runtime.store.update_execution(
                intent_id,
                status=status,
                last_event="EntryOrderFilled",
            )
            # Attached protection should be visible in the cache immediately after
            # the parent fill/reconciliation. The periodic scan closes trading if not.
            self._refresh_protection_gate()
        elif role in {"STOP_LOSS", "TAKE_PROFIT"}:
            self._runtime.store.update_execution(
                intent_id,
                protection_status=f"{role}_FILLED",
                protection_verified=False if role == "STOP_LOSS" else None,
                last_event=f"{role}_FILLED",
            )
            self._refresh_protection_gate()
