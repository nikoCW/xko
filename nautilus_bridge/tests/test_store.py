from decimal import Decimal

import pytest

from intent_store import IntentStore
from trading_models import IntentStatus, Side, TradeIntentCreate


def make_request() -> TradeIntentCreate:
    return TradeIntentCreate(
        instrument_id="BTC-USDT-SWAP.OKX",
        side=Side.BUY,
        risk_pct=Decimal("0.5"),
        entry_price=Decimal("100000"),
        stop_loss=Decimal("99000"),
        take_profit=Decimal("102000"),
    )


def test_approval_and_idempotent_queue(tmp_path) -> None:
    store = IntentStore(str(tmp_path / "intents.db"))
    record, code = store.create(make_request())
    assert record.status == IntentStatus.CREATED
    assert "-" not in record.client_order_id
    assert len(record.client_order_id) <= 32

    approved = store.verify_and_approve(record.intent_id, code)
    assert approved.status == IntentStatus.APPROVED

    queued1 = store.mark_queued(record.intent_id)
    queued2 = store.mark_queued(record.intent_id)
    assert queued1.status == IntentStatus.QUEUED
    assert queued2.status == IntentStatus.QUEUED


def test_wrong_approval_code_fails(tmp_path) -> None:
    store = IntentStore(str(tmp_path / "intents.db"))
    record, _ = store.create(make_request())
    with pytest.raises(PermissionError):
        store.verify_and_approve(record.intent_id, "000000")


def test_protection_lifecycle_fields_persist(tmp_path) -> None:
    store = IntentStore(str(tmp_path / "intents.db"))
    record, _ = store.create(make_request())

    updated = store.update_execution(
        record.intent_id,
        nautilus_order_id=record.client_order_id,
        stop_loss_order_id="XKS123456789012345678901234",
        take_profit_order_id="XKT123456789012345678901234",
        protection_mode="OKX_ATTACHED_OCO",
        protection_status="ATTACHED_SUBMITTED",
        protection_verified=True,
        protection_error="temporary",
    )
    assert updated.protection_verified is True
    assert updated.protection_error == "temporary"

    cleared = store.update_execution(
        record.intent_id,
        protection_status="ATTACHED_ACTIVE",
        clear_protection_error=True,
    )
    restored = store.get(record.intent_id)
    assert cleared.protection_error is None
    assert restored.stop_loss_order_id == "XKS123456789012345678901234"
    assert restored.take_profit_order_id == "XKT123456789012345678901234"
    assert restored.protection_mode == "OKX_ATTACHED_OCO"
    assert restored.protection_status == "ATTACHED_ACTIVE"
    assert restored.protection_verified is True
    assert restored.protection_error is None
