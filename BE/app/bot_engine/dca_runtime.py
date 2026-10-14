"""DCA (Dollar-Cost Averaging) bot runtime helpers.

The bot buys the base quantity with a market order, places safety limit orders
below it, and lets the shared position take-profit follow the average entry
price down. Order sync, take-profit and cycle rollover are reused from the grid
runtime; only the order ladder and the cycle lifecycle live here.

TradingBot columns are reused: order_qty is the base quantity,
grid_orders_count is the number of safety orders, grid_step_percent is the
distance to the first safety order.
"""
from __future__ import annotations

from app.bot_engine.events import log_bot_event
from app.bot_engine.exchange import registry
from app.bot_engine.exchange.port import Order
from app.bot_engine.grid_runtime import (
    POSITION_TP_ROLE,
    _active_order_exists,
    _get_bot_owned_long_position,
    _is_current_generation_link_id,
    _log_risk_blocked,
    _mark_order_filled_logged,
    _next_cycle_id,
    _pending_completed_position_take_profit_orders,
    _record_order,
    _rollover_completed_take_profit_cycle,
    _safe_float,
    _should_tick,
    _utcnow,
    ensure_live_trading_allowed,
    get_bot_setting,
    get_order_link_prefix,
    is_live_environment,
    sync_bot_orders,
    sync_position_take_profit,
)
from app.bot_engine.market_data import get_instrument_rules, get_last_price
from app.bot_engine.orders import (
    ACTIVE_ORDER_STATUSES,
    OrderRequest,
    get_open_orders,
    normalize_order_request,
    place_order,
    validate_order_request,
)
from app.bot_engine.positions import get_open_positions
from app.models.trading_bot import TradingBot
from app.models.trading_bot_order import TradingBotOrder

DCA_ENTRY_ROLE_PREFIX = "dca_entry_"


def _dca_level_role(level: int) -> str:
    return f"{DCA_ENTRY_ROLE_PREFIX}{level}"


def _format_optional_number(value) -> str | None:
    if value is None:
        return None
    return f"{float(value):.12f}".rstrip("0").rstrip(".")


def build_dca_ladder(bot: TradingBot, anchor_price: float) -> list[dict]:
    """Plan every buy order of one DCA cycle. Level 1 is the base market order."""
    volume_multiplier = _safe_float(
        get_bot_setting(bot, "dca_volume_multiplier", 1.5), 1.5)
    step_multiplier = _safe_float(
        get_bot_setting(bot, "dca_step_multiplier", 1.3), 1.3)

    ladder = [{"level": 1, "qty": float(bot.order_qty), "price": None}]

    qty = float(bot.order_qty)
    step_percent = float(bot.grid_step_percent)
    deviation_percent = 0.0
    for index in range(int(bot.grid_orders_count)):
        qty = qty * volume_multiplier
        deviation_percent = deviation_percent + step_percent
        step_percent = step_percent * step_multiplier
        price = anchor_price * (1 - deviation_percent / 100)
        if price <= 0:
            break
        ladder.append({"level": index + 2, "qty": qty, "price": price})
    return ladder


def get_planned_ladder_qty(bot: TradingBot) -> float:
    return sum(level["qty"] for level in build_dca_ladder(bot, anchor_price=1.0))


def get_effective_dca_settings(bot: TradingBot) -> dict:
    planned_qty = get_planned_ladder_qty(bot)
    return {
        "dca_volume_multiplier": _safe_float(get_bot_setting(bot, "dca_volume_multiplier", 1.5), 1.5),
        "dca_step_multiplier": _safe_float(get_bot_setting(bot, "dca_step_multiplier", 1.3), 1.3),
        "take_profit_percent": _safe_float(get_bot_setting(bot, "take_profit_percent", 1.5), 1.5),
        "max_position_qty": _safe_float(get_bot_setting(bot, "max_position_qty", planned_qty), planned_qty),
        "max_open_orders": int(get_bot_setting(bot, "max_open_orders", bot.grid_orders_count + 2)),
        "max_notional_usdt": get_bot_setting(bot, "max_notional_usdt", None),
        "allow_live_trading": bool(get_bot_setting(bot, "allow_live_trading", False)),
        "stop_bot_on_error": bool(get_bot_setting(bot, "stop_bot_on_error", True)),
        "error_max_retries": int(get_bot_setting(bot, "error_max_retries", 5)),
        "error_retry_base_seconds": int(get_bot_setting(bot, "error_retry_base_seconds", 5)),
        "error_retry_max_seconds": int(get_bot_setting(bot, "error_retry_max_seconds", 300)),
        "cancel_orders_on_stop": bool(get_bot_setting(bot, "cancel_orders_on_stop", True)),
        **(bot.settings or {}),
    }


def validate_dca_configuration(bot: TradingBot) -> None:
    settings = get_effective_dca_settings(bot)
    if int(bot.grid_orders_count) < 1:
        raise ValueError("DCA bot needs at least one safety order")
    if settings["dca_volume_multiplier"] < 1:
        raise ValueError("DCA volume multiplier must be at least 1")
    if settings["dca_step_multiplier"] < 1:
        raise ValueError("DCA step multiplier must be at least 1")
    if settings["take_profit_percent"] <= 0:
        raise ValueError("Take-profit percent must be greater than zero")
    ladder = build_dca_ladder(bot, anchor_price=100.0)
    if len(ladder) != int(bot.grid_orders_count) + 1:
        raise ValueError(
            "DCA safety orders reach a non-positive price. "
            "Reduce the step, the step multiplier, or the safety order count."
        )


def _current_generation_open_orders(adapter, bot: TradingBot) -> list[Order]:
    return [
        order
        for order in get_open_orders(adapter, category=bot.category, symbol=bot.symbol)
        if _is_current_generation_link_id(bot, order.client_tag)
    ]


def _pending_buy_qty(open_orders: list[Order]) -> float:
    return sum(_safe_float(order.qty) for order in open_orders if order.side == "Buy")


def _get_current_long_position(adapter, bot: TradingBot) -> dict | None:
    """The account's long position on the bot's symbol, as the grid runtime reads it."""
    for position in get_open_positions(adapter, category=bot.category, symbol=bot.symbol):
        if position.size <= 0:
            continue
        side = position.side.lower()
        if side and side not in {"buy", "long"}:
            continue
        if not position.avg_entry_price or position.avg_entry_price <= 0:
            continue
        return {"size": position.size, "avg_entry_price": position.avg_entry_price}
    return None


def _active_dca_entry_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.order_role.like(f"{DCA_ENTRY_ROLE_PREFIX}%"),
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .all()
    )


def _evaluate_dca_buy_order_risk(
    db,
    bot: TradingBot,
    *,
    current_price: float,
    current_position_size: float,
    pending_buy_qty: float,
    current_open_orders: int,
    new_order_qty: float,
) -> tuple[str | None, dict]:
    settings = get_effective_dca_settings(bot)
    potential_total_qty = current_position_size + pending_buy_qty + new_order_qty
    estimated_notional = potential_total_qty * current_price
    payload = {
        "max_position_qty": settings["max_position_qty"],
        "current_position_qty": current_position_size,
        "pending_buy_qty": pending_buy_qty,
        "candidate_order_qty": new_order_qty,
        "potential_total_qty_after_order": potential_total_qty,
        "max_open_orders": settings["max_open_orders"],
        "current_open_orders": current_open_orders,
        "max_notional_usdt": settings["max_notional_usdt"],
        "estimated_notional_usdt_after_order": estimated_notional,
        "allow_live_trading": settings["allow_live_trading"],
        "is_live_environment": is_live_environment(bot),
    }

    live_message = ensure_live_trading_allowed(db, bot)
    if live_message is not None:
        return live_message, payload
    if potential_total_qty > settings["max_position_qty"] + 1e-12:
        return "Max position quantity exceeded", payload
    if current_open_orders + 1 > settings["max_open_orders"]:
        return "Max open orders exceeded", payload
    if settings["max_notional_usdt"] is not None and estimated_notional > float(settings["max_notional_usdt"]):
        return "Max notional exposure exceeded", payload
    return None, payload


def create_missing_dca_orders(
    db, bot: TradingBot, current_position_size: float = 0.0, *, adapter=None,
) -> list[TradingBotOrder]:
    """Open a new DCA cycle when the bot is flat.

    The base order and every safety order are placed in one shot so the whole
    ladder keeps one anchor price for the lifetime of the cycle.
    """
    if current_position_size > 0:
        return []
    if _active_dca_entry_orders(db, bot):
        return []
    if _pending_completed_position_take_profit_orders(db, bot):
        # The previous cycle is still waiting for cancellation confirmation.
        return []

    if adapter is None:
        adapter = registry.exchange_for(bot)
    anchor_price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
    rules = get_instrument_rules(adapter, category=bot.category, symbol=bot.symbol)

    open_exchange_orders = _current_generation_open_orders(adapter, bot)
    pending_buy_qty = _pending_buy_qty(open_exchange_orders)
    current_open_orders = len(open_exchange_orders)

    created_orders: list[TradingBotOrder] = []
    for planned in build_dca_ladder(bot, anchor_price):
        level = planned["level"]
        role = _dca_level_role(level)
        cycle_id = _next_cycle_id(db, bot, role)
        order = normalize_order_request(
            OrderRequest(
                side="Buy",
                order_type="Market" if planned["price"] is None else "Limit",
                order_role=role,
                order_link_id=f"{get_order_link_prefix(bot)}dca-entry-{level}-{cycle_id}",
                qty=planned["qty"],
                price=planned["price"],
            ),
            rules,
        )

        risk_message, risk_payload = _evaluate_dca_buy_order_risk(
            db,
            bot,
            current_price=anchor_price,
            current_position_size=current_position_size,
            pending_buy_qty=pending_buy_qty,
            current_open_orders=current_open_orders,
            new_order_qty=order.qty,
        )
        if risk_message is not None:
            _log_risk_blocked(db, bot, risk_message, risk_payload)
            break

        validation_error = validate_order_request(order, rules)
        if validation_error:
            log_bot_event(db, bot, "error", validation_error, {
                          "order_link_id": order.order_link_id})
            if level == 1:
                # Without the base order there is no entry, so safety
                # orders alone would just be a passive grid.
                break
            continue
        if _active_order_exists(db, bot, order.order_link_id):
            continue

        response = place_order(
            adapter, category=bot.category, symbol=bot.symbol, order=order)
        created = _record_order(db, bot, order, response)
        created_orders.append(created)
        pending_buy_qty += created.qty
        current_open_orders += 1
        log_bot_event(db, bot, "dca_entry_created", "Created DCA cycle order", {
            "order_link_id": order.order_link_id,
            "level": level,
            "side": order.side,
            "order_type": order.order_type,
            "qty": order.qty,
            "price": order.price,
        })

    return created_orders


def get_dca_risk_summary(db, bot: TradingBot, *, adapter=None) -> dict:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    settings = get_effective_dca_settings(bot)
    exchange_position = _get_current_long_position(adapter, bot)
    position = _get_bot_owned_long_position(
        db, bot, exchange_position=exchange_position)
    current_position_qty = position["size"] if position is not None else 0.0

    open_orders = _current_generation_open_orders(adapter, bot)
    pending_buy_qty = _pending_buy_qty(open_orders)
    current_open_orders = len(open_orders)
    current_price = get_last_price(adapter, category=bot.category, symbol=bot.symbol)
    potential_total_qty = current_position_qty + pending_buy_qty
    estimated_notional = potential_total_qty * current_price

    blocked = False
    reason = None
    if is_live_environment(bot) and not settings["allow_live_trading"]:
        blocked = True
        reason = "Live trading is disabled for this bot"
    elif potential_total_qty > settings["max_position_qty"] + 1e-12:
        blocked = True
        reason = "Max position quantity exceeded"
    elif current_open_orders > settings["max_open_orders"]:
        blocked = True
        reason = "Max open orders exceeded"
    elif settings["max_notional_usdt"] is not None and estimated_notional > float(settings["max_notional_usdt"]):
        blocked = True
        reason = "Max notional exposure exceeded"

    return {
        "max_position_qty": _format_optional_number(settings["max_position_qty"]),
        "current_position_qty": _format_optional_number(current_position_qty),
        "pending_buy_qty": _format_optional_number(pending_buy_qty),
        "potential_total_qty": _format_optional_number(potential_total_qty),
        "max_open_orders": settings["max_open_orders"],
        "current_open_orders": current_open_orders,
        "max_notional_usdt": _format_optional_number(settings["max_notional_usdt"]),
        "estimated_notional_usdt": _format_optional_number(estimated_notional),
        "allow_live_trading": settings["allow_live_trading"],
        "is_live_environment": is_live_environment(bot),
        "blocked": blocked,
        "reason": reason,
    }


def tick_dca_bot(db, bot: TradingBot, *, force: bool = False, adapter=None) -> dict:
    if bot.runtime_status != "running":
        return {"orders": [], "events": 0, "message": "Bot is not running"}
    if not bot.is_active:
        bot.runtime_status = "stopped"
        bot.last_error = "Bot is inactive"
        log_bot_event(db, bot, "error", "Bot is inactive and cannot trade")
        db.add(bot)
        db.commit()
        return {"orders": [], "events": 1, "message": "Bot inactive"}
    if not force and not _should_tick(bot):
        return {"orders": [], "events": 0, "message": "Tick skipped by interval"}

    live_message = ensure_live_trading_allowed(db, bot)
    if live_message is not None:
        bot.last_run_at = _utcnow()
        bot.last_error = None
        db.add(bot)
        db.commit()
        return {"orders": [], "events": 1, "message": live_message}

    try:
        if adapter is None:
            adapter = registry.exchange_for(bot)
        synced = sync_bot_orders(db, bot, adapter=adapter)

        for change in synced:
            order = change["order"]
            if change["became_filled"] and order.order_role.startswith(DCA_ENTRY_ROLE_PREFIX):
                if order.filled_event_logged_at is None:
                    log_bot_event(db, bot, "dca_entry_filled", "DCA cycle order filled", {
                        "order_link_id": order.order_link_id,
                        "side": order.side,
                        "qty": order.qty,
                        "price": order.price,
                    })
                    _mark_order_filled_logged(order)
                    db.add(order)
            if change["became_filled"] and order.order_role == POSITION_TP_ROLE:
                if order.filled_event_logged_at is None:
                    log_bot_event(db, bot, "position_take_profit_filled",
                        "Position take-profit filled", {
                            "order_link_id": order.order_link_id,
                            "side": order.side,
                            "qty": order.qty,
                            "price": order.price,
                        })
                    _mark_order_filled_logged(order)
                    db.add(order)
            if change["status_changed"] and change["new_status"] == "Rejected":
                log_bot_event(db, bot, "order_rejected", "Order rejected", {
                    "order_link_id": order.order_link_id,
                    "side": order.side,
                    "qty": order.qty,
                    "price": order.price,
                })

        rollover_changes, rollover_triggered, cancellation_confirmed = (
            _rollover_completed_take_profit_cycle(
                db, bot,
                entry_role_prefix=DCA_ENTRY_ROLE_PREFIX,
                event_prefix="dca",
            )
        )
        if rollover_triggered and not cancellation_confirmed:
            bot.last_run_at = _utcnow()
            bot.last_error = None
            db.add(bot)
            db.commit()
            return {
                "orders": rollover_changes,
                "events": len(synced),
                "message": "Waiting for previous cycle cancellation confirmation",
            }

        tp_changes, current_position_size = sync_position_take_profit(db, bot)
        created_orders = create_missing_dca_orders(db, bot, current_position_size, adapter=adapter)

        bot.last_run_at = _utcnow()
        bot.last_error = None
        db.add(bot)
        db.commit()
        return {
            "orders": rollover_changes + tp_changes + created_orders,
            "events": len(synced),
            "message": (
                "Tick completed; take-profit cycle rolled over"
                if rollover_triggered
                else "Tick completed"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        if not bot.is_backtest:
            raise
        bot.last_run_at = _utcnow()
        bot.last_error = str(exc)
        if get_effective_dca_settings(bot)["stop_bot_on_error"]:
            bot.runtime_status = "stopped"
        log_bot_event(db, bot, "error", str(exc))
        db.add(bot)
        db.commit()
        return {"orders": [], "events": 1, "message": str(exc)}
