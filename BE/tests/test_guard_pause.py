"""Guard before every worker tick: pause on refusal, never retry (T09; AC-10, AC-15b)."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.capabilities import Capability, CapabilitySet, MarketType
from app.bot_engine.runtime import tick_running_bots
from app.db.session import SessionLocal
from app.models.operations import Incident
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.models.user import User


@pytest.fixture(autouse=True)
def _schema(app):
    yield


@pytest.fixture()
def no_exchange_traffic(monkeypatch):
    import httpx2

    original = httpx2.Client.send

    def guarded(client, request, *args, **kwargs):
        if "/v5/" in request.url.path:
            raise AssertionError(f"exchange request sent: {request.url.path}")
        return original(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx2.Client, "send", guarded)


@pytest.fixture()
def notified(monkeypatch):
    """Notifiable events per bot id (a scheduled tick also visits other tests' running bots)."""
    sent: dict[int, list[str]] = {}
    from app.services import telegram_notifications

    def capture(db, event):
        if event.event_type in telegram_notifications.NOTIFIABLE_EVENTS:
            sent.setdefault(event.bot_id, []).append(event.event_type)

    monkeypatch.setattr(telegram_notifications, "enqueue_event_notification", capture)
    return sent


def _running_bot(**venue) -> int:
    db = SessionLocal()
    try:
        user = User(email=f"pause_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        values = {"exchange": "bybit", "environment": "emulator", **venue}
        bot = TradingBot(
            user_id=user.id, name="paused by guard", strategy_type="grid", category="linear",
            symbol="BTCUSDT", order_qty=0.01, grid_orders_count=2, grid_step_percent=1.0,
            runtime_status="running", settings={}, **values,
        )
        db.add(bot)
        db.flush()
        db.add(TradingBotOrder(
            bot_id=bot.id, user_id=user.id, exchange=bot.exchange, environment=bot.environment,
            category="linear", symbol="BTCUSDT", side="Buy", order_type="Limit", order_role="grid_entry_1",
            order_link_id=f"bot-{bot.id}-g1-entry-1-1", qty=0.01, price=60000.0, status="New", raw_response={},
        ))
        db.commit()
        return bot.id
    finally:
        db.close()


def _tick(bot_id: int | None) -> None:
    stub = SimpleNamespace(state=SimpleNamespace(bot_worker_locks={}))
    if bot_id is None:
        asyncio.run(tick_running_bots(stub))  # scheduled fallback reconciliation
    else:
        asyncio.run(tick_running_bots(stub, bot_ids={bot_id}, force_bot_ids={bot_id}))  # live event wake-up


def _state(bot_id: int):
    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        events = db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot_id).all()
        incidents = db.query(Incident).filter(Incident.bot_id == bot_id).all()
        orders = db.query(TradingBotOrder).filter(TradingBotOrder.bot_id == bot_id).all()
        return bot, events, incidents, orders
    finally:
        db.close()


@pytest.mark.parametrize("trigger", ["scheduled", "live_event"])
def test_running_bot_on_a_venue_without_adapter_is_paused_without_exchange_traffic(trigger, no_exchange_traffic, notified):
    bot_id = _running_bot(exchange="binance", environment="live")

    _tick(None if trigger == "scheduled" else bot_id)

    bot, events, incidents, orders = _state(bot_id)
    assert bot.runtime_status == "paused"
    assert bot.last_error == "Bot paused: venue binance/live has no exchange adapter"
    assert (bot.last_error_type, bot.last_error_severity, bot.last_error_action) == ("configuration", "error", "pause")
    assert bot.last_error_code is None and bot.error_retry_count == 0 and bot.next_retry_at is None
    [event] = events
    assert event.event_type == "venue_unsupported"
    assert event.message == bot.last_error
    assert event.payload == {"reason": "venue_unsupported", "exchange": "binance", "environment": "live"}
    assert [incident.incident_type for incident in incidents] == ["VENUE_UNSUPPORTED"]
    assert [(order.status, order.order_link_id) for order in orders] == [("New", f"bot-{bot_id}-g1-entry-1-1")]
    assert notified[bot_id] == ["venue_unsupported"]


def test_missing_capability_pauses_and_names_capability_and_market_type(no_exchange_traffic, notified):
    declared = CapabilitySet({MarketType.LINEAR: frozenset(Capability) - {Capability.ORDER_LOOKUP_BY_TAG}})
    calls: list[str] = []

    class StandIn:
        def capabilities(self):
            return declared

        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"stand-in called: {name}")

    bot_id = _running_bot(exchange="standin", environment="test")
    with registry.registered_venue("standin", "test", lambda account: StandIn()):
        _tick(bot_id)

    bot, events, incidents, _ = _state(bot_id)
    assert bot.runtime_status == "paused"
    assert bot.last_error == (
        "Bot paused: capability order_lookup_by_tag is not available on market type linear for venue standin/test"
    )
    [event] = events
    assert event.event_type == "capability_missing"
    assert event.payload == {
        "reason": "capability_missing", "exchange": "standin", "environment": "test",
        "capability": "order_lookup_by_tag", "market_type": "linear",
    }
    assert [incident.incident_type for incident in incidents] == ["CAPABILITY_MISSING"]
    assert calls == []
    assert notified[bot_id] == ["capability_missing"]


def test_a_retrying_bot_is_paused_not_retried(no_exchange_traffic, notified):
    bot_id = _running_bot(exchange="binance", environment="live")
    db = SessionLocal()
    try:
        bot = db.get(TradingBot, bot_id)
        bot.runtime_status = "retrying"
        bot.error_retry_count = 2
        db.commit()
    finally:
        db.close()

    _tick(bot_id)

    bot, events, _, _ = _state(bot_id)
    assert (bot.runtime_status, bot.error_retry_count, bot.next_retry_at) == ("paused", 0, None)
    assert [event.event_type for event in events] == ["venue_unsupported"]


def test_a_guard_paused_bot_is_not_paused_twice(no_exchange_traffic, notified):
    bot_id = _running_bot(exchange="binance", environment="live")
    _tick(bot_id)
    _tick(bot_id)
    _tick(None)

    _, events, incidents, _ = _state(bot_id)
    assert len(events) == 1 and len(incidents) == 1
    assert notified[bot_id] == ["venue_unsupported"]


def test_guard_refusals_never_reach_the_reaction_table():
    from app.bot_engine.error_handling import pause_bot_for_guard  # noqa: F401 - the separate pause path exists
    from app.services.telegram_notifications import ERROR_EVENTS

    assert {"venue_unsupported", "capability_missing"} <= ERROR_EVENTS
