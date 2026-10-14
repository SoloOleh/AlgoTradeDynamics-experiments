from math import sin

from app.bot_engine.strategies.pattern_scalper import Candle, _settings, _signal
from app.models.trading_bot import TradingBot


SETTINGS = {
    "strategy_revision": 2,
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


def _trend_candles(direction: int) -> list[Candle]:
    candles = []
    base = 100 if direction > 0 else 110
    for index in range(60):
        close = base + direction * index * 0.08 + sin(index * 1.7) * 0.35
        open_price = close - direction * (0.15 if index % 2 == 0 else -0.08)
        candles.append(Candle(
            open_time=index * 300_000,
            open=open_price,
            high=max(open_price, close) + 0.2,
            low=min(open_price, close) - 0.2,
            close=close,
            volume=100,
        ))

    latest = candles[-1]
    if direction > 0:
        breakout = max(item.high for item in candles[-21:-1]) + 0.25
        candles[-1] = Candle(latest.open_time, breakout - 0.5, breakout + 0.1, breakout - 0.6, breakout, 180)
    else:
        breakout = min(item.low for item in candles[-21:-1]) - 0.25
        candles[-1] = Candle(latest.open_time, breakout + 0.5, breakout + 0.6, breakout - 0.1, breakout, 180)
    return candles


def test_pattern_scalper_detects_long_breakout():
    signal = _signal(_trend_candles(1), SETTINGS)
    assert signal is not None
    assert signal.side == "Buy"
    assert signal.score >= SETTINGS["minimum_signal_score"]
    assert signal.atr > 0


def test_pattern_scalper_detects_short_breakout():
    signal = _signal(_trend_candles(-1), SETTINGS)
    assert signal is not None
    assert signal.side == "Sell"
    assert signal.score >= SETTINGS["minimum_signal_score"]
    assert signal.atr > 0


def test_v1_bot_is_upgraded_to_quality_entry_defaults():
    bot = TradingBot(
        user_id=1,
        name="legacy scalper",
        strategy_type="pattern_scalper",
        category="linear",
        symbol="BTCUSDT",
        order_qty=0.001,
        settings={
            "minimum_signal_score": 0.70,
            "volume_multiplier": 1.2,
            "cooldown_minutes": 5,
        },
    )
    settings = _settings(bot)
    assert settings["strategy_revision"] == 4
    assert settings["minimum_signal_score"] == 0.85
    assert settings["volume_multiplier"] == 3.0
    assert settings["cooldown_minutes"] == 15.0
    assert settings["require_trend_confirmation"] is True
    assert settings["require_breakout_confirmation"] is True
    assert settings["require_volume_confirmation"] is True
    assert settings["require_rsi_confirmation"] is True
    assert settings["require_retest_confirmation"] is True
    assert settings["breakout_buffer_atr"] >= 0.08
    assert settings["minimum_ema_separation_atr"] >= 0.08
    assert settings["context_timeframe"] == "5"
    assert settings["pattern_volume_multiplier"] == 1.5
    assert settings["enable_flag"] is True
    assert settings["enable_triangle"] is True
    assert settings["enable_double_top_bottom"] is True
    assert settings["enable_liquidity_sweep"] is True


def test_quality_mode_rejects_breakout_without_required_volume():
    settings = {**SETTINGS, "minimum_signal_score": 0.70, "volume_multiplier": 3.0}
    candles = _trend_candles(1)
    latest = candles[-1]
    candles[-1] = Candle(
        latest.open_time, latest.open, latest.high, latest.low, latest.close, 150
    )
    assert _signal(candles, settings) is None


def _v3_retest_candles(direction: int, *, hold: bool = True) -> list[Candle]:
    candles: list[Candle] = []
    price = 100.0 if direction > 0 else 120.0
    for index in range(70):
        drift = 0.12 * direction
        open_price = price
        close = open_price + drift
        high = max(open_price, close) + 0.12
        low = min(open_price, close) - 0.12
        candles.append(Candle(index * 300_000, open_price, high, low, close, 100.0))
        price = close

    prior = candles[-20:]
    if direction > 0:
        level = max(item.high for item in prior)
        breakout_open = level - 0.10
        breakout_close = level + 0.45
        candles.append(Candle(70 * 300_000, breakout_open, breakout_close + 0.05, breakout_open - 0.05, breakout_close, 450.0))
        if hold:
            confirm_open = level + 0.06
            confirm_close = level + 0.22
            confirm_low = level - 0.03
        else:
            confirm_open = level - 0.30
            confirm_close = level - 0.45
            confirm_low = level - 0.55
        candles.append(Candle(71 * 300_000, confirm_open, max(confirm_open, confirm_close) + 0.05, confirm_low, confirm_close, 125.0))
    else:
        level = min(item.low for item in prior)
        breakout_open = level + 0.10
        breakout_close = level - 0.45
        candles.append(Candle(70 * 300_000, breakout_open, breakout_open + 0.05, breakout_close - 0.05, breakout_close, 450.0))
        if hold:
            confirm_open = level - 0.06
            confirm_close = level - 0.22
            confirm_high = level + 0.03
        else:
            confirm_open = level + 0.30
            confirm_close = level + 0.45
            confirm_high = level + 0.55
        candles.append(Candle(71 * 300_000, confirm_open, confirm_high, min(confirm_open, confirm_close) - 0.05, confirm_close, 125.0))
    return candles


V3_SETTINGS = {
    **SETTINGS,
    "strategy_revision": 3,
    "volume_multiplier": 3.0,
    "minimum_signal_score": 0.85,
    "require_trend_confirmation": True,
    "require_breakout_confirmation": True,
    "require_volume_confirmation": True,
    "require_rsi_confirmation": False,
    "require_retest_confirmation": True,
    "breakout_buffer_atr": 0.08,
    "minimum_body_atr": 0.25,
    "maximum_breakout_body_atr": 2.5,
    "minimum_ema_separation_atr": 0.02,
    "minimum_ema_slope_atr": 0.005,
    "minimum_breakout_close_location": 0.60,
    "retest_tolerance_atr": 0.40,
    "retest_max_penetration_atr": 0.50,
    "retest_reclaim_atr": 0.01,
    "minimum_confirmation_body_atr": 0.02,
}


def test_v3_enters_only_after_long_breakout_retest_holds():
    signal = _signal(_v3_retest_candles(1), V3_SETTINGS)
    assert signal is not None
    assert signal.side == "Buy"
    assert any("retested and held" in reason for reason in signal.reasons)


def test_v3_enters_only_after_short_breakout_retest_holds():
    signal = _signal(_v3_retest_candles(-1), V3_SETTINGS)
    assert signal is not None
    assert signal.side == "Sell"
    assert any("retested and held" in reason for reason in signal.reasons)


def test_v3_rejects_failed_retest():
    assert _signal(_v3_retest_candles(1, hold=False), V3_SETTINGS) is None



def _v4_settings() -> dict:
    bot = TradingBot(
        user_id=1, name="default v4", strategy_type="pattern_scalper",
        category="linear", symbol="BTCUSDT", order_qty=0.001, settings={},
    )
    return _settings(bot)


def _v4_bull_flag_candles() -> list[Candle]:
    candles: list[Candle] = []
    price = 100.0
    # 188 one-minute candles make a clean bullish 5m context.
    for index in range(188):
        open_price = price
        close = open_price + 0.08
        candles.append(Candle(index * 60_000, open_price, close + 0.04, open_price - 0.04, close, 100.0))
        price = close
    # Strong impulse.
    for offset in range(6):
        index = 188 + offset
        open_price = price
        close = open_price + 0.35
        candles.append(Candle(index * 60_000, open_price, close + 0.05, open_price - 0.03, close, 180.0))
        price = close
    # Controlled pullback / flag.
    for offset in range(5):
        index = 194 + offset
        open_price = price
        close = open_price - 0.18
        candles.append(Candle(index * 60_000, open_price, open_price + 0.04, close - 0.04, close, 80.0))
        price = close
    breakout_level = max(item.high for item in candles[-5:])
    candles.append(Candle(199 * 60_000, price, breakout_level + 0.24, price - 0.03, breakout_level + 0.20, 300.0))
    return candles


def test_v4_ema_context_alone_does_not_create_an_entry():
    settings = _v4_settings()
    candles: list[Candle] = []
    price = 100.0
    for index in range(200):
        open_price = price
        close = open_price + 0.08
        candles.append(Candle(index * 60_000, open_price, close + 0.04, open_price - 0.04, close, 100.0))
        price = close
    assert _signal(candles, settings) is None


def test_v4_bull_flag_combines_pattern_with_5m_context():
    signal = _signal(_v4_bull_flag_candles(), _v4_settings())
    assert signal is not None
    assert signal.side == "Buy"
    assert signal.pattern == "bull_flag"
    assert signal.indicators["market_regime"] == "bullish_trend"
    assert signal.indicators["context_timeframe"] == "5"


def _v4_double_bottom_candles() -> list[Candle]:
    candles: list[Candle] = []
    for index in range(168):
        center = 100.0 + sin(index / 5.0) * 0.15
        candles.append(Candle(index * 60_000, center - 0.03, center + 0.18, center - 0.18, center + 0.03, 100.0))
    for offset in range(31):
        index = 168 + offset
        value = 100.0 + 0.45 * sin(offset * 3.14159 / 7.0)
        if offset in (5, 20):
            value = 99.0
        candles.append(Candle(index * 60_000, value, value + 0.18, value - 0.08, value + 0.02, 100.0))
    neckline = max(item.high for item in candles[-32:-1])
    candles.append(Candle(199 * 60_000, 100.1, neckline + 0.29, 100.06, neckline + 0.25, 220.0))
    return candles


def test_v4_double_bottom_can_trade_reversal_without_bullish_ema_entry_rule():
    signal = _signal(_v4_double_bottom_candles(), _v4_settings())
    assert signal is not None
    assert signal.side == "Buy"
    assert signal.pattern == "double_bottom"
    assert signal.indicators["market_regime"] in {"range", "transition", "bearish_trend"}


def _v4_liquidity_sweep_candles() -> list[Candle]:
    candles: list[Candle] = []
    for index in range(198):
        center = 100.0 + sin(index / 6.0) * 0.12
        candles.append(Candle(index * 60_000, center - 0.02, center + 0.16, center - 0.16, center + 0.02, 100.0))
    prior_low = min(item.low for item in candles[-20:])
    candles.append(Candle(198 * 60_000, prior_low + 0.08, prior_low + 0.12, prior_low - 0.15, prior_low + 0.06, 180.0))
    candles.append(Candle(199 * 60_000, prior_low + 0.07, prior_low + 0.32, prior_low + 0.05, prior_low + 0.28, 220.0))
    return candles


def test_v4_detects_confirmed_liquidity_sweep():
    signal = _signal(_v4_liquidity_sweep_candles(), _v4_settings())
    assert signal is not None
    assert signal.side == "Buy"
    assert signal.pattern == "liquidity_sweep_long"


def _v4_triangle_candles() -> list[Candle]:
    candles: list[Candle] = []
    for index in range(187):
        center = 100.0 + sin(index / 7.0) * 0.08
        candles.append(Candle(index * 60_000, center - 0.02, center + 0.15, center - 0.15, center + 0.02, 100.0))
    for offset in range(12):
        upper = 101.0 - offset * 0.05
        lower = 99.0 + offset * 0.05
        midpoint = (upper + lower) / 2.0
        open_price = midpoint - 0.03
        close = midpoint + (0.03 if offset % 2 == 0 else -0.01)
        candles.append(Candle((187 + offset) * 60_000, open_price, upper, lower, close, 90.0))
    candles.append(Candle(199 * 60_000, 100.45, 100.84, 100.42, 100.80, 250.0))
    return candles


def test_v4_detects_triangle_compression_breakout():
    signal = _signal(_v4_triangle_candles(), _v4_settings())
    assert signal is not None
    assert signal.side == "Buy"
    assert signal.pattern == "triangle_breakout"


def test_start_needs_short_selling_on_the_bots_market_type():
    import pytest

    from app.bot_engine.exchange import registry
    from app.bot_engine.exchange.bybit.capabilities import BYBIT_CAPABILITIES
    from app.bot_engine.exchange.capabilities import Capability, CapabilitySet, MarketType
    from app.bot_engine.strategies.pattern_scalper import PatternScalperStrategy

    no_shorts = CapabilitySet({MarketType.LINEAR: BYBIT_CAPABILITIES.for_market_type("linear") - {Capability.SHORT_SELLING}})

    class Venue:
        def capabilities(self):
            return no_shorts

    bot = TradingBot(exchange="stand-in", environment="demo", category="linear", symbol="BTCUSDT",
                     strategy_type="pattern_scalper", order_qty=0.01, grid_orders_count=1,
                     grid_step_percent=0, settings={})
    with registry.registered_venue("stand-in", "demo", lambda account: Venue()):
        with pytest.raises(ValueError, match="supports linear perpetuals only"):
            PatternScalperStrategy().validate_start(None, bot)
