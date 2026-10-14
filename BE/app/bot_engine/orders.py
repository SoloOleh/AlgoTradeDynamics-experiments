"""Place, cancel, and query exchange orders for trading bots.

Every helper takes an `ExchangeAdapter` and returns the port's neutral types. Passing a raw exchange
session instead still works and returns today's payload dicts: that path is the shim the runtimes
use until they move onto the port (T11–T14) and is deleted in T20.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Literal

from app.bot_engine.events import log_bot_event
from app.bot_engine.exchange.errors import ErrorClass, ExchangeError
from app.bot_engine.exchange.port import InstrumentRules, NewOrder, Order, PlacedOrder
from app.core.clock import utcnow


FINAL_ORDER_STATUSES = {"Filled", "Cancelled", "Rejected", "Deactivated"}
ACTIVE_ORDER_STATUSES = {"New", "Created",
                         "PartiallyFilled", "PendingNew", "Untriggered"}


@dataclass(slots=True)
class OrderRequest:
    side: str
    order_type: str
    order_role: str
    order_link_id: str
    qty: float
    price: float | None
    parent_order_id: int | None = None
    reduce_only: bool = False


def is_exchange_adapter(source) -> bool:
    """True for an adapter on the port, False for a raw exchange session (the T20-bound shim)."""
    return getattr(type(source), "lookup_order", None) is not None


def get_open_orders(session, *, category: str, symbol: str) -> list[dict] | list[Order]:
    if is_exchange_adapter(session):
        return session.open_orders(category, symbol)
    response = session.get_open_orders(
        category=category, symbol=symbol, openOnly=0)
    return response.get("result", {}).get("list", [])


def get_orders_by_prefix(session, *, category: str, symbol: str, order_link_prefix: str) -> list[dict]:
    """Today's reconciliation page (open orders + the last 50 history items) as payload dicts."""
    if is_exchange_adapter(session):
        orders = session.open_orders(category, symbol) + session.order_history(category, symbol, limit=50)
        return [order.raw for order in orders if (order.client_tag or "").startswith(order_link_prefix)]
    open_orders = get_open_orders(session, category=category, symbol=symbol)
    matched_open = [
        item for item in open_orders if (item.get("orderLinkId") or "").startswith(order_link_prefix)
    ]
    history = session.get_order_history(
        category=category, symbol=symbol, limit=50)
    history_items = history.get("result", {}).get("list", [])
    matched_history = [
        item for item in history_items if (item.get("orderLinkId") or "").startswith(order_link_prefix)
    ]
    return matched_open + matched_history


def get_instrument_rules(session, *, category: str, symbol: str) -> InstrumentRules:
    if is_exchange_adapter(session):
        return session.instrument_rules(category, symbol)
    response = session.get_instruments_info(category=category, symbol=symbol)
    items = response.get("result", {}).get("list", [])
    if not items:
        raise ValueError(f"Instrument info not found for {symbol}")

    info = items[0]
    lot_size_filter = info.get("lotSizeFilter", {})
    price_filter = info.get("priceFilter", {})
    return InstrumentRules(
        min_order_qty=float(lot_size_filter.get("minOrderQty") or 0),
        qty_step=float(lot_size_filter.get("qtyStep") or 0.000001),
        tick_size=float(price_filter.get("tickSize") or 0.000001),
        min_notional_value=float(lot_size_filter.get("minNotionalValue") or 0),
    )


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value

    # Values such as 100 * 1.015 can arrive as 101.49999999999999.
    # Add only a sub-nanostep tolerance before flooring so an exact tick is
    # not incorrectly moved one full step lower by binary floating-point noise.
    step_decimal = Decimal(str(step))
    value_decimal = Decimal(str(value))
    tolerance = step_decimal * Decimal("1e-9")
    quantized = (value_decimal + tolerance) / step_decimal
    rounded = quantized.quantize(Decimal("1"), rounding=ROUND_DOWN) * step_decimal
    return float(rounded)


