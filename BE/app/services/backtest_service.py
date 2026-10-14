"""Emulator-driven backtest orchestration for existing trading bots."""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session, object_session

from app.bot_engine.bot import tick_bot_once
from app.bot_engine.strategies import get_strategy
from app.core.clock import use_simulated_time
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.ui_stream_service import backtest_ui_stream_hub
from app.models.backtest import BacktestCycle, BacktestPoint, BacktestRun
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.schemas.backtest import BacktestCreate
from app.services.backtest_emulator import BacktestEmulatorClient, emulator_instrument_rules, emulator_venue

ACTIVE_STATUSES = {"queued", "running", "paused"}
FINAL_STATUSES = {"completed", "failed", "cancelled"}
MAX_CHART_POINTS = 1800
# Strategies that only need a tick on the first point and after an order fill.
FILL_DRIVEN_STRATEGY_TYPES = {"grid", "dca"}
FAST_SIGNAL_STRATEGY_TYPES = {"pattern_scalper", "momentum"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _interval_seconds(interval: str) -> int:
    mapping = {
        "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
        "60": 3600, "120": 7200, "240": 14400, "360": 21600,
        "720": 43200, "D": 86400, "W": 604800, "M": 2592000,
    }
    return mapping.get(str(interval), 60)




def _ceil_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    units = (value_decimal / step_decimal).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return float(units * step_decimal)


def _validate_initial_grid_quantity(
    bot: TradingBot,
    *,
    first_price: float,
    rules,
) -> None:
    min_order_qty = rules.min_order_qty
    qty_step = rules.qty_step
    min_notional = rules.min_notional_value

    configured_qty = _float(bot.order_qty)
    if bot.strategy_type != "grid":
        required_for_notional = min_notional / first_price if min_notional > 0 and first_price > 0 else 0.0
        minimum_qty = _ceil_to_step(max(min_order_qty, required_for_notional), qty_step)
        if configured_qty + 1e-12 < minimum_qty:
            coin = bot.symbol.removesuffix("USDT")
            raise ValueError(
                f"Order quantity is too small for this backtest. Configured: {configured_qty:g} {coin}. "
                f"Minimum at the starting price is {minimum_qty:g} {coin}."
            )
        return

    levels = max(int(bot.grid_orders_count or 1), 1)
    step_percent = max(_float(bot.grid_step_percent), 0.0)
    lowest_multiplier = 1.0 - ((levels - 1) * step_percent / 100.0)
    lowest_grid_price = first_price * lowest_multiplier
    if lowest_grid_price <= 0:
        raise ValueError("Grid configuration produces a non-positive order price")

    required_for_notional = min_notional / lowest_grid_price if min_notional > 0 else 0.0
    minimum_qty = _ceil_to_step(max(min_order_qty, required_for_notional), qty_step)

    if configured_qty + 1e-12 < minimum_qty:
        coin = bot.symbol.removesuffix("USDT")
        raise ValueError(
            f"Order quantity is too small for this backtest. "
            f"Configured: {configured_qty:g} {coin}. "
            f"Minimum for all {levels} grid levels at the starting price is "
            f"{minimum_qty:g} {coin} (Bybit minimum notional: {min_notional:g} USDT). "
            f"Increase the bot Order quantity to at least {minimum_qty:g} {coin} and try again."
        )


def _bot_snapshot(bot: TradingBot) -> dict[str, Any]:
    return {
        "id": bot.id,
        "name": bot.name,
        "exchange": bot.exchange,
        "environment": bot.environment,
        "strategy_type": bot.strategy_type,
        "category": bot.category,
        "symbol": bot.symbol,
        "order_qty": bot.order_qty,
        "grid_orders_count": bot.grid_orders_count,
        "grid_step_percent": bot.grid_step_percent,
        "is_active": bot.is_active,
        "settings": dict(get_strategy(bot.strategy_type).get_effective_settings(bot)),
    }


def list_backtests(db: Session, user_id: int, limit: int = 100) -> list[BacktestRun]:
    return (
        db.query(BacktestRun)
        .filter(BacktestRun.user_id == user_id)
        .order_by(desc(BacktestRun.created_at), desc(BacktestRun.id))
        .limit(limit)
        .all()
    )


def get_backtest_by_id(db: Session, run_id: int, user_id: int) -> BacktestRun | None:
    return db.query(BacktestRun).filter(BacktestRun.id == run_id, BacktestRun.user_id == user_id).first()


def create_backtest(db: Session, payload: BacktestCreate, user_id: int) -> BacktestRun:
    if payload.end_time <= payload.start_time:
        raise ValueError("Backtest end time must be greater than start time")
    bot = (
        db.query(TradingBot)
        .filter(
            TradingBot.id == payload.bot_id,
            TradingBot.user_id == user_id,
            TradingBot.is_backtest.is_(False),
        )
        .first()
    )
    if bot is None:
        raise ValueError("Trading bot not found")
    get_strategy(bot.strategy_type)
    if bot.strategy_type == "pattern_scalper" and bot.category != "linear":
        raise ValueError("Pattern Scalper currently supports linear perpetuals only")
    if bot.strategy_type == "momentum" and bot.category == "inverse":
        raise ValueError("Momentum currently supports linear and spot markets only")

    settings = get_settings()
    emulator = BacktestEmulatorClient(settings.exchange_emulator_url)
    try:
        dataset = emulator.dataset(payload.dataset_id)
        if str(dataset.get("symbol") or "").upper() != bot.symbol.upper():
            raise ValueError(
                f"Dataset {dataset.get('name') or payload.dataset_id} belongs to {dataset.get('symbol')}, "
                f"but the selected bot trades {bot.symbol}"
            )
        if str(dataset.get("category") or "") != bot.category:
            raise ValueError("Dataset market category does not match the selected bot")
        if bot.strategy_type in FAST_SIGNAL_STRATEGY_TYPES and not bool((dataset.get("quality") or {}).get("has_volume")):
            raise ValueError(f"{bot.strategy_type.replace('_', ' ').title()} requires a dataset with non-zero volume data")

        range_stats = emulator.range_stats(
            dataset_id=payload.dataset_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
        )
        candle_count = int(range_stats.get("count") or 0)
        if candle_count <= 0:
            raise ValueError("No candles exist in the selected dataset and period")
        if bot.strategy_type in FAST_SIGNAL_STRATEGY_TYPES and not bool(range_stats.get("has_volume")):
            raise ValueError(f"{bot.strategy_type.replace('_', ' ').title()} requires non-zero volume inside the selected period")
        if not range_stats.get("coverage_complete"):
            raise ValueError("The selected period is outside the dataset coverage")
        missing = int(range_stats.get("missing_candles") or 0)
        if missing > 0:
            first_gap = (range_stats.get("gaps") or [{}])[0]
            raise ValueError(
                f"The selected dataset range contains {missing} missing candles. "
                f"First gap: {first_gap.get('after', 'unknown')} → {first_gap.get('before', 'unknown')}. "
                "Download the period again or choose a complete range."
            )

        first_candle = emulator.first_candle(
            dataset_id=payload.dataset_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
        )
        if first_candle is None:
            raise ValueError("No candles exist in the selected dataset and period")

        rules = emulator_instrument_rules(
            emulator_venue(), category=bot.category, symbol=bot.symbol,
            missing=f"Instrument rules were not found for {bot.symbol}",
        )
        _validate_initial_grid_quantity(
            bot,
            first_price=_float(first_candle.get("open")),
            rules=rules,
        )
    finally:
        emulator.close()

    interval = str(dataset["interval"])
    snapshot = _bot_snapshot(bot)
    name = payload.name or f"{bot.name} · {dataset.get('name') or 'dataset'}"
    run = BacktestRun(
        user_id=user_id,
        source_bot_id=bot.id,
        dataset_id=payload.dataset_id,
        dataset_name=str(dataset.get("name") or f"Dataset #{payload.dataset_id}"),
        name=name,
        bot_name=bot.name,
        symbol=bot.symbol,
        interval=interval,
        start_time=payload.start_time,
        end_time=payload.end_time,
        initial_balance=payload.initial_balance,
        fee_rate=payload.fee_rate,
        slippage_percent=payload.slippage_percent,
        path_mode=payload.path_mode,
        end_behavior=payload.end_behavior,
        status="queued",
        total_candles=candle_count,
        bot_snapshot=snapshot,
        configuration={
            "dataset": dataset,
            "range_quality": range_stats,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "initial_balance": payload.initial_balance,
            "fee_rate": payload.fee_rate,
            "slippage_percent": payload.slippage_percent,
            "path_mode": payload.path_mode,
            "end_behavior": payload.end_behavior,
            "application_version": "1.5.0",
            "backtest_engine": (
                "fast_scalper" if bot.strategy_type == "pattern_scalper"
                else "fast_momentum" if bot.strategy_type == "momentum"
                else "emulator"
            ),
        },
        metrics={},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def request_pause(db: Session, run: BacktestRun) -> BacktestRun:
    if run.status not in {"queued", "running"}:
        raise ValueError("Only a queued or running backtest can be paused")
    run.pause_requested = True
    db.commit()
    db.refresh(run)
    return run


def request_resume(db: Session, run: BacktestRun) -> BacktestRun:
    if run.status not in {"paused", "running"}:
        raise ValueError("Only a paused backtest can be resumed")
    run.pause_requested = False
    if run.status == "paused":
        run.status = "running"
    db.commit()
    db.refresh(run)
    return run


def request_cancel(db: Session, run: BacktestRun) -> BacktestRun:
    if run.status in FINAL_STATUSES:
        return run
    run.cancel_requested = True
    db.commit()
    db.refresh(run)
    return run


def delete_backtest(db: Session, run: BacktestRun) -> None:
    if run.status in {"queued", "running", "paused"}:
        raise ValueError("Cancel the running backtest before deleting it")
    account_id = run.emulator_account_id
    # Delete the hidden bot explicitly as well as relying on the database FK.
    # This keeps SQLite development databases clean even when FK enforcement is off.
    if run.temp_bot_id:
        temp_bot = db.get(TradingBot, run.temp_bot_id)
        if temp_bot is not None:
            db.delete(temp_bot)
            db.flush()
    db.delete(run)
    db.commit()
    if account_id:
        emulator = BacktestEmulatorClient(get_settings().exchange_emulator_url)
        try:
            emulator.delete_account(account_id)
        except Exception:
            pass
        finally:
            emulator.close()


def list_points(db: Session, run_id: int) -> list[BacktestPoint]:
    return db.query(BacktestPoint).filter(BacktestPoint.run_id == run_id).order_by(asc(BacktestPoint.timestamp)).all()


def list_cycles(db: Session, run_id: int) -> list[BacktestCycle]:
    return db.query(BacktestCycle).filter(BacktestCycle.run_id == run_id).order_by(BacktestCycle.cycle_number).all()


def list_orders(db: Session, run: BacktestRun) -> list[TradingBotOrder]:
    if not run.temp_bot_id:
        return []
    return (
        db.query(TradingBotOrder)
        .filter(TradingBotOrder.bot_id == run.temp_bot_id)
        .order_by(asc(TradingBotOrder.created_at), asc(TradingBotOrder.id))
        .all()
    )


def list_events(db: Session, run: BacktestRun, limit: int = 5000) -> list[TradingBotEvent]:
    if not run.temp_bot_id:
        return []
    return (
        db.query(TradingBotEvent)
        .filter(TradingBotEvent.bot_id == run.temp_bot_id)
        .order_by(asc(TradingBotEvent.created_at), asc(TradingBotEvent.id))
        .limit(limit)
        .all()
    )


def list_executions(run: BacktestRun) -> list[dict]:
    if not run.emulator_account_id:
        # Fast Pattern Scalper backtests persist their synthetic executions on the
        # filled TradingBotOrder rows so the existing results UI keeps the same
        # execution contract without needing a full emulator account.
        if not run.temp_bot_id:
            return []
        db = object_session(run)
        owns_session = db is None
        if db is None:
            db = SessionLocal()
        try:
            orders = (
                db.query(TradingBotOrder)
                .filter(TradingBotOrder.bot_id == run.temp_bot_id)
                .order_by(asc(TradingBotOrder.created_at), asc(TradingBotOrder.id))
                .all()
            )
            executions = []
            for order in orders:
                execution = (order.raw_response or {}).get("execution")
                if isinstance(execution, dict):
                    executions.append(execution)
            return _sort_executions(executions)
        finally:
            if owns_session:
                db.close()
    emulator = BacktestEmulatorClient(get_settings().exchange_emulator_url)
    try:
        return _sort_executions(
            emulator.executions(run.emulator_account_id, run.symbol, limit=100000)
        )
    finally:
        emulator.close()


def list_datasets() -> list[dict]:
    emulator = BacktestEmulatorClient(get_settings().exchange_emulator_url)
    try:
        return emulator.datasets()
    finally:
        emulator.close()


def _path_for_candle(candle: dict, path_mode: str) -> list[float]:
    # High-before-low is the conservative ambiguous-candle path for the long grid strategy.
    # Pattern strategies still use only already closed candles for signal generation.
    if path_mode in {"conservative", "ohlc"}:
        return [candle["open"], candle["high"], candle["low"], candle["close"]]
    if path_mode == "olhc":
        return [candle["open"], candle["low"], candle["high"], candle["close"]]
    return [candle["close"]]


def _local_signature(db: Session, bot_id: int) -> tuple:
    orders = (
        db.query(TradingBotOrder.id, TradingBotOrder.status, TradingBotOrder.updated_at)
        .filter(TradingBotOrder.bot_id == bot_id)
        .order_by(TradingBotOrder.id)
        .all()
    )
    events_count = db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot_id).count()
    return tuple((row.id, row.status, str(row.updated_at)) for row in orders), events_count


def _tick_until_stable(db: Session, bot: TradingBot, max_ticks: int = 7) -> None:
    previous = None
    for _ in range(max_ticks):
        tick_bot_once(db, bot)
        db.refresh(bot)
        if bot.last_error:
            raise RuntimeError(bot.last_error)
        current = _local_signature(db, bot.id)
        if current == previous:
            return
        previous = current


def _assert_initial_grid_created(db: Session, bot: TradingBot) -> None:
    """Fail a backtest when its initial grid cannot create any order.

    Order validation in the shared live runtime is intentionally recoverable and is
    recorded as bot events. During a historical backtest, however, retrying the
    same invalid configuration on every candle produces thousands of duplicate
    errors and a misleading Completed result.
    """
    active_orders = (
        db.query(TradingBotOrder.id)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status.in_({"New", "PartiallyFilled", "Untriggered"}),
        )
        .count()
    )
    if active_orders > 0:
        return

    error_events = (
        db.query(TradingBotEvent)
        .filter(
            TradingBotEvent.bot_id == bot.id,
            TradingBotEvent.event_type == "error",
        )
        .order_by(asc(TradingBotEvent.id))
        .all()
    )
    if not error_events:
        return

    unique_messages: list[str] = []
    for event in error_events:
        message = str(event.message or "Backtest bot failed to create its initial grid")
        if message not in unique_messages:
            unique_messages.append(message)

    details = "; ".join(unique_messages[:5])
    if len(unique_messages) > 5:
        details += f"; and {len(unique_messages) - 5} more errors"
    raise RuntimeError(f"Initial grid creation failed: {details}")


