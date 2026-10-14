from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.bot_engine.error_handling import (
    BotErrorAction,
    BotErrorType,
    classify_bot_error,
    clear_bot_runtime_error,
    handle_bot_runtime_error,
)
from app.bot_engine.exchange.bybit.errors import classify_bybit_failure
from app.bot_engine.exchange.errors import ExchangeError
from app.db.base import Base
from app.models import *  # noqa: F401,F403 - register all relationships for isolated DB
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.user import User


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _bybit_failure(message: str, *, code: int) -> ExchangeError:
    """What the Bybit adapter hands the engine for a Bybit answer with this code (ADR-0005)."""
    return classify_bybit_failure(RuntimeError(f"{message} (ErrCode: {code})"))


def _bot(db, *, suffix: str) -> TradingBot:
    user = User(email=f"errors-{suffix}@test.dev", hashed_password="test")
    db.add(user)
    db.flush()
    bot = TradingBot(
        user_id=user.id,
        name=f"Error bot {suffix}",
        exchange="bybit",
        environment="demo",
        strategy_type="grid",
        category="linear",
        symbol="BTCUSDT",
        order_qty=0.001,
        grid_orders_count=2,
        grid_step_percent=1,
        is_active=True,
        runtime_status="running",
        settings={},
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot


def test_rate_limit_is_retryable_with_backoff():
    decision = classify_bot_error(_bybit_failure("Too many visits", code=10006))
    assert decision.error_type == BotErrorType.RATE_LIMIT
    assert decision.action == BotErrorAction.RETRY_BACKOFF
    assert decision.retryable is True


def test_insufficient_funds_pauses_bot(db):
    bot = _bot(db, suffix="funds")
    decision = handle_bot_runtime_error(
        db,
        bot,
        _bybit_failure("Wallet balance is insufficient", code=110004),
    )
    db.refresh(bot)
    assert decision.error_type == BotErrorType.INSUFFICIENT_FUNDS
    assert decision.action == BotErrorAction.PAUSE
    assert bot.runtime_status == "paused"
    assert bot.last_error_type == "insufficient_funds"
    assert bot.last_error_action == "pause"
    assert bot.next_retry_at is None


def test_order_state_error_requests_resync_and_retry(db):
    bot = _bot(db, suffix="order-state")
    decision = handle_bot_runtime_error(
        db,
        bot,
        _bybit_failure("Order does not exist", code=110001),
    )
    db.refresh(bot)
    assert decision.action == BotErrorAction.RESYNC_AND_RETRY
    assert bot.runtime_status == "retrying"
    assert bot.error_retry_count == 1
    assert bot.next_retry_at is not None
    event = db.query(TradingBotEvent).filter(
        TradingBotEvent.bot_id == bot.id,
        TradingBotEvent.event_type == "bot_error",
    ).order_by(TradingBotEvent.id.desc()).first()
    assert event is not None
    assert event.payload["action"] == "resync_and_retry"
    assert event.payload["error_type"] == "order_state"


def test_authentication_error_stops_without_retry(db):
    bot = _bot(db, suffix="auth")
    decision = handle_bot_runtime_error(
        db,
        bot,
        _bybit_failure("API key is invalid", code=10003),
    )
    db.refresh(bot)
    assert decision.action == BotErrorAction.STOP
    assert bot.runtime_status == "error"
    assert bot.last_error_severity == "critical"
    assert bot.next_retry_at is None


def test_retry_limit_escalates_to_pause(db):
    bot = _bot(db, suffix="retry-limit")
    bot.settings = {"error_max_retries": 1, "error_retry_base_seconds": 1}
    db.add(bot)
    db.commit()
    handle_bot_runtime_error(db, bot, TimeoutError("temporary timeout"))
    db.refresh(bot)
    assert bot.runtime_status == "retrying"
    # Simulate another failed retry after the first budget has been consumed.
    bot.runtime_status = "running"
    db.add(bot)
    db.commit()
    handle_bot_runtime_error(db, bot, TimeoutError("temporary timeout again"))
    db.refresh(bot)
    assert bot.runtime_status == "paused"
    assert bot.last_error_action == "pause"
    assert bot.error_retry_count == 2


def test_success_clears_recovery_state(db):
    bot = _bot(db, suffix="clear")
    handle_bot_runtime_error(db, bot, TimeoutError("temporary timeout"))
    db.refresh(bot)
    assert bot.last_error_type == "network"
    bot.runtime_status = "running"
    clear_bot_runtime_error(bot)
    db.add(bot)
    db.commit()
    db.refresh(bot)
    assert bot.last_error is None
    assert bot.last_error_type is None
    assert bot.error_retry_count == 0
    assert bot.next_retry_at is None
