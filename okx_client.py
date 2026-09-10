"""Small signed OKX v5 adapter; never retries mutations."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from rules import RuleViolation, dec


class OKXError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(f"OKX 请求失败，代码 {code}；请核对账户和接口状态")


class OKXClient:
    def __init__(self, *, transport=None):
        self.mode = os.getenv("OKX_MODE", "demo")
        if self.mode not in {"read_only", "demo", "live"}:
            raise ValueError("OKX_MODE must be read_only/demo/live")
        self.base = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")
        if self.base not in {"https://www.okx.com", "https://eea.okx.com", "https://us.okx.com"}:
            raise ValueError("OKX_API_BASE must be an explicitly supported HTTPS OKX API host")
        self.key = os.getenv("OKX_API_KEY", "")
        self.secret = os.getenv("OKX_API_SECRET", "")
        self.passphrase = os.getenv("OKX_API_PASSPHRASE", "")
        self.token = os.getenv("MCP_AUTH_TOKEN", "")
        self.transport = transport
        if any((self.key, self.secret, self.passphrase)):
            if not self.configured or len(self.token) < 32:
                raise ValueError("Private access requires all three OKX credentials and MCP_AUTH_TOKEN >=32 characters")

    @property
    def configured(self):
        return bool(self.key and self.secret and self.passphrase)

    async def request(self, method, path, params=None, body=None, *, private=False):
        if method not in {"GET", "POST"}:
            raise RuleViolation("不支持的方法")
        if method == "POST":
            if path not in {"/api/v5/trade/order", "/api/v5/trade/cancel-order", "/api/v5/trade/cancel-algos"}:
                raise RuleViolation("此服务不提供转账、提现或账户设置变更")
            if self.mode == "read_only":
                raise RuleViolation("只读模式禁止下单")
            private = True
        query = "?" + urlencode(params) if params else ""
        request_path = path + query
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=True) if body is not None else ""
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.mode == "demo":
            headers["x-simulated-trading"] = "1"
        if private:
            if not self.configured or len(self.token) < 32:
                raise RuleViolation("尚未配置安全的 OKX 私有账户连接")
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signature = base64.b64encode(hmac.new(self.secret.encode(), (timestamp + method + request_path + payload).encode(), hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-PASSPHRASE": self.passphrase,
                            "OK-ACCESS-TIMESTAMP": timestamp, "OK-ACCESS-SIGN": signature})
        async with httpx.AsyncClient(timeout=15, transport=self.transport, follow_redirects=False) as client:
            response = await client.request(method, self.base + request_path, headers=headers, content=payload or None)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict) or str(data.get("code")) != "0":
            raise OKXError(str(data.get("code", "malformed")) if isinstance(data, dict) else "malformed")
        rows = data.get("data")
        if not isinstance(rows, list):
            raise OKXError("malformed-data")
        if any(str(row.get("sCode", "0")) != "0" for row in rows if isinstance(row, dict)):
            raise OKXError(next(str(row["sCode"]) for row in rows if str(row.get("sCode", "0")) != "0"))
        return rows

    async def get(self, path, params=None, private=False):
        return await self.request("GET", path, params, private=private)

    async def post(self, path, body):
        return await self.request("POST", path, body=body, private=True)

    async def pages(self, path, params=None, id_field="ordId"):
        params = dict(params or {}, limit="100")
        result, seen = [], set()
        for _ in range(50):
            rows = await self.get(path, params, private=True)
            for row in rows:
                key = row[id_field]
                if key in seen:
                    raise RuleViolation("账户订单分页重复，停止预览")
                seen.add(key)
                result.append(row)
            if len(rows) < 100:
                return result
            params["after"] = rows[-1][id_field]
        raise RuleViolation("账户订单分页超限，不能使用不完整风险数据")

    async def snapshot(self):
        config = (await self.get("/api/v5/account/config", private=True))[0]
        balance = (await self.get("/api/v5/account/balance", private=True))[0]
        positions = await self.get("/api/v5/account/positions", private=True)
        orders = await self.pages("/api/v5/trade/orders-pending")
        algos = []
        for kind in ("conditional", "oco", "trigger", "move_order_stop"):
            algos.extend(await self.pages("/api/v5/trade/orders-algo-pending", {"ordType": kind}, "algoId"))
        return {"mode": self.mode, "config": config, "balance": balance,
                "positions": [p for p in positions if dec(p.get("pos", "0")) != 0],
                "orders": orders, "algos": algos}

    async def instrument(self, inst_id):
        parts = inst_id.split("-")
        kind = "SWAP" if inst_id.endswith("-SWAP") else "FUTURES" if len(parts) == 3 else "SPOT"
        rows = await self.get("/api/v5/public/instruments", {"instType": kind, "instId": inst_id})
        if len(rows) != 1 or rows[0].get("instId") != inst_id:
            raise RuleViolation("无法取得唯一且匹配的交易规格")
        return rows[0]

    async def ticker(self, inst_id):
        rows = await self.get("/api/v5/market/ticker", {"instId": inst_id})
        if len(rows) != 1 or rows[0].get("instId") != inst_id:
            raise RuleViolation("无法取得匹配报价")
        return rows[0]

    async def candles(self, inst_id, bar, limit=60):
        rows = await self.get("/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
        return [{"ts": int(c[0]), "open": c[1], "high": c[2], "low": c[3], "close": c[4],
                 "volume": c[5], "quoteVolume": c[7], "confirmed": str(c[8]) == "1"}
                for c in rows if len(c) >= 9]

    async def fee(self, spec):
        params = {"instType": spec["instType"]}
        params["instId" if spec["instType"] == "SPOT" else "instFamily"] = spec["instId"] if spec["instType"] == "SPOT" else spec["instFamily"]
        row = (await self.get("/api/v5/account/trade-fee", params, private=True))[0]
        return abs(dec(row["taker"]))
