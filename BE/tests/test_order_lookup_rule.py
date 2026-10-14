"""The AC-08 order-lookup rule: a failed lookup is never proof that an order is gone (T10).

Flow 5 (sad.md §6): open → stays active; in history → takes its final state; `unknown_order` or in
neither list → gone; any other failure → order stays active, nothing tied to it is placed or
cancelled, the bot asks again within `error_max_retries` and pauses once the budget is spent.
"""
from __future__ import annotations

import uuid

import pytest

from app.bot_engine import grid_runtime
from app.bot_engine.error_handling import handle_bot_runtime_error
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
from app.bot_engine.exchange.port import ExchangeAdapter, InstrumentRules, NewOrder, Order, OrderLookup, Position, Ticker
from app.bot_engine.market_data import get_instrument_rules, get_last_price, get_ticker_snapshot
from app.bot_engine.orders import (
    OrderRequest,
    cancel_order,
    get_open_orders,
    place_order,
    resolve_order_state,
)
from app.bot_engine.positions import get_open_positions
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.models.user import User


@pytest.fixture(autouse=True)
def _schema(app):
    yield


def _order(status: str, *, tag: str, filled: float = 0.0) -> Order:
    return Order(
        order_id="ex-1", client_tag=tag, status=status, side="Buy", order_type="Limit", qty=0.01,
        filled_qty=filled, price=60000.0, avg_price=None, reduce_only=False,
        raw={"orderId": "ex-1", "orderLinkId": tag, "orderStatus": status, "side": "Buy",
             "orderType": "Limit", "qty": "0.01", "cumExecQty": str(filled), "price": "60000"},
    )


