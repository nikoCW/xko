from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_models import EntryType, Side, TradeIntentCreate


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
