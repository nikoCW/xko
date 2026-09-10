from __future__ import annotations

import math
import asyncio
import hmac
from contextlib import asynccontextmanager, suppress
import os
from datetime import datetime, timezone
from typing import Any

from okx_client import OKXClient
from rules import POLICY, RuleViolation, dec, grid_plan, instrument_kind
from state import Store
from trading import TradingService, account_values
from monitor import Monitor
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
DEFAULT_QUOTE = os.getenv("OKX_QUOTE", "USDT").upper()

STABLE_BASES = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDS", "USDG", "PYUSD", "EURT"
}

client = OKXClient()
service = TradingService(client, Store())
monitor = Monitor(service)


@asynccontextmanager
async def lifespan(_server):
    task = asyncio.create_task(monitor.run()) if os.getenv("MONITOR_ENABLED", "false").lower() == "true" else None
    try:
        yield {}
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


mcp = MCPServer(
    name="okx-live-trader", title="OKX Live Trader", version="2.0.0",
    description="OKX rules, account review, confirmed FOK orders, spot grid plans and persistent alerts.",
    instructions=("Use get_rules first. Market signals are not execution. Show the entire preview and wait "
                  "for the user's explicit confirmation before execute_preview. Never fabricate confirmation. "
                  "A plan is not an order; acknowledgement is not a fill. Grid planning does not start a bot. "
                  "Unknown order states require reconciliation, never resubmission."),
    lifespan=lifespan,
)


async def okx_get(path: str, params: dict[str, Any] | None = None):
    return await client.get(path, params)


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default

def symbol_to_inst(symbol: str, quote: str = DEFAULT_QUOTE) -> str:
    s = symbol.strip().upper()
    if "-" in s:
        return s
    return f"{s}-{quote.upper()}"

def ticker_view(row: dict[str, Any]) -> dict[str, Any]:
    last = fnum(row.get("last"))
    open24h = fnum(row.get("open24h"))
    change_pct = ((last / open24h) - 1.0) * 100.0 if open24h > 0 else None
    inst = str(row.get("instId", ""))
    base = inst.split("-")[0] if "-" in inst else inst
    return {
        "symbol": base,
        "instId": inst,
        "last": last,
        "change24hPct": round(change_pct, 4) if change_pct is not None else None,
        "high24h": fnum(row.get("high24h")),
        "low24h": fnum(row.get("low24h")),
        "quoteVolume24h": fnum(row.get("volCcy24h")),
        "baseVolume24h": fnum(row.get("vol24h")),
        "bid": fnum(row.get("bidPx")),
        "ask": fnum(row.get("askPx")),
        "ts": row.get("ts"),
    }

async def all_spot_tickers(quote: str = DEFAULT_QUOTE) -> list[dict[str, Any]]:
    rows = await okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
    suffix = f"-{quote.upper()}"
    parsed = []
    for row in rows:
        if not str(row.get("instId", "")).endswith(suffix):
            continue
        item = ticker_view(row)
        if item["last"] <= 0 or item["change24hPct"] is None:
            continue
        parsed.append(item)
    return parsed

