from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field


HOST = os.getenv("SHADOW_DASHBOARD_HOST", "127.0.0.1").strip()
PORT = int(os.getenv("SHADOW_DASHBOARD_PORT", "8766"))
BRIDGE_HOST = os.getenv("NAUTILUS_BRIDGE_HOST", "127.0.0.1").strip()
BRIDGE_PORT = int(os.getenv("NAUTILUS_BRIDGE_PORT", "8765"))
BRIDGE_URL = os.getenv("SHADOW_BRIDGE_URL", f"http://{BRIDGE_HOST}:{BRIDGE_PORT}").rstrip("/")
BRIDGE_API_TOKEN = os.getenv("BRIDGE_API_TOKEN", "").strip()
REVIEW_DB_PATH = Path(
    os.getenv("SHADOW_REVIEW_DB_PATH", "/var/lib/xko-nautilus/shadow-reviews.db")
)
HTML_PATH = Path(__file__).with_name("shadow_dashboard.html")
REQUEST_TIMEOUT = float(os.getenv("SHADOW_DASHBOARD_REQUEST_TIMEOUT_SECS", "12"))


class BridgeCallError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ReviewRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=1000)
    preview: dict[str, Any] = Field(default_factory=dict)


class ReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_reviews (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    note TEXT NOT NULL,
                    intent_status TEXT NOT NULL,
                    preview_json TEXT NOT NULL,
                    health_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_reviews_intent "
                "ON shadow_reviews(intent_id, review_id DESC)"
            )
            conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def add(
        self,
        *,
        intent_id: str,
        operator: str,
        note: str,
        intent_status: str,
        preview: dict[str, Any],
        health: dict[str, Any],
    ) -> dict[str, Any]:
        reviewed_at = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO shadow_reviews(
                    intent_id, reviewed_at, operator, note, intent_status,
                    preview_json, health_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    reviewed_at,
                    operator.strip(),
                    note.strip(),
                    intent_status,
                    json.dumps(preview, separators=(",", ":"), sort_keys=True),
                    json.dumps(health, separators=(",", ":"), sort_keys=True),
                ),
            )
            conn.commit()
            review_id = int(cur.lastrowid)
        return {
            "review_id": review_id,
            "intent_id": intent_id,
            "reviewed_at": reviewed_at,
            "operator": operator.strip(),
            "note": note.strip(),
            "intent_status": intent_status,
        }

    def list_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT review_id, intent_id, reviewed_at, operator, note, intent_status,
                       preview_json, health_json
                FROM shadow_reviews
                ORDER BY review_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["preview"] = json.loads(item.pop("preview_json"))
            item["health"] = json.loads(item.pop("health_json"))
            out.append(item)
        return out


