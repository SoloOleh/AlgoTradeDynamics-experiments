"""Backtest equivalence (T20): every backtest engine gives the same result after its instrument read
moved from the emulator's `/v5/` endpoint onto the Bybit emulator adapter.

The snapshot (`golden/recordings/backtests.json`) was recorded on the commit before the change:
    GOLDEN_CAPTURE_BACKTESTS=1 pytest tests/test_backtest_equivalence.py
The instrument rules always come from a real exchange emulator; only the candle feed is synthetic.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.golden.harness import EmulatorFactory
from tests.test_fast_momentum_backtest import SETTINGS as MOMENTUM_SETTINGS, _history as momentum_history
from tests.test_fast_scalper_backtest import SETTINGS as SCALPER_SETTINGS

SNAPSHOT = Path(__file__).parent / "golden" / "recordings" / "backtests.json"
CAPTURE = os.environ.get("GOLDEN_CAPTURE_BACKTESTS") == "1"
VOLATILE = ("duration", "elapsed", "seconds", "_at", "speed", "per_second")


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    factory = EmulatorFactory()
    running = factory.start(tmp_path_factory.mktemp("backtests") / "emulator.db")
    running.set_price("BTCUSDT", 100.0, simulation_time=datetime(2026, 1, 5, tzinfo=timezone.utc))
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


@pytest.fixture(scope="module")
def results():
    collected: dict = {}
    yield collected
    if CAPTURE:
        stored = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
        SNAPSHOT.write_text(json.dumps({**stored, **collected}, indent=2, sort_keys=True) + "\n")


def _expect(results: dict, name: str, value) -> None:
    value = json.loads(json.dumps(value, default=str))
    results[name] = value
    if not CAPTURE:
        assert value == json.loads(SNAPSHOT.read_text())[name]


def _stable(metrics: dict) -> dict:
    return {key: value for key, value in metrics.items() if not any(token in key for token in VOLATILE)}


def _db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 - register all relationships
    from app.db.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'backtest.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


class CandleFeed:
    """The admin-plane candle feed; instrument rules come from the real emulator through the adapter."""

    def __init__(self, url: str, rows: list[dict]) -> None:
        self._rows = rows

    def candles(self, **_):
        yield from self._rows

    def close(self):
        pass


def _run(user_id: int, strategy: str, category: str, interval: str, rows: list[dict], settings: dict):
    from app.models.backtest import BacktestRun

    return BacktestRun(
        user_id=user_id, dataset_id=1, dataset_name=strategy, name=strategy, bot_name=strategy, symbol="BTCUSDT",
        interval=interval, start_time=rows[0]["open_time"], end_time=rows[-1]["open_time"] + 60_000,
        initial_balance=10_000, fee_rate=0.0002, slippage_percent=0.01, path_mode="conservative",
        end_behavior="force_close", status="queued", total_candles=len(rows),
        bot_snapshot={
            "strategy_type": strategy, "category": category, "symbol": "BTCUSDT", "order_qty": 1.0,
            "grid_orders_count": 1, "grid_step_percent": 0, "settings": settings,
        },
        configuration={}, metrics={},
    )


def _engine_result(db, engine_cls, run, feed, monkeypatch, hub) -> dict:
    from app.models.backtest import BacktestCycle

    monkeypatch.setattr(hub, "publish", lambda *args, **kwargs: None)
    engine = engine_cls(db, run)
    engine.emulator.close()
    engine.emulator = feed
    engine.run_all()
    cycles = db.query(BacktestCycle).filter_by(run_id=run.id).order_by(BacktestCycle.cycle_number).all()
    return {
        "status": run.status,
        "error": run.error_message if hasattr(run, "error_message") else None,
        "metrics": _stable(run.metrics or {}),
        "cycles": [(cycle.cycle_number, round(float(cycle.net_pnl or 0), 8)) for cycle in cycles],
    }


def test_fast_momentum_backtest_is_unchanged(tmp_path, emulator_url, monkeypatch, results):
    from app.models.user import User
    from app.services.fast_momentum_backtest import FastMomentumBacktestEngine, backtest_ui_stream_hub

    db = _db(tmp_path)
    user = User(email="bt-momentum@test.dev", hashed_password="x")
    db.add(user)
    db.flush()
    rows = [
        {"open_time": c.open_time, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
        for c in momentum_history(320)
    ]
    settings = {**MOMENTUM_SETTINGS, "position_side": "long", "timeframe": "15", "minimum_signal_score": 60.0,
                "atr_take_profit_multiplier": 1.0, "trailing_stop_atr_multiplier": 1.1}
    run = _run(user.id, "momentum", "linear", "15", rows, settings)
    db.add(run)
    db.commit()

    result = _engine_result(db, FastMomentumBacktestEngine, run, CandleFeed(emulator_url, rows), monkeypatch,
                            backtest_ui_stream_hub)
    assert result["status"] == "completed" and result["cycles"]
    _expect(results, "fast_momentum", result)
    db.close()


def _scalper_rows(count: int, start_ms: int) -> list[dict]:
    rows, price = [], 100.0
    for index in range(count):
        phase, open_price, volume = index % 80, price, 100.0
        if 20 <= phase <= 25:
            close, volume = open_price + 0.28, 160.0
        elif 26 <= phase <= 30:
            close, volume = open_price - 0.10, 80.0
        elif phase == 31 and len(rows) >= 5:
            close, volume = max(open_price + 0.05, max(item["high"] for item in rows[-5:]) + 0.18), 260.0
        else:
            close = open_price + 0.035
        rows.append({"open_time": start_ms + index * 300_000, "open": open_price, "high": max(open_price, close) + 0.04,
                     "low": min(open_price, close) - 0.04, "close": close, "volume": volume})
        price = close
    return rows


def test_fast_scalper_backtest_is_unchanged(tmp_path, emulator_url, monkeypatch, results):
    from app.models.user import User
    from app.services.fast_scalper_backtest import FastScalperBacktestEngine, backtest_ui_stream_hub

    db = _db(tmp_path)
    user = User(email="bt-scalper@test.dev", hashed_password="x")
    db.add(user)
    db.flush()
    rows = _scalper_rows(800, 1_700_000_000_000)
    settings = {**SCALPER_SETTINGS, "strategy_revision": 4, "timeframe": "5", "context_timeframe": "5"}
    run = _run(user.id, "pattern_scalper", "linear", "5", rows, settings)
    db.add(run)
    db.commit()

    result = _engine_result(db, FastScalperBacktestEngine, run, CandleFeed(emulator_url, rows), monkeypatch,
                            backtest_ui_stream_hub)
    assert result["status"] == "completed"
    _expect(results, "fast_scalper", result)
    db.close()


def test_emulator_backtest_quantity_check_is_unchanged(app, emulator, emulator_url, results):
    from app.db.session import SessionLocal
    from app.models.trading_bot import TradingBot
    from app.models.user import User
    from app.schemas.backtest import BacktestCreate
    from app.services.backtest_service import create_backtest

    start_ms = 1_767_225_600_000
    rows = [(start_ms + i * 60_000, 100.0, 100.5, 99.5, 100.0 + (i % 5) * 0.1, 10.0) for i in range(120)]
    dataset_id = emulator.import_candles("BTCUSDT", rows)

    db = SessionLocal()
    try:
        user = User(email=f"bt_{uuid.uuid4().hex[:10]}@test.dev", hashed_password="x")
        db.add(user)
        db.flush()
        outcomes = {}
        for label, qty in (("too_small", 0.00001), ("enough", 1.0)):
            bot = TradingBot(
                user_id=user.id, name=f"grid {label}", exchange="bybit", environment="emulator", strategy_type="grid",
                category="linear", symbol="BTCUSDT", order_qty=qty, grid_orders_count=3, grid_step_percent=1.0,
                settings={},
            )
            db.add(bot)
            db.commit()
            payload = BacktestCreate(bot_id=bot.id, dataset_id=dataset_id, start_time=rows[0][0], end_time=rows[-1][0])
            try:
                run = create_backtest(db, payload, user.id)
                outcomes[label] = {"created": True, "engine": run.configuration["backtest_engine"],
                                   "total_candles": run.total_candles}
            except ValueError as exc:
                db.rollback()
                outcomes[label] = {"created": False, "error": str(exc)}
    finally:
        db.close()

    assert outcomes["too_small"]["created"] is False and outcomes["enough"]["created"] is True
    _expect(results, "emulator_quantity_check", outcomes)
