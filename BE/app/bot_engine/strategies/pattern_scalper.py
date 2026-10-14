"""Explainable multi-pattern scalper.

Revision 4 separates market context from entry setup. Higher-timeframe EMA and
price structure classify trend/range/transition, but EMA can no longer open a
trade by itself. Entries require an explicit OHLCV pattern (breakout-retest,
flag, triangle/compression, double top/bottom or liquidity sweep) plus context
and confirmation. The strategy opens at most one position and manages SL/TP,
holding time, cooldown and daily loss.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from sqlalchemy import desc

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.port import PlacedOrder
from app.bot_engine.events import log_bot_event
from app.bot_engine.error_handling import ExchangeOperationError
from app.bot_engine.grid_runtime import (
    cancel_all_bot_orders,
    ensure_live_trading_allowed,
    get_order_link_prefix,
    sync_bot_orders,
)
from app.bot_engine.market_data import get_instrument_rules, get_last_price, get_ticker_snapshot
from app.bot_engine.orders import (
    ACTIVE_ORDER_STATUSES,
    OrderRequest,
    normalize_order_request,
    place_order,
    validate_order_request,
)
from app.bot_engine.positions import get_open_positions
from app.core.clock import utcnow
from app.bot_engine.exchange.capabilities import Capability
from app.bot_engine.strategies.base import COMMON_CAPABILITIES
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder


@dataclass(slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class Signal:
    side: str
    score: float
    price: float
    atr: float
    candle_time: int
    reasons: list[str]
    indicators: dict[str, Any]
    pattern: str = "breakout_retest"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _fmt(value: Any) -> str | None:
    if value is None:
        return None
    numeric = _float(value)
    return f"{numeric:.12f}".rstrip("0").rstrip(".") if numeric else "0"


def _ensure_accepted(placed: PlacedOrder, action: str) -> None:
    if placed.rejection is None:
        return
    message = placed.rejection.message or "Unknown exchange error"
    raise ExchangeOperationError(str(message), code=placed.rejection.raw_code, operation=action)


def _interval_seconds(interval: str) -> int:
    return {
        "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
        "60": 3600, "120": 7200, "240": 14400, "360": 21600,
        "720": 43200, "D": 86400, "W": 604800, "M": 2592000,
    }.get(str(interval), 300)


def _settings(bot: TradingBot) -> dict:
    # Revision 4 turns EMA into market context instead of an entry rule.
    # Explicit price-action patterns create candidates, then higher-timeframe
    # context and confirmations decide whether the candidate is tradable.
    # Existing bots are upgraded in-memory so runtime and fast backtests share
    # exactly the same signal contract.
    raw = dict(bot.settings or {})
    value = {
        "strategy_revision": 4,
        "timeframe": "1",
        "context_timeframe": "5",
        "lookback_candles": 200,
        "ema_fast_period": 20,
        "ema_slow_period": 50,
        "rsi_period": 14,
        "atr_period": 14,
        "breakout_lookback": 20,
        "volume_lookback": 20,
        "volume_multiplier": 3.0,
        "minimum_signal_score": 0.85,
        "require_trend_confirmation": True,
        "require_breakout_confirmation": True,
        "require_volume_confirmation": True,
        "require_rsi_confirmation": True,
        "require_retest_confirmation": True,
        "breakout_buffer_atr": 0.08,
        "minimum_body_atr": 0.25,
        "maximum_breakout_body_atr": 1.60,
        "minimum_ema_separation_atr": 0.08,
        "minimum_ema_slope_atr": 0.015,
        "minimum_breakout_close_location": 0.65,
        "retest_tolerance_atr": 0.25,
        "retest_max_penetration_atr": 0.35,
        "retest_reclaim_atr": 0.03,
        "minimum_confirmation_body_atr": 0.08,
        "context_ema_fast_period": 12,
        "context_ema_slow_period": 36,
        "context_structure_lookback": 12,
        "context_min_ema_separation_atr": 0.10,
        "context_min_ema_slope_atr": 0.02,
        "pattern_volume_multiplier": 1.5,
        "enable_breakout_retest": True,
        "enable_flag": True,
        "enable_triangle": True,
        "enable_double_top_bottom": True,
        "enable_liquidity_sweep": True,
        "flag_impulse_lookback": 6,
        "flag_pullback_lookback": 5,
        "flag_min_impulse_atr": 1.6,
        "flag_max_retrace": 0.62,
        "triangle_lookback": 12,
        "triangle_min_contraction": 0.22,
        "double_pattern_lookback": 32,
        "double_pattern_tolerance_atr": 0.45,
        "double_pattern_min_separation": 5,
        "liquidity_sweep_lookback": 20,
        "liquidity_sweep_penetration_atr": 0.08,
        "liquidity_sweep_reclaim_atr": 0.04,
        "rsi_long_min": 50.0,
        "rsi_long_max": 70.0,
        "rsi_short_min": 30.0,
        "rsi_short_max": 50.0,
        "stop_loss_atr": 1.2,
        "take_profit_atr": 1.8,
        "max_holding_minutes": 30,
        "cooldown_minutes": 15,
        "risk_per_trade_percent": 0.5,
        "max_daily_loss_percent": 2.0,
        "allow_short": True,
        "position_sizing": "risk_capped",
        "max_position_qty": float(bot.order_qty),
        "max_open_orders": 1,
        "max_notional_usdt": None,
        "allow_live_trading": False,
        "stop_bot_on_error": True,
        "error_max_retries": 5,
        "error_retry_base_seconds": 5,
        "error_retry_max_seconds": 300,
        "cancel_orders_on_stop": True,
        "run_interval_seconds": 5,
    }
    value.update(raw)

    revision = int(_float(raw.get("strategy_revision"), 1))
    if revision < 2:
        value["minimum_signal_score"] = max(_float(value.get("minimum_signal_score"), 0.0), 0.85)
        value["volume_multiplier"] = max(_float(value.get("volume_multiplier"), 0.0), 3.0)
        value["cooldown_minutes"] = max(_float(value.get("cooldown_minutes"), 0.0), 15.0)
        value["require_trend_confirmation"] = True
        value["require_breakout_confirmation"] = True
        value["require_volume_confirmation"] = True
        value["breakout_buffer_atr"] = max(_float(value.get("breakout_buffer_atr"), 0.0), 0.05)
        value["minimum_body_atr"] = max(_float(value.get("minimum_body_atr"), 0.0), 0.25)

    if revision < 3:
        value["strategy_revision"] = 3
        value["minimum_signal_score"] = max(_float(value.get("minimum_signal_score"), 0.0), 0.85)
        value["volume_multiplier"] = max(_float(value.get("volume_multiplier"), 0.0), 3.0)
        value["cooldown_minutes"] = max(_float(value.get("cooldown_minutes"), 0.0), 15.0)
        value["require_trend_confirmation"] = True
        value["require_breakout_confirmation"] = True
        value["require_volume_confirmation"] = True
        value["require_rsi_confirmation"] = True
        value["require_retest_confirmation"] = True
        value["breakout_buffer_atr"] = max(_float(value.get("breakout_buffer_atr"), 0.0), 0.08)
        value["minimum_body_atr"] = max(_float(value.get("minimum_body_atr"), 0.0), 0.25)
        value["maximum_breakout_body_atr"] = min(max(_float(value.get("maximum_breakout_body_atr"), 1.60), 0.5), 3.0)
        value["minimum_ema_separation_atr"] = max(_float(value.get("minimum_ema_separation_atr"), 0.0), 0.08)
        value["minimum_ema_slope_atr"] = max(_float(value.get("minimum_ema_slope_atr"), 0.0), 0.015)
        value["minimum_breakout_close_location"] = max(_float(value.get("minimum_breakout_close_location"), 0.0), 0.65)
        value["retest_tolerance_atr"] = min(max(_float(value.get("retest_tolerance_atr"), 0.25), 0.0), 1.0)
        value["retest_max_penetration_atr"] = min(max(_float(value.get("retest_max_penetration_atr"), 0.35), 0.0), 1.5)
        value["retest_reclaim_atr"] = max(_float(value.get("retest_reclaim_atr"), 0.03), 0.03)
        value["minimum_confirmation_body_atr"] = max(_float(value.get("minimum_confirmation_body_atr"), 0.08), 0.08)
        # Avoid buying an already overextended breakout or shorting an exhausted dump.
        value["rsi_long_max"] = min(_float(value.get("rsi_long_max"), 70.0), 70.0)
        value["rsi_short_min"] = max(_float(value.get("rsi_short_min"), 30.0), 30.0)

    if revision < 4:
        value["strategy_revision"] = 4
        value.setdefault("context_timeframe", "5")
        value["context_ema_fast_period"] = max(int(value.get("context_ema_fast_period") or 12), 2)
        value["context_ema_slow_period"] = max(int(value.get("context_ema_slow_period") or 36), 3)
        value["context_structure_lookback"] = max(int(value.get("context_structure_lookback") or 12), 6)
        value["context_min_ema_separation_atr"] = max(_float(value.get("context_min_ema_separation_atr"), 0.10), 0.0)
        value["context_min_ema_slope_atr"] = max(_float(value.get("context_min_ema_slope_atr"), 0.02), 0.0)
        value["pattern_volume_multiplier"] = max(_float(value.get("pattern_volume_multiplier"), 1.5), 1.0)
        for key in ("enable_breakout_retest", "enable_flag", "enable_triangle", "enable_double_top_bottom", "enable_liquidity_sweep"):
            value[key] = bool(value.get(key, True))
        value["flag_impulse_lookback"] = max(int(value.get("flag_impulse_lookback") or 6), 3)
        value["flag_pullback_lookback"] = max(int(value.get("flag_pullback_lookback") or 5), 3)
        value["flag_min_impulse_atr"] = max(_float(value.get("flag_min_impulse_atr"), 1.6), 0.5)
        value["flag_max_retrace"] = min(max(_float(value.get("flag_max_retrace"), 0.62), 0.2), 0.9)
        value["triangle_lookback"] = max(int(value.get("triangle_lookback") or 12), 6)
        value["triangle_min_contraction"] = min(max(_float(value.get("triangle_min_contraction"), 0.22), 0.05), 0.75)
        value["double_pattern_lookback"] = max(int(value.get("double_pattern_lookback") or 32), 12)
        value["double_pattern_tolerance_atr"] = max(_float(value.get("double_pattern_tolerance_atr"), 0.45), 0.1)
        value["double_pattern_min_separation"] = max(int(value.get("double_pattern_min_separation") or 5), 3)
        value["liquidity_sweep_lookback"] = max(int(value.get("liquidity_sweep_lookback") or 20), 8)
        value["liquidity_sweep_penetration_atr"] = max(_float(value.get("liquidity_sweep_penetration_atr"), 0.08), 0.0)
        value["liquidity_sweep_reclaim_atr"] = max(_float(value.get("liquidity_sweep_reclaim_atr"), 0.04), 0.0)

    value["timeframe"] = str(value.get("timeframe") or "1")
    value["context_timeframe"] = str(value.get("context_timeframe") or "5")
    for key, minimum in (
        ("lookback_candles", 60), ("ema_fast_period", 2), ("ema_slow_period", 3),
        ("rsi_period", 2), ("atr_period", 2), ("breakout_lookback", 2),
        ("volume_lookback", 2), ("max_open_orders", 1),
        ("context_ema_fast_period", 2), ("context_ema_slow_period", 3),
        ("context_structure_lookback", 6), ("flag_impulse_lookback", 3),
        ("flag_pullback_lookback", 3), ("triangle_lookback", 6),
        ("double_pattern_lookback", 12), ("double_pattern_min_separation", 3),
        ("liquidity_sweep_lookback", 8),
    ):
        value[key] = max(int(value.get(key) or minimum), minimum)
    value["max_position_qty"] = max(_float(value.get("max_position_qty"), bot.order_qty), 0)
    return value


def _state(bot: TradingBot) -> dict:
    return deepcopy((bot.settings or {}).get("pattern_scalper_state") or {})


def _save_state(db, bot: TradingBot, state: dict) -> None:
    # JSON columns do not track nested in-place changes. Deep copies ensure each
    # state transition is detected and persisted instead of reusing stale data.
    settings = deepcopy(dict(bot.settings or {}))
    settings["pattern_scalper_state"] = deepcopy(state)
    bot.settings = settings
    db.add(bot)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(value * alpha + result[-1] * (1 - alpha))
    return result


def _rsi(values: list[float], period: int) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-period - 1:-1], values[-period:]):
        change = current - previous
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    gain = sum(gains) / period
    loss = sum(losses) / period
    if loss <= 1e-15:
        return 100.0 if gain > 0 else 50.0
    return 100 - (100 / (1 + gain / loss))


def _atr(candles: list[Candle], period: int) -> float:
    if len(candles) <= period:
        return 0.0
    ranges: list[float] = []
    for previous, current in zip(candles[-period - 1:-1], candles[-period:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _closed_candles(adapter, bot: TradingBot, settings: dict) -> list[Candle]:
    interval = settings["timeframe"]
    end_ms = int(utcnow().timestamp() * 1000) - _interval_seconds(interval) * 1000
    return [
        Candle(item.open_time, item.open, item.high, item.low, item.close, item.volume)
        for item in adapter.candles(
            bot.category,
            bot.symbol,
            interval=interval,
            end_ms=end_ms,
            limit=min(settings["lookback_candles"], 1000),
        )
    ]


def _signal_v2(candles: list[Candle], settings: dict) -> Signal | None:
    required = max(
        settings["ema_slow_period"] + 3,
        settings["rsi_period"] + 2,
        settings["atr_period"] + 2,
        settings["breakout_lookback"] + 2,
        settings["volume_lookback"] + 2,
    )
    if len(candles) < required:
        return None
    closes = [item.close for item in candles]
    fast_values = _ema(closes, settings["ema_fast_period"])
    slow_values = _ema(closes, settings["ema_slow_period"])
    latest = candles[-1]
    atr = _atr(candles, settings["atr_period"])
    if atr <= 0:
        return None
    fast, fast_previous, slow = fast_values[-1], fast_values[-2], slow_values[-1]
    rsi = _rsi(closes, settings["rsi_period"])
    breakout = candles[-settings["breakout_lookback"] - 1:-1]
    previous_high = max(item.high for item in breakout)
    previous_low = min(item.low for item in breakout)
    volume_window = candles[-settings["volume_lookback"] - 1:-1]
    average_volume = sum(item.volume for item in volume_window) / len(volume_window)
    volume_ratio = latest.volume / average_volume if average_volume > 0 else 1.0
    body_atr = abs(latest.close - latest.open) / atr
    breakout_buffer = atr * max(_float(settings.get("breakout_buffer_atr"), 0.05), 0.0)

    long_trend = fast > slow and fast > fast_previous
    short_trend = fast < slow and fast < fast_previous
    long_breakout = latest.close > previous_high + breakout_buffer
    short_breakout = latest.close < previous_low - breakout_buffer
    volume_confirmed = volume_ratio >= _float(settings["volume_multiplier"], 3.0)
    long_rsi = _float(settings["rsi_long_min"], 50) <= rsi <= _float(settings["rsi_long_max"], 72)
    short_rsi = _float(settings["rsi_short_min"], 28) <= rsi <= _float(settings["rsi_short_max"], 50)
    minimum_body_atr = max(_float(settings.get("minimum_body_atr"), 0.25), 0.0)
    long_body = latest.close > latest.open and body_atr >= minimum_body_atr
    short_body = latest.close < latest.open and body_atr >= minimum_body_atr

    long_score, short_score = 0.0, 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []
    if long_trend:
        long_score += 0.25; long_reasons.append("EMA trend is bullish")
    if long_breakout:
        long_score += 0.30; long_reasons.append("Closed above the recent high")
    if volume_confirmed:
        long_score += 0.20; long_reasons.append(f"Volume is {volume_ratio:.2f}× average")
    if long_rsi:
        long_score += 0.15; long_reasons.append(f"RSI confirms momentum ({rsi:.1f})")
    if long_body:
        long_score += 0.10; long_reasons.append("Bullish candle has meaningful range")

    if short_trend:
        short_score += 0.25; short_reasons.append("EMA trend is bearish")
    if short_breakout:
        short_score += 0.30; short_reasons.append("Closed below the recent low")
    if volume_confirmed:
        short_score += 0.20; short_reasons.append(f"Volume is {volume_ratio:.2f}× average")
    if short_rsi:
        short_score += 0.15; short_reasons.append(f"RSI confirms momentum ({rsi:.1f})")
    if short_body:
        short_score += 0.10; short_reasons.append("Bearish candle has meaningful range")

    require_trend = bool(settings.get("require_trend_confirmation", True))
    require_breakout = bool(settings.get("require_breakout_confirmation", True))
    require_volume = bool(settings.get("require_volume_confirmation", True))
    long_qualified = (not require_trend or long_trend) and (not require_breakout or long_breakout) and (not require_volume or volume_confirmed)
    short_qualified = (not require_trend or short_trend) and (not require_breakout or short_breakout) and (not require_volume or volume_confirmed)

    indicators = {
        "ema_fast": fast, "ema_slow": slow, "rsi": rsi, "atr": atr,
        "volume_ratio": volume_ratio, "body_atr": body_atr,
        "breakout_buffer": breakout_buffer,
        "previous_high": previous_high, "previous_low": previous_low,
    }
    minimum = _float(settings["minimum_signal_score"], 0.85)
    if long_qualified and long_score >= minimum and long_score >= short_score:
        return Signal("Buy", long_score, latest.close, atr, latest.open_time, long_reasons, indicators)
    if bool(settings.get("allow_short", True)) and short_qualified and short_score >= minimum:
        return Signal("Sell", short_score, latest.close, atr, latest.open_time, short_reasons, indicators)
    return None


def _signal_v3(candles: list[Candle], settings: dict) -> Signal | None:
    """Breakout + one-candle retest confirmation entry.

    The penultimate closed candle must create a high-quality breakout.  The
    latest closed candle must then retest that broken level, avoid a meaningful
    failure through it, reclaim it and close back in the breakout direction.
    This deliberately trades later than revision 2 in exchange for rejecting
    many first-touch/fake breakouts.
    """
    required = max(
        settings["ema_slow_period"] + 4,
        settings["rsi_period"] + 3,
        settings["atr_period"] + 3,
        settings["breakout_lookback"] + 3,
        settings["volume_lookback"] + 3,
    )
    if len(candles) < required:
        return None

    breakout_candle = candles[-2]
    confirmation = candles[-1]
    breakout_history = candles[:-1]
    breakout_closes = [item.close for item in breakout_history]
    current_closes = [item.close for item in candles]

    fast_values = _ema(breakout_closes, settings["ema_fast_period"])
    slow_values = _ema(breakout_closes, settings["ema_slow_period"])
    if len(fast_values) < 3 or len(slow_values) < 2:
        return None
    fast = fast_values[-1]
    fast_previous = fast_values[-2]
    fast_three_back = fast_values[-3]
    slow = slow_values[-1]
    breakout_atr = _atr(breakout_history, settings["atr_period"])
    current_atr = _atr(candles, settings["atr_period"])
    if breakout_atr <= 0 or current_atr <= 0:
        return None

    breakout_window = breakout_history[-settings["breakout_lookback"] - 1:-1]
    previous_high = max(item.high for item in breakout_window)
    previous_low = min(item.low for item in breakout_window)
    volume_window = breakout_history[-settings["volume_lookback"] - 1:-1]
    average_volume = sum(item.volume for item in volume_window) / len(volume_window)
    volume_ratio = breakout_candle.volume / average_volume if average_volume > 0 else 1.0

    body_atr = abs(breakout_candle.close - breakout_candle.open) / breakout_atr
    candle_range = max(breakout_candle.high - breakout_candle.low, 1e-12)
    close_location = (breakout_candle.close - breakout_candle.low) / candle_range
    breakout_buffer = breakout_atr * max(_float(settings.get("breakout_buffer_atr"), 0.08), 0.0)
    ema_separation_atr = abs(fast - slow) / breakout_atr
    ema_slope_atr = abs(fast - fast_three_back) / (2.0 * breakout_atr)

    long_trend = fast > slow and fast > fast_previous
    short_trend = fast < slow and fast < fast_previous
    long_breakout = breakout_candle.close > previous_high + breakout_buffer
    short_breakout = breakout_candle.close < previous_low - breakout_buffer
    volume_confirmed = volume_ratio >= _float(settings.get("volume_multiplier"), 3.0)

    minimum_body = max(_float(settings.get("minimum_body_atr"), 0.25), 0.0)
    maximum_body = max(_float(settings.get("maximum_breakout_body_atr"), 1.60), minimum_body)
    body_quality = minimum_body <= body_atr <= maximum_body
    minimum_close_location = min(max(_float(settings.get("minimum_breakout_close_location"), 0.65), 0.5), 0.95)
    long_close_quality = close_location >= minimum_close_location
    short_close_quality = close_location <= 1.0 - minimum_close_location
    trend_strength = (
        ema_separation_atr >= _float(settings.get("minimum_ema_separation_atr"), 0.08)
        and ema_slope_atr >= _float(settings.get("minimum_ema_slope_atr"), 0.015)
    )

    rsi = _rsi(current_closes, settings["rsi_period"])
    long_rsi = _float(settings.get("rsi_long_min"), 50) <= rsi <= _float(settings.get("rsi_long_max"), 70)
    short_rsi = _float(settings.get("rsi_short_min"), 30) <= rsi <= _float(settings.get("rsi_short_max"), 50)

    tolerance = breakout_atr * max(_float(settings.get("retest_tolerance_atr"), 0.25), 0.0)
    max_penetration = breakout_atr * max(_float(settings.get("retest_max_penetration_atr"), 0.35), 0.0)
    reclaim = breakout_atr * max(_float(settings.get("retest_reclaim_atr"), 0.03), 0.0)
    confirmation_body_atr = abs(confirmation.close - confirmation.open) / current_atr
    minimum_confirmation_body = max(_float(settings.get("minimum_confirmation_body_atr"), 0.08), 0.0)

    long_retest = (
        confirmation.low <= previous_high + tolerance
        and confirmation.low >= previous_high - max_penetration
        and confirmation.close >= previous_high + reclaim
        and confirmation.close > confirmation.open
        and confirmation_body_atr >= minimum_confirmation_body
    )
    short_retest = (
        confirmation.high >= previous_low - tolerance
        and confirmation.high <= previous_low + max_penetration
        and confirmation.close <= previous_low - reclaim
        and confirmation.close < confirmation.open
        and confirmation_body_atr >= minimum_confirmation_body
    )

    require_trend = bool(settings.get("require_trend_confirmation", True))
    require_breakout = bool(settings.get("require_breakout_confirmation", True))
    require_volume = bool(settings.get("require_volume_confirmation", True))
    require_rsi = bool(settings.get("require_rsi_confirmation", True))
    require_retest = bool(settings.get("require_retest_confirmation", True))

    long_qualified = (
        (not require_trend or (long_trend and trend_strength))
        and (not require_breakout or (long_breakout and body_quality and long_close_quality))
        and (not require_volume or volume_confirmed)
        and (not require_rsi or long_rsi)
        and (not require_retest or long_retest)
    )
    short_qualified = (
        (not require_trend or (short_trend and trend_strength))
        and (not require_breakout or (short_breakout and body_quality and short_close_quality))
        and (not require_volume or volume_confirmed)
        and (not require_rsi or short_rsi)
        and (not require_retest or short_retest)
    )

    # The score remains explainable, but revision 3 quality gates are mandatory
    # by default; score now describes how many independent confirmations agree.
    long_score = 0.0
    short_score = 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []
    if long_trend and trend_strength:
        long_score += 0.20
        long_reasons.append("EMA trend is bullish and sufficiently strong")
    if long_breakout and body_quality and long_close_quality:
        long_score += 0.20
        long_reasons.append("Breakout candle closed cleanly above resistance")
    if volume_confirmed:
        long_score += 0.15
        long_reasons.append(f"Breakout volume is {volume_ratio:.2f}× average")
    if long_rsi:
        long_score += 0.15
        long_reasons.append(f"RSI confirms momentum without overextension ({rsi:.1f})")
    if long_retest:
        long_score += 0.30
        long_reasons.append("Broken resistance was retested and held as support")

    if short_trend and trend_strength:
        short_score += 0.20
        short_reasons.append("EMA trend is bearish and sufficiently strong")
    if short_breakout and body_quality and short_close_quality:
        short_score += 0.20
        short_reasons.append("Breakout candle closed cleanly below support")
    if volume_confirmed:
        short_score += 0.15
        short_reasons.append(f"Breakout volume is {volume_ratio:.2f}× average")
    if short_rsi:
        short_score += 0.15
        short_reasons.append(f"RSI confirms momentum without overextension ({rsi:.1f})")
    if short_retest:
        short_score += 0.30
        short_reasons.append("Broken support was retested and held as resistance")

    indicators = {
        "ema_fast": fast,
        "ema_slow": slow,
        "ema_separation_atr": ema_separation_atr,
        "ema_slope_atr": ema_slope_atr,
        "rsi": rsi,
        "atr": current_atr,
        "breakout_atr": breakout_atr,
        "volume_ratio": volume_ratio,
        "body_atr": body_atr,
        "breakout_close_location": close_location,
        "breakout_buffer": breakout_buffer,
        "breakout_level_long": previous_high,
        "breakout_level_short": previous_low,
        "confirmation_body_atr": confirmation_body_atr,
        "retest_tolerance": tolerance,
        "retest_max_penetration": max_penetration,
        "retest_reclaim": reclaim,
        "breakout_candle_time": float(breakout_candle.open_time),
    }
    minimum = _float(settings.get("minimum_signal_score"), 0.85)
    if long_qualified and long_score >= minimum and long_score >= short_score:
        return Signal("Buy", long_score, confirmation.close, current_atr, confirmation.open_time, long_reasons, indicators)
    if bool(settings.get("allow_short", True)) and short_qualified and short_score >= minimum:
        return Signal("Sell", short_score, confirmation.close, current_atr, confirmation.open_time, short_reasons, indicators)
    return None



def _infer_candle_interval_seconds(candles: list[Candle]) -> int:
    differences = [
        int((current.open_time - previous.open_time) / 1000)
        for previous, current in zip(candles[-40:-1], candles[-39:])
        if current.open_time > previous.open_time
    ]
    return min(differences) if differences else 60


def _aggregate_candles(candles: list[Candle], target_seconds: int) -> list[Candle]:
    """Aggregate fully closed entry candles into a higher-timeframe context.

    A 1m historical/live feed can therefore use 5m context without fetching a
    second source. Partial higher-timeframe buckets are deliberately discarded
    so the context never looks into an unfinished candle.
    """
    if not candles:
        return []
    base_seconds = _infer_candle_interval_seconds(candles)
    if target_seconds <= base_seconds or target_seconds % base_seconds:
        return list(candles)
    expected = max(target_seconds // base_seconds, 1)
    bucket_ms = target_seconds * 1000
    groups: dict[int, list[Candle]] = {}
    for candle in candles:
        bucket = (candle.open_time // bucket_ms) * bucket_ms
        groups.setdefault(bucket, []).append(candle)
    result: list[Candle] = []
    for bucket in sorted(groups):
        rows = sorted(groups[bucket], key=lambda item: item.open_time)
        if len(rows) < expected:
            continue
        rows = rows[:expected]
        result.append(Candle(
            open_time=bucket,
            open=rows[0].open,
            high=max(item.high for item in rows),
            low=min(item.low for item in rows),
            close=rows[-1].close,
            volume=sum(item.volume for item in rows),
        ))
    return result


def _linear_slope(values: list[float]) -> float:
    count = len(values)
    if count < 2:
        return 0.0
    mean_x = (count - 1) / 2.0
    mean_y = sum(values) / count
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    if denominator <= 1e-15:
        return 0.0
    return sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values)) / denominator


def _market_context(candles: list[Candle], settings: dict) -> dict[str, Any]:
    context_timeframe = str(settings.get("context_timeframe") or "5")
    context = _aggregate_candles(candles, _interval_seconds(context_timeframe))
    fast_period = int(settings.get("context_ema_fast_period") or 12)
    slow_period = int(settings.get("context_ema_slow_period") or 36)
    structure_lookback = int(settings.get("context_structure_lookback") or 12)
    required = max(slow_period + 3, structure_lookback + 2, int(settings.get("atr_period") or 14) + 2)
    if len(context) < required:
        return {
            "regime": "unknown",
            "timeframe": context_timeframe,
            "candles": len(context),
            "ema_fast": 0.0,
            "ema_slow": 0.0,
            "ema_separation_atr": 0.0,
            "ema_slope_atr": 0.0,
            "bullish_structure": False,
            "bearish_structure": False,
        }

    closes = [item.close for item in context]
    fast_values = _ema(closes, fast_period)
    slow_values = _ema(closes, slow_period)
    atr = _atr(context, int(settings.get("atr_period") or 14))
    if atr <= 0:
        return {"regime": "unknown", "timeframe": context_timeframe, "candles": len(context)}

    fast = fast_values[-1]
    slow = slow_values[-1]
    fast_three_back = fast_values[-3]
    signed_slope = (fast - fast_three_back) / (2.0 * atr)
    separation = abs(fast - slow) / atr

    recent = context[-structure_lookback:]
    midpoint = max(len(recent) // 2, 1)
    first = recent[:midpoint]
    second = recent[midpoint:]
    bullish_structure = bool(second) and (
        max(item.high for item in second) > max(item.high for item in first) + 0.03 * atr
        and min(item.low for item in second) > min(item.low for item in first) - 0.08 * atr
    )
    bearish_structure = bool(second) and (
        min(item.low for item in second) < min(item.low for item in first) - 0.03 * atr
        and max(item.high for item in second) < max(item.high for item in first) + 0.08 * atr
    )

    min_sep = _float(settings.get("context_min_ema_separation_atr"), 0.10)
    min_slope = _float(settings.get("context_min_ema_slope_atr"), 0.02)
    bullish_ema = fast > slow and signed_slope >= min_slope and separation >= min_sep
    bearish_ema = fast < slow and signed_slope <= -min_slope and separation >= min_sep
    if bullish_ema and bullish_structure:
        regime = "bullish_trend"
    elif bearish_ema and bearish_structure:
        regime = "bearish_trend"
    elif separation < min_sep * 0.8 and abs(signed_slope) < min_slope * 0.8:
        regime = "range"
    else:
        regime = "transition"

    return {
        "regime": regime,
        "timeframe": context_timeframe,
        "candles": len(context),
        "ema_fast": fast,
        "ema_slow": slow,
        "ema_separation_atr": separation,
        "ema_slope_atr": signed_slope,
        "atr": atr,
        "bullish_structure": bullish_structure,
        "bearish_structure": bearish_structure,
    }


def _pattern_volume_ratio(candles: list[Candle], settings: dict, target_index: int = -1) -> float:
    lookback = max(int(settings.get("volume_lookback") or 20), 2)
    index = target_index if target_index >= 0 else len(candles) + target_index
    start = max(index - lookback, 0)
    prior = candles[start:index]
    if not prior:
        return 1.0
    average = sum(item.volume for item in prior) / len(prior)
    return candles[index].volume / average if average > 0 else 1.0


def _flag_candidate(candles: list[Candle], settings: dict, atr: float) -> dict[str, Any] | None:
    impulse_n = int(settings.get("flag_impulse_lookback") or 6)
    pullback_n = int(settings.get("flag_pullback_lookback") or 5)
    if len(candles) < impulse_n + pullback_n + 2 or atr <= 0:
        return None
    trigger = candles[-1]
    pullback = candles[-pullback_n - 1:-1]
    impulse = candles[-pullback_n - impulse_n - 1:-pullback_n - 1]
    min_impulse = _float(settings.get("flag_min_impulse_atr"), 1.6) * atr
    max_retrace = _float(settings.get("flag_max_retrace"), 0.62)
    buffer = 0.03 * atr

    long_move = impulse[-1].close - impulse[0].open
    if long_move >= min_impulse:
        peak = max(item.high for item in impulse)
        pullback_low = min(item.low for item in pullback)
        retrace = max(peak - pullback_low, 0.0) / max(long_move, 1e-12)
        orderly = pullback[-1].close <= pullback[0].close + 0.15 * atr
        breakout_level = max(item.high for item in pullback)
        if 0.08 <= retrace <= max_retrace and orderly and trigger.close > breakout_level + buffer and trigger.close > trigger.open:
            quality = min(0.58, 0.50 + max(0.0, max_retrace - retrace) * 0.12)
            return {
                "side": "Buy", "pattern": "bull_flag", "quality": quality,
                "reasons": ["Bull flag: impulse, controlled pullback and continuation breakout"],
                "indicators": {"flag_retrace": retrace, "flag_breakout_level": breakout_level, "flag_impulse_atr": long_move / atr},
            }

    short_move = impulse[0].open - impulse[-1].close
    if short_move >= min_impulse:
        trough = min(item.low for item in impulse)
        pullback_high = max(item.high for item in pullback)
        retrace = max(pullback_high - trough, 0.0) / max(short_move, 1e-12)
        orderly = pullback[-1].close >= pullback[0].close - 0.15 * atr
        breakout_level = min(item.low for item in pullback)
        if 0.08 <= retrace <= max_retrace and orderly and trigger.close < breakout_level - buffer and trigger.close < trigger.open:
            quality = min(0.58, 0.50 + max(0.0, max_retrace - retrace) * 0.12)
            return {
                "side": "Sell", "pattern": "bear_flag", "quality": quality,
                "reasons": ["Bear flag: impulse, controlled pullback and continuation breakdown"],
                "indicators": {"flag_retrace": retrace, "flag_breakout_level": breakout_level, "flag_impulse_atr": short_move / atr},
            }
    return None


def _triangle_candidate(candles: list[Candle], settings: dict, atr: float) -> dict[str, Any] | None:
    lookback = int(settings.get("triangle_lookback") or 12)
    if len(candles) < lookback + 2 or atr <= 0:
        return None
    window = candles[-lookback - 1:-1]
    trigger = candles[-1]
    highs = [item.high for item in window]
    lows = [item.low for item in window]
    high_slope = _linear_slope(highs)
    low_slope = _linear_slope(lows)
    mean_high = sum(highs) / lookback
    mean_low = sum(lows) / lookback
    mean_x = (lookback - 1) / 2.0
    high_intercept = mean_high - high_slope * mean_x
    low_intercept = mean_low - low_slope * mean_x
    upper_start = high_intercept
    lower_start = low_intercept
    upper_end = high_intercept + high_slope * (lookback - 1)
    lower_end = low_intercept + low_slope * (lookback - 1)
    start_width = max(upper_start - lower_start, 1e-12)
    end_width = upper_end - lower_end
    if end_width <= 0:
        return None
    contraction = (start_width - end_width) / start_width
    min_contraction = _float(settings.get("triangle_min_contraction"), 0.22)
    # Convergence permits one side to be almost flat (ascending/descending triangle).
    slope_tolerance = 0.04 * atr
    converging = high_slope <= slope_tolerance and low_slope >= -slope_tolerance and high_slope < low_slope
    if not converging or contraction < min_contraction:
        return None
    projected_upper = high_intercept + high_slope * lookback
    projected_lower = low_intercept + low_slope * lookback
    buffer = 0.04 * atr
    quality = min(0.58, 0.49 + min(max(contraction, 0.0), 0.6) * 0.15)
    if trigger.close > projected_upper + buffer and trigger.close > trigger.open:
        return {
            "side": "Buy", "pattern": "triangle_breakout", "quality": quality,
            "reasons": ["Triangle/compression resolved upward"],
            "indicators": {"triangle_contraction": contraction, "triangle_upper": projected_upper, "triangle_lower": projected_lower},
        }
    if trigger.close < projected_lower - buffer and trigger.close < trigger.open:
        return {
            "side": "Sell", "pattern": "triangle_breakdown", "quality": quality,
            "reasons": ["Triangle/compression resolved downward"],
            "indicators": {"triangle_contraction": contraction, "triangle_upper": projected_upper, "triangle_lower": projected_lower},
        }
    return None


def _local_pivots(window: list[Candle], kind: str, radius: int = 2) -> list[int]:
    result: list[int] = []
    for index in range(radius, len(window) - radius):
        nearby = window[index - radius:index + radius + 1]
        if kind == "low" and window[index].low <= min(item.low for item in nearby):
            result.append(index)
        elif kind == "high" and window[index].high >= max(item.high for item in nearby):
            result.append(index)
    return result


def _find_similar_pivot_pair(window: list[Candle], pivots: list[int], kind: str, tolerance: float, minimum_separation: int) -> tuple[int, int] | None:
    for right_pos in range(len(pivots) - 1, 0, -1):
        right = pivots[right_pos]
        for left_pos in range(right_pos - 1, -1, -1):
            left = pivots[left_pos]
            if right - left < minimum_separation:
                continue
            left_price = window[left].low if kind == "low" else window[left].high
            right_price = window[right].low if kind == "low" else window[right].high
            if abs(left_price - right_price) <= tolerance:
                return left, right
    return None


def _double_candidate(candles: list[Candle], settings: dict, atr: float) -> dict[str, Any] | None:
    lookback = int(settings.get("double_pattern_lookback") or 32)
    if len(candles) < lookback + 2 or atr <= 0:
        return None
    history = candles[-lookback - 1:-1]
    trigger = candles[-1]
    tolerance = _float(settings.get("double_pattern_tolerance_atr"), 0.45) * atr
    minimum_separation = int(settings.get("double_pattern_min_separation") or 5)
    buffer = 0.04 * atr

    lows = _local_pivots(history, "low")
    pair = _find_similar_pivot_pair(history, lows, "low", tolerance, minimum_separation)
    if pair:
        left, right = pair
        between = history[left:right + 1]
        neckline = max(item.high for item in between)
        if trigger.close > neckline + buffer and trigger.close > trigger.open:
            difference = abs(history[left].low - history[right].low) / atr
            quality = min(0.60, 0.52 + max(0.0, 0.45 - difference) * 0.12)
            return {
                "side": "Buy", "pattern": "double_bottom", "quality": quality,
                "reasons": ["Double bottom confirmed by neckline breakout"],
                "indicators": {"double_level": (history[left].low + history[right].low) / 2.0, "neckline": neckline, "pivot_separation": right - left},
            }

    highs = _local_pivots(history, "high")
    pair = _find_similar_pivot_pair(history, highs, "high", tolerance, minimum_separation)
    if pair:
        left, right = pair
        between = history[left:right + 1]
        neckline = min(item.low for item in between)
        if trigger.close < neckline - buffer and trigger.close < trigger.open:
            difference = abs(history[left].high - history[right].high) / atr
            quality = min(0.60, 0.52 + max(0.0, 0.45 - difference) * 0.12)
            return {
                "side": "Sell", "pattern": "double_top", "quality": quality,
                "reasons": ["Double top confirmed by neckline breakdown"],
                "indicators": {"double_level": (history[left].high + history[right].high) / 2.0, "neckline": neckline, "pivot_separation": right - left},
            }
    return None


def _liquidity_sweep_candidate(candles: list[Candle], settings: dict, atr: float) -> dict[str, Any] | None:
    lookback = int(settings.get("liquidity_sweep_lookback") or 20)
    if len(candles) < lookback + 3 or atr <= 0:
        return None
    sweep = candles[-2]
    confirmation = candles[-1]
    prior = candles[-lookback - 2:-2]
    prior_low = min(item.low for item in prior)
    prior_high = max(item.high for item in prior)
    penetration = _float(settings.get("liquidity_sweep_penetration_atr"), 0.08) * atr
    reclaim = _float(settings.get("liquidity_sweep_reclaim_atr"), 0.04) * atr
    lower_wick = min(sweep.open, sweep.close) - sweep.low
    upper_wick = sweep.high - max(sweep.open, sweep.close)

    if (
        sweep.low < prior_low - penetration
        and sweep.close > prior_low + reclaim
        and lower_wick >= 0.18 * atr
        and confirmation.close > sweep.close
        and confirmation.close > confirmation.open
        and confirmation.low > sweep.low
    ):
        return {
            "side": "Buy", "pattern": "liquidity_sweep_long", "quality": 0.56,
            "reasons": ["Liquidity sweep below support was reclaimed and confirmed"],
            "indicators": {"swept_level": prior_low, "sweep_wick_atr": lower_wick / atr},
        }
    if (
        sweep.high > prior_high + penetration
        and sweep.close < prior_high - reclaim
        and upper_wick >= 0.18 * atr
        and confirmation.close < sweep.close
        and confirmation.close < confirmation.open
        and confirmation.high < sweep.high
    ):
        return {
            "side": "Sell", "pattern": "liquidity_sweep_short", "quality": 0.56,
            "reasons": ["Liquidity sweep above resistance was rejected and confirmed"],
            "indicators": {"swept_level": prior_high, "sweep_wick_atr": upper_wick / atr},
        }
    return None


def _context_score(candidate: dict[str, Any], context: dict[str, Any]) -> tuple[float, str] | None:
    side = candidate["side"]
    pattern = candidate["pattern"]
    regime = context.get("regime") or "unknown"
    context_label = str(context.get("timeframe") or "higher-timeframe")
    continuation = pattern in {"breakout_retest", "bull_flag", "bear_flag"}
    reversal = pattern in {"double_bottom", "double_top"}
    sweep = pattern.startswith("liquidity_sweep")

    if continuation:
        expected = "bullish_trend" if side == "Buy" else "bearish_trend"
        if regime != expected:
            return None
        return 0.20, f"{context_label} market context confirms the {('bullish' if side == 'Buy' else 'bearish')} continuation"

    if pattern.startswith("triangle"):
        expected = "bullish_trend" if side == "Buy" else "bearish_trend"
        opposite = "bearish_trend" if side == "Buy" else "bullish_trend"
        if regime == opposite:
            return None
        if regime == expected:
            return 0.18, "Triangle breakout agrees with the higher-timeframe trend"
        return 0.13, f"Triangle broke from a {regime.replace('_', ' ')} context"

    if reversal:
        opposite = "bearish_trend" if side == "Buy" else "bullish_trend"
        if regime == opposite:
            return 0.20, "Reversal pattern formed against the mature higher-timeframe move"
        if regime in {"range", "transition"}:
            return 0.17, f"Reversal pattern formed in a {regime} context"
        return 0.11, "Reversal structure is valid, but the existing trend already points the same way"

    if sweep:
        opposite = "bearish_trend" if side == "Buy" else "bullish_trend"
        if regime == opposite:
            return None
        if regime in {"range", "transition"}:
            return 0.19, f"Liquidity sweep occurred in a {regime} market"
        return 0.17, "Liquidity sweep reclaimed liquidity in the direction of the higher-timeframe trend"

    return 0.10, f"Market regime: {regime}"


def _signal_v4(candles: list[Candle], settings: dict) -> Signal | None:
    """Multi-pattern scalper with higher-timeframe market context.

    EMA is no longer an entry pattern. It participates only in the context
    classifier. Entries must come from explicit price structures: breakout
    retest, flag, triangle/compression, double top/bottom or liquidity sweep.
    """
    required = max(
        int(settings.get("lookback_candles") or 200),
        int(settings.get("double_pattern_lookback") or 32) + 5,
        int(settings.get("liquidity_sweep_lookback") or 20) + 5,
    )
    # Context aggregation needs roughly 36 completed 5m candles when running on 1m.
    if len(candles) < min(required, 190):
        return None
    current_atr = _atr(candles, int(settings.get("atr_period") or 14))
    if current_atr <= 0:
        return None
    context = _market_context(candles, settings)
    if context.get("regime") == "unknown":
        return None

    candidates: list[dict[str, Any]] = []
    if bool(settings.get("enable_breakout_retest", True)):
        v3_settings = dict(settings)
        v3_settings["minimum_signal_score"] = 0.0
        v3_settings["require_trend_confirmation"] = False
        v3_settings["require_rsi_confirmation"] = False
        v3_settings["volume_multiplier"] = _float(settings.get("pattern_volume_multiplier"), 1.5)
        old = _signal_v3(candles, v3_settings)
        if old is not None:
            candidates.append({
                "side": old.side,
                "pattern": "breakout_retest",
                "quality": 0.53,
                "reasons": ["Breakout level survived a retest before entry"],
                "indicators": dict(old.indicators),
            })
    if bool(settings.get("enable_flag", True)):
        candidate = _flag_candidate(candles, settings, current_atr)
        if candidate:
            candidates.append(candidate)
    if bool(settings.get("enable_triangle", True)):
        candidate = _triangle_candidate(candles, settings, current_atr)
        if candidate:
            candidates.append(candidate)
    if bool(settings.get("enable_double_top_bottom", True)):
        candidate = _double_candidate(candles, settings, current_atr)
        if candidate:
            candidates.append(candidate)
    if bool(settings.get("enable_liquidity_sweep", True)):
        candidate = _liquidity_sweep_candidate(candles, settings, current_atr)
        if candidate:
            candidates.append(candidate)
    if not candidates:
        return None

    latest = candles[-1]
    closes = [item.close for item in candles]
    rsi = _rsi(closes, int(settings.get("rsi_period") or 14))
    volume_ratio = _pattern_volume_ratio(candles, settings)
    volume_threshold = _float(settings.get("pattern_volume_multiplier"), 1.5)
    body_atr = abs(latest.close - latest.open) / current_atr
    scored: list[Signal] = []

    for candidate in candidates:
        context_result = _context_score(candidate, context)
        if context_result is None:
            continue
        context_points, context_reason = context_result
        side = candidate["side"]
        pattern = candidate["pattern"]
        score = _float(candidate.get("quality"), 0.5) + context_points
        reasons = [f"Pattern: {pattern.replace('_', ' ').title()}", *candidate.get("reasons", []), context_reason]

        volume_confirmed = volume_ratio >= volume_threshold
        continuation = pattern in {"breakout_retest", "bull_flag", "bear_flag"} or pattern.startswith("triangle")
        if volume_confirmed:
            score += 0.10
            reasons.append(f"Entry volume is {volume_ratio:.2f}× recent average")
        elif continuation:
            # Continuation breakouts without participation are exactly the fakeouts
            # revision 4 is intended to reject.
            continue

        if pattern in {"double_bottom", "liquidity_sweep_long"}:
            rsi_confirmed = 38.0 <= rsi <= 68.0
        elif pattern in {"double_top", "liquidity_sweep_short"}:
            rsi_confirmed = 32.0 <= rsi <= 62.0
        elif side == "Buy":
            rsi_confirmed = _float(settings.get("rsi_long_min"), 50) <= rsi <= _float(settings.get("rsi_long_max"), 70)
        else:
            rsi_confirmed = _float(settings.get("rsi_short_min"), 30) <= rsi <= _float(settings.get("rsi_short_max"), 50)
        if rsi_confirmed:
            score += 0.08
            reasons.append(f"RSI is compatible with the setup ({rsi:.1f})")

        candle_confirmed = (
            (side == "Buy" and latest.close > latest.open)
            or (side == "Sell" and latest.close < latest.open)
        ) and body_atr >= _float(settings.get("minimum_confirmation_body_atr"), 0.08)
        if candle_confirmed:
            score += 0.08
            reasons.append("Entry candle confirms the pattern direction")

        score = min(score, 1.0)
        indicators: dict[str, Any] = {
            **dict(candidate.get("indicators") or {}),
            "pattern": pattern,
            "pattern_quality": _float(candidate.get("quality"), 0.5),
            "market_regime": context.get("regime"),
            "context_timeframe": context.get("timeframe"),
            "context_ema_fast": context.get("ema_fast"),
            "context_ema_slow": context.get("ema_slow"),
            "context_ema_separation_atr": context.get("ema_separation_atr"),
            "context_ema_slope_atr": context.get("ema_slope_atr"),
            "rsi": rsi,
            "atr": current_atr,
            "volume_ratio": volume_ratio,
            "body_atr": body_atr,
        }
        if score >= _float(settings.get("minimum_signal_score"), 0.85):
            scored.append(Signal(
                side=side,
                score=score,
                price=latest.close,
                atr=current_atr,
                candle_time=latest.open_time,
                reasons=reasons,
                indicators=indicators,
                pattern=pattern,
            ))

    if not scored:
        return None
    scored.sort(key=lambda item: (item.score, _float(item.indicators.get("pattern_quality"))), reverse=True)
    winner = scored[0]
    if winner.side == "Sell" and not bool(settings.get("allow_short", True)):
        return None
    return winner


def _signal(candles: list[Candle], settings: dict) -> Signal | None:
    revision = int(_float(settings.get("strategy_revision"), 1))
    if revision >= 4:
        return _signal_v4(candles, settings)
    if revision >= 3:
        return _signal_v3(candles, settings)
    return _signal_v2(candles, settings)


def _position(adapter, bot: TradingBot) -> dict | None:
    for position in get_open_positions(adapter, category=bot.category, symbol=bot.symbol):
        size = position.size
        if size <= 0:
            continue
        side_value = (position.side or "Buy").lower()
        side = "Sell" if side_value in {"sell", "short"} else "Buy"
        return {
            "side": side,
            "size": size,
            "avg_entry_price": _float(position.avg_entry_price),
            "position": position,
        }
    return None


def _record_order(db, bot: TradingBot, order: OrderRequest, placed: PlacedOrder, payload: dict | None = None) -> TradingBotOrder:
    record = TradingBotOrder(
        created_at=utcnow(), updated_at=utcnow(), bot_id=bot.id, user_id=bot.user_id,
        exchange=bot.exchange, environment=bot.environment, category=bot.category, symbol=bot.symbol,
        side=order.side, order_type=order.order_type, order_role=order.order_role,
        order_link_id=order.order_link_id, qty=order.qty, price=order.price,
        exchange_order_id=placed.order_id, status=placed.status or "New",
        parent_order_id=order.parent_order_id,
        raw_response={
            **dict(placed.raw or {}), "orderLinkId": order.order_link_id, "side": order.side,
            "orderType": order.order_type, "qty": str(order.qty),
            "price": str(order.price) if order.price is not None else None,
            "reduceOnly": order.reduce_only, "strategy": payload or {},
        },
    )
    db.add(record)
    db.flush()
    return record


def _latest_event(db, bot: TradingBot) -> TradingBotEvent | None:
    return db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id).order_by(
        desc(TradingBotEvent.created_at), desc(TradingBotEvent.id)
    ).first()


def _wallet_balance(adapter) -> float | None:
    # Fail open on a failed balance read: sizing and the daily-loss guard skip (accepted debt).
    try:
        balance = adapter.balance()
    except Exception:  # noqa: BLE001
        return None
    for value in (balance.total_equity, balance.total_wallet, balance.wallet):
        if value is not None and value >= 0:
            return value
    coin = next((value for value in (balance.coin_equity, balance.coin_wallet) if value is not None), None)
    if coin is not None and coin >= 0:
        return coin
    return None


def _daily_pnl(adapter, bot: TradingBot) -> float:
    now = utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        executions = adapter.executions(
            bot.category,
            bot.symbol,
            start_ms=int(start.timestamp() * 1000),
            end_ms=int(now.timestamp() * 1000),
        )
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(_float(item.realised_pnl) - _float(item.fee) for item in executions)


def _risk_reason(adapter, bot: TradingBot, settings: dict, price: float, qty: float) -> str | None:
    if qty <= 0:
        return "Calculated order quantity is zero"
    maximum = _float(settings.get("max_position_qty"), bot.order_qty)
    if maximum > 0 and qty > maximum + 1e-12:
        return "Max position quantity exceeded"
    max_notional = settings.get("max_notional_usdt")
    if max_notional is not None and qty * price > _float(max_notional):
        return "Max notional exposure exceeded"
    balance = _wallet_balance(adapter)
    daily_limit = _float(settings.get("max_daily_loss_percent"), 2)
    if balance and daily_limit > 0 and _daily_pnl(adapter, bot) <= -(balance * daily_limit / 100):
        return "Maximum daily loss reached"
    return None


def _entry_qty(adapter, bot: TradingBot, settings: dict, entry: float, stop: float) -> float:
    maximum = min(float(bot.order_qty), _float(settings.get("max_position_qty"), bot.order_qty))
    if str(settings.get("position_sizing") or "risk_capped") != "risk_capped":
        return maximum
    balance = _wallet_balance(adapter)
    distance = abs(entry - stop)
    if not balance or distance <= 0:
        return maximum
    budget = balance * _float(settings.get("risk_per_trade_percent"), 0.5) / 100
    return min(maximum, budget / distance)


def _levels(signal: Signal, settings: dict, entry: float) -> tuple[float, float]:
    stop_distance = signal.atr * _float(settings.get("stop_loss_atr"), 1.2)
    take_distance = signal.atr * _float(settings.get("take_profit_atr"), 1.8)
    return (entry - stop_distance, entry + take_distance) if signal.side == "Buy" else (entry + stop_distance, entry - take_distance)


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _cooldown(bot: TradingBot, settings: dict, state: dict) -> bool:
    last_exit = _parse_time(state.get("last_exit_at"))
    return bool(last_exit and (utcnow() - last_exit).total_seconds() < _float(settings.get("cooldown_minutes"), 5) * 60)


def _should_tick(bot: TradingBot, settings: dict) -> bool:
    if bot.last_run_at is None:
        return True
    last_run = bot.last_run_at if bot.last_run_at.tzinfo else bot.last_run_at.replace(tzinfo=timezone.utc)
    return (utcnow() - last_run).total_seconds() >= _float(settings.get("run_interval_seconds"), 5)


def _close(db, bot: TradingBot, adapter, position: dict, role: str, reason: str, trade: dict | None) -> TradingBotOrder:
    now = utcnow()
    side = "Sell" if position["side"] == "Buy" else "Buy"
    rules = get_instrument_rules(adapter, category=bot.category, symbol=bot.symbol)
    request = normalize_order_request(OrderRequest(
        side=side, order_type="Market", order_role=role,
        order_link_id=f"{get_order_link_prefix(bot)}{role}-{int(now.timestamp() * 1000)}",
        qty=position["size"], price=None, reduce_only=True,
    ), rules)
    validation = validate_order_request(request, rules)
    if validation:
        raise ValueError(validation)
    response = place_order(adapter, category=bot.category, symbol=bot.symbol, order=request)
    _ensure_accepted(response, "Position close")
    record = _record_order(db, bot, request, response, {"exit_reason": reason, "trade": trade or {}})
    state = _state(bot)
    state["current_trade"] = trade or state.get("current_trade")
    state["pending_exit"] = {
        "order_link_id": request.order_link_id,
        "submitted_at": now.isoformat(),
        "reason": reason,
    }
    _save_state(db, bot, state)
    log_bot_event(db, bot, "scalper_exit_submitted", reason, {
        "side": side, "qty": request.qty, "role": role,
        "order_link_id": request.order_link_id, "exchange_order_id": record.exchange_order_id,
    })
    return record


class PatternScalperStrategy:
    strategy_type = "pattern_scalper"

    def get_effective_settings(self, bot: TradingBot) -> dict:
        return _settings(bot)

    def required_capabilities(self, bot: TradingBot) -> frozenset[Capability]:
        return COMMON_CAPABILITIES | {
            Capability.MARKET_ORDERS, Capability.POSITION_READS, Capability.SHORT_SELLING,
            Capability.WALLET_BALANCE, Capability.CANDLES,
        }

    def validate_start(self, db, bot: TradingBot) -> None:
        capabilities = registry.adapter_for_venue(bot.exchange, bot.environment).capabilities()
        if not capabilities.supports(Capability.SHORT_SELLING, bot.category):
            raise ValueError("Pattern Scalper currently supports linear perpetuals only")
        message = ensure_live_trading_allowed(db, bot)
        if message:
            raise ValueError(message)
        settings = _settings(bot)
        if settings["ema_fast_period"] >= settings["ema_slow_period"]:
            raise ValueError("EMA fast period must be lower than EMA slow period")
        if settings["context_ema_fast_period"] >= settings["context_ema_slow_period"]:
            raise ValueError("Context EMA fast period must be lower than context EMA slow period")
        entry_seconds = _interval_seconds(settings["timeframe"])
        context_seconds = _interval_seconds(settings["context_timeframe"])
        if context_seconds > entry_seconds and context_seconds % entry_seconds == 0:
            ratio = context_seconds // entry_seconds
            minimum_lookback = (settings["context_ema_slow_period"] + 3) * ratio
            if settings["lookback_candles"] < minimum_lookback:
                raise ValueError(
                    f"Lookback candles must be at least {minimum_lookback} for "
                    f"{settings['timeframe']}m entry + {settings['context_timeframe']}m context"
                )
        if _float(settings["stop_loss_atr"]) <= 0 or _float(settings["take_profit_atr"]) <= 0:
            raise ValueError("ATR stop-loss and take-profit multipliers must be greater than zero")
        if not 0 < _float(settings["minimum_signal_score"]) <= 1:
            raise ValueError("Minimum signal score must be between 0 and 1")
        if _float(settings["volume_multiplier"]) <= 0:
            raise ValueError("Volume multiplier must be greater than zero")
        if _float(settings.get("pattern_volume_multiplier")) <= 0:
            raise ValueError("Pattern volume multiplier must be greater than zero")
        if not any(bool(settings.get(key)) for key in (
            "enable_breakout_retest", "enable_flag", "enable_triangle",
            "enable_double_top_bottom", "enable_liquidity_sweep",
        )):
            raise ValueError("At least one scalper entry pattern must be enabled")
        if _float(settings.get("breakout_buffer_atr"), 0.0) < 0:
            raise ValueError("Breakout ATR buffer cannot be negative")
        if _float(settings.get("maximum_breakout_body_atr"), 1.6) < _float(settings.get("minimum_body_atr"), 0.25):
            raise ValueError("Maximum breakout body ATR must be at least the minimum body ATR")
        if _float(settings.get("minimum_ema_separation_atr"), 0.0) < 0 or _float(settings.get("minimum_ema_slope_atr"), 0.0) < 0:
            raise ValueError("EMA strength filters cannot be negative")
        close_location = _float(settings.get("minimum_breakout_close_location"), 0.65)
        if not 0.5 <= close_location <= 0.95:
            raise ValueError("Breakout close location must be between 0.5 and 0.95")
        for key in ("retest_tolerance_atr", "retest_max_penetration_atr", "retest_reclaim_atr", "minimum_confirmation_body_atr"):
            if _float(settings.get(key), 0.0) < 0:
                raise ValueError(f"{key} cannot be negative")

    def sync_orders(self, db, bot: TradingBot, *, adapter=None) -> list[TradingBotOrder]:
        return [change["order"] for change in sync_bot_orders(db, bot, adapter=adapter)]

    def cancel_orders(self, db, bot: TradingBot, *, adapter=None) -> list[TradingBotOrder]:
        return cancel_all_bot_orders(db, bot, adapter=adapter)

    def get_runtime_state(self, db, bot: TradingBot) -> tuple[str, str | None]:
        latest = _latest_event(db, bot)
        risk = latest.message if latest and latest.event_type == "risk_blocked" else None
        if bot.runtime_status in {"retrying", "paused", "error"}:
            return bot.runtime_status, risk
        if bot.runtime_status != "running":
            return "stopped", risk
        if bot.last_error:
            return "error", risk
        if _position(registry.exchange_for(bot), bot):
            return "position_open", risk
        settings = _settings(bot)
        if _cooldown(bot, settings, _state(bot)):
            return "cooldown", risk
        if risk:
            return "risk_blocked", risk
        return "waiting_for_signal", None

    def get_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        ticker = get_ticker_snapshot(adapter, category=bot.category, symbol=bot.symbol)
        mark = _float(ticker.mark_price if ticker.mark_price is not None else ticker.last_price)
        position = _position(adapter, bot)
        if not position:
            return {"symbol": bot.symbol, "category": bot.category, "side": None, "size": "0", "avg_entry_price": None,
                    "mark_price": _fmt(mark), "liq_price": None, "unrealized_pnl": None, "unrealized_pnl_percent": None,
                    "leverage": None, "margin_mode": None, "position_value": None, "take_profit": None}
        exchange, average, size = position["position"], position["avg_entry_price"], position["size"]
        unrealized = _float(exchange.unrealised_pnl)
        if not unrealized and mark and average:
            unrealized = ((mark - average) if position["side"] == "Buy" else (average - mark)) * size
        value = _float(exchange.position_value, size * mark)
        trade = _state(bot).get("current_trade") or {}
        tp = None
        if trade.get("take_profit") is not None:
            tp = {"order_id": None, "order_link_id": trade.get("entry_order_link_id"), "price": _fmt(trade["take_profit"]),
                  "qty": _fmt(size), "status": "ManagedByStrategy", "reduce_only": True}
        return {"symbol": bot.symbol, "category": bot.category, "side": position["side"], "size": _fmt(size),
                "avg_entry_price": _fmt(average), "mark_price": _fmt(mark), "liq_price": _fmt(exchange.liq_price),
                "unrealized_pnl": _fmt(unrealized), "unrealized_pnl_percent": _fmt(unrealized / value * 100 if value else None),
                "leverage": _fmt(exchange.leverage), "margin_mode": exchange.margin_mode,
                "position_value": _fmt(value), "take_profit": tp}

    def get_risk(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        settings = _settings(bot)
        position = _position(adapter, bot)
        current_qty = position["size"] if position else 0.0
        active = db.query(TradingBotOrder).filter(
            TradingBotOrder.bot_id == bot.id, TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES)
        ).all()
        pending = sum(order.qty for order in active if not order.is_reduce_only)
        price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
        potential = current_qty + pending
        reason = _risk_reason(adapter, bot, settings, price, potential or bot.order_qty)
        if not reason and _cooldown(bot, settings, _state(bot)):
            reason = "Cooldown active"
        return {"max_position_qty": _fmt(settings["max_position_qty"]), "current_position_qty": _fmt(current_qty),
                "pending_buy_qty": _fmt(pending), "potential_total_qty": _fmt(potential),
                "max_open_orders": settings["max_open_orders"], "current_open_orders": len(active),
                "max_notional_usdt": _fmt(settings.get("max_notional_usdt")), "estimated_notional_usdt": _fmt(potential * price),
                "allow_live_trading": bool(settings.get("allow_live_trading", False)),
                "is_live_environment": str(bot.environment).lower() == "live",
                "blocked": bool(reason and reason != "Cooldown active"), "reason": reason}

    def close_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        position = _position(adapter, bot)
        if not position:
            return {"message": "No open position to close", "order": None}
        order = _close(db, bot, adapter, position, "scalper_manual_close", "Manual position close requested", _state(bot).get("current_trade"))
        db.flush()
        return {"message": "Position close order submitted", "order": order}

    def tick(self, db, bot: TradingBot, *, force: bool = False, adapter=None) -> dict:
        settings = _settings(bot)
        if bot.runtime_status != "running":
            return {"orders": [], "events": 0, "message": "Bot is not running"}
        if not bot.is_active:
            bot.runtime_status = "stopped"; bot.last_error = "Bot is inactive"
            log_bot_event(db, bot, "error", bot.last_error); db.add(bot); db.commit()
            return {"orders": [], "events": 1, "message": bot.last_error}
        if not _should_tick(bot, settings):
            return {"orders": [], "events": 0, "message": "Tick skipped by interval"}
        live_message = ensure_live_trading_allowed(db, bot)
        if live_message:
            bot.last_run_at = utcnow(); db.add(bot); db.commit()
            return {"orders": [], "events": 1, "message": live_message}

        try:
            if adapter is None:
                adapter = registry.exchange_for(bot)
            synced = sync_bot_orders(db, bot, adapter=adapter)
            position = _position(adapter, bot)
            state = _state(bot)
            trade = dict(state.get("current_trade") or {})
            created: list[TradingBotOrder] = []

            pending_exit = dict(state.get("pending_exit") or {})
            if not position and trade:
                opened_at = _parse_time(trade.get("opened_at"))
                entry_is_settling = not pending_exit and opened_at is not None and (utcnow() - opened_at).total_seconds() < 30
                if not entry_is_settling:
                    state["current_trade"] = None
                    state["pending_exit"] = None
                    state["last_exit_at"] = utcnow().isoformat()
                    _save_state(db, bot, state)
                    log_bot_event(db, bot, "scalper_position_closed", "Scalper position is closed", {
                        "entry_order_link_id": trade.get("entry_order_link_id"),
                        "side": trade.get("side"),
                        "entry_price": trade.get("entry_price"),
                        "exit_reason": pending_exit.get("reason") if pending_exit else None,
                        "exit_order_link_id": pending_exit.get("order_link_id") if pending_exit else None,
                    })
                    trade = {}
                    pending_exit = {}
            elif not position and pending_exit:
                state["pending_exit"] = None
                _save_state(db, bot, state)
                pending_exit = {}

            current_price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
            if position:
                exit_is_settling = False
                if pending_exit:
                    submitted_at = _parse_time(pending_exit.get("submitted_at"))
                    exit_is_settling = submitted_at is not None and (utcnow() - submitted_at).total_seconds() < 60
                    if not exit_is_settling:
                        state["pending_exit"] = None
                        _save_state(db, bot, state)
                        pending_exit = {}
                if not trade:
                    candles = _closed_candles(adapter, bot, settings)
                    atr = _atr(candles, settings["atr_period"])
                    recovery = Signal(position["side"], 0, position["avg_entry_price"], atr, candles[-1].open_time if candles else 0, ["Recovered open position"], {"atr": atr})
                    stop, take = _levels(recovery, settings, position["avg_entry_price"])
                    trade = {"side": position["side"], "entry_price": position["avg_entry_price"], "stop_loss": stop,
                             "take_profit": take, "opened_at": utcnow().isoformat(), "recovered": True}
                    state["current_trade"] = trade; _save_state(db, bot, state)
                opened = _parse_time(trade.get("opened_at")) or utcnow()
                held = max((utcnow() - opened).total_seconds(), 0)
                stop, take = _float(trade.get("stop_loss")), _float(trade.get("take_profit"))
                role = reason = None
                if position["side"] == "Buy" and stop and current_price <= stop: role, reason = "scalper_stop_loss", "Long stop-loss reached"
                elif position["side"] == "Buy" and take and current_price >= take: role, reason = "scalper_take_profit", "Long take-profit reached"
                elif position["side"] == "Sell" and stop and current_price >= stop: role, reason = "scalper_stop_loss", "Short stop-loss reached"
                elif position["side"] == "Sell" and take and current_price <= take: role, reason = "scalper_take_profit", "Short take-profit reached"
                elif held >= _float(settings.get("max_holding_minutes"), 30) * 60: role, reason = "scalper_timeout", "Maximum holding time reached"
                if role and not exit_is_settling:
                    created.append(_close(db, bot, adapter, position, role, reason, trade))
            elif not trade:
                active_entries = [order for order in db.query(TradingBotOrder).filter(
                    TradingBotOrder.bot_id == bot.id, TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES)
                ).all() if not order.is_reduce_only]
                if not active_entries and not _cooldown(bot, settings, state):
                    signal = _signal(_closed_candles(adapter, bot, settings), settings)
                    if signal and signal.candle_time != state.get("last_signal_candle"):
                        state["last_signal_candle"] = signal.candle_time
                        _save_state(db, bot, state)
                        stop, take = _levels(signal, settings, current_price)
                        rules = get_instrument_rules(adapter, category=bot.category, symbol=bot.symbol)
                        request = normalize_order_request(OrderRequest(
                            side=signal.side, order_type="Market",
                            order_role="scalper_entry_long" if signal.side == "Buy" else "scalper_entry_short",
                            order_link_id=f"{get_order_link_prefix(bot)}scalper-entry-{signal.side.lower()}-{signal.candle_time}",
                            qty=_entry_qty(adapter, bot, settings, current_price, stop), price=None, reduce_only=False,
                        ), rules)
                        risk = _risk_reason(adapter, bot, settings, current_price, request.qty)
                        validation = validate_order_request(request, rules)
                        notional = request.qty * current_price
                        if not validation and rules.min_notional_value > 0 and notional + 1e-12 < rules.min_notional_value:
                            validation = f"Order notional {notional:.8f} is below Bybit minimum {rules.min_notional_value}"
                        if risk or validation:
                            message = risk or validation or "Entry blocked"
                            log_bot_event(db, bot, "risk_blocked", message, {"side": signal.side, "score": signal.score, "qty": request.qty, "price": current_price})
                        else:
                            response = place_order(adapter, category=bot.category, symbol=bot.symbol, order=request)
                            _ensure_accepted(response, "Scalper entry")
                            payload = {"signal_score": signal.score, "signal_reasons": signal.reasons, "signal_pattern": signal.pattern, "indicators": signal.indicators,
                                       "signal_candle_time": signal.candle_time, "entry_price": current_price, "stop_loss": stop, "take_profit": take}
                            created.append(_record_order(db, bot, request, response, payload))
                            state["current_trade"] = {"side": signal.side, "entry_price": current_price, "stop_loss": stop,
                                                      "take_profit": take, "opened_at": utcnow().isoformat(),
                                                      "entry_order_link_id": request.order_link_id, "signal_score": signal.score,
                                                      "signal_pattern": signal.pattern, "signal_reasons": signal.reasons, "indicators": signal.indicators}
                            _save_state(db, bot, state)
                            log_bot_event(db, bot, "scalper_signal_entered", "Pattern scalper opened a position", {
                                "side": signal.side, "score": signal.score, "pattern": signal.pattern, "reasons": signal.reasons,
                                "entry_price": current_price, "stop_loss": stop, "take_profit": take,
                                "qty": request.qty, "order_link_id": request.order_link_id,
                            })

            bot.last_run_at = utcnow(); bot.last_error = None; db.add(bot); db.commit()
            return {"orders": [change["order"] for change in synced] + created, "events": len(synced), "message": "Pattern scalper tick completed"}
        except Exception as exc:  # noqa: BLE001
            if not bot.is_backtest:
                raise
            bot.last_run_at = utcnow(); bot.last_error = str(exc)
            if bool(settings.get("stop_bot_on_error", True)):
                bot.runtime_status = "stopped"
            log_bot_event(db, bot, "error", str(exc)); db.add(bot); db.commit()
            return {"orders": [], "events": 1, "message": str(exc)}