def _dashboard_values(payload: dict) -> dict[str, float]:
    account = payload.get("account") or {}
    position = payload.get("position") or {}
    return {
        "balance": _float(account.get("balance")),
        "equity": _float(account.get("equity")),
        "available": _float(account.get("available_balance")),
        "unrealized": _float(account.get("unrealized_pnl")),
        "position_qty": _float(position.get("size")),
        "position_value": _float(position.get("positionValue")),
        "margin_used": _float(account.get("margin_used")),
        "avg_entry": _float(position.get("avgPrice")),
        "mark_price": _float(position.get("markPrice")),
    }


def _execution_time(item: dict) -> int:
    return int(item.get("execTime") or 0)


def _execution_sequence(item: dict) -> int:
    return int(item.get("execSeq") or 0)


def _sort_executions(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda item: (
            _execution_time(item),
            _execution_sequence(item),
            str(item.get("execId") or ""),
        ),
    )


def _new_cycle_state(state: dict, execution: dict) -> dict[str, Any]:
    state["cycle_number"] += 1
    exec_time = _execution_time(execution)
    return {
        "number": state["cycle_number"],
        "side": str(execution.get("side") or "Buy").title(),
        "started_at": exec_time,
        "last_time": exec_time,
        "time_in_loss": 0.0,
        "max_unrealized_loss": 0.0,
        "max_qty": 0.0,
        "max_value": 0.0,
        "entries_filled": 0,
        "avg_entry": 0.0,
        "gross_pnl": 0.0,
        "fees": 0.0,
        "execution_count": 0,
        "start_exec_seq": _execution_sequence(execution),
        "end_exec_seq": None,
        "closed_at": None,
        "exit_price": None,
    }


