"""Place, cancel, and query exchange orders for trading bots.

Every helper takes an `ExchangeAdapter` and returns the port's neutral types.
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


def get_open_orders(adapter, *, category: str, symbol: str) -> list[Order]:
    return adapter.open_orders(category, symbol)


def get_orders_by_prefix(adapter, *, category: str, symbol: str, order_link_prefix: str) -> list[Order]:
    """The reconciliation page: open orders + the last 50 history items with the tag prefix."""
    orders = adapter.open_orders(category, symbol) + adapter.order_history(category, symbol, limit=50)
    return [order for order in orders if (order.client_tag or "").startswith(order_link_prefix)]


def get_instrument_rules(adapter, *, category: str, symbol: str) -> InstrumentRules:
    return adapter.instrument_rules(category, symbol)


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
        return f"Order qty {order.qty} is below the exchange minimum {rules.min_order_qty}"
    if order.price is not None and rules.min_notional_value > 0 and (order.qty * order.price) < rules.min_notional_value:
        return (
            f"Order notional {order.qty * order.price:.8f} is below the exchange minimum "
            f"{rules.min_notional_value}"
        )
    return None


def ensure_accepted(placed: PlacedOrder, action: str) -> None:
    """Raise the adapter's classified refusal of a placement, naming the operation (ADR-0005).

    The stored text keeps the shape it had before the adapter existed: `<action>: <message> (code N)`.
    """
    rejection = placed.rejection
    if rejection is None:
        return
    message = rejection.message or "Unknown exchange error"
    suffix = f" (code {rejection.raw_code})" if rejection.raw_code is not None else ""
    raise ExchangeError(
        rejection.error_class, raw_code=rejection.raw_code, message=f"{action}: {message}{suffix}",
        http_status=rejection.http_status,
    )


def place_order(adapter, *, category: str, symbol: str, order: OrderRequest) -> PlacedOrder:
    if _requires_reduce_only(order) and not order.reduce_only:
        raise ValueError(
            "Position close orders must be reduce-only before placing")
    return adapter.place_order(category, symbol, NewOrder(
        side=order.side,
        order_type=order.order_type,
        qty=order.qty,
        price=order.price,
        client_tag=order.order_link_id,
        reduce_only=order.reduce_only,
    ))


def cancel_order(adapter, *, category: str, symbol: str, order_id: str) -> dict:
    return adapter.cancel_order(category, symbol, order_id=order_id)


def cancel_order_by_link_id(adapter, *, category: str, symbol: str, order_link_id: str) -> dict:
    return adapter.cancel_order(category, symbol, client_tag=order_link_id)


# --------------------------------------------------------------------------- order lookup (AC-08)


@dataclass(frozen=True, slots=True)
class OrderResolution:
    """What the exchange said about an order the bot believes is active."""

    state: Literal["open", "final", "gone"]
    order: Order | None = None


def apply_exchange_order(record, order: Order) -> None:
    """Copy an exchange order onto the stored order row (today's reconciliation update)."""
    record.exchange_order_id = order.order_id or record.exchange_order_id
    record.order_link_id = order.client_tag or record.order_link_id
    record.side = order.side or record.side
    record.order_type = order.order_type or record.order_type
    record.status = order.status or record.status
    if order.qty is not None:
        record.qty = order.qty
    if order.filled_qty is not None:
        record.filled_qty = order.filled_qty
    elif order.leaves_qty is not None:
        record.filled_qty = max(record.qty - order.leaves_qty, 0)
    if order.price is not None:
        record.price = order.price
    existing_payload = dict(record.raw_response or {})
    existing_payload.update(order.raw)
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
    apply_exchange_order(order_row, lookup.order)
    db.add(order_row)
    return OrderResolution(lookup.state, lookup.order)
