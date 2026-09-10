"""SQLite journal: durable previews, atomic submission claims and alert outbox."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from rules import POLICY, RuleViolation, dec, fmt


class Store:
    def __init__(self, path=None):
        self.path = path or os.getenv("STATE_DB", "data/trader.sqlite3")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, result TEXT);
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY, inst TEXT NOT NULL, condition TEXT NOT NULL,
                    threshold TEXT NOT NULL, cooldown INTEGER NOT NULL, last_fired REAL NOT NULL DEFAULT 0,
                    was_true INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connection() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))

    def create_plan(self, payload):
        identity = uuid.uuid4().hex
        now = time.time()
        with self.connection() as db:
            db.execute("INSERT INTO plans VALUES (?,?,?,?,?,NULL)", (identity, now, now + POLICY.preview_ttl_seconds, "preview", json.dumps(payload, sort_keys=True)))
        return self.plan(identity)

    def plan(self, identity):
        with self.connection() as db:
            row = db.execute("SELECT * FROM plans WHERE id=?", (identity,)).fetchone()
        if not row:
            raise RuleViolation("预览不存在")
        return {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"]) if row["result"] else None}

    def claim(self, identity):
        # BEGIN IMMEDIATE serializes all claims, including across processes.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM plans WHERE state IN ('submitting','unknown')").fetchone():
                raise RuleViolation("另一执行仍在核对中，请先查询状态")
            cursor = db.execute("UPDATE plans SET state='submitting' WHERE id=? AND state='preview' AND expires>?", (identity, time.time()))
            if cursor.rowcount != 1:
                raise RuleViolation("预览已过期或已消费，禁止重复执行")

    def finish(self, identity, state, result):
        with self.connection() as db:
            db.execute("UPDATE plans SET state=?, result=? WHERE id=?", (state, json.dumps(result), identity))

    def unresolved(self):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT id FROM plans WHERE state IN ('submitting','unknown','protection_pending')")]

    def daily_guard(self, equity):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            key = "equity:" + day
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            high = max(dec(row[0]) if row else dec(equity), dec(equity))
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, fmt(high)))
        if dec(equity) <= high * (1 - dec(POLICY.daily_drawdown)):
            raise RuleViolation("当日观测净值较高水位回撤达到3%，暂停开仓")

    def event(self, kind, payload, key=None):
        identity = hashlib.sha256(key.encode()).hexdigest() if key else uuid.uuid4().hex
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO events (id,created,kind,payload) VALUES (?,?,?,?)", (identity, time.time(), kind, json.dumps(payload)))
        return identity

    def events(self, limit=50, pending=False):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events " + ("WHERE delivered=0 " if pending else "") + "ORDER BY created DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]