def _apply_execution_to_cycle_state(state: dict, execution: dict) -> dict[str, Any] | None:
    side = str(execution.get("side") or "").title()
    qty = _float(execution.get("execQty"))
    price = _float(execution.get("execPrice"))
    fee = _float(execution.get("execFee"))
    closed_pnl = _float(execution.get("closedPnl"))
    exec_time = _execution_time(execution)
    exec_seq = _execution_sequence(execution)

    if side not in {"Buy", "Sell"}:
        raise RuntimeError(f"Unsupported execution side: {execution.get('side')}")
    if qty <= 0:
        raise RuntimeError(f"Execution {execution.get('execId')} has a non-positive quantity")

    position_qty = _float(state.get("execution_position_qty"))
    avg_entry = _float(state.get("execution_avg_entry"))
    position_side = state.get("execution_position_side")

    if position_qty <= 1e-12:
        if state.get("current_cycle") is not None:
            raise RuntimeError("A new position started while the previous backtest cycle was still open")
        state["current_cycle"] = _new_cycle_state(state, execution)
        state["execution_position_side"] = side
        position_side = side

    cycle = state.get("current_cycle")
    if cycle is None:
        raise RuntimeError("Execution accounting has no active cycle")

    if side == position_side:
        new_qty = position_qty + qty
        new_avg = ((position_qty * avg_entry) + (qty * price)) / new_qty
        state["execution_position_qty"] = new_qty
        state["execution_avg_entry"] = new_avg
        cycle["entries_filled"] += 1
        cycle["fees"] += fee
        cycle["execution_count"] += 1
        cycle["last_time"] = exec_time
        cycle["end_exec_seq"] = exec_seq
        cycle["avg_entry"] = new_avg
        cycle["max_qty"] = max(cycle["max_qty"], new_qty)
        cycle["max_value"] = max(cycle["max_value"], new_qty * price)
        state["max_position_qty"] = max(state["max_position_qty"], new_qty)
        state["max_position_value"] = max(state["max_position_value"], new_qty * price)
        return None

    if position_qty <= 1e-12:
        raise RuntimeError("A closing execution was received without an open backtest cycle")
    if qty > position_qty + 1e-9:
        raise RuntimeError(f"Closing execution quantity {qty:g} exceeds reconstructed position {position_qty:g}")

    cycle["fees"] += fee
    cycle["gross_pnl"] += closed_pnl
    cycle["execution_count"] += 1
    cycle["last_time"] = exec_time
    cycle["end_exec_seq"] = exec_seq
    cycle["exit_price"] = price
    new_qty = max(position_qty - qty, 0.0)
    state["execution_position_qty"] = new_qty
    if new_qty <= 1e-12:
        state["execution_position_qty"] = 0.0
        state["execution_avg_entry"] = 0.0
        state["execution_position_side"] = None
        cycle["closed_at"] = exec_time
        state["current_cycle"] = None
        state["closed_cycle_count"] += 1
        return cycle
    state["execution_avg_entry"] = avg_entry
    return None


