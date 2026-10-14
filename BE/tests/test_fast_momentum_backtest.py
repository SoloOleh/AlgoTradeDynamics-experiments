from math import sin

from app.bot_engine.strategies.momentum import Candle, _signal
from app.services.fast_momentum_backtest import MomentumSignalCache, _closed_pnl, _slipped_price


SETTINGS = {
	"timeframe": "15",
	"lookback_candles": 200,
	"position_side": "both",
	"fast_ema_period": 20,
	"slow_ema_period": 50,
	"rsi_period": 14,
	"rsi_long_threshold": 55,
	"rsi_short_threshold": 45,
	"rsi_long_ceiling": 80,
	"rsi_short_floor": 20,
	"volume_period": 20,
	"volume_multiplier": 1.2,
	"atr_period": 14,
	"atr_stop_loss_multiplier": 1.2,
	"atr_take_profit_multiplier": 2.0,
	"risk_per_trade_percent": 1.0,
	"cooldown_minutes": 0,
	"trailing_stop_enabled": True,
	"trailing_stop_atr_multiplier": 1.5,
	"minimum_signal_score": 70.0,
	"minimum_atr_percent": 0.0,
	"estimated_fee_rate": 0.0002,
	"position_sizing": "risk_capped",
	"max_position_qty": 1.0,
	"max_open_orders": 1,
	"allow_live_trading": False,
	"run_interval_seconds": 0,
}


def _history(count=260):
	result = []
	price = 100.0
	for index in range(count):
		phase = index % 120
		if phase < 35:
			drift = 0.14 + sin(index / 5.0) * 0.015
			volume = 125.0 + (index % 6) * 5.0
		elif phase < 55:
			drift = 0.26 + sin(index / 4.0) * 0.02
			volume = 260.0
		elif phase < 75:
			drift = -0.33 + sin(index / 4.0) * 0.02
			volume = 235.0
		elif phase < 95:
			drift = -0.16 + sin(index / 6.0) * 0.015
			volume = 150.0
		else:
			drift = 0.08 + sin(index / 7.0) * 0.01
			volume = 120.0
		open_price = price
		close = max(open_price + drift, 1.0)
		high = max(open_price, close) + 0.08
		low = min(open_price, close) - 0.08
		result.append(Candle(index * 900_000, open_price, high, low, close, volume))
		price = close
	return result


def test_incremental_signal_cache_matches_live_signal_logic():
	cache = MomentumSignalCache(dict(SETTINGS))
	history = []
	for candle in _history():
		cache.append(candle)
		history.append(candle)
		expected = _signal(history[-SETTINGS["lookback_candles"]:], SETTINGS)
		actual = cache.signal()
		assert actual.type == expected.type
		assert actual.side == expected.side
		assert actual.timestamp == expected.timestamp
		assert abs(actual.score - expected.score) < 1e-12
		assert abs(actual.confidence - expected.confidence) < 1e-12
		for key in expected.indicators:
			expected_value = expected.indicators[key]
			actual_value = actual.indicators[key]
			if isinstance(expected_value, (int, float)) and isinstance(actual_value, (int, float)):
				assert abs(actual_value - expected_value) < 1e-9
			else:
				assert actual_value == expected_value


def test_fast_momentum_trade_math_matches_fill_rules():
	assert _slipped_price(100, "Buy", 0.1) == 100.1
	assert _slipped_price(100, "Sell", 0.1) == 99.9
	assert abs(_closed_pnl("Buy", 100, 110, 2) - 20) < 1e-12
	assert abs(_closed_pnl("Sell", 100, 90, 2) - 20) < 1e-12


