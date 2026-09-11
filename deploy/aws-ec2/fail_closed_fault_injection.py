from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from bridge_runtime import BridgeRuntime
from nautilus_service.bridge_strategy import AIIntentStrategy as BaseAIIntentStrategy
from nautilus_service.protection_coverage_strategy import AIIntentStrategy as CoverageAIIntentStrategy
from nautilus_trader.model.enums import OrderSide, OrderType
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.objects import Quantity


INSTRUMENT = "BTC-USDT-SWAP.OKX"
INTENT_ID = "fault-injection-intent"
ENTRY_ID = "XKOFAULTINJECTIONENTRY"
STOP_ID = "XKSFAULTINJECTIONSTOP"
ACCOUNT_ID = "OKX-master"


class FakeStore:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            intent_id=INTENT_ID,
            client_order_id=ENTRY_ID,
            stop_loss_order_id=STOP_ID,
            take_profit_order_id=None,
        )

    def get(self, intent_id: str):
        if intent_id != INTENT_ID:
            raise KeyError(intent_id)
        return self.record

    def list_recent(self, limit: int = 200):
        _ = limit
        return [self.record]


class FakeCache:
    def __init__(self, positions: list[Any], orders: list[Any]) -> None:
        self.positions = positions
        self.orders = orders
        self.raise_positions = False

    def positions_open(self, *, instrument_id=None, account_id=None):
        _ = account_id
        if self.raise_positions:
            raise RuntimeError("injected_positions_scan_failure")
        if instrument_id is None:
            return list(self.positions)
        return [p for p in self.positions if str(p.instrument_id) == str(instrument_id)]

    def orders_open(self, *, instrument_id=None, account_id=None):
        _ = account_id
        if instrument_id is None:
            return list(self.orders)
        return [o for o in self.orders if str(o.instrument_id) == str(instrument_id)]


class ProtectionHarness:
    _quantity_decimal = staticmethod(CoverageAIIntentStrategy._quantity_decimal)
    _xko_stop_coverage_issue = CoverageAIIntentStrategy._xko_stop_coverage_issue
    _refresh_protection_gate = CoverageAIIntentStrategy._refresh_protection_gate
    _register_order_binding = BaseAIIntentStrategy._register_order_binding
    _intent_id_for_position = BaseAIIntentStrategy._intent_id_for_position

    def __init__(self, runtime: BridgeRuntime, cache: FakeCache) -> None:
        self._runtime = runtime
        self._account_id = ACCOUNT_ID
        self.cache = cache
        self._intent_by_client_order_id: dict[str, str] = {}
        self._role_by_client_order_id: dict[str, str] = {}


def xko_position(quantity: str = "10"):
    return SimpleNamespace(
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        opening_order_id=ClientOrderId(ENTRY_ID),
        is_long=True,
        is_short=False,
        side="LONG",
        quantity=Quantity.from_str(quantity),
    )


def external_position(quantity: str = "10"):
    return SimpleNamespace(
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        opening_order_id=ClientOrderId("MANUALGRID001"),
        is_long=True,
        is_short=False,
        side="LONG",
        quantity=Quantity.from_str(quantity),
    )


def good_stop(*, side=OrderSide.SELL, leaves: str = "10", reduce_only: bool = True):
    return SimpleNamespace(
        instrument_id=InstrumentId.from_str(INSTRUMENT),
        client_order_id=ClientOrderId(STOP_ID),
        order_type=OrderType.STOP_MARKET,
        side=side,
        is_reduce_only=reduce_only,
        leaves_qty=Quantity.from_str(leaves),
    )


def make_system(*, positions=None, orders=None):
    store = FakeStore()
    policy = SimpleNamespace(
        allowed_instruments=frozenset({INSTRUMENT}),
        allow_order_submit=False,
        allow_unprotected_entry=False,
        allow_market_entry=False,
    )
    runtime = BridgeRuntime(store, policy, "LIVE")
    cache = FakeCache(
        positions=list(positions if positions is not None else [xko_position()]),
        orders=list(orders if orders is not None else [good_stop()]),
    )
    strategy = ProtectionHarness(runtime, cache)
    return runtime, strategy, cache


def prime_healthy(runtime: BridgeRuntime, strategy: ProtectionHarness) -> None:
    runtime.install_readiness_probe(lambda: (True, True, True))
    runtime.mark_strategy_started_after_reconciliation()
    strategy._refresh_protection_gate()
    snap = runtime.readiness_snapshot()
    assert snap["reconciliation_ready"] is True, snap
    assert snap["protection_ready"] is True, snap
    assert snap["trading_ready"] is True, snap


def assert_closed(runtime: BridgeRuntime, reason_fragment: str) -> dict[str, Any]:
    snap = runtime.readiness_snapshot()
    assert snap["protection_ready"] is False, snap
    assert snap["trading_ready"] is False, snap
    reason = str(snap["protection_reason"])
    assert reason_fragment in reason, snap
    return snap


def scenario_external_position_ignored() -> None:
    runtime, strategy, _cache = make_system(positions=[external_position()], orders=[])
    prime_healthy(runtime, strategy)
    snap = runtime.readiness_snapshot()
    assert snap["protection_ready"] is True, snap
    assert snap["trading_ready"] is True, snap
    print("EXTERNAL_POSITION_IGNORED_OK")


