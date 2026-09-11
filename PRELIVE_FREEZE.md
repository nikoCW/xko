# xko pre-live safety freeze

Status: **PRE-LIVE / ORDER SUBMISSION MUST REMAIN DISABLED**

This freeze records the last runtime baseline that completed the no-money validation chain and adds operational safety tooling around it. It does **not** authorize live order submission and it does not claim real OKX venue acceptance has been tested.

## Frozen runtime baseline

Fully dry-run-tested runtime baseline:

```text
4e76f932f79bdfad2258c863e7c3fdac64656c75
```

Validation completed against this runtime lineage:

- exact OKX SWAP market reference and contract-aware sizing;
- per-instrument quantity and notional caps;
- NautilusTrader 1.231 protected bracket construction;
- deterministic parent/SL/TP client IDs;
- attached SL/TP reduce-only, opposite-side, full-quantity coverage;
- external/manual/grid ownership isolation;
- target-instrument-must-be-flat submit policy;
- restart/reconciliation dry validation;
- `/submit` hard lock returning HTTP 403 before store mutation or command queueing;
- OKX adapter `attachAlgoOrds` serialization with `sl_ord_px=-1`, `tp_ord_px=-1`, trigger type `last`;
- zero-network payload dry-run;
- fail-closed fault injection for missing stop, wrong stop side, undercoverage, protection-scan exception, persistent protection failure, and reconciliation loss/reconnect.

## Required safety configuration

These values remain mandatory for this pre-live freeze:

```text
OKX_DEMO=false
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

`trading_ready=true` means operational/reconciliation/protection readiness only. It never overrides `ALLOW_ORDER_SUBMIT=false`.

## Operator commands

Run the full host audit after pulling the freeze tooling:

```bash
sudo bash /opt/xko/deploy/aws-ec2/prelive-audit.sh
```

Emergency submission kill switch (idempotent):

```bash
sudo bash /opt/xko/deploy/aws-ec2/emergency-disable-submit.sh
```

The kill switch creates a root-only backup of the env file, forces all three submission/entry policy flags to `false`, restarts the bridge, and verifies the health endpoint reports submission disabled. It does not stop the bridge, so reconciliation/protection monitoring can continue.

Rollback to the fully tested runtime baseline:

```bash
sudo bash /opt/xko/deploy/aws-ec2/rollback-prelive.sh
```

Rollback first runs the kill switch, refuses a dirty worktree or an unknown/non-ancestor target, resets to the tested runtime baseline, compiles the core strategy modules, restarts the service, and verifies submission remains disabled.

## Release reference

The repository should keep a stable branch named:

```text
prelive-rc1
```

That branch is a pin for this freeze and must not be advanced casually. `main` may continue to receive changes, so production automation must not be treated as frozen merely because `prelive-rc1` exists.

## Render gateway audit

The MCP gateway is the existing Render Free service `okx-live-trader-mcp`, using repository `nikoCW/xko`, branch `main`, with commit-triggered auto-deploy. This means Render is **not hard-frozen while auto-deploy remains enabled on `main`**.

Before any future live-money enablement, either disable Render auto-deploy or pin the Render service to a reviewed release branch/ref. Do not create a second bridge service and do not move OKX credentials into Render.

## Network invariants

- Caddy is the public TLS endpoint at `bridge.the3111.xyz`.
- Nautilus bridge port `8765` must listen on loopback only.
- AWS inbound should expose only the intentionally public/admin ports; never expose `8765`.
- Non-health bridge endpoints remain Bearer-token protected.

## Live enablement blockers

Do **not** set `ALLOW_ORDER_SUBMIT=true` until a venue-safe test environment exists and all of the following are validated end-to-end at OKX:

1. parent order acknowledgement;
2. attached SL + TP acknowledgement and reporting;
3. entry fill behavior;
4. protective trigger/fill behavior;
5. OCO sibling cancellation/reporting;
6. restart/reconciliation with an actual XKO-owned open position and attached protection;
7. fail-closed behavior against real reconciled venue state.

With only real-money credentials available, the current freeze intentionally stops before this boundary.
