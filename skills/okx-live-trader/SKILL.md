---
name: okx-live-trader
description: Use live OKX market data to scan crypto opportunities, filter extreme or low-quality movers, compare candidates against BTC/ETH/SOL, inspect price charts, and produce a conditional trading plan. Use when the user invokes OKX Live Trader, asks for an OKX market scan, relative-strength scan, crypto momentum setup, or a structured short-term trade plan based on current OKX data.
version: 1.0.0
---

# OKX Live Trader

Use this skill as a structured, read-only market-analysis workflow. Prefer live OKX market-data tools when available. Never claim an order was placed, modified, cancelled, or filled unless a real execution tool is explicitly available and actually used.

## Core workflow

1. **Resolve the task**
   - Identify the requested universe, timeframe, and whether the user wants a scan, a single-symbol analysis, or a comparison.
   - If not specified, default to a short-term momentum scan using current spot-market data.

2. **Scan the market**
   - Retrieve top gainers and, when useful, top losers.
   - Note price change, market cap, and 24h volume when available.
   - Do not mechanically select the largest percentage mover.

3. **Filter candidates**
   - Prefer candidates with stronger liquidity and more credible market depth proxies.
   - Flag unusually small market caps, extreme turnover, or outsized one-day moves as higher-risk.
   - Select one primary candidate and briefly explain why it survived the filter.

4. **Benchmark relative strength**
   - Compare the candidate with BTC, ETH, and SOL unless the user requests other benchmarks.
   - State whether the candidate is showing meaningful relative strength or weakness versus the majors.

5. **Inspect the chart**
   - Default to a 90-day daily chart for short-term swing context.
   - Use another interval or history window if the user specifies it.
   - Identify only levels supported by returned market data. Do not fabricate indicators or candles that were not provided.

6. **Build a conditional plan**
   - Provide: current context, trigger condition, invalidation condition, stop logic, and one or more profit-management ideas.
   - Prefer conditional phrasing such as “if price holds above…” rather than unconditional buy/sell instructions.
   - If the day is already highly extended, explicitly discuss chase risk and the option to wait for confirmation or a pullback.

7. **State limitations**
   - Distinguish market-data analysis from execution.
   - If OKX tools are unavailable in the current environment, say that live OKX data is unavailable and do not substitute invented prices.

## Output format

Keep the response compact and trader-friendly:

- **Market regime:** broad move or dispersion
- **Selected candidate:** symbol + why
- **Relative strength:** candidate vs BTC/ETH/SOL
- **Key live data:** current price, 24h change, high/low, volume when available
- **Trade setup:** trigger, stop/invalidation, targets or management logic
- **Risk note:** what would make the setup unattractive
- **Execution status:** explicitly say “analysis only / no order placed” unless a real execution action occurred

## Guardrails

- Use live tool results as the source of truth for current prices.
- Do not invent leverage, position size, account balance, liquidation price, funding, open interest, order-book depth, or technical indicators unless those data are actually available.
- Do not imply certainty or guaranteed profit.
- If the user requests execution but only read-only OKX tools are available, explain the limitation and provide the proposed order parameters for review instead of pretending to execute.
