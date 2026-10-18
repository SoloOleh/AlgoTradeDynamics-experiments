import random
from math import sin

from app.bot_engine.strategies.pattern_scalper import Candle, _signal
from app.services.fast_scalper_backtest import (
    PatternSignalCache,
    _closed_pnl,
    _slipped_price,
)


SETTINGS = {
    "strategy_revision": 2,
    "lookback_candles": 200,
    "ema_fast_period": 20,
    "ema_slow_period": 50,
    "rsi_period": 14,
    "atr_period": 14,
    "breakout_lookback": 20,
    "volume_lookback": 20,
    "volume_multiplier": 1.2,
    "minimum_signal_score": 0.70,
    "rsi_long_min": 50,
    "rsi_long_max": 72,
    "rsi_short_min": 28,
    "rsi_short_max": 50,
    "allow_short": True,
}


def _history(count=650):
    rng = random.Random(42)
    result = []
    price = 100.0
    for index in range(count):
        drift = 0.025 + sin(index / 17) * 0.08 + rng.uniform(-0.18, 0.18)
        open_price = price
        close = max(open_price + drift, 1)
        high = max(open_price, close) + rng.uniform(0.05, 0.35)
        low = min(open_price, close) - rng.uniform(0.05, 0.35)
        volume = 80 + rng.uniform(0, 70)
        if index % 67 == 0:
            volume *= 2.0
            close += 0.55
            high = max(high, close + 0.1)
        result.append(Candle(index * 300_000, open_price, high, low, close, volume))
        price = close
    return result


def test_incremental_signal_cache_matches_live_signal_logic():
    cache = PatternSignalCache(dict(SETTINGS))
    history = []
    for candle in _history():
        cache.append(candle)
        history.append(candle)
        expected = _signal(history[-SETTINGS["lookback_candles"]:], SETTINGS)
        actual = cache.signal()
        if expected is None:
            assert actual is None
            continue
        assert actual is not None
        assert actual.side == expected.side
        assert actual.candle_time == expected.candle_time
        assert abs(actual.score - expected.score) < 1e-12
        assert abs(actual.atr - expected.atr) < 1e-10
        for key in expected.indicators:
            assert abs(actual.indicators[key] - expected.indicators[key]) < 1e-9


def test_fast_trade_math_matches_emulator_market_fill_rules():
    assert _slipped_price(100, "Buy", 0.1) == 100.1
    assert _slipped_price(100, "Sell", 0.1) == 99.9
    assert abs(_closed_pnl("Buy", 100, 110, 2) - 20) < 1e-12
    assert abs(_closed_pnl("Sell", 100, 90, 2) - 20) < 1e-12