async def candles_for(inst_id: str, bar: str = "1D", limit: int = 90) -> list[dict[str, Any]]:
    limit = max(2, min(int(limit), 300))
    rows = await okx_get(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": bar, "limit": str(limit)},
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 9:
            continue
        out.append({
            "ts": int(row[0]),
            "time": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat(),
            "open": fnum(row[1]),
            "high": fnum(row[2]),
            "low": fnum(row[3]),
            "close": fnum(row[4]),
            "volume": fnum(row[5]),
            "quoteVolume": fnum(row[7]),
            "confirmed": str(row[8]) == "1",
        })
    out.sort(key=lambda x: x["ts"])
    return out

def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    completed = [c for c in candles if c["confirmed"]]
    if len(completed) < period + 1:
        return None
    trs = []
    for prev, cur in zip(completed[:-1], completed[1:]):
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

def candle_summary(candles: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [c for c in candles if c["confirmed"]]
    recent20 = completed[-20:] if len(completed) >= 20 else completed
    recent50 = completed[-50:] if len(completed) >= 50 else completed
    a14 = atr(candles, 14)

    def avg_close(items):
        return sum(c["close"] for c in items) / len(items) if items else None

    return {
        "bars": len(candles),
        "completedBars": len(completed),
        "high20": max((c["high"] for c in recent20), default=None),
        "low20": min((c["low"] for c in recent20), default=None),
        "sma20": avg_close(recent20),
        "sma50": avg_close(recent50),
        "atr14": a14,
        "lastCompletedClose": completed[-1]["close"] if completed else None,
    }

@mcp.tool(
    name="market_scan",
    description=(
        "Scan OKX spot markets for top 24h gainers and losers after a quote-volume "
        "filter. Use this for market breadth and candidate discovery."
    ),
)
async def market_scan(
    quote: str = "USDT",
    top_n: int = 10,
    min_quote_volume: float = 5_000_000,
) -> dict[str, Any]:
    quote = quote.upper()
    items = await eligible_markets(quote, min_quote_volume)
    filtered = [
        x for x in items
        if x["quoteVolume24h"] >= min_quote_volume and x["symbol"] not in STABLE_BASES
    ]
    filtered.sort(key=lambda x: x["change24hPct"])
    n = max(1, min(int(top_n), 50))
    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "quote": quote,
        "minQuoteVolume": min_quote_volume,
        "eligibleCount": len(filtered),
        "topGainers": list(reversed(filtered[-n:])),
        "topLosers": filtered[:n],
        "note": "Read-only OKX public market data; no orders are sent.",
    }

@mcp.tool(
    name="compare_symbols",
    description=(
        "Compare multiple crypto symbols on OKX using current price, 24h change, "
        "24h high/low and quote volume. Symbols may be BTC or BTC-USDT."
    ),
)
async def compare_symbols(
    symbols: list[str],
    quote: str = "USDT",
) -> dict[str, Any]:
    quote = quote.upper()
    lookup = {x["instId"]: x for x in await all_spot_tickers(quote)}
    rows = []
    missing = []
    for symbol in symbols[:20]:
        inst = symbol_to_inst(symbol, quote)
        if inst in lookup:
            rows.append(lookup[inst])
        else:
            missing.append(inst)
    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "items": rows,
        "missing": missing,
    }

@mcp.tool(
    name="get_candles",
    description=(
        "Retrieve recent OKX candlesticks for one spot symbol. Returns chronological "
        "OHLCV bars plus a compact 20/50-bar trend and ATR summary."
    ),
)
async def get_candles(
    symbol: str,
    quote: str = "USDT",
    bar: str = "1D",
    limit: int = 90,
) -> dict[str, Any]:
    inst = symbol_to_inst(symbol, quote)
    candles = await candles_for(inst, bar, limit)
    return {
        "instId": inst,
        "bar": bar,
        "summary": candle_summary(candles),
        "candles": candles,
    }

async def eligible_markets(quote="USDT", min_quote_volume=5_000_000):
    if quote.upper() != "USDT":
        raise RuleViolation("规则执行只支持USDT报价")
    threshold = max(dec(min_quote_volume), dec(POLICY.min_quote_volume))
    specs = {r["instId"]: r for r in await client.get("/api/v5/public/instruments", {"instType": "SPOT"})}
    rows = []
    for item in await all_spot_tickers(quote):
        spec = specs.get(item["instId"], {})
        if spec.get("state") != "live" or item["symbol"] in STABLE_BASES:
            continue
        if fnum(spec.get("listTime")) <= 0 or datetime.now(timezone.utc).timestamp() - fnum(spec["listTime"]) / 1000 < 7 * 86400:
            continue
        if dec(item["quoteVolume24h"]) < threshold or item["bid"] <= 0 or item["ask"] < item["bid"]:
            continue
        spread = (item["ask"] - item["bid"]) / ((item["ask"] + item["bid"]) / 2) * 10000
        if spread > 20:
            continue
        age = datetime.now(timezone.utc).timestamp() - fnum(item["ts"]) / 1000
        if not -5 <= age <= 15:
            continue
        item["spreadBps"] = spread
        rows.append(item)
    # Liquidity first; a leaderboard is discovery, never a buy signal.
    return sorted(rows, key=lambda x: (-x["quoteVolume24h"], x["spreadBps"]))


