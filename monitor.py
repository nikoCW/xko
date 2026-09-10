"""Read-only background monitoring with durable, deduplicated notifications."""
import asyncio
import json
import logging
import os
import time
import uuid

import httpx

from rules import RuleViolation, dec, positive, quote_price
from trading import account_values

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.webhook = os.getenv("ALERT_WEBHOOK_URL", "")
        if self.webhook and not self.webhook.startswith("https://"):
            raise ValueError("ALERT_WEBHOOK_URL requires HTTPS")

    def add(self, inst_id, condition, threshold, cooldown=900):
        if condition not in {"above", "below"} or not 60 <= cooldown <= 86400:
            raise RuleViolation("提醒条件为above/below，冷却时间60至86400秒")
        identity = uuid.uuid4().hex
        value = str(positive(threshold))
        with self.store.connection() as db:
            if db.execute("SELECT COUNT(*) FROM alerts WHERE enabled=1").fetchone()[0] >= 20:
                raise RuleViolation("最多20条启用提醒，避免行情请求超限")
            db.execute("INSERT INTO alerts (id,inst,condition,threshold,cooldown) VALUES (?,?,?,?,?)", (identity, inst_id, condition, value, cooldown))
        return {"alertId": identity, "instId": inst_id, "condition": condition, "threshold": value,
                "delivery": "webhook" if self.webhook else "local_event_log", "startsOnNextPoll": True}

    def list(self):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM alerts ORDER BY id")]

    def enable(self, identity, enabled):
        with self.store.connection() as db:
            if enabled and db.execute("SELECT COUNT(*) FROM alerts WHERE enabled=1 AND id<>?", (identity,)).fetchone()[0] >= 20:
                raise RuleViolation("最多20条启用提醒")
            cur = db.execute("UPDATE alerts SET enabled=? WHERE id=?", (int(enabled), identity))
            if cur.rowcount != 1:
                raise RuleViolation("提醒不存在")
        return {"alertId": identity, "enabled": enabled}

    def observe(self, alert, price, now=None):
        now = time.time() if now is None else now
        truth = price >= positive(alert["threshold"]) if alert["condition"] == "above" else price <= positive(alert["threshold"])
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM alerts WHERE id=? AND enabled=1", (alert["id"],)).fetchone()
            if not row:
                return
            if truth and not row["was_true"] and now - row["last_fired"] >= row["cooldown"]:
                event = {"alertId": row["id"], "instId": row["inst"], "condition": row["condition"], "threshold": row["threshold"], "price": str(price)}
                db.execute("INSERT INTO events (id,created,kind,payload) VALUES (?,?,?,?)", (uuid.uuid4().hex, now, "price_alert", json.dumps(event)))
                db.execute("UPDATE alerts SET last_fired=?,was_true=1 WHERE id=?", (now, row["id"]))
            elif not truth:
                db.execute("UPDATE alerts SET was_true=0 WHERE id=?", (row["id"],))

    async def once(self):
        # One polling lease per database, surviving multiple ASGI processes.
        now = time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key='monitor_lease'").fetchone()
            if row and float(row[0]) > now:
                return
            # 20 instruments * 15s timeout + reconciliation bounded below.
            db.execute("INSERT OR REPLACE INTO kv VALUES ('monitor_lease',?)", (str(now + 900),))
        try:
            tickers = {}
            for alert in self.list():
                if not alert["enabled"]:
                    continue
                try:
                    inst = alert["inst"]
                    if inst not in tickers:
                        tickers[inst] = await self.service.client.ticker(inst)
                    ticker = tickers[inst]
                    quote_price(ticker, "sell", opening=False)
                    self.observe(alert, positive(ticker["last"]))
                except Exception:
                    self.store.event("market_data_error", {"instId": alert["inst"]}, key=f"market:{alert['inst']}:{int(now // 900)}")
            if self.service.client.configured:
                try:
                    await self.account_risks(now)
                except Exception:
                    self.store.event("account_data_error", {"message": "账户监控未完成，不能证明当前风险正常"}, key=f"account-error:{int(now // 900)}")
                for identity in self.store.unresolved()[:10]:
                    # In-flight submissions are not queried by the worker.
                    plan = self.store.plan(identity)
                    if plan["state"] == "submitting" and now < plan["expires"] + 30:
                        continue
                    await self.service.reconcile(identity)
            await self.deliver()
            self.store.set("monitor_last_success", time.time())
        finally:
            self.store.set("monitor_lease", 0)

    async def account_risks(self, now):
        snapshot = await self.service.client.snapshot()
        equity, available = account_values(snapshot)
        bucket = int(now // 900)
        try:
            self.store.daily_guard(equity)
        except RuleViolation:
            self.store.set("paused", True)
            self.store.event("daily_drawdown_pause", {"equityUSDT": str(equity)}, key=f"drawdown:{bucket}")
        if available / equity < dec("0.30"):
            self.store.event("cash_reserve_low", {"cashRatio": str(available / equity)}, key=f"cash:{bucket}")
        for position in snapshot["positions"]:
            inst, amount = position["instId"], dec(position["pos"])
            if position.get("instType") not in {"SWAP", "FUTURES"}:
                continue
            side = "sell" if amount > 0 else "buy"
            covered = any(a.get("instId") == inst and a.get("side") == side
                and a.get("posSide") in {"net", None, ""} and a.get("tdMode") == position.get("mgnMode")
                and dec(a.get("slTriggerPx") or "0") > 0 and dec(a.get("sz") or "0") >= abs(amount)
                for a in snapshot["algos"])
            if position.get("posSide") != "net" or position.get("mgnMode") != "isolated" or not covered:
                self.store.set("paused", True)
                self.store.event("position_protection_unverified", {"instId": inst}, key=f"position-protection:{inst}:{bucket}")
            mark, liquidation = dec(position.get("markPx") or "0"), dec(position.get("liqPx") or "0")
            if mark > 0 and liquidation > 0 and abs(mark - liquidation) / mark < dec("0.05"):
                self.store.set("paused", True)
                self.store.event("liquidation_near", {"instId": inst, "markPx": str(mark), "liqPx": str(liquidation)}, key=f"liquidation:{inst}:{bucket}")

    async def deliver(self):
        # No webhook configured means events remain available through get_events.
        if not self.webhook:
            return
        for event in self.store.events(limit=20, pending=True):
            headers = {"Idempotency-Key": event["id"]}
            token = os.getenv("ALERT_WEBHOOK_TOKEN", "")
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.post(self.webhook, headers=headers, json={k: event[k] for k in ("id", "created", "kind", "payload")})
                    response.raise_for_status()
                delivered = 1
            except Exception:
                delivered = 0
                log.warning("Alert delivery failed for event %s; retained for retry", event["id"])
            with self.store.connection() as db:
                db.execute("UPDATE events SET delivered=?,attempts=attempts+1 WHERE id=?", (delivered, event["id"]))

    async def run(self):
        interval = max(60, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")))
        while True:
            try:
                await self.once()
            except Exception:
                log.error("Monitor cycle failed; next cycle will retry")
            await asyncio.sleep(interval)
