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

- Ubuntu Server 24.04 LTS, x86_64.
- 2 GiB RAM or more recommended for NautilusTrader + Python + WebSockets. A 1 GiB instance can be tight.
- Assign an Elastic IP.
- Security Group inbound:
  - TCP 22: your own public IP only.
  - TCP 80: internet, for ACME certificate issuance/redirects.
  - TCP 443: internet.
  - Do **not** open TCP 8765 publicly.

All non-health bridge endpoints require `BRIDGE_API_TOKEN`; HTTPS protects the token and request contents in transit. For a future live-money deployment, add another network/auth layer before enabling order submission.

## 2. Persist `/var/lib/xko-nautilus` on EBS

The bridge stores SQLite at:

```text
/var/lib/xko-nautilus/intents.db
```

For Demo, the EC2 root EBS volume is enough across reboots. For stronger durability, attach a separate EBS volume, mount it at `/var/lib/xko-nautilus`, add it to `/etc/fstab` by UUID, and disable Delete on Termination for that data volume.

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
- installs `nautilus_bridge/requirements.txt`;
- installs Caddy;
- creates `/var/lib/xko-nautilus`;
- installs the `xko-nautilus-bridge` systemd unit;
- creates `/etc/xko/nautilus-bridge.env` on first run;
- generates a random 256-bit `BRIDGE_API_TOKEN` on first run.

The script deliberately does not start the bridge before you fill the OKX credentials.

## 4. Add OKX Demo credentials

Edit the root-owned environment file:

```bash
sudo nano /etc/xko/nautilus-bridge.env
```

Fill only:

```text
OKX_API_KEY=
OKX_API_SECRET=
OKX_API_PASSPHRASE=
```

Keep these safety values unchanged:

```text
OKX_DEMO=true
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

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

The log should show Demo mode and the safety switches disabled for submission. Wait until the bridge reports ready/reconciliation success before Preview calls.

Public health check:

```bash
curl https://bridge.example.com/health
```

Expected shape:

```json
{
  "status": "ok",
  "ready": true,
  "okx_environment": "DEMO",
  "order_submit_enabled": false,
  "unprotected_entry_enabled": false
}
```

`ready` can be `false` briefly while Nautilus connects and reconciles.

## 7. Connect the existing Render MCP gateway

On the existing Render service `okx-live-trader-mcp`, set:

```text
XKO_NAUTILUS_URL=https://bridge.example.com
BRIDGE_API_TOKEN=<same value from /etc/xko/nautilus-bridge.env>
```

Then restart/redeploy only that existing Render web service. Do not create another Render Blueprint service for the bridge.

After that, ChatGPT MCP tools call the EC2 bridge through HTTPS.

## 8. OKX Trusted IP Access

If you enable Trusted IP Access on the OKX API key, whitelist the **EC2 Elastic IP**, because EC2/Nautilus is the component that makes authenticated OKX requests.

Do not whitelist the Render outbound ranges for the OKX key unless Render itself is making private authenticated OKX API calls.

For Demo use a dedicated Demo API key. For future live use, use a dedicated low-privilege/sub-account key with Read + Trade only and no withdrawal permission.

## Updating EC2

After code changes are merged to `main`:

```bash
cd /opt/xko
sudo bash deploy/aws-ec2/update.sh
```

This fast-forwards the checkout, refreshes Python dependencies, reinstalls the systemd unit, and restarts the bridge. It does not overwrite `/etc/xko/nautilus-bridge.env` or the SQLite DB.

## Current V1 safety limitation

`stop_loss` is currently used for risk sizing, but protective SL/TP child execution is not implemented yet. Therefore this repository intentionally keeps order submission blocked. Do not change `ALLOW_ORDER_SUBMIT` or `ALLOW_UNPROTECTED_ENTRY` for real-money trading until protective exits and restart/reconciliation behavior have been implemented and tested end to end.
