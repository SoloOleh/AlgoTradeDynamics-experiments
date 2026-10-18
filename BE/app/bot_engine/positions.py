"""Read current positions for a trading bot through its exchange adapter."""
from __future__ import annotations

from app.bot_engine.exchange.port import Position


def get_open_positions(adapter, *, category: str, symbol: str) -> list[Position]:
    """Open positions (size > 0) on the bot's symbol."""
    return adapter.positions(category, symbol)
