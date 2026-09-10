# xko + ChatGPT + NautilusTrader + OKX bridge

A minimal, safety-first skeleton for putting ChatGPT/MCP above NautilusTrader.

## Architecture

```text
ChatGPT
   |
   | MCP tools
   v
xko / nautilus_mcp_tools.py
   |
   | HTTP JSON (localhost by default)
   v
Nautilus bridge API
   |
   | thread-safe command queue
   v
AIIntentStrategy (Nautilus event thread)
   |
   +--> FixedRiskSizer
   +--> Nautilus RiskEngine
   +--> ExecutionEngine / reconciliation
   v
OKX adapter
```

The LLM does **not** choose order quantity. It proposes:

- instrument
- side
- entry type
- reference entry price
- stop loss
- optional take profit
- risk percentage
- reason

Nautilus computes quantity with `FixedRiskSizer` from the instrument definition and current account equity.

## Safety defaults

This skeleton defaults to:

- `OKX_DEMO=true`
- `ALLOW_ORDER_SUBMIT=false`
- local API bind `127.0.0.1`
- explicit allowed-instrument list
- max risk percentage
- max quantity hard cap
- Nautilus reconciliation enabled
- Nautilus live RiskEngine enabled
- deterministic/alphanumeric `client_order_id`
- durable intent records in SQLite
- one-time human approval code printed only to the local Nautilus service console

Even if ChatGPT creates an intent, it cannot retrieve the approval code through the MCP tools.

### Important limitation of v1

`stop_loss` is used for **position sizing**, but this skeleton intentionally does not submit a protective stop/TP child order yet. Therefore live entry submission is additionally protected by:

```env
ALLOW_UNPROTECTED_ENTRY=false
```

Keep that `false`.

For a safe production version, the next phase should implement and integration-test OKX protective exits (including partial fills, child rejection, reconnect and reconciliation) before live money is enabled.

## Version basis

The code targets the current NautilusTrader Python API shape used by the 1.231.x line:

- `OKXExecutionClientConfig`
- `OKXExecutionClientFactory`
- `LiveNode.builder(...)`
- `FixedRiskSizer`
- `portfolio.equity(...)`

Pinning is intentional so an upstream API rename does not silently change the bridge.

## Files

```text
.
├── .env.example
├── requirements.txt
├── trading_models.py
├── intent_store.py
├── bridge_runtime.py
├── nautilus_mcp_tools.py
├── nautilus_service
│   ├── __init__.py
│   ├── api.py
│   ├── bridge_strategy.py
│   └── main.py
└── tests
    └── test_models.py
```

## 1. Install

Python 3.12 is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure OKX Demo credentials

Copy:

```bash
cp .env.example .env
```

Fill in your OKX Demo API credentials:

```env
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_API_PASSPHRASE=...
```

Keep:

```env
OKX_DEMO=true
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
```

## 3. Start Nautilus service

```bash
python -m nautilus_service.main
```

The service exposes a local bridge API, default:

```text
http://127.0.0.1:8765
```

When an intent is created, the service console prints a one-time code such as:

```text
[HUMAN APPROVAL] intent=... code=482193
```

The code is intentionally not returned by the API.

## 4. Register tools in your existing xko server

Copy these files into your xko project:

```text
trading_models.py
nautilus_mcp_tools.py
```

Then, after your existing `mcp = MCPServer(...)` initialization in `server.py`, add:

```python
from nautilus_mcp_tools import register_nautilus_tools

register_nautilus_tools(mcp)
```

Your existing read-only market tools can stay unchanged.

## 5. ChatGPT flow

A safe interaction becomes:

```text
You:
Analyze BTC and propose a trade.

ChatGPT:
Uses xko market tools.
Creates TradeIntent.
Calls preview_trade_intent.
Shows calculated quantity and risk.

You:
I approve. Code 482193.

ChatGPT:
approve_trade_intent(intent_id, "482193")

You:
Submit it.

ChatGPT:
submit_trade_intent(intent_id)
```

With the default `.env`, the final submit is still blocked.

To test actual order routing on **OKX Demo only**, first make sure you understand the unprotected-entry limitation, then explicitly set:

```env
ALLOW_ORDER_SUBMIT=true
ALLOW_UNPROTECTED_ENTRY=true
```

Do not use those settings with a live account.

## API endpoints

- `GET /health`
- `POST /intents`
- `GET /intents/{intent_id}`
- `POST /intents/{intent_id}/preview`
- `POST /intents/{intent_id}/approve`
- `POST /intents/{intent_id}/submit`

## Hard risk controls

`MAX_RISK_PCT` is checked by bridge policy before Nautilus sizing.

`MAX_ORDER_QTY` is passed as the `FixedRiskSizer` hard limit.

`MAX_NOTIONAL_PER_ORDER_USDT` is configured into Nautilus `LiveRiskEngineConfig` for every allowed instrument.

These controls are separate from the LLM. The model cannot change them through MCP.

## Production work still required

Before live money, add:

1. Native or strategy-managed protective stop/TP lifecycle.
2. Partial-fill child-size handling.
3. Restart recovery: rebuild intent-to-order mapping from reconciled client order IDs.
4. Daily loss / account drawdown kill switch.
5. Max portfolio exposure and max leverage policy.
6. Explicit socket/reconciliation-health gate before new entries.
7. Persistent audit log of every tool call, approval and execution event.
8. Integration tests against OKX Demo for timeout-after-submit and reconnect scenarios.
