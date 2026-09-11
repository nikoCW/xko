# AWS EC2 deployment — xko Nautilus Bridge

This deployment keeps the public ChatGPT MCP gateway on the existing Render Free web service and moves the stateful NautilusTrader/OKX bridge to AWS EC2.

```text
ChatGPT
  -> Render Free: okx-live-trader-mcp
  -> HTTPS + Bearer token
  -> AWS EC2 + Elastic IP
  -> Caddy :443
  -> 127.0.0.1:8765 xko Nautilus Bridge
  -> NautilusTrader
  -> OKX
```

## Why EC2

- No Render Free sleep for the trading engine.
- SQLite intent/order state can live on EBS instead of ephemeral `/tmp`.
- An Elastic IP gives the bridge a stable outbound IP for OKX Trusted IP Access.
- The Nautilus API itself stays on loopback; only Caddy is internet-facing.

## 1. Create the EC2 instance

Recommended starting point for this bridge:

- Ubuntu Server 24.04 LTS. Use an architecture supported by the selected NautilusTrader wheel.
- 2 GiB RAM or more recommended for NautilusTrader + Python + WebSockets. A smaller instance can be tight.
- Assign an Elastic IP.
- Security Group inbound:
  - TCP 22: your own public IP only.
  - TCP 80: internet, for ACME certificate issuance/redirects.
  - TCP 443: internet during initial integration; restrict further when practical.
  - Do **not** open TCP 8765 publicly.

All non-health bridge endpoints require `BRIDGE_API_TOKEN`; HTTPS protects the token and request contents in transit. Add another network/auth layer before any future live-money enablement where practical.

## 2. Persist `/var/lib/xko-nautilus` on EBS

The bridge stores SQLite at:

```text
/var/lib/xko-nautilus/intents.db
```

The EC2 root EBS volume survives ordinary reboots. For stronger durability, attach a separate EBS volume, mount it at `/var/lib/xko-nautilus`, add it to `/etc/fstab` by UUID, and disable Delete on Termination for that data volume.

Do this before starting the bridge if you are using a separate data volume. Do not format a device that already contains data.

## 3. Bootstrap the host

SSH into the EC2 instance, then:

```bash
git clone https://github.com/nikoCW/xko.git
cd xko
sudo bash deploy/aws-ec2/bootstrap.sh
```

The bootstrap script:

- creates the locked-down `xko` system user;
- clones/updates the repository under `/opt/xko`;
- creates `/opt/xko/.venv`;
- installs the lean AWS runtime requirements;
- installs Caddy;
- creates `/var/lib/xko-nautilus`;
- installs the `xko-nautilus-bridge` systemd unit;
- creates `/etc/xko/nautilus-bridge.env` on first run;
- generates a random 256-bit `BRIDGE_API_TOKEN` on first run.

The script deliberately does not start the bridge before you fill the OKX credentials.

## 4. Add OKX credentials

Edit the root-owned environment file:

```bash
sudo nano /etc/xko/nautilus-bridge.env
```

Fill only your own credential values:

```text
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
```

Prefer dedicated OKX Demo credentials for any order-execution test. If only LIVE credentials are available, use them only for authenticated read/portfolio/preview validation while order submission remains disabled.

Keep these safety values unchanged during validation:

```text
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

`ALLOW_UNPROTECTED_ENTRY=true` is a startup configuration error. The bridge has no unprotected execution path.

Do not paste credentials into chat and do not commit `/etc/xko/nautilus-bridge.env` to GitHub.

## 5. Add DNS + HTTPS

Create a DNS A record such as:

```text
bridge.example.com -> EC2 Elastic IP
```

Then install the repo Caddy config and replace the example hostname:

```bash
sudo cp /opt/xko/deploy/aws-ec2/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Caddy reverse-proxies HTTPS to `127.0.0.1:8765`, so the Nautilus API port is never exposed directly.

## 6. Start Nautilus Bridge

```bash
sudo systemctl start xko-nautilus-bridge
sudo systemctl status xko-nautilus-bridge --no-pager
sudo journalctl -u xko-nautilus-bridge -f
```

Wait until the bridge reports reconciliation, connectivity, portfolio, and protection readiness before Preview calls.

Public health check:

```bash
curl https://bridge.example.com/health
```

Expected protected-path shape after startup with no unprotected allowed position:

```json
{
  "status": "ok",
  "ready": true,
  "portfolio_ready": true,
  "reconciliation_ready": true,
  "execution_connected": true,
  "data_connected": true,
  "protection_ready": true,
  "preview_ready": true,
  "trading_ready": true,
  "reconciliation_invalidated": false,
  "protection_invalidated": false,
  "order_submit_enabled": false,
  "unprotected_entry_enabled": false,
  "protected_submit_only": true,
  "protective_bracket_mode": "OKX_ATTACHED_OCO"
}
```

`ready` can be false briefly while Nautilus connects, reconciles, and performs the first protection scan. `trading_ready=true` is operational readiness only; it does not bypass `ALLOW_ORDER_SUBMIT=false`.

## 7. Protected execution model

For supported linear OKX SWAP intents, Preview performs contract-aware sizing. The protected execution path then builds one Nautilus bracket order list with:

```text
entry: LIMIT (or MARKET only if separately enabled)
SL:    STOP_MARKET, mandatory, LAST_PRICE trigger
TP:    MARKET_IF_TOUCHED, mandatory, LAST_PRICE trigger
```

NautilusTrader 1.231's OKX adapter translates a representable bracket into a single parent placement with venue-native attached TP/SL (`attachAlgoOrds`). This is intentionally used instead of submitting separate reduce-only conditional algos after the entry fill.

The bridge persists deterministic parent/SL/TP client IDs and protection lifecycle state. On restart, Nautilus reconciliation runs before the strategy starts, then the bridge scans open allowed positions and open orders. An open allowed position without a live reduce-only stop closes `protection_ready`; persistent failure is latched restart-required fail-closed.

## 8. Connect the existing Render MCP gateway

On the existing Render service `okx-live-trader-mcp`, set:

```text
XKO_NAUTILUS_URL=https://bridge.example.com
BRIDGE_API_TOKEN=<same value from /etc/xko/nautilus-bridge.env>
```

Then restart/redeploy only that existing Render web service. Do not create another Render Blueprint service for the bridge.

After that, ChatGPT MCP tools call the EC2 bridge through HTTPS.

## 9. OKX Trusted IP Access

If you enable Trusted IP Access on the OKX API key, whitelist the **EC2 Elastic IP**, because EC2/Nautilus is the component that makes authenticated OKX requests.

Do not whitelist Render outbound ranges for the OKX key unless Render itself makes private authenticated OKX API calls.

For future live use, prefer a dedicated low-privilege/sub-account key with Read + Trade only and no withdrawal permission.

## Updating EC2

After code changes are merged to `main`:

```bash
cd /opt/xko
sudo bash deploy/aws-ec2/update.sh
```

This fast-forwards the checkout, refreshes Python dependencies, reinstalls the systemd unit, and restarts the bridge. It does not overwrite `/etc/xko/nautilus-bridge.env` or the SQLite DB.

## Submission remains disabled during validation

Protective bracket construction and protection readiness are now implemented, but that is not the same as validating real venue execution for this particular account/runtime. Keep `ALLOW_ORDER_SUBMIT=false` while testing health and Preview. Do not enable real-money submission merely to smoke-test the new code.

Before any future enablement, validate parent + attached TP/SL acknowledgement, fills, shared OCO reporting, sibling cancellation, restart reconciliation, and missing-stop fail-closed behavior in an environment where sending test orders is acceptable.
