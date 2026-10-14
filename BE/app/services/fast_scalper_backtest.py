"""Fast in-memory historical backtest engine for Pattern Scalper.

The live/demo Pattern Scalper still uses the exchange adapter.  This engine is
only for historical backtests: candles are loaded once, signals are calculated
locally, and market orders/positions are simulated in memory using the same
fee/slippage/PnL rules as the exchange emulator.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import statistics
import time
from typing import Any

from sqlalchemy.orm import Session

from app.bot_engine.orders import InstrumentRules, round_to_step
from app.bot_engine.strategies.pattern_scalper import Candle, Signal, _levels, _settings, _signal
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.backtest import BacktestCycle, BacktestPoint, BacktestRun
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.services.backtest_emulator import BacktestEmulatorClient, emulator_instrument_rules, emulator_venue
from app.services.ui_stream_service import backtest_ui_stream_hub

MAX_CHART_POINTS = 1800
DEFAULT_LEVERAGE = 10.0
EPSILON = 1e-12


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _interval_seconds(interval: str) -> int:
    return {
        "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
        "60": 3600, "120": 7200, "240": 14400, "360": 21600,
        "720": 43200, "D": 86400, "W": 604800, "M": 2592000,
    }.get(str(interval), 60)


def _path_for_candle(candle: Candle, path_mode: str) -> list[float]:
    # Keep exactly the same path semantics as the existing emulator backtest.
    if path_mode in {"conservative", "ohlc"}:
        return [candle.open, candle.high, candle.low, candle.close]
    if path_mode == "olhc":
        return [candle.open, candle.low, candle.high, candle.close]
    return [candle.close]


def _slipped_price(price: float, side: str, slippage_percent: float) -> float:
    adjustment = max(slippage_percent, 0.0) / 100.0
    if side == "Buy":
        return price * (1.0 + adjustment)
    return price * (1.0 - adjustment)


def _closed_pnl(side: str, entry_price: float, exit_price: float, qty: float) -> float:
    if side == "Buy":
        return (exit_price - entry_price) * qty
    return (entry_price - exit_price) * qty


class _RollingEma:
    """Exact EMA of a sliding window, matching pattern_scalper._ema semantics.

    `_ema` seeds each requested window with its oldest close.  This helper can
    remove that oldest close in O(1), then append the newest close in O(1), so a
    long backtest does not recompute 200-value EMA arrays for every candle.
    """

    def __init__(self, period: int, maxlen: int) -> None:
        self.alpha = 2.0 / (period + 1.0)
        self.beta = 1.0 - self.alpha
        self.maxlen = max(maxlen, 1)
        self.values: deque[float] = deque()
        self.ema: float | None = None

    def _remove_oldest(self) -> None:
        if len(self.values) < 2 or self.ema is None:
            if self.values:
                self.values.popleft()
            self.ema = self.values[0] if self.values else None
            return
        n = len(self.values)
        oldest = self.values[0]
        next_value = self.values[1]
        # For y = beta^(n-1)*x0 + alpha*sum(beta^k*x), removing x0
        # and re-seeding at x1 changes only the x0/x1 seed contribution.
        correction = self.beta ** (n - 1)
        self.ema = self.ema - correction * oldest + correction * next_value
        self.values.popleft()

    def prepare_for_append(self) -> float | None:
        if len(self.values) >= self.maxlen:
            self._remove_oldest()
        return self.ema

    def append(self, value: float) -> float:
        if not self.values:
            self.values.append(value)
            self.ema = value
            return value
        self.values.append(value)
        self.ema = self.beta * float(self.ema) + self.alpha * value
        return self.ema


class PatternSignalCache:
    """Incremental signal calculator equivalent to `_signal(last lookback candles)`."""

    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.lookback = max(int(settings["lookback_candles"]), 1)
        self.candles: deque[Candle] = deque(maxlen=self.lookback)
        self.fast = _RollingEma(int(settings["ema_fast_period"]), self.lookback)
        self.slow = _RollingEma(int(settings["ema_slow_period"]), self.lookback)

    def append(self, candle: Candle) -> None:
        # Keep EMA windows aligned with the candle deque before appending.
        self.fast.prepare_for_append()
        self.slow.prepare_for_append()
        self.fast.append(candle.close)
        self.slow.append(candle.close)
        self.candles.append(candle)

    def signal(self) -> Signal | None:
        settings = self.settings
        candles = list(self.candles)
        if int(_float(settings.get("strategy_revision"), 1)) >= 3:
            # Revision 3 is intentionally state-like but expressed entirely from
            # the last closed candles (breakout candle + following retest candle).
            # Reusing the shared signal function guarantees demo/live parity.
            return _signal(candles, settings)
        required = max(
            settings["ema_slow_period"] + 3,
            settings["rsi_period"] + 2,
            settings["atr_period"] + 2,
            settings["breakout_lookback"] + 2,
            settings["volume_lookback"] + 2,
        )
        if len(candles) < required:
            return None

        latest = candles[-1]
        fast = float(self.fast.ema or 0.0)
        slow = float(self.slow.ema or 0.0)
        # `_signal` asks for EMA[-2] from the same current window.  Rebuild only
        # this one value from <=200 closes; unlike the old engine this is local
        # CPU work, not HTTP/DB work.  It also preserves exact live semantics.
        alpha = self.fast.alpha
        fast_previous = candles[0].close
        for item in candles[1:-1]:
            fast_previous = item.close * alpha + fast_previous * (1.0 - alpha)

        period = int(settings["rsi_period"])
        gains = 0.0
        losses = 0.0
        closes = [item.close for item in candles[-period - 1:]]
        for previous, current in zip(closes[:-1], closes[1:]):
            change = current - previous
            gains += max(change, 0.0)
            losses += max(-change, 0.0)
        average_gain = gains / period
        average_loss = losses / period
        if average_loss <= 1e-15:
            rsi = 100.0 if average_gain > 0 else 50.0
        else:
            rsi = 100.0 - (100.0 / (1.0 + average_gain / average_loss))

        atr_period = int(settings["atr_period"])
        atr_ranges: list[float] = []
        atr_window = candles[-atr_period - 1:]
        for previous, current in zip(atr_window[:-1], atr_window[1:]):
            atr_ranges.append(max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            ))
        atr = sum(atr_ranges) / len(atr_ranges) if atr_ranges else 0.0
        if atr <= 0:
            return None

        breakout = candles[-int(settings["breakout_lookback"]) - 1:-1]
        previous_high = max(item.high for item in breakout)
        previous_low = min(item.low for item in breakout)
        volume_window = candles[-int(settings["volume_lookback"]) - 1:-1]
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

        long_score = 0.0
        short_score = 0.0
        long_reasons: list[str] = []
        short_reasons: list[str] = []
        if long_trend:
            long_score += 0.25
            long_reasons.append("EMA trend is bullish")
        if long_breakout:
            long_score += 0.30
            long_reasons.append("Closed above the recent high")
        if volume_confirmed:
            long_score += 0.20
            long_reasons.append(f"Volume is {volume_ratio:.2f}× average")
        if long_rsi:
            long_score += 0.15
            long_reasons.append(f"RSI confirms momentum ({rsi:.1f})")
        if long_body:
            long_score += 0.10
            long_reasons.append("Bullish candle has meaningful range")

        if short_trend:
            short_score += 0.25
            short_reasons.append("EMA trend is bearish")
        if short_breakout:
            short_score += 0.30
            short_reasons.append("Closed below the recent low")
        if volume_confirmed:
            short_score += 0.20
            short_reasons.append(f"Volume is {volume_ratio:.2f}× average")
        if short_rsi:
            short_score += 0.15
            short_reasons.append(f"RSI confirms momentum ({rsi:.1f})")
        if short_body:
            short_score += 0.10
            short_reasons.append("Bearish candle has meaningful range")

        require_trend = bool(settings.get("require_trend_confirmation", True))
        require_breakout = bool(settings.get("require_breakout_confirmation", True))
        require_volume = bool(settings.get("require_volume_confirmation", True))
        long_qualified = (not require_trend or long_trend) and (not require_breakout or long_breakout) and (not require_volume or volume_confirmed)
        short_qualified = (not require_trend or short_trend) and (not require_breakout or short_breakout) and (not require_volume or volume_confirmed)

        indicators = {
            "ema_fast": fast,
            "ema_slow": slow,
            "rsi": rsi,
            "atr": atr,
            "volume_ratio": volume_ratio,
            "body_atr": body_atr,
            "breakout_buffer": breakout_buffer,
            "previous_high": previous_high,
            "previous_low": previous_low,
        }
        minimum = _float(settings["minimum_signal_score"], 0.85)
        if long_qualified and long_score >= minimum and long_score >= short_score:
            return Signal("Buy", long_score, latest.close, atr, latest.open_time, long_reasons, indicators)
        if bool(settings.get("allow_short", True)) and short_qualified and short_score >= minimum:
            return Signal("Sell", short_score, latest.close, atr, latest.open_time, short_reasons, indicators)
        return None


@dataclass(slots=True)
class FastPosition:
    side: str
    qty: float
    avg_entry: float
    opened_at_ms: int
    stop_loss: float
    take_profit: float
    signal: Signal
    entry_order_id: str


class FastScalperBacktestEngine:
    def __init__(self, db: Session, run: BacktestRun) -> None:
        self.db = db
        self.run = run
        self.emulator = BacktestEmulatorClient(get_settings().exchange_emulator_url, timeout=60)
        self.exchange = emulator_venue()
        self.temp_bot: TradingBot | None = None
        self.settings: dict[str, Any] = {}
        self.rules: InstrumentRules | None = None
        self.position: FastPosition | None = None
        self.balance = run.initial_balance
        self.executions: list[dict[str, Any]] = []
        self.exec_sequence = 0
        self.order_sequence = 0
        self.last_signal_candle: int | None = None
        self.last_exit_at_ms: int | None = None
        self.daily_net: dict[str, float] = {}
        self.last_point_time: int | None = None
        self.last_price: float = 0.0
        self.signal_cache: PatternSignalCache | None = None
        self.sample_every = max(math.ceil(run.total_candles / MAX_CHART_POINTS), 1)
        self.progress_every = max(math.ceil(run.total_candles / 100), 250)
        self.pending_points: list[BacktestPoint] = []
        self.state: dict[str, Any] = {
            "peak_equity": run.initial_balance,
            "max_drawdown": 0.0,
            "current_drawdown": 0.0,
            "drawdown_started": None,
            "longest_drawdown": 0.0,
            "deepest_drawdown_time": None,
            "max_drawdown_recovery": 0.0,
            "max_drawdown_recovered": False,
            "current_drawdown_recovery": 0.0,
            "time_in_position": 0.0,
            "time_in_loss": 0.0,
            "loss_started": None,
            "longest_loss": 0.0,
            "max_unrealized_loss": 0.0,
            "max_position_qty": 0.0,
            "max_position_value": 0.0,
            "max_margin_used": 0.0,
            "lowest_available": run.initial_balance,
            "previous_time": None,
            "previous_values": None,
            "current_cycle": None,
            "cycle_number": 0,
            "closed_cycle_count": 0,
            "execution_position_qty": 0.0,
            "execution_avg_entry": 0.0,
            "execution_position_side": None,
        }

    def close(self) -> None:
        self.emulator.close()

    def _make_temp_bot(self) -> TradingBot:
        snapshot = dict(self.run.bot_snapshot or {})
        bot_settings = dict(snapshot.get("settings") or {})
        bot_settings.pop("pattern_scalper_state", None)
        bot_settings.update({
            "run_interval_seconds": 0,
            "stop_bot_on_error": True,
            "timeframe": self.run.interval,
        })
        bot = TradingBot(
            user_id=self.run.user_id,
            name=f"[Fast Backtest #{self.run.id}] {self.run.bot_name}",
            exchange="bybit",
            environment="emulator",
            strategy_type="pattern_scalper",
            category=str(snapshot.get("category") or "linear"),
            symbol=self.run.symbol,
            order_qty=_float(snapshot.get("order_qty"), 0.001),
            grid_orders_count=int(snapshot.get("grid_orders_count") or 2),
            grid_step_percent=_float(snapshot.get("grid_step_percent"), 5),
            is_active=True,
            runtime_status="running",
            settings=bot_settings,
            is_backtest=True,
            backtest_run_id=self.run.id,
            started_at=_utcnow(),
        )
        self.db.add(bot)
        self.db.flush()
        self.run.temp_bot_id = bot.id
        self.temp_bot = bot
        self.settings = _settings(bot)
        self.signal_cache = PatternSignalCache(self.settings)
        return bot

    def _load_candles(self) -> list[Candle]:
        result: list[Candle] = []
        for row in self.emulator.candles(
            dataset_id=int(self.run.dataset_id or 0),
            start_time=self.run.start_time,
            end_time=self.run.end_time,
        ):
            result.append(Candle(
                open_time=int(row["open_time"]),
                open=_float(row["open"]),
                high=_float(row["high"]),
                low=_float(row["low"]),
                close=_float(row["close"]),
                volume=_float(row.get("volume")),
            ))
        return result

    def _snapshot(self, mark_price: float) -> dict[str, float]:
        unrealized = 0.0
        qty = 0.0
        position_value = 0.0
        margin_used = 0.0
        avg_entry = 0.0
        if self.position is not None:
            qty = self.position.qty
            avg_entry = self.position.avg_entry
            direction = 1.0 if self.position.side == "Buy" else -1.0
            unrealized = (mark_price - avg_entry) * qty * direction
            position_value = qty * mark_price
            margin_used = position_value / DEFAULT_LEVERAGE
        equity = self.balance + unrealized
        available = max(equity - margin_used, 0.0)
        return {
            "balance": self.balance,
            "equity": equity,
            "available": available,
            "unrealized": unrealized,
            "position_qty": qty,
            "position_value": position_value,
            "margin_used": margin_used,
            "avg_entry": avg_entry,
            "mark_price": mark_price,
        }

    def _daily_key(self, timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).date().isoformat()

    def _record_daily_execution(self, execution: dict[str, Any]) -> None:
        key = self._daily_key(int(execution["execTime"]))
        self.daily_net[key] = self.daily_net.get(key, 0.0) + _float(execution["closedPnl"]) - _float(execution["execFee"])

    def _entry_quantity(self, current_price: float, stop_loss: float) -> float:
        assert self.temp_bot is not None
        assert self.rules is not None
        maximum = min(
            float(self.temp_bot.order_qty),
            _float(self.settings.get("max_position_qty"), self.temp_bot.order_qty),
        )
        qty = maximum
        if str(self.settings.get("position_sizing") or "risk_capped") == "risk_capped":
            distance = abs(current_price - stop_loss)
            if self.balance > 0 and distance > 0:
                budget = self.balance * _float(self.settings.get("risk_per_trade_percent"), 0.5) / 100.0
                qty = min(maximum, budget / distance)
        return round_to_step(qty, self.rules.qty_step)

    def _entry_block_reason(self, qty: float, price: float, timestamp_ms: int) -> str | None:
        assert self.rules is not None
        if qty <= 0:
            return "Calculated order quantity is zero"
        maximum = _float(self.settings.get("max_position_qty"), qty)
        if maximum > 0 and qty > maximum + EPSILON:
            return "Max position quantity exceeded"
        max_notional = self.settings.get("max_notional_usdt")
        if max_notional is not None and qty * price > _float(max_notional):
            return "Max notional exposure exceeded"
        daily_limit = _float(self.settings.get("max_daily_loss_percent"), 2)
        day_pnl = self.daily_net.get(self._daily_key(timestamp_ms), 0.0)
        if self.balance > 0 and daily_limit > 0 and day_pnl <= -(self.balance * daily_limit / 100.0):
            return "Maximum daily loss reached"
        if qty < self.rules.min_order_qty:
            return f"Order qty {qty:g} is below Bybit minimum {self.rules.min_order_qty:g}"
        minimum_notional = max(self.rules.min_notional_value, 5.0)
        if minimum_notional > 0 and qty * price + EPSILON < minimum_notional:
            return f"Order notional {qty * price:.8f} is below minimum {minimum_notional:g}"
        required_margin = qty * price / DEFAULT_LEVERAGE
        available = self._snapshot(price)["available"]
        if required_margin > available + 1e-9:
            return "Insufficient available balance"
        return None

    def _cooldown_active(self, timestamp_ms: int) -> bool:
        if self.last_exit_at_ms is None:
            return False
        cooldown_ms = _float(self.settings.get("cooldown_minutes"), 5) * 60_000.0
        return timestamp_ms - self.last_exit_at_ms < cooldown_ms

    def _next_order_id(self, role: str) -> str:
        self.order_sequence += 1
        return f"fast-bt-{self.run.id}-{self.order_sequence}-{role}"

    def _new_order(
        self,
        *,
        side: str,
        role: str,
        qty: float,
        fill_price: float,
        timestamp_ms: int,
        execution: dict[str, Any],
        strategy_payload: dict[str, Any],
    ) -> TradingBotOrder:
        assert self.temp_bot is not None
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
        exchange_order_id = str(execution["orderId"])
        return TradingBotOrder(
            bot_id=self.temp_bot.id,
            user_id=self.run.user_id,
            exchange="bybit",
            environment="emulator",
            category=self.temp_bot.category,
            symbol=self.run.symbol,
            side=side,
            order_type="Market",
            order_role=role,
            order_link_id=exchange_order_id,
            qty=qty,
            filled_qty=qty,
            price=None,
            exchange_order_id=exchange_order_id,
            status="Filled",
            raw_response={
                "retCode": 0,
                "retMsg": "OK",
                "result": {"orderId": exchange_order_id, "orderStatus": "Filled"},
                "side": side,
                "orderType": "Market",
                "qty": str(qty),
                "reduceOnly": role not in {"scalper_entry_long", "scalper_entry_short"},
                "strategy": strategy_payload,
                "execution": execution,
                "backtest_engine": "fast_scalper",
                "fill_price": fill_price,
            },
            created_at=timestamp,
            updated_at=timestamp,
            filled_event_logged_at=timestamp,
        )

    def _event(self, event_type: str, message: str, timestamp_ms: int, payload: dict[str, Any] | None = None) -> None:
        assert self.temp_bot is not None
        self.db.add(TradingBotEvent(
            bot_id=self.temp_bot.id,
            user_id=self.run.user_id,
            event_type=event_type,
            message=message,
            payload=payload,
            created_at=datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc),
        ))

    def _execution(self, *, order_id: str, side: str, qty: float, price: float, fee: float, closed_pnl: float, timestamp_ms: int) -> dict[str, Any]:
        self.exec_sequence += 1
        return {
            "execId": f"fast-exec-{self.run.id}-{self.exec_sequence}",
            "execSeq": self.exec_sequence,
            "execTime": timestamp_ms,
            "orderId": order_id,
            "orderLinkId": order_id,
            "symbol": self.run.symbol,
            "side": side,
            "execPrice": f"{price:.12f}".rstrip("0").rstrip("."),
            "execQty": f"{qty:.12f}".rstrip("0").rstrip("."),
            "execFee": f"{fee:.12f}".rstrip("0").rstrip("."),
            "closedPnl": f"{closed_pnl:.12f}".rstrip("0").rstrip("."),
            "isMaker": False,
            "execType": "Trade",
        }

    def _apply_execution_accounting(
        self,
        execution: dict[str, Any],
        *,
        signal: Signal | None = None,
        exit_reason: str | None = None,
        exit_role: str | None = None,
    ) -> None:
        # Import lazily to keep the engine independent from the dispatcher at module import time.
        from app.services.backtest_service import _apply_execution_to_cycle_state, _store_cycle

        cycle = _apply_execution_to_cycle_state(self.state, execution)
        if signal is not None and self.state.get("current_cycle") is not None:
            self.state["current_cycle"]["details"] = {
                "signal_score": signal.score,
                "signal_reasons": signal.reasons,
                "signal_pattern": signal.pattern,
                "indicators": signal.indicators,
                "signal_candle_time": signal.candle_time,
            }
        if (exit_reason or exit_role) and self.state.get("current_cycle") is not None:
            details = dict(self.state["current_cycle"].get("details") or {})
            if exit_reason:
                details["exit_reason"] = exit_reason
            if exit_role:
                details["exit_role"] = exit_role
            self.state["current_cycle"]["details"] = details
        if cycle is not None:
            details = dict(cycle.get("details") or {})
            if exit_reason:
                details["exit_reason"] = exit_reason
            if exit_role:
                details["exit_role"] = exit_role
            cycle["details"] = details
            _store_cycle(self.db, self.run.id, cycle)

    def _open_position(self, signal: Signal, raw_price: float, timestamp_ms: int) -> None:
        assert self.rules is not None
        stop_loss, take_profit = _levels(signal, self.settings, raw_price)
        qty = self._entry_quantity(raw_price, stop_loss)
        self.last_signal_candle = signal.candle_time
        blocked = self._entry_block_reason(qty, raw_price, timestamp_ms)
        if blocked:
            self._event("risk_blocked", blocked, timestamp_ms, {
                "side": signal.side,
                "score": signal.score,
                "qty": qty,
                "price": raw_price,
            })
            return

        fill_price = _slipped_price(raw_price, signal.side, self.run.slippage_percent)
        fee = fill_price * qty * self.run.fee_rate
        order_id = self._next_order_id("entry")
        execution = self._execution(
            order_id=order_id,
            side=signal.side,
            qty=qty,
            price=fill_price,
            fee=fee,
            closed_pnl=0.0,
            timestamp_ms=timestamp_ms,
        )
        self.balance -= fee
        self.executions.append(execution)
        self._record_daily_execution(execution)
        self.position = FastPosition(
            side=signal.side,
            qty=qty,
            avg_entry=fill_price,
            opened_at_ms=timestamp_ms,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal=signal,
            entry_order_id=order_id,
        )
        payload = {
            "signal_score": signal.score,
            "signal_reasons": signal.reasons,
            "signal_pattern": signal.pattern,
            "indicators": signal.indicators,
            "signal_candle_time": signal.candle_time,
            "entry_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        self.db.add(self._new_order(
            side=signal.side,
            role="scalper_entry_long" if signal.side == "Buy" else "scalper_entry_short",
            qty=qty,
            fill_price=fill_price,
            timestamp_ms=timestamp_ms,
            execution=execution,
            strategy_payload=payload,
        ))
        self._apply_execution_accounting(execution, signal=signal)
        self._event("scalper_signal_entered", "Pattern scalper opened a position", timestamp_ms, {
            "side": signal.side,
            "score": signal.score,
            "pattern": signal.pattern,
            "reasons": signal.reasons,
            "entry_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "qty": qty,
            "order_id": order_id,
        })

    def _exit_trigger(self, raw_price: float, timestamp_ms: int) -> tuple[str, str] | None:
        position = self.position
        if position is None:
            return None
        if position.side == "Buy" and position.stop_loss and raw_price <= position.stop_loss:
            return "scalper_stop_loss", "Long stop-loss reached"
        if position.side == "Buy" and position.take_profit and raw_price >= position.take_profit:
            return "scalper_take_profit", "Long take-profit reached"
        if position.side == "Sell" and position.stop_loss and raw_price >= position.stop_loss:
            return "scalper_stop_loss", "Short stop-loss reached"
        if position.side == "Sell" and position.take_profit and raw_price <= position.take_profit:
            return "scalper_take_profit", "Short take-profit reached"
        held_seconds = max((timestamp_ms - position.opened_at_ms) / 1000.0, 0.0)
        if held_seconds >= _float(self.settings.get("max_holding_minutes"), 30) * 60.0:
            return "scalper_timeout", "Maximum holding time reached"
        return None

    def _close_position(self, raw_price: float, timestamp_ms: int, role: str, reason: str) -> None:
        position = self.position
        if position is None:
            return
        close_side = "Sell" if position.side == "Buy" else "Buy"
        fill_price = _slipped_price(raw_price, close_side, self.run.slippage_percent)
        gross_pnl = _closed_pnl(position.side, position.avg_entry, fill_price, position.qty)
        fee = fill_price * position.qty * self.run.fee_rate
        order_id = self._next_order_id(role)
        execution = self._execution(
            order_id=order_id,
            side=close_side,
            qty=position.qty,
            price=fill_price,
            fee=fee,
            closed_pnl=gross_pnl,
            timestamp_ms=timestamp_ms,
        )
        self.balance += gross_pnl - fee
        self.executions.append(execution)
        self._record_daily_execution(execution)
        payload = {
            "exit_reason": reason,
            "entry_order_id": position.entry_order_id,
            "entry_price": position.avg_entry,
            "exit_market_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "signal_score": position.signal.score,
            "signal_reasons": position.signal.reasons,
        }
        self.db.add(self._new_order(
            side=close_side,
            role=role,
            qty=position.qty,
            fill_price=fill_price,
            timestamp_ms=timestamp_ms,
            execution=execution,
            strategy_payload=payload,
        ))
        self._apply_execution_accounting(execution, exit_reason=reason, exit_role=role)
        self._event("scalper_exit_filled", reason, timestamp_ms, {
            "side": close_side,
            "qty": position.qty,
            "market_price": raw_price,
            "fill_price": fill_price,
            "gross_pnl": gross_pnl,
            "fee": fee,
            "role": role,
        })
        self.position = None
        self.last_exit_at_ms = timestamp_ms

    def _advance_time(self, point_time: int) -> None:
        """Accumulate elapsed-position metrics before the current price can close it."""
        state = self.state
        previous_time = state["previous_time"]
        previous_values = state["previous_values"]
        if previous_time is None or previous_values is None:
            return
        elapsed = max((point_time - previous_time) / 1000.0, 0.0)
        if previous_values["position_qty"] > 0:
            state["time_in_position"] += elapsed
            if previous_values["unrealized"] < 0:
                state["time_in_loss"] += elapsed
                if state["current_cycle"]:
                    state["current_cycle"]["time_in_loss"] += elapsed
        if state["drawdown_started"] is not None:
            state["longest_drawdown"] = max(
                state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000.0
            )
        if state["loss_started"] is not None:
            state["longest_loss"] = max(
                state["longest_loss"], (point_time - state["loss_started"]) / 1000.0
            )

    def _update_market_metrics(self, values: dict[str, float], point_time: int) -> None:
        state = self.state
        equity = values["equity"]
        state["peak_equity"] = max(state["peak_equity"], equity)
        drawdown = ((equity - state["peak_equity"]) / state["peak_equity"] * 100.0) if state["peak_equity"] else 0.0
        state["current_drawdown"] = drawdown
        if drawdown < state["max_drawdown"]:
            state["max_drawdown"] = drawdown
            state["deepest_drawdown_time"] = point_time
            state["max_drawdown_recovered"] = False
            state["max_drawdown_recovery"] = 0.0
        if drawdown < -EPSILON and state["drawdown_started"] is None:
            state["drawdown_started"] = point_time
        elif drawdown >= -EPSILON and state["drawdown_started"] is not None:
            state["longest_drawdown"] = max(
                state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000.0
            )
            if state["deepest_drawdown_time"] is not None and not state["max_drawdown_recovered"]:
                state["max_drawdown_recovery"] = max((point_time - state["deepest_drawdown_time"]) / 1000.0, 0.0)
                state["max_drawdown_recovered"] = True
            state["drawdown_started"] = None

        negative_open = values["position_qty"] > 0 and values["unrealized"] < 0
        if negative_open and state["loss_started"] is None:
            state["loss_started"] = point_time
        elif not negative_open and state["loss_started"] is not None:
            state["longest_loss"] = max(
                state["longest_loss"], (point_time - state["loss_started"]) / 1000.0
            )
            state["loss_started"] = None

        state["max_unrealized_loss"] = min(state["max_unrealized_loss"], values["unrealized"])
        state["max_position_qty"] = max(state["max_position_qty"], values["position_qty"])
        state["max_position_value"] = max(state["max_position_value"], values["position_value"])
        state["max_margin_used"] = max(state["max_margin_used"], values["margin_used"])
        state["lowest_available"] = min(state["lowest_available"], values["available"])

        cycle = state.get("current_cycle")
        if cycle is not None:
            cycle["last_time"] = point_time
            cycle["max_unrealized_loss"] = min(cycle["max_unrealized_loss"], values["unrealized"])
            cycle["max_qty"] = max(cycle["max_qty"], values["position_qty"])
            cycle["max_value"] = max(cycle["max_value"], values["position_value"])
            cycle["avg_entry"] = values["avg_entry"] or cycle["avg_entry"]

        state["previous_time"] = point_time
        state["previous_values"] = values

    def _check_control_flags(self) -> bool:
        self.db.expire(self.run)
        self.db.refresh(self.run)
        if self.run.cancel_requested:
            self.run.status = "cancelled"
            self.run.completed_at = _utcnow()
            if self.temp_bot is not None:
                self.temp_bot.runtime_status = "stopped"
                self.temp_bot.stopped_at = _utcnow()
                self.db.add(self.temp_bot)
            self.db.add(self.run)
            self.db.commit()
            backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "cancelled", force=True)
            return False

        if self.run.pause_requested and self.run.status != "paused":
            self.run.status = "paused"
            self.db.add(self.run)
            self.db.commit()
            backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "paused", force=True)

        while self.run.pause_requested:
            time.sleep(0.25)
            self.db.expire(self.run)
            self.db.refresh(self.run)
            if self.run.cancel_requested:
                self.run.status = "cancelled"
                self.run.completed_at = _utcnow()
                self.db.add(self.run)
                self.db.commit()
                backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "cancelled", force=True)
                return False

        if self.run.status == "paused":
            self.run.status = "running"
            self.db.add(self.run)
            self.db.commit()
            backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "running", force=True)
        return True

    def _progress_commit(self, index: int, candle: Candle, values: dict[str, float]) -> None:
        self.run.processed_candles = index
        self.run.progress = min(index / max(self.run.total_candles, 1) * 100.0, 100.0)
        self.run.current_time = candle.open_time
        self.run.current_price = candle.close
        self.run.metrics = {
            "backtest_engine": "fast_scalper",
            "equity": values["equity"],
            "net_total_pnl": values["equity"] - self.run.initial_balance,
            "maximum_drawdown_percent": self.state["max_drawdown"],
            "time_in_position_seconds": self.state["time_in_position"],
            "maximum_unrealized_loss": self.state["max_unrealized_loss"],
        }
        for point in self.pending_points:
            self.db.add(point)
        self.pending_points.clear()
        self.db.add(self.run)
        self.db.commit()
        backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "progress")

    def _finalize_metrics(self, final_values: dict[str, float]) -> dict[str, Any]:
        cycles = self.db.query(BacktestCycle).filter(BacktestCycle.run_id == self.run.id).order_by(BacktestCycle.cycle_number).all()
        closed = [cycle for cycle in cycles if cycle.status == "closed"]
        winning = [cycle for cycle in closed if cycle.net_pnl > 0]
        durations = [cycle.duration_seconds for cycle in closed]
        fees = sum(_float(item.get("execFee")) for item in self.executions)
        gross_realized = sum(_float(item.get("closedPnl")) for item in self.executions)
        net_realized = self.balance - self.run.initial_balance
        total_pnl = final_values["equity"] - self.run.initial_balance
        period_seconds = max((self.run.end_time - self.run.start_time) / 1000.0, 1.0)
        state = self.state
        long_cycles = [cycle for cycle in closed if str((cycle.details or {}).get("side") or "").lower() == "buy"]
        short_cycles = [cycle for cycle in closed if str((cycle.details or {}).get("side") or "").lower() == "sell"]
        exit_groups: dict[str, list[BacktestCycle]] = {
            "scalper_take_profit": [],
            "scalper_stop_loss": [],
            "scalper_timeout": [],
            "other": [],
        }
        for cycle in closed:
            role = str((cycle.details or {}).get("exit_role") or "")
            exit_groups[role if role in exit_groups else "other"].append(cycle)

        def exit_pnl(role: str) -> float:
            return sum(cycle.net_pnl for cycle in exit_groups[role])

        pattern_groups: dict[str, list[BacktestCycle]] = {}
        for cycle in closed:
            details = cycle.details or {}
            pattern = str(details.get("signal_pattern") or (details.get("indicators") or {}).get("pattern") or "legacy")
            pattern_groups.setdefault(pattern, []).append(cycle)
        pattern_performance = []
        for pattern, group in sorted(pattern_groups.items()):
            wins = [cycle for cycle in group if cycle.net_pnl > 0]
            pattern_performance.append({
                "pattern": pattern,
                "trades": len(group),
                "wins": len(wins),
                "losses": len(group) - len(wins),
                "win_rate_percent": (len(wins) / len(group) * 100.0) if group else 0.0,
                "gross_pnl": sum(cycle.gross_pnl for cycle in group),
                "fees": sum(cycle.fees for cycle in group),
                "net_pnl": sum(cycle.net_pnl for cycle in group),
                "average_pnl": (sum(cycle.net_pnl for cycle in group) / len(group)) if group else 0.0,
            })
        pattern_performance.sort(key=lambda item: (item["net_pnl"], item["trades"]), reverse=True)

        return {
            "backtest_engine": "fast_scalper",
            "initial_balance": self.run.initial_balance,
            "final_balance": self.balance,
            "final_equity": final_values["equity"],
            "gross_realized_pnl": gross_realized,
            "net_realized_pnl": net_realized,
            "unrealized_pnl": final_values["unrealized"],
            "net_total_pnl": total_pnl,
            "return_percent": (total_pnl / self.run.initial_balance) * 100.0,
            "total_fees": fees,
            "closed_cycles": len(closed),
            "winning_cycles": len(winning),
            "losing_cycles": len(closed) - len(winning),
            "win_rate_percent": (len(winning) / len(closed) * 100.0) if closed else 0.0,
            "average_cycle_pnl": (sum(c.net_pnl for c in closed) / len(closed)) if closed else 0.0,
            "best_cycle_pnl": max((c.net_pnl for c in closed), default=0.0),
            "worst_cycle_pnl": min((c.net_pnl for c in closed), default=0.0),
            "average_cycle_duration_seconds": (sum(durations) / len(durations)) if durations else 0.0,
            "median_cycle_duration_seconds": statistics.median(durations) if durations else 0.0,
            "longest_cycle_seconds": max(durations, default=0.0),
            "time_in_position_seconds": state["time_in_position"],
            "time_in_position_percent": state["time_in_position"] / period_seconds * 100.0,
            "time_in_loss_seconds": state["time_in_loss"],
            "time_in_loss_percent": state["time_in_loss"] / period_seconds * 100.0,
            "longest_negative_pnl_period_seconds": state["longest_loss"],
            "maximum_unrealized_loss": state["max_unrealized_loss"],
            "maximum_drawdown_percent": state["max_drawdown"],
            "longest_drawdown_seconds": state["longest_drawdown"],
            "maximum_drawdown_recovery_seconds": state["max_drawdown_recovery"],
            "maximum_drawdown_recovered": state["max_drawdown_recovered"],
            "current_drawdown_recovery_seconds": state["current_drawdown_recovery"],
            "current_drawdown_percent": state["current_drawdown"],
            "maximum_position_qty": state["max_position_qty"],
            "maximum_position_value": state["max_position_value"],
            "maximum_used_margin": state["max_margin_used"],
            "lowest_available_balance": state["lowest_available"],
            "maximum_grid_levels_filled": 1 if cycles else 0,
            "open_position_at_end": final_values["position_qty"] > 0,
            "open_position_qty": final_values["position_qty"],
            "open_position_value": final_values["position_value"],
            "open_avg_entry_price": final_values["avg_entry"] or None,
            "open_orders_at_end": 0,
            "executions_count": len(self.executions),
            "long_trades": len(long_cycles),
            "short_trades": len(short_cycles),
            "take_profit_cycles": len(exit_groups["scalper_take_profit"]),
            "take_profit_net_pnl": exit_pnl("scalper_take_profit"),
            "stop_loss_cycles": len(exit_groups["scalper_stop_loss"]),
            "stop_loss_net_pnl": exit_pnl("scalper_stop_loss"),
            "timeout_cycles": len(exit_groups["scalper_timeout"]),
            "timeout_net_pnl": exit_pnl("scalper_timeout"),
            "other_exit_cycles": len(exit_groups["other"]),
            "other_exit_net_pnl": exit_pnl("other"),
            "average_fee_per_cycle": (fees / len(closed)) if closed else 0.0,
            "pattern_performance": pattern_performance,
            "candles_processed": self.run.processed_candles,
            "execution_duration_seconds": max(
                (_utcnow() - (self.run.started_at if self.run.started_at and self.run.started_at.tzinfo else self.run.started_at.replace(tzinfo=timezone.utc))).total_seconds()
                if self.run.started_at else 0.0,
                0.0,
            ),
        }

    def run_all(self) -> None:
        self.run.status = "running"
        self.run.started_at = _utcnow()
        self.run.error = None
        self.run.emulator_account_id = None
        configuration = dict(self.run.configuration or {})
        configuration["backtest_engine"] = "fast_scalper"
        configuration["engine_version"] = 4
        self.run.configuration = configuration
        self.db.add(self.run)
        self.db.commit()
        backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "running", force=True)

        self._make_temp_bot()
        self.rules = emulator_instrument_rules(
            self.exchange, category="linear", symbol=self.run.symbol,
            missing="Instrument rules were not found for the selected symbol",
        )
        candles = self._load_candles()
        if len(candles) != self.run.total_candles:
            # The dataset was validated at creation.  A changed dataset should fail
            # loudly instead of silently running on a different history.
            raise RuntimeError(
                f"Dataset changed after backtest creation: expected {self.run.total_candles} candles, loaded {len(candles)}"
            )
        assert self.signal_cache is not None

        interval_seconds = _interval_seconds(self.run.interval)
        values = self._snapshot(candles[0].open if candles else 0.0)
        for index, candle in enumerate(candles, start=1):
            cached_signal = self.signal_cache.signal()
            path = _path_for_candle(candle, self.run.path_mode)
            segment_ms = max(int(interval_seconds * 1000 / max(len(path), 1)), 1)

            for point_index, raw_price in enumerate(path):
                point_time = candle.open_time + point_index * segment_ms
                self.last_point_time = point_time
                self.last_price = raw_price
                self._advance_time(point_time)

                had_position = self.position is not None
                if had_position:
                    trigger = self._exit_trigger(raw_price, point_time)
                    if trigger is not None:
                        self._close_position(raw_price, point_time, trigger[0], trigger[1])
                elif (
                    cached_signal is not None
                    and cached_signal.candle_time != self.last_signal_candle
                    and not self._cooldown_active(point_time)
                ):
                    self._open_position(cached_signal, raw_price, point_time)

                values = self._snapshot(raw_price)
                self._update_market_metrics(values, point_time)

            # The current candle only becomes signal input after it is fully replayed.
            self.signal_cache.append(candle)

            self.run.processed_candles = index
            self.run.progress = min(index / max(self.run.total_candles, 1) * 100.0, 100.0)
            self.run.current_time = candle.open_time
            self.run.current_price = candle.close
            if index == 1 or index % self.sample_every == 0 or index == self.run.total_candles:
                close_values = self._snapshot(candle.close)
                peak = max(self.state["peak_equity"], close_values["equity"])
                drawdown = ((close_values["equity"] - peak) / peak * 100.0) if peak else 0.0
                self.pending_points.append(BacktestPoint(
                    run_id=self.run.id,
                    timestamp=candle.open_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    balance=close_values["balance"],
                    equity=close_values["equity"],
                    available_balance=close_values["available"],
                    unrealized_pnl=close_values["unrealized"],
                    position_qty=close_values["position_qty"],
                    position_value=close_values["position_value"],
                    drawdown_percent=drawdown,
                ))

            if index % self.progress_every == 0 or index == self.run.total_candles:
                if not self._check_control_flags():
                    return
                self._progress_commit(index, candle, values)

        if self.position is not None and self.run.end_behavior == "force_close":
            self._close_position(
                self.last_price,
                self.last_point_time or self.run.end_time,
                "scalper_manual_close",
                "Position force-closed at end of backtest",
            )
            values = self._snapshot(self.last_price)
            self._update_market_metrics(values, self.last_point_time or self.run.end_time)

        final_time = self.run.end_time
        final_values = self._snapshot(self.last_price)
        if self.state["current_cycle"]:
            # Keep-open cycles remain visible in the same cycles table.
            from app.services.backtest_service import _store_cycle
            cycle_state = self.state["current_cycle"]
            cycle_state["last_time"] = final_time
            _store_cycle(self.db, self.run.id, cycle_state)
            self.state["current_cycle"] = None

        if self.state["drawdown_started"] is not None:
            self.state["longest_drawdown"] = max(
                self.state["longest_drawdown"], (final_time - self.state["drawdown_started"]) / 1000.0
            )
        if self.state["loss_started"] is not None:
            self.state["longest_loss"] = max(
                self.state["longest_loss"], (final_time - self.state["loss_started"]) / 1000.0
            )
        if self.state["deepest_drawdown_time"] is not None and not self.state["max_drawdown_recovered"]:
            self.state["current_drawdown_recovery"] = max(
                (final_time - self.state["deepest_drawdown_time"]) / 1000.0, 0.0
            )

        for point in self.pending_points:
            self.db.add(point)
        self.pending_points.clear()
        self.db.flush()
        self.run.metrics = self._finalize_metrics(final_values)
        self.run.status = "completed"
        self.run.progress = 100.0
        self.run.completed_at = _utcnow()
        if self.temp_bot is not None:
            self.temp_bot.runtime_status = "stopped"
            self.temp_bot.stopped_at = _utcnow()
            self.db.add(self.temp_bot)
        self.db.add(self.run)
        self.db.commit()
        backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "completed", force=True)


def run_fast_scalper_backtest_job(run_id: int) -> None:
    db = SessionLocal()
    engine: FastScalperBacktestEngine | None = None
    try:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status != "queued":
            return
        engine = FastScalperBacktestEngine(db, run)
        engine.run_all()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(BacktestRun, run_id)
        if run is not None and run.status != "cancelled":
            run.status = "failed"
            run.error = str(exc)
            run.completed_at = _utcnow()
            if run.temp_bot_id:
                failed_bot = db.get(TradingBot, run.temp_bot_id)
                if failed_bot is not None:
                    failed_bot.runtime_status = "stopped"
                    failed_bot.stopped_at = _utcnow()
                    db.add(failed_bot)
            db.add(run)
            db.commit()
            backtest_ui_stream_hub.publish(run.user_id, run.id, "failed", force=True)
    finally:
        if engine is not None:
            engine.close()
        db.close()
