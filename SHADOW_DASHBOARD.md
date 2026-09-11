# XKO Shadow Operator Dashboard v1

Status: **LIVE SHADOW ONLY / NO APPROVE / NO ORDER SUBMIT**

This dashboard is a separate, loopback-only process for observing the real bridge and recording human review notes while all execution switches remain disabled. It does not modify the frozen Nautilus bridge runtime and it intentionally exposes no approval or submit route.

## Capabilities

- Read `/health` and recent TradeIntent records from the local Nautilus bridge.
- Display reconciliation, protection, preview and execution connectivity gates.
- Display instrument/risk/entry/SL/TP and current persisted intent status.
- Run the existing Nautilus **PREVIEW** path for a selected intent.
- Display bracket dry-run fields returned by the bridge, including target-instrument occupancy, quantities, trigger resolution and `venue_submit_called`.
- Record a separate human shadow review (`operator`, `note`, current health snapshot and preview snapshot).
- Show a go-live checklist. Real venue lifecycle validation is deliberately hard-coded as **not validated**, so the dashboard cannot declare this build ready for automatic live execution.

The dashboard has no endpoint that calls bridge `/approve` or `/submit`.

## Safety invariants

Installation refuses unless all three are exactly false:

```text
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

The service itself refuses any non-loopback bind. Default listener:

```text
127.0.0.1:8766
```

Port 8766 must not be opened in the AWS Security Group and is not added to Caddy. Access is through an SSH port-forward only.

The installer also checks that the core bridge/runtime paths have not changed since the pre-live freeze commit:

```text
97956abf51f567c1d87b20cb297a1d72bbbf07d7
```

## Install on EC2

```bash
sudo -u xko git -C /opt/xko pull --ff-only origin main
sudo bash /opt/xko/deploy/aws-ec2/install-shadow-dashboard.sh
```

Expected terminal markers include:

```text
SHADOW_DASHBOARD_INSTALL_OK safety_flags submit=false unprotected=false market=false
SHADOW_DASHBOARD_INSTALL_OK core_bridge_runtime_unchanged_since_prelive_freeze
SHADOW_DASHBOARD_INSTALL_OK no_approve_or_submit_bridge_route
SHADOW_DASHBOARD_INSTALL_OK port_8766_loopback_only
SHADOW_DASHBOARD_INSTALL_OK state_shadow_submit_capability=false
SHADOW_DASHBOARD_INSTALL_OK review_db_permissions=600
SHADOW_DASHBOARD_INSTALL_OK
```

## Open the dashboard

From the operator's own computer, create an SSH tunnel to EC2:

```bash
ssh -L 8766:127.0.0.1:8766 ubuntu@<EC2_PUBLIC_IP>
```

Keep that terminal open, then browse:

```text
http://127.0.0.1:8766/
```

Because the service is loopback-only, the browser does not need to receive or store `BRIDGE_API_TOKEN`; the local dashboard process uses the existing `/etc/xko/nautilus-bridge.env` server-side.

## Human review semantics

`Mark reviewed (NO TRADE)` is intentionally different from bridge intent approval.

It writes only to:

```text
/var/lib/xko-nautilus/shadow-reviews.db
```

The review contains the selected intent ID, timestamp, operator label, free-text note, intent status, current shadow preview snapshot and bridge health snapshot. It does not mutate the TradeIntent lifecycle and does not authorize execution.

## Operations

```bash
sudo systemctl status xko-shadow-dashboard --no-pager
sudo journalctl -u xko-shadow-dashboard -f
curl -fsS http://127.0.0.1:8766/health
```

To stop the UI without affecting Nautilus:

```bash
sudo systemctl disable --now xko-shadow-dashboard
```

The Nautilus bridge continues independently.