@mcp.tool()
async def run_live_trader(quote: str = "USDT", min_quote_volume: float = 5_000_000,
                          scan_top_n: int = 10, lookback_days: int = 90) -> dict[str, Any]:
    """Scan liquid markets and check closed breakout/retest signals. Never sends orders."""
    rows = await eligible_markets(quote, min_quote_volume)
    candidates = rows[:max(1, min(scan_top_n, 10))]
    signals = []
    for row in candidates:
        try:
            signal = await service.signal(row["instId"], "long")
            signals.append({"instId": row["instId"], **signal})
        except Exception:
            signals.append({"instId": row["instId"], "eligible": False, "reasons": ["行情不足或暂不可用"]})
    return {"asOf": datetime.now(timezone.utc).isoformat(), "mode": "ANALYSIS_ONLY",
            "marketCandidates": candidates, "signals": signals,
            "decision": "REVIEW" if any(s["eligible"] for s in signals) else "WAIT",
            "note": "候选通过信号仍需账户风控、止损目标和执行预览。lookback_days保留兼容；新规则固定15m/1H/4H。"}


@mcp.tool()
async def get_rules() -> dict[str, Any]:
    """Read executable policy limits, modes, supported order types and grid boundaries."""
    return {"version": "2.0.0", "policy": POLICY.view(), "mode": client.mode,
            "execution": "fresh_confirmation_only", "orders": ["spot buy/sell FOK", "isolated linear swap/futures long/short FOK", "ordinary/algo cancellation"],
            "newExposure": "Only with no existing derivative positions, ordinary/algo orders or allocated bot funds; no same-asset inventory additions.",
            "signal": "Closed 15m breakout + later 15m retest; breakout volume >=1.5x prior20; 4H trend; no >4% 1H chase",
            "grid": "Unleveraged spot planning and alerts only; no native grid bot endpoint or automatic replenishment",
            "monitorEnabled": os.getenv("MONITOR_ENABLED", "false").lower() == "true",
            "monitorLastSuccess": service.store.get("monitor_last_success"),
            "privateAccountConfigured": client.configured,
            "limitations": ["No backtest/profitability claim", "No autonomous account mutations", "No cross/coin-margined/options/margin borrowing", "Single-tenant deployment; persistent SQLite disk required"]}


@mcp.tool()
async def account_overview() -> dict[str, Any]:
    """Read account equity, cash, positions, ordinary/algo orders and unresolved executions."""
    return await service.account()


@mcp.tool()
async def analyze_trade(inst_id: str, direction: str = "long") -> dict[str, Any]:
    """Evaluate closed-candle directional entry evidence; analysis only, no order."""
    spec = await client.instrument(inst_id.upper())
    kind = instrument_kind(spec)
    if kind == "SPOT" and direction == "short":
        raise RuleViolation("现货不能开空")
    return {"instId": spec["instId"], **await service.signal(spec["instId"], direction)}


@mcp.tool()
async def preview_order(inst_id: str, intent: str, price: str, stop: str | None = None,
                        target: str | None = None, size: str | None = None, leverage: int = 1) -> dict[str, Any]:
    """Create a 120-second exact FOK execution preview. intent: enter_long/enter_short/exit_long/exit_short. Prices/size are decimal strings; size is base units for spot, contracts for derivatives. Entry needs stop and target; exit needs size. Does not place an order."""
    return await service.preview(inst_id, intent, price, stop, target, size, leverage)


@mcp.tool()
async def execute_preview(preview_id: str, confirmation: str) -> dict[str, Any]:
    """Mutates the OKX account. Only call AFTER the user explicitly confirms the entire displayed preview. confirmation must equal 用户回复: 确认执行 <preview_id>. Never manufacture confirmation. Revalidates and submits at most once."""
    require_local_auth()
    return await service.execute(preview_id, confirmation)


