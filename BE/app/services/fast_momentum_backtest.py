"""Fast in-memory historical backtest engine for the Momentum strategy."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import statistics
from typing import Any

from sqlalchemy.orm import Session

from app.bot_engine.orders import InstrumentRules, round_to_step
from app.bot_engine.strategies.momentum import (
    Candle,
    MomentumSignal,
    _float,
    _market_regime,
    _settings,
    _short_trading_supported,
    _signal,
    _signal_payload,
    _trade_levels,
)
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.backtest import BacktestCycle, BacktestPoint, BacktestRun
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.services.backtest_emulator import BacktestEmulatorClient, emulator_instrument_rules, emulator_venue
from app.services.ui_stream_service import backtest_ui_stream_hub
from app.bot_engine.exchange.redaction import scrub

MAX_CHART_POINTS = 1800
EPSILON = 1e-12


@dataclass(slots=True)
class FastPosition:
    side: str
    qty: float
    avg_entry: float
    opened_at_ms: int
    stop_loss: float
    take_profit: float
    trailing_stop: float
    highest_price: float
    lowest_price: float
    signal: MomentumSignal
    entry_order_id: str


class MomentumSignalCache:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings
        self.lookback = max(int(settings["lookback_candles"]), 1)
        self.candles: deque[Candle] = deque(maxlen=self.lookback)

    def append(self, candle: Candle) -> None:
        self.candles.append(candle)

    def signal(self) -> MomentumSignal:
        return _signal(list(self.candles), self.settings)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _path_for_candle(candle: Candle, path_mode: str) -> list[float]:
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


class FastMomentumBacktestEngine:
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
        self.last_point_time: int | None = None
        self.last_price: float = 0.0
        self.signal_cache: MomentumSignalCache | None = None
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
        bot_settings.pop("momentum_state", None)
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
            strategy_type="momentum",
            category=str(snapshot.get("category") or "linear"),
            symbol=self.run.symbol,
            order_qty=_float(snapshot.get("order_qty"), 0.001),
            grid_orders_count=int(snapshot.get("grid_orders_count") or 1),
            grid_step_percent=_float(snapshot.get("grid_step_percent"), 0.0),
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
        self.signal_cache = MomentumSignalCache(self.settings)
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

    def _leverage(self) -> float:
        return max(_float(self.settings.get("assumed_leverage"), 1.0), 1.0)

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
            margin_used = position_value / self._leverage()
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

    def _next_order_id(self, role: str) -> str:
        self.order_sequence += 1
        return f"fast-momentum-{self.run.id}-{self.order_sequence}-{role}"

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
                "reduceOnly": role not in {"momentum_entry_long", "momentum_entry_short"},
                "strategy": strategy_payload,
                "execution": execution,
                "backtest_engine": "fast_momentum",
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
            "execId": f"fast-momentum-exec-{self.run.id}-{self.exec_sequence}",
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
        signal: MomentumSignal | None = None,
        exit_reason: str | None = None,
        exit_role: str | None = None,
    ) -> None:
        from app.services.backtest_service import _apply_execution_to_cycle_state, _store_cycle

        cycle = _apply_execution_to_cycle_state(self.state, execution)
        if signal is not None and self.state.get("current_cycle") is not None:
            self.state["current_cycle"]["details"] = {
                "signal_score": signal.score,
                "signal_reasons": signal.reasons,
                "signal_type": signal.type,
                "indicators": signal.indicators,
                "signal_candle_time": signal.timestamp,
                "market_regime": signal.market_regime,
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

    def _entry_quantity(self, current_price: float, stop_loss: float) -> float:
        assert self.temp_bot is not None
        assert self.rules is not None
        maximum = min(float(self.temp_bot.order_qty), _float(self.settings.get("max_position_qty"), self.temp_bot.order_qty))
        qty = maximum
        if str(self.settings.get("position_sizing") or "risk_capped") == "risk_capped":
            distance = abs(current_price - stop_loss)
            fee_per_unit = current_price * self.run.fee_rate * 2.0
            risk_per_unit = distance + fee_per_unit
            if self.balance > 0 and risk_per_unit > 0:
                budget = self.balance * _float(self.settings.get("risk_per_trade_percent"), 1.0) / 100.0
                qty = min(maximum, budget / risk_per_unit)
        return round_to_step(qty, self.rules.qty_step)

    def _entry_block_reason(self, qty: float, price: float) -> str | None:
        assert self.rules is not None
        if qty <= 0:
            return "Calculated order quantity is zero"
        maximum = _float(self.settings.get("max_position_qty"), qty)
        if maximum > 0 and qty > maximum + EPSILON:
            return "Max position quantity exceeded"
        max_notional = self.settings.get("max_notional_usdt")
        if max_notional is not None and qty * price > _float(max_notional):
            return "Max notional exposure exceeded"
        if qty < self.rules.min_order_qty:
            return f"Order qty {qty:g} is below Bybit minimum {self.rules.min_order_qty:g}"
        minimum_notional = max(self.rules.min_notional_value, 5.0)
        if minimum_notional > 0 and qty * price + EPSILON < minimum_notional:
            return f"Order notional {qty * price:.8f} is below minimum {minimum_notional:g}"
        required_margin = qty * price / self._leverage()
        available = self._snapshot(price)["available"]
        if required_margin > available + EPSILON:
            return "Insufficient available balance"
        return None

    def _cooldown_active(self, timestamp_ms: int) -> bool:
        if self.last_exit_at_ms is None:
            return False
        cooldown_ms = _float(self.settings.get("cooldown_minutes"), 0.0) * 60_000.0
        return timestamp_ms - self.last_exit_at_ms < cooldown_ms

    def _open_position(self, signal: MomentumSignal, raw_price: float, timestamp_ms: int) -> None:
        assert self.rules is not None
        fill_side = signal.side or "Buy"
        fill_price = _slipped_price(raw_price, fill_side, self.run.slippage_percent)
        stop_loss, take_profit = _trade_levels(signal, self.settings, fill_price)
        qty = self._entry_quantity(fill_price, stop_loss)
        self.last_signal_candle = signal.timestamp
        blocked = self._entry_block_reason(qty, fill_price)
        if blocked:
            self._event("risk_blocked", blocked, timestamp_ms, {
                "side": fill_side,
                "score": signal.score,
                "qty": qty,
                "price": raw_price,
            })
            return
        fee = fill_price * qty * self.run.fee_rate
        order_id = self._next_order_id("entry")
        execution = self._execution(
            order_id=order_id,
            side=fill_side,
            qty=qty,
            price=fill_price,
            fee=fee,
            closed_pnl=0.0,
            timestamp_ms=timestamp_ms,
        )
        self.balance -= fee
        self.executions.append(execution)
        self.position = FastPosition(
            side=fill_side,
            qty=qty,
            avg_entry=fill_price,
            opened_at_ms=timestamp_ms,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=stop_loss,
            highest_price=fill_price,
            lowest_price=fill_price,
            signal=signal,
            entry_order_id=order_id,
        )
        payload = {
            **_signal_payload(signal),
            "entry_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        }
        self.db.add(self._new_order(
            side=fill_side,
            role="momentum_entry_long" if fill_side == "Buy" else "momentum_entry_short",
            qty=qty,
            fill_price=fill_price,
            timestamp_ms=timestamp_ms,
            execution=execution,
            strategy_payload=payload,
        ))
        self._apply_execution_accounting(execution, signal=signal)
        self._event("momentum_signal_entered", f"Momentum bot opened a {signal.type.upper()} position", timestamp_ms, {
            "side": fill_side,
            "score": signal.score,
            "market_regime": signal.market_regime,
            "reasons": signal.reasons,
            "entry_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "qty": qty,
            "order_id": order_id,
        })

    def _update_trailing(self, raw_price: float, atr: float) -> None:
        if self.position is None or not bool(self.settings.get("trailing_stop_enabled", True)) or atr <= 0:
            return
        trailing_distance = atr * _float(self.settings.get("trailing_stop_atr_multiplier"), 2.0)
        if self.position.side == "Buy":
            self.position.highest_price = max(self.position.highest_price, raw_price)
            self.position.trailing_stop = max(self.position.trailing_stop, self.position.highest_price - trailing_distance)
        else:
            self.position.lowest_price = min(self.position.lowest_price, raw_price)
            self.position.trailing_stop = min(self.position.trailing_stop, self.position.lowest_price + trailing_distance)

    def _exit_trigger(self, raw_price: float, signal: MomentumSignal | None) -> tuple[str, str] | None:
        position = self.position
        if position is None:
            return None
        if position.side == "Buy" and raw_price <= position.stop_loss:
            return "momentum_stop_loss", "Momentum stop-loss reached"
        if position.side == "Buy" and raw_price >= position.take_profit:
            return "momentum_take_profit", "Momentum take-profit reached"
        if position.side == "Buy" and bool(self.settings.get("trailing_stop_enabled", True)) and raw_price <= position.trailing_stop:
            return "momentum_trailing_stop", "Momentum trailing stop reached"
        if position.side == "Sell" and raw_price >= position.stop_loss:
            return "momentum_stop_loss", "Momentum stop-loss reached"
        if position.side == "Sell" and raw_price <= position.take_profit:
            return "momentum_take_profit", "Momentum take-profit reached"
        if position.side == "Sell" and bool(self.settings.get("trailing_stop_enabled", True)) and raw_price >= position.trailing_stop:
            return "momentum_trailing_stop", "Momentum trailing stop reached"
        if signal is not None:
            minimum_score = _float(self.settings.get("minimum_signal_score"), 70.0)
            if position.side == "Buy" and signal.type == "short" and signal.score >= minimum_score:
                return "momentum_signal_exit", "Strong opposite momentum signal detected"
            if position.side == "Sell" and signal.type == "long" and signal.score >= minimum_score:
                return "momentum_signal_exit", "Strong opposite momentum signal detected"
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
        payload = {
            "exit_reason": reason,
            "entry_order_id": position.entry_order_id,
            "entry_price": position.avg_entry,
            "exit_market_price": raw_price,
            "fill_price": fill_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "trailing_stop": position.trailing_stop,
            "signal": _signal_payload(position.signal),
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
        self._event("momentum_exit_filled", reason, timestamp_ms, {
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
            state["longest_drawdown"] = max(state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000.0)
        if state["loss_started"] is not None:
            state["longest_loss"] = max(state["longest_loss"], (point_time - state["loss_started"]) / 1000.0)

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
            state["longest_drawdown"] = max(state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000.0)
            if state["deepest_drawdown_time"] is not None and not state["max_drawdown_recovered"]:
                state["max_drawdown_recovery"] = max((point_time - state["deepest_drawdown_time"]) / 1000.0, 0.0)
                state["max_drawdown_recovered"] = True
            state["drawdown_started"] = None

        negative_open = values["position_qty"] > 0 and values["unrealized"] < 0
        if negative_open and state["loss_started"] is None:
            state["loss_started"] = point_time
        elif not negative_open and state["loss_started"] is not None:
            state["longest_loss"] = max(state["longest_loss"], (point_time - state["loss_started"]) / 1000.0)
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
        if self.run.status == "paused" and not self.run.pause_requested:
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
            "backtest_engine": "fast_momentum",
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
        losing = [cycle for cycle in closed if cycle.net_pnl < 0]
        durations = [cycle.duration_seconds for cycle in closed]
        period_seconds = max((self.run.end_time - self.run.start_time) / 1000.0, 1.0)
        profits = sum(cycle.net_pnl for cycle in winning)
        losses = abs(sum(cycle.net_pnl for cycle in losing))
        profit_factor = profits / losses if losses > 0 else (float("inf") if profits > 0 else 0.0)
        latest_signal = self.signal_cache.signal() if self.signal_cache is not None else None

        def exit_pnl(role: str) -> float:
            group = exit_groups[role] if role in exit_groups else exit_groups["other"]
            return sum(cycle.net_pnl for cycle in group)

        trade_returns = [
            (cycle.net_pnl / cycle.max_position_value * 100.0)
            for cycle in closed
            if cycle.max_position_value > 0
        ]
        sharpe_ratio = None
        if len(trade_returns) >= 2:
            mean_return = statistics.fmean(trade_returns)
            deviation = statistics.pstdev(trade_returns)
            sharpe_ratio = mean_return / deviation if deviation > 0 else None
        exit_groups: dict[str, list[BacktestCycle]] = {
            "momentum_take_profit": [],
            "momentum_stop_loss": [],
            "momentum_trailing_stop": [],
            "momentum_signal_exit": [],
            "other": [],
        }
        for cycle in closed:
            role = str((cycle.details or {}).get("exit_role") or "")
            exit_groups[role if role in exit_groups else "other"].append(cycle)
        trades = [
            {
                "cycle_number": cycle.cycle_number,
                "side": (cycle.details or {}).get("side"),
                "signal_type": (cycle.details or {}).get("signal_type"),
                "signal_score": (cycle.details or {}).get("signal_score"),
                "entry_price": cycle.avg_entry_price,
                "exit_price": cycle.exit_price,
                "net_pnl": cycle.net_pnl,
                "gross_pnl": cycle.gross_pnl,
                "fees": cycle.fees,
                "return_percent": (cycle.net_pnl / cycle.max_position_value * 100.0) if cycle.max_position_value else None,
                "exit_role": (cycle.details or {}).get("exit_role"),
                "exit_reason": (cycle.details or {}).get("exit_reason"),
                "opened_at": cycle.started_at_ms,
                "closed_at": cycle.closed_at_ms,
            }
            for cycle in cycles
        ]
        return {
            "backtest_engine": "fast_momentum",
            "initial_balance": self.run.initial_balance,
            "final_balance": self.balance,
            "final_equity": final_values["equity"],
            "gross_realized_pnl": sum(_float(item.get("closedPnl")) for item in self.executions),
            "net_realized_pnl": self.balance - self.run.initial_balance,
            "unrealized_pnl": final_values["unrealized"],
            "net_total_pnl": final_values["equity"] - self.run.initial_balance,
            "return_percent": (final_values["equity"] - self.run.initial_balance) / self.run.initial_balance * 100.0,
            "total_fees": sum(_float(item.get("execFee")) for item in self.executions),
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
            "time_in_position_seconds": self.state["time_in_position"],
            "time_in_position_percent": self.state["time_in_position"] / period_seconds * 100.0,
            "time_in_loss_seconds": self.state["time_in_loss"],
            "time_in_loss_percent": self.state["time_in_loss"] / period_seconds * 100.0,
            "longest_negative_pnl_period_seconds": self.state["longest_loss"],
            "maximum_unrealized_loss": self.state["max_unrealized_loss"],
            "maximum_drawdown_percent": self.state["max_drawdown"],
            "longest_drawdown_seconds": self.state["longest_drawdown"],
            "maximum_drawdown_recovery_seconds": self.state["max_drawdown_recovery"],
            "maximum_drawdown_recovered": self.state["max_drawdown_recovered"],
            "current_drawdown_recovery_seconds": self.state["current_drawdown_recovery"],
            "current_drawdown_percent": self.state["current_drawdown"],
            "maximum_position_qty": self.state["max_position_qty"],
            "maximum_position_value": self.state["max_position_value"],
            "maximum_used_margin": self.state["max_margin_used"],
            "lowest_available_balance": self.state["lowest_available"],
            "maximum_grid_levels_filled": 1 if cycles else 0,
            "open_position_at_end": final_values["position_qty"] > 0,
            "open_position_qty": final_values["position_qty"],
            "open_position_value": final_values["position_value"],
            "open_avg_entry_price": final_values["avg_entry"] or None,
            "open_orders_at_end": 0,
            "executions_count": len(self.executions),
            "long_trades": len([cycle for cycle in closed if str((cycle.details or {}).get("side") or "").lower() == "buy"]),
            "short_trades": len([cycle for cycle in closed if str((cycle.details or {}).get("side") or "").lower() == "sell"]),
            "take_profit_cycles": len(exit_groups["momentum_take_profit"]),
            "take_profit_net_pnl": exit_pnl("momentum_take_profit"),
            "stop_loss_cycles": len(exit_groups["momentum_stop_loss"]),
            "stop_loss_net_pnl": exit_pnl("momentum_stop_loss"),
            "trailing_stop_cycles": len(exit_groups["momentum_trailing_stop"]),
            "trailing_stop_net_pnl": exit_pnl("momentum_trailing_stop"),
            "signal_exit_cycles": len(exit_groups["momentum_signal_exit"]),
            "signal_exit_net_pnl": exit_pnl("momentum_signal_exit"),
            "other_exit_cycles": len(exit_groups["other"]),
            "other_exit_net_pnl": exit_pnl("other"),
            "average_fee_per_cycle": (sum(cycle.fees for cycle in closed) / len(closed)) if closed else 0.0,
            "profit_factor": profit_factor,
            "average_trade_percent": statistics.fmean(trade_returns) if trade_returns else 0.0,
            "sharpe_ratio": sharpe_ratio,
            "total_trades": len(closed),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "trades": trades,
            "candles_processed": self.run.processed_candles,
            "market_regime": _market_regime(
                _float((latest_signal.indicators if latest_signal else {}).get("fastEma")),
                _float((latest_signal.indicators if latest_signal else {}).get("slowEma")),
                _float((latest_signal.indicators if latest_signal else {}).get("atr")),
            ) if latest_signal is not None else "sideways",
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
        configuration["backtest_engine"] = "fast_momentum"
        configuration["engine_version"] = 1
        self.run.configuration = configuration
        self.db.add(self.run)
        self.db.commit()
        backtest_ui_stream_hub.publish(self.run.user_id, self.run.id, "running", force=True)

        self._make_temp_bot()
        assert self.temp_bot is not None
        self.rules = emulator_instrument_rules(
            self.exchange, category=self.temp_bot.category, symbol=self.run.symbol,
            missing="Instrument rules were not found for the selected symbol",
        )
        candles = self._load_candles()
        if len(candles) != self.run.total_candles:
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
                if self.position is not None:
                    atr = _float(cached_signal.indicators.get("atr")) if cached_signal else _float(self.position.signal.indicators.get("atr"))
                    if atr <= 0:
                        atr = _float(self.position.signal.indicators.get("atr"))
                    self._update_trailing(raw_price, atr)
                    trigger = self._exit_trigger(raw_price, cached_signal)
                    if trigger is not None:
                        self._close_position(raw_price, point_time, trigger[0], trigger[1])
                elif (
                    cached_signal.type != "none"
                    and cached_signal.timestamp != self.last_signal_candle
                    and not self._cooldown_active(point_time)
                    and (cached_signal.type != "short" or _short_trading_supported(self.temp_bot, self.settings))
                ):
                    self._event("momentum_signal_generated", f"Momentum {cached_signal.type.upper()} signal scored {cached_signal.score:.0f}/100", point_time, _signal_payload(cached_signal))
                    self._open_position(cached_signal, raw_price, point_time)
                values = self._snapshot(raw_price)
                self._update_market_metrics(values, point_time)
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
                "momentum_manual_close",
                "Position force-closed at end of backtest",
            )
            values = self._snapshot(self.last_price)
            self._update_market_metrics(values, self.last_point_time or self.run.end_time)

        final_time = self.run.end_time
        final_values = self._snapshot(self.last_price)
        if self.state["current_cycle"]:
            from app.services.backtest_service import _store_cycle
            cycle_state = self.state["current_cycle"]
            cycle_state["last_time"] = final_time
            _store_cycle(self.db, self.run.id, cycle_state)
            self.state["current_cycle"] = None
        if self.state["drawdown_started"] is not None:
            self.state["longest_drawdown"] = max(self.state["longest_drawdown"], (final_time - self.state["drawdown_started"]) / 1000.0)
        if self.state["loss_started"] is not None:
            self.state["longest_loss"] = max(self.state["longest_loss"], (final_time - self.state["loss_started"]) / 1000.0)
        if self.state["deepest_drawdown_time"] is not None and not self.state["max_drawdown_recovered"]:
            self.state["current_drawdown_recovery"] = max((final_time - self.state["deepest_drawdown_time"]) / 1000.0, 0.0)
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


def run_fast_momentum_backtest_job(run_id: int) -> None:
    db = SessionLocal()
    engine: FastMomentumBacktestEngine | None = None
    try:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status != "queued":
            return
        engine = FastMomentumBacktestEngine(db, run)
        engine.run_all()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        run = db.get(BacktestRun, run_id)
        if run is not None and run.status != "cancelled":
            run.status = "failed"
            run.error = scrub(str(exc))
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



