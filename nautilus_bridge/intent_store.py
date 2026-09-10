from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import uuid
from pathlib import Path

from trading_models import IntentRecord, IntentStatus, TradeIntentCreate


class IntentStore:
    """Small durable intent registry; approval codes are stored only as hashes."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    record_json TEXT NOT NULL,
                    approval_hash TEXT NOT NULL
                )
                """
            )
            conn.commit()

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_client_order_id() -> str:
        return "XKO" + uuid.uuid4().hex[:24].upper()

    def create(self, request: TradeIntentCreate) -> tuple[IntentRecord, str]:
        intent_id = str(uuid.uuid4())
        now = IntentRecord.now()
        record = IntentRecord(
            intent_id=intent_id,
            client_order_id=self._new_client_order_id(),
            status=IntentStatus.CREATED,
            created_at=now,
            updated_at=now,
            request=request,
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        approval_hash = self._hash_code(code)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO intents(intent_id, client_order_id, record_json, approval_hash) VALUES (?, ?, ?, ?)",
                (record.intent_id, record.client_order_id, record.model_dump_json(), approval_hash),
            )
            conn.commit()
        return record, code

    def get(self, intent_id: str) -> IntentRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown intent_id: {intent_id}")
        return IntentRecord.model_validate_json(row["record_json"])

    def list_recent(self, limit: int = 50) -> list[IntentRecord]:
        limit = max(1, min(limit, 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM intents ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [IntentRecord.model_validate_json(r["record_json"]) for r in rows]

    def _save(self, record: IntentRecord) -> IntentRecord:
        record.updated_at = IntentRecord.now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE intents SET record_json = ? WHERE intent_id = ?",
                (record.model_dump_json(), record.intent_id),
            )
            if cur.rowcount != 1:
                raise KeyError(f"Unknown intent_id: {record.intent_id}")
            conn.commit()
        return record

    def verify_and_approve(self, intent_id: str, approval_code: str) -> IntentRecord:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT record_json, approval_hash FROM intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown intent_id: {intent_id}")
        record = IntentRecord.model_validate_json(row["record_json"])
        if record.status in {
            IntentStatus.APPROVED, IntentStatus.QUEUED, IntentStatus.SUBMITTED,
            IntentStatus.ACCEPTED, IntentStatus.PARTIALLY_FILLED, IntentStatus.FILLED,
        }:
            return record
        if record.status != IntentStatus.CREATED:
            raise ValueError(f"Cannot approve intent in status {record.status}")
        supplied = self._hash_code(approval_code.strip())
        if not hmac.compare_digest(supplied, row["approval_hash"]):
            raise PermissionError("Invalid approval code")
        record.status = IntentStatus.APPROVED
        record.approved_at = IntentRecord.now()
        return self._save(record)

    def mark_queued(self, intent_id: str) -> IntentRecord:
        record = self.get(intent_id)
        if record.status in {
            IntentStatus.QUEUED, IntentStatus.SUBMITTED, IntentStatus.ACCEPTED,
            IntentStatus.PARTIALLY_FILLED, IntentStatus.FILLED,
        }:
            return record
        if record.status != IntentStatus.APPROVED:
            raise ValueError(f"Intent must be APPROVED before submit; got {record.status}")
        record.status = IntentStatus.QUEUED
        return self._save(record)

    def update_execution(
        self,
        intent_id: str,
        *,
        status: IntentStatus | None = None,
        sized_quantity: str | None = None,
        equity_used: str | None = None,
        risk_fraction_used: str | None = None,
        nautilus_order_id: str | None = None,
        last_event: str | None = None,
        error: str | None = None,
        mark_submitted: bool = False,
    ) -> IntentRecord:
        record = self.get(intent_id)
        if status is not None:
            record.status = status
        if sized_quantity is not None:
            record.sized_quantity = sized_quantity
        if equity_used is not None:
            record.equity_used = equity_used
        if risk_fraction_used is not None:
            record.risk_fraction_used = risk_fraction_used
        if nautilus_order_id is not None:
            record.nautilus_order_id = nautilus_order_id
        if last_event is not None:
            record.last_event = last_event
        if error is not None:
            record.error = error
        if mark_submitted:
            record.submitted_at = IntentRecord.now()
        return self._save(record)
