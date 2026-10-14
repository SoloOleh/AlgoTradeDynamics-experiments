"""Read current positions for a trading bot from its exchange.

An `ExchangeAdapter` returns neutral `Position`s (size > 0 only); a raw exchange session still
returns today's payload dicts (the shim deleted in T20, see `orders.py`).
"""
from __future__ import annotations

from app.bot_engine.exchange.port import Position
from app.bot_engine.orders import is_exchange_adapter


def get_open_positions(session, *, category: str, symbol: str) -> list[dict] | list[Position]:
    if is_exchange_adapter(session):
        return session.positions(category, symbol)
    response = session.get_positions(category=category, symbol=symbol)
    items = response.get("result", {}).get("list", [])
    positions: list[dict] = []
    for item in items:
        size = item.get("size") or item.get("qty") or "0"
        try:
            if float(size) == 0:
                continue
        except (TypeError, ValueError):
            continue
        positions.append(item)
    return positions
