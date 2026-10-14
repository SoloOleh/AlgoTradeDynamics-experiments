"""Test double: answers the adapter's order lookup from a test's in-memory remote order state."""
from __future__ import annotations

from app.bot_engine.exchange.bybit.adapter import order_from_payload
from app.bot_engine.exchange.port import ACTIVE_ORDER_STATUSES, OrderLookup


class RemoteStateLookup:
    def __init__(self, remote_state: dict[str, dict]) -> None:
        self.remote_state = remote_state

    def lookup_order(self, market_type: str, symbol: str, client_tag: str) -> OrderLookup:
        payload = self.remote_state.get(client_tag)
        if not payload:
            return OrderLookup.not_found()
        order = order_from_payload(dict(payload))
        return OrderLookup.open(order) if order.status in ACTIVE_ORDER_STATUSES else OrderLookup.final(order)
