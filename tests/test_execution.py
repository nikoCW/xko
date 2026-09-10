import asyncio
import copy
import time

import pytest

from rules import RuleViolation
from state import Store
from trading import TradingService
from tests.test_rules import signal_bars, spec


class Exchange:
    mode = "demo"
    configured = True

    def __init__(self):
        self.price = "102"
        self.posts = []
        self.timeout = False
        self.protected = True
        self.lookup_error = False
        self.balance = {"totalEq": "10000", "details": [{"ccy": "USDT", "eqUsd": "10000", "eq": "10000", "availBal": "10000", "availEq": "10000"}]}
        self.orders, self.positions = [], []
        self.order_state = "filled"

    async def instrument(self, inst):
        return spec("SWAP" if inst.endswith("SWAP") else "SPOT")

    async def ticker(self, inst):
        return {"instId": inst, "bidPx": self.price, "askPx": self.price, "last": self.price, "ts": str(int(time.time()*1000)), "volCcy24h": "9000000"}

    async def candles(self, inst, bar):
        return dict(zip(("15m", "1H", "4H"), signal_bars()))[bar]

    async def fee(self, spec):
        return "0.001"

    async def snapshot(self):
        return copy.deepcopy({"mode": "demo", "config": {"uid": "test", "posMode": "net_mode", "acctLv": "2", "autoLoan": False},
            "balance": self.balance, "positions": self.positions, "orders": self.orders, "algos": []})

    async def post(self, path, body):
        self.posts.append(copy.deepcopy(body))
        if self.timeout:
            raise TimeoutError()
        return [{"ordId": "123", "sCode": "0"}]

    async def get(self, path, params=None, private=False):
        if path.endswith("leverage-info"):
            return [{"lever": "1"}]
        if path.endswith("funding-rate"):
            return [{"fundingRate": "0.0001"}]
        if self.lookup_error:
            raise TimeoutError()
        submitted = self.posts[-1]
        if path.endswith("order-algo"):
            if not self.protected:
                return []
            attached = submitted["attachAlgoOrds"][0]
            return [{"algoClOrdId": attached["attachAlgoClOrdId"], "instId": submitted["instId"], "side": "sell" if submitted["side"] == "buy" else "buy",
                     "state": "live", "sz": submitted["sz"], "tdMode": submitted["tdMode"],
                     "slOrdPx": "-1", "tpOrdPx": "-1", "slTriggerPxType": "last", "tpTriggerPxType": "last",
                     "slTriggerPx": attached["slTriggerPx"], "tpTriggerPx": attached["tpTriggerPx"]}]
        return [{"ordId": "123", "instId": submitted["instId"], "state": self.order_state, "accFillSz": submitted["sz"]}]


@pytest.fixture
def svc(tmp_path):
    return TradingService(Exchange(), Store(str(tmp_path / "state.db")))


async def preview(svc):
    return await svc.preview("BTC-USDT", "enter_long", "102", "100", "110", size="1")


@pytest.mark.asyncio
async def test_confirmation_and_exactly_one_submission(svc):
    p = await preview(svc)
    assert svc.client.posts == []
    with pytest.raises(RuleViolation):
        await svc.execute(p["previewId"], "yes")
    result = await svc.execute(p["previewId"], p["confirmation"])
    assert result["state"] == "filled"
    assert "verifiedProtection" in result["result"]
    await svc.execute(p["previewId"], p["confirmation"])
    assert len(svc.client.posts) == 1
    assert svc.client.posts[0] == p["order"]


@pytest.mark.asyncio
async def test_expired_preview_never_posts(svc):
    p = await preview(svc)
    with svc.store.connection() as db:
        db.execute("UPDATE plans SET expires=0")
    with pytest.raises(RuleViolation):
        await svc.execute(p["previewId"], p["confirmation"])
    assert not svc.client.posts


@pytest.mark.asyncio
async def test_price_drift_invalidates(svc):
    p = await preview(svc)
    svc.client.price = "110"
    with pytest.raises(RuleViolation):
        await svc.execute(p["previewId"], p["confirmation"])
    assert not svc.client.posts
    assert svc.store.plan(p["previewId"])["state"] == "invalidated"