class FakeAdapter:
    """Answers lookups from a script; records every order-mutating call."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.lookups: list[str] = []
        self.placed: list[NewOrder] = []
        self.cancelled: list[dict] = []

    def venue(self): ...
    def capabilities(self): ...

    def instrument_rules(self, market_type, symbol):
        return InstrumentRules(min_order_qty=0.001, qty_step=0.001, tick_size=0.1, min_notional_value=5.0)

    def ticker(self, market_type, symbol):
        return Ticker(symbol=symbol, last_price=None, mark_price=61000.0)

    def candles(self, market_type, symbol, *, interval, end_ms, limit):
        return []

    def place_order(self, market_type, symbol, order):
        self.placed.append(order)
        raise AssertionError("no order may be placed in a lookup test")

    def cancel_order(self, market_type, symbol, *, order_id=None, client_tag=None):
        self.cancelled.append({"order_id": order_id, "client_tag": client_tag})
        raise AssertionError("no order may be cancelled in a lookup test")

    def open_orders(self, market_type, symbol):
        return []

    def order_history(self, market_type, symbol, *, limit):
        return []

    def lookup_order(self, market_type, symbol, client_tag):
        self.lookups.append(client_tag)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def executions(self, market_type, symbol, *, start_ms=None, end_ms=None):
        return []

    def positions(self, market_type, symbol):
        return [Position(symbol=symbol, side="Buy", size=0.02, avg_entry_price=60000.0)]

    def balance(self): ...
    def streams(self): ...
    def check_connection(self): ...


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def bot_and_order(db):
    user = User(email=f"lookup_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
    db.add(user)
    db.flush()
    bot = TradingBot(
        user_id=user.id, name="lookup rule", strategy_type="grid", category="linear", symbol="BTCUSDT",
        order_qty=0.01, grid_orders_count=2, grid_step_percent=1.0, runtime_status="running",
        exchange="bybit", environment="emulator", settings={},
    )
    db.add(bot)
    db.flush()
    order = TradingBotOrder(
        bot_id=bot.id, user_id=user.id, exchange="bybit", environment="emulator", category="linear",
        symbol="BTCUSDT", side="Buy", order_type="Limit", order_role="grid_entry_1",
        order_link_id=f"bot-{bot.id}-g1-entry-1-1", qty=0.01, price=60000.0, status="New", raw_response={},
    )
    db.add(order)
    db.commit()
    return bot, order


def _events(db, bot) -> list[str]:
    return [item.event_type for item in db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id).all()]


def test_fake_adapter_satisfies_the_port():
    assert isinstance(FakeAdapter(), ExchangeAdapter)


def test_open_order_stays_active_and_takes_the_exchange_fields(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([OrderLookup.open(_order("PartiallyFilled", tag=order.order_link_id, filled=0.004))])

    resolution = resolve_order_state(db, bot, adapter, order)

    assert resolution.state == "open"
    assert adapter.lookups == [order.order_link_id]
    assert order.status == "PartiallyFilled"
    assert order.filled_qty == pytest.approx(0.004)
    assert order.exchange_order_id == "ex-1"


def test_order_in_history_takes_its_final_state(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([OrderLookup.final(_order("Filled", tag=order.order_link_id, filled=0.01))])

    resolution = resolve_order_state(db, bot, adapter, order)

    assert resolution.state == "final"
    assert order.status == "Filled"
    assert order.filled_qty == pytest.approx(0.01)
    assert "order_reconciled_missing" not in _events(db, bot)


def test_order_in_neither_list_is_gone(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([OrderLookup.not_found()])

    resolution = resolve_order_state(db, bot, adapter, order)
    db.commit()

    assert resolution.state == "gone"
    assert order.status == "Cancelled"
    assert order.raw_response["reconciliation"]["reason"] == "missing_from_exchange"
    assert order.raw_response["reconciliation"]["previousStatus"] == "New"
    assert "order_reconciled_missing" in _events(db, bot)


def test_unknown_order_class_marks_the_order_gone(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([ExchangeError(ErrorClass.UNKNOWN_ORDER, raw_code="110001", message="Order does not exist")])

    resolution = resolve_order_state(db, bot, adapter, order)

    assert resolution.state == "gone"
    assert order.status == "Cancelled"


@pytest.mark.parametrize("error_class", [
    ErrorClass.RATE_LIMITED, ErrorClass.NETWORK, ErrorClass.EXCHANGE_UNAVAILABLE,
    ErrorClass.ORDER_STATE_CONFLICT, ErrorClass.TIME_SYNC,
])
def test_failed_lookup_keeps_the_order_active_and_raises(db, bot_and_order, error_class):
    bot, order = bot_and_order
    failure = ExchangeError(error_class, raw_code="10006", message="lookup failed")
    adapter = FakeAdapter([failure])

    with pytest.raises(ExchangeError) as raised:
        resolve_order_state(db, bot, adapter, order)

    assert raised.value is failure
    assert order.status == "New"
    assert order.raw_response == {}
    assert "order_reconciled_missing" not in _events(db, bot)
    assert adapter.placed == [] and adapter.cancelled == []


def test_five_failed_lookups_keep_the_order_active_then_the_bot_pauses(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([ExchangeError(ErrorClass.NETWORK, message="Read timed out")])

    statuses = []
    for _ in range(6):
        try:
            resolve_order_state(db, bot, adapter, order)
        except ExchangeError as exc:
            db.rollback()
            handle_bot_runtime_error(db, db.get(TradingBot, bot.id), exc)
        statuses.append(db.get(TradingBot, bot.id).runtime_status)
        assert db.get(TradingBotOrder, order.id).status == "New"

    assert statuses == ["retrying"] * 5 + ["paused"]
    assert len(adapter.lookups) == 6
    assert adapter.placed == [] and adapter.cancelled == []


def test_sync_never_marks_an_order_missing_on_a_failed_lookup(db, bot_and_order, monkeypatch):
    """The reconciliation page misses the order and the exact lookup fails: the order stays active."""
    bot, order = bot_and_order
    adapter = FakeAdapter([ExchangeError(ErrorClass.RATE_LIMITED, raw_code="10006", message="Too many visits!")])

    with pytest.raises(ExchangeError):
        grid_runtime.sync_bot_orders(db, bot, adapter=adapter)
    db.rollback()

    assert db.get(TradingBotOrder, order.id).status == "New"
    assert adapter.lookups == [order.order_link_id]
    assert adapter.placed == [] and adapter.cancelled == []


def test_sync_marks_an_order_gone_only_when_the_lookup_says_so(db, bot_and_order):
    bot, order = bot_and_order
    adapter = FakeAdapter([OrderLookup.not_found()])

    changes = grid_runtime.sync_bot_orders(db, bot, adapter=adapter)
    db.commit()

    assert db.get(TradingBotOrder, order.id).status == "Cancelled"
    assert [change["new_status"] for change in changes] == ["Cancelled"]


# ---------------------------------------------------------------- helpers on the port


def test_helpers_take_an_adapter_and_return_neutral_types(db):
    adapter = FakeAdapter([OrderLookup.not_found()])

    assert get_instrument_rules(adapter, category="linear", symbol="BTCUSDT") == InstrumentRules(
        min_order_qty=0.001, qty_step=0.001, tick_size=0.1, min_notional_value=5.0,
    )
    assert get_ticker_snapshot(adapter, category="linear", symbol="BTCUSDT").mark_price == 61000.0
    assert get_last_price(adapter, category="linear", symbol="BTCUSDT") == 61000.0
    assert [item.size for item in get_open_positions(adapter, category="linear", symbol="BTCUSDT")] == [0.02]
    assert get_open_orders(adapter, category="linear", symbol="BTCUSDT") == []


def test_place_and_cancel_go_through_the_adapter():
    adapter = FakeAdapter([OrderLookup.not_found()])
    request = OrderRequest(side="Sell", order_type="Limit", order_role="take_profit_1",
                           order_link_id="bot-1-g1-tp-1-1", qty=0.01, price=61000.0, reduce_only=True)

    with pytest.raises(AssertionError, match="no order may be placed"):
        place_order(adapter, category="linear", symbol="BTCUSDT", order=request)
    with pytest.raises(AssertionError, match="no order may be cancelled"):
        cancel_order(adapter, category="linear", symbol="BTCUSDT", order_id="ex-1")

    assert adapter.placed == [NewOrder(side="Sell", order_type="Limit", qty=0.01, price=61000.0,
                                       client_tag="bot-1-g1-tp-1-1", reduce_only=True)]
    assert adapter.cancelled == [{"order_id": "ex-1", "client_tag": None}]


def test_place_refuses_a_close_order_that_is_not_reduce_only():
    adapter = FakeAdapter([OrderLookup.not_found()])
    request = OrderRequest(side="Sell", order_type="Limit", order_role="take_profit_1",
                           order_link_id="bot-1-g1-tp-1-1", qty=0.01, price=61000.0, reduce_only=False)

    with pytest.raises(ValueError, match="reduce-only"):
        place_order(adapter, category="linear", symbol="BTCUSDT", order=request)
    assert adapter.placed == []
