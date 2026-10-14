"""Test doubles for the exchange adapter over a test's in-memory remote order state."""
from __future__ import annotations

from app.bot_engine.exchange.bybit.adapter import order_from_payload
from app.bot_engine.exchange.bybit.capabilities import BYBIT_CAPABILITIES
from app.bot_engine.exchange.port import (
    ACTIVE_ORDER_STATUSES,
    Balance,
    InstrumentRules,
    NewOrder,
    OrderLookup,
    PlacedOrder,
    Position,
    Ticker,
    Venue,
)


class RemoteStateLookup:
    def __init__(self, remote_state: dict[str, dict]) -> None:
        self.remote_state = remote_state

    def lookup_order(self, market_type: str, symbol: str, client_tag: str) -> OrderLookup:
        payload = self.remote_state.get(client_tag)
        if not payload:
            return OrderLookup.not_found()
        order = order_from_payload(dict(payload))
        return OrderLookup.open(order) if order.status in ACTIVE_ORDER_STATUSES else OrderLookup.final(order)


class RemoteStateExchange(RemoteStateLookup):
    """A whole-port test double over a test's in-memory remote order state and positions.

    `place` receives the neutral `NewOrder` and returns the placed order's status; `cancel` receives
    the client tag. Market data comes from the fixed price and rules the test passes.
    """

    def __init__(self, remote_state, *, remote_positions=None, price=100.0, rules=None, place=None, cancel=None):
        super().__init__(remote_state)
        self.remote_positions = remote_positions if remote_positions is not None else []
        self.price = price
        self.rules = rules
        self.place = place
        self.cancel = cancel

    def venue(self):
        return Venue("bybit", "demo")

    def capabilities(self):
        return BYBIT_CAPABILITIES

    def instrument_rules(self, market_type, symbol):
        return InstrumentRules(
            min_order_qty=self.rules.min_order_qty, qty_step=self.rules.qty_step,
            tick_size=self.rules.tick_size, min_notional_value=self.rules.min_notional_value,
        )

    def ticker(self, market_type, symbol):
        return Ticker(symbol=symbol, last_price=float(self.price), mark_price=float(self.price))

    def candles(self, market_type, symbol, *, interval, end_ms, limit):
        return []

    def place_order(self, market_type, symbol, order: NewOrder) -> PlacedOrder:
        status = self.place(order)
        result = {"orderId": f"oid-{order.client_tag}", "orderStatus": status}
        return PlacedOrder(order_id=result["orderId"], client_tag=order.client_tag, status=status,
                           raw={"result": result})

    def cancel_order(self, market_type, symbol, *, order_id=None, client_tag=None):
        return self.cancel(client_tag)

    def open_orders(self, market_type, symbol):
        return [
            order_from_payload(dict(payload)) for payload in self.remote_state.values()
            if payload.get("orderStatus") in ACTIVE_ORDER_STATUSES
        ]

    def order_history(self, market_type, symbol, *, limit):
        return [
            order_from_payload(dict(payload)) for payload in self.remote_state.values()
            if payload.get("orderStatus") not in ACTIVE_ORDER_STATUSES
        ][:limit]

    def executions(self, market_type, symbol, *, start_ms=None, end_ms=None):
        return []

    def positions(self, market_type, symbol):
        return positions_from(self.remote_positions, symbol=symbol)

    def balance(self):
        return Balance()

    def streams(self):
        raise NotImplementedError

    def check_connection(self):
        raise NotImplementedError


def orders_from(payloads) -> list:
    """Neutral orders from a test's remote order payloads."""
    return [order_from_payload(dict(payload)) for payload in payloads]


def positions_from(items, *, symbol: str = "BTCUSDT") -> list[Position]:
    """Neutral open positions (size > 0) from a test's remote position payloads."""
    return [
        Position(
            symbol=symbol, side=str(item.get("side") or ""), size=float(item.get("size") or 0),
            avg_entry_price=float(item["avgPrice"]) if item.get("avgPrice") else None,
            liq_price=float(item["liqPrice"]) if item.get("liqPrice") else None,
            leverage=float(item["leverage"]) if item.get("leverage") else None,
            margin_mode=item.get("tradeMode") or item.get("marginMode"),
        )
        for item in items if float(item.get("size") or 0) != 0
    ]


def placed_from(response: dict, client_tag: str) -> PlacedOrder:
    """The neutral placement answer for a test's fake exchange response."""
    result = response.get("result", {})
    return PlacedOrder(order_id=result.get("orderId"), client_tag=client_tag,
                       status=result.get("orderStatus"), raw=response)


def ticker_at(price) -> Ticker:
    return Ticker(symbol="BTCUSDT", last_price=float(price), mark_price=float(price))