def normalize_order_request(order: OrderRequest, rules: InstrumentRules) -> OrderRequest:
    qty = round_to_step(order.qty, rules.qty_step)
    price = round_to_step(
        order.price, rules.tick_size) if order.price is not None else None
    return OrderRequest(
        side=order.side,
        order_type=order.order_type,
        order_role=order.order_role,
        order_link_id=order.order_link_id,
        qty=qty,
        price=price,
        parent_order_id=order.parent_order_id,
        reduce_only=order.reduce_only,
    )


def _requires_reduce_only(order: OrderRequest) -> bool:
    return (
        order.order_role in {"position_take_profit", "position_close", "take_profit_recovered"}
        or order.order_role.startswith((
            "take_profit_", "tp_", "scalper_take_profit", "scalper_stop_loss",
            "scalper_timeout", "scalper_manual_close", "momentum_take_profit",
            "momentum_stop_loss", "momentum_trailing_stop", "momentum_signal_exit",
            "momentum_manual_close", "momentum_emergency_close",
        ))
    )


def validate_order_request(order: OrderRequest, rules: InstrumentRules) -> str | None:
    if _requires_reduce_only(order):
        long_close = (
            order.order_role in {"position_take_profit", "position_close", "take_profit_recovered"}
            or order.order_role.startswith(("take_profit_", "tp_"))
        )
        if long_close and order.side != "Sell":
            return "Long position close orders must use Sell side"
        if not order.reduce_only:
            return "Position close orders must be reduce-only"
    if order.qty < rules.min_order_qty:
        return f"Order qty {order.qty} is below Bybit minimum {rules.min_order_qty}"
    if order.price is not None and rules.min_notional_value > 0 and (order.qty * order.price) < rules.min_notional_value:
        return (
            f"Order notional {order.qty * order.price:.8f} is below Bybit minimum "
            f"{rules.min_notional_value}"
        )
    return None


def place_order(session, *, category: str, symbol: str, order: OrderRequest) -> dict | PlacedOrder:
    if _requires_reduce_only(order) and not order.reduce_only:
        raise ValueError(
            "Position close orders must be reduce-only before placing")
    if is_exchange_adapter(session):
        return session.place_order(category, symbol, NewOrder(
            side=order.side,
            order_type=order.order_type,
            qty=order.qty,
            price=order.price,
            client_tag=order.order_link_id,
            reduce_only=order.reduce_only,
        ))
    payload = {
        "category": category,
        "symbol": symbol,
        "side": order.side,
        "orderType": order.order_type,
        "qty": str(order.qty),
        "orderLinkId": order.order_link_id,
    }
    if order.price is not None:
        payload["price"] = str(order.price)
    if order.reduce_only:
        payload["reduceOnly"] = True
    return session.place_order(**payload)


def cancel_order(session, *, category: str, symbol: str, order_id: str) -> dict:
    if is_exchange_adapter(session):
        return session.cancel_order(category, symbol, order_id=order_id)
    return session.cancel_order(category=category, symbol=symbol, orderId=order_id)


def get_order_status(session, *, category: str, symbol: str, order_id: str) -> dict:
    response = session.get_open_orders(
        category=category,
        symbol=symbol,
        orderId=order_id,
        openOnly=0,
    )
    items = response.get("result", {}).get("list", [])
    if items:
        return items[0]

    history = session.get_order_history(
        category=category, symbol=symbol, orderId=order_id)
    history_items = history.get("result", {}).get("list", [])
    if history_items:
        return history_items[0]
    return {}


def get_order_status_by_link_id(
    session, *, category: str, symbol: str, order_link_id: str
) -> dict:
    """Return the exact exchange order matching an orderLinkId, if it still exists.

    This targeted lookup is used during reconciliation when an active local order is
    absent from the regular open/history page. It avoids treating pagination as an
    exchange reset.
    """
    response = session.get_open_orders(
        category=category,
        symbol=symbol,
        orderLinkId=order_link_id,
        openOnly=0,
    )
    items = response.get("result", {}).get("list", [])
    if items:
        return items[0]

    history = session.get_order_history(
        category=category,
        symbol=symbol,
        orderLinkId=order_link_id,
        limit=1,
    )
    history_items = history.get("result", {}).get("list", [])
    if history_items:
        return history_items[0]
    return {}


