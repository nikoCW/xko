from __future__ import annotations

import os
import queue
import threading
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


class BridgeRuntime:
    def __init__(self, store: IntentStore, policy: RiskPolicy, okx_environment: str) -> None:
        self.store = store
        self.policy = policy
        self.okx_environment = okx_environment
        self.commands: queue.Queue[BridgeCommand] = queue.Queue(maxsize=1000)
        self.ready = threading.Event()