def _update_current_cycle_market_state(state: dict, values: dict[str, float], point_time: int) -> None:
    cycle = state.get("current_cycle")
    if cycle is None:
        return
    cycle["last_time"] = point_time
    cycle["max_unrealized_loss"] = min(cycle["max_unrealized_loss"], values["unrealized"])
    cycle["max_qty"] = max(cycle["max_qty"], values["position_qty"])
    cycle["max_value"] = max(cycle["max_value"], values["position_value"])
    cycle["avg_entry"] = values["avg_entry"] or cycle["avg_entry"]


def _store_cycle(db: Session, run_id: int, state: dict) -> BacktestCycle:
    close_time = state.get("closed_at")
    duration = max(((close_time or state["last_time"]) - state["started_at"]) / 1000, 0)
    gross_pnl = _float(state.get("gross_pnl"))
    fees = _float(state.get("fees"))
    cycle = BacktestCycle(
        run_id=run_id,
        cycle_number=state["number"],
        started_at_ms=state["started_at"],
        closed_at_ms=close_time,
        duration_seconds=duration,
        time_in_loss_seconds=state["time_in_loss"],
        max_unrealized_loss=state["max_unrealized_loss"],
        max_position_qty=state["max_qty"],
        max_position_value=state["max_value"],
        entries_filled=state["entries_filled"],
        avg_entry_price=state.get("avg_entry"),
        exit_price=state.get("exit_price"),
        gross_pnl=gross_pnl,
        fees=fees,
        net_pnl=gross_pnl - fees,
        status="closed" if close_time is not None else "open",
        details={
            "start_exec_seq": state.get("start_exec_seq"),
            "end_exec_seq": state.get("end_exec_seq"),
            "execution_count": state.get("execution_count", 0),
            "side": state.get("side"),
            **dict(state.get("details") or {}),
        },
    )
    db.add(cycle)
    db.flush()
    return cycle


