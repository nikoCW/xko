# OKX Live Trader + Nautilus Bridge

`xko` uses a split-process architecture:

```text
ChatGPT
  -> Render Free MCP gateway (`okx-live-trader-mcp`)
  -> HTTPS + Bearer token
  -> AWS EC2 Nautilus bridge
  -> NautilusTrader risk/execution/reconciliation
  -> OKX
```

The public MCP gateway provides OKX market analysis tools and registers human-gated Nautilus intent tools. ChatGPT emits structured trade intent; sizing, risk checks, execution state, and reconciliation live in the Nautilus process.

## MCP tools

Market-analysis tools include:

- `market_scan`
- `compare_symbols`
- `get_candles`
- `run_live_trader`

Nautilus bridge tools include:

- `nautilus_health`
- `create_trade_intent`
- `preview_trade_intent`
- `get_trade_intent`
- `approve_trade_intent`
- `submit_trade_intent`

The current V1 bridge intentionally blocks order submission by default.

## Deployment

### Render — MCP gateway only

Keep the existing `okx-live-trader-mcp` Render web service. Its normal start command is:

```text
uvicorn server:app --host 0.0.0.0 --port $PORT
```

After the EC2 bridge is online, set these environment variables on the existing Render service:

```text
XKO_NAUTILUS_URL=https://bridge.example.com
BRIDGE_API_TOKEN=<same random token used by EC2>
```

Do not create another Render Blueprint bridge service. The stateful bridge has moved to AWS EC2.

### AWS EC2 — Nautilus bridge

Use the deployment package in:

```text
deploy/aws-ec2/
```

Start with:

```bash
git clone https://github.com/nikoCW/xko.git
cd xko
sudo bash deploy/aws-ec2/bootstrap.sh
```

Full instructions, EBS persistence, Elastic IP, HTTPS/Caddy, OKX Trusted IP, systemd, and update workflow are documented in `deploy/aws-ec2/README.md`.

## Safety defaults

Keep these values while validating the Demo path:

```text
OKX_DEMO=true
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

The bridge uses `stop_loss` for position-risk sizing, but protective SL/TP child execution is not implemented yet. Do not enable real-money order submission until protective exits and restart/reconciliation behavior are implemented and tested end to end.

## Local gateway test

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn server:app --host 127.0.0.1 --port 8000
```

Gateway health:

```text
http://127.0.0.1:8000/health
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```
