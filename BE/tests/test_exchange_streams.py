from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app.bot_engine.exchange.bybit.adapter import BybitAdapter
from app.bot_engine.exchange.bybit.streams import auth_payload
from app.bot_engine.exchange_streams import (
    ExchangeStreamEvent,
    ExchangeStreamSupervisor,
)
from app.core.config import get_settings


def _bot(**overrides):
    values = {
        "id": 1,
        "environment": "emulator",
        "category": "linear",
        "symbol": "BTCUSDT",
        "settings": {"emulator_api_key": "emu-key"},
        "strategy_type": "grid",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_emulator_uses_bybit_like_public_and_private_ws_paths(monkeypatch):
    monkeypatch.setenv("EXCHANGE_EMULATOR_URL", "http://exchange-emulator:8001")
    get_settings.cache_clear()
    public, private = BybitAdapter("emulator", emulator_api_key="emu-key").streams().sources("linear", "BTCUSDT")
    assert (public.authenticate, private.authenticate) == (False, False)
    assert urlparse(public.url).path == "/v5/public/linear"
    assert urlparse(private.url).path == "/v5/private"
    assert parse_qs(urlparse(public.url).query)["api_key"] == ["emu-key"]
    assert parse_qs(urlparse(private.url).query)["api_key"] == ["emu-key"]


def test_bybit_demo_uses_mainnet_public_and_demo_private(monkeypatch):
    monkeypatch.setenv("BYBIT_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "demo-secret")
    get_settings.cache_clear()
    streams = BybitAdapter("demo").streams()
    public, private = streams.sources("linear", "BTCUSDT")
    assert public.url == "wss://stream.bybit.com/v5/public/linear"
    assert private.url == "wss://stream-demo.bybit.com/v5/private"
    assert private.authenticate is True
    assert streams.credentials() == ("demo-key", "demo-secret")


def test_private_auth_payload_is_bybit_hmac(monkeypatch):
    monkeypatch.setattr("app.bot_engine.exchange.bybit.streams.time.time", lambda: 1000.0)
    payload = auth_payload("key", "secret")
    expires = 1_010_000
    expected = hmac.new(b"secret", f"GET/realtime{expires}".encode(), hashlib.sha256).hexdigest()
    assert payload == {"op": "auth", "args": ["key", expires, expected]}


def test_private_exchange_events_force_immediate_reconciliation():
    async def scenario():
        supervisor = ExchangeStreamSupervisor()
        await supervisor.on_event(ExchangeStreamEvent(
            bot_id=7, topic="execution", data={"symbol": "BTCUSDT"}, received_at=time.time()
        ))
        bot_ids = await supervisor.wait_for_bot_ids(0.1)
        assert bot_ids == {7}
        assert supervisor.consume_force_tick_ids(bot_ids) == {7}
        assert supervisor.consume_force_tick_ids(bot_ids) == set()

    asyncio.run(scenario())


def test_ticker_event_does_not_force_strategy_interval_bypass():
    async def scenario():
        supervisor = ExchangeStreamSupervisor()
        supervisor._ticker_intervals[8] = 0.5
        await supervisor.on_event(ExchangeStreamEvent(
            bot_id=8, topic="tickers.BTCUSDT", data={"symbol": "BTCUSDT"}, received_at=time.time()
        ))
        bot_ids = await supervisor.wait_for_bot_ids(0.1)
        assert bot_ids == {8}
        assert supervisor.consume_force_tick_ids(bot_ids) == set()

    asyncio.run(scenario())


# ---------------------------------------------------------------- neutral events (T16)

import json  # noqa: E402
import logging  # noqa: E402

import pytest  # noqa: E402

from app.bot_engine.exchange.port import Order, Position, StreamEvent, Ticker  # noqa: E402


def _streams():
    return BybitAdapter("emulator", emulator_api_key="emu-key").streams()


@pytest.mark.parametrize("message, kind, check", [
    ({"topic": "order", "data": [{"symbol": "BTCUSDT", "orderLinkId": "bot-1-g1-entry-1-1", "orderStatus": "Filled",
                                   "side": "Buy", "orderType": "Limit", "qty": "0.01", "cumExecQty": "0.01",
                                   "price": "60000"}]},
     "order", lambda payload: isinstance(payload["orders"][0], Order) and payload["orders"][0].status == "Filled"),
    ({"topic": "execution", "data": [{"symbol": "BTCUSDT", "execId": "e1", "orderLinkId": "bot-1-g1-entry-1-1",
                                       "side": "Buy", "execQty": "0.01", "execPrice": "60000", "execFee": "0.03",
                                       "closedPnl": "0", "execTime": "1700000000000"}]},
     "execution", lambda payload: payload["executions"][0].fee == 0.03 and payload["executions"][0].qty == 0.01),
    ({"topic": "position", "data": [{"symbol": "BTCUSDT", "side": "Buy", "size": "0.02", "avgPrice": "60000"}]},
     "position", lambda payload: isinstance(payload["positions"][0], Position) and payload["positions"][0].size == 0.02),
    ({"topic": "wallet", "data": [{"accountType": "UNIFIED", "totalEquity": "1000"}]},
     "wallet", lambda payload: payload["balance"].total_equity == 1000.0),
    ({"topic": "tickers.BTCUSDT", "data": {"symbol": "BTCUSDT", "lastPrice": "60100", "markPrice": "60090"}},
     "ticker", lambda payload: payload["ticker"] == Ticker("BTCUSDT", 60100.0, 60090.0)),
])
def test_each_topic_translates_to_a_neutral_event(message, kind, check):
    event = _streams().translate(message, "BTCUSDT")

    assert isinstance(event, StreamEvent)
    assert (event.kind, event.symbol) == (kind, "BTCUSDT")
    assert check(event.payload)
    assert not any(key in json.dumps(event.payload, default=repr) for key in ("orderLinkId", "closedPnl", "execFee"))


def test_messages_for_another_symbol_or_without_a_topic_are_dropped():
    streams = _streams()
    assert streams.translate({"topic": "order", "data": [{"symbol": "ETHUSDT", "orderStatus": "New"}]}, "BTCUSDT") is None
    assert streams.translate({"op": "pong"}, "BTCUSDT") is None
    assert streams.translate({"topic": "tickers.ETHUSDT", "data": {"symbol": "ETHUSDT"}}, "BTCUSDT") is None


def test_a_bot_stream_emits_neutral_data_under_todays_topics(monkeypatch):
    from app.bot_engine import exchange_streams

    received = []

    async def on_event(event):
        received.append(event)

    async def fake_run(self, source, symbol, *, on_event, on_connected, **_):
        await on_connected(source.label)
        await on_event(self.translate({"topic": "order", "data": [{"symbol": symbol, "orderStatus": "New"}]}, symbol))
        await on_event(self.translate({"topic": f"tickers.{symbol}", "data": {"symbol": symbol, "lastPrice": "1"}}, symbol))

    monkeypatch.setattr("app.bot_engine.exchange.bybit.streams.BybitStreams.run", fake_run)

    async def scenario():
        stream = exchange_streams.BotExchangeStream(
            _bot(), BybitAdapter("emulator", emulator_api_key="emu-key"), on_event,
        )
        stream.start()
        await asyncio.sleep(0.05)
        await stream.stop()

    asyncio.run(scenario())
    topics = sorted(event.topic for event in received)
    assert topics == sorted(["system.connected", "system.connected", "order", "order", "tickers.BTCUSDT", "tickers.BTCUSDT"])
    assert all(isinstance(event.data, StreamEvent) for event in received if not event.topic.startswith("system."))


def test_emulator_key_in_a_failing_ws_url_never_reaches_a_log_line(monkeypatch, caplog):
    key = "emu-secret-key-7731"
    streams = BybitAdapter("emulator", emulator_api_key=key).streams()
    public, _ = streams.sources("linear", "BTCUSDT")

    def failing_connect(url, **kwargs):
        raise OSError(f"cannot connect to {url}")

    monkeypatch.setattr("app.bot_engine.exchange.bybit.streams.websockets.connect", failing_connect)

    async def nothing(*args, **kwargs):
        return None

    async def scenario():
        task = asyncio.create_task(streams.run(public, "BTCUSDT", on_event=nothing, on_connected=nothing))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())

    lines = [record.getMessage() for record in caplog.records]
    assert any("disconnected" in line for line in lines)
    assert not any(key in line for line in lines)
    assert any("****7731" in line for line in lines)


def test_supervisor_opens_no_stream_for_a_bot_whose_venue_has_no_adapter(monkeypatch):
    from types import SimpleNamespace as NS

    import httpx2

    from app.bot_engine import exchange_streams

    def no_traffic(*args, **kwargs):
        raise AssertionError("no exchange request may be sent")

    monkeypatch.setattr(httpx2.Client, "send", no_traffic)
    monkeypatch.setattr("app.bot_engine.exchange.bybit.streams.websockets.connect", no_traffic)
    bots = [NS(id=41, exchange="binance", environment="live", category="linear", symbol="BTCUSDT",
               strategy_type="grid", settings={}, runtime_status="running")]

    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return bots

    class Db:
        def query(self, model):
            return Query()

        def close(self):
            pass

    monkeypatch.setattr(exchange_streams, "SessionLocal", Db)

    async def scenario():
        supervisor = ExchangeStreamSupervisor()
        await supervisor.reconcile_if_due(force=True)
        assert supervisor._streams == {}
        await supervisor.stop()

    asyncio.run(scenario())


def test_live_event_for_a_bot_whose_venue_was_removed_pauses_it_without_requests(client, monkeypatch):
    import uuid

    import httpx2

    from app.bot_engine.runtime import tick_running_bots
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot
    from app.models.user import User

    def no_traffic(*args, **kwargs):
        raise AssertionError("no exchange request may be sent")

    monkeypatch.setattr(httpx2.Client, "send", no_traffic)
    db = SessionLocal()
    try:
        user = User(email=f"stream_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        bot = TradingBot(
            user_id=user.id, name="stream bot", exchange="retired", environment="live", strategy_type="grid",
            category="linear", symbol="BTCUSDT", order_qty=0.01, grid_orders_count=2, grid_step_percent=1.0,
            runtime_status="running", settings={},
        )
        db.add(bot)
        db.commit()
        bot_id = bot.id
    finally:
        db.close()

    async def scenario():
        supervisor = ExchangeStreamSupervisor()
        await supervisor.on_event(ExchangeStreamEvent(bot_id=bot_id, topic="order", data=None, received_at=time.time()))
        bot_ids = await supervisor.wait_for_bot_ids(0.1)
        app = SimpleNamespace(state=SimpleNamespace(bot_worker_locks={}))
        await tick_running_bots(app, bot_ids=bot_ids, force_bot_ids=supervisor.consume_force_tick_ids(bot_ids))

    asyncio.run(scenario())
    db = SessionLocal()
    try:
        paused = db.get(TradingBot, bot_id)
        assert paused.runtime_status == "paused"
        assert paused.last_error == "Bot paused: venue retired/live has no exchange adapter"
    finally:
        db.close()