def _final_metrics(
    run: BacktestRun,
    dashboard: dict,
    executions: list[dict],
    state: dict,
    cycles: list[BacktestCycle],
) -> dict[str, Any]:
    values = _dashboard_values(dashboard)
    fees = sum(_float(item.get("execFee")) for item in executions)
    gross_realized = sum(_float(item.get("closedPnl")) for item in executions)
    net_realized = values["balance"] - run.initial_balance
    total_pnl = values["equity"] - run.initial_balance
    closed = [cycle for cycle in cycles if cycle.status == "closed"]
    winning = [cycle for cycle in closed if cycle.net_pnl > 0]
    durations = [cycle.duration_seconds for cycle in closed]
    period_seconds = max((run.end_time - run.start_time) / 1000, 1)
    return {
        "initial_balance": run.initial_balance,
        "final_balance": values["balance"],
        "final_equity": values["equity"],
        "gross_realized_pnl": gross_realized,
        "net_realized_pnl": net_realized,
        "unrealized_pnl": values["unrealized"],
        "net_total_pnl": total_pnl,
        "return_percent": (total_pnl / run.initial_balance) * 100,
        "total_fees": fees,
        "closed_cycles": len(closed),
        "winning_cycles": len(winning),
        "losing_cycles": len(closed) - len(winning),
        "win_rate_percent": (len(winning) / len(closed) * 100) if closed else 0,
        "average_cycle_pnl": (sum(c.net_pnl for c in closed) / len(closed)) if closed else 0,
        "best_cycle_pnl": max((c.net_pnl for c in closed), default=0),
        "worst_cycle_pnl": min((c.net_pnl for c in closed), default=0),
        "average_cycle_duration_seconds": (sum(durations) / len(durations)) if durations else 0,
        "median_cycle_duration_seconds": statistics.median(durations) if durations else 0,
        "longest_cycle_seconds": max(durations, default=0),
        "time_in_position_seconds": state["time_in_position"],
        "time_in_position_percent": state["time_in_position"] / period_seconds * 100,
        "time_in_loss_seconds": state["time_in_loss"],
        "time_in_loss_percent": state["time_in_loss"] / period_seconds * 100,
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
        "maximum_grid_levels_filled": max((cycle.entries_filled for cycle in cycles), default=0),
        "open_position_at_end": values["position_qty"] > 0,
        "open_position_qty": values["position_qty"],
        "open_position_value": values["position_value"],
        "open_avg_entry_price": values["avg_entry"] or None,
        "open_orders_at_end": int(dashboard.get("open_orders") or 0),
        "executions_count": len(executions),
        "candles_processed": run.processed_candles,
        "execution_duration_seconds": max(
            (
                _utcnow()
                - (run.started_at if run.started_at and run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc))
            ).total_seconds() if run.started_at else 0,
            0,
        ),
    }


