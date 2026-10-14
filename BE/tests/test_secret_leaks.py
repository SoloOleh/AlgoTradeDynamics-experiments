"""No key or secret reaches any sink (T17; AC-16, ADR-0008).

Each configured venue key/secret and an emulator account key inside a stream address are pushed
through every sink: bot events (message + nested payload), the bot's last error (runtime error and
guard pause), the operations log, incidents, Telegram delivery, the clear-history cancel error and
every log record. A key appears at most as `****abcd`; a secret never appears, even in part.
"""
from __future__ import annotations

import logging
import uuid

import pytest

from app.bot_engine.exchange.redaction import get_scrubber

KEY = "LeakTestKey0000abcd"
SECRET = "LeakTestSecret-9f8e7d6c"
EMULATOR_KEY = "emu-leak-key-5150"
WS_URL = f"ws://exchange-emulator:8001/v5/private?api_key={EMULATOR_KEY}"
LEAKY = f"connect {WS_URL} failed; key={KEY} secret={SECRET} raw {KEY}/{SECRET}"


@pytest.fixture(autouse=True)
def known_credentials(app):
    scrubber = get_scrubber()
    scrubber.add_key(KEY)
    scrubber.add_secret(SECRET)
    scrubber.add_key(EMULATOR_KEY)
    yield


def assert_clean(text) -> None:
    text = str(text)
    assert KEY not in text and EMULATOR_KEY not in text
    assert SECRET not in text and SECRET[:12] not in text
    assert "****abcd" in text or "****5150" in text


@pytest.fixture()
def db():
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def bot(db):
    from app.models.trading_bot import TradingBot
    from app.models.user import User

    user = User(email=f"leak_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
    db.add(user)
    db.flush()
    item = TradingBot(
        user_id=user.id, name="leak bot", exchange="bybit", environment="emulator", strategy_type="grid",
        category="linear", symbol="BTCUSDT", order_qty=0.01, grid_orders_count=2, grid_step_percent=1.0,
        runtime_status="running", settings={},
    )
    db.add(item)
    db.commit()
    return item


def test_bot_event_message_and_nested_payload(db, bot):
    from app.bot_engine.events import log_bot_event

    event = log_bot_event(db, bot, "error", LEAKY, {"error": LEAKY, "nested": {"items": [LEAKY, 3]}})
    db.commit()

    assert_clean(event.message)
    assert_clean(event.payload["error"])
    assert_clean(event.payload["nested"]["items"][0])
    assert event.payload["nested"]["items"][1] == 3


def test_runtime_error_last_error_events_operations_log_and_incident(db, bot):
    from app.bot_engine.error_handling import handle_bot_runtime_error
    from app.models.operations import Incident, OperationLog
    from app.models.trading_bot_event import TradingBotEvent

    handle_bot_runtime_error(db, bot, RuntimeError(LEAKY))

    assert_clean(bot.last_error)
    for event in db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id):
        assert_clean(event.message)
    for item in db.query(OperationLog).filter(OperationLog.bot_id == bot.id):
        assert_clean(item.message)
    incidents = db.query(Incident).filter(Incident.bot_id == bot.id).all()
    assert incidents
    for item in incidents:
        assert_clean(item.description)


def test_guard_pause_last_error(db, bot):
    from app.bot_engine.error_handling import pause_bot_for_guard
    from app.bot_engine.exchange.errors import VenueNotSupported

    pause_bot_for_guard(db, bot, VenueNotSupported(f"bybit?api_key={EMULATOR_KEY}", "live"))
    db.commit()

    assert EMULATOR_KEY not in bot.last_error and EMULATOR_KEY[:8] not in bot.last_error
    assert "api_key=****" in bot.last_error


def test_operations_log_and_incident_written_directly(db, bot):
    from app.services.operations_service import create_bot_incident, record_operation_log

    log = record_operation_log(db, level="ERROR", service="BOT_WORKER", message=LEAKY, bot_id=bot.id)
    incident = create_bot_incident(
        db, bot=bot, severity="ERROR", incident_type="network", description=LEAKY, action_taken="retry_backoff",
    )

    assert_clean(log.message)
    assert_clean(incident.description)


def test_telegram_text_is_scrubbed_before_it_is_sent(monkeypatch):
    from app.services import telegram_notifications

    sent: list[str] = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {}}

    def post(url, json, timeout):
        sent.append(json["text"])
        return Response()

    monkeypatch.setattr(telegram_notifications.httpx, "post", post)
    telegram_notifications.send_message_sync(1, LEAKY)

    [text] = sent
    assert_clean(text)


def test_telegram_async_delivery_is_scrubbed():
    import asyncio

    from app.services import telegram_notifications

    sent: list[str] = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {}}

    class Client:
        async def post(self, url, json):
            sent.append(json["text"])
            return Response()

    asyncio.run(telegram_notifications._send_message(Client(), 1, LEAKY))
    [text] = sent
    assert_clean(text)


def test_clear_history_cancel_error_is_scrubbed(db, bot, monkeypatch):
    from app.models.user import User
    from app.services import trading_bot_service

    def failing(*args, **kwargs):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(trading_bot_service, "sync_bot_orders", failing)
    result = trading_bot_service.clear_trading_bot_history(db, bot, db.get(User, bot.user_id))

    assert_clean(result["exchange_cancel_error"])


def test_every_log_record_passes_the_scrubber(capsys):
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging()
        logging.getLogger("app.bot_engine.exchange_streams").warning("WS failed: %s (%s)", WS_URL, LEAKY)
        try:
            raise RuntimeError(LEAKY)
        except RuntimeError:
            logging.getLogger("app.anything").exception("request failed for %s", KEY)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    output = capsys.readouterr().out
    assert "WS failed" in output and "request failed" in output and "Traceback" in output
    assert_clean(output)
