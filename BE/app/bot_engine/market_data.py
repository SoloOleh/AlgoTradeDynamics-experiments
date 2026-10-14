"""Read ticker and price data for a trading bot from its exchange.

Helpers take an `ExchangeAdapter` and return neutral types; a raw exchange session still returns
today's payload dicts (the shim deleted in T20, see `orders.py`).
"""
from __future__ import annotations

from app.bot_engine.exchange.port import Ticker
from app.bot_engine.orders import InstrumentRules, get_instrument_rules as load_instrument_rules, is_exchange_adapter


def get_ticker_snapshot(session, *, category: str, symbol: str) -> dict | Ticker:
    if is_exchange_adapter(session):
        return session.ticker(category, symbol)
    response = session.get_tickers(category=category, symbol=symbol)
    items = response.get("result", {}).get("list", [])
    if not items:
        raise ValueError(f"Ticker not found for {symbol}")
    return items[0]


def get_last_price(session, *, category: str, symbol: str) -> float:
    ticker = get_ticker_snapshot(session, category=category, symbol=symbol)
    if isinstance(ticker, Ticker):
        last_price = ticker.last_price if ticker.last_price is not None else ticker.mark_price
    else:
        last_price = ticker.get("lastPrice") or ticker.get("markPrice")
    if last_price is None:
        raise ValueError(f"Ticker price missing for {symbol}")
    return float(last_price)


def get_instrument_rules(session, *, category: str, symbol: str) -> InstrumentRules:
    return load_instrument_rules(session, category=category, symbol=symbol)
