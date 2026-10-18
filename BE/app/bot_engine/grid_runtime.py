"""Continuous grid bot runtime helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy import desc

from app.core.clock import utcnow as runtime_utcnow
from app.bot_engine.events import log_bot_event
from app.bot_engine.exchange import registry
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
from app.bot_engine.exchange.port import Order, PlacedOrder
from app.bot_engine.market_data import get_instrument_rules, get_last_price, get_ticker_snapshot
from app.bot_engine.orders import (
    ACTIVE_ORDER_STATUSES,
    OrderRequest,
    cancel_order_by_link_id,
    apply_exchange_order,
    get_open_orders,
    get_orders_by_prefix,
    normalize_order_request,
    place_order,
    resolve_order_state,
    validate_order_request,
)
from app.bot_engine.positions import get_open_positions
from app.models.trading_bot import TradingBot
from app.models.trading_bot_event import TradingBotEvent
from app.models.trading_bot_order import TradingBotOrder
from app.bot_engine.exchange.redaction import scrub


def _utcnow() -> datetime:
    return runtime_utcnow()


def get_bot_setting(bot: TradingBot, key: str, default):
    if bot.settings and key in bot.settings:
        return bot.settings[key]
    return default


def get_order_link_generation(bot: TradingBot) -> int:
    generation = getattr(bot, "order_link_generation", 1) or 1
    return max(int(generation), 1)


def get_order_link_prefix(bot: TradingBot) -> str:
    return f"bot-{bot.id}-g{get_order_link_generation(bot)}-"


def get_legacy_order_link_prefix(bot: TradingBot) -> str:
    return f"bot-{bot.id}-"


ENTRY_LINK_RE = re.compile(
    r"^bot-(?P<bot_id>\d+)(?:-g(?P<generation>\d+))?-entry-(?P<level>\d+)-(?P<cycle>\d+)$")
DCA_ENTRY_LINK_RE = re.compile(
    r"^bot-(?P<bot_id>\d+)(?:-g(?P<generation>\d+))?-dca-entry-(?P<level>\d+)-(?P<cycle>\d+)$")
TP_LINK_RE = re.compile(
    r"^bot-(?P<bot_id>\d+)(?:-g(?P<generation>\d+))?-tp-for-(?P<entry_id>\d+)$")
POSITION_TP_LINK_RE = re.compile(
    r"^bot-(?P<bot_id>\d+)(?:-g(?P<generation>\d+))?-position-tp(?:-(?P<timestamp_ms>\d+))?$")
POSITION_TP_ROLE = "position_take_profit"
POSITION_CLOSE_ROLE = "position_close"
LEGACY_TP_ROLE_PREFIXES = ("take_profit_", "tp_")


def _level_role(level_index: int) -> str:
    return f"grid_entry_{level_index}"


def _close_position_link_id(bot: TradingBot) -> str:
    timestamp_ms = int(_utcnow().timestamp() * 1000)
    return f"{get_order_link_prefix(bot)}position-close-{timestamp_ms}"


def is_live_environment(bot: TradingBot) -> bool:
    return str(bot.environment).lower() == "live"


def get_effective_bot_settings(bot: TradingBot) -> dict:
    order_qty = _safe_float(bot.order_qty, 0.0)
    grid_orders_count = max(int(bot.grid_orders_count or 1), 1)
    max_position_qty = order_qty * grid_orders_count
    return {
        "max_position_qty": float(get_bot_setting(bot, "max_position_qty", max_position_qty)),
        "max_open_orders": int(get_bot_setting(bot, "max_open_orders", grid_orders_count + 1)),
        "max_notional_usdt": get_bot_setting(bot, "max_notional_usdt", None),
        "max_grid_levels": int(get_bot_setting(bot, "max_grid_levels", grid_orders_count)),
        "allow_live_trading": bool(get_bot_setting(bot, "allow_live_trading", False)),
        "stop_bot_on_error": bool(get_bot_setting(bot, "stop_bot_on_error", True)),
        "error_max_retries": int(get_bot_setting(bot, "error_max_retries", 5)),
        "error_retry_base_seconds": int(get_bot_setting(bot, "error_retry_base_seconds", 5)),
        "error_retry_max_seconds": int(get_bot_setting(bot, "error_retry_max_seconds", 300)),
        "cancel_orders_on_stop": bool(get_bot_setting(bot, "cancel_orders_on_stop", True)),
        **(bot.settings or {}),
    }


def _position_tp_link_prefix(bot: TradingBot) -> str:
    return f"{get_order_link_prefix(bot)}position-tp"


def _legacy_position_tp_link_prefix(bot: TradingBot) -> str:
    return f"bot-{bot.id}-position-tp"


def _position_tp_link_id(bot: TradingBot) -> str:
    timestamp_ms = int(_utcnow().timestamp() * 1000)
    return f"{_position_tp_link_prefix(bot)}-{timestamp_ms}"


def _is_legacy_take_profit_role(order_role: str) -> bool:
    if order_role == POSITION_TP_ROLE:
        return False
    if order_role == "take_profit_recovered":
        return True
    return order_role.startswith(LEGACY_TP_ROLE_PREFIXES)


def _is_current_generation_link_id(bot: TradingBot, order_link_id: str | None) -> bool:
    return bool(order_link_id) and order_link_id.startswith(get_order_link_prefix(bot))


def _is_position_tp_link_id_for_bot(bot: TradingBot, order_link_id: str | None) -> bool:
    if not order_link_id:
        return False
    match = POSITION_TP_LINK_RE.match(order_link_id)
    if not match:
        return False
    if int(match.group("bot_id")) != bot.id:
        return False
    generation = match.group("generation")
    return generation is None or int(generation) == get_order_link_generation(bot)


def _is_current_generation_position_tp_link_id(bot: TradingBot, order_link_id: str | None) -> bool:
    return bool(order_link_id) and order_link_id.startswith(_position_tp_link_prefix(bot))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_optional_number(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _next_cycle_id(db, bot: TradingBot, order_role: str) -> int:
    existing = (
        db.query(TradingBotOrder)
        .filter(TradingBotOrder.bot_id == bot.id, TradingBotOrder.order_role == order_role)
        .count()
    )
    return existing + 1


def _active_order_exists(db, bot: TradingBot, order_link_id: str) -> bool:
    return (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.order_link_id == order_link_id,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .first()
        is not None
    )


def _infer_order_fields_from_link_id(order_link_id: str) -> tuple[str, int | None]:
    dca_match = DCA_ENTRY_LINK_RE.match(order_link_id)
    if dca_match:
        level = int(dca_match.group("level"))
        return f"dca_entry_{level}", None

    entry_match = ENTRY_LINK_RE.match(order_link_id)
    if entry_match:
        level = int(entry_match.group("level"))
        return _level_role(level), None

    position_tp_match = POSITION_TP_LINK_RE.match(order_link_id)
    if position_tp_match:
        return POSITION_TP_ROLE, None

    tp_match = TP_LINK_RE.match(order_link_id)
    if tp_match:
        entry_id = int(tp_match.group("entry_id"))
        return "take_profit_recovered", entry_id

    return "recovered", None


def _record_order(db, bot: TradingBot, order: OrderRequest, placed: PlacedOrder, *, status_override: str | None = None) -> TradingBotOrder:
    existing = None
    if order.order_link_id:
        existing = (
            db.query(TradingBotOrder)
            .filter(TradingBotOrder.bot_id == bot.id, TradingBotOrder.order_link_id == order.order_link_id)
            .first()
        )
    record = existing or TradingBotOrder(
        created_at=_utcnow(),
        updated_at=_utcnow(),
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
        parent_order_id=order.parent_order_id,
    )
    record.price = order.price
    record.exchange_order_id = placed.order_id or record.exchange_order_id
    record.status = status_override or placed.status or record.status or "New"
    payload = dict(placed.raw or {})
    payload.update({
        "orderLinkId": order.order_link_id,
        "side": order.side,
        "orderType": order.order_type,
        "qty": str(order.qty),
        "price": str(order.price) if order.price is not None else None,
        "reduceOnly": order.reduce_only,
    })
    record.raw_response = payload
    db.add(record)
    db.flush()
    return record


def _update_local_order_from_exchange(record: TradingBotOrder, remote: Order) -> None:
    apply_exchange_order(record, remote)


def _mark_order_filled_logged(record: TradingBotOrder) -> None:
    if record.filled_event_logged_at is None:
        record.filled_event_logged_at = _utcnow()


def _status_change_payload(record: TradingBotOrder, old_status: str, new_status: str) -> dict:
    return {
        "order": record,
        "old_status": old_status,
        "new_status": new_status,
        "status_changed": old_status != new_status,
        "became_filled": old_status != "Filled" and new_status == "Filled",
    }


def _build_grid_entry_order(db, bot: TradingBot, level_index: int, current_price: float) -> OrderRequest:
    role = _level_role(level_index)
    cycle_id = _next_cycle_id(db, bot, role)
    price = current_price * \
        (1 - ((bot.grid_step_percent / 100) * (level_index - 1)))
    return OrderRequest(
        side="Buy",
        order_type="Limit",
        order_role=role,
        order_link_id=f"{get_order_link_prefix(bot)}entry-{level_index}-{cycle_id}",
        qty=bot.order_qty,
        price=price,
    )


def _get_current_long_position(adapter, *, category: str, symbol: str) -> dict | None:
    for position in get_open_positions(adapter, category=category, symbol=symbol):
        if position.size <= 0:
            continue
        side = position.side.lower()
        if side and side not in {"buy", "long"}:
            continue
        if not position.avg_entry_price or position.avg_entry_price <= 0:
            continue
        return {
            "size": position.size,
            "avg_entry_price": position.avg_entry_price,
            "position": position,
        }
    return None


def _filled_order_qty(order: TradingBotOrder) -> float:
    """Return the executed quantity represented by a locally persisted filled order."""
    filled_qty = _safe_float(order.filled_qty, 0.0)
    if filled_qty > 1e-12:
        return filled_qty
    if order.status == "Filled":
        return max(_safe_float(order.qty, 0.0), 0.0)
    return 0.0


def _filled_entry_price(order: TradingBotOrder) -> float | None:
    """Best available execution price for a filled bot entry."""
    raw = order.raw_response or {}
    for value in (raw.get("avgPrice"), raw.get("execPrice"), raw.get("price"), order.price):
        price = _safe_float(value, 0.0)
        if price > 0:
            return price
    return None


def _get_bot_owned_long_position(
    db,
    bot: TradingBot,
    *,
    exchange_position: dict | None = None,
) -> dict | None:
    """Reconstruct the long quantity owned by this bot from its own fills.

    Bybit/emulator exposes one net position per account+symbol. Multiple bots on
    the same account therefore see the same exchange position. Runtime decisions
    must not treat that shared quantity as belonging to every bot. The order-link
    prefix is the ownership boundary.
    """
    orders = (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status == "Filled",
        )
        .order_by(TradingBotOrder.created_at, TradingBotOrder.id)
        .all()
    )

    position_qty = 0.0
    avg_entry_price: float | None = None
    for order in orders:
        if not _is_current_generation_link_id(bot, order.order_link_id):
            continue

        qty = _filled_order_qty(order)
        if qty <= 1e-12:
            continue

        is_entry = order.side == "Buy" and order.order_role.startswith(("grid_entry_", "dca_entry_"))
        is_exit = order.side == "Sell" and order.order_role in {POSITION_TP_ROLE, POSITION_CLOSE_ROLE}

        if is_entry:
            price = _filled_entry_price(order)
            if position_qty <= 1e-12:
                avg_entry_price = price
                position_qty = qty
            else:
                new_qty = position_qty + qty
                if avg_entry_price is not None and price is not None:
                    avg_entry_price = ((position_qty * avg_entry_price) + (qty * price)) / new_qty
                else:
                    avg_entry_price = None
                position_qty = new_qty
            continue

        if is_exit and position_qty > 1e-12:
            position_qty = max(position_qty - min(qty, position_qty), 0.0)
            if position_qty <= 1e-12:
                position_qty = 0.0
                avg_entry_price = None

    if position_qty <= 1e-12:
        return None

    # Market entries can initially have no execution price in the local row. It
    # is safe to borrow the exchange average only when the whole exchange
    # position equals this bot's reconstructed quantity. If another bot left a
    # position behind, sizes differ and we deliberately do not adopt its basis.
    if avg_entry_price is None and exchange_position is not None:
        exchange_size = _safe_float(exchange_position.get("size"), 0.0)
        if abs(exchange_size - position_qty) <= 1e-9:
            avg_entry_price = _safe_float(exchange_position.get("avg_entry_price"), 0.0) or None

    return {
        "size": position_qty,
        "avg_entry_price": avg_entry_price,
        "position": (exchange_position or {}).get("position"),
    }


def _build_position_take_profit_order(bot: TradingBot, position_size: float, avg_entry_price: float) -> OrderRequest:
    take_profit_percent = float(
        get_bot_setting(bot, "take_profit_percent", 1.5))
    tp_price = avg_entry_price * (1 + take_profit_percent / 100)
    return OrderRequest(
        side="Sell",
        order_type="Limit",
        order_role=POSITION_TP_ROLE,
        order_link_id=_position_tp_link_id(bot),
        qty=position_size,
        price=tp_price,
        reduce_only=True,
    )


def _build_close_position_order(bot: TradingBot, position_size: float) -> OrderRequest:
    return OrderRequest(
        side="Sell",
        order_type="Market",
        order_role=POSITION_CLOSE_ROLE,
        order_link_id=_close_position_link_id(bot),
        qty=position_size,
        price=None,
        reduce_only=True,
    )


def _active_orders_by_role(db, bot: TradingBot, order_role: str) -> list[TradingBotOrder]:
    return (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.order_role == order_role,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .order_by(desc(TradingBotOrder.id))
        .all()
    )


def _active_position_take_profit_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return [
        order
        for order in _active_orders_by_role(db, bot, POSITION_TP_ROLE)
        if _is_position_tp_link_id_for_bot(bot, order.order_link_id)
    ]


def _latest_bot_event(db, bot: TradingBot, event_type: str | None = None) -> object | None:
    query = db.query(TradingBotEvent).filter(TradingBotEvent.bot_id == bot.id)
    if event_type is not None:
        query = query.filter(TradingBotEvent.event_type == event_type)
    return query.order_by(desc(TradingBotEvent.created_at), desc(TradingBotEvent.id)).first()


def _active_legacy_take_profit_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return [
        order
        for order in db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .order_by(desc(TradingBotOrder.id))
        .all()
        if _is_legacy_take_profit_role(order.order_role)
    ]


def _attach_position_take_profit_snapshot(record: TradingBotOrder, position: dict, order: OrderRequest) -> None:
    payload = dict(record.raw_response or {})
    payload.update({
        "reduceOnly": True,
        "positionSize": position["size"],
        "avgEntryPrice": position["avg_entry_price"],
        "tpQty": order.qty,
        "tpPrice": order.price,
    })
    record.raw_response = payload


def _active_local_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .order_by(desc(TradingBotOrder.created_at), desc(TradingBotOrder.id))
        .all()
    )


def _active_grid_entry_orders(db, bot: TradingBot, role_prefix: str = "grid_entry_") -> list[TradingBotOrder]:
    return [
        order
        for order in _active_local_orders(db, bot)
        if order.order_role.startswith(role_prefix)
        and _is_current_generation_link_id(bot, order.order_link_id)
    ]


def _pending_completed_position_take_profit_orders(db, bot: TradingBot) -> list[TradingBotOrder]:
    return [
        order
        for order in db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.order_role == POSITION_TP_ROLE,
            TradingBotOrder.status == "Filled",
            TradingBotOrder.cycle_completed_at.is_(None),
        )
        .order_by(TradingBotOrder.created_at, TradingBotOrder.id)
        .all()
        if _is_current_generation_position_tp_link_id(bot, order.order_link_id)
    ]


def _current_generation_open_orders(adapter, bot: TradingBot) -> list[Order]:
    return [
        order
        for order in get_open_orders(adapter, category=bot.category, symbol=bot.symbol)
        if _is_current_generation_link_id(bot, order.client_tag)
    ]


def _pending_buy_qty(open_orders: list[Order]) -> float:
    return sum(_safe_float(order.qty) for order in open_orders if order.side == "Buy")


def _risk_block_payload(bot: TradingBot, *, current_position_qty: float, pending_buy_qty: float, current_open_orders: int, current_price: float | None) -> dict:
    settings = get_effective_bot_settings(bot)
    potential_total_qty = current_position_qty + pending_buy_qty
    estimated_notional = None if current_price is None else potential_total_qty * current_price
    return {
        "max_position_qty": settings["max_position_qty"],
        "current_position_qty": current_position_qty,
        "pending_buy_qty": pending_buy_qty,
        "potential_total_qty": potential_total_qty,
        "max_open_orders": settings["max_open_orders"],
        "current_open_orders": current_open_orders,
        "max_notional_usdt": settings["max_notional_usdt"],
        "estimated_notional_usdt": estimated_notional,
        "allow_live_trading": settings["allow_live_trading"],
        "is_live_environment": is_live_environment(bot),
    }


def _log_risk_blocked(db, bot: TradingBot, message: str, payload: dict) -> None:
    latest = _latest_bot_event(db, bot, "risk_blocked")
    if latest is not None and latest.message == message and latest.payload == payload:
        return
    log_bot_event(db, bot, "risk_blocked", message, payload)


def ensure_live_trading_allowed(db, bot: TradingBot) -> str | None:
    settings = get_effective_bot_settings(bot)
    if is_live_environment(bot) and not settings["allow_live_trading"]:
        payload = {
            "allow_live_trading": settings["allow_live_trading"],
            "is_live_environment": True,
        }
        _log_risk_blocked(
            db, bot, "Live trading is disabled for this bot", payload)
        return "Live trading is disabled for this bot"
    return None


def _find_active_position_take_profit_order(db, bot: TradingBot, adapter=None) -> TradingBotOrder | None:
    if adapter is not None:
        _sync_exchange_open_position_take_profit_orders(db, bot, adapter)
    active_tps = _active_position_take_profit_orders(db, bot)
    return active_tps[0] if active_tps else None


def get_position_snapshot(db, bot: TradingBot, *, adapter=None) -> dict:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    ticker = get_ticker_snapshot(
        adapter, category=bot.category, symbol=bot.symbol)
    exchange_position = _get_current_long_position(
        adapter, category=bot.category, symbol=bot.symbol)
    position = _get_bot_owned_long_position(
        db, bot, exchange_position=exchange_position)
    active_tp = _find_active_position_take_profit_order(db, bot, adapter)
    mark_price_value = ticker.mark_price if ticker.mark_price is not None else ticker.last_price
    mark_price = _safe_float(
        mark_price_value, 0.0) if mark_price_value is not None else None

    if position is None:
        return {
            "symbol": bot.symbol,
            "category": bot.category,
            "side": None,
            "size": "0",
            "avg_entry_price": None,
            "mark_price": _format_optional_number(mark_price_value),
            "liq_price": None,
            "unrealized_pnl": None,
            "unrealized_pnl_percent": None,
            "leverage": None,
            "margin_mode": None,
            "position_value": None,
            "take_profit": None,
        }

    exchange = position.get("position")
    size = position["size"]
    avg_entry_price = position.get("avg_entry_price")
    position_value = size * mark_price if mark_price is not None else None
    unrealized_pnl = None
    unrealized_pnl_percent = None
    if avg_entry_price is not None and mark_price is not None:
        unrealized_pnl = (mark_price - avg_entry_price) * size
        entry_value = size * avg_entry_price
        if entry_value > 0:
            unrealized_pnl_percent = (unrealized_pnl / entry_value) * 100

    take_profit = None
    if active_tp is not None:
        take_profit = {
            "order_id": active_tp.exchange_order_id,
            "order_link_id": active_tp.order_link_id,
            "price": _format_optional_number(active_tp.price),
            "qty": _format_optional_number(active_tp.qty),
            "status": active_tp.status,
            "reduce_only": bool((active_tp.raw_response or {}).get("reduceOnly")),
        }

    return {
        "symbol": bot.symbol,
        "category": bot.category,
        "side": "Buy",
        "size": _format_optional_number(size),
        "avg_entry_price": _format_optional_number(avg_entry_price),
        "mark_price": _format_optional_number(mark_price_value),
        # Bybit sends an empty liqPrice when there is none; the page showed that empty text.
        "liq_price": (_format_optional_number(exchange.liq_price) or "") if exchange else None,
        "unrealized_pnl": _format_optional_number(unrealized_pnl),
        "unrealized_pnl_percent": _format_optional_number(unrealized_pnl_percent),
        "leverage": _format_optional_number(exchange.leverage if exchange else None),
        "margin_mode": exchange.margin_mode if exchange else None,
        "position_value": _format_optional_number(position_value),
        "take_profit": take_profit,
    }


def get_risk_summary(db, bot: TradingBot, *, adapter=None) -> dict:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    settings = get_effective_bot_settings(bot)
    exchange_position = _get_current_long_position(
        adapter, category=bot.category, symbol=bot.symbol)
    position = _get_bot_owned_long_position(
        db, bot, exchange_position=exchange_position)
    current_position_qty = position["size"] if position is not None else 0.0
    open_orders = _current_generation_open_orders(adapter, bot)
    pending_buy_qty = _pending_buy_qty(open_orders)
    current_open_orders = len(open_orders)
    current_price = get_last_price(
        adapter, category=bot.category, symbol=bot.symbol)
    potential_total_qty = current_position_qty + pending_buy_qty
    estimated_notional = potential_total_qty * current_price

    blocked = False
    reason = None
    if is_live_environment(bot) and not settings["allow_live_trading"]:
        blocked = True
        reason = "Live trading is disabled for this bot"
    elif potential_total_qty > settings["max_position_qty"]:
        blocked = True
        reason = "Max position quantity exceeded"
    elif current_open_orders > settings["max_open_orders"]:
        blocked = True
        reason = "Max open orders exceeded"
    elif settings["max_notional_usdt"] is not None and estimated_notional > float(settings["max_notional_usdt"]):
        blocked = True
        reason = "Max notional exposure exceeded"
    elif potential_total_qty == settings["max_position_qty"]:
        blocked = False
        reason = "At max planned exposure"
    elif current_open_orders == settings["max_open_orders"]:
        blocked = False
        reason = "At max open orders limit"
    elif settings["max_notional_usdt"] is not None and estimated_notional == float(settings["max_notional_usdt"]):
        blocked = False
        reason = "At max notional limit"

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

def get_runtime_state(db, bot: TradingBot) -> tuple[str, str | None]:
    latest_event = _latest_bot_event(db, bot)
    last_risk_message = None
    if latest_event is not None and latest_event.event_type == "risk_blocked":
        last_risk_message = latest_event.message

    if bot.runtime_status in {"retrying", "paused", "error"}:
        return bot.runtime_status, last_risk_message
    if bot.runtime_status != "running":
        return "stopped", last_risk_message
    if bot.last_error:
        return "error", last_risk_message
    if latest_event is not None and latest_event.event_type == "risk_blocked":
        return "risk_blocked", latest_event.message

    active_orders = _active_local_orders(db, bot)
    has_active_tp = any(order.order_role ==
                        POSITION_TP_ROLE for order in active_orders)
    has_active_entries = any(order.order_role.startswith(
        ("grid_entry_", "dca_entry_")) for order in active_orders)
    if has_active_tp:
        return "tp_active", last_risk_message
    if has_active_entries:
        return "waiting_for_entry", last_risk_message
    return "running", last_risk_message


def _evaluate_new_buy_order_risk(db, bot: TradingBot, *, current_price: float, current_position_size: float, pending_buy_qty: float, current_open_orders: int, new_order_qty: float) -> tuple[str | None, dict]:
    settings = get_effective_bot_settings(bot)
    potential_total_qty = current_position_size + pending_buy_qty + new_order_qty
    estimated_notional = potential_total_qty * current_price
    payload = {
        **_risk_block_payload(
            bot,
            current_position_qty=current_position_size,
            pending_buy_qty=pending_buy_qty,
            current_open_orders=current_open_orders,
            current_price=current_price,
        ),
        "candidate_order_qty": new_order_qty,
        "potential_total_qty_after_order": potential_total_qty,
        "estimated_notional_usdt_after_order": estimated_notional,
    }

    live_message = ensure_live_trading_allowed(db, bot)
    if live_message is not None:
        return live_message, payload
    if potential_total_qty > settings["max_position_qty"]:
        return "Max position quantity reached", payload
    if current_open_orders >= settings["max_open_orders"]:
        return "Max open orders reached", payload
    max_notional = settings["max_notional_usdt"]
    if max_notional is not None and estimated_notional > float(max_notional):
        return "Max notional exposure reached", payload
    return None, payload


def _cancel_local_order(db, bot: TradingBot, adapter, order: TradingBotOrder, *, event_type: str, message: str) -> TradingBotOrder:
    response = cancel_order_by_link_id(
        adapter,
        category=bot.category,
        symbol=bot.symbol,
        order_link_id=order.order_link_id,
    )
    order.status = "Cancelled"
    payload = dict(order.raw_response or {})
    payload.update(response)
    order.raw_response = payload
    db.add(order)
    log_bot_event(db, bot, event_type, message, {
                  "order_link_id": order.order_link_id})
    return order


def _rollover_completed_take_profit_cycle(
    db,
    bot: TradingBot,
    *,
    entry_role_prefix: str = "grid_entry_",
    event_prefix: str = "grid",
    adapter=None,
) -> tuple[list[TradingBotOrder], bool, bool]:
    """Cancel the previous grid after a filled position TP before starting a new cycle.

    Returns (changed_orders, rollover_triggered, cancellation_confirmed).
    A filled TP remains pending until every active grid entry from the old cycle is
    absent from the exchange open-order list. This prevents a new grid from being
    created while an old averaging order can still fill.
    The DCA runtime reuses this with its own role prefix and event names.
    """
    completed_tps = _pending_completed_position_take_profit_orders(db, bot)
    if not completed_tps:
        return [], False, True

    if adapter is None:
        adapter = registry.exchange_for(bot)
    stale_entries = _active_grid_entry_orders(db, bot, entry_role_prefix)
    changed_orders: list[TradingBotOrder] = []

    for entry in stale_entries:
        changed_orders.append(
            _cancel_local_order(
                db,
                bot,
                adapter,
                entry,
                event_type=f"{event_prefix}_entry_cancelled_after_take_profit",
                message="Cancelled stale entry order after position take-profit",
            )
        )

    cancelled_link_ids = {
        entry.order_link_id for entry in stale_entries if entry.order_link_id
    }
    remaining_open_orders = {
        remote.client_tag: remote
        for remote in _current_generation_open_orders(adapter, bot)
        if remote.client_tag in cancelled_link_ids
        and (remote.status or "") in ACTIVE_ORDER_STATUSES
    }

    if remaining_open_orders:
        for entry in stale_entries:
            remote = remaining_open_orders.get(entry.order_link_id)
            if remote is not None:
                _update_local_order_from_exchange(entry, remote)
                db.add(entry)

        payload = {
            "take_profit_order_link_ids": [
                order.order_link_id for order in completed_tps
            ],
            "remaining_grid_order_link_ids": sorted(remaining_open_orders),
        }
        latest = _latest_bot_event(db, bot, f"{event_prefix}_cycle_rollover_waiting")
        if latest is None or latest.payload != payload:
            log_bot_event(
                db,
                bot,
                f"{event_prefix}_cycle_rollover_waiting",
                "Waiting for stale entry order cancellation confirmation",
                payload,
            )
        return changed_orders, True, False

    completed_at = _utcnow()
    for take_profit in completed_tps:
        take_profit.cycle_completed_at = completed_at
        db.add(take_profit)

    log_bot_event(
        db,
        bot,
        f"{event_prefix}_cycle_completed",
        "Completed take-profit cycle and cleared the previous entry ladder",
        {
            "take_profit_order_link_ids": [
                order.order_link_id for order in completed_tps
            ],
            "cancelled_grid_order_link_ids": sorted(cancelled_link_ids),
        },
    )
    return changed_orders, True, True


def _order_matches_target(order: TradingBotOrder, target: OrderRequest) -> bool:
    price_matches = order.price == target.price
    qty_matches = order.qty == target.qty
    reduce_only = bool((order.raw_response or {}).get("reduceOnly"))
    return price_matches and qty_matches and reduce_only


def _is_duplicate_client_tag(placed: PlacedOrder) -> bool:
    rejection = placed.rejection
    return isinstance(rejection, ExchangeError) and rejection.error_class is ErrorClass.DUPLICATE_CLIENT_TAG


def _upsert_remote_order(db, bot: TradingBot, remote: Order) -> TradingBotOrder | None:
    order_link_id = remote.client_tag
    if not order_link_id:
        return None
    record = (
        db.query(TradingBotOrder)
        .filter(TradingBotOrder.bot_id == bot.id, TradingBotOrder.order_link_id == order_link_id)
        .first()
    )
    if record is None:
        inferred_role, parent_order_id = _infer_order_fields_from_link_id(
            order_link_id)
        record = TradingBotOrder(
            bot_id=bot.id,
            user_id=bot.user_id,
            exchange=bot.exchange,
            environment=bot.environment,
            category=bot.category,
            symbol=bot.symbol,
            side=remote.side or "Buy",
            order_type=remote.order_type or "Limit",
            order_role=inferred_role,
            order_link_id=order_link_id,
            qty=float(remote.qty or 0),
            parent_order_id=parent_order_id,
            status=remote.status or "New",
            raw_response=dict(remote.raw),
        )
        db.add(record)
        db.flush()
    _update_local_order_from_exchange(record, remote)
    db.add(record)
    return record


def _sync_exchange_open_position_take_profit_orders(db, bot: TradingBot, adapter) -> list[TradingBotOrder]:
    synced: list[TradingBotOrder] = []
    for remote in get_open_orders(adapter, category=bot.category, symbol=bot.symbol):
        if not _is_position_tp_link_id_for_bot(bot, remote.client_tag):
            continue
        if remote.side != "Sell":
            continue
        if not remote.reduce_only:
            continue
        if (remote.status or "") not in ACTIVE_ORDER_STATUSES:
            continue
        record = _upsert_remote_order(db, bot, remote)
        if record is not None:
            synced.append(record)
    db.flush()
    return synced


def sync_position_take_profit(db, bot: TradingBot, *, adapter=None) -> tuple[list[TradingBotOrder], float]:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    rules = get_instrument_rules(
        adapter, category=bot.category, symbol=bot.symbol)
    exchange_position = _get_current_long_position(
        adapter, category=bot.category, symbol=bot.symbol)
    position = _get_bot_owned_long_position(
        db, bot, exchange_position=exchange_position)

    _sync_exchange_open_position_take_profit_orders(db, bot, adapter)

    active_position_tps = _active_position_take_profit_orders(db, bot)
    legacy_tp_orders = _active_legacy_take_profit_orders(db, bot)
    changed_orders: list[TradingBotOrder] = []

    for legacy_order in legacy_tp_orders:
        changed_orders.append(
            _cancel_local_order(
                db,
                bot,
                adapter,
                legacy_order,
                event_type="position_take_profit_cancelled",
                message="Cancelled legacy take-profit order",
            )
        )

    # A position visible on the exchange is not automatically owned by this
    # bot. Only this bot's filled current-generation entry orders establish
    # ownership. This prevents a newly started Grid/DCA bot from adopting a
    # position left behind by a previously stopped bot on the same account.
    if position is None:
        for active_tp in active_position_tps:
            changed_orders.append(
                _cancel_local_order(
                    db,
                    bot,
                    adapter,
                    active_tp,
                    event_type="position_take_profit_cancelled",
                    message="Cancelled position take-profit order",
                )
            )
        return changed_orders, 0.0

    owned_size = position["size"]
    if exchange_position is None:
        for active_tp in active_position_tps:
            changed_orders.append(
                _cancel_local_order(
                    db,
                    bot,
                    adapter,
                    active_tp,
                    event_type="position_take_profit_cancelled",
                    message="Cancelled position take-profit order because exchange position is closed",
                )
            )
        _log_risk_blocked(
            db,
            bot,
            "Bot-owned position is not present on the exchange",
            {"bot_position_qty": owned_size, "exchange_position_qty": 0.0},
        )
        return changed_orders, owned_size

    exchange_size = exchange_position["size"]
    if owned_size > exchange_size + 1e-9:
        _log_risk_blocked(
            db,
            bot,
            "Bot-owned position exceeds exchange long position",
            {
                "bot_position_qty": owned_size,
                "exchange_position_qty": exchange_size,
            },
        )
        return changed_orders, owned_size

    if position.get("avg_entry_price") is None:
        log_bot_event(
            db,
            bot,
            "position_reconciliation_waiting",
            "Waiting for this bot's entry fill price before creating take-profit",
            {
                "bot_position_qty": owned_size,
                "exchange_position_qty": exchange_size,
            },
        )
        return changed_orders, owned_size

    target_order = normalize_order_request(
        _build_position_take_profit_order(
            bot, owned_size, position["avg_entry_price"]),
        rules,
    )
    if target_order.qty > owned_size + 1e-12 or target_order.qty > exchange_size + 1e-12:
        payload = {
            "bot_position_qty": owned_size,
            "exchange_position_qty": exchange_size,
            "tp_qty": target_order.qty,
            "order_link_id": target_order.order_link_id,
        }
        _log_risk_blocked(
            db, bot, "Position TP quantity exceeds available bot-owned position", payload)
        return changed_orders, owned_size
    validation_error = validate_order_request(target_order, rules)
    if validation_error:
        log_bot_event(db, bot, "error", validation_error, {
                      "order_link_id": target_order.order_link_id})
        return changed_orders, owned_size

    matching_tp = None
    for active_tp in active_position_tps:
        if matching_tp is None and _order_matches_target(active_tp, target_order):
            matching_tp = active_tp
            _attach_position_take_profit_snapshot(
                active_tp, position, target_order)
            db.add(active_tp)
            continue
        changed_orders.append(
            _cancel_local_order(
                db,
                bot,
                adapter,
                active_tp,
                event_type="position_take_profit_cancelled",
                message="Cancelled outdated position take-profit order",
            )
        )

    if matching_tp is not None:
        return changed_orders, owned_size

    response = place_order(adapter, category=bot.category,
                           symbol=bot.symbol, order=target_order)
    if _is_duplicate_client_tag(response):
        log_bot_event(db, bot, "error", "Duplicate position TP client order tag rejected by the exchange", {
            "order_link_id": target_order.order_link_id,
            "ret_code": response.rejection.raw_code,
        })
        return changed_orders, owned_size
    created = _record_order(db, bot, target_order, response)
    _attach_position_take_profit_snapshot(created, position, target_order)
    db.add(created)
    changed_orders.append(created)

    event_type = "position_take_profit_updated" if active_position_tps else "position_take_profit_created"
    message = "Updated position take-profit order" if active_position_tps else "Created position take-profit order"
    log_bot_event(db, bot, event_type, message, {
        "order_link_id": target_order.order_link_id,
        "side": target_order.side,
        "qty": target_order.qty,
        "price": target_order.price,
    })
    return changed_orders, owned_size

def _should_tick(bot: TradingBot) -> bool:
    interval = int(get_bot_setting(bot, "run_interval_seconds", 10))
    if bot.last_run_at is None:
        return True
    last_run_at = bot.last_run_at
    if last_run_at.tzinfo is None:
        last_run_at = last_run_at.replace(tzinfo=timezone.utc)
    return (_utcnow() - last_run_at).total_seconds() >= interval


def _is_reconciliation_candidate(
    bot: TradingBot, order: TradingBotOrder, *, include_legacy: bool
) -> bool:
    if not order.order_link_id or order.status not in ACTIVE_ORDER_STATUSES:
        return False
    if _is_current_generation_link_id(bot, order.order_link_id):
        return True
    return include_legacy and order.order_link_id.startswith(
        get_legacy_order_link_prefix(bot)
    )


def sync_bot_orders(db, bot: TradingBot, *, include_legacy: bool = False, adapter=None) -> list[dict]:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    prefixes = [get_order_link_prefix(bot)]
    if include_legacy:
        prefixes.append(get_legacy_order_link_prefix(bot))

    remote_by_link: dict[str, Order] = {}
    for prefix in prefixes:
        for remote in get_orders_by_prefix(
            adapter,
            category=bot.category,
            symbol=bot.symbol,
            order_link_prefix=prefix,
        ):
            order_link_id = remote.client_tag
            if not order_link_id:
                continue
            if not include_legacy and not _is_current_generation_link_id(bot, order_link_id):
                continue
            remote_by_link[order_link_id] = remote

    local_orders = (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.user_id == bot.user_id,
        )
        .all()
    )
    local_by_link = {
        order.order_link_id: order
        for order in local_orders
        if order.order_link_id
    }

    changes: list[dict] = []

    for order_link_id, remote in remote_by_link.items():
        record = local_by_link.get(order_link_id)
        if record is None:
            record = _upsert_remote_order(db, bot, remote)
            if record is None:
                continue
            local_by_link[order_link_id] = record
        old_status = record.status
        _update_local_order_from_exchange(record, remote)
        new_status = record.status
        db.add(record)
        changes.append(_status_change_payload(record, old_status, new_status))

    # An emulator account reset deletes orders instead of leaving cancelled history.
    # Reconcile any locally active order that is absent from both exchange open orders
    # and exact order history. Once marked final, the running worker can safely build
    # a clean replacement grid in the same tick.
    for record in local_orders:
        if not _is_reconciliation_candidate(
            bot, record, include_legacy=include_legacy
        ):
            continue
        order_link_id = record.order_link_id
        if order_link_id in remote_by_link:
            continue

        # AC-08: only an exchange answer can mark the order gone; a failed
        # lookup raises and leaves the order active for the next tick.
        old_status = record.status
        resolve_order_state(db, bot, adapter, record)
        changes.append(_status_change_payload(record, old_status, record.status))

    db.flush()
    return changes


def create_missing_grid_entries(
    db, bot: TradingBot, current_position_size: float = 0.0, *, adapter=None,
) -> list[TradingBotOrder]:
    created_orders: list[TradingBotOrder] = []
    if adapter is None:
        adapter = registry.exchange_for(bot)
    current_price = get_last_price(
        adapter, category=bot.category, symbol=bot.symbol)
    rules = get_instrument_rules(
        adapter, category=bot.category, symbol=bot.symbol)
    settings = get_effective_bot_settings(bot)
    open_exchange_orders = _current_generation_open_orders(adapter, bot)
    pending_buy_qty = _pending_buy_qty(open_exchange_orders)
    current_open_orders = len(open_exchange_orders)
    max_grid_levels = min(bot.grid_orders_count, settings["max_grid_levels"])

    for level_index in range(1, max_grid_levels + 1):
        role = _level_role(level_index)
        last_entry = (
            db.query(TradingBotOrder)
            .filter(TradingBotOrder.bot_id == bot.id, TradingBotOrder.order_role == role)
            .order_by(desc(TradingBotOrder.id))
            .first()
        )

        if last_entry and last_entry.status in ACTIVE_ORDER_STATUSES:
            continue
        if last_entry and last_entry.status == "Filled":
            if current_position_size > 0:
                continue

        order = normalize_order_request(_build_grid_entry_order(
            db, bot, level_index, current_price), rules)
        risk_message, risk_payload = _evaluate_new_buy_order_risk(
            db,
            bot,
            current_price=current_price,
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
            continue
        if _active_order_exists(db, bot, order.order_link_id):
            continue

        response = place_order(
            adapter, category=bot.category, symbol=bot.symbol, order=order)
        created = _record_order(db, bot, order, response)
        created_orders.append(created)
        pending_buy_qty += created.qty
        current_open_orders += 1
        log_bot_event(db, bot, "grid_entry_created", "Created grid entry order", {
            "order_link_id": order.order_link_id,
            "side": order.side,
            "qty": order.qty,
            "price": order.price,
        })

    return created_orders


def cancel_all_bot_orders(db, bot: TradingBot, *, adapter=None) -> list[TradingBotOrder]:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    cancelled: list[TradingBotOrder] = []
    active_orders = (
        db.query(TradingBotOrder)
        .filter(
            TradingBotOrder.bot_id == bot.id,
            TradingBotOrder.order_link_id.isnot(None),
            TradingBotOrder.status.in_(ACTIVE_ORDER_STATUSES),
        )
        .all()
    )
    for order in active_orders:
        response = cancel_order_by_link_id(
            adapter,
            category=bot.category,
            symbol=bot.symbol,
            order_link_id=order.order_link_id,
        )
        order.status = "Cancelled"
        order.raw_response = response
        db.add(order)
        cancelled.append(order)
        log_bot_event(db, bot, "order_cancelled", "Cancelled bot order", {
                      "order_link_id": order.order_link_id})
    return cancelled


def close_bot_position(db, bot: TradingBot, *, adapter=None) -> dict:
    if adapter is None:
        adapter = registry.exchange_for(bot)
    rules = get_instrument_rules(
        adapter, category=bot.category, symbol=bot.symbol)
    exchange_position = _get_current_long_position(
        adapter, category=bot.category, symbol=bot.symbol)
    position = _get_bot_owned_long_position(
        db, bot, exchange_position=exchange_position)
    if position is None:
        return {"message": "No open position owned by this bot to close", "order": None}
    if exchange_position is None:
        raise ValueError("Bot-owned position is not present on the exchange")

    close_qty = min(position["size"], exchange_position["size"])
    if close_qty <= 1e-12:
        return {"message": "No open position owned by this bot to close", "order": None}

    for active_tp in _active_position_take_profit_orders(db, bot):
        _cancel_local_order(
            db,
            bot,
            adapter,
            active_tp,
            event_type="position_take_profit_cancelled",
            message="Cancelled position take-profit order before closing position",
        )

    close_order = normalize_order_request(
        _build_close_position_order(bot, close_qty),
        rules,
    )
    if close_order.qty > position["size"] + 1e-12 or close_order.qty > exchange_position["size"] + 1e-12:
        raise ValueError(
            "Close position order quantity exceeds bot-owned or exchange position size")

    validation_error = validate_order_request(close_order, rules)
    if validation_error:
        raise ValueError(validation_error)

    log_bot_event(db, bot, "position_close_requested", "Requested manual position close", {
        "order_link_id": close_order.order_link_id,
        "position_size": position["size"],
        "exchange_position_size": exchange_position["size"],
    })
    response = place_order(adapter, category=bot.category,
                           symbol=bot.symbol, order=close_order)
    created = _record_order(db, bot, close_order, response)
    if created.exchange_order_id is not None:
        log_bot_event(db, bot, "position_closed", "Submitted reduce-only market close order", {
            "order_link_id": close_order.order_link_id,
            "exchange_order_id": created.exchange_order_id,
        })
    db.add(created)
    db.flush()
    return {"message": "Position close order submitted", "order": created}

def tick_grid_bot(db, bot: TradingBot, *, adapter=None) -> dict:
    if bot.runtime_status != "running":
        return {"orders": [], "events": 0, "message": "Bot is not running"}
    if not bot.is_active:
        bot.runtime_status = "stopped"
        bot.last_error = "Bot is inactive"
        log_bot_event(db, bot, "error", "Bot is inactive and cannot trade")
        db.add(bot)
        db.commit()
        return {"orders": [], "events": 1, "message": "Bot inactive"}
    if not _should_tick(bot):
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
            if change["became_filled"] and order.order_role.startswith("grid_entry_"):
                if order.filled_event_logged_at is None:
                    log_bot_event(db, bot, "grid_entry_filled", "Grid entry filled", {
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
            _rollover_completed_take_profit_cycle(db, bot, adapter=adapter)
        )
        if rollover_triggered and not cancellation_confirmed:
            bot.last_run_at = _utcnow()
            bot.last_error = None
            db.add(bot)
            db.commit()
            return {
                "orders": rollover_changes,
                "events": len(synced),
                "message": "Waiting for previous grid cancellation confirmation",
            }

        tp_changes, current_position_size = sync_position_take_profit(db, bot, adapter=adapter)
        created_entries = create_missing_grid_entries(
            db, bot, current_position_size, adapter=adapter)

        bot.last_run_at = _utcnow()
        bot.last_error = None
        db.add(bot)
        db.commit()
        return {
            "orders": rollover_changes + tp_changes + created_entries,
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
        error = scrub(str(exc))
        bot.last_error = error
        if get_effective_bot_settings(bot)["stop_bot_on_error"]:
            bot.runtime_status = "stopped"
        log_bot_event(db, bot, "error", error)
        db.add(bot)
        db.commit()
        return {"orders": [], "events": 1, "message": error}
