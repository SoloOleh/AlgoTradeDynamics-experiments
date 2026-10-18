"""Canonicalise golden-run recordings into the records that trading equivalence compares.

Compared (ADR-0007): order-mutating requests (side, rounded price and quantity, client tag, the
place/cancel sequence), the final bot state (status, cycles, positions, PnL) and event categories
(event type + stored error type + raw code). Wording, read requests, exchange-assigned ids and
timestamps are not compared.
"""
from __future__ import annotations

PLACE_PATH = "/v5/order/create"
CANCEL_PATH = "/v5/order/cancel"


def _collect_order_links(value, links: dict[str, str]) -> None:
    if isinstance(value, dict):
        order_id = value.get("orderId")
        link_id = value.get("orderLinkId")
        if order_id and link_id:
            links[str(order_id)] = link_id
        for item in value.values():
            _collect_order_links(item, links)
    elif isinstance(value, list):
        for item in value:
            _collect_order_links(item, links)


def _in_window(step, from_step: int | None) -> bool:
    return from_step is None or (step is not None and step >= from_step)


def canonical_orders(requests: list[dict], *, from_step: int | None = None) -> list[dict]:
    links: dict[str, str] = {}
    for item in requests:
        _collect_order_links(item.get("response"), links)
        _collect_order_links(item.get("body"), links)

    orders: list[dict] = []
    for item in requests:
        if item["method"] != "POST" or not _in_window(item.get("step"), from_step):
            continue
        body = item.get("body") or {}
        if item["path"] == PLACE_PATH:
            orders.append({
                "op": "place",
                "side": body.get("side"),
                "order_type": body.get("orderType"),
                "qty": body.get("qty"),
                "price": body.get("price"),
                "order_link_id": body.get("orderLinkId"),
                "reduce_only": bool(body.get("reduceOnly", False)),
            })
        elif item["path"] == CANCEL_PATH:
            orders.append({
                "op": "cancel",
                "order_link_id": body.get("orderLinkId") or links.get(str(body.get("orderId"))),
            })
    return orders


def canonical_events(events: list[dict], *, from_step: int | None = None) -> list[dict]:
    return [
        {
            "event_type": event["event_type"],
            "error_type": (event.get("payload") or {}).get("error_type"),
            "error_code": (event.get("payload") or {}).get("error_code"),
        }
        for event in events
        if _in_window(event.get("step"), from_step)
    ]


def _round(value, digits: int = 8):
    if value in (None, ""):
        return value
    return round(float(value), digits)


def canonical_state(final_state: dict) -> dict:
    bot = final_state["bot"]
    exchange = final_state.get("exchange") or {}
    return {
        "runtime_status": bot["runtime_status"],
        "order_link_generation": bot["order_link_generation"],
        "last_error_type": bot.get("last_error_type"),
        "last_error_code": bot.get("last_error_code"),
        "orders": [
            {
                "order_link_id": order["order_link_id"],
                "order_role": order["order_role"],
                "side": order["side"],
                "order_type": order["order_type"],
                "status": order["status"],
                "qty": _round(order["qty"]),
                "filled_qty": _round(order["filled_qty"]),
                "price": _round(order["price"]),
                "cycle_completed": order["cycle_completed"],
            }
            for order in final_state["orders"]
        ],
        "positions": [
            {
                "symbol": item["symbol"],
                "side": item["side"],
                "size": _round(item["size"]),
                "avg_price": _round(item["avg_price"]),
            }
            for item in exchange.get("positions", [])
        ],
        "balance": _round(exchange.get("balance"), 6),
    }


def canonicalise(recording: dict, *, from_step: int | None = None) -> dict:
    return {
        "orders": canonical_orders(recording["requests"], from_step=from_step),
        "events": canonical_events(recording["events"], from_step=from_step),
        "state": canonical_state(recording["final_state"]),
    }
