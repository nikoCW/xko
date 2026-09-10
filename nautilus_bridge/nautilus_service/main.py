from __future__ import annotations

import os
import threading

import uvicorn
from dotenv import load_dotenv

from nautilus_trader.adapters.okx import OKXDataClientConfig, OKXDataClientFactory
from nautilus_trader.adapters.okx import OKXEnvironment, OKXExecutionClientConfig
from nautilus_trader.adapters.okx import OKXExecutionClientFactory, OKXInstrumentType, OKXMarginMode
from nautilus_trader.common import Environment
from nautilus_trader.config import LiveRiskEngineConfig, StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import AccountId, StrategyId, TraderId

from bridge_runtime import BridgeRuntime, RiskPolicy, env_bool
from intent_store import IntentStore
from nautilus_service.api import create_app
from nautilus_service.bridge_strategy import AIIntentStrategy


def parse_instrument_type(raw: str) -> OKXInstrumentType:
    mapping = {
        "SPOT": OKXInstrumentType.SPOT,
        "SWAP": OKXInstrumentType.SWAP,
        "FUTURES": OKXInstrumentType.FUTURES,
        "OPTION": OKXInstrumentType.OPTION,
    }
    raw = raw.strip().upper()
    if raw not in mapping:
        raise ValueError(f"Unsupported OKX_INSTRUMENT_TYPE={raw}; use one of {sorted(mapping)}")
    return mapping[raw]


def parse_margin_mode(raw: str) -> OKXMarginMode:
    mapping = {"CROSS": OKXMarginMode.CROSS, "ISOLATED": OKXMarginMode.ISOLATED}
    raw = raw.strip().upper()
    if raw not in mapping:
        raise ValueError(f"Unsupported OKX_MARGIN_MODE={raw}; use CROSS or ISOLATED")
    return mapping[raw]


def make_risk_notional_map(policy: RiskPolicy) -> dict[str, int]:
    limit = int(os.getenv("MAX_NOTIONAL_PER_ORDER_USDT", "5000"))
    return {instrument_id: limit for instrument_id in policy.allowed_instruments}


def api_host() -> str:
    return os.getenv("NAUTILUS_BRIDGE_HOST", "127.0.0.1").strip()


def api_port() -> int:
    # Render exposes its allocated port through PORT. Keep the dedicated variable
    # for local development and non-Render deployments.
    return int(os.getenv("PORT") or os.getenv("NAUTILUS_BRIDGE_PORT", "8765"))


def validate_api_security(host: str) -> None:
    token = os.getenv("BRIDGE_API_TOKEN", "").strip()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in loopback_hosts and not token:
        raise RuntimeError(
            "BRIDGE_API_TOKEN is required when the Nautilus bridge binds beyond loopback"
        )


def start_api(runtime: BridgeRuntime) -> threading.Thread:
    host = api_host()
    port = api_port()
    validate_api_security(host)
    app = create_app(runtime)

    def run() -> None:
        uvicorn.run(app, host=host, port=port, log_level="info")

    thread = threading.Thread(target=run, name="nautilus-bridge-api", daemon=True)
    thread.start()
    return thread


def main() -> None:
    load_dotenv()
    is_demo = env_bool("OKX_DEMO", True)
    okx_environment = OKXEnvironment.DEMO if is_demo else OKXEnvironment.LIVE

    if okx_environment == OKXEnvironment.LIVE and env_bool("ALLOW_UNPROTECTED_ENTRY", False):
        raise RuntimeError(
            "This starter refuses LIVE + ALLOW_UNPROTECTED_ENTRY=true. "
            "Implement/test protective exits before removing this guard."
        )

    account_id = AccountId.from_str(os.getenv("OKX_ACCOUNT_ID", "OKX-001"))
    policy = RiskPolicy.from_env()
    store = IntentStore(os.getenv("INTENT_DB_PATH", "./data/intents.db"))
    runtime = BridgeRuntime(store, policy, "DEMO" if is_demo else "LIVE")

    instrument_type = parse_instrument_type(os.getenv("OKX_INSTRUMENT_TYPE", "SWAP"))
    margin_mode = parse_margin_mode(os.getenv("OKX_MARGIN_MODE", "CROSS"))

    risk_config = LiveRiskEngineConfig(
        bypass=False,
        max_order_submit_rate=os.getenv("MAX_ORDER_SUBMIT_RATE", "5/00:00:01"),
        max_order_modify_rate=os.getenv("MAX_ORDER_MODIFY_RATE", "10/00:00:01"),
        max_notional_per_order=make_risk_notional_map(policy),
        debug=False,
    )

    node = (
        LiveNode.builder("XKO-NAUTILUS-001", TraderId.from_str("XKO-001"), Environment.LIVE)
        .with_reconciliation(reconciliation=True)
        .with_risk_engine_config(risk_config)
        .with_timeout_connection(30)
        .with_timeout_reconciliation(30)
        .with_timeout_portfolio(30)
        .add_data_client(
            None,
            OKXDataClientFactory(),
            OKXDataClientConfig(instrument_types=[instrument_type], environment=okx_environment),
        )
        .add_exec_client(
            None,
            OKXExecutionClientFactory(),
            OKXExecutionClientConfig(
                account_id=account_id,
                instrument_types=[instrument_type],
                environment=okx_environment,
                margin_mode=margin_mode,
            ),
        )
        .build()
    )

    node.add_strategy(
        AIIntentStrategy(
            config=StrategyConfig(
                strategy_id=StrategyId.from_str("AI-INTENT-001"),
                use_hyphens_in_client_order_ids=False,
            ),
            runtime=runtime,
            account_id=account_id,
        )
    )

    start_api(runtime)
    print(
        f"[bridge] starting Nautilus environment={'DEMO' if is_demo else 'LIVE'} "
        f"submit_enabled={policy.allow_order_submit} "
        f"unprotected_entry={policy.allow_unprotected_entry} "
        f"api={api_host()}:{api_port()}",
        flush=True,
    )
    node.run()


if __name__ == "__main__":
    main()
