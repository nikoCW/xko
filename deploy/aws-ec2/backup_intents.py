from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


REQUIRED_COLUMNS = {"intent_id", "client_order_id", "record_json", "approval_hash"}


def ro_conn(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.execute("PRAGMA query_only=ON")
    return conn


def integrity_ok(path: Path) -> None:
    with ro_conn(path) as conn:
        result = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
        if result != ["ok"]:
            raise RuntimeError(f"integrity_check failed: {result}")
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(intents)").fetchall()}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise RuntimeError(f"intents schema missing columns: {sorted(missing)}")
        # Force record payloads to be readable without printing their contents.
        rows = conn.execute("SELECT record_json FROM intents").fetchall()
        for row in rows:
            if not isinstance(row[0], str) or not row[0]:
                raise RuntimeError("invalid record_json payload")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(source: Path, destination: Path) -> None:
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    try:
        with ro_conn(source) as source_conn:
            with sqlite3.connect(tmp, timeout=10) as target_conn:
                source_conn.backup(target_conn)
        tmp.chmod(0o600)
        integrity_ok(tmp)
        os.replace(tmp, destination)
        destination.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def prune(directory: Path, keep: int) -> int:
    backups = sorted(directory.glob("intents-*.db"), key=lambda p: p.name, reverse=True)
    removed = 0
    for old in backups[keep:]:
        old.unlink()
        checksum = old.with_suffix(old.suffix + ".sha256")
        if checksum.exists():
            checksum.unlink()
        removed += 1
    return removed


def main() -> int:
    source = Path(os.getenv("INTENT_DB_PATH", "/var/lib/xko-nautilus/intents.db"))
    backup_dir = Path(os.getenv("XKO_BACKUP_DIR", "/var/backups/xko-nautilus"))
    keep = int(os.getenv("XKO_BACKUP_KEEP", "14"))

    if keep < 2 or keep > 365:
        raise SystemExit("BACKUP_FAIL XKO_BACKUP_KEEP must be between 2 and 365")
    if not source.is_file() or source.is_symlink():
        raise SystemExit("BACKUP_FAIL source database missing or unsafe")

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o750)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"intents-{stamp}.db"
    if destination.exists():
        raise SystemExit("BACKUP_FAIL destination already exists")

    create_backup(source, destination)
    digest = sha256(destination)
    checksum_path = destination.with_suffix(destination.suffix + ".sha256")
    checksum_path.write_text(f"{digest}  {destination.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)

    removed = prune(backup_dir, keep)
    # Deliberately print no intent IDs, record bodies, approval hashes, or credentials.
    print(f"BACKUP_OK file={destination.name}")
    print("BACKUP_OK integrity_check=ok")
    print(f"BACKUP_OK sha256={digest}")
    print(f"BACKUP_OK retention_keep={keep} pruned={removed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"BACKUP_FAIL {type(exc).__name__}:{exc}", file=sys.stderr)
        raise
