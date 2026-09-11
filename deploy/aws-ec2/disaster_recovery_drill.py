from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from trading_models import IntentRecord


REQUIRED_COLUMNS = {"intent_id", "client_order_id", "record_json", "approval_hash"}


def read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def integrity_check(conn: sqlite3.Connection) -> None:
    rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
    if rows != ["ok"]:
        raise RuntimeError(f"SQLite integrity_check failed: {rows}")


def validate_schema(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError(f"intents schema missing columns: {sorted(missing)}")


def read_snapshot(conn: sqlite3.Connection) -> tuple[list[IntentRecord], str]:
    rows = conn.execute(
        "SELECT intent_id, client_order_id, record_json, approval_hash FROM intents ORDER BY intent_id"
    ).fetchall()

    records: list[IntentRecord] = []
    digest = hashlib.sha256()
    for row in rows:
        record = IntentRecord.model_validate_json(row["record_json"])
        if record.intent_id != row["intent_id"]:
            raise RuntimeError(f"intent_id mismatch for {row['intent_id']}")
        if record.client_order_id != row["client_order_id"]:
            raise RuntimeError(f"client_order_id mismatch for {row['intent_id']}")
        records.append(record)
        # Digest the exact persisted row without ever printing approval hashes or record JSON.
        for value in (
            row["intent_id"],
            row["client_order_id"],
            row["record_json"],
            row["approval_hash"],
        ):
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)

    return records, digest.hexdigest()


def create_online_backup(source: Path, backup: Path) -> None:
    # The source is opened SQLite read-only. sqlite3.Connection.backup() gives a consistent
    # online snapshot even while the live bridge remains up; this process cannot write source.
    with read_only_connection(source) as source_conn:
        with sqlite3.connect(backup, timeout=5) as backup_conn:
            source_conn.backup(backup_conn)


def validate_database(path: Path) -> tuple[list[IntentRecord], str]:
    with read_only_connection(path) as conn:
        integrity_check(conn)
        validate_schema(conn)
        return read_snapshot(conn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only xko SQLite DR backup/restore drill")
    parser.add_argument("--source", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--restore", required=True)
    args = parser.parse_args()

    source = Path(args.source)
    backup = Path(args.backup)
    restore = Path(args.restore)

    if not source.is_file():
        raise SystemExit(f"DR_FAIL source database missing: {source}")
    if source.is_symlink():
        raise SystemExit(f"DR_FAIL refusing symlink source database: {source}")
    if backup.exists() or restore.exists():
        raise SystemExit("DR_FAIL backup/restore target already exists")
    if source.resolve() in {backup.resolve(), restore.resolve()}:
        raise SystemExit("DR_FAIL backup/restore target resolves to live database")

    backup.parent.mkdir(parents=True, exist_ok=True)
    restore.parent.mkdir(parents=True, exist_ok=True)

    create_online_backup(source, backup)
    backup.chmod(0o600)
    backup_records, backup_digest = validate_database(backup)
    print("DR_OK source_open_mode=read_only")
    print("DR_OK online_backup_integrity")

    # Simulate restoring the backup into a completely separate path. The live DB is never
    # replaced, renamed, truncated, or opened writable by this drill.
    shutil.copy2(backup, restore)
    restore.chmod(0o600)
    restored_records, restored_digest = validate_database(restore)

    if backup_digest != restored_digest:
        raise SystemExit("DR_FAIL restored database differs from backup snapshot")
    if len(backup_records) != len(restored_records):
        raise SystemExit("DR_FAIL restored intent count differs from backup snapshot")

    statuses = Counter(str(record.status) for record in restored_records)
    # JSON here contains counts only; no intent IDs, approval hashes, credentials, or tokens.
    print("DR_OK restored_db_integrity")
    print(f"DR_OK intents_readable={len(restored_records)}")
    print("DR_OK intent_status_counts=" + json.dumps(dict(sorted(statuses.items())), separators=(",", ":")))
    print("DR_OK restored_db_matches_backup=true")
    print("DR_OK live_database_replacement_performed=false")


if __name__ == "__main__":
    main()
