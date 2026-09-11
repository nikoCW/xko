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

from nautilus_bridge.nautilus_mcp_tools import register_nautilus_tools

OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
DEFAULT_QUOTE = os.getenv("OKX_QUOTE", "USDT").upper()
SUPPORTED_MARKET_TYPES = {"SPOT", "SWAP"}
STABLE_BASES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDS", "USDG", "PYUSD", "EURT"}

mcp = MCPServer(
    name="okx-live-trader",
    title="OKX Live Trader",
    version="1.2.0",
    description=(
        "OKX market-analysis MCP app with an optional, separately hosted, human-gated "
        "NautilusTrader execution bridge. Public market tools are read-only."
    ),
    instructions=(
        "Use market_scan, get_ticker, compare_symbols, get_candles and run_live_trader for public "
        "OKX analysis. For any *.SWAP.OKX trade intent, obtain the entry/reference market data "
        "from the exact matching OKX SWAP instrument (for example BTC-USDT-SWAP), never from "
        "BTC-USDT SPOT and never use SPOT as a fallback. Call get_ticker on that SWAP before "
        "create_trade_intent and pass its exact instId as price_reference_instrument_id. For "
        "trading, call nautilus_health first, then create_trade_intent and preview_trade_intent. "
        "Never choose raw order quantity yourself; Nautilus sizes from risk_pct, entry and stop. "
        "approve_trade_intent requires a one-time code explicitly supplied by the user. Call "
        "submit_trade_intent only after an explicit user instruction to execute. Never claim an "
        "order was placed until the Nautilus execution status confirms it."
    ),
)


