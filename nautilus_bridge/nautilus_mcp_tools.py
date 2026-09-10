from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import httpx

from trading_models import EntryType, Side, TradeIntentCreate


BASE_URL = os.getenv("XKO_NAUTILUS_URL", "http://127.0.0.1:8765").rstrip("/")


async def _request(method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(method, f"{BASE_URL}{path}", json=json)
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        raise RuntimeError(f"Nautilus bridge error HTTP {response.status_code}: {detail}")
    return response.json()


def register_nautilus_tools(mcp: Any) -> None:
    @mcp.tool(
        name="nautilus_health",
        description="Read Nautilus bridge health and safety switches before trading.",
    )
    async def nautilus_health() -> dict[str, Any]:
        return await _request("GET", "/health")

    @mcp.tool(
        name="create_trade_intent",
        description=(
            "Create a proposed OKX trade intent. This does not place an order. "
            "Nautilus calculates quantity from risk_pct, entry and stop."
        ),
    )
    async def create_trade_intent(
        instrument_id: str,
        side: str,
        entry_type: str,
        risk_pct: float,
        entry_price: str,
        stop_loss: str,
        take_profit: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        request = TradeIntentCreate(
            instrument_id=instrument_id,
            side=Side(side.upper()),
            entry_type=EntryType(entry_type.upper()),
            risk_pct=Decimal(str(risk_pct)),
            entry_price=Decimal(entry_price),
            stop_loss=Decimal(stop_loss),
            take_profit=Decimal(take_profit) if take_profit else None,
            reason=reason,
        )
        return await _request("POST", "/intents", json=request.model_dump(mode="json"))

    @mcp.tool(
        name="preview_trade_intent",
        description="Validate and size an intent in Nautilus without placing an order.",
    )
    async def preview_trade_intent(intent_id: str) -> dict[str, Any]:
        return await _request("POST", f"/intents/{intent_id}/preview")

    @mcp.tool(
        name="get_trade_intent",
        description="Read a trade intent and its Nautilus execution status.",
    )
    async def get_trade_intent(intent_id: str) -> dict[str, Any]:
        return await _request("GET", f"/intents/{intent_id}")

    @mcp.tool(
        name="approve_trade_intent",
        description=(
            "Human-gated approval. Call only when the user explicitly supplies the "
            "one-time approval code printed by the local Nautilus service."
        ),
    )
    async def approve_trade_intent(intent_id: str, approval_code: str) -> dict[str, Any]:
        return await _request(
            "POST",
            f"/intents/{intent_id}/approve",
            json={"approval_code": approval_code},
        )

    @mcp.tool(
        name="submit_trade_intent",
        description=(
            "WRITE ACTION. Submit an already human-approved intent into Nautilus. "
            "Requires local safety switches to permit submission."
        ),
    )
    async def submit_trade_intent(intent_id: str) -> dict[str, Any]:
        return await _request("POST", f"/intents/{intent_id}/submit")
