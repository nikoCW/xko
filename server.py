from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
DEFAULT_QUOTE = os.getenv("OKX_QUOTE", "USDT").upper()

STABLE_BASES = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDS", "USDG", "PYUSD", "EURT"
}

mcp = MCPServer(
    name="okx-live-trader",
    title="OKX Live Trader",
    version="1.0.0",
    description=(
        "Read-only OKX market-analysis MCP app. Scans spot markets, compares "
        "relative strength, retrieves candlesticks, and produces conditional "
        "trade plans. It never places, amends, or cancels orders."
    ),
    instructions=(
        "Use this app for OKX market-data and short-term trading analysis. "
        "Prefer run_live_trader for an end-to-end workflow. Always state that "
        "the output is conditional market analysis, not an executed order. "
        "Never claim an order was placed because this server is read-only."
    ),
)

async def okx_get(path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    url = f"{OKX_API_BASE}{path}"
    headers = {
        "User-Agent": "okx-live-trader-mcp/1.0",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        response = await client.get(url, params=params or {})
        response.raise_for_status()
        payload = response.json()

    if str(payload.get("code", "")) != "0":
        raise RuntimeError(f"OKX API error: {payload.get('code')} {payload.get('msg')}")
    return payload.get("data", [])

def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
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
    if not completed:
        completed = candles[:]
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
    min_quote_volume: float = 1_000_000,
) -> dict[str, Any]:
    quote = quote.upper()
    items = await all_spot_tickers(quote)
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

@mcp.tool(
    name="run_live_trader",
    description=(
        "Run the full OKX Live Trader workflow end to end: scan liquid spot markets, "
        "pick a momentum candidate, compare it with BTC/ETH/SOL, inspect daily candles, "
        "and return a conditional breakout plan with trigger, stop logic, targets and "
        "invalidation. This tool is analysis-only and never executes a trade."
    ),
)
async def run_live_trader(
    quote: str = "USDT",
    min_quote_volume: float = 5_000_000,
    scan_top_n: int = 15,
    lookback_days: int = 90,
) -> dict[str, Any]:
    quote = quote.upper()
    tickers = await all_spot_tickers(quote)
    benchmarks = {"BTC", "ETH", "SOL"}

    eligible = [
        x for x in tickers
        if x["quoteVolume24h"] >= min_quote_volume
        and x["symbol"] not in STABLE_BASES
        and x["symbol"] not in benchmarks
    ]
    eligible.sort(key=lambda x: x["change24hPct"], reverse=True)
    leaders = eligible[: max(3, min(int(scan_top_n), 50))]
    if not leaders:
        raise RuntimeError("No eligible OKX spot symbols matched the current filters.")

    # Prefer strong momentum with meaningful turnover, while lightly penalizing
    # extremely stretched one-day moves.
    def score(x: dict[str, Any]) -> float:
        vol_m = max(x["quoteVolume24h"] / 1_000_000, 1e-9)
        stretch = max(x["change24hPct"] - 30.0, 0.0)
        return x["change24hPct"] + 2.0 * math.log10(vol_m + 1.0) - 0.35 * stretch

    candidate = max(leaders, key=score)

    lookup = {x["symbol"]: x for x in tickers}
    comparison = [candidate]
    for b in ("BTC", "ETH", "SOL"):
        if b in lookup:
            comparison.append(lookup[b])

    candles = await candles_for(candidate["instId"], "1D", min(max(lookback_days, 30), 300))
    summary = candle_summary(candles)
    a14 = summary.get("atr14")

    trigger_candidates = [
        candidate["high24h"],
        summary.get("high20") or 0.0,
    ]
    trigger = max(trigger_candidates)

    if a14 and a14 > 0:
        stop = trigger - 1.5 * a14
    else:
        # Fallback: use the current 24h range as a volatility proxy.
        day_range = max(candidate["high24h"] - candidate["low24h"], trigger * 0.02)
        stop = trigger - day_range

    risk = max(trigger - stop, 0.0)
    target1 = trigger + risk if risk > 0 else None
    target2 = trigger + 2 * risk if risk > 0 else None

    benchmark_changes = {
        x["symbol"]: x["change24hPct"] for x in comparison if x["symbol"] in benchmarks
    }
    strongest_benchmark = max(benchmark_changes.values()) if benchmark_changes else None
    relative_edge = (
        candidate["change24hPct"] - strongest_benchmark
        if strongest_benchmark is not None else None
    )

    invalidation_levels = {
        "24hLow": candidate["low24h"],
        "20DayLow": summary.get("low20"),
        "logic": (
            "The momentum thesis weakens if price breaks below the 24h low; "
            "a break below the 20-day low is a broader structural invalidation."
        ),
    }

    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "mode": "READ_ONLY_ANALYSIS",
        "workflow": [
            "scan_liquid_spot_market",
            "rank_momentum_candidates",
            "compare_with_BTC_ETH_SOL",
            "inspect_90d_daily_candles",
            "build_conditional_trade_plan",
        ],
        "filters": {
            "quote": quote,
            "minQuoteVolume24h": min_quote_volume,
            "stablecoinsExcluded": True,
            "benchmarksExcludedFromCandidateSelection": sorted(benchmarks),
        },
        "marketLeaders": leaders[:10],
        "candidate": candidate,
        "comparison": comparison,
        "relativeStrengthVsStrongestBenchmarkPctPoints": (
            round(relative_edge, 4) if relative_edge is not None else None
        ),
        "candleAnalysis": summary,
        "tradePlan": {
            "bias": "conditional_long_momentum",
            "entryTrigger": trigger,
            "entryRule": (
                "Do not treat a touch as confirmation. Prefer a sustained break "
                "above the trigger with relative strength still intact."
            ),
            "initialStopReference": round(stop, 12),
            "riskPerUnit": round(risk, 12),
            "target1_1R": round(target1, 12) if target1 is not None else None,
            "target2_2R": round(target2, 12) if target2 is not None else None,
            "invalidation": invalidation_levels,
        },
        "limitations": [
            "No market-cap data is used; liquidity filtering is based on OKX 24h quote volume.",
            "No order is placed, amended or cancelled.",
            "This is conditional market analysis, not financial advice or a guarantee of outcome.",
        ],
    }

@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "OKX Live Trader",
        "mode": "read-only",
        "mcpEndpoint": "/mcp",
    })

# Stateless + JSON response makes deployment simple and horizontally safe.
# DNS-rebinding protection is disabled here because the deployment hostname is
# assigned dynamically by platforms such as Render/Railway. This server exposes
# only public, read-only market-data tools and contains no credentials.
app = mcp.streamable_http_app(
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
