"""Preview -> revalidate -> claim once -> submit -> reconcile; no autonomous writes."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from decimal import Decimal

from okx_client import OKXClient, OKXError
from rules import (POLICY, RuleViolation, breakout_signal, dec, exact_step, fmt,
                   instrument_kind, positive, quote_price, size_trade)
from state import Store


def account_values(snapshot):
    details = snapshot["balance"]["details"]
    usdt = next((d for d in details if d["ccy"] == "USDT"), None)
    if not usdt:
        raise RuleViolation("账户无 USDT 余额记录")
    # totalEq is USD; use the exchange's USDT USD valuation to convert.
    rate = positive(usdt["eqUsd"]) / positive(usdt["eq"])
    equity = positive(snapshot["balance"]["totalEq"]) / rate
    available = dec(usdt["availBal"])
    if usdt.get("availEq") not in {None, ""}:
        available = min(available, dec(usdt["availEq"]))
    return equity, available


def fingerprint(snapshot):
    # Ignore mark-to-market fields; preserve every exposure/order quantity and state.
    data = {
        "mode": snapshot["mode"],
        "config": {k: snapshot["config"].get(k) for k in ("uid", "acctLv", "posMode", "autoLoan")},
        "positions": sorted([ {k: p.get(k) for k in ("instId", "posSide", "pos", "mgnMode", "lever")} for p in snapshot["positions"]], key=lambda x: json.dumps(x, sort_keys=True)),
        "orders": sorted([{k: p.get(k) for k in ("ordId", "instId", "sz", "accFillSz", "state", "px", "side")} for p in snapshot["orders"]], key=lambda x: str(x["ordId"])),
        "algos": sorted([{k: p.get(k) for k in ("algoId", "instId", "sz", "state", "side", "slTriggerPx", "tpTriggerPx")} for p in snapshot["algos"]], key=lambda x: str(x["algoId"]))}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class TradingService:
    def __init__(self, client=None, store=None):
        self.client = client or OKXClient()
        self.store = store or Store()

    async def signal(self, inst, direction):
        # Sequential requests avoid bursts against shared market-data rate limits.
        bars = [await self.client.candles(inst, bar) for bar in ("15m", "1H", "4H")]
        return breakout_signal(*bars, direction)

    async def account(self):
        snapshot = await self.client.snapshot()
        equity, available = account_values(snapshot)
        return {"mode": self.client.mode, "equityUSDT": fmt(equity), "availableUSDT": fmt(available),
                "cashRatio": fmt(available / equity), "positions": snapshot["positions"],
                "orders": snapshot["orders"], "algoOrders": snapshot["algos"],
                "paused": self.store.get("paused", False), "unresolvedPreviews": self.store.unresolved(),
                "scope": "交易账户；非资金账户。外部机器人分配通过余额 stgyEq 检查，不能管理其网格。"}

    async def _build(self, request, existing=None):
        inst, intent = request["instId"], request["intent"]
        if intent not in {"enter_long", "enter_short", "exit_long", "exit_short"}:
            raise RuleViolation("无效的交易意图")
        opening, direction = intent.startswith("enter"), intent.split("_")[1]
        side = "buy" if intent in {"enter_long", "exit_short"} else "sell"
        spec = await self.client.instrument(inst)
        kind = instrument_kind(spec)
        if kind == "SPOT" and direction == "short":
            raise RuleViolation("现货禁止做空")
        ticker = await self.client.ticker(inst)
        quote = quote_price(ticker, side, opening=opening)
        entry = exact_step(request["price"], spec["tickSz"], "委托价")
        # Only bounded fill-or-kill orders: no forgotten entry or unprotected partial fill.
        if abs(entry / quote - 1) > dec(POLICY.max_price_drift):
            raise RuleViolation("委托价距可成交报价超过0.5%，请按新报价重新预览")
        if (side == "buy" and entry < quote) or (side == "sell" and entry > quote):
            raise RuleViolation("FOK价格当前不可成交，请重新预览；不会自动改价")
        snapshot = await self.client.snapshot()
        equity, available = account_values(snapshot) if opening else (None, None)
        if opening and snapshot["config"].get("autoLoan") is True:
            raise RuleViolation("请先在OKX关闭自动借币；服务不会替你更改账户模式")
        if kind != "SPOT":
            if snapshot["config"].get("posMode") != "net_mode" or str(snapshot["config"].get("acctLv")) not in {"2", "3"}:
                raise RuleViolation("合约执行仅支持单向持仓的合约/跨币种保证金账户模式")
            leverage = request["leverage"]
            leverage_rows = await self.client.get("/api/v5/account/leverage-info", {"instId": inst, "mgnMode": "isolated"}, private=True)
            if not leverage_rows or any(dec(r["lever"]) != leverage for r in leverage_rows):
                raise RuleViolation("OKX当前逐仓杠杆与预览不符；请先自行设置后重新预览")
        else:
            leverage = 1
            if request["leverage"] != 1:
                raise RuleViolation("现货杠杆必须为1")
        fee = await self.client.fee(spec)
        signal, sizing = None, None
        if opening:
            if self.store.get("paused", False):
                raise RuleViolation("已暂停新增风险；减仓和状态核对仍可用")
            # Existing submitting preview is this request during revalidation.
            unresolved = [p for p in self.store.unresolved() if p != existing]
            if unresolved:
                raise RuleViolation("存在未核对的订单或保护单，禁止新增风险")
            self.store.daily_guard(equity)
            if time.time() - self.store.get("last-entry:" + inst, 0) < POLICY.cooldown_seconds:
                raise RuleViolation("同标的开仓冷却15分钟")
            if snapshot["positions"] or snapshot["orders"] or snapshot["algos"]:
                raise RuleViolation("保守首版要求无未平合约、普通挂单或策略挂单后才能新增仓位；先处理已有风险")
            for detail in snapshot["balance"]["details"]:
                if dec(detail.get("stgyEq") or "0") > 0 or dec(detail.get("liab") or "0") > 0:
                    raise RuleViolation("已有机器人资金或负债，不能完整验证风险，暂停新增仓位")
                if detail["ccy"] == inst.split("-")[0] and dec(detail.get("cashBal") or "0") > 0:
                    raise RuleViolation("已有同币种库存，禁止重叠加仓")
            listed = dec(spec.get("listTime")) / 1000
            if dec(time.time()) - listed < POLICY.min_listing_days * 86400:
                raise RuleViolation("上市不足7天")
            if kind == "FUTURES" and dec(spec.get("expTime")) / 1000 - dec(time.time()) < 7 * 86400:
                raise RuleViolation("交割合约距到期不足7天，暂停开仓")
            # Spot turnover is quote currency; contract ticker volume has different units.
            spot_inst = inst.split("-")[0] + "-USDT"
            spot_ticker = ticker if kind == "SPOT" else await self.client.ticker(spot_inst)
            quote_price(spot_ticker, "buy")
            if dec(spot_ticker.get("volCcy24h")) < dec(POLICY.min_quote_volume):
                raise RuleViolation("对应现货24小时成交额不足500万USDT")
            signal = await self.signal(inst, direction)
            if not signal["eligible"]:
                raise RuleViolation("；".join(signal["reasons"]))
            stop = positive(request["stop"])
            structural = positive(signal["structuralStop"])
            if (direction == "long" and stop > structural) or (direction == "short" and stop < structural):
                raise RuleViolation("止损位于结构失效位内侧，容易被正常回踩触发")
            if kind != "SPOT":
                funding = await self.client.get("/api/v5/public/funding-rate", {"instId": inst}) if kind == "SWAP" else []
                if funding and abs(dec(funding[0]["fundingRate"])) > Decimal("0.001"):
                    raise RuleViolation("当前资金费率绝对值超过0.1%，暂停新增风险")
            sizing = size_trade(spec, equity, available, entry, request["stop"], request["target"], direction, leverage, fee, request.get("size"))
            quantity = sizing["quantity"]
        else:
            quantity = fmt(exact_step(request.get("size"), spec["lotSz"], "平仓数量"))
            if dec(quantity) < positive(spec["minSz"]):
                raise RuleViolation("平仓数量低于交易所最小量")
            if kind == "SPOT":
                holding = next((d for d in snapshot["balance"]["details"] if d["ccy"] == inst.split("-")[0]), {})
                if dec(quantity) > dec(holding.get("availBal") or "0"):
                    raise RuleViolation("现货卖出超过可用库存")
                if any(a["instId"] == inst for a in snapshot["algos"]):
                    raise RuleViolation("现货存在策略卖单，请先核对/撤销相关策略单后重新预览")
            else:
                positions = [p for p in snapshot["positions"] if p["instId"] == inst and p["mgnMode"] == "isolated" and p["posSide"] == "net"]
                signed = sum(dec(p["pos"]) for p in positions)
                if (direction == "long" and signed <= 0) or (direction == "short" and signed >= 0) or dec(quantity) > abs(signed):
                    raise RuleViolation("平仓方向或数量与当前逐仓净持仓不符")
        order = {"instId": inst, "tdMode": "cash" if kind == "SPOT" else "isolated",
                 "side": side, "ordType": "fok", "px": fmt(entry), "sz": quantity,
                 "clOrdId": "xko" + uuid.uuid4().hex[:28], "stpMode": "cancel_taker"}
        if kind != "SPOT":
            order["posSide"] = "net"
            if not opening:
                order["reduceOnly"] = True
        if opening:
            order["attachAlgoOrds"] = [{"attachAlgoClOrdId": "xkos" + uuid.uuid4().hex[:27],
                "slTriggerPx": fmt(positive(request["stop"])), "slOrdPx": "-1", "slTriggerPxType": "last",
                "tpTriggerPx": fmt(positive(request["target"])), "tpOrdPx": "-1", "tpTriggerPxType": "last"}]
        return {"action": "order", "mode": self.client.mode, "request": request, "order": order,
                "quote": fmt(quote), "accountFingerprint": fingerprint(snapshot), "spec": spec,
                "risk": sizing, "signal": signal, "equityUSDT": fmt(equity) if equity is not None else None,
                "notes": ["FOK全成或全撤；API受理不代表成交", "附带止盈止损须在成交后核对激活；交易所拒绝时不能降级成无保护订单", "止损可能滑点或跳空；估算不是最大损失保证", "人工确认前不会发单"]}

    async def preview(self, inst_id, intent, price, stop=None, target=None, size=None, leverage=1):
        if self.client.mode == "read_only":
            raise RuleViolation("只读模式不能建立执行预览")
        request = {"instId": inst_id.upper().strip(), "intent": intent, "price": price,
                   "stop": stop, "target": target, "size": size, "leverage": leverage}
        plan = self.store.create_plan(await self._build(request))
        return self.public_preview(plan)

    def public_preview(self, plan):
        payload = dict(plan["payload"])
        payload.pop("accountFingerprint", None)
        payload.pop("spec", None)
        return {"previewId": plan["id"], "expiresAt": plan["expires"], "state": plan["state"],
                **payload, "confirmation": "确认执行 " + plan["id"]}

    async def preview_cancel(self, inst_id, order_id, algo=False):
        snapshot = await self.client.snapshot()
        rows = snapshot["algos"] if algo else snapshot["orders"]
        field = "algoId" if algo else "ordId"
        target = next((o for o in rows if o[field] == order_id and o["instId"] == inst_id), None)
        if not target:
            raise RuleViolation("指定订单不是当前有效挂单")
        body = {"instId": inst_id, field: order_id}
        payload = {"action": "cancel_algo" if algo else "cancel", "mode": self.client.mode,
                   "order": body, "currentOrder": target,
                   "warning": "撤销止损可能使持仓失去保护；确认撤单将暂停新增风险，需重新核对账户"}
        return self.public_preview(self.store.create_plan(payload))

    async def execute(self, identity, confirmation):
        if confirmation != "确认执行 " + identity:
            raise RuleViolation("需要用户对本次完整预览作精确确认")
        plan = self.store.plan(identity)
        if plan["state"] != "preview":
            return await self.reconcile(identity)
        payload = plan["payload"]
        if payload["mode"] != self.client.mode or self.client.mode == "read_only":
            raise RuleViolation("运行模式已变化或为只读，请重新预览")
        self.store.claim(identity)
        try:
            if payload["action"] == "order":
                fresh = await self._build(payload["request"], existing=identity)
                if fresh["accountFingerprint"] != payload["accountFingerprint"]:
                    raise RuleViolation("账户持仓/挂单发生变化，请重新预览")
                if abs(positive(fresh["quote"]) / positive(payload["quote"]) - 1) > dec(POLICY.max_price_drift):
                    raise RuleViolation("确认后报价变化超过0.5%")
                if fresh["spec"] != payload["spec"]:
                    raise RuleViolation("交易规格发生变化，请重新预览")
                if dec(fresh["order"]["sz"]) < dec(payload["order"]["sz"]):
                    raise RuleViolation("最新风控允许数量下降，请重新预览")
                # Re-run sizing on the exact approved quantity; never increase it.
                if payload["request"]["intent"].startswith("enter"):
                    exact = {**payload["request"], "size": payload["order"]["sz"]}
                    await self._build(exact, existing=identity)
                latest = quote_price(await self.client.ticker(payload["order"]["instId"]), payload["order"]["side"],
                                     opening=payload["request"]["intent"].startswith("enter"))
                if abs(latest / positive(payload["quote"]) - 1) > dec(POLICY.max_price_drift):
                    raise RuleViolation("提交前报价变化超过0.5%，请重新预览")
                path, body = "/api/v5/trade/order", payload["order"]
            else:
                snapshot = await self.client.snapshot()
                algo = payload["action"] == "cancel_algo"
                field = "algoId" if algo else "ordId"
                rows = snapshot["algos"] if algo else snapshot["orders"]
                current = next((r for r in rows if r[field] == payload["order"][field]), None)
                if current != payload["currentOrder"]:
                    raise RuleViolation("挂单状态变化，请重新预览撤单")
                self.store.set("paused", True)
                path = "/api/v5/trade/cancel-algos" if algo else "/api/v5/trade/cancel-order"
                body = [payload["order"]] if algo else payload["order"]
            if time.time() >= plan["expires"]:
                raise RuleViolation("重新核对耗时后预览已过期")
        except Exception:
            self.store.finish(identity, "invalidated", {"message": "执行前校验失败；没有提交，请重新预览"})
            raise
        try:
            ack = await self.client.post(path, body)
            self.store.finish(identity, "unknown", {"acknowledgement": ack})
            if payload["action"] == "order" and payload["request"]["intent"].startswith("enter"):
                self.store.set("last-entry:" + payload["order"]["instId"], time.time())
        except OKXError as exc:
            if exc.code in {"51000", "51008", "51020"}:
                self.store.finish(identity, "rejected", {"code": exc.code, "message": "交易所明确拒绝，请修正原因后重新预览"})
                self.store.event("order_rejected", {"previewId": identity, "code": exc.code})
                return {"previewId": identity, "state": "rejected", "code": exc.code}
            self.store.finish(identity, "unknown", {"code": exc.code, "message": "响应未证明订单未创建，必须核对原ID"})
        except Exception:
            # A timeout/5xx might be a successful exchange order. Never resubmit.
            self.store.finish(identity, "unknown", {"message": "提交结果不确定，必须按原订单ID核对，禁止重试下单"})
            self.store.event("execution_unknown", {"previewId": identity}, key="unknown:" + identity)
        return await self.reconcile(identity)

    async def reconcile(self, identity):
        plan = self.store.plan(identity)
        payload = plan["payload"]
        if plan["state"] in {"preview", "invalidated", "rejected", "filled", "canceled", "algo_effective"}:
            return {"previewId": identity, "state": plan["state"], "result": plan["result"]}
        if payload["mode"] != self.client.mode:
            raise RuleViolation("禁止跨模拟/实盘模式核对订单")
        try:
            if payload["action"] == "cancel_algo":
                rows = await self.client.get("/api/v5/trade/order-algo", {"algoId": payload["order"]["algoId"]}, private=True)
            else:
                key = "clOrdId" if payload["action"] == "order" else "ordId"
                rows = await self.client.get("/api/v5/trade/order", {"instId": payload["order"]["instId"], key: payload["order"][key]}, private=True)
            if len(rows) != 1:
                raise RuleViolation("订单状态未唯一返回")
            order = rows[0]
            state = order["state"]
            if state == "filled" and payload["action"] == "order" and "attachAlgoOrds" in payload["order"]:
                attached = payload["order"]["attachAlgoOrds"][0]
                algos = await self.client.get("/api/v5/trade/order-algo", {"algoClOrdId": attached["attachAlgoClOrdId"]}, private=True)
                verified = next((a for a in algos if a.get("algoClOrdId") == attached["attachAlgoClOrdId"]
                    and a.get("instId") == order["instId"]
                    and a.get("side") == ("sell" if payload["order"]["side"] == "buy" else "buy")
                    and a.get("tdMode") == payload["order"]["tdMode"]
                    and a.get("slOrdPx") == "-1" and a.get("tpOrdPx") == "-1"
                    and a.get("slTriggerPxType") == "last" and a.get("tpTriggerPxType") == "last"
                    and a.get("state") in {"live", "effective"}
                    and dec(a.get("sz", "0")) == dec(order["accFillSz"])
                    and dec(a.get("slTriggerPx", "0")) == dec(attached["slTriggerPx"])
                    and dec(a.get("tpTriggerPx", "0")) == dec(attached["tpTriggerPx"])), None)
                if not verified:
                    state = "protection_pending"
                    self.store.set("paused", True)
                    self.store.event("protection_missing", {"previewId": identity, "instId": order["instId"]}, key="protection:" + identity)
                else:
                    order["verifiedProtection"] = verified
            elif state == "partially_filled":
                state = "protection_pending"
                self.store.set("paused", True)
                self.store.event("unexpected_partial_fill", {"previewId": identity}, key="partial:" + identity)
            elif state == "effective":
                state = "algo_effective"
            elif state not in {"filled", "canceled"}:
                state = "unknown"
            self.store.finish(identity, state, order)
            self.store.event("order_status", {"previewId": identity, "state": state, "instId": payload["order"]["instId"]}, key=identity + ":" + state)
            return {"previewId": identity, "state": state, "result": order}
        except Exception:
            # Keep a durable lock even on not-found: matching engines may be eventually consistent.
            filled_entry = (payload["action"] == "order" and "attachAlgoOrds" in payload["order"]
                            and 'order' in locals() and order.get("state") == "filled")
            state = "protection_pending" if filled_entry or plan["state"] == "protection_pending" else "unknown"
            if filled_entry:
                self.store.set("paused", True)
                self.store.event("protection_missing", {"previewId": identity}, key="protection:" + identity)
            self.store.finish(identity, state, {"message": "状态尚不能核实；请稍后使用 reconcile_order，不能重新发单"})
            self.store.event("reconcile_pending", {"previewId": identity}, key="reconcile:" + identity)
            return {"previewId": identity, "state": state, "message": "无法核实成交/保护状态，新增交易已被阻止；请查询原订单"}