review_store = ReviewStore(REVIEW_DB_PATH)
app = FastAPI(
    title="xko Shadow Operator Dashboard",
    version="1.0.0",
    description=(
        "Local-only live-shadow operator UI. It can read bridge state, request PREVIEW, "
        "and record human review notes. It has no approval or submit endpoint."
    ),
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


def _bridge_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    headers = {"Accept": "application/json"}
    if BRIDGE_API_TOKEN:
        headers["Authorization"] = f"Bearer {BRIDGE_API_TOKEN}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = URLRequest(
        f"{BRIDGE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw else {}
            detail = str(body.get("detail") or body)
        except json.JSONDecodeError:
            detail = raw or str(exc)
        raise BridgeCallError(exc.code, detail) from exc
    except (URLError, TimeoutError) as exc:
        raise BridgeCallError(503, f"Bridge unavailable: {exc}") from exc


def _health() -> dict[str, Any]:
    value = _bridge_request("GET", "/health")
    if not isinstance(value, dict):
        raise BridgeCallError(502, "Bridge /health returned unexpected payload")
    return value


def _intents(limit: int = 50) -> list[dict[str, Any]]:
    value = _bridge_request("GET", f"/intents?limit={max(1, min(limit, 200))}")
    if not isinstance(value, list):
        raise BridgeCallError(502, "Bridge /intents returned unexpected payload")
    return value


def _market_entry_enabled() -> bool:
    return os.getenv("ALLOW_MARKET_ENTRY", "false").strip().lower() in {"1", "true", "yes", "on"}


def _checklist(health: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        (
            "Bridge health",
            health.get("status") == "ok" and health.get("ready") is True,
            "Bridge is operational and ready",
        ),
        (
            "Order submission hard lock",
            health.get("order_submit_enabled") is False,
            "ALLOW_ORDER_SUBMIT must remain false in shadow mode",
        ),
        (
            "Unprotected entry blocked",
            health.get("unprotected_entry_enabled") is False,
            "Unprotected execution must remain disabled",
        ),
        (
            "Market entry blocked",
            not _market_entry_enabled(),
            "ALLOW_MARKET_ENTRY must remain false",
        ),
        (
            "Reconciliation healthy",
            health.get("reconciliation_ready") is True
            and health.get("reconciliation_invalidated") is False,
            "Execution reconciliation is current",
        ),
        (
            "Protection healthy",
            health.get("protection_ready") is True
            and health.get("protection_invalidated") is False,
            "XKO-owned protection scanner is healthy",
        ),
        (
            "Preview path ready",
            health.get("preview_ready") is True,
            "Nautilus sizing/bracket preview is available",
        ),
        (
            "Protected bracket mode",
            health.get("protected_submit_only") is True
            and health.get("protective_bracket_mode") == "OKX_ATTACHED_OCO",
            "Only venue-attached protected brackets are supported",
        ),
        (
            "Target instrument flat policy",
            health.get("target_instrument_position_policy")
            == "target_instrument_must_be_flat_before_submit",
            "Existing manual/grid exposure blocks XKO entry on the same instrument",
        ),
        (
            "Real venue lifecycle validation",
            False,
            "NOT VALIDATED: parent ACK, attached SL/TP ACK, fills, OCO, restart reconciliation",
        ),
    ]
    return [
        {"name": name, "ok": ok, "detail": detail, "blocking": not ok}
        for name, ok, detail in checks
    ]


def _alerts(health: dict[str, Any]) -> list[str]:
    alerts: list[str] = []
    if health.get("order_submit_enabled") is not False:
        alerts.append("CRITICAL: order submission is not hard-locked off")
    if health.get("unprotected_entry_enabled") is not False:
        alerts.append("CRITICAL: unprotected entry is enabled")
    if _market_entry_enabled():
        alerts.append("CRITICAL: market entry is enabled")
    if health.get("reconciliation_invalidated") is True:
        alerts.append("Reconciliation has been invalidated; restart/recovery required")
    if health.get("protection_invalidated") is True:
        alerts.append("Protection has been invalidated; restart/recovery required")
    if health.get("preview_ready") is not True:
        alerts.append(f"Preview unavailable: {health.get('readiness_reason') or 'not ready'}")
    if health.get("protection_ready") is not True:
        alerts.append(f"Protection not ready: {health.get('protection_reason') or 'not ready'}")
    return alerts


def _mode(health: dict[str, Any]) -> str:
    unsafe = (
        health.get("order_submit_enabled") is not False
        or health.get("unprotected_entry_enabled") is not False
        or _market_entry_enabled()
    )
    if unsafe:
        return "UNSAFE_CONFIGURATION"
    if str(health.get("okx_environment", "")).upper() == "LIVE":
        return "LIVE_SHADOW"
    return "DEMO_SHADOW"


def _http_error(exc: BridgeCallError) -> HTTPException:
    code = exc.status_code if 400 <= exc.status_code < 500 else 503
    return HTTPException(status_code=code, detail=exc.detail)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    if not HTML_PATH.is_file():
        raise HTTPException(status_code=500, detail=f"Missing dashboard HTML: {HTML_PATH}")
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


@app.get("/health")
def local_health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "xko-shadow-dashboard",
        "bind": f"{HOST}:{PORT}",
        "bridge_url": BRIDGE_URL,
        "submit_capability": False,
    }


@app.get("/api/state")
def state() -> dict[str, Any]:
    try:
        health = _health()
        intents = _intents(50)
    except BridgeCallError as exc:
        raise _http_error(exc) from exc
    checklist = _checklist(health)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "mode": _mode(health),
        "go_live_ready": all(item["ok"] for item in checklist),
        "health": health,
        "checklist": checklist,
        "alerts": _alerts(health),
        "intents": intents,
        "reviews": review_store.list_recent(100),
        "capabilities": {
            "read_state": True,
            "preview": True,
            "record_review": True,
            "approve": False,
            "submit": False,
        },
    }


@app.post("/api/preview/{intent_id}")
def preview(intent_id: str) -> Any:
    try:
        health = _health()
        if _mode(health) == "UNSAFE_CONFIGURATION":
            raise HTTPException(
                status_code=423,
                detail="Shadow dashboard locked because execution safety flags are not all disabled",
            )
        return _bridge_request("POST", f"/intents/{intent_id}/preview")
    except BridgeCallError as exc:
        raise _http_error(exc) from exc


@app.post("/api/review/{intent_id}")
def review(intent_id: str, request: ReviewRequest) -> dict[str, Any]:
    try:
        health = _health()
        if _mode(health) == "UNSAFE_CONFIGURATION":
            raise HTTPException(
                status_code=423,
                detail="Review logging locked because execution safety flags are not all disabled",
            )
        intent = _bridge_request("GET", f"/intents/{intent_id}")
    except BridgeCallError as exc:
        raise _http_error(exc) from exc
    if not isinstance(intent, dict):
        raise HTTPException(status_code=502, detail="Bridge intent lookup returned unexpected payload")
    return review_store.add(
        intent_id=intent_id,
        operator=request.operator,
        note=request.note,
        intent_status=str(intent.get("status", "UNKNOWN")),
        preview=request.preview,
        health=health,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            "SHADOW_DASHBOARD_HOST must remain loopback-only; refusing public bind"
        )
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
