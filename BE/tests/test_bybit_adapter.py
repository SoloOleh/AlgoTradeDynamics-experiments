"""Contract tests for the Bybit adapter against the real emulator (T06, ADR-0002)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.bot_engine.exchange.bybit.adapter import BybitAdapter
from app.bot_engine.exchange.bybit.capabilities import BYBIT_CAPABILITIES
from app.bot_engine.exchange.capabilities import Capability, MarketType
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError, UnclassifiedExchangeFailure
from app.bot_engine.exchange.port import ExchangeAdapter, NewOrder
from tests.golden.harness import EmulatorFactory, RequestRecorder


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    factory = EmulatorFactory()
    running = factory.start(tmp_path_factory.mktemp("adapter") / "emulator.db")
    running.set_price("BTCUSDT", 65000.0, simulation_time=datetime(2026, 1, 5, tzinfo=timezone.utc))
    try:
        yield running
    finally:
        factory.close()


@pytest.fixture()
def emulator_url(emulator):
    from app.core.config import get_settings

    settings = get_settings()
    original = settings.exchange_emulator_url
    settings.exchange_emulator_url = emulator.url
    try:
        yield emulator.url
    finally:
        settings.exchange_emulator_url = original


def _new_account(emulator, name: str) -> str:
    response = emulator.admin.post("/api/admin/accounts", json={"name": name, "initial_balance": 100000})
    response.raise_for_status()
    return response.json()["api_key"]


def _tag(label: str) -> str:
    return f"bot-9{int(time.time() * 1000) % 10_000_000}-g1-{label}"


def _bodies(recorder: RequestRecorder) -> list[tuple[str, str, bytes]]:
    return [(item["method"], item["path"], json.dumps(item["body"]).encode()) for item in recorder.requests if item["method"] == "POST"]


def test_adapter_implements_the_port(emulator_url):
    assert isinstance(BybitAdapter("emulator"), ExchangeAdapter)


def _legacy_place(session, *, category, symbol, side, order_type, qty, price, reduce_only, tag):
    """The pre-refactor request, verbatim from the session branch of `orders.place_order` (deleted in T20)."""
    payload = {
        "category": category,
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": str(qty),
        "orderLinkId": tag,
    }
    if price is not None:
        payload["price"] = str(price)
    if reduce_only:
        payload["reduceOnly"] = True
    return session.place_order(**payload)


def test_place_and_cancel_requests_are_byte_identical_to_todays(emulator, emulator_url):
    from app.bot_engine.exchange.bybit.client import _get_emulator_session

    key = _new_account(emulator, "bytes")
    session = _get_emulator_session(emulator_url, key)
    adapter = BybitAdapter("emulator", emulator_api_key=key)

    cases = [
        ("Buy", "Limit", 0.001, 64000.5, False, "entry-1-1"),
        ("Buy", "Market", 0.002, None, False, "dca-entry-1-1"),
        ("Sell", "Limit", 0.003, 66000.1, True, "position-tp-1"),
    ]
    for side, order_type, qty, price, reduce_only, label in cases:
        tag = _tag(label)
        with RequestRecorder().active() as old:
            _legacy_place(session, category="linear", symbol="BTCUSDT", side=side, order_type=order_type,
                          qty=qty, price=price, reduce_only=reduce_only, tag=tag)
            session.cancel_order(category="linear", symbol="BTCUSDT", orderLinkId=tag)
        with RequestRecorder().active() as new:
            adapter.place_order("linear", "BTCUSDT", NewOrder(
                side=side, order_type=order_type, qty=qty, price=price, client_tag=tag, reduce_only=reduce_only,
            ))
            adapter.cancel_order("linear", "BTCUSDT", client_tag=tag)
        assert [path for _, path, _ in _bodies(old)] == ["/v5/order/create", "/v5/order/cancel"]
        assert _bodies(new) == _bodies(old), (side, order_type)


def test_cancel_by_order_id_matches_todays_request(emulator, emulator_url):
    from app.bot_engine.exchange.bybit.client import _get_emulator_session

    key = _new_account(emulator, "cancel-id")
    adapter = BybitAdapter("emulator", emulator_api_key=key)
    placed = adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, _tag("c")))
    with RequestRecorder().active() as old:
        # The pre-refactor `orders.cancel_order` session call, verbatim.
        _get_emulator_session(emulator_url, key).cancel_order(category="linear", symbol="BTCUSDT", orderId=placed.order_id)
    with RequestRecorder().active() as new:
        adapter.cancel_order("linear", "BTCUSDT", order_id=placed.order_id)
    assert len(_bodies(old)) == 1
    assert _bodies(new) == _bodies(old)


def test_placed_order_translates_the_response(emulator, emulator_url):
    adapter = BybitAdapter("emulator", emulator_api_key=_new_account(emulator, "placed"))
    tag = _tag("p")
    placed = adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, tag))
    assert placed.accepted and placed.client_tag == tag and placed.status == "New" and placed.order_id
    assert placed.raw["result"]["orderLinkId"] == tag


def test_refused_placement_is_returned_not_raised(emulator, emulator_url):
    adapter = BybitAdapter("emulator", emulator_api_key=_new_account(emulator, "dup"))
    tag = _tag("dup")
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, tag))
    duplicate = adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, tag))
    assert not duplicate.accepted
    assert duplicate.rejection.error_class is ErrorClass.DUPLICATE_CLIENT_TAG
    assert duplicate.rejection.raw_code == "110072"


def test_lookup_order_answers_open_final_and_not_found(emulator, emulator_url):
    adapter = BybitAdapter("emulator", emulator_api_key=_new_account(emulator, "lookup"))
    resting = _tag("rest")
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, resting))
    lookup = adapter.lookup_order("linear", "BTCUSDT", resting)
    assert lookup.is_open and lookup.order.client_tag == resting and lookup.order.price == 60000.0

    adapter.cancel_order("linear", "BTCUSDT", client_tag=resting)
    cancelled = adapter.lookup_order("linear", "BTCUSDT", resting)
    assert cancelled.is_final and cancelled.order.status == "Cancelled"

    filled = _tag("mkt")
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Market", 0.001, None, filled))
    final = adapter.lookup_order("linear", "BTCUSDT", filled)
    assert final.is_final and final.order.status == "Filled" and final.order.filled_qty == 0.001

    assert adapter.lookup_order("linear", "BTCUSDT", _tag("never")).is_not_found


def test_lookup_failure_raises_and_never_answers_not_found(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "exchange_emulator_url", "http://127.0.0.1:9")
    adapter = BybitAdapter("emulator", emulator_api_key="unreachable-key")
    with pytest.raises(ExchangeError) as raised:
        adapter.lookup_order("linear", "BTCUSDT", "bot-1-g1-entry-1-1")
    assert raised.value.error_class is ErrorClass.NETWORK


class _UnrecognisedRefusal:
    """Answers every request with a refusal whose code the adapter does not map."""

    def __getattr__(self, method):
        return lambda **params: {"retCode": 170134, "retMsg": "Order price has too many decimals.", "result": {}}


def test_unrecognised_refusal_of_a_placement_stays_unclassified():
    """An unmapped code in the answer body is not an invalid order (ADR-0005; review 2026-10-18-2, finding 2)."""
    adapter = BybitAdapter("demo")
    adapter._session = _UnrecognisedRefusal()

    placed = adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, "bot-1-g1-entry-1"))

    assert not placed.accepted
    assert isinstance(placed.rejection, UnclassifiedExchangeFailure)
    assert placed.rejection.raw_code == "170134"


def test_unrecognised_refusal_of_a_lookup_raises_unclassified():
    """AC-08 clarification: an unclassified lookup failure stops the bot as internal, so it must arrive unclassified."""
    adapter = BybitAdapter("demo")
    adapter._session = _UnrecognisedRefusal()

    with pytest.raises(UnclassifiedExchangeFailure) as raised:
        adapter.lookup_order("linear", "BTCUSDT", "bot-1-g1-entry-1-1")
    assert raised.value.raw_code == "170134"


def test_reads_translate_to_neutral_types(emulator, emulator_url):
    adapter = BybitAdapter("emulator", emulator_api_key=_new_account(emulator, "reads"))
    rules = adapter.instrument_rules("linear", "BTCUSDT")
    assert (rules.min_order_qty, rules.qty_step, rules.tick_size, rules.min_notional_value) == (0.001, 0.001, 0.1, 5.0)

    ticker = adapter.ticker("linear", "BTCUSDT")
    assert ticker.last_price == 65000.0 and ticker.mark_price == 65000.0

    tag = _tag("pos")
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Market", 0.01, None, tag))
    positions = adapter.positions("linear", "BTCUSDT")
    assert len(positions) == 1
    assert (positions[0].side, positions[0].size, positions[0].avg_entry_price) == ("Buy", 0.01, 65000.0)

    open_tag = _tag("open")
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, open_tag))
    assert open_tag in {order.client_tag for order in adapter.open_orders("linear", "BTCUSDT")}
    assert tag in {order.client_tag for order in adapter.order_history("linear", "BTCUSDT", limit=50)}

    balance = adapter.balance()
    assert balance.total_available is not None and balance.total_equity is not None
    assert balance.coin_wallet == balance.total_wallet


def test_executions_carry_fees_and_realised_pnl_exactly(emulator, emulator_url):
    adapter = BybitAdapter("emulator", emulator_api_key=_new_account(emulator, "fills"))
    adapter.place_order("linear", "BTCUSDT", NewOrder("Buy", "Market", 0.01, None, _tag("in")))
    emulator.set_price("BTCUSDT", 66000.0, simulation_time=datetime(2026, 1, 5, 0, 1, tzinfo=timezone.utc))
    adapter.place_order("linear", "BTCUSDT", NewOrder("Sell", "Market", 0.01, None, _tag("out"), reduce_only=True))
    emulator.set_price("BTCUSDT", 65000.0, simulation_time=datetime(2026, 1, 5, 0, 2, tzinfo=timezone.utc))

    executions = adapter.executions("linear", "BTCUSDT")
    assert len(executions) == 2
    sell = next(item for item in executions if item.side == "Sell")
    raw = adapter._request("get_executions", category="linear", symbol="BTCUSDT", limit=100)["result"]["list"]
    raw_sell = next(item for item in raw if item["side"] == "Sell")
    assert sell.fee == Decimal(raw_sell["execFee"])
    assert sell.realised_pnl == Decimal(raw_sell["closedPnl"])
    assert sell.qty == Decimal("0.01") and sell.price == Decimal(raw_sell["execPrice"])
    assert sell.exec_seq == raw_sell["execSeq"]


def test_two_bots_with_their_own_keys_trade_on_their_own_accounts(emulator, emulator_url):
    first_key, second_key = _new_account(emulator, "bot-a"), _new_account(emulator, "bot-b")
    first, second = BybitAdapter("emulator", emulator_api_key=first_key), BybitAdapter("emulator", emulator_api_key=second_key)
    first.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.001, 60000.0, _tag("a")))
    second.place_order("linear", "BTCUSDT", NewOrder("Buy", "Limit", 0.002, 60000.0, _tag("b")))

    assert {order.qty for order in first.open_orders("linear", "BTCUSDT")} == {0.001}
    assert {order.qty for order in second.open_orders("linear", "BTCUSDT")} == {0.002}

    # Each account's balance changes only with its own bot's trade (test-plan AC-03 row).
    first_before, second_before = first.balance(), second.balance()
    first.place_order("linear", "BTCUSDT", NewOrder("Buy", "Market", 0.01, None, _tag("a-fill")))
    first_after, second_after = first.balance(), second.balance()
    assert first_after.total_available != first_before.total_available
    assert second_after == second_before
    second.place_order("linear", "BTCUSDT", NewOrder("Buy", "Market", 0.01, None, _tag("b-fill")))
    assert second.balance().total_available != second_after.total_available
    assert first.balance() == first_after


def test_bot_without_a_key_uses_the_default_emulator_account(emulator_url):
    from app.core.config import get_settings

    adapter = BybitAdapter("emulator")
    assert adapter.account_key == get_settings().exchange_emulator_default_api_key


def test_capability_declaration_matches_the_catalogue():
    everywhere = set(Capability) - {Capability.SHORT_SELLING}
    for market_type in MarketType:
        declared = BYBIT_CAPABILITIES.for_market_type(market_type)
        assert everywhere <= declared
        assert (Capability.SHORT_SELLING in declared) == (market_type != MarketType.SPOT)
    assert BYBIT_CAPABILITIES.client_tag_max_length == 36


def test_position_carries_value_and_unrealised_pnl(monkeypatch):
    adapter = BybitAdapter("demo")

    class Session:
        def get_positions(self, **params):
            return {"retCode": 0, "result": {"list": [
                {"symbol": "BTCUSDT", "side": "Buy", "size": "0.02", "avgPrice": "100.0",
                 "positionValue": "2.0", "unrealisedPnl": "0.4", "liqPrice": "", "leverage": "10",
                 "tradeMode": 0},
                {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01", "avgPrice": "101.0",
                 "unrealizedPnl": "-0.1"},
            ]}}

    adapter._session = Session()
    first, second = adapter.positions("linear", "BTCUSDT")

    assert (first.position_value, first.unrealised_pnl, first.liq_price, first.leverage) == (2.0, 0.4, None, 10.0)
    assert (second.position_value, second.unrealised_pnl) == (None, -0.1)
