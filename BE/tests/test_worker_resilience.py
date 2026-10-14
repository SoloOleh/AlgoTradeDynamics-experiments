"""A failure while resolving one bot's adapter stays with that bot (review follow-up T21; AC-10)."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.bot_engine import runtime
from app.bot_engine.runtime import tick_running_bots
from app.db.session import SessionLocal
from app.models.trading_bot import TradingBot
from app.models.user import User


@pytest.fixture(autouse=True)
def _schema(app):
    yield


def _running_bot(**values) -> int:
    db = SessionLocal()
    try:
        user = User(email=f"resilience_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        fields = {
            "exchange": "bybit", "environment": "emulator", "strategy_type": "grid", "category": "linear",
            "symbol": "BTCUSDT", "order_qty": 0.01, "grid_orders_count": 2, "grid_step_percent": 1.0,
            "settings": {}, **values,
        }
        bot = TradingBot(user_id=user.id, name="resilience", runtime_status="running", **fields)
        db.add(bot)
        db.commit()
        return bot.id
    finally:
        db.close()


def _bot(bot_id: int) -> TradingBot:
    db = SessionLocal()
    try:
        return db.get(TradingBot, bot_id)
    finally:
        db.close()


def test_a_bot_whose_adapter_cannot_be_resolved_is_stopped_alone_and_the_tick_goes_on():
    broken = _running_bot(strategy_type="momentum", settings={"lookback_candles": "abc"})
    refused = _running_bot(exchange="binance", environment="live")
    stub = SimpleNamespace(state=SimpleNamespace(bot_worker_locks={}))

    asyncio.run(tick_running_bots(stub, bot_ids={broken, refused}))

    stopped = _bot(broken)
    assert (stopped.runtime_status, stopped.last_error_type, stopped.last_error_action) == ("error", "internal", "stop")
    assert _bot(refused).runtime_status == "paused"


def test_the_worker_loop_survives_a_failed_tick(monkeypatch):
    calls = []

    class Supervisor:
        async def reconcile_if_due(self, force=False):
            return None

        async def wait_for_bot_ids(self, timeout):
            return {1}

        def consume_force_tick_ids(self, bot_ids):
            return set()

        async def stop(self):
            return None

    async def failing_tick(app, *, bot_ids=None, force_bot_ids=None):
        calls.append(bot_ids)
        if len(calls) == 1:
            raise RuntimeError("unexpected failure in one tick")
        app.state.bot_worker_running = False

    monkeypatch.setattr(runtime, "ExchangeStreamSupervisor", Supervisor)
    monkeypatch.setattr(runtime, "tick_running_bots", failing_tick)
    app = SimpleNamespace(state=SimpleNamespace(bot_worker_running=True))

    asyncio.run(runtime.worker_loop(app))

    assert len(calls) == 2
