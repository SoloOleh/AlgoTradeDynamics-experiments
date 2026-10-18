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


def test_an_unclassified_transport_failure_leaves_the_adapter_scrubbed():
    """The adapter scrubs every error it raises (ADR-0008), even one it cannot classify."""
    import traceback

    from app.bot_engine.exchange.bybit.adapter import BybitAdapter

    class Session:
        def get_tickers(self, **params):
            raise KeyError(LEAKY)

    adapter = BybitAdapter("demo")
    adapter._session = Session()
    with pytest.raises(Exception) as raised:
        adapter.ticker("linear", "BTCUSDT")

    assert_clean(raised.value)
    assert_clean("".join(traceback.format_exception(raised.value)))


def test_bot_page_snapshot_stream_errors_are_scrubbed(db, bot, monkeypatch):
    from app.api import websockets

    def failing(*args, **kwargs):
        raise RuntimeError(LEAKY)

    for name in ("get_trading_bot_performance", "get_trading_bot_position", "get_trading_bot_risk"):
        monkeypatch.setattr(websockets, name, failing)
    snapshot = websockets._build_bot_snapshot(bot.id, bot.user_id)

    assert set(snapshot["stream_errors"]) == {"performance", "position", "risk"}
    for text in snapshot["stream_errors"].values():
        assert_clean(text)


def test_uvicorns_own_handlers_pass_the_scrubber(capsys):
    """uvicorn's loggers do not propagate to the root handler; their handlers get the filter too."""
    import sys

    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    error_logger = logging.getLogger("uvicorn.error")
    saved_error = (list(error_logger.handlers), error_logger.propagate)
    uvicorn_handler = logging.StreamHandler(sys.stdout)
    error_logger.handlers[:] = [uvicorn_handler]
    error_logger.propagate = False
    try:
        configure_logging()
        try:
            raise RuntimeError(LEAKY)
        except RuntimeError:
            error_logger.exception("Exception in ASGI application")
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        error_logger.handlers[:], error_logger.propagate = saved_error

    output = capsys.readouterr().out
    assert "Exception in ASGI application" in output and "Traceback" in output
    assert_clean(output)


def test_a_malformed_log_record_does_not_raise_into_the_caller():
    from app.core.logging import ScrubbingFilter

    record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "%s and %s", ("only one",), None)
    assert ScrubbingFilter().filter(record) is True


def test_a_malformed_log_record_reaches_no_output_unscrubbed(capsys):
    """A format/argument mismatch makes the handler report the raw record on stderr (Handler.handleError)."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging()
        logging.getLogger("app.anything").warning("key=%s secret=%s on %s", KEY, LEAKY)
        try:
            raise RuntimeError(LEAKY)
        except RuntimeError:
            logging.getLogger("app.anything").exception("request %s failed: %d", SECRET)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "key=" in output and "request" in output
    assert_clean(output)


class _Unprintable:
    """An object that cannot be turned into text at all, carrying a secret it must never show."""

    def __str__(self):
        raise RuntimeError(f"cannot render {LEAKY}")

    __repr__ = __str__


def test_a_log_record_that_cannot_be_turned_into_text_does_not_raise_into_the_caller(capsys):
    """The fallback text itself must not raise: Handler.handleError does not cover filter()."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging()
        logging.getLogger("app.anything").warning(_Unprintable())
        logging.getLogger("app.anything").warning("value %s", _Unprintable())
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert output.count("unformattable log record at ") == 2
    assert "test_secret_leaks.py" in output
    # The fallback text holds neither msg nor args, so not even a masked key appears.
    assert KEY not in output and EMULATOR_KEY not in output and SECRET[:12] not in output
    assert "cannot render" not in output and "****" not in output


def test_malformed_exception_info_does_not_raise_into_the_caller(capsys):
    """Without the filter, Handler.handleError swallows a malformed exc_info; the filter must not raise it."""
    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        configure_logging()
        logging.getLogger("app.anything").error("bad exc_info", exc_info=(ValueError, "not-an-exception", None))
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "bad exc_info" in output
    assert "unformattable exception info" in output


