"""Read ticker and price data for a trading bot through its exchange adapter."""
from __future__ import annotations

from app.bot_engine.exchange.port import InstrumentRules, Ticker
from app.bot_engine.orders import get_instrument_rules as load_instrument_rules


def get_ticker_snapshot(adapter, *, category: str, symbol: str) -> Ticker:
    return adapter.ticker(category, symbol)


def get_last_price(adapter, *, category: str, symbol: str) -> float:
    ticker = get_ticker_snapshot(adapter, category=category, symbol=symbol)
    last_price = ticker.last_price if ticker.last_price is not None else ticker.mark_price
    if last_price is None:
        raise ValueError(f"Ticker price missing for {symbol}")
    return float(last_price)


def get_instrument_rules(adapter, *, category: str, symbol: str) -> InstrumentRules:
    return load_instrument_rules(adapter, category=category, symbol=symbol)
