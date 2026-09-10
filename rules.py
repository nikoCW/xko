"""Deterministic rules. Monetary values stay Decimal until JSON serialization."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
import time


class RuleViolation(ValueError):
    pass


def dec(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RuleViolation("缺失或无效的数值") from exc
    if not result.is_finite():
        raise RuleViolation("数值必须有限，不能为 NaN/Infinity")
    return result


def positive(value: Any) -> Decimal:
    result = dec(value)
    if result <= 0:
        raise RuleViolation("价格、数量和资金必须大于零")
    return result


def fmt(value: Decimal) -> str:
    return format(value, "f")


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / positive(step)).to_integral_value(rounding=ROUND_DOWN) * step


def exact_step(value: Any, step: Any, name: str) -> Decimal:
    number, increment = positive(value), positive(step)
    if number % increment:
        raise RuleViolation(f"{name} 必须是 {increment} 的整数倍；请重新预览")
    return number


@dataclass(frozen=True)
class Policy:
    trade_risk: str = "0.005"
    derivative_risk: str = "0.03"
    cash_reserve: str = "0.30"
    max_asset_notional: str = "0.20"
    daily_drawdown: str = "0.03"
    max_leverage: int = 3
    min_reward_risk: str = "2"
    max_spread_bps: str = "20"
    max_quote_age_seconds: int = 15
    preview_ttl_seconds: int = 120
    max_price_drift: str = "0.005"
    cooldown_seconds: int = 900
    fee_rate_floor: str = "0.001"
    slippage_rate: str = "0.001"
    min_quote_volume: str = "5000000"
    min_listing_days: int = 7
    grid_budget_ratio: str = "0.10"
    grid_loss_ratio: str = "0.01"

    def view(self):
        return asdict(self)


POLICY = Policy()


def instrument_kind(spec: dict) -> str:
    kind = spec.get("instType")
    if spec.get("state") != "live":
        raise RuleViolation("交易标的未处于 live 状态")
    if kind == "SPOT" and spec.get("quoteCcy") == "USDT":
        return kind
    if (kind in {"SWAP", "FUTURES"} and spec.get("settleCcy") == "USDT"
            and spec.get("ctType") == "linear"
            and spec.get("ctValCcy") == spec["instId"].split("-")[0]):
        return kind
    raise RuleViolation("只支持 USDT 现货和币本位面值的 USDT 线性合约")


def unit_value(spec: dict) -> Decimal:
    return Decimal(1) if instrument_kind(spec) == "SPOT" else positive(spec["ctVal"]) * positive(spec.get("ctMult") or "1")


def quote_price(ticker: dict, side: str, *, opening=True, now=None) -> Decimal:
    now = time.time() if now is None else now
    age = dec(now) - dec(ticker.get("ts")) / 1000
    if age < -5 or age > POLICY.max_quote_age_seconds:
        raise RuleViolation("报价过期或时钟异常")
    bid, ask = positive(ticker.get("bidPx")), positive(ticker.get("askPx"))
    if ask < bid:
        raise RuleViolation("买卖报价倒挂")
    spread = (ask - bid) / ((ask + bid) / 2) * 10000
    if opening and spread > dec(POLICY.max_spread_bps):
        raise RuleViolation("价差过大，暂停新增风险")
    return ask if side == "buy" else bid


def closed_bars(candles: list[dict], seconds: int, minimum: int, now=None) -> list[dict]:
    now = time.time() if now is None else now
    bars = sorted((c for c in candles if c.get("confirmed") is True), key=lambda c: c["ts"])
    if len(bars) < minimum:
        raise RuleViolation(f"需要至少 {minimum} 根已收盘 K 线")
    bars = bars[-minimum:]
    for c in bars:
        o, h, l, close = (positive(c[k]) for k in ("open", "high", "low", "close"))
        if not l <= min(o, close) <= max(o, close) <= h or dec(c["volume"]) < 0:
            raise RuleViolation("K 线价格或成交量异常")
    if any(b["ts"] - a["ts"] != seconds * 1000 for a, b in zip(bars, bars[1:])):
        raise RuleViolation("K 线存在缺口或重复")
    age = dec(now) - dec(bars[-1]["ts"]) / 1000 - seconds
    if age < 0 or age > seconds + 30:
        raise RuleViolation("已收盘 K 线已过期或时间异常")
    return bars


def breakout_signal(m15: list[dict], h1: list[dict], h4: list[dict], direction: str, now=None) -> dict:
    if direction not in {"long", "short"}:
        raise RuleViolation("方向必须为 long/short")
    bars = closed_bars(m15, 900, 22, now)
    hour = closed_bars(h1, 3600, 2, now)[-1]
    higher = closed_bars(h4, 14400, 20, now)
    history, breakout, retest = bars[:-2], bars[-2], bars[-1]
    level = max(positive(c["high"]) for c in history) if direction == "long" else min(positive(c["low"]) for c in history)
    mean_volume = sum(dec(c["volume"]) for c in history) / len(history)
    volume_ok = mean_volume > 0 and dec(breakout["volume"]) >= mean_volume * Decimal("1.5")
    sma = sum(positive(c["close"]) for c in higher) / len(higher)
    hour_move = positive(hour["close"]) / positive(hour["open"]) - 1
    if direction == "long":
        structure = positive(breakout["close"]) > level and positive(retest["low"]) <= level * Decimal("1.002") and positive(retest["low"]) >= level * Decimal("0.997") and positive(retest["close"]) > level
        trend = positive(higher[-1]["close"]) > sma
        chase = hour_move > Decimal("0.04")
        stop = min(positive(retest["low"]), level) * Decimal("0.998")
    else:
        structure = positive(breakout["close"]) < level and positive(retest["high"]) >= level * Decimal("0.998") and positive(retest["high"]) <= level * Decimal("1.003") and positive(retest["close"]) < level
        trend = positive(higher[-1]["close"]) < sma
        chase = hour_move < Decimal("-0.04")
        stop = max(positive(retest["high"]), level) * Decimal("1.002")
    reasons = [label for ok, label in [(structure, "未完成突破后的独立收盘回踩"), (volume_ok, "突破量不足前20根均量的1.5倍"), (trend, "4小时趋势不支持"), (not chase, "最近收盘1小时涨跌超过4%，禁止追价")] if not ok]
    return {"eligible": not reasons, "direction": direction, "reasons": reasons,
            "level": fmt(level), "structuralStop": fmt(stop), "signalTs": retest["ts"],
            "confirmations": {"closedBreakoutRetest": structure, "volumeExpansion": volume_ok, "higherTimeframe": trend}}


def size_trade(spec: dict, equity: Any, available: Any, entry: Any, stop: Any,
               target: Any, direction: str, leverage: int = 1, fee_rate: Any = "0.001",
               requested_size: Any = None) -> dict:
    kind = instrument_kind(spec)
    equity, available = positive(equity), dec(available)
    entry = exact_step(entry, spec["tickSz"], "委托价")
    stop = exact_step(stop, spec["tickSz"], "止损价")
    target = exact_step(target, spec["tickSz"], "止盈价")
    if direction not in {"long", "short"} or (kind == "SPOT" and direction != "long"):
        raise RuleViolation("现货只允许买入持有；做空需使用线性合约")
    if not (stop < entry < target if direction == "long" else target < entry < stop):
        raise RuleViolation("止损/入场/止盈的方向或顺序错误")
    if not 1 <= leverage <= POLICY.max_leverage or (kind == "SPOT" and leverage != 1):
        raise RuleViolation("现货无杠杆；合约杠杆范围为1至3倍")
    if kind != "SPOT" and abs(entry - stop) / entry >= Decimal("0.5") / leverage:
        raise RuleViolation("合约止损距离过宽，不满足杠杆保证金缓冲；不能依赖强平代替止损")
    fee = max(abs(dec(fee_rate)), dec(POLICY.fee_rate_floor))
    slip = dec(POLICY.slippage_rate)
    loss_unit = abs(entry - stop) + (entry + stop) * (fee + slip)
    reward_unit = abs(target - entry) - (entry + target) * (fee + slip)
    rr = reward_unit / loss_unit
    if rr < dec(POLICY.min_reward_risk):
        raise RuleViolation("扣除估算手续费和滑点后盈亏比不足2:1")
    spendable = available - equity * dec(POLICY.cash_reserve)
    if spendable <= 0:
        raise RuleViolation("可用 USDT 不足以保留30%现金")
    unit = unit_value(spec)
    max_size = min(equity * dec(POLICY.trade_risk) / (loss_unit * unit),
                   equity * dec(POLICY.max_asset_notional) / (entry * unit),
                   spendable / (entry * unit * (Decimal(1) / leverage + fee + slip)))
    quantity = floor_step(max_size, positive(spec["lotSz"]))
    if requested_size is not None:
        requested = exact_step(requested_size, spec["lotSz"], "数量")
        if requested > quantity:
            raise RuleViolation(f"请求数量超过风控上限 {fmt(quantity)}")
        quantity = requested
    if quantity < positive(spec["minSz"]) or quantity <= 0:
        raise RuleViolation("风险预算下的数量低于最小下单量")
    loss, notional = quantity * unit * loss_unit, quantity * unit * entry
    return {"quantity": fmt(quantity), "quantityUnit": "base_currency" if kind == "SPOT" else "contracts",
            "baseQuantity": fmt(quantity * unit), "notionalUSDT": fmt(notional),
            "estimatedStopLossUSDT": fmt(loss), "riskRatio": fmt(loss / equity),
            "netRewardRisk": fmt(rr), "feeRateUsed": fmt(fee), "slippageRateUsed": fmt(slip),
            "cashAfterUSDT": fmt(available - notional / leverage - notional * (fee + slip)),
            "leverage": leverage, "stopLossIsGuaranteed": False}


def grid_plan(spec: dict, equity: Any, available: Any, lower: Any, upper: Any,
              stop: Any, budget: Any, grids: int, current: Any, fee_rate: Any,
              h4: list[dict], now=None) -> dict:
    if instrument_kind(spec) != "SPOT":
        raise RuleViolation("本版本仅规划无杠杆现货网格；合约网格不支持执行")
    if not 3 <= grids <= 50:
        raise RuleViolation("网格数量必须为3至50")
    lower, upper, stop = (exact_step(v, spec["tickSz"], "网格边界") for v in (lower, upper, stop))
    current, equity, budget, available = positive(current), positive(equity), positive(budget), positive(available)
    if not 0 < stop < lower < current < upper:
        raise RuleViolation("需满足 止损 < 下界 < 当前价 < 上界")
    bars = closed_bars(h4, 14400, 20, now)
    highs, lows = max(positive(c["high"]) for c in bars), min(positive(c["low"]) for c in bars)
    width = highs - lows
    if width <= 0 or abs(positive(bars[-1]["close"]) - positive(bars[0]["close"])) > width * Decimal("0.35"):
        raise RuleViolation("4小时走势偏单边，不符合震荡网格条件")
    if lower < lows or upper > highs:
        raise RuleViolation("网格边界应位于最近20根4小时K线支持的区间内")
    costs = max(abs(dec(fee_rate)), dec(POLICY.fee_rate_floor)) + dec(POLICY.slippage_rate)
    if budget > equity * dec(POLICY.grid_budget_ratio) or available - budget * (1 + costs) < equity * dec(POLICY.cash_reserve):
        raise RuleViolation("网格预算超过净值10%或现金储备不足30%")
    # Conservative inventory scenario: entire budget bought at upper boundary.
    worst_loss = budget * ((upper - stop) / upper + 2 * costs)
    if worst_loss > equity * dec(POLICY.grid_loss_ratio):
        raise RuleViolation("网格最坏库存止损估算超过净值1%")
    levels = [floor_step(lower + (upper - lower) * i / grids, positive(spec["tickSz"])) for i in range(grids + 1)]
    min_edge = min((b - a) / b for a, b in zip(levels, levels[1:]))
    if min_edge <= 2 * costs + Decimal("0.001"):
        raise RuleViolation("单格收益不足以覆盖双边费用、滑点和0.1%余量")
    quantities = [floor_step(budget / grids / p, positive(spec["lotSz"])) for p in levels[:-1]]
    if any(q < positive(spec["minSz"]) for q in quantities):
        raise RuleViolation("单格数量低于交易所最小量")
    return {"mode": "PLAN_ONLY", "instId": spec["instId"], "budgetUSDT": fmt(budget),
            "levels": [fmt(p) for p in levels], "quantities": [fmt(q) for q in quantities],
            "stop": fmt(stop), "estimatedWorstInventoryLossUSDT": fmt(worst_loss),
            "rules": ["仅在震荡区间运行；买单成交后才为实得库存建立上邻格卖单", "禁止卖出未持有库存、翻倍补仓、扩大区间或自动追加预算", "跌破止损需退出预览；突破上界停止补单，不能自动追涨重建", "每次新增、撤销、停止网格或卖出库存都需要精确执行确认"],
            "execution": "本版本不启动交易所网格机器人；可创建价格提醒，按预览逐单处理"}