def test_uvicorns_access_log_keeps_its_format_arguments(capsys):
    import sys

    from uvicorn.logging import AccessFormatter

    from app.core.logging import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    access = logging.getLogger("uvicorn.access")
    saved = (list(access.handlers), access.propagate, access.level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access.handlers[:] = [handler]
    access.propagate = False
    try:
        configure_logging()
        access.setLevel(logging.INFO)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/api/health", "1.1", 200)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        access.handlers[:], access.propagate, access.level = saved

    assert '127.0.0.1:5000 - "GET /api/health HTTP/1.1" 200' in capsys.readouterr().out


class _LeakyAdapter:
    """Every exchange call fails with a text holding a key and a secret."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise RuntimeError(LEAKY)
        return fail


def _tick_grid(db, bot):
    from app.bot_engine.grid_runtime import tick_grid_bot
    return tick_grid_bot(db, bot, adapter=_LeakyAdapter())


def _tick_dca(db, bot):
    from app.bot_engine.dca_runtime import tick_dca_bot
    return tick_dca_bot(db, bot, force=True, adapter=_LeakyAdapter())


def _tick_strategy(name):
    def tick(db, bot):
        from app.bot_engine.strategies import get_strategy
        return get_strategy(name).tick(db, bot, force=True, adapter=_LeakyAdapter())
    return tick


@pytest.mark.parametrize(("strategy", "tick"), [
    ("grid", _tick_grid), ("dca", _tick_dca),
    ("momentum", _tick_strategy("momentum")), ("pattern_scalper", _tick_strategy("pattern_scalper")),
])
def test_a_backtest_bots_tick_error_is_scrubbed(db, bot, strategy, tick):
    from app.models.trading_bot_event import TradingBotEvent

    bot.strategy_type, bot.is_backtest, bot.is_active = strategy, True, True
    db.commit()

    result = tick(db, bot)

    assert_clean(result["message"])
    assert_clean(bot.last_error)
    events = db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id, TradingBotEvent.event_type == "error").all()
    assert events and all(KEY not in event.message and SECRET not in event.message for event in events)


def _queued_run(db, user_id: int, strategy: str):
    from app.models.backtest import BacktestRun

    run = BacktestRun(
        user_id=user_id, dataset_id=1, dataset_name=strategy, name=strategy, bot_name=strategy, symbol="BTCUSDT",
        interval="1", start_time=0, end_time=60_000, initial_balance=10_000, fee_rate=0.0002,
        slippage_percent=0.01, path_mode="conservative", end_behavior="force_close", status="queued",
        total_candles=1, configuration={}, metrics={},
        bot_snapshot={"strategy_type": strategy, "category": "linear", "symbol": "BTCUSDT", "order_qty": 1.0,
                      "grid_orders_count": 1, "grid_step_percent": 0, "settings": {}},
    )
    db.add(run)
    db.commit()
    return run.id


@pytest.mark.parametrize(("module", "job", "target"), [
    ("app.services.backtest_service", "_run_emulator_backtest_job", "BacktestEmulatorClient.create_account"),
    ("app.services.fast_momentum_backtest", "run_fast_momentum_backtest_job", "FastMomentumBacktestEngine.__init__"),
    ("app.services.fast_scalper_backtest", "run_fast_scalper_backtest_job", "FastScalperBacktestEngine.__init__"),
])
def test_a_failed_backtest_runs_error_is_scrubbed(db, bot, monkeypatch, module, job, target):
    import importlib

    from app.models.backtest import BacktestRun
    from app.services.ui_stream_service import backtest_ui_stream_hub

    mod = importlib.import_module(module)
    owner, attr = target.split(".")

    def fail(*args, **kwargs):
        raise RuntimeError(LEAKY)

    monkeypatch.setattr(getattr(mod, owner), attr, fail)
    monkeypatch.setattr(backtest_ui_stream_hub, "publish", lambda *args, **kwargs: None)
    run_id = _queued_run(db, bot.user_id, "grid")

    getattr(mod, job)(run_id)

    db.expire_all()
    run = db.get(BacktestRun, run_id)
    assert run.status == "failed"
    assert_clean(run.error)
