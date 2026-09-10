from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from intent_store import IntentStore


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class CommandKind(StrEnum):
    PREVIEW = "PREVIEW"
    SUBMIT = "SUBMIT"


@dataclass(slots=True)
class BridgeCommand:
    kind: CommandKind
    intent_id: str
    reply: queue.Queue | None = None


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    allowed_instruments: frozenset[str]
    equity_currency: str
    max_risk_pct: Decimal
    max_order_qty: Decimal
    max_notional_per_order_usdt: Decimal
    allow_market_entry: bool
    allow_order_submit: bool
    allow_unprotected_entry: bool

    @classmethod
    def from_env(cls) -> "RiskPolicy":
        allowed = frozenset(
            x.strip().upper()
            for x in os.getenv(
                "ALLOWED_INSTRUMENTS",
                "BTC-USDT-SWAP.OKX,ETH-USDT-SWAP.OKX,SOL-USDT-SWAP.OKX",
            ).split(",")
            if x.strip()
        )
        return cls(
            allowed_instruments=allowed,
            equity_currency=os.getenv("EQUITY_CURRENCY", "USDT").strip().upper(),
            max_risk_pct=Decimal(os.getenv("MAX_RISK_PCT", "0.50")),
            max_order_qty=Decimal(os.getenv("MAX_ORDER_QTY", "10")),
            max_notional_per_order_usdt=Decimal(
                os.getenv("MAX_NOTIONAL_PER_ORDER_USDT", "5000")
            ),
            allow_market_entry=env_bool("ALLOW_MARKET_ENTRY", False),
            allow_order_submit=env_bool("ALLOW_ORDER_SUBMIT", False),
            allow_unprotected_entry=env_bool("ALLOW_UNPROTECTED_ENTRY", False),
        )


ReadinessProbe = Callable[[], tuple[bool, bool, bool]]


class BridgeRuntime:
    """Cross-thread bridge state with fail-closed operational readiness gates.

    The readiness probe returns:
        (portfolio_ready, execution_connected, data_connected)

    Startup reconciliation is confirmed when the strategy starts because NautilusTrader
    starts Trader/Strategy only after client connection, execution reconciliation and
    portfolio initialization have completed successfully. If connectivity/portfolio
    readiness is later lost, reconciliation is invalidated and cannot automatically
    re-arm; a full process restart is required to run startup reconciliation again.
    """

    def __init__(self, store: IntentStore, policy: RiskPolicy, okx_environment: str) -> None:
        self.store = store
        self.policy = policy
        self.okx_environment = okx_environment
        self.commands: queue.Queue[BridgeCommand] = queue.Queue(maxsize=1000)

        self.strategy_ready = threading.Event()
        self.portfolio_ready = threading.Event()
        self.reconciliation_ready = threading.Event()
        self.execution_connected = threading.Event()
        self.data_connected = threading.Event()
        self.preview_ready = threading.Event()
        self.trading_ready = threading.Event()

        # Backward-compatible alias. `ready` now means operationally trading-ready,
        # not merely "strategy callback has started". It does not enable submission.
        self.ready = self.trading_ready

        self._readiness_probe: ReadinessProbe | None = None
        self._readiness_lock = threading.RLock()
        self._reconciliation_invalidated = False
        self._last_readiness_error: str | None = None

    @staticmethod
    def _assign(event: threading.Event, value: bool) -> None:
        if value:
            event.set()
        else:
            event.clear()

    def install_readiness_probe(self, probe: ReadinessProbe) -> None:
        if not callable(probe):
            raise TypeError("readiness probe must be callable")
        with self._readiness_lock:
            self._readiness_probe = probe
            self._last_readiness_error = None

    def mark_strategy_started_after_reconciliation(self) -> None:
        """Confirm startup lifecycle reached Strategy.on_start()."""
        with self._readiness_lock:
            self.strategy_ready.set()
            if not self._reconciliation_invalidated:
                self.reconciliation_ready.set()
        self.refresh_readiness()

    def mark_strategy_stopped(self) -> None:
        """Fail closed on strategy shutdown; restart is required to re-confirm reconciliation."""
        with self._readiness_lock:
            self.strategy_ready.clear()
            self.portfolio_ready.clear()
            self.execution_connected.clear()
            self.data_connected.clear()
            self.preview_ready.clear()
            self.trading_ready.clear()
            self.reconciliation_ready.clear()
            self._reconciliation_invalidated = True

    def refresh_readiness(self) -> None:
        """Refresh operational gates from Nautilus read-only state.

        This is intended to run on the Nautilus strategy/event thread. Any loss of
        portfolio/client readiness after the strategy has started invalidates the
        reconciliation gate until a full process restart.
        """
        with self._readiness_lock:
            probe = self._readiness_probe

        if probe is None:
            portfolio_ok = False
            execution_ok = False
            data_ok = False
            probe_error = "readiness_probe_not_installed"
        else:
            try:
                portfolio_ok, execution_ok, data_ok = probe()
                portfolio_ok = bool(portfolio_ok)
                execution_ok = bool(execution_ok)
                data_ok = bool(data_ok)
                probe_error = None
            except Exception as exc:
                portfolio_ok = False
                execution_ok = False
                data_ok = False
                probe_error = f"{type(exc).__name__}: {exc}"

        with self._readiness_lock:
            self._assign(self.portfolio_ready, portfolio_ok)
            self._assign(self.execution_connected, execution_ok)
            self._assign(self.data_connected, data_ok)
            self._last_readiness_error = probe_error

            strategy_ok = self.strategy_ready.is_set()
            operational_ok = strategy_ok and portfolio_ok and execution_ok and data_ok

            # Do not invalidate reconciliation while the service is merely starting.
            # Once Strategy.on_start has been reached, any operational loss is latched
            # fail-closed so reconnect alone can never silently re-enable trading.
            if strategy_ok and not operational_ok:
                self.reconciliation_ready.clear()
                self._reconciliation_invalidated = True

            self._assign(self.preview_ready, operational_ok)
            self._assign(
                self.trading_ready,
                operational_ok and self.reconciliation_ready.is_set(),
            )

    def readiness_reason(self) -> str | None:
        with self._readiness_lock:
            if self._last_readiness_error:
                return f"readiness_probe_error:{self._last_readiness_error}"
            if not self.strategy_ready.is_set():
                return "strategy_not_ready"
            if not self.portfolio_ready.is_set():
                return "portfolio_not_ready"
            if not self.execution_connected.is_set():
                return "execution_client_disconnected"
            if not self.data_connected.is_set():
                return "data_client_disconnected"
            if not self.reconciliation_ready.is_set():
                if self._reconciliation_invalidated:
                    return "reconciliation_invalidated_restart_required"
                return "reconciliation_not_confirmed"
            return None

    def readiness_snapshot(self) -> dict[str, bool | str | None]:
        with self._readiness_lock:
            return {
                "ready": self.trading_ready.is_set(),
                "strategy_ready": self.strategy_ready.is_set(),
                "portfolio_ready": self.portfolio_ready.is_set(),
                "reconciliation_ready": self.reconciliation_ready.is_set(),
                "execution_connected": self.execution_connected.is_set(),
                "data_connected": self.data_connected.is_set(),
                "preview_ready": self.preview_ready.is_set(),
                "trading_ready": self.trading_ready.is_set(),
                "reconciliation_invalidated": self._reconciliation_invalidated,
                "readiness_reason": self.readiness_reason(),
            }