@pytest.mark.asyncio
async def test_timeout_reconciles_without_retry(svc):
    p = await preview(svc)
    svc.client.timeout = True
    result = await svc.execute(p["previewId"], p["confirmation"])
    assert result["state"] == "filled"
    assert len(svc.client.posts) == 1


@pytest.mark.asyncio
async def test_unknown_survives_restart_and_blocks_duplicate(svc):
    p = await preview(svc)
    svc.client.timeout = svc.client.lookup_error = True
    result = await svc.execute(p["previewId"], p["confirmation"])
    assert result["state"] == "unknown"
    restarted = TradingService(svc.client, Store(svc.store.path))
    await restarted.execute(p["previewId"], p["confirmation"])
    assert len(svc.client.posts) == 1
    with pytest.raises(RuleViolation):
        await preview(restarted)


@pytest.mark.asyncio
async def test_missing_protection_pauses_entries(svc):
    p = await preview(svc)
    svc.client.protected = False
    result = await svc.execute(p["previewId"], p["confirmation"])
    assert result["state"] == "protection_pending"
    assert svc.store.get("paused") is True
    assert any(e["kind"] == "protection_missing" for e in svc.store.events())


@pytest.mark.asyncio
async def test_live_demo_switch_invalidates(svc):
    p = await preview(svc)
    svc.client.mode = "live"
    with pytest.raises(RuleViolation):
        await svc.execute(p["previewId"], p["confirmation"])
    assert not svc.client.posts


@pytest.mark.asyncio
async def test_explicit_rejection_does_not_lock_forever(svc):
    from okx_client import OKXError
    p = await preview(svc)
    async def rejected(*args):
        raise OKXError("51008")
    svc.client.post = rejected
    result = await svc.execute(p["previewId"], p["confirmation"])
    assert result["state"] == "rejected"
    assert svc.store.unresolved() == []


@pytest.mark.asyncio
async def test_preflight_api_error_never_submits(svc):
    from okx_client import OKXError
    p = await preview(svc)
    async def failed(*args):
        raise OKXError("50000")
    svc.client.snapshot = failed
    with pytest.raises(OKXError):
        await svc.execute(p["previewId"], p["confirmation"])
    assert not svc.client.posts
    assert svc.store.plan(p["previewId"])["state"] == "invalidated"


@pytest.mark.asyncio
async def test_cash_exit_works_without_usdt_balance(svc):
    svc.client.balance = {"totalEq": "1000", "details": [{"ccy": "BTC", "availBal": "1"}]}
    p = await svc.preview("BTC-USDT", "exit_long", "102", size="0.5")
    assert p["order"]["side"] == "sell"
    with pytest.raises(RuleViolation):
        await svc.preview("BTC-USDT", "exit_long", "102", size="2")


@pytest.mark.asyncio
async def test_exit_is_reduce_only_and_does_not_need_entry_signal(svc):
    svc.client.positions = [{"instId": "BTC-USDT-SWAP", "mgnMode": "isolated", "posSide": "net", "pos": "2", "lever": "1"}]
    svc.store.set("paused", True)
    p = await svc.preview("BTC-USDT-SWAP", "exit_long", "102", size="1")
    assert p["order"]["reduceOnly"] is True
    assert p["order"]["side"] == "sell"
    assert "attachAlgoOrds" not in p["order"]
    with pytest.raises(RuleViolation):
        await svc.preview("BTC-USDT-SWAP", "exit_short", "102", size="1")


def test_claim_is_atomic_and_daily_drawdown_persists(tmp_path):
    store = Store(str(tmp_path / "store.db"))
    a, b = store.create_plan({}), store.create_plan({})
    store.claim(a["id"])
    with pytest.raises(RuleViolation):
        Store(store.path).claim(b["id"])
    store.daily_guard("10000")
    with pytest.raises(RuleViolation):
        Store(store.path).daily_guard("9600")