@mcp.tool()
async def reconcile_order(preview_id: str) -> dict[str, Any]:
    """Read exchange order/protection state by the original client ID; never resubmits."""
    require_local_auth()
    return await service.reconcile(preview_id)


@mcp.tool()
async def preview_cancel_order(inst_id: str, order_id: str, algo: bool = False) -> dict[str, Any]:
    """Preview cancellation of one current ordinary/algo order. Does not cancel. Cancelling protection requires explicit acknowledgement of the warning in the preview."""
    return await service.preview_cancel(inst_id.upper(), order_id, algo)


@mcp.tool()
async def plan_spot_grid(inst_id: str, lower: str, upper: str, stop: str,
                         budget: str, grids: int = 10) -> dict[str, Any]:
    """Plan a cash spot grid using current balance, fee and range data; DOES NOT start a bot or place orders."""
    inst_id = inst_id.upper()
    snapshot = await client.snapshot()
    equity, available = account_values(snapshot)
    spec = await client.instrument(inst_id)
    ticker = await client.ticker(inst_id)
    from rules import quote_price
    quote_price(ticker, "buy")
    return grid_plan(spec, equity, available, lower, upper, stop, budget, grids,
                     ticker["last"], await client.fee(spec), await client.candles(inst_id, "4H"))


def require_local_auth():
    if len(client.token) < 32:
        raise RuleViolation("启用提醒或操作本地状态前请配置MCP_AUTH_TOKEN，至少32字符")


@mcp.tool()
async def create_price_alert(inst_id: str, condition: str, threshold: str, cooldown_seconds: int = 900) -> dict[str, Any]:
    """Save an above/below price reminder. Runs only while the configured monitor is online. No trades. Without a configured webhook, reminders stay in get_events."""
    require_local_auth()
    if os.getenv("MONITOR_ENABLED", "false").lower() != "true":
        raise RuleViolation("请先启用MONITOR_ENABLED并运行服务，避免创建不会执行的提醒")
    spec = await client.instrument(inst_id.upper())
    instrument_kind(spec)
    return monitor.add(spec["instId"], condition, threshold, cooldown_seconds)


@mcp.tool()
async def list_price_alerts() -> list[dict[str, Any]]:
    """Read persistent price reminders and their state."""
    require_local_auth()
    return monitor.list()


@mcp.tool()
async def set_price_alert_enabled(alert_id: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable a saved local reminder; does not change an OKX order."""
    require_local_auth()
    return monitor.enable(alert_id, enabled)


@mcp.tool()
async def get_events(limit: int = 50) -> list[dict[str, Any]]:
    """Read price, execution and protection reminders plus delivery status."""
    require_local_auth()
    return service.store.events(limit)


@mcp.tool()
async def pause_new_entries(paused: bool = True) -> dict[str, Any]:
    """Set the local new-entry switch. Does NOT close positions or cancel orders; confirmed reductions remain possible."""
    require_local_auth()
    service.store.set("paused", paused)
    return {"paused": paused, "note": "仅影响本服务新开仓，现有OKX订单继续运行"}


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "OKX Live Trader", "version": "2.0.0"})


class BearerAuth:
    """Single-tenant authentication. Put OAuth in front for clients needing OAuth discovery."""
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health" and self.token:
            headers = dict(scope.get("headers", []))
            actual = headers.get(b"authorization", b"")
            expected = ("Bearer " + self.token).encode()
            if not hmac.compare_digest(actual, expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# Hostnames are explicitly configured; do not disable DNS rebinding protection.
allowed_hosts = [s.strip() for s in os.getenv("MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*,[::1],[::1]:*").split(",") if s.strip()]
if os.getenv("RENDER_EXTERNAL_HOSTNAME"):
    allowed_hosts.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
raw_app = mcp.streamable_http_app(host="0.0.0.0", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts))
app = BearerAuth(raw_app, client.token)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
