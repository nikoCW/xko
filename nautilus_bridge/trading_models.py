from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EntryType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class IntentStatus(StrEnum):
    CREATED = "CREATED"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


class TradeIntentCreate(BaseModel):
    instrument_id: str = Field(description="Nautilus instrument ID, e.g. BTC-USDT-SWAP.OKX")
    side: Side
    entry_type: EntryType = EntryType.LIMIT
    risk_pct: Decimal = Field(
        gt=Decimal("0"),
        le=Decimal("100"),
        description="Percent of account equity risked to stop, e.g. 0.5 means 0.5%",
    )
    entry_price: Decimal = Field(
        gt=Decimal("0"),
        description="Reference entry price. Required even for MARKET because sizing uses it.",
    )
    stop_loss: Decimal = Field(gt=Decimal("0"))
    take_profit: Decimal | None = Field(default=None, gt=Decimal("0"))
    reason: str = Field(default="", max_length=2000)

    @field_validator("instrument_id")
    @classmethod
    def normalize_instrument(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.endswith(".OKX"):
            raise ValueError("instrument_id must use Nautilus OKX form ending in .OKX")
        return value

    @model_validator(mode="after")
    def validate_price_geometry(self) -> "TradeIntentCreate":
        if self.side == Side.BUY:
            if self.stop_loss >= self.entry_price:
                raise ValueError("BUY stop_loss must be below entry_price")
            if self.take_profit is not None and self.take_profit <= self.entry_price:
                raise ValueError("BUY take_profit must be above entry_price")
        else:
            if self.stop_loss <= self.entry_price:
                raise ValueError("SELL stop_loss must be above entry_price")
            if self.take_profit is not None and self.take_profit >= self.entry_price:
                raise ValueError("SELL take_profit must be below entry_price")
        return self


class IntentRecord(BaseModel):
    intent_id: str
    client_order_id: str
    status: IntentStatus
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None = None
    submitted_at: datetime | None = None
    request: TradeIntentCreate
    sized_quantity: str | None = None
    equity_used: str | None = None
    risk_fraction_used: str | None = None
    nautilus_order_id: str | None = None
    stop_loss_order_id: str | None = None
    take_profit_order_id: str | None = None
    protection_mode: str | None = None
    protection_status: str | None = None
    protection_verified: bool = False
    protection_error: str | None = None
    last_event: str | None = None
    error: str | None = None

    @classmethod
    def now(cls) -> datetime:
        return datetime.now(timezone.utc)


class ApprovalRequest(BaseModel):
    approval_code: str = Field(min_length=4, max_length=32)


class BridgeHealth(BaseModel):
    status: str
    ready: bool
    strategy_ready: bool
    portfolio_ready: bool
    reconciliation_ready: bool
    execution_connected: bool
    data_connected: bool
    protection_ready: bool
    preview_ready: bool
    trading_ready: bool
    reconciliation_invalidated: bool
    protection_invalidated: bool
    readiness_reason: str | None = None
    protection_reason: str | None = None
    okx_environment: str
    order_submit_enabled: bool
    unprotected_entry_enabled: bool
    protected_submit_only: bool
    protective_bracket_mode: str
    protection_scope: str
    target_instrument_position_policy: str
    max_order_qty_default: str
    max_order_qty_by_instrument: dict[str, str]
    allowed_instruments: list[str]


class PreviewResult(BaseModel):
    intent_id: str
    instrument_id: str
    quantity: str
    equity: str
    risk_fraction: str
    submit_enabled: bool
    warning: str | None = None


class CommandResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
