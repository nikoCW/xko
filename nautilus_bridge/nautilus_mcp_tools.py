from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

try:
    # Imported from the repository root, e.g. server_with_nautilus.py.
    from .trading_models import EntryType, Side, TradeIntentCreate
except ImportError:  # pragma: no cover - supports running from nautilus_bridge/ directly
    from trading_models import EntryType, Side, TradeIntentCreate


_RAW_BASE_URL = os.getenv("XKO_NAUTILUS_URL", "http://127.0.0.1:8765").rstrip("/")
BASE_URL = _RAW_BASE_URL if "://" in _RAW_BASE_URL else f"http://{_RAW_BASE_URL}"
BRIDGE_API_TOKEN = os.getenv("BRIDGE_API_TOKEN", "").strip()
OKX_API_BASE = os.getenv("OKX_API_BASE", "https://www.okx.com").rstrip("/")


def _headers() -> dict[str, str]:
    if not BRIDGE_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {BRIDGE_API_TOKEN}"}


async def _request(method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.request(
            method,
            f"{BASE_URL}{path}",
            json=json,
            headers=_headers(),
        )
    if response.is_error:
        try:
            detail = response.json().get("detail")
        except Exception:
            detail = response.text
        raise RuntimeError(f"Nautilus bridge error HTTP {response.status_code}: {detail}")
    return response.json()


def _canonical_okx_market_inst(instrument_id: str) -> str:
    normalized = instrument_id.strip().upper()
    if not normalized.endswith(".OKX"):
        raise ValueError("instrument_id must end in .OKX")
    return normalized[:-4]


async def _exact_market_reference(inst_id: str) -> dict[str, Any]:
    """Fetch the exact OKX public ticker and refuse any instrument substitution."""
    headers = {"User-Agent": "okx-live-trader-mcp/1.2", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
        response = await client.get(
            f"{OKX_API_BASE}/api/v5/market/ticker",
            params={"instId": inst_id},
        )
        response.raise_for_status()
        payload = response.json()

    if str(payload.get("code", "")) != "0":
        raise RuntimeError(f"OKX API error: {payload.get('code')} {payload.get('msg')}")
    rows = payload.get("data", [])
    if not rows:
        raise RuntimeError(f"OKX returned no ticker for {inst_id}")
    row = rows[0]
    returned_inst = str(row.get("instId", "")).strip().upper()
    if returned_inst != inst_id.upper():
        raise RuntimeError(
            f"OKX ticker instrument mismatch: requested={inst_id} returned={returned_inst}"
        )
    last = Decimal(str(row.get("last", "0")))
    if last <= 0:
        raise RuntimeError(f"OKX ticker for {inst_id} has non-positive last price")
    return {
        "instId": returned_inst,
        "last": str(last),
        "bid": str(row.get("bidPx", "")),
        "ask": str(row.get("askPx", "")),
        "ts": row.get("ts"),
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "source": "OKX /api/v5/market/ticker",
    }


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
            "Create a proposed OKX trade intent. This does not place an order. Nautilus "
            "calculates quantity from risk_pct, entry and stop. For *.SWAP.OKX, "
            "price_reference_instrument_id is required and must exactly match the OKX SWAP "
            "instId returned by get_ticker (for example BTC-USDT-SWAP). SPOT fallback is "
            "forbidden; this tool independently verifies that exact SWAP ticker with OKX."
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
        price_reference_instrument_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        canonical_market_inst = _canonical_okx_market_inst(instrument_id)
        is_swap = canonical_market_inst.endswith("-SWAP")
        market_reference: dict[str, Any] | None = None

        if is_swap:
            supplied_reference = (price_reference_instrument_id or "").strip().upper()
            if supplied_reference.endswith(".OKX"):
                supplied_reference = supplied_reference[:-4]
            if not supplied_reference:
                raise ValueError(
                    "SWAP intent requires price_reference_instrument_id from get_ticker on the "
                    f"exact matching SWAP instrument {canonical_market_inst}; SPOT fallback is forbidden"
                )
            if supplied_reference != canonical_market_inst:
                raise ValueError(
                    "SWAP price reference mismatch: "
                    f"intent={canonical_market_inst} reference={supplied_reference}. "
                    "Use the exact matching SWAP instId; SPOT fallback is forbidden."
                )
            market_reference = await _exact_market_reference(canonical_market_inst)

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
        result = await _request("POST", "/intents", json=request.model_dump(mode="json"))
        if market_reference is not None:
            entry = Decimal(entry_price)
            reference_last = Decimal(market_reference["last"])
            difference_pct = ((entry / reference_last) - Decimal("1")) * Decimal("100")
            result["price_reference"] = market_reference
            result["entry_vs_reference_pct"] = str(difference_pct)
            result["spot_fallback_used"] = False
        return result

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
            "Call only after explicit user instruction to execute. The Nautilus bridge "
            "must independently permit submission through its local safety switches."
        ),
    )
    async def submit_trade_intent(intent_id: str) -> dict[str, Any]:
        return await _request("POST", f"/intents/{intent_id}/submit")
