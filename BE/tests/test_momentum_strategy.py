from math import sin

import pytest

from app.bot_engine.orders import InstrumentRules, OrderRequest, validate_order_request
from app.bot_engine.strategies.momentum import MomentumStrategy, Candle, _settings, _short_trading_supported, _signal
from app.models.trading_bot import TradingBot


def _signal_settings(**overrides):
    bot = TradingBot(
        user_id=1,
        name="momentum",
        strategy_type="momentum",
        category=overrides.pop("category", "linear"),
        symbol="BTCUSDT",
        order_qty=0.01,
        settings=overrides,
    )
    return _settings(bot)


def _trend_candles(direction: int, count: int = 90) -> list[Candle]:
    candles: list[Candle] = []
    price = 100.0 if direction > 0 else 120.0
    for index in range(count):
        drift = (0.07 * direction) + (sin(index / 4.0) * 0.03)
        open_price = price + (sin(index / 6.0) * 0.02)
        close = open_price + drift
        wick = 0.08 + abs(sin(index / 5.0)) * 0.04
        high = max(open_price, close) + wick
        low = min(open_price, close) - wick
        volume = 110.0 + (index % 7) * 4.0
        candles.append(Candle(index * 300_000, open_price, high, low, close, volume))
        price = close

    latest = candles[-1]
    impulse = 0.38
    if direction > 0:
        candles[-1] = Candle(
            latest.open_time,
            latest.close - 0.10,
            latest.close + impulse + 0.08,
            latest.close - 0.18,
            latest.close + impulse,
            320.0,
        )
    else:
        candles[-1] = Candle(
            latest.open_time,
            latest.close + 0.10,
            latest.close + 0.18,
            latest.close - impulse - 0.08,
            latest.close - impulse,
            320.0,
        )
    return candles


def test_momentum_signal_detects_long_setup():
    signal = _signal(
        _trend_candles(1),
        _signal_settings(volume_multiplier=1.2, minimum_signal_score=70, minimum_atr_percent=0.0),
    )
    assert signal.type == "long"
    assert signal.side == "Buy"
    assert signal.score >= 70
    assert signal.market_regime == "bullish"
    assert signal.indicators["atr"] > 0
    assert signal.indicators["longScore"] >= signal.indicators["shortScore"]


def test_momentum_signal_detects_short_setup():
    signal = _signal(
        _trend_candles(-1),
        _signal_settings(volume_multiplier=1.2, minimum_signal_score=70, minimum_atr_percent=0.0),
    )
    assert signal.type == "short"
    assert signal.side == "Sell"
    assert signal.score >= 70
    assert signal.market_regime == "bearish"
    assert signal.indicators["atr"] > 0
    assert signal.indicators["shortScore"] >= signal.indicators["longScore"]


def test_spot_momentum_bot_cannot_start_in_short_mode():
    bot = TradingBot(
        user_id=1,
        name="spot-short",
        strategy_type="momentum",
        category="spot",
        symbol="BTCUSDT",
        order_qty=0.01,
        settings={"position_side": "short"},
    )

    with pytest.raises(ValueError, match="Spot momentum bots cannot open short positions"):
        MomentumStrategy().validate_start(None, bot)


def test_short_trading_supported_respects_spot_category_hint():
    assert _short_trading_supported(None, {"position_side": "both"}, category_hint="linear") is True
    assert _short_trading_supported(None, {"position_side": "both"}, category_hint="spot") is False


def test_momentum_close_orders_must_be_reduce_only():
    rules = InstrumentRules(min_order_qty=0.001, qty_step=0.001, tick_size=0.1, min_notional_value=0)
    order = OrderRequest(
        side="Sell",
        order_type="Market",
        order_role="momentum_take_profit",
        order_link_id="momentum-close-1",
        qty=0.01,
        price=None,
        reduce_only=False,
    )

    assert validate_order_request(order, rules) == "Position close orders must be reduce-only"



def test_order_record_reduce_only_reads_the_stored_payload_including_legacy_rows():
    from app.models.trading_bot_order import TradingBotOrder

    assert TradingBotOrder(raw_response={"reduceOnly": True}).is_reduce_only is True
    assert TradingBotOrder(raw_response={"reduceOnly": False}).is_reduce_only is False
    assert TradingBotOrder(raw_response={}).is_reduce_only is False
    assert TradingBotOrder(raw_response=None).is_reduce_only is False
