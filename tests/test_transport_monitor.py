import base64
import hashlib
import hmac
import json

import httpx
import pytest

from monitor import Monitor
from okx_client import OKXClient, OKXError
from rules import RuleViolation, dec
from state import Store
from trading import TradingService
from tests.test_execution import Exchange


def private_env(monkeypatch):
    for k, v in {"OKX_API_KEY": "test-key", "OKX_API_SECRET": "test-secret", "OKX_API_PASSPHRASE": "test-pass", "MCP_AUTH_TOKEN": "x" * 32, "OKX_MODE": "demo"}.items():
        monkeypatch.setenv(k, v)


@pytest.mark.asyncio
async def test_signature_includes_query_and_exact_json(monkeypatch):
    private_env(monkeypatch)
    requests = []

    def respond(request):
        requests.append(request)
        stamp = request.headers["OK-ACCESS-TIMESTAMP"]
        signed = stamp + request.method + request.url.raw_path.decode() + request.content.decode()
        expected = base64.b64encode(hmac.new(b"test-secret", signed.encode(), hashlib.sha256).digest()).decode()
        assert request.headers["OK-ACCESS-SIGN"] == expected
        assert request.headers["x-simulated-trading"] == "1"
        return httpx.Response(200, json={"code": "0", "data": [{"sCode": "0"}]})

    client = OKXClient(transport=httpx.MockTransport(respond))
    await client.get("/api/v5/trade/order", {"instId": "BTC-USDT", "clOrdId": "xko1"}, private=True)
    await client.post("/api/v5/trade/order", {"instId": "BTC-USDT", "sz": "0.1"})
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_item_failure_is_not_success(monkeypatch):
    private_env(monkeypatch)
    client = OKXClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"code": "0", "data": [{"sCode": "51000"}]})))
    with pytest.raises(OKXError):
        await client.post("/api/v5/trade/order", {})
    with pytest.raises(RuleViolation):
        await client.post("/api/v5/asset/withdrawal", {})


def test_credentials_require_auth(monkeypatch):
    private_env(monkeypatch)
    monkeypatch.delenv("MCP_AUTH_TOKEN")
    with pytest.raises(ValueError):
        OKXClient()


@pytest.mark.asyncio
async def test_no_redirect_or_automatic_retry(monkeypatch):
    private_env(monkeypatch)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503)
    c = OKXClient(transport=httpx.MockTransport(respond))
    with pytest.raises(httpx.HTTPStatusError):
        await c.post("/api/v5/trade/order", {})
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bearer_auth_health_and_mcp():
    from server import BearerAuth
    from starlette.responses import JSONResponse
    async def endpoint(scope, receive, send):
        await JSONResponse({"ok": True})(scope, receive, send)
    app = BearerAuth(endpoint, "a" * 32)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/mcp")).status_code == 401
        assert (await c.get("/mcp", headers={"Authorization": "Bearer " + "a" * 32})).status_code == 200


def test_real_mcp_lifespan_and_host_validation():
    from starlette.testclient import TestClient
    from server import app
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    with TestClient(app, base_url="http://localhost") as c:
        assert c.get("/health").status_code == 200
        result = c.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json=initialize)
        assert result.status_code == 200
        assert "result" in result.json()
        denied = c.post("/mcp", headers={"Accept": "application/json, text/event-stream", "Host": "attacker.invalid"}, json=initialize)
        assert denied.status_code == 421


def test_alert_crossing_cooldown_and_restart(tmp_path):
    svc = TradingService(Exchange(), Store(str(tmp_path / "state.db")))
    monitor = Monitor(svc)
    monitor.add("BTC-USDT", "above", "100", 60)
    alert = monitor.list()[0]
    monitor.observe(alert, dec("101"), now=1000)
    monitor.observe(alert, dec("102"), now=2000)
    assert len(svc.store.events()) == 1  # Staying above does not spam.
    restored = Monitor(TradingService(svc.client, Store(svc.store.path)))
    restored.observe(alert, dec("90"), now=2010)
    restored.observe(alert, dec("101"), now=2020)
    assert len(svc.store.events()) == 2
    restored.observe(alert, dec("90"), now=2025)
    restored.observe(alert, dec("101"), now=2030)
    assert len(svc.store.events()) == 2


@pytest.mark.asyncio
async def test_monitor_only_reads(tmp_path):
    svc = TradingService(Exchange(), Store(str(tmp_path / "state.db")))
    monitor = Monitor(svc)
    monitor.add("BTC-USDT", "above", "100", 60)
    await monitor.once()
    assert svc.client.posts == []
    assert len(svc.store.events()) == 1


@pytest.mark.asyncio
async def test_position_warning_and_drawdown_pause_without_trade(tmp_path):
    svc = TradingService(Exchange(), Store(str(tmp_path / "state.db")))
    svc.store.daily_guard("11000")
    svc.client.positions = [{"instId": "BTC-USDT-SWAP", "instType": "SWAP", "mgnMode": "isolated", "posSide": "net", "pos": "1", "markPx": "102", "liqPx": "100"}]
    monitor = Monitor(svc)
    await monitor.account_risks(1000)
    kinds = {e["kind"] for e in svc.store.events()}
    assert {"daily_drawdown_pause", "position_protection_unverified", "liquidation_near"} <= kinds
    assert svc.store.get("paused") is True
    assert svc.client.posts == []