def test_fast_engine_persists_compatible_results(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401 - register all relationships
    from app.db.base import Base
    from app.models.backtest import BacktestCycle, BacktestPoint, BacktestRun
    from app.models.trading_bot_order import TradingBotOrder
    from app.models.user import User
    from app.services.backtest_service import list_executions
    from app.services.fast_scalper_backtest import FastScalperBacktestEngine, backtest_ui_stream_hub
    from app.bot_engine.exchange.port import InstrumentRules

    db_engine = create_engine(f"sqlite:///{tmp_path / 'fast.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(db_engine)
    Session = sessionmaker(bind=db_engine, autoflush=False)
    db = Session()
    user = User(email="fast@test.dev", hashed_password="x")
    db.add(user)
    db.flush()

    count = 800
    start_ms = 1_700_000_000_000
    settings = {
        **SETTINGS,
        "strategy_revision": 4,
        "timeframe": "5",
        "context_timeframe": "5",
        "volume_multiplier": 1.2,
        "pattern_volume_multiplier": 1.2,
        "minimum_signal_score": 0.70,
        "require_trend_confirmation": True,
        "require_breakout_confirmation": True,
        "require_volume_confirmation": True,
        "require_rsi_confirmation": False,
        "require_retest_confirmation": True,
        "breakout_buffer_atr": 0.01,
        "minimum_body_atr": 0.05,
        "maximum_breakout_body_atr": 5.0,
        "minimum_ema_separation_atr": 0.0,
        "minimum_ema_slope_atr": 0.0,
        "minimum_breakout_close_location": 0.55,
        "retest_tolerance_atr": 0.8,
        "retest_max_penetration_atr": 0.8,
        "retest_reclaim_atr": 0.0,
        "minimum_confirmation_body_atr": 0.0,
        "context_ema_fast_period": 12,
        "context_ema_slow_period": 36,
        "context_structure_lookback": 12,
        "context_min_ema_separation_atr": 0.03,
        "context_min_ema_slope_atr": 0.005,
        "enable_breakout_retest": True,
        "enable_flag": True,
        "enable_triangle": True,
        "enable_double_top_bottom": True,
        "enable_liquidity_sweep": True,
        "flag_impulse_lookback": 6,
        "flag_pullback_lookback": 5,
        "flag_min_impulse_atr": 1.2,
        "flag_max_retrace": 0.7,
        "triangle_lookback": 12,
        "triangle_min_contraction": 0.2,
        "double_pattern_lookback": 32,
        "double_pattern_tolerance_atr": 0.45,
        "double_pattern_min_separation": 5,
        "liquidity_sweep_lookback": 20,
        "liquidity_sweep_penetration_atr": 0.08,
        "liquidity_sweep_reclaim_atr": 0.04,
        "stop_loss_atr": 1.2,
        "take_profit_atr": 1.8,
        "max_holding_minutes": 30,
        "cooldown_minutes": 0,
        "risk_per_trade_percent": 0.5,
        "max_daily_loss_percent": 20,
        "position_sizing": "risk_capped",
        "max_position_qty": 1.0,
        "max_notional_usdt": None,
    }
    run = BacktestRun(
        user_id=user.id,
        dataset_id=1,
        dataset_name="fast",
        name="fast",
        bot_name="scalper",
        symbol="BTCUSDT",
        interval="5",
        start_time=start_ms,
        end_time=start_ms + count * 300_000,
        initial_balance=10_000,
        fee_rate=0.0002,
        slippage_percent=0.01,
        path_mode="conservative",
        end_behavior="force_close",
        status="queued",
        total_candles=count,
        bot_snapshot={
            "strategy_type": "pattern_scalper",
            "category": "linear",
            "symbol": "BTCUSDT",
            "order_qty": 1.0,
            "grid_orders_count": 2,
            "grid_step_percent": 5,
            "settings": settings,
        },
        configuration={},
        metrics={},
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    class FakeExchange:
        def instrument_rules(self, market_type, symbol):
            return InstrumentRules(min_order_qty=0.001, qty_step=0.001, tick_size=0.01, min_notional_value=5.0)

    class FakeEmulator:
        def candles(self, **_):
            generated = []
            price = 100.0
            for index in range(count):
                phase = index % 80
                open_price = price
                volume = 100.0
                if 20 <= phase <= 25:
                    close = open_price + 0.28
                    volume = 160.0
                elif 26 <= phase <= 30:
                    close = open_price - 0.10
                    volume = 80.0
                elif phase == 31 and len(generated) >= 5:
                    level = max(item["high"] for item in generated[-5:])
                    close = max(open_price + 0.05, level + 0.18)
                    volume = 260.0
                else:
                    close = open_price + 0.035
                high = max(open_price, close) + 0.04
                low = min(open_price, close) - 0.04
                item = {
                    "open_time": start_ms + index * 300_000,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
                generated.append(item)
                yield item
                price = close

        def close(self):
            pass

    published = []
    monkeypatch.setattr(
        backtest_ui_stream_hub,
        "publish",
        lambda user_id, run_id, reason="progress", force=False: published.append((user_id, run_id, reason, force)),
    )

    engine = FastScalperBacktestEngine(db, run)
    engine.emulator.close()
    engine.emulator = FakeEmulator()
    engine.exchange = FakeExchange()
    engine.run_all()

    assert run.status == "completed"
    assert run.metrics["backtest_engine"] == "fast_scalper"
    assert run.emulator_account_id is None
    assert db.query(BacktestPoint).filter_by(run_id=run.id).count() > 0
    assert db.query(BacktestCycle).filter_by(run_id=run.id).count() > 0
    orders = db.query(TradingBotOrder).filter_by(bot_id=run.temp_bot_id).all()
    assert len(orders) >= 2
    executions = list_executions(run)
    assert len(executions) == len(orders)
    assert all(item.get("execId") for item in executions)
    assert any(order.order_role.startswith("scalper_entry") for order in orders)
    assert any(order.order_role.startswith("scalper_") and "entry" not in order.order_role for order in orders)
    exit_count = (
        run.metrics["take_profit_cycles"]
        + run.metrics["stop_loss_cycles"]
        + run.metrics["timeout_cycles"]
        + run.metrics["other_exit_cycles"]
    )
    assert exit_count == run.metrics["closed_cycles"]
    assert run.metrics["average_fee_per_cycle"] >= 0
    assert run.configuration["engine_version"] == 4
    reasons = [item[2] for item in published]
    assert reasons[0] == "running"
    assert "progress" in reasons
    assert reasons[-1] == "completed"
    assert run.metrics["pattern_performance"]
    assert any(item["pattern"] == "bull_flag" for item in run.metrics["pattern_performance"])
    first_cycle = db.query(BacktestCycle).filter_by(run_id=run.id).order_by(BacktestCycle.cycle_number).first()
    assert (first_cycle.details or {}).get("signal_pattern")
    db.close()
