from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_models import EntryType, IntentRecord, IntentStatus, Side, TradeIntentCreate


def test_buy_geometry_valid() -> None:
    x = TradeIntentCreate(
        instrument_id="btc-usdt-swap.okx",
        side=Side.BUY,
        entry_type=EntryType.LIMIT,
        risk_pct=Decimal("0.5"),
        entry_price=Decimal("100000"),
        stop_loss=Decimal("99000"),
        take_profit=Decimal("102000"),
    )
    assert x.instrument_id == "BTC-USDT-SWAP.OKX"


def test_buy_stop_must_be_below_entry() -> None:
    with pytest.raises(ValidationError):
        TradeIntentCreate(
            instrument_id="BTC-USDT-SWAP.OKX",
            side=Side.BUY,
            risk_pct=Decimal("0.5"),
            entry_price=Decimal("100000"),
            stop_loss=Decimal("101000"),
        )


def test_sell_geometry_valid() -> None:
    x = TradeIntentCreate(
        instrument_id="ETH-USDT-SWAP.OKX",
        side=Side.SELL,
        risk_pct=Decimal("0.25"),
        entry_price=Decimal("4000"),
        stop_loss=Decimal("4100"),
        take_profit=Decimal("3800"),
    )
    assert x.side == Side.SELL


def test_okx_suffix_required() -> None:
    with pytest.raises(ValidationError):
        TradeIntentCreate(
            instrument_id="BTC-USDT-SWAP",
            side=Side.BUY,
            risk_pct=Decimal("0.5"),
            entry_price=Decimal("100000"),
            stop_loss=Decimal("99000"),
        )


def test_old_intent_json_gets_safe_protection_defaults() -> None:
    now = datetime.now(timezone.utc)
    old_record = {
        "intent_id": "old-intent",
        "client_order_id": "XKO123456789012345678901234",
        "status": IntentStatus.CREATED.value,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "request": {
            "instrument_id": "BTC-USDT-SWAP.OKX",
            "side": "BUY",
            "entry_type": "LIMIT",
            "risk_pct": "0.5",
            "entry_price": "100000",
            "stop_loss": "99000",
            "take_profit": "102000",
            "reason": "legacy row",
        },
    }

    restored = IntentRecord.model_validate(old_record)
    assert restored.stop_loss_order_id is None
    assert restored.take_profit_order_id is None
    assert restored.protection_mode is None
    assert restored.protection_status is None
    assert restored.protection_verified is False
    assert restored.protection_error is None
