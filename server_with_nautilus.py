from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from nautilus_bridge.nautilus_mcp_tools import register_nautilus_tools
from server import mcp


register_nautilus_tools(mcp)

_DESCRIPTION = (
    "OKX market-analysis MCP app with a separately hosted, human-gated NautilusTrader "
    "execution bridge. Market-data tools are read-only. Trade-intent tools can only "
    "reach execution when the external Nautilus service is reachable and its local "
    "safety switches permit submission."
)

_INSTRUCTIONS = (
    "Use market_scan, compare_symbols, get_candles and run_live_trader for public OKX "
    "analysis. For trading, first call nautilus_health, then create_trade_intent and "
    "preview_trade_intent. Never choose raw order quantity yourself; Nautilus sizes from "
    "risk_pct, entry and stop. approve_trade_intent requires a one-time code explicitly "
    "supplied by the user. Call submit_trade_intent only after explicit user instruction "
    "to execute. Never claim an order was placed until the Nautilus execution status says so."
)


def _update_server_identity() -> None:
    # MCP SDK v2 keeps identity on its low-level server. The compatibility branch
    # supports older deployments that used _mcp_server.
    for target in (
        mcp,
        getattr(mcp, "_lowlevel_server", None),
        getattr(mcp, "_mcp_server", None),
    ):
        if target is None:
            continue
        for name, value in (
            ("description", _DESCRIPTION),
            ("instructions", _INSTRUCTIONS),
        ):
            try:
                setattr(target, name, value)
            except (AttributeError, TypeError):
                pass


_update_server_identity()


@mcp.custom_route("/bridge-health", methods=["GET"])
async def bridge_health(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "OKX Live Trader + Nautilus Gateway",
            "mode": "read-only-market-data + gated-nautilus-tools",
            "mcpEndpoint": "/mcp",
            "nautilusBridgeConfigured": bool(os.getenv("XKO_NAUTILUS_URL")),
        }
    )


app = mcp.streamable_http_app(
    host="0.0.0.0",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(
        "streamable-http",
        host="0.0.0.0",
        port=port,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )
