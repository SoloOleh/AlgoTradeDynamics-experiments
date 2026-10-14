"""Performance summary helpers for live/demo trading bots."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import asc
from sqlalchemy.orm import Session

from app.bot_engine.exchange import registry
from app.bot_engine.exchange.capabilities import Capability
from app.bot_engine.exchange.errors import GuardRefusal
from app.bot_engine.exchange.port import Execution
from app.bot_engine.grid_runtime import get_order_link_prefix, get_position_snapshot
from app.bot_engine.strategies import get_strategy
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder


ZERO = Decimal("0")


@dataclass
class InventoryLot:
    qty: Decimal
    price: Decimal
    fee_per_unit: Decimal


def _to_decimal(value: Any, default: Decimal = ZERO) -> Decimal:
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _format_decimal(value: Decimal | float | int | None) -> str | None:
    if value is None:
        return None
    decimal_value = _to_decimal(value)
    return format(decimal_value.normalize(), "f") if decimal_value != 0 else "0"


def _order_payload(order: TradingBotOrder) -> dict[str, Any]:
    return dict(order.raw_response or {})


def _filled_qty(order: TradingBotOrder) -> Decimal:
    payload = _order_payload(order)
    qty = _to_decimal(payload.get("cumExecQty"))
    if qty > 0:
        return qty
    qty = _to_decimal(order.filled_qty)
    if qty > 0:
        return qty
    if order.status == "Filled":
        return _to_decimal(order.qty)
    return ZERO


def _average_execution_price(order: TradingBotOrder) -> Decimal:
    payload = _order_payload(order)
    avg_price = _to_decimal(payload.get("avgPrice") or payload.get("avgPriceE4"))
    if avg_price > 0:
        return avg_price

    value = _to_decimal(payload.get("cumExecValue") or payload.get("cumExecAmount"))
    qty = _filled_qty(order)
    if value > 0 and qty > 0:
        return value / qty

    return _to_decimal(order.price)


def _executed_fee(order: TradingBotOrder) -> Decimal:
    payload = _order_payload(order)
    for key in ("cumExecFee", "execFee", "fee", "cumFee"):
        fee = _to_decimal(payload.get(key))
        if fee > 0:
            return fee
    return ZERO


def _is_buy_entry(order: TradingBotOrder) -> bool:
    return order.side == "Buy" and order.order_role.startswith(("grid_entry_", "dca_entry_"))


def _is_position_closing_sell(order: TradingBotOrder) -> bool:
    if order.side != "Sell":
        return False
    if order.order_role in {"position_take_profit", "position_close", "take_profit_recovered"}:
        return True
    return order.order_role.startswith(("take_profit_", "tp_"))


def _pct(value: Decimal, base: Decimal) -> Decimal | None:
    if base == 0:
        return None
    return (value / base) * Decimal("100")



def _execution_history(adapter, bot: TradingBot) -> list[Execution]:
    return adapter.executions(bot.category, bot.symbol)


def _get_scalper_performance(db: Session, bot: TradingBot) -> dict[str, Any]:
    try:
        adapter = registry.exchange_for(bot, {Capability.EXECUTION_PNL})
    except GuardRefusal:
        # Nothing is sent to any exchange; the bot page stays readable (AC-02, AC-10).
        adapter = None
    prefix = get_order_link_prefix(bot)
    history = _execution_history(adapter, bot) if adapter is not None else []
    executions = [item for item in history if (item.client_tag or "").startswith(prefix)]
    executions.sort(key=lambda item: (item.exec_time_ms or 0, item.exec_seq or 0))

    position_side: str | None = None
    position_qty = ZERO
    average_entry = ZERO
    cycle_fees = ZERO
    cycle_gross = ZERO
    cycle_entry_cost = ZERO
    closed_cycles = 0
    winning_cycles = 0
    gross_realized = ZERO
    closed_fees = ZERO
    total_fees = ZERO
    total_entry_cost = ZERO
    total_exit_value = ZERO

    for item in executions:
        side = (item.side or "").title()
        qty = _to_decimal(item.qty)
        price = _to_decimal(item.price)
        fee = _to_decimal(item.fee)
        pnl = _to_decimal(item.realised_pnl)
        if side not in {"Buy", "Sell"} or qty <= 0 or price <= 0:
            continue
        total_fees += fee
        if position_qty <= 0:
            position_side = side
            position_qty = qty
            average_entry = price
            cycle_fees = fee
            cycle_gross = ZERO
            cycle_entry_cost = qty * price
            total_entry_cost += qty * price
            continue
        if side == position_side:
            new_qty = position_qty + qty
            average_entry = ((position_qty * average_entry) + (qty * price)) / new_qty
            position_qty = new_qty
            cycle_fees += fee
            cycle_entry_cost += qty * price
            total_entry_cost += qty * price
            continue

        close_qty = min(qty, position_qty)
        gross_realized += pnl
        cycle_gross += pnl
        cycle_fees += fee
        total_exit_value += close_qty * price
        position_qty -= close_qty
        if position_qty <= Decimal("1e-12"):
            net_cycle = cycle_gross - cycle_fees
            closed_cycles += 1
            if net_cycle > 0:
                winning_cycles += 1
            closed_fees += cycle_fees
            position_qty = ZERO
            average_entry = ZERO
            position_side = None
            cycle_fees = ZERO
            cycle_gross = ZERO
            cycle_entry_cost = ZERO

    net_realized = gross_realized - closed_fees
    average_cycle = net_realized / closed_cycles if closed_cycles else ZERO
    win_rate = Decimal(winning_cycles) / Decimal(closed_cycles) * Decimal("100") if closed_cycles else ZERO
    position = None
    if adapter is not None:
        try:
            position = get_strategy(bot.strategy_type).get_position(db, bot, adapter=adapter)
        except Exception:  # noqa: BLE001
            position = None
    unrealized = _to_decimal(position.get("unrealized_pnl")) if position else ZERO
    open_qty = _to_decimal(position.get("size")) if position else ZERO
    open_value = _to_decimal(position.get("position_value")) if position else ZERO
    total_pnl = net_realized + unrealized
    return {
        "closed_cycles": closed_cycles,
        "winning_cycles": winning_cycles,
        "win_rate_percent": _format_decimal(win_rate),
        "gross_realized_pnl": _format_decimal(gross_realized),
        "closed_fees": _format_decimal(closed_fees),
        "total_fees": _format_decimal(total_fees),
        "net_realized_pnl": _format_decimal(net_realized),
        "realized_pnl_percent": _format_decimal(_pct(net_realized, total_entry_cost)),
        "average_cycle_pnl": _format_decimal(average_cycle),
        "unrealized_pnl": _format_decimal(unrealized),
        "total_pnl": _format_decimal(total_pnl),
        "total_pnl_percent": _format_decimal(_pct(total_pnl, total_entry_cost)),
        "open_position_qty": _format_decimal(open_qty),
        "open_position_value": _format_decimal(open_value),
        "total_buy_cost": _format_decimal(total_entry_cost),
        "total_sell_value": _format_decimal(total_exit_value),
    }

def get_performance_summary(db: Session, bot: TradingBot) -> dict[str, Any]:
    """Calculate a best-effort PnL summary from executions and open position PnL."""
    if bot.strategy_type in {"pattern_scalper", "momentum"}:
        return _get_scalper_performance(db, bot)
    orders = (
        db.query(TradingBotOrder)
        .filter(TradingBotOrder.bot_id == bot.id, TradingBotOrder.user_id == bot.user_id)
        .order_by(asc(TradingBotOrder.created_at), asc(TradingBotOrder.id))
        .all()
    )
    filled_orders = [order for order in orders if order.status == "Filled"]

    inventory: list[InventoryLot] = []
    closed_cycles = 0
    winning_cycles = 0
    gross_realized_pnl = ZERO
    closed_fees = ZERO
    total_fees = ZERO
    total_buy_cost = ZERO
    total_sell_value = ZERO

    for order in filled_orders:
        qty = _filled_qty(order)
        price = _average_execution_price(order)
        fee = _executed_fee(order)
        total_fees += fee

        if qty <= 0 or price <= 0:
            continue

        if _is_buy_entry(order):
            total_buy_cost += qty * price
            inventory.append(
                InventoryLot(
                    qty=qty,
                    price=price,
                    fee_per_unit=(fee / qty) if qty > 0 else ZERO,
                )
            )
            continue

        if not _is_position_closing_sell(order):
            continue

        remaining_sell_qty = qty
        sell_fee_per_unit = (fee / qty) if qty > 0 else ZERO
        cycle_gross = ZERO
        cycle_fees = ZERO
        matched_qty = ZERO

        while remaining_sell_qty > 0 and inventory:
            lot = inventory[0]
            matched = min(lot.qty, remaining_sell_qty)
            cycle_gross += (price - lot.price) * matched
            cycle_fees += (lot.fee_per_unit + sell_fee_per_unit) * matched
            matched_qty += matched
            lot.qty -= matched
            remaining_sell_qty -= matched
            if lot.qty <= 0:
                inventory.pop(0)

        if matched_qty <= 0:
            continue

        total_sell_value += matched_qty * price
        gross_realized_pnl += cycle_gross
        closed_fees += cycle_fees
        closed_cycles += 1
        if cycle_gross - cycle_fees > 0:
            winning_cycles += 1

    net_realized_pnl = gross_realized_pnl - closed_fees
    average_cycle_pnl = net_realized_pnl / closed_cycles if closed_cycles > 0 else ZERO
    win_rate_percent = (Decimal(winning_cycles) / Decimal(closed_cycles)) * Decimal("100") if closed_cycles else ZERO

    try:
        position = get_position_snapshot(db, bot)
    except Exception:  # noqa: BLE001 - performance must not break the bot page if exchange snapshot fails.
        position = None

    unrealized_pnl = _to_decimal(position.get("unrealized_pnl")) if position else ZERO
    open_position_qty = _to_decimal(position.get("size")) if position else ZERO
    open_position_value = _to_decimal(position.get("position_value")) if position else ZERO
    total_pnl = net_realized_pnl + unrealized_pnl

    realized_pnl_percent = _pct(net_realized_pnl, total_buy_cost)
    total_pnl_percent = _pct(total_pnl, total_buy_cost)

    return {
        "closed_cycles": closed_cycles,
        "winning_cycles": winning_cycles,
        "win_rate_percent": _format_decimal(win_rate_percent),
        "gross_realized_pnl": _format_decimal(gross_realized_pnl),
        "closed_fees": _format_decimal(closed_fees),
        "total_fees": _format_decimal(total_fees),
        "net_realized_pnl": _format_decimal(net_realized_pnl),
        "realized_pnl_percent": _format_decimal(realized_pnl_percent),
        "average_cycle_pnl": _format_decimal(average_cycle_pnl),
        "unrealized_pnl": _format_decimal(unrealized_pnl),
        "total_pnl": _format_decimal(total_pnl),
        "total_pnl_percent": _format_decimal(total_pnl_percent),
        "open_position_qty": _format_decimal(open_position_qty),
        "open_position_value": _format_decimal(open_position_value),
        "total_buy_cost": _format_decimal(total_buy_cost),
        "total_sell_value": _format_decimal(total_sell_value),
    }