def _run_emulator_backtest_job(run_id: int) -> None:
    db = SessionLocal()
    emulator = BacktestEmulatorClient(get_settings().exchange_emulator_url, timeout=60)
    run: BacktestRun | None = None
    temp_bot: TradingBot | None = None
    try:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status != "queued":
            return
        run.status = "running"
        run.started_at = _utcnow()
        run.error = None
        db.commit()
        backtest_ui_stream_hub.publish(run.user_id, run.id, "running", force=True)

        account = emulator.create_account(
            f"Backtest #{run.id} · {run.bot_name}",
            run.initial_balance,
            fee_rate=run.fee_rate,
            slippage_percent=run.slippage_percent,
        )
        run.emulator_account_id = int(account["id"])
        snapshot = dict(run.bot_snapshot)
        bot_settings = dict(snapshot.get("settings") or {})
        bot_settings.pop("pattern_scalper_state", None)
        bot_settings.pop("momentum_state", None)
        bot_settings.update({
            "emulator_api_key": account["api_key"],
            "run_interval_seconds": 0,
            "stop_bot_on_error": True,
            "timeframe": run.interval,
        })
        temp_bot = TradingBot(
            user_id=run.user_id,
            name=f"[Backtest #{run.id}] {run.bot_name}",
            exchange="bybit",
            environment="emulator",
            strategy_type=snapshot.get("strategy_type", "grid"),
            category=snapshot.get("category", "linear"),
            symbol=run.symbol,
            order_qty=_float(snapshot.get("order_qty"), 0.001),
            grid_orders_count=int(snapshot.get("grid_orders_count") or 2),
            grid_step_percent=_float(snapshot.get("grid_step_percent"), 5),
            is_active=True,
            runtime_status="running",
            settings=bot_settings,
            is_backtest=True,
            backtest_run_id=run.id,
            started_at=_utcnow(),
        )
        db.add(temp_bot)
        db.flush()
        run.temp_bot_id = temp_bot.id
        db.commit()

        interval_seconds = _interval_seconds(run.interval)
        sample_every = max(math.ceil(run.total_candles / MAX_CHART_POINTS), 1)
        state: dict[str, Any] = {
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
            "last_execution_sequence": 0,
            "processed_execution_ids": set(),
        }

        for index, candle in enumerate(
            emulator.candles(
                dataset_id=int(run.dataset_id or 0),
                start_time=run.start_time,
                end_time=run.end_time,
            ),
            start=1,
        ):
            db.expire(run)
            db.refresh(run)
            if run.cancel_requested:
                run.status = "cancelled"
                run.completed_at = _utcnow()
                temp_bot.runtime_status = "stopped"
                temp_bot.stopped_at = _utcnow()
                db.add_all([run, temp_bot])
                db.commit()
                backtest_ui_stream_hub.publish(run.user_id, run.id, "cancelled", force=True)
                return
            while run.pause_requested:
                if run.status != "paused":
                    run.status = "paused"
                    db.commit()
                    backtest_ui_stream_hub.publish(run.user_id, run.id, "paused", force=True)
                time.sleep(0.4)
                db.expire(run)
                db.refresh(run)
                if run.cancel_requested:
                    run.status = "cancelled"
                    run.completed_at = _utcnow()
                    temp_bot.runtime_status = "stopped"
                    temp_bot.stopped_at = _utcnow()
                    db.add_all([run, temp_bot])
                    db.commit()
                    backtest_ui_stream_hub.publish(run.user_id, run.id, "cancelled", force=True)
                    return
            if run.status == "paused":
                run.status = "running"
                db.commit()
                backtest_ui_stream_hub.publish(run.user_id, run.id, "running", force=True)

            path = _path_for_candle(candle, run.path_mode)
            segment_ms = max(int(interval_seconds * 1000 / max(len(path), 1)), 1)
            for point_index, price in enumerate(path):
                point_time = int(candle["open_time"]) + point_index * segment_ms

                previous_time = state["previous_time"]
                previous_values = state["previous_values"]
                if previous_time is not None and previous_values is not None:
                    elapsed = max((point_time - previous_time) / 1000, 0)
                    if previous_values["position_qty"] > 0:
                        state["time_in_position"] += elapsed
                        if previous_values["unrealized"] < 0:
                            state["time_in_loss"] += elapsed
                            if state["current_cycle"]:
                                state["current_cycle"]["time_in_loss"] += elapsed
                    if state["drawdown_started"] is not None:
                        state["longest_drawdown"] = max(
                            state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000
                        )
                    if state["loss_started"] is not None:
                        state["longest_loss"] = max(
                            state["longest_loss"], (point_time - state["loss_started"]) / 1000
                        )

                price_result = emulator.set_price(
                    run.emulator_account_id,
                    run.symbol,
                    _float(price),
                    point_time,
                    dataset_id=int(run.dataset_id or 0),
                )
                simulated_dt = datetime.fromtimestamp(point_time / 1000, tz=timezone.utc)
                # Grid and DCA only need to react to fills after initialization.
                # Pattern scalper evaluates each simulated point, while signals
                # still use closed candles only.
                ticked = (
                    temp_bot.strategy_type not in FILL_DRIVEN_STRATEGY_TYPES
                    or (index == 1 and point_index == 0)
                    or int(price_result.get("filled_orders") or 0) > 0
                )
                if ticked:
                    with use_simulated_time(simulated_dt):
                        _tick_until_stable(db, temp_bot, max_ticks=5)
                    if temp_bot.strategy_type in FILL_DRIVEN_STRATEGY_TYPES and index == 1 and point_index == 0:
                        _assert_initial_grid_created(db, temp_bot)

                    new_executions = _sort_executions(
                        emulator.executions(
                            run.emulator_account_id,
                            run.symbol,
                            limit=100000,
                            after_sequence=state["last_execution_sequence"],
                        )
                    )
                    for execution in new_executions:
                        exec_id = str(execution.get("execId") or "")
                        if exec_id in state["processed_execution_ids"]:
                            continue
                        closed_cycle = _apply_execution_to_cycle_state(state, execution)
                        state["processed_execution_ids"].add(exec_id)
                        state["last_execution_sequence"] = max(
                            state["last_execution_sequence"], _execution_sequence(execution)
                        )
                        if closed_cycle is not None:
                            _store_cycle(db, run.id, closed_cycle)
                dashboard = emulator.dashboard(run.emulator_account_id, run.symbol)
                values = _dashboard_values(dashboard)

                equity = values["equity"]
                state["peak_equity"] = max(state["peak_equity"], equity)
                drawdown = ((equity - state["peak_equity"]) / state["peak_equity"] * 100) if state["peak_equity"] else 0
                state["current_drawdown"] = drawdown
                if drawdown < state["max_drawdown"]:
                    state["max_drawdown"] = drawdown
                    state["deepest_drawdown_time"] = point_time
                    state["max_drawdown_recovered"] = False
                    state["max_drawdown_recovery"] = 0.0
                if drawdown < -1e-12 and state["drawdown_started"] is None:
                    state["drawdown_started"] = point_time
                elif drawdown >= -1e-12 and state["drawdown_started"] is not None:
                    state["longest_drawdown"] = max(
                        state["longest_drawdown"], (point_time - state["drawdown_started"]) / 1000
                    )
                    if state["deepest_drawdown_time"] is not None and not state["max_drawdown_recovered"]:
                        state["max_drawdown_recovery"] = max(
                            (point_time - state["deepest_drawdown_time"]) / 1000, 0
                        )
                        state["max_drawdown_recovered"] = True
                    state["drawdown_started"] = None

                negative_open = values["position_qty"] > 0 and values["unrealized"] < 0
                if negative_open and state["loss_started"] is None:
                    state["loss_started"] = point_time
                elif not negative_open and state["loss_started"] is not None:
                    state["longest_loss"] = max(
                        state["longest_loss"], (point_time - state["loss_started"]) / 1000
                    )
                    state["loss_started"] = None

                state["max_unrealized_loss"] = min(state["max_unrealized_loss"], values["unrealized"])
                state["max_position_qty"] = max(state["max_position_qty"], values["position_qty"])
                state["max_position_value"] = max(state["max_position_value"], values["position_value"])
                state["max_margin_used"] = max(state["max_margin_used"], values["margin_used"])
                state["lowest_available"] = min(state["lowest_available"], values["available"])

                reconstructed_qty = _float(state["execution_position_qty"])
                if not math.isclose(reconstructed_qty, values["position_qty"], rel_tol=0, abs_tol=1e-9):
                    raise RuntimeError(
                        "Execution accounting does not match emulator position: "
                        f"reconstructed={reconstructed_qty:g}, emulator={values['position_qty']:g}, "
                        f"time={point_time}"
                    )
                _update_current_cycle_market_state(state, values, point_time)

                state["previous_time"] = point_time
                state["previous_values"] = values

            run.processed_candles = index
            run.progress = min(index / max(run.total_candles, 1) * 100, 100)
            run.current_time = int(candle["open_time"])
            run.current_price = _float(candle["close"])
            if index == 1 or index % sample_every == 0 or index == run.total_candles:
                db.add(BacktestPoint(
                    run_id=run.id,
                    timestamp=int(candle["open_time"]),
                    open=_float(candle["open"]), high=_float(candle["high"]),
                    low=_float(candle["low"]), close=_float(candle["close"]),
                    balance=values["balance"], equity=values["equity"],
                    available_balance=values["available"], unrealized_pnl=values["unrealized"],
                    position_qty=values["position_qty"], position_value=values["position_value"],
                    drawdown_percent=drawdown,
                ))
            if index % 10 == 0 or index == run.total_candles:
                run.metrics = {
                    "equity": values["equity"],
                    "net_total_pnl": values["equity"] - run.initial_balance,
                    "maximum_drawdown_percent": state["max_drawdown"],
                    "time_in_position_seconds": state["time_in_position"],
                    "maximum_unrealized_loss": state["max_unrealized_loss"],
                }
                db.add(run)
                db.commit()
                backtest_ui_stream_hub.publish(run.user_id, run.id, "progress")

        final_time = run.end_time
        final_dashboard = emulator.dashboard(run.emulator_account_id, run.symbol)
        final_values = _dashboard_values(final_dashboard)
        if run.end_behavior == "force_close" and final_values["position_qty"] > 0:
            with use_simulated_time(datetime.fromtimestamp(final_time / 1000, tz=timezone.utc)):
                get_strategy(temp_bot.strategy_type).close_position(db, temp_bot)
                db.commit()
                _tick_until_stable(db, temp_bot)

            final_executions = _sort_executions(
                emulator.executions(
                    run.emulator_account_id,
                    run.symbol,
                    limit=100000,
                    after_sequence=state["last_execution_sequence"],
                )
            )
            for execution in final_executions:
                exec_id = str(execution.get("execId") or "")
                if exec_id in state["processed_execution_ids"]:
                    continue
                closed_cycle = _apply_execution_to_cycle_state(state, execution)
                state["processed_execution_ids"].add(exec_id)
                state["last_execution_sequence"] = max(
                    state["last_execution_sequence"], _execution_sequence(execution)
                )
                if closed_cycle is not None:
                    _store_cycle(db, run.id, closed_cycle)

            final_dashboard = emulator.dashboard(run.emulator_account_id, run.symbol)
            final_values = _dashboard_values(final_dashboard)

        if not math.isclose(
            _float(state["execution_position_qty"]),
            final_values["position_qty"],
            rel_tol=0,
            abs_tol=1e-9,
        ):
            raise RuntimeError(
                "Final execution accounting does not match emulator position: "
                f"reconstructed={state['execution_position_qty']:g}, "
                f"emulator={final_values['position_qty']:g}"
            )

        if state["current_cycle"]:
            _update_current_cycle_market_state(state, final_values, final_time)
            _store_cycle(db, run.id, state["current_cycle"])
            state["current_cycle"] = None

        if state["drawdown_started"] is not None:
            state["longest_drawdown"] = max(
                state["longest_drawdown"], (run.end_time - state["drawdown_started"]) / 1000
            )
        if state["loss_started"] is not None:
            state["longest_loss"] = max(
                state["longest_loss"], (run.end_time - state["loss_started"]) / 1000
            )
        if state["deepest_drawdown_time"] is not None and not state["max_drawdown_recovered"]:
            state["current_drawdown_recovery"] = max(
                (run.end_time - state["deepest_drawdown_time"]) / 1000, 0
            )

        executions = _sort_executions(
            emulator.executions(run.emulator_account_id, run.symbol, limit=100000)
        )
        if len(executions) != len(state["processed_execution_ids"]):
            raise RuntimeError(
                "Backtest execution stream was not processed completely: "
                f"stored={len(executions)}, processed={len(state['processed_execution_ids'])}"
            )
        cycles = list_cycles(db, run.id)
        closed_cycles = [cycle for cycle in cycles if cycle.status == "closed"]
        if len(closed_cycles) != state["closed_cycle_count"]:
            raise RuntimeError(
                "Stored cycle count does not match execution accounting: "
                f"stored={len(closed_cycles)}, reconstructed={state['closed_cycle_count']}"
            )

        run.metrics = _final_metrics(run, final_dashboard, executions, state, cycles)
        run.status = "completed"
        run.progress = 100
        run.completed_at = _utcnow()
        temp_bot.runtime_status = "stopped"
        temp_bot.stopped_at = _utcnow()
        db.add_all([run, temp_bot])
        db.commit()
        backtest_ui_stream_hub.publish(run.user_id, run.id, "completed", force=True)
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
        emulator.close()
        db.close()


def run_backtest_job(run_id: int) -> None:
    """Dispatch a backtest to the engine that matches the strategy mechanics."""
    db = SessionLocal()
    try:
        run = db.get(BacktestRun, run_id)
        if run is None or run.status != "queued":
            return
        strategy_type = str((run.bot_snapshot or {}).get("strategy_type") or "grid")
    finally:
        db.close()

    if strategy_type == "pattern_scalper":
        from app.services.fast_scalper_backtest import run_fast_scalper_backtest_job
        run_fast_scalper_backtest_job(run_id)
        return
    if strategy_type == "momentum":
        from app.services.fast_momentum_backtest import run_fast_momentum_backtest_job
        run_fast_momentum_backtest_job(run_id)
        return
    _run_emulator_backtest_job(run_id)
