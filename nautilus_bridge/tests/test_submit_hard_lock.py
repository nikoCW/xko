from __future__ import annotations

import queue
from types import SimpleNamespace

from fastapi.testclient import TestClient

from nautilus_service.api import create_app


class ExplodingStore:
    """Any store access means the first hard lock was bypassed."""

    def get(self, _intent_id: str):
        raise AssertionError("submit hard lock must reject before store access")

    def mark_queued(self, _intent_id: str):
        raise AssertionError("submit hard lock must reject before lifecycle mutation")


class FakeRuntime:
    def __init__(self) -> None:
        self.policy = SimpleNamespace(
            allow_order_submit=False,
            allow_unprotected_entry=False,
        )
        self.store = ExplodingStore()
        self.commands: queue.Queue = queue.Queue()
        self.trading_ready = SimpleNamespace(is_set=lambda: True)

    def readiness_reason(self):
        return None


def test_submit_hard_lock_rejects_before_store_or_queue(monkeypatch):
    monkeypatch.delenv("BRIDGE_API_TOKEN", raising=False)
    runtime = FakeRuntime()
    client = TestClient(create_app(runtime))

    response = client.post("/intents/intent-does-not-matter/submit")

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Order submission disabled by ALLOW_ORDER_SUBMIT=false"
    }
    assert runtime.commands.empty()
