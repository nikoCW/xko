from __future__ import annotations

import os
import threading

import uvicorn
from dotenv import load_dotenv

from nautilus_trader.adapters.okx import OKX
from nautilus_trader.adapters.okx import OKXDataClientConfig
from nautilus_trader.adapters.okx import OKXExecClientConfig
from nautilus_trader.adapters.okx import OKXLiveDataClientFactory
from nautilus_trader.adapters.okx import OKXLiveExecClientFactory
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.config import LiveExecEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.config import StrategyConfig
from nautilus_trader.config import TradingNodeConfig
from nautilus_trader.core.nautilus_pyo3 import OKXEnvironment
from nautilus_trader.core.nautilus_pyo3 import OKXInstrumentType
from nautilus_trader.core.nautilus_pyo3 import OKXMarginMode
from nautilus_trader.live.config import LiveRiskEngineConfig
from nautilus_trader.live.node import TradingNode
from nautilus_trader.model.identifiers import AccountId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.identifiers import TraderId

from bridge_runtime import BridgeRuntime, RiskPolicy, env_bool
from intent_store import IntentStore
from nautilus_service.api import create_app
from nautilus_service.bridge_strategy import AIIntentStrategy


def parse_instrument_type(raw: str) -> OKXInstrumentType:
    mapping = {
        "SPOT": OKXInstrumentType.SPOT,
        "MARGIN": OKXInstrumentType.MARGIN,
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
    limit = int(policy.max_notional_per_order_usdt)
    return {instrument_id: limit for instrument_id in policy.allowed_instruments}


def api_host() -> str:
    return os.getenv("NAUTILUS_BRIDGE_HOST", "127.0.0.1").strip()


def api_port() -> int:
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

    # The execution path is protected-only. Never permit a configuration flag to
    # reactivate the old unprotected entry path, especially against LIVE.
    if env_bool("ALLOW_UNPROTECTED_ENTRY", False):
        raise RuntimeError(
            "ALLOW_UNPROTECTED_ENTRY=true is forbidden. "
            "xko only supports protected OKX attached-OCO bracket submission."
        )

    # NautilusTrader 1.231 OKXExecutionClient derives the execution account ID as
    # "<client-name>-master". With the standard OKX client name this is OKX-master.
    # Keep the strategy/portfolio account ID identical to the execution client.
    account_id = AccountId(f"{OKX}-master")
    policy = RiskPolicy.from_env()
    store = IntentStore(os.getenv("INTENT_DB_PATH", "./data/intents.db"))
    runtime = BridgeRuntime(store, policy, "DEMO" if is_demo else "LIVE")

    instrument_type = parse_instrument_type(os.getenv("OKX_INSTRUMENT_TYPE", "SWAP"))
    margin_mode = parse_margin_mode(os.getenv("OKX_MARGIN_MODE", "CROSS"))
    load_ids = frozenset(InstrumentId.from_str(x) for x in policy.allowed_instruments)
    provider_config = InstrumentProviderConfig(load_all=False, load_ids=load_ids)

    risk_config = LiveRiskEngineConfig(
        bypass=False,
        max_order_submit_rate=os.getenv("MAX_ORDER_SUBMIT_RATE", "5/00:00:01"),
        max_order_modify_rate=os.getenv("MAX_ORDER_MODIFY_RATE", "10/00:00:01"),
        max_notional_per_order=make_risk_notional_map(policy),
        debug=False,
    )

    config_node = TradingNodeConfig(
        trader_id=TraderId("XKO-001"),
        logging=LoggingConfig(log_level="INFO", use_pyo3=True),
        exec_engine=LiveExecEngineConfig(
            reconciliation=True,
            reconciliation_instrument_ids=list(load_ids),
            graceful_shutdown_on_exception=True,
        ),
        risk_engine=risk_config,
        data_clients={
            OKX: OKXDataClientConfig(
                environment=okx_environment,
                instrument_provider=provider_config,
                instrument_types=(instrument_type,),
                http_timeout_secs=20,
            ),
        },
        exec_clients={
            OKX: OKXExecClientConfig(
                environment=okx_environment,
                instrument_provider=provider_config,
                instrument_types=(instrument_type,),
                margin_mode=margin_mode,
                http_timeout_secs=20,
            ),
        },
        timeout_connection=30.0,
        timeout_reconciliation=30.0,
        timeout_portfolio=30.0,
        timeout_disconnection=10.0,
        timeout_post_stop=5.0,
    )

    node = TradingNode(config=config_node)
    node.trader.add_strategy(
        AIIntentStrategy(
            config=StrategyConfig(
                # NautilusTrader 1.231 passes config.strategy_id directly to Logger(name=...),
                # which expects a str. Leave strategy_id unset and use a stable order ID tag.
                order_id_tag="001",
                use_hyphens_in_client_order_ids=False,
            ),
            runtime=runtime,
            account_id=account_id,
        )
    )
    node.add_data_client_factory(OKX, OKXLiveDataClientFactory)
    node.add_exec_client_factory(OKX, OKXLiveExecClientFactory)
    node.build()

    # Read-only state probe. It is invoked by AIIntentStrategy's timer on the
    # Nautilus event thread; API threads only read the resulting thread-safe gates.
    def readiness_probe() -> tuple[bool, bool, bool]:
        return (
            bool(getattr(node.portfolio, "initialized", False)),
            bool(node.kernel.exec_engine.check_connected()),
            bool(node.kernel.data_engine.check_connected()),
        )

    runtime.install_readiness_probe(readiness_probe)

    start_api(runtime)
    print(
        f"[bridge] starting Nautilus environment={'DEMO' if is_demo else 'LIVE'} "
        f"submit_enabled={policy.allow_order_submit} "
        "protected_submit_only=True protection_mode=OKX_ATTACHED_OCO "
        f"api={api_host()}:{api_port()}",
        flush=True,
    )

    try:
        node.run()
    finally:
        node.dispose()


if __name__ == "__main__":
    main()
