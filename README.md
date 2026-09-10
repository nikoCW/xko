# OKX Live Trader — Remote MCP for ChatGPT

A read-only MCP app for OKX public market data.

## What it exposes

- `market_scan` — top gainers/losers with liquidity filter
- `compare_symbols` — BTC/ETH/SOL or arbitrary symbol comparison
- `get_candles` — recent OHLCV candles + ATR/trend summary
- `run_live_trader` — full workflow:
  scan → filter → candidate → benchmark comparison → daily candles → conditional plan

This server **does not contain any order-placement tools** and does not need an OKX API key.

## Deploy on Render

1. Push these files to your GitHub repository.
2. In Render, create a new **Web Service** from the repository.
3. Build command:
   `pip install -r requirements.txt`
4. Start command:
   `uvicorn server:app --host 0.0.0.0 --port $PORT`
5. Health check path:
   `/health`
6. After deploy, open:
   `https://YOUR-SERVICE.onrender.com/health`
   and verify it returns `"status":"ok"`.

Your MCP endpoint is:

`https://YOUR-SERVICE.onrender.com/mcp`

## Add it to ChatGPT

In the custom MCP app dialog:

- Name: `OKX Live Trader`
- Connection: `Server URL`
- Server URL: `https://YOUR-SERVICE.onrender.com/mcp`
- Authentication: `No authentication`
- Accept the custom MCP warning
- Create

Then start a new chat and select / mention the app when available.

Suggested prompt:

> @OKX Live Trader 扫描当前 OKX 市场，过滤低流动性标的，找出一个相对 BTC/ETH/SOL 最强的短线机会，检查 90 天日 K，并给出触发、止损、目标和失效条件。只分析，不下单。

## Local test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8000
```

Health:
`http://127.0.0.1:8000/health`

MCP:
`http://127.0.0.1:8000/mcp`

Note: ChatGPT cannot connect directly to your localhost URL; local testing is only for verifying the server before deploying it.

## Security

This version is intentionally read-only and uses only public OKX market-data endpoints.
Do not add API keys to this public unauthenticated service.

If you later want demo/live trading, add authentication first and use a dedicated OKX sub-account / demo key with minimal permissions.