def cancel_order_by_link_id(session, *, category: str, symbol: str, order_link_id: str) -> dict:
    if is_exchange_adapter(session):
        return session.cancel_order(category, symbol, client_tag=order_link_id)
    return session.cancel_order(category=category, symbol=symbol, orderLinkId=order_link_id)


# --------------------------------------------------------------------------- order lookup (AC-08)


@dataclass(frozen=True, slots=True)
class OrderResolution:
    """What the exchange said about an order the bot believes is active."""

    state: Literal["open", "final", "gone"]
    order: Order | None = None


def apply_exchange_order(record, payload: dict) -> None:
    """Copy an exchange order payload onto the stored order row (today's reconciliation update)."""
    record.exchange_order_id = payload.get("orderId") or record.exchange_order_id
    record.order_link_id = payload.get("orderLinkId") or record.order_link_id
    record.side = payload.get("side") or record.side
    record.order_type = payload.get("orderType") or record.order_type
    record.status = payload.get("orderStatus") or record.status
    if payload.get("qty") is not None:
        record.qty = float(payload["qty"])
    if payload.get("cumExecQty") is not None:
        record.filled_qty = float(payload["cumExecQty"])
    elif payload.get("leavesQty") is not None:
        try:
            record.filled_qty = max(record.qty - float(payload["leavesQty"]), 0)
        except (TypeError, ValueError):
            pass
    if payload.get("price"):
        record.price = float(payload["price"])
    existing_payload = dict(record.raw_response or {})
    existing_payload.update(payload)
    record.raw_response = existing_payload


def mark_order_missing_from_exchange(db, bot, order) -> str:
    """Mark an active order gone (stored as `Cancelled`) once the exchange proved it is absent.

    Returns the status the order had before.
    """
    old_status = order.status
    reconciled_at = utcnow()
    order.status = "Cancelled"
    payload = dict(order.raw_response or {})
    payload.update(
        {
            "orderStatus": "Cancelled",
            "reconciliation": {
                "reason": "missing_from_exchange",
                "previousStatus": old_status,
                "reconciledAt": reconciled_at.isoformat(),
            },
        }
    )
    order.raw_response = payload
    db.add(order)
    log_bot_event(
        db,
        bot,
        "order_reconciled_missing",
        "Marked local active order as cancelled because it is missing from the exchange",
        {
            "order_link_id": order.order_link_id,
            "order_role": order.order_role,
            "previous_status": old_status,
            "new_status": order.status,
        },
    )
    return old_status


def resolve_order_state(db, bot, adapter, order_row) -> OrderResolution:
    """Ask the exchange about an active order by its client tag and apply the answer (Flow 5).

    `open` keeps it active, `final` takes its final state from history, `not_found` or the
    `unknown_order` class marks it gone. Any other failure is re-raised with the order row
    untouched: the tick aborts, so nothing tied to the order is placed or cancelled, and the
    worker's error handler retries within `error_max_retries` and then pauses (AC-08).
    """
    try:
        lookup = adapter.lookup_order(bot.category, bot.symbol, order_row.order_link_id)
    except ExchangeError as exc:
        if exc.error_class is not ErrorClass.UNKNOWN_ORDER:
            raise
        mark_order_missing_from_exchange(db, bot, order_row)
        return OrderResolution("gone")

    if lookup.is_not_found:
        mark_order_missing_from_exchange(db, bot, order_row)
        return OrderResolution("gone")
    apply_exchange_order(order_row, lookup.order.raw)
    db.add(order_row)
    return OrderResolution(lookup.state, lookup.order)
