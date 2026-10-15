"""Explainable EMA/RSI/volume/ATR momentum strategy.

The runtime stays inside the existing strategy registry and reuses the shared
exchange/session/order reconciliation helpers.  Momentum entries are generated
from closed candles only, scored on a 0..100 scale, then executed with
risk-capped market orders plus ATR-based stop / take-profit / trailing-stop
management.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from sqlalchemy import desc

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.errors import GuardRefusal
from app.bot_engine.exchange.port import PlacedOrder
from app.bot_engine.events import log_bot_event
from app.bot_engine.grid_runtime import (
    cancel_all_bot_orders,
    ensure_live_trading_allowed,
    get_order_link_prefix,
    sync_bot_orders,
)
from app.bot_engine.market_data import get_instrument_rules, get_last_price, get_ticker_snapshot
from app.bot_engine.orders import (
    ensure_accepted as _ensure_accepted,
    ACTIVE_ORDER_STATUSES,
    OrderRequest,
    normalize_order_request,
    place_order,
    round_to_step,
    validate_order_request,
)
from app.bot_engine.positions import get_open_positions
from app.core.clock import utcnow
from app.bot_engine.exchange.capabilities import Capability
from app.bot_engine.strategies.base import COMMON_CAPABILITIES
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder

_SIGNAL_HISTORY_LIMIT = 30
_SUPPORTED_TIMEFRAMES = {"1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "W", "M"}


@dataclass(slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(slots=True)
class MomentumSignal:
    type: str
    score: float
    confidence: float
    price: float
    timestamp: int
    indicators: dict[str, Any]
    reasons: list[str]
    market_regime: str = "sideways"

    @property
    def side(self) -> str | None:
        if self.type == "long":
            return "Buy"
        if self.type == "short":
            return "Sell"
        return None


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


def _normalize_timeframe(value: Any) -> str:
    text = str(value or "15").strip().lower()
    mapping = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "6h": "360",
        "12h": "720",
        "1d": "D",
        "1w": "W",
        "1mo": "M",
    }
    normalized = mapping.get(text, str(value or "15").strip())
    return normalized if normalized in _SUPPORTED_TIMEFRAMES else "15"



def _interval_seconds(interval: str) -> int:
    return {
        "1": 60,
        "3": 180,
        "5": 300,
        "15": 900,
        "30": 1800,
        "60": 3600,
        "120": 7200,
        "240": 14400,
        "360": 21600,
        "720": 43200,
        "D": 86400,
        "W": 604800,
        "M": 2592000,
    }.get(str(interval), 900)


def _settings(bot: TradingBot) -> dict[str, Any]:
    raw = dict(bot.settings or {})
    value: dict[str, Any] = {
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
        "volume_multiplier": 1.5,
        "atr_period": 14,
        "atr_stop_loss_multiplier": 1.5,
        "atr_take_profit_multiplier": 3.0,
        "risk_per_trade_percent": 1.0,
        "max_open_positions": 1,
        "cooldown_minutes": 15,
        "trailing_stop_enabled": True,
        "trailing_stop_atr_multiplier": 2.0,
        "minimum_signal_score": 70.0,
        "minimum_atr_percent": 0.10,
        "minimum_average_volume": 0.0,
        "estimated_fee_rate": 0.0006,
        "position_sizing": "risk_capped",
        "max_position_qty": float(bot.order_qty),
        "max_open_orders": 1,
        "max_notional_usdt": None,
        "allow_live_trading": False,
        "close_position_on_stop": True,
        "stop_bot_on_error": True,
        "error_max_retries": 5,
        "error_retry_base_seconds": 5,
        "error_retry_max_seconds": 300,
        "cancel_orders_on_stop": True,
        "run_interval_seconds": 5,
    }
    value.update(raw)
    value["timeframe"] = _normalize_timeframe(value.get("timeframe"))
    for key, minimum in (
        ("lookback_candles", 80),
        ("fast_ema_period", 2),
        ("slow_ema_period", 3),
        ("rsi_period", 2),
        ("volume_period", 2),
        ("atr_period", 2),
        ("max_open_positions", 1),
        ("max_open_orders", 1),
    ):
        value[key] = max(int(value.get(key) or minimum), minimum)
    for key, minimum in (
        ("rsi_long_threshold", 1),
        ("rsi_short_threshold", 1),
        ("rsi_long_ceiling", 1),
        ("rsi_short_floor", 0),
    ):
        value[key] = max(int(value.get(key) or minimum), minimum)
    for key, minimum in (
        ("volume_multiplier", 0.01),
        ("atr_stop_loss_multiplier", 0.01),
        ("atr_take_profit_multiplier", 0.01),
        ("risk_per_trade_percent", 0.01),
        ("cooldown_minutes", 0.0),
        ("trailing_stop_atr_multiplier", 0.01),
        ("minimum_signal_score", 1.0),
        ("minimum_atr_percent", 0.0),
        ("minimum_average_volume", 0.0),
        ("estimated_fee_rate", 0.0),
    ):
        value[key] = max(_float(value.get(key), minimum), minimum)
    value["minimum_signal_score"] = min(value["minimum_signal_score"], 100.0)
    value["max_position_qty"] = max(_float(value.get("max_position_qty"), bot.order_qty), 0.0)
    value["position_side"] = str(value.get("position_side") or "both").lower()
    return value


def _state(bot: TradingBot) -> dict[str, Any]:
    return deepcopy((bot.settings or {}).get("momentum_state") or {})


def _save_state(db, bot: TradingBot, state: dict[str, Any]) -> None:
    settings = deepcopy(dict(bot.settings or {}))
    settings["momentum_state"] = deepcopy(state)
    bot.settings = settings
    db.add(bot)


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1.0 - alpha)))
    return result


def _rsi(values: list[float], period: int) -> float:
    if len(values) <= period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for previous, current in zip(values[-period - 1:-1], values[-period:]):
        change = current - previous
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain = gains / period
    average_loss = losses / period
    if average_loss <= 1e-15:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + average_gain / average_loss))


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


def _closed_candles(adapter, bot: TradingBot, settings: dict[str, Any]) -> list[Candle]:
    interval = settings["timeframe"]
    end_ms = int(utcnow().timestamp() * 1000) - _interval_seconds(interval) * 1000
    return [
        Candle(item.open_time, item.open, item.high, item.low, item.close, item.volume)
        for item in adapter.candles(
            bot.category,
            bot.symbol,
            interval=interval,
            end_ms=end_ms,
            limit=min(int(settings["lookback_candles"]), 1000),
        )
    ]


def _position(adapter, bot: TradingBot) -> dict[str, Any] | None:
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


def _position_unless_refused(bot: TradingBot) -> dict | None:
    """The open position for the runtime state; none while the guard refuses the venue (AC-10)."""
    try:
        adapter = registry.exchange_for(bot)
    except GuardRefusal:
        return None
    return _position(adapter, bot)


def _wallet_balance(adapter) -> float | None:
    # Fail open on a failed balance read: sizing and the balance guard skip (accepted debt).
    try:
        balance = adapter.balance()
    except Exception:  # noqa: BLE001
        return None
    for value in (balance.total_available, balance.total_equity, balance.total_wallet, balance.wallet):
        if value is not None and value >= 0:
            return value
    coin = next(
        (value for value in (balance.coin_available, balance.coin_equity, balance.coin_wallet) if value is not None),
        None,
    )
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


def _market_regime(fast: float, slow: float, atr: float) -> str:
    if atr <= 0:
        return "sideways"
    separation = abs(fast - slow) / atr
    if fast > slow and separation >= 0.10:
        return "bullish"
    if fast < slow and separation >= 0.10:
        return "bearish"
    return "sideways"


def _empty_signal(candles: list[Candle], reasons: list[str], *, indicators: dict[str, Any] | None = None) -> MomentumSignal:
    latest = candles[-1] if candles else Candle(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    base = {
        "fastEma": 0.0,
        "slowEma": 0.0,
        "rsi": 50.0,
        "volume": latest.volume,
        "averageVolume": 0.0,
        "volumeRatio": 0.0,
        "atr": 0.0,
        "atrPercent": 0.0,
        "priceMomentum": 0.0,
        "candleBodyPercent": 0.0,
        "market_regime": "sideways",
    }
    if indicators:
        base.update(indicators)
    return MomentumSignal(
        type="none",
        score=0.0,
        confidence=0.0,
        price=latest.close,
        timestamp=latest.open_time,
        indicators=base,
        reasons=reasons,
        market_regime=str(base.get("market_regime") or "sideways"),
    )


def _signal_payload(signal: MomentumSignal) -> dict[str, Any]:
    return {
        "type": signal.type,
        "score": round(signal.score, 4),
        "confidence": round(signal.confidence, 4),
        "price": signal.price,
        "timestamp": signal.timestamp,
        "market_regime": signal.market_regime,
        "indicators": deepcopy(signal.indicators),
        "reasons": list(signal.reasons),
    }


def _append_signal_history(state: dict[str, Any], signal: MomentumSignal) -> None:
    if signal.type == "none":
        return
    history = [item for item in list(state.get("signal_history") or []) if isinstance(item, dict)]
    payload = _signal_payload(signal)
    if history and history[-1].get("timestamp") == payload["timestamp"] and history[-1].get("type") == payload["type"]:
        history[-1] = payload
    else:
        history.append(payload)
    state["signal_history"] = history[-_SIGNAL_HISTORY_LIMIT:]


def _signal(candles: list[Candle], settings: dict[str, Any]) -> MomentumSignal:
    required = max(
        int(settings["slow_ema_period"]) + 3,
        int(settings["rsi_period"]) + 2,
        int(settings["atr_period"]) + 2,
        int(settings["volume_period"]) + 2,
        6,
    )
    if len(candles) < required:
        return _empty_signal(candles, ["Not enough closed candles for momentum analysis"])

    closes = [item.close for item in candles]
    latest = candles[-1]
    previous = candles[-2]
    fast_values = _ema(closes, int(settings["fast_ema_period"]))
    slow_values = _ema(closes, int(settings["slow_ema_period"]))
    fast = fast_values[-1]
    slow = slow_values[-1]
    rsi = _rsi(closes, int(settings["rsi_period"]))
    atr = _atr(candles, int(settings["atr_period"]))
    volume_window = candles[-int(settings["volume_period"]) - 1:-1]
    average_volume = sum(item.volume for item in volume_window) / len(volume_window) if volume_window else 0.0
    volume_ratio = latest.volume / average_volume if average_volume > 0 else 0.0
    atr_percent = (atr / latest.close * 100.0) if latest.close > 0 else 0.0
    price_momentum = latest.close - previous.close
    candle_range = max(latest.high - latest.low, 0.0)
    candle_body_percent = abs(latest.close - latest.open) / candle_range if candle_range > 0 else 0.0
    regime = _market_regime(fast, slow, atr)
    indicators = {
        "fastEma": fast,
        "slowEma": slow,
        "rsi": rsi,
        "volume": latest.volume,
        "averageVolume": average_volume,
        "volumeRatio": volume_ratio,
        "atr": atr,
        "atrPercent": atr_percent,
        "priceMomentum": price_momentum,
        "candleBodyPercent": candle_body_percent,
        "market_regime": regime,
    }
    if atr <= 0:
        return _empty_signal(candles, ["ATR is zero; volatility is insufficient"], indicators=indicators)
    if atr_percent < _float(settings.get("minimum_atr_percent"), 0.0):
        return _empty_signal(candles, ["ATR is below the minimum volatility threshold"], indicators=indicators)
    if average_volume <= _float(settings.get("minimum_average_volume"), 0.0):
        return _empty_signal(candles, ["Average volume is below the minimum liquidity threshold"], indicators=indicators)

    long_score = 0.0
    short_score = 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    long_trend = fast > slow
    short_trend = fast < slow
    long_rsi = _float(settings["rsi_long_threshold"]) <= rsi <= _float(settings.get("rsi_long_ceiling"), 100.0)
    short_rsi = _float(settings.get("rsi_short_floor"), 0.0) <= rsi <= _float(settings["rsi_short_threshold"])
    volume_confirmed = average_volume > 0 and latest.volume >= average_volume * _float(settings["volume_multiplier"], 1.5)
    long_price_momentum = latest.close > previous.close and latest.close >= fast
    short_price_momentum = latest.close < previous.close and latest.close <= fast
    bullish_candle = latest.close > latest.open and candle_body_percent >= 0.55
    bearish_candle = latest.close < latest.open and candle_body_percent >= 0.55

    if long_trend:
        long_score += 30.0
        long_reasons.append("Bullish EMA trend")
    if long_rsi:
        long_score += 20.0
        long_reasons.append(f"RSI confirms bullish momentum ({rsi:.1f})")
    elif rsi > _float(settings.get("rsi_long_ceiling"), 100.0):
        long_reasons.append("RSI is overextended for a fresh long entry")
    if volume_confirmed:
        long_score += 25.0
        long_reasons.append(f"Volume is {volume_ratio:.2f}× average")
    if long_price_momentum:
        long_score += 15.0
        long_reasons.append("Price is accelerating with the trend")
    if bullish_candle:
        long_score += 10.0
        long_reasons.append("Bullish candle confirmation")

    if short_trend:
        short_score += 30.0
        short_reasons.append("Bearish EMA trend")
    if short_rsi:
        short_score += 20.0
        short_reasons.append(f"RSI confirms bearish momentum ({rsi:.1f})")
    elif rsi < _float(settings.get("rsi_short_floor"), 0.0):
        short_reasons.append("RSI is overextended for a fresh short entry")
    if volume_confirmed:
        short_score += 25.0
        short_reasons.append(f"Volume is {volume_ratio:.2f}× average")
    if short_price_momentum:
        short_score += 15.0
        short_reasons.append("Price is accelerating with the downtrend")
    if bearish_candle:
        short_score += 10.0
        short_reasons.append("Bearish candle confirmation")

    position_side = str(settings.get("position_side") or "both").lower()
    allow_long = position_side in {"both", "long"}
    allow_short = position_side in {"both", "short"} and _short_trading_supported(None, settings, category_hint=None)
    if position_side == "short":
        allow_long = False
    if position_side == "long":
        allow_short = False

    chosen_type = "none"
    chosen_score = 0.0
    chosen_reasons: list[str] = []
    if allow_long and long_score >= short_score and long_score >= _float(settings["minimum_signal_score"]):
        chosen_type = "long"
        chosen_score = long_score
        chosen_reasons = long_reasons
    elif allow_short and short_score > long_score and short_score >= _float(settings["minimum_signal_score"]):
        chosen_type = "short"
        chosen_score = short_score
        chosen_reasons = short_reasons
    elif allow_short and not allow_long and short_score >= _float(settings["minimum_signal_score"]):
        chosen_type = "short"
        chosen_score = short_score
        chosen_reasons = short_reasons

    if chosen_type == "none":
        reasons = []
        if long_score > 0:
            reasons.append(f"Long score {long_score:.0f}/100")
        if short_score > 0:
            reasons.append(f"Short score {short_score:.0f}/100")
        if not reasons:
            reasons.append("Momentum conditions are not aligned")
        return MomentumSignal(
            type="none",
            score=max(long_score, short_score),
            confidence=max(long_score, short_score),
            price=latest.close,
            timestamp=latest.open_time,
            indicators={
                **indicators,
                "longScore": long_score,
                "shortScore": short_score,
            },
            reasons=reasons,
            market_regime=regime,
        )

    return MomentumSignal(
        type=chosen_type,
        score=chosen_score,
        confidence=chosen_score,
        price=latest.close,
        timestamp=latest.open_time,
        indicators={
            **indicators,
            "longScore": long_score,
            "shortScore": short_score,
        },
        reasons=chosen_reasons,
        market_regime=regime,
    )


def _short_trading_supported(bot: TradingBot | None, settings: dict[str, Any], *, category_hint: str | None = None) -> bool:
    category = str(category_hint or getattr(bot, "category", None) or "linear").lower()
    return category != "spot" and str(settings.get("position_side") or "both").lower() in {"both", "short"}


def _trade_levels(signal: MomentumSignal, settings: dict[str, Any], entry_price: float) -> tuple[float, float]:
    atr = _float(signal.indicators.get("atr"))
    stop_distance = atr * _float(settings.get("atr_stop_loss_multiplier"), 1.5)
    take_distance = atr * _float(settings.get("atr_take_profit_multiplier"), 3.0)
    if signal.type == "short":
        return entry_price + stop_distance, entry_price - take_distance
    return entry_price - stop_distance, entry_price + take_distance


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _cooldown_active(settings: dict[str, Any], state: dict[str, Any]) -> bool:
    last_exit = _parse_time(state.get("last_exit_at"))
    return bool(last_exit and (utcnow() - last_exit).total_seconds() < _float(settings.get("cooldown_minutes"), 0.0) * 60.0)


def _should_tick(bot: TradingBot, settings: dict[str, Any]) -> bool:
    if bot.last_run_at is None:
        return True
    last_run = bot.last_run_at if bot.last_run_at.tzinfo else bot.last_run_at.replace(tzinfo=timezone.utc)
    return (utcnow() - last_run).total_seconds() >= _float(settings.get("run_interval_seconds"), 5.0)


def _latest_event(db, bot: TradingBot) -> TradingBotEvent | None:
    return db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id).order_by(
        desc(TradingBotEvent.created_at), desc(TradingBotEvent.id)
    ).first()


def _active_entry_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return [
        order
        for order in db.query(TradingBotOrder).filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        ).all()
        if not order.is_reduce_only
    ]


def _risk_reason(adapter, bot: TradingBot, settings: dict[str, Any], price: float, qty: float) -> str | None:
    if qty <= 0:
        return "Calculated order quantity is zero"
    maximum = _float(settings.get("max_position_qty"), bot.order_qty)
    if maximum > 0 and qty > maximum + 1e-12:
        return "Max position quantity exceeded"
    max_notional = settings.get("max_notional_usdt")
    if max_notional is not None and qty * price > _float(max_notional):
        return "Max notional exposure exceeded"
    balance = _wallet_balance(adapter)
    if balance is not None and price > 0 and (qty * price) > balance + 1e-9:
        return "Insufficient available balance"
    daily_limit = _float(settings.get("max_daily_loss_percent"), 0.0)
    if balance and daily_limit > 0 and _daily_pnl(adapter, bot) <= -(balance * daily_limit / 100.0):
        return "Maximum daily loss reached"
    return None


def _entry_qty(adapter, bot: TradingBot, settings: dict[str, Any], rules, entry: float, stop: float) -> float:
    maximum = min(float(bot.order_qty), _float(settings.get("max_position_qty"), bot.order_qty))
    if str(settings.get("position_sizing") or "risk_capped") != "risk_capped":
        return round_to_step(maximum, rules.qty_step)
    balance = _wallet_balance(adapter)
    distance = abs(entry - stop)
    fee_per_unit = entry * _float(settings.get("estimated_fee_rate"), 0.0) * 2.0
    risk_per_unit = distance + fee_per_unit
    if not balance or risk_per_unit <= 0:
        return round_to_step(maximum, rules.qty_step)
    budget = balance * _float(settings.get("risk_per_trade_percent"), 1.0) / 100.0
    return round_to_step(min(maximum, budget / risk_per_unit), rules.qty_step)


def _update_trailing_trade(trade: dict[str, Any], settings: dict[str, Any], current_price: float, atr: float) -> None:
    if not bool(settings.get("trailing_stop_enabled", True)) or atr <= 0:
        return
    trailing_multiplier = _float(settings.get("trailing_stop_atr_multiplier"), 2.0)
    side = str(trade.get("side") or "Buy")
    if side == "Buy":
        highest = max(_float(trade.get("highest_price"), current_price), current_price)
        trailing = highest - atr * trailing_multiplier
        previous = _float(trade.get("trailing_stop"), _float(trade.get("stop_loss")))
        trade["highest_price"] = highest
        trade["trailing_stop"] = max(previous, trailing)
    else:
        lowest = min(_float(trade.get("lowest_price"), current_price), current_price)
        trailing = lowest + atr * trailing_multiplier
        previous = _float(trade.get("trailing_stop"), _float(trade.get("stop_loss")))
        trade["lowest_price"] = lowest
        trade["trailing_stop"] = min(previous if previous > 0 else trailing, trailing)


def _position_exit_reason(
    *,
    position: dict[str, Any],
    trade: dict[str, Any],
    current_price: float,
    signal: MomentumSignal | None,
    settings: dict[str, Any],
    emergency_risk: str | None,
) -> tuple[str, str] | None:
    if emergency_risk:
        return "momentum_emergency_close", emergency_risk
    side = position["side"]
    stop = _float(trade.get("stop_loss"))
    take = _float(trade.get("take_profit"))
    trailing = _float(trade.get("trailing_stop"), stop)
    if side == "Buy":
        if stop and current_price <= stop:
            return "momentum_stop_loss", "Momentum stop-loss reached"
        if take and current_price >= take:
            return "momentum_take_profit", "Momentum take-profit reached"
        if bool(settings.get("trailing_stop_enabled", True)) and trailing and current_price <= trailing:
            return "momentum_trailing_stop", "Momentum trailing stop reached"
        if signal and signal.type == "short" and signal.score >= _float(settings.get("minimum_signal_score"), 70.0):
            return "momentum_signal_exit", "Strong opposite momentum signal detected"
    else:
        if stop and current_price >= stop:
            return "momentum_stop_loss", "Momentum stop-loss reached"
        if take and current_price <= take:
            return "momentum_take_profit", "Momentum take-profit reached"
        if bool(settings.get("trailing_stop_enabled", True)) and trailing and current_price >= trailing:
            return "momentum_trailing_stop", "Momentum trailing stop reached"
        if signal and signal.type == "long" and signal.score >= _float(settings.get("minimum_signal_score"), 70.0):
            return "momentum_signal_exit", "Strong opposite momentum signal detected"
    return None


def _record_order(db, bot: TradingBot, order: OrderRequest, placed: PlacedOrder, payload: dict | None = None) -> TradingBotOrder:
    record = TradingBotOrder(
        created_at=utcnow(),
        updated_at=utcnow(),
        bot_id=bot.id,
        user_id=bot.user_id,
        exchange=bot.exchange,
        environment=bot.environment,
        category=bot.category,
        symbol=bot.symbol,
        side=order.side,
        order_type=order.order_type,
        order_role=order.order_role,
        order_link_id=order.order_link_id,
        qty=order.qty,
        price=order.price,
        exchange_order_id=placed.order_id,
        status=placed.status or "New",
        parent_order_id=order.parent_order_id,
        raw_response={
            **dict(placed.raw or {}),
            "orderLinkId": order.order_link_id,
            "side": order.side,
            "orderType": order.order_type,
            "qty": str(order.qty),
            "price": str(order.price) if order.price is not None else None,
            "reduceOnly": order.reduce_only,
            "strategy": payload or {},
        },
    )
    db.add(record)
    db.flush()
    return record


def _close(db, bot: TradingBot, adapter, position: dict[str, Any], role: str, reason: str, trade: dict[str, Any] | None) -> TradingBotOrder:
    now = utcnow()
    side = "Sell" if position["side"] == "Buy" else "Buy"
    rules = get_instrument_rules(adapter, category=bot.category, symbol=bot.symbol)
    request = normalize_order_request(OrderRequest(
        side=side,
        order_type="Market",
        order_role=role,
        order_link_id=f"{get_order_link_prefix(bot)}{role}-{int(now.timestamp() * 1000)}",
        qty=position["size"],
        price=None,
        reduce_only=True,
    ), rules)
    validation = validate_order_request(request, rules)
    if validation:
        raise ValueError(validation)
    response = place_order(adapter, category=bot.category, symbol=bot.symbol, order=request)
    _ensure_accepted(response, "Momentum close")
    record = _record_order(db, bot, request, response, {
        "exit_reason": reason,
        "trade": trade or {},
    })
    state = _state(bot)
    state["pending_exit"] = {
        "order_link_id": request.order_link_id,
        "submitted_at": now.isoformat(),
        "reason": reason,
    }
    state["current_trade"] = trade or state.get("current_trade")
    _save_state(db, bot, state)
    log_bot_event(db, bot, "momentum_exit_submitted", reason, {
        "side": side,
        "qty": request.qty,
        "role": role,
        "order_link_id": request.order_link_id,
        "exchange_order_id": record.exchange_order_id,
    })
    return record


class MomentumStrategy:
    strategy_type = "momentum"

    def get_effective_settings(self, bot: TradingBot) -> dict:
        return _settings(bot)

    def required_capabilities(self, bot: TradingBot) -> frozenset[Capability]:
        needs = COMMON_CAPABILITIES | {
            Capability.MARKET_ORDERS, Capability.POSITION_READS, Capability.WALLET_BALANCE, Capability.CANDLES,
        }
        if _short_trading_supported(bot, _settings(bot)):
            needs |= {Capability.SHORT_SELLING}
        return needs

    def validate_start(self, db, bot: TradingBot) -> None:
        settings = _settings(bot)
        if settings["fast_ema_period"] >= settings["slow_ema_period"]:
            raise ValueError("Momentum fast EMA period must be lower than slow EMA period")
        if settings["position_side"] not in {"long", "short", "both"}:
            raise ValueError("Momentum position side must be long, short, or both")
        if bot.category == "spot" and settings["position_side"] == "short":
            raise ValueError("Spot momentum bots cannot open short positions")
        message = ensure_live_trading_allowed(db, bot)
        if message:
            raise ValueError(message)
        if settings["rsi_short_threshold"] >= settings["rsi_long_threshold"]:
            raise ValueError("Momentum short RSI threshold must be lower than long RSI threshold")
        if settings["rsi_long_threshold"] >= settings.get("rsi_long_ceiling", 100):
            raise ValueError("Momentum long RSI threshold must be lower than the long RSI ceiling")
        if settings.get("rsi_short_floor", 0) >= settings["rsi_short_threshold"]:
            raise ValueError("Momentum short RSI floor must be lower than the short RSI threshold")
        if settings["minimum_signal_score"] <= 0 or settings["minimum_signal_score"] > 100:
            raise ValueError("Momentum minimum signal score must be between 1 and 100")
        if settings["atr_stop_loss_multiplier"] <= 0 or settings["atr_take_profit_multiplier"] <= 0:
            raise ValueError("Momentum ATR stop-loss and take-profit multipliers must be greater than zero")
        if settings["trailing_stop_enabled"] and settings["trailing_stop_atr_multiplier"] <= 0:
            raise ValueError("Momentum trailing stop ATR multiplier must be greater than zero")
        if settings["volume_multiplier"] <= 0:
            raise ValueError("Momentum volume multiplier must be greater than zero")
        if settings["risk_per_trade_percent"] <= 0:
            raise ValueError("Momentum risk per trade percent must be greater than zero")

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
        settings = _settings(bot)
        position = _position_unless_refused(bot)
        if position:
            return "position_open", risk
        if _cooldown_active(settings, _state(bot)):
            return "cooldown", risk
        if _active_entry_orders(db, bot):
            return "waiting_for_entry", risk
        if risk:
            return "risk_blocked", risk
        return "waiting_for_signal", None

    def get_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        ticker = get_ticker_snapshot(adapter, category=bot.category, symbol=bot.symbol)
        mark = _float(ticker.mark_price if ticker.mark_price is not None else ticker.last_price)
        position = _position(adapter, bot)
        state = _state(bot)
        trade = state.get("current_trade") or {}
        if not position:
            return {
                "symbol": bot.symbol,
                "category": bot.category,
                "side": None,
                "size": "0",
                "avg_entry_price": None,
                "mark_price": _fmt(mark),
                "liq_price": None,
                "unrealized_pnl": None,
                "unrealized_pnl_percent": None,
                "leverage": None,
                "margin_mode": None,
                "position_value": None,
                "take_profit": None,
            }
        exchange = position["position"]
        average = position["avg_entry_price"]
        size = position["size"]
        unrealized = _float(exchange.unrealised_pnl)
        if not unrealized and mark and average:
            unrealized = ((mark - average) if position["side"] == "Buy" else (average - mark)) * size
        value = _float(exchange.position_value, size * mark)
        tp = None
        if trade.get("take_profit") is not None:
            tp = {
                "order_id": None,
                "order_link_id": trade.get("entry_order_link_id"),
                "price": _fmt(trade.get("take_profit")),
                "qty": _fmt(size),
                "status": "ManagedByStrategy",
                "reduce_only": True,
            }
        return {
            "symbol": bot.symbol,
            "category": bot.category,
            "side": position["side"],
            "size": _fmt(size),
            "avg_entry_price": _fmt(average),
            "mark_price": _fmt(mark),
            "liq_price": _fmt(exchange.liq_price) or "0",
            "unrealized_pnl": _fmt(unrealized),
            "unrealized_pnl_percent": _fmt(unrealized / value * 100 if value else None),
            "leverage": _fmt(exchange.leverage),
            "margin_mode": exchange.margin_mode,
            "position_value": _fmt(value),
            "take_profit": tp,
        }

    def get_risk(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        settings = _settings(bot)
        position = _position(adapter, bot)
        current_qty = position["size"] if position else 0.0
        active = db.query(TradingBotOrder).filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        ).all()
        pending = sum(order.qty for order in active if not order.is_reduce_only)
        price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
        potential = current_qty + pending
        reason = _risk_reason(adapter, bot, settings, price, potential or bot.order_qty)
        if not reason and _cooldown_active(settings, _state(bot)):
            reason = "Cooldown active"
        return {
            "max_position_qty": _fmt(settings["max_position_qty"]),
            "current_position_qty": _fmt(current_qty),
            "pending_buy_qty": _fmt(pending),
            "potential_total_qty": _fmt(potential),
            "max_open_orders": settings["max_open_orders"],
            "current_open_orders": len(active),
            "max_notional_usdt": _fmt(settings.get("max_notional_usdt")),
            "estimated_notional_usdt": _fmt(potential * price),
            "allow_live_trading": bool(settings.get("allow_live_trading", False)),
            "is_live_environment": str(bot.environment).lower() == "live",
            "blocked": bool(reason and reason != "Cooldown active"),
            "reason": reason,
        }

    def close_position(self, db, bot: TradingBot, *, adapter=None) -> dict:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        position = _position(adapter, bot)
        if not position:
            return {"message": "No open position to close", "order": None}
        order = _close(
            db,
            bot,
            adapter,
            position,
            "momentum_manual_close",
            "Manual momentum position close requested",
            _state(bot).get("current_trade"),
        )
        db.flush()
        return {"message": "Position close order submitted", "order": order}

    def tick(self, db, bot: TradingBot, *, force: bool = False, adapter=None) -> dict:
        settings = _settings(bot)
        if bot.runtime_status != "running":
            return {"orders": [], "events": 0, "message": "Bot is not running"}
        if not bot.is_active:
            bot.runtime_status = "stopped"
            bot.last_error = "Bot is inactive"
            log_bot_event(db, bot, "error", bot.last_error)
            db.add(bot)
            db.commit()
            return {"orders": [], "events": 1, "message": bot.last_error}
        if not force and not _should_tick(bot, settings):
            return {"orders": [], "events": 0, "message": "Tick skipped by interval"}
        live_message = ensure_live_trading_allowed(db, bot)
        if live_message:
            bot.last_run_at = utcnow()
            db.add(bot)
            db.commit()
            return {"orders": [], "events": 1, "message": live_message}

        try:
            if adapter is None:
                adapter = registry.exchange_for(bot)
            synced = sync_bot_orders(db, bot, adapter=adapter)
            candles = _closed_candles(adapter, bot, settings)
            current_price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
            signal = _signal(candles, settings)
            state = _state(bot)
            created: list[TradingBotOrder] = []
            position = _position(adapter, bot)
            trade = dict(state.get("current_trade") or {})
            pending_exit = dict(state.get("pending_exit") or {})

            state["last_analysis"] = _signal_payload(signal)
            state["market_regime"] = signal.market_regime
            state["last_analysis_at"] = utcnow().isoformat()
            state["current_signal"] = signal.type
            state["current_signal_score"] = signal.score

            if signal.type != "none" and signal.timestamp != state.get("last_logged_signal_candle"):
                state["last_logged_signal_candle"] = signal.timestamp
                _append_signal_history(state, signal)
                log_bot_event(db, bot, "momentum_signal_generated", f"Momentum {signal.type.upper()} signal scored {signal.score:.0f}/100", _signal_payload(signal))

            if not position and trade:
                opened_at = _parse_time(trade.get("opened_at"))
                entry_is_settling = not pending_exit and opened_at is not None and (utcnow() - opened_at).total_seconds() < 30
                if not entry_is_settling:
                    state["current_trade"] = None
                    state["pending_exit"] = None
                    state["last_exit_at"] = utcnow().isoformat()
                    _save_state(db, bot, state)
                    log_bot_event(db, bot, "momentum_position_closed", "Momentum position is closed", {
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

            if position:
                atr_now = _float(signal.indicators.get("atr"))
                if not trade:
                    recovery_signal = signal if signal.type != "none" else MomentumSignal(
                        type="long" if position["side"] == "Buy" else "short",
                        score=0.0,
                        confidence=0.0,
                        price=position["avg_entry_price"],
                        timestamp=candles[-1].open_time if candles else 0,
                        indicators={"atr": atr_now, **(signal.indicators or {})},
                        reasons=["Recovered open position"],
                        market_regime=signal.market_regime,
                    )
                    stop, take = _trade_levels(recovery_signal, settings, position["avg_entry_price"])
                    trade = {
                        "side": position["side"],
                        "entry_price": position["avg_entry_price"],
                        "stop_loss": stop,
                        "take_profit": take,
                        "trailing_stop": stop,
                        "highest_price": current_price,
                        "lowest_price": current_price,
                        "opened_at": utcnow().isoformat(),
                        "recovered": True,
                        "market_regime": signal.market_regime,
                        "signal_score": signal.score,
                        "signal_reasons": signal.reasons,
                        "indicators": signal.indicators,
                    }
                trade.setdefault("entry_price", position["avg_entry_price"])
                trade.setdefault("side", position["side"])
                trade.setdefault("highest_price", current_price)
                trade.setdefault("lowest_price", current_price)
                trade.setdefault("trailing_stop", _float(trade.get("stop_loss")))
                _update_trailing_trade(trade, settings, current_price, atr_now)
                state["current_trade"] = trade
                _save_state(db, bot, state)

                exit_is_settling = False
                if pending_exit:
                    submitted_at = _parse_time(pending_exit.get("submitted_at"))
                    exit_is_settling = submitted_at is not None and (utcnow() - submitted_at).total_seconds() < 60
                    if not exit_is_settling:
                        state["pending_exit"] = None
                        _save_state(db, bot, state)
                        pending_exit = {}

                emergency = _risk_reason(adapter, bot, settings, current_price, position["size"])
                if emergency == "Insufficient available balance":
                    emergency = None
                exit_reason = _position_exit_reason(
                    position=position,
                    trade=trade,
                    current_price=current_price,
                    signal=signal,
                    settings=settings,
                    emergency_risk=emergency,
                )
                if exit_reason and not exit_is_settling:
                    created.append(_close(db, bot, adapter, position, exit_reason[0], exit_reason[1], trade))
            elif not trade:
                active_entries = _active_entry_orders(db, bot)
                if not active_entries and not _cooldown_active(settings, state):
                    desired_side = signal.side
                    if signal.type == "short" and not _short_trading_supported(bot, settings):
                        desired_side = None
                    if signal.type != "none" and desired_side and signal.timestamp != state.get("last_entry_signal_candle"):
                        stop, take = _trade_levels(signal, settings, current_price)
                        rules = get_instrument_rules(adapter, category=bot.category, symbol=bot.symbol)
                        raw_qty = _entry_qty(adapter, bot, settings, rules, current_price, stop)
                        request = normalize_order_request(OrderRequest(
                            side=desired_side,
                            order_type="Market",
                            order_role="momentum_entry_long" if desired_side == "Buy" else "momentum_entry_short",
                            order_link_id=f"{get_order_link_prefix(bot)}momentum-entry-{desired_side.lower()}-{signal.timestamp}",
                            qty=raw_qty,
                            price=None,
                            reduce_only=False,
                        ), rules)
                        risk = _risk_reason(adapter, bot, settings, current_price, request.qty)
                        validation = validate_order_request(request, rules)
                        notional = request.qty * current_price
                        if not validation and rules.min_notional_value > 0 and notional + 1e-12 < rules.min_notional_value:
                            validation = f"Order notional {notional:.8f} is below the exchange minimum {rules.min_notional_value}"
                        if risk or validation:
                            message = risk or validation or "Entry blocked"
                            log_bot_event(db, bot, "risk_blocked", message, {
                                "side": desired_side,
                                "score": signal.score,
                                "qty": request.qty,
                                "price": current_price,
                            })
                        else:
                            response = place_order(adapter, category=bot.category, symbol=bot.symbol, order=request)
                            _ensure_accepted(response, "Momentum entry")
                            payload = {
                                "signal": _signal_payload(signal),
                                "entry_price": current_price,
                                "stop_loss": stop,
                                "take_profit": take,
                            }
                            created.append(_record_order(db, bot, request, response, payload))
                            state["last_entry_signal_candle"] = signal.timestamp
                            state["current_trade"] = {
                                "side": desired_side,
                                "entry_price": current_price,
                                "stop_loss": stop,
                                "take_profit": take,
                                "trailing_stop": stop,
                                "highest_price": current_price,
                                "lowest_price": current_price,
                                "opened_at": utcnow().isoformat(),
                                "entry_order_link_id": request.order_link_id,
                                "signal_score": signal.score,
                                "signal_confidence": signal.confidence,
                                "signal_reasons": signal.reasons,
                                "indicators": signal.indicators,
                                "market_regime": signal.market_regime,
                            }
                            _save_state(db, bot, state)
                            log_bot_event(db, bot, "momentum_signal_entered", f"Momentum bot opened a {signal.type.upper()} position", {
                                "side": desired_side,
                                "score": signal.score,
                                "reasons": signal.reasons,
                                "entry_price": current_price,
                                "stop_loss": stop,
                                "take_profit": take,
                                "qty": request.qty,
                                "order_link_id": request.order_link_id,
                                "market_regime": signal.market_regime,
                            })
                elif _cooldown_active(settings, state):
                    state["cooldown_active"] = True

            _save_state(db, bot, state)
            bot.last_run_at = utcnow()
            bot.last_error = None
            db.add(bot)
            db.commit()
            return {
                "orders": [change["order"] for change in synced] + created,
                "events": len(synced),
                "message": "Momentum tick completed",
            }
        except Exception as exc:  # noqa: BLE001
            if not bot.is_backtest:
                raise
            bot.last_run_at = utcnow()
            bot.last_error = str(exc)
            if bool(settings.get("stop_bot_on_error", True)):
                bot.runtime_status = "stopped"
            log_bot_event(db, bot, "error", str(exc))
            db.add(bot)
            db.commit()
            return {"orders": [], "events": 1, "message": str(exc)}


