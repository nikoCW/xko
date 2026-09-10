import time
from decimal import Decimal

import pytest

from rules import RuleViolation, breakout_signal, dec, grid_plan, quote_price, size_trade


def spec(kind="SPOT"):
    return {"instId": "BTC-USDT" if kind == "SPOT" else "BTC-USDT-SWAP", "instType": kind,
            "state": "live", "quoteCcy": "USDT", "settleCcy": "USDT", "ctType": "linear",
            "ctValCcy": "BTC", "ctVal": "0.01", "ctMult": "1", "tickSz": "0.1",
            "lotSz": "0.01" if kind == "SPOT" else "1", "minSz": "0.01" if kind == "SPOT" else "1",
            "listTime": "1500000000000", "instFamily": "BTC-USDT"}


def bars(seconds, n=22):
    start = int(time.time()) // seconds * seconds - n * seconds
    return [{"ts": (start + i * seconds) * 1000, "open": "100", "high": "103", "low": "99", "close": "100", "volume": "100", "confirmed": True} for i in range(n)]


def signal_bars():
    m = bars(900)
    for c in m[:-2]:
        c["high"] = "101"
    m[-2].update(close="102", volume="200")
    m[-1].update(open="102", low="100.9", close="101.5")
    h1, h4 = bars(3600), bars(14400)
    h4[-1]["close"] = "101"
    return m, h1, h4


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", None, "junk"])
def test_reject_nonfinite(value):
    with pytest.raises(RuleViolation):
        dec(value)


def test_closed_breakout_then_retest():
    m, h1, h4 = signal_bars()
    assert breakout_signal(m, h1, h4, "long")["eligible"]
    m[-1]["confirmed"] = False
    with pytest.raises(RuleViolation):
        breakout_signal(m, h1, h4, "long")


def test_no_future_or_gapped_candles():
    m, h1, h4 = signal_bars()
    m[2]["ts"] += 1
    with pytest.raises(RuleViolation, match="缺口"):
        breakout_signal(m, h1, h4, "long")


def test_volume_and_no_chase():
    m, h1, h4 = signal_bars()
    m[-2]["volume"] = "100"
    assert not breakout_signal(m, h1, h4, "long")["eligible"]
    m[-2]["volume"] = "200"
    h1[-1].update(high="110", close="105")
    assert not breakout_signal(m, h1, h4, "long")["eligible"]


@pytest.mark.parametrize("kind", ["SPOT", "SWAP"])
def test_size_respects_loss_cash_lots_and_contract_value(kind):
    result = size_trade(spec(kind), "10000", "10000", "102", "100", "110", "long")
    assert dec(result["estimatedStopLossUSDT"]) <= 50
    assert dec(result["notionalUSDT"]) <= 2000
    assert dec(result["cashAfterUSDT"]) >= 3000
    assert dec(result["quantity"]) % dec(spec(kind)["lotSz"]) == 0
    multiplier = 1 if kind == "SPOT" else Decimal("0.01")
    assert dec(result["baseQuantity"]) == dec(result["quantity"]) * multiplier


@pytest.mark.parametrize("changes", [{"stop": "103"}, {"target": "105"}, {"entry": "102.01"}, {"available": "3000"}, {"leverage": 4}, {"requested_size": "99999"}])
def test_reject_invalid_risk(changes):
    args = dict(spec=spec(), equity="10000", available="10000", entry="102", stop="100", target="110", direction="long")
    args.update(changes)
    with pytest.raises(RuleViolation):
        size_trade(**args)


def test_short_contract_target_and_units():
    result = size_trade(spec("SWAP"), "10000", "10000", "100", "102", "90", "short", leverage=3)
    assert dec(result["netRewardRisk"]) >= 2
    with pytest.raises(RuleViolation):
        size_trade(spec(), "10000", "10000", "100", "102", "90", "short")


def test_quote_staleness_and_spread():
    t = {"bidPx": "100", "askPx": "100.1", "ts": str(int(time.time() * 1000))}
    assert quote_price(t, "buy") == Decimal("100.1")
    t["ts"] = "1"
    with pytest.raises(RuleViolation):
        quote_price(t, "buy")
    t.update(ts=str(int(time.time() * 1000)), askPx="105")
    with pytest.raises(RuleViolation):
        quote_price(t, "buy")
    assert quote_price(t, "sell", opening=False) == 100


def test_grid_cost_and_worst_inventory_loss():
    h4 = bars(14400, 20)
    for c in h4:
        c.update(low="90", high="110")
    args = dict(spec=spec(), equity="10000", available="10000", lower="95", upper="105", stop="90", budget="500", grids=5, current="100", fee_rate="0.001", h4=h4)
    p = grid_plan(**args)
    assert p["mode"] == "PLAN_ONLY"
    assert len(p["levels"]) == 6
    assert dec(p["estimatedWorstInventoryLossUSDT"]) <= 100
    with pytest.raises(RuleViolation):
        grid_plan(**{**args, "grids": 50})
    with pytest.raises(RuleViolation):
        grid_plan(**{**args, "budget": "1000", "stop": "50"})
