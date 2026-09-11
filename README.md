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

The public MCP gateway provides OKX market analysis tools and registers human-gated Nautilus intent tools. ChatGPT emits structured trade intent; sizing, risk checks, execution state, protective-order state, and reconciliation live in the Nautilus process.

## MCP tools

Market-analysis tools include:

- `market_scan`
- `get_ticker`
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

Order submission is intentionally blocked by default.

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

## Protected execution model

For supported linear OKX SWAP intents, the bridge sizes risk in native contract units and builds a Nautilus bracket containing:

- entry order (`LIMIT`, or `MARKET` only when explicitly enabled);
- mandatory `STOP_MARKET` stop-loss;
- mandatory `MARKET_IF_TOUCHED` take-profit;
- `TriggerType.LAST_PRICE` for both protective triggers.

On NautilusTrader 1.231, the OKX adapter translates a representable bracket `SubmitOrderList` into one venue-native parent order carrying attached TP/SL (`attachAlgoOrds`). This avoids the old post-fill flow where separate reduce-only conditional orders could leave an unprotected gap or be rejected by OKX.

Protection ownership is scoped to XKO. The global `protection_ready` scan only evaluates reconciled positions whose opening order belongs to a persisted XKO intent. Manual positions, grid bots, and other external strategies do not globally disable the bridge. To avoid unsafe co-management of an OKX net position, submission has a separate per-instrument isolation rule: the target instrument must be flat before XKO can submit a new protected bracket. Preview remains allowed and reports whether the target instrument is already occupied.

If an XKO-owned open position has no live XKO stop-loss child, `protection_ready` closes immediately; persistent failure becomes restart-required fail-closed state.

## Safety defaults

Keep these values while validating the protected path:

```text
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

`ALLOW_UNPROTECTED_ENTRY=true` is now treated as a configuration error; there is no unprotected submission path. A Preview can report `protected_submit_ready=true`, but that is only bracket construction/readiness evidence and does **not** mean live order submission has been validated.

Do not enable real-money order submission until the attached-OCO path has been tested end to end in an environment where sending test orders is acceptable, including parent/child acknowledgement, fill handling, restart reconciliation, OCO sibling cancellation, and missing-protection fail-closed behavior.

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