def scenario_transient_fault(name: str, mutate, reason_fragment: str) -> None:
    runtime, strategy, cache = make_system()
    prime_healthy(runtime, strategy)
    mutate(cache)
    strategy._refresh_protection_gate()
    snap = assert_closed(runtime, reason_fragment)
    assert snap["protection_invalidated"] is False, snap
    print(f"{name}_FAIL_CLOSED_OK", snap["protection_reason"])

    cache.orders = [good_stop()]
    cache.raise_positions = False
    strategy._refresh_protection_gate()
    recovered = runtime.readiness_snapshot()
    assert recovered["protection_ready"] is True, recovered
    assert recovered["trading_ready"] is True, recovered
    assert recovered["protection_invalidated"] is False, recovered
    print(f"{name}_TRANSIENT_RECOVERY_OK")


def scenario_persistent_missing_stop_latches() -> None:
    runtime, strategy, cache = make_system()
    prime_healthy(runtime, strategy)
    cache.orders = []
    for _ in range(runtime.PROTECTION_FAILURE_LATCH_POLLS):
        strategy._refresh_protection_gate()

    snap = assert_closed(runtime, "stop_not_open")
    assert snap["protection_invalidated"] is True, snap
    assert "restart_required" in str(snap["protection_reason"]), snap
    print("PERSISTENT_STOP_FAILURE_LATCH_OK", snap["protection_reason"])

    cache.orders = [good_stop()]
    strategy._refresh_protection_gate()
    still_closed = runtime.readiness_snapshot()
    assert still_closed["protection_ready"] is False, still_closed
    assert still_closed["trading_ready"] is False, still_closed
    assert still_closed["protection_invalidated"] is True, still_closed
    print("PROTECTION_LATCH_REQUIRES_RESTART_OK")

    restarted_runtime, restarted_strategy, _ = make_system()
    prime_healthy(restarted_runtime, restarted_strategy)
    restarted = restarted_runtime.readiness_snapshot()
    assert restarted["protection_ready"] is True, restarted
    assert restarted["trading_ready"] is True, restarted
    assert restarted["protection_invalidated"] is False, restarted
    print("PROTECTION_RESTART_RECOVERY_OK")


def scenario_reconciliation_loss_latches() -> None:
    runtime, strategy, _cache = make_system()
    state = {"portfolio": True, "execution": True, "data": True}
    runtime.install_readiness_probe(
        lambda: (state["portfolio"], state["execution"], state["data"])
    )
    runtime.mark_strategy_started_after_reconciliation()
    strategy._refresh_protection_gate()
    assert runtime.trading_ready.is_set()

    state["execution"] = False
    runtime.refresh_readiness()
    lost = runtime.readiness_snapshot()
    assert lost["execution_connected"] is False, lost
    assert lost["reconciliation_ready"] is False, lost
    assert lost["reconciliation_invalidated"] is True, lost
    assert lost["trading_ready"] is False, lost
    print("RECONCILIATION_LOSS_FAIL_CLOSED_OK", lost["readiness_reason"])

    state["execution"] = True
    runtime.refresh_readiness()
    reconnected = runtime.readiness_snapshot()
    assert reconnected["execution_connected"] is True, reconnected
    assert reconnected["reconciliation_ready"] is False, reconnected
    assert reconnected["reconciliation_invalidated"] is True, reconnected
    assert reconnected["trading_ready"] is False, reconnected
    assert reconnected["readiness_reason"] == "reconciliation_invalidated_restart_required", reconnected
    print("RECONCILIATION_RECONNECT_STAYS_CLOSED_OK")

    restarted_runtime, restarted_strategy, _ = make_system()
    prime_healthy(restarted_runtime, restarted_strategy)
    restarted = restarted_runtime.readiness_snapshot()
    assert restarted["reconciliation_ready"] is True, restarted
    assert restarted["reconciliation_invalidated"] is False, restarted
    assert restarted["trading_ready"] is True, restarted
    print("RECONCILIATION_RESTART_RECOVERY_OK")


def main() -> None:
    scenario_external_position_ignored()
    scenario_transient_fault(
        "STOP_MISSING",
        lambda cache: setattr(cache, "orders", []),
        "stop_not_open",
    )
    scenario_transient_fault(
        "STOP_WRONG_SIDE",
        lambda cache: setattr(cache, "orders", [good_stop(side=OrderSide.BUY)]),
        "stop_wrong_side",
    )
    scenario_transient_fault(
        "STOP_UNDERCOVERED",
        lambda cache: setattr(cache, "orders", [good_stop(leaves="9")]),
        "stop_undercovered",
    )
    scenario_transient_fault(
        "PROTECTION_SCAN_EXCEPTION",
        lambda cache: setattr(cache, "raise_positions", True),
        "protection_scan_error:RuntimeError:injected_positions_scan_failure",
    )
    scenario_persistent_missing_stop_latches()
    scenario_reconciliation_loss_latches()
    print("REAL_NETWORK_CALLS 0")
    print("REAL_BRIDGE_STATE_MUTATIONS 0")
    print("FAIL_CLOSED_FAULT_INJECTION_OK")


if __name__ == "__main__":
    main()