def test_fast_momentum_engine_persists_results_and_uses_snapshot_category(tmp_path, monkeypatch):
	from sqlalchemy import create_engine
	from sqlalchemy.orm import sessionmaker

	import app.models  # noqa: F401 - register all relationships
	from app.db.base import Base
	from app.models.backtest import BacktestCycle, BacktestPoint, BacktestRun
	from app.models.trading_bot_order import TradingBotOrder
	from app.models.user import User
	from app.services.backtest_service import list_executions
	from app.services.fast_momentum_backtest import FastMomentumBacktestEngine, backtest_ui_stream_hub
	from app.bot_engine.exchange.port import InstrumentRules

	db_engine = create_engine(f"sqlite:///{tmp_path / 'fast_momentum.db'}", connect_args={"check_same_thread": False})
	Base.metadata.create_all(db_engine)
	Session = sessionmaker(bind=db_engine, autoflush=False)
	db = Session()
	user = User(email="fast-momentum@test.dev", hashed_password="x")
	db.add(user)
	db.flush()

	candles = _history(320)
	start_ms = candles[0].open_time
	end_ms = candles[-1].open_time + 900_000
	run = BacktestRun(
		user_id=user.id,
		dataset_id=1,
		dataset_name="spot-momentum",
		name="spot-momentum",
		bot_name="momentum",
		symbol="BTCUSDT",
		interval="15",
		start_time=start_ms,
		end_time=end_ms,
		initial_balance=10_000,
		fee_rate=0.0002,
		slippage_percent=0.01,
		path_mode="conservative",
		end_behavior="force_close",
		status="queued",
		total_candles=len(candles),
		bot_snapshot={
			"strategy_type": "momentum",
			"category": "spot",
			"symbol": "BTCUSDT",
			"order_qty": 1.0,
			"grid_orders_count": 1,
			"grid_step_percent": 0,
			"settings": {
				**SETTINGS,
				"position_side": "long",
				"timeframe": "15",
				"minimum_signal_score": 60.0,
				"atr_take_profit_multiplier": 1.0,
				"trailing_stop_atr_multiplier": 1.1,
			},
		},
		configuration={},
		metrics={},
	)
	db.add(run)
	db.commit()
	db.refresh(run)

	instrument_calls = []

	class FakeExchange:
		def instrument_rules(self, market_type, symbol):
			instrument_calls.append({"category": market_type, "symbol": symbol})
			return InstrumentRules(min_order_qty=0.001, qty_step=0.001, tick_size=0.01, min_notional_value=5.0)

	class FakeEmulator:
		def candles(self, **_):
			for candle in candles:
				yield {
					"open_time": candle.open_time,
					"open": candle.open,
					"high": candle.high,
					"low": candle.low,
					"close": candle.close,
					"volume": candle.volume,
				}

		def close(self):
			pass

	published = []
	monkeypatch.setattr(
		backtest_ui_stream_hub,
		"publish",
		lambda user_id, run_id, reason="progress", force=False: published.append((user_id, run_id, reason, force)),
	)

	engine = FastMomentumBacktestEngine(db, run)
	engine.emulator.close()
	engine.emulator = FakeEmulator()
	engine.exchange = FakeExchange()
	engine.run_all()

	assert instrument_calls
	assert instrument_calls[0]["category"] == "spot"
	assert run.status == "completed"
	assert run.metrics["backtest_engine"] == "fast_momentum"
	assert db.query(BacktestPoint).filter_by(run_id=run.id).count() > 0
	assert db.query(BacktestCycle).filter_by(run_id=run.id).count() > 0
	orders = db.query(TradingBotOrder).filter_by(bot_id=run.temp_bot_id).all()
	assert any(order.order_role.startswith("momentum_entry_") for order in orders)
	assert any(order.order_role.startswith("momentum_") and "entry" not in order.order_role for order in orders)
	executions = list_executions(run)
	assert len(executions) == len(orders)
	assert run.metrics["closed_cycles"] >= 1
	assert run.metrics["take_profit_cycles"] + run.metrics["stop_loss_cycles"] + run.metrics["trailing_stop_cycles"] + run.metrics["signal_exit_cycles"] + run.metrics["other_exit_cycles"] == run.metrics["closed_cycles"]
	assert run.metrics["long_trades"] >= 1
	assert run.metrics["market_regime"] in {"bullish", "bearish", "sideways"}
	reasons = [item[2] for item in published]
	assert reasons[0] == "running"
	assert "progress" in reasons
	assert reasons[-1] == "completed"
	first_cycle = db.query(BacktestCycle).filter_by(run_id=run.id).order_by(BacktestCycle.cycle_number).first()
	assert (first_cycle.details or {}).get("signal_type") in {"long", "short"}
	db.close()


