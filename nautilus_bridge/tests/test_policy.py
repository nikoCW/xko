from decimal import Decimal

import pytest

from bridge_runtime import RiskPolicy, parse_instrument_decimal_map


def test_parse_instrument_decimal_map() -> None:
    limits = parse_instrument_decimal_map(
        "BTC-USDT-SWAP.OKX:10, hype-usdt-swap.okx:100",
        env_name="MAX_ORDER_QTY_BY_INSTRUMENT",
    )
    assert limits == {
        "BTC-USDT-SWAP.OKX": Decimal("10"),
        "HYPE-USDT-SWAP.OKX": Decimal("100"),
    }


def test_risk_policy_uses_instrument_override_and_global_fallback(monkeypatch) -> None:
    monkeypatch.setenv(
        "ALLOWED_INSTRUMENTS",
        "BTC-USDT-SWAP.OKX,HYPE-USDT-SWAP.OKX",
    )
    monkeypatch.setenv("MAX_ORDER_QTY", "10")
    monkeypatch.setenv(
        "MAX_ORDER_QTY_BY_INSTRUMENT",
        "HYPE-USDT-SWAP.OKX:100",
    )

    policy = RiskPolicy.from_env()

    assert policy.max_order_qty_for("HYPE-USDT-SWAP.OKX") == Decimal("100")
    assert policy.max_order_qty_source_for("HYPE-USDT-SWAP.OKX") == "MAX_ORDER_QTY_BY_INSTRUMENT"
    assert policy.max_order_qty_for("BTC-USDT-SWAP.OKX") == Decimal("10")
    assert policy.max_order_qty_source_for("BTC-USDT-SWAP.OKX") == "MAX_ORDER_QTY"
    assert policy.max_order_qty_snapshot() == {"HYPE-USDT-SWAP.OKX": "100"}


def test_invalid_instrument_quantity_limit_fails_closed() -> None:
    with pytest.raises(ValueError):
        parse_instrument_decimal_map(
            "HYPE-USDT-SWAP.OKX:0",
            env_name="MAX_ORDER_QTY_BY_INSTRUMENT",
        )

    with pytest.raises(ValueError):
        parse_instrument_decimal_map(
            "HYPE-USDT-SWAP:100",
            env_name="MAX_ORDER_QTY_BY_INSTRUMENT",
        )