async def okx_get(path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    headers = {"User-Agent": "okx-live-trader-mcp/1.2", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        response = await client.get(f"{OKX_API_BASE}{path}", params=params or {})
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


def normalize_inst_type(value: str) -> str:
    inst_type = value.strip().upper()
    if inst_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError(f"Unsupported inst_type={value!r}; use SPOT or SWAP")
    return inst_type


def normalize_market_instrument(
    symbol: str,
    quote: str = DEFAULT_QUOTE,
    inst_type: str = "SPOT",
) -> tuple[str, str]:
    """Return canonical OKX instId plus effective market type.

    Explicit -SWAP symbols always resolve as SWAP. Bare/base or BASE-QUOTE symbols
    use inst_type. Nautilus IDs ending in .OKX are accepted as convenience input.
    """
    raw = symbol.strip().upper()
    if not raw:
        raise ValueError("symbol must not be empty")
    if raw.endswith(".OKX"):
        raw = raw[:-4]

    requested_type = normalize_inst_type(inst_type)
    quote = quote.strip().upper()

    if raw.endswith("-SWAP"):
        return raw, "SWAP"

    if "-" not in raw:
        raw = f"{raw}-{quote}"

    if requested_type == "SWAP":
        if raw.count("-") != 1:
            raise ValueError(
                f"Cannot derive OKX SWAP instrument from {symbol!r}; use BASE-QUOTE-SWAP"
            )
        raw = f"{raw}-SWAP"

    return raw, requested_type


def ticker_view(row: dict[str, Any], inst_type: str | None = None) -> dict[str, Any]:
    last = fnum(row.get("last"))
    open24h = fnum(row.get("open24h"))
    change_pct = ((last / open24h) - 1.0) * 100.0 if open24h > 0 else None
    inst = str(row.get("instId", ""))
    effective_type = inst_type or ("SWAP" if inst.endswith("-SWAP") else "SPOT")
    base = inst.split("-")[0] if "-" in inst else inst
    return {
        "symbol": base,
        "instId": inst,
        "instType": effective_type,
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


async def ticker_for(inst_id: str, inst_type: str) -> dict[str, Any]:
    rows = await okx_get("/api/v5/market/ticker", {"instId": inst_id})
    if not rows:
        raise RuntimeError(f"OKX returned no ticker for {inst_id}")
    row = rows[0]
    if str(row.get("instId", "")).upper() != inst_id.upper():
        raise RuntimeError(
            f"OKX ticker instrument mismatch: requested={inst_id} returned={row.get('instId')}"
        )
    item = ticker_view(row, inst_type)
    if item["last"] <= 0:
        raise RuntimeError(f"OKX ticker for {inst_id} has non-positive last price")
    return item


async def all_tickers(inst_type: str, quote: str = DEFAULT_QUOTE) -> list[dict[str, Any]]:
    inst_type = normalize_inst_type(inst_type)
    rows = await okx_get("/api/v5/market/tickers", {"instType": inst_type})
    quote = quote.upper()
    suffix = f"-{quote}" if inst_type == "SPOT" else f"-{quote}-SWAP"
    parsed: list[dict[str, Any]] = []
    for row in rows:
        inst_id = str(row.get("instId", ""))
        if not inst_id.endswith(suffix):
            continue
        item = ticker_view(row, inst_type)
        if item["last"] > 0 and item["change24hPct"] is not None:
            parsed.append(item)
    return parsed


async def all_spot_tickers(quote: str = DEFAULT_QUOTE) -> list[dict[str, Any]]:
    return await all_tickers("SPOT", quote)


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
        out.append(
            {
                "ts": int(row[0]),
                "time": datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc).isoformat(),
                "open": fnum(row[1]),
                "high": fnum(row[2]),
                "low": fnum(row[3]),
                "close": fnum(row[4]),
                "volume": fnum(row[5]),
                "quoteVolume": fnum(row[7]),
                "confirmed": str(row[8]) == "1",
            }
        )
    out.sort(key=lambda x: x["ts"])
    return out


def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    completed = [c for c in candles if c["confirmed"]]
    if len(completed) < period + 1:
        return None
    trs = [
        max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        for prev, cur in zip(completed[:-1], completed[1:])
    ]
    return sum(trs[-period:]) / period if len(trs) >= period else None


def candle_summary(candles: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [c for c in candles if c["confirmed"]] or candles[:]
    recent20 = completed[-20:]
    recent50 = completed[-50:]
    avg = lambda items: sum(c["close"] for c in items) / len(items) if items else None
    return {
        "bars": len(candles),
        "completedBars": len(completed),
        "high20": max((c["high"] for c in recent20), default=None),
        "low20": min((c["low"] for c in recent20), default=None),
        "sma20": avg(recent20),
        "sma50": avg(recent50),
        "atr14": atr(candles, 14),
        "lastCompletedClose": completed[-1]["close"] if completed else None,
    }


@mcp.tool(
    name="market_scan",
    description="Scan liquid OKX spot markets for top 24h gainers and losers.",
)
async def market_scan(
    quote: str = "USDT",
    top_n: int = 10,
    min_quote_volume: float = 1_000_000,
) -> dict[str, Any]:
    quote = quote.upper()
    filtered = [
        x
        for x in await all_spot_tickers(quote)
        if x["quoteVolume24h"] >= min_quote_volume and x["symbol"] not in STABLE_BASES
    ]
    filtered.sort(key=lambda x: x["change24hPct"])
    n = max(1, min(int(top_n), 50))
    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "quote": quote,
        "instType": "SPOT",
        "minQuoteVolume": min_quote_volume,
        "eligibleCount": len(filtered),
        "topGainers": list(reversed(filtered[-n:])),
        "topLosers": filtered[:n],
        "note": "Read-only OKX public SPOT market data; no orders are sent.",
    }


@mcp.tool(
    name="get_ticker",
    description=(
        "Get the exact OKX ticker for SPOT or SWAP. For a Nautilus *.SWAP.OKX trade intent, "
        "call this with the matching BASE-QUOTE-SWAP instrument and use the returned instId "
        "as price_reference_instrument_id. Never substitute SPOT for SWAP."
    ),
)
async def get_ticker(
    symbol: str,
    quote: str = "USDT",
    inst_type: str = "SPOT",
) -> dict[str, Any]:
    inst_id, effective_type = normalize_market_instrument(symbol, quote, inst_type)
    item = await ticker_for(inst_id, effective_type)
    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "requested": symbol,
        "instId": inst_id,
        "instType": effective_type,
        "ticker": item,
        "source": "OKX /api/v5/market/ticker",
    }


@mcp.tool(
    name="compare_symbols",
    description=(
        "Compare OKX symbols by current price, 24h change, range and volume. Supports SPOT "
        "and SWAP. Symbols ending in -SWAP (or .SWAP.OKX form) are always queried as SWAP."
    ),
)
async def compare_symbols(
    symbols: list[str],
    quote: str = "USDT",
    inst_type: str = "SPOT",
) -> dict[str, Any]:
    quote = quote.upper()
    requested: list[tuple[str, str]] = [
        normalize_market_instrument(symbol, quote, inst_type) for symbol in symbols[:20]
    ]
    needed_types = {market_type for _, market_type in requested}
    lookups: dict[str, dict[str, dict[str, Any]]] = {}
    for market_type in needed_types:
        lookups[market_type] = {
            item["instId"]: item for item in await all_tickers(market_type, quote)
        }

    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for inst_id, market_type in requested:
        item = lookups[market_type].get(inst_id)
        if item is None:
            missing.append(inst_id)
        else:
            rows.append(item)
    return {
        "asOf": datetime.now(timezone.utc).isoformat(),
        "items": rows,
        "missing": missing,
        "marketTypesQueried": sorted(needed_types),
        "spotFallbackUsed": False,
    }


@mcp.tool(
    name="get_candles",
    description=(
        "Retrieve recent OKX SPOT or SWAP candles plus compact trend and ATR summary. "
        "An explicit -SWAP symbol is never rewritten to SPOT."
    ),
)
async def get_candles(
    symbol: str,
    quote: str = "USDT",
    bar: str = "1D",
    limit: int = 90,
    inst_type: str = "SPOT",
) -> dict[str, Any]:
    inst_id, effective_type = normalize_market_instrument(symbol, quote, inst_type)
    candles = await candles_for(inst_id, bar, limit)
    return {
        "instId": inst_id,
        "instType": effective_type,
        "bar": bar,
        "summary": candle_summary(candles),
        "candles": candles,
        "spotFallbackUsed": False,
    }


@mcp.tool(
    name="run_live_trader",
    description=(
        "Run the read-only OKX analysis workflow: scan liquid spot markets, select a "
        "momentum candidate, compare benchmarks, inspect daily candles, and return a "
        "conditional breakout plan. This tool never executes a trade."
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
        x
        for x in tickers
        if x["quoteVolume24h"] >= min_quote_volume
        and x["symbol"] not in STABLE_BASES
        and x["symbol"] not in benchmarks
    ]
    eligible.sort(key=lambda x: x["change24hPct"], reverse=True)
    leaders = eligible[: max(3, min(int(scan_top_n), 50))]
    if not leaders:
        raise RuntimeError("No eligible OKX spot symbols matched the current filters.")

    def score(x: dict[str, Any]) -> float:
        vol_m = max(x["quoteVolume24h"] / 1_000_000, 1e-9)
        stretch = max(x["change24hPct"] - 30.0, 0.0)
        return x["change24hPct"] + 2.0 * math.log10(vol_m + 1.0) - 0.35 * stretch

    candidate = max(leaders, key=score)
    lookup = {x["symbol"]: x for x in tickers}
    comparison = [candidate] + [lookup[b] for b in ("BTC", "ETH", "SOL") if b in lookup]
    candles = await candles_for(
        candidate["instId"],
        "1D",
        min(max(lookback_days, 30), 300),
    )
    summary = candle_summary(candles)
    trigger = max(candidate["high24h"], summary.get("high20") or 0.0)
    a14 = summary.get("atr14")
    if a14 and a14 > 0:
        stop = trigger - 1.5 * a14
    else:
        day_range = max(candidate["high24h"] - candidate["low24h"], trigger * 0.02)
        stop = trigger - day_range
    risk = max(trigger - stop, 0.0)
    target1 = trigger + risk if risk > 0 else None
    target2 = trigger + 2 * risk if risk > 0 else None
    benchmark_changes = {
        x["symbol"]: x["change24hPct"] for x in comparison if x["symbol"] in benchmarks
    }
    strongest = max(benchmark_changes.values()) if benchmark_changes else None
    relative_edge = candidate["change24hPct"] - strongest if strongest is not None else None

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
                "Do not treat a touch as confirmation. Prefer a sustained break above "
                "the trigger with relative strength still intact."
            ),
            "initialStopReference": round(stop, 12),
            "riskPerUnit": round(risk, 12),
            "target1_1R": round(target1, 12) if target1 is not None else None,
            "target2_2R": round(target2, 12) if target2 is not None else None,
            "invalidation": {
                "24hLow": candidate["low24h"],
                "20DayLow": summary.get("low20"),
                "logic": (
                    "The momentum thesis weakens below the 24h low; a break below the "
                    "20-day low is a broader structural invalidation."
                ),
            },
        },
        "limitations": [
            "No market-cap data is used; liquidity filtering uses OKX 24h quote volume.",
            "This analysis tool does not place, amend, or cancel orders.",
        ],
    }


# Register separately hosted, human-gated Nautilus tools into the same MCP endpoint.
register_nautilus_tools(mcp)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "OKX Live Trader + Nautilus Gateway",
            "mode": "read-only-market-data + gated-nautilus-tools",
            "mcpEndpoint": "/mcp",
            "marketDataVersion": "spot+swap-v1",
            "nautilusBridgeConfigured": bool(os.getenv("XKO_NAUTILUS_URL")),
        }
    )


app = mcp.streamable_http_app(
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
