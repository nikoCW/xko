from types import SimpleNamespace

from nautilus_trader.model.enums import OrderSide, OrderType

from nautilus_service.protection_coverage_strategy import AIIntentStrategy


class Qty:
    def __init__(self, value: str) -> None:
        self._value = value

    def as_decimal(self):
        return self._value


class FakeStore:
    def __init__(self, stop_id: str = "XKS-STOP") -> None:
        self._record = SimpleNamespace(stop_loss_order_id=stop_id)

    def get(self, _intent_id: str):
        return self._record


class FakeCache:
    def __init__(self, orders) -> None:
        self._orders = orders

    def orders_open(self, **_kwargs):
        return list(self._orders)


def fake_strategy(order):
    return SimpleNamespace(
        _runtime=SimpleNamespace(store=FakeStore()),
        _account_id="OKX-master",
        cache=FakeCache([order]),
        _quantity_decimal=AIIntentStrategy._quantity_decimal,
    )


def fake_position(*, long: bool, quantity: str = "10"):
    return SimpleNamespace(
        instrument_id="BTC-USDT-SWAP.OKX",
        is_long=long,
        is_short=not long,
        side="LONG" if long else "SHORT",
        quantity=Qty(quantity),
    )


def fake_stop(*, side, leaves: str = "10", reduce_only: bool = True):
    return SimpleNamespace(
        client_order_id="XKS-STOP",
        order_type=OrderType.STOP_MARKET,
        side=side,
        is_reduce_only=reduce_only,
        leaves_qty=Qty(leaves),
    )


def coverage_issue(strategy, position):
    return AIIntentStrategy._xko_stop_coverage_issue(strategy, "intent-1", position)


def test_long_requires_sell_stop_with_full_coverage():
    strategy = fake_strategy(fake_stop(side=OrderSide.SELL, leaves="10"))
    assert coverage_issue(strategy, fake_position(long=True, quantity="10")) is None


def test_short_requires_buy_stop_with_full_coverage():
    strategy = fake_strategy(fake_stop(side=OrderSide.BUY, leaves="10"))
    assert coverage_issue(strategy, fake_position(long=False, quantity="10")) is None


def test_wrong_stop_side_fails_closed():
    strategy = fake_strategy(fake_stop(side=OrderSide.BUY, leaves="10"))
    issue = coverage_issue(strategy, fake_position(long=True, quantity="10"))
    assert issue is not None and issue.startswith("stop_wrong_side:")


def test_undercovered_stop_fails_closed():
    strategy = fake_strategy(fake_stop(side=OrderSide.SELL, leaves="9"))
    issue = coverage_issue(strategy, fake_position(long=True, quantity="10"))
    assert issue == "stop_undercovered:leaves=9:position=10"


def test_non_reduce_only_stop_fails_closed():
    strategy = fake_strategy(fake_stop(side=OrderSide.SELL, reduce_only=False))
    assert coverage_issue(strategy, fake_position(long=True)) == "stop_not_reduce_only"
