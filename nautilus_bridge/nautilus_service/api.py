from __future__ import annotations

import hmac
import os
import queue

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from bridge_runtime import BridgeCommand, BridgeRuntime, CommandKind
from trading_models import ApprovalRequest, BridgeHealth, CommandResult, IntentStatus, TradeIntentCreate


PROTECTIVE_BRACKET_MODE = "OKX_ATTACHED_OCO"
PROTECTION_SCOPE = "xko_owned_positions_only"
TARGET_POSITION_POLICY = "target_instrument_must_be_flat_before_submit"


def create_app(runtime: BridgeRuntime) -> FastAPI:
    app = FastAPI(
        title="xko Nautilus Bridge",
        version="0.3.0",
        description="Private human-gated bridge from MCP trade intents to NautilusTrader.",
    )
    bridge_api_token = os.getenv("BRIDGE_API_TOKEN", "").strip()

    @app.middleware("http")
    async def authenticate_bridge(request: Request, call_next):
        # Keep only the liveness/readiness endpoint unauthenticated. All intent/order
        # state requires the bridge bearer token.
        if request.url.path != "/health" and bridge_api_token:
            authorization = request.headers.get("authorization", "")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token, bridge_api_token):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized bridge request"})
        return await call_next(request)

    @app.get("/health", response_model=BridgeHealth)
    def health() -> BridgeHealth:
        readiness = runtime.readiness_snapshot()
        return BridgeHealth(
            status="ok",
            **readiness,
            okx_environment=runtime.okx_environment,
            order_submit_enabled=runtime.policy.allow_order_submit,
            unprotected_entry_enabled=runtime.policy.allow_unprotected_entry,
            protected_submit_only=True,
            protective_bracket_mode=PROTECTIVE_BRACKET_MODE,
            protection_scope=PROTECTION_SCOPE,
            target_instrument_position_policy=TARGET_POSITION_POLICY,
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
        if not runtime.preview_ready.is_set():
            reason = runtime.readiness_reason() or "not_ready"
            raise HTTPException(
                status_code=503,
                detail=f"Preview readiness gate closed: {reason}",
            )

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
        # Policy remains the first hard lock. This deployment should stay false until
        # attached-OCO behavior has been validated end-to-end in a non-money test path.
        if not runtime.policy.allow_order_submit:
            raise HTTPException(
                status_code=403,
                detail="Order submission disabled by ALLOW_ORDER_SUBMIT=false",
            )

        # The bridge no longer has an unprotected execution path. A true value is a
        # configuration error and must never be treated as permission to bypass protection.
        if runtime.policy.allow_unprotected_entry:
            raise HTTPException(
                status_code=403,
                detail=(
                    "ALLOW_UNPROTECTED_ENTRY=true is forbidden. "
                    "This bridge submits protected OKX attached-OCO brackets only."
                ),
            )

        # Global protection readiness covers XKO-owned positions only. Manual/grid
        # positions do not globally close this gate; the strategy event thread performs
        # a second per-intent preflight which requires the target instrument to be flat.
        if not runtime.trading_ready.is_set():
            reason = runtime.readiness_reason() or "not_ready"
            raise HTTPException(
                status_code=503,
                detail=f"Trading readiness gate closed: {reason}",
            )

        try:
            before = runtime.store.get(intent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # OrderFactory.bracket in the protected OKX path always carries both attached
        # children. SL is mandatory and already required by TradeIntentCreate; TP is
        # mandatory for this execution mode as well. Refuse before mutating lifecycle state.
        if before.request.take_profit is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Protected submit requires take_profit. "
                    "Refusing to queue an intent without both attached SL and TP."
                ),
            )

        try:
            record = runtime.store.mark_queued(intent_id)
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
                    protection_status="NOT_SUBMITTED",
                    protection_verified=False,
                )
                raise HTTPException(status_code=503, detail="Bridge command queue is full") from exc

        return runtime.store.get(intent_id).model_dump(mode="json")

    return app
