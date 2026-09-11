# xko disaster recovery drill

This runbook validates backup and recovery mechanics without replacing the live database or enabling order submission.

## Safety invariants

The drill refuses to run unless all three flags are exactly `false`:

```text
ALLOW_ORDER_SUBMIT=false
ALLOW_UNPROTECTED_ENTRY=false
ALLOW_MARKET_ENTRY=false
```

It also refuses to restart the bridge if tracked runtime files have changed since the frozen pre-live commit. The only accepted post-freeze changes are the DR verifier, its wrapper, and this document.

The live SQLite database is opened read-only by the drill. SQLite's online backup API writes a consistent snapshot to a root-only temporary workspace. Restore verification happens only against a second temporary copy; the live database is never renamed, overwritten, truncated, or opened writable by the DR verifier.

The environment file, Caddy configuration, and effective systemd unit are copied into the same root-only temporary workspace and byte-compared. Their contents are never printed. The temporary workspace is deleted automatically at exit.

## Run

On EC2:

```bash
sudo -u xko git -C /opt/xko pull --ff-only origin main
sudo -u xko git -C /opt/xko rev-parse --short HEAD
sudo -u xko env PYTHONPATH=/opt/xko/nautilus_bridge \
  /opt/xko/.venv/bin/python -m py_compile \
  /opt/xko/deploy/aws-ec2/disaster_recovery_drill.py
sudo bash /opt/xko/deploy/aws-ec2/test-disaster-recovery-drill.sh
```

The bridge is restarted once, using the unchanged runtime, to prove startup reconciliation and protection recovery. A restart performs normal authenticated read/reconciliation traffic to OKX, but the submit path remains disabled.

## Pass criteria

A passing drill reports all of the following concepts:

```text
DR_OK source_open_mode=read_only
DR_OK online_backup_integrity
DR_OK restored_db_integrity
DR_OK intents_readable=<count>
DR_OK restored_db_matches_backup=true
DR_OK config_recovery_copies_verified
DR_OK service_rebuild_inputs_present
DR_OK post_restart_reconciliation_and_protection
DR_OK post_restart_submit_enabled=false
DR_OK bridge_port_loopback_only_after_restart
DR_OK no_order_submit_path_activity_during_drill
DR_OK live_database_replaced=false
DR_OK live_configuration_replaced=false
DISASTER_RECOVERY_DRILL_OK
```

`intent_status_counts` contains aggregate counts only; the verifier does not print approval hashes, intent payloads, API credentials, or bridge tokens.

## What this proves

The drill proves that the current on-host SQLite state can be snapshotted consistently while the service is live, that a separate restored copy passes `PRAGMA integrity_check`, that every persisted `record_json` can be parsed into the current `IntentRecord` model, that essential configuration can be copied and compared safely, and that the unchanged bridge returns to reconciled/protected health with submission disabled after restart.

## What this does not prove

This is not an off-host durability test. A root EBS failure, account compromise, region failure, or simultaneous loss of the instance and its local volume can still destroy on-host backups. Before real-money enablement, add an encrypted off-host backup strategy (for example an encrypted EBS snapshot or encrypted object storage), define retention and restore ownership, and test restoration onto a replacement host.

It also does not validate live OKX order acceptance. The pre-live freeze still stops before any real venue submission boundary.
