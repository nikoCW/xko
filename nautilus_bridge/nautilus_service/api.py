from __future__ import annotations

import hmac
import os
import queue

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from bridge_runtime import BridgeCommand, BridgeRuntime, CommandKind
from trading_models import ApprovalRequest, BridgeHealth, CommandResult, IntentStatus, TradeIntentCreate


def create_app(runtime: BridgeRuntime) -> FastAPI:
    app = FastAPI(
        title="xko Nautilus Bridge",
        version="0.1.0",
        description="Private human-gated bridge from MCP trade intents to NautilusTrader.",
    )
    bridge_api_token = os.getenv("BRIDGE_API_TOKEN", "").strip()

    @app.middleware("http")
    async def authenticate_bridge(request: Request, call_next):
        # Render uses /health for the private-service health check, so keep only this
        # endpoint unauthenticated. All intent/order state requires the internal token.
        if request.url.path != "/health" and bridge_api_token:
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token, bridge_api_token):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized bridge request"})
        return await call_next(request)

    @app.get("/health", response_model=BridgeHealth)
    def health() -> BridgeHealth:
        return BridgeHealth(
            status="ok",
            ready=runtime.ready.is_set(),
            okx_environment=runtime.okx_environment,
            order_submit_enabled=runtime.policy.allow_order_submit,
            unprotected_entry_enabled=runtime.policy.allow_unprotected_entry,
            allowed_instruments=sorted(runtime.policy.allowed_instruments),
        )

    @app.post("/intents")
    def create_intent(request: TradeIntentCreate) -> dict:
        try:
            record, approval_code = runtime.store.create(request)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        print(
            f"[HUMAN APPROVAL] intent={record.intent_id} "
            f"instrument={record.request.instrument_id} "
            f"side={record.request.side} risk={record.request.risk_pct}% "
            f"code={approval_code}",
            flush=True,
        )

        data = record.model_dump(mode="json")
        data["approval_required"] = True
        data["approval_code_delivery"] = "local Nautilus service console only"
        return data

    @app.get("/intents")
    def list_intents(limit: int = 50) -> list[dict]:
        return [x.model_dump(mode="json") for x in runtime.store.list_recent(limit)]

    @app.get("/intents/{intent_id}")
    def get_intent(intent_id: str) -> dict:
        try:
            return runtime.store.get(intent_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/intents/{intent_id}/approve")
    def approve_intent(intent_id: str, request: ApprovalRequest) -> dict:
        try:
            record = runtime.store.verify_and_approve(intent_id, request.approval_code)
            return record.model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/intents/{intent_id}/preview", response_model=CommandResult)
    def preview_intent(intent_id: str) -> CommandResult:
        if not runtime.ready.is_set():
            raise HTTPException(status_code=503, detail="Nautilus strategy is not ready")

        try:
            runtime.store.get(intent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        reply: queue.Queue = queue.Queue(maxsize=1)
        try:
            runtime.commands.put_nowait(
                BridgeCommand(kind=CommandKind.PREVIEW, intent_id=intent_id, reply=reply)
            )
        except queue.Full as exc:
            raise HTTPException(status_code=503, detail="Bridge command queue is full") from exc

        timeout = float(os.getenv("PREVIEW_TIMEOUT_SECS", "5"))
        try:
            result = reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise HTTPException(status_code=504, detail="Preview timed out") from exc

        return CommandResult.model_validate(result)

    @app.post("/intents/{intent_id}/submit")
    def submit_intent(intent_id: str) -> dict:
        if not runtime.ready.is_set():
            raise HTTPException(status_code=503, detail="Nautilus strategy is not ready")
        if not runtime.policy.allow_order_submit:
            raise HTTPException(
                status_code=403,
                detail="Order submission disabled by ALLOW_ORDER_SUBMIT=false",
            )
        if not runtime.policy.allow_unprotected_entry:
            raise HTTPException(
                status_code=403,
                detail=(
                    "V1 entry submission blocked by ALLOW_UNPROTECTED_ENTRY=false. "
                    "Protective stop/TP execution is intentionally not implemented yet."
                ),
            )

        try:
            before = runtime.store.get(intent_id)
            record = runtime.store.mark_queued(intent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if before.status != record.status:
            try:
                runtime.commands.put_nowait(
                    BridgeCommand(kind=CommandKind.SUBMIT, intent_id=intent_id)
                )
            except queue.Full as exc:
                runtime.store.update_execution(
                    intent_id,
                    status=IntentStatus.ERROR,
                    error="Bridge command queue full after mark_queued",
                )
                raise HTTPException(status_code=503, detail="Bridge command queue is full") from exc

        return runtime.store.get(intent_id).model_dump(mode="json")

    return app
