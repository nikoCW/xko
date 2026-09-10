# Deploy the Nautilus bridge on Render

The production shape is two services:

```text
ChatGPT -> xko MCP web service -> Render private network -> Nautilus bridge private service -> OKX Demo
```

The Nautilus bridge is intentionally a **private service**. It has no public URL.

## Secrets

Set these three values manually on the `xko-nautilus-bridge` service in the Render dashboard:

- `OKX_API_KEY`
- `OKX_API_SECRET`
- `OKX_API_PASSPHRASE`

Do not commit them to GitHub.

The Blueprint generates `BRIDGE_API_TOKEN` on the private service and injects the same secret into the public MCP gateway using `fromService`.

## Demo-only defaults

The Blueprint keeps:

```env
OKX_DEMO=true
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
```

This is deliberate. The first deployment is for health checks and `TradeIntent -> preview` only.

## Persistence

The bridge stores intent/approval/idempotency state in SQLite at:

```text
/var/data/xko-nautilus/intents.db
```

A 1 GB Render persistent disk is mounted at `/var/data/xko-nautilus` so restarts and deploys do not erase execution state.

## First verification

After the private service is healthy, call the MCP tool:

```text
nautilus_health
```

Expected properties:

- `ready: true`
- `okx_environment: DEMO`
- `order_submit_enabled: false`
- `unprotected_entry_enabled: false`

Then create and preview a small intent. Do not enable order submission yet.

## Cost note

Render persistent disks require a paid service. Review the currently selected Render compute/disk pricing before syncing the Blueprint.
