"""Capability vocabulary declared statically per market type (ADR-0006)."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum


class Capability(StrEnum):
    LIMIT_ORDERS = "limit_orders"
    MARKET_ORDERS = "market_orders"
    REDUCE_ONLY_ORDERS = "reduce_only_orders"
    POSITION_READS = "position_reads"
    SHORT_SELLING = "short_selling"
    EXECUTION_PNL = "execution_pnl"
    WALLET_BALANCE = "wallet_balance"
    CANDLES = "candles"
    ORDER_LOOKUP_BY_TAG = "order_lookup_by_tag"
    PRIVATE_EVENT_STREAM = "private_event_stream"


class MarketType(StrEnum):
    SPOT = "spot"
    LINEAR = "linear"
    INVERSE = "inverse"


class CapabilitySet:
    """What an adapter supports per market type, plus declared values the guard does not enforce."""

    def __init__(
        self,
        by_market_type: Mapping[MarketType, Iterable[Capability]],
        *,
        client_tag_max_length: int | None = None,
    ) -> None:
        self._by_market_type = {
            MarketType(market_type): frozenset(Capability(item) for item in items)
            for market_type, items in by_market_type.items()
        }
        # Declared, not enforced by the guard in this step (sad.md §5).
        self.client_tag_max_length = client_tag_max_length

    def for_market_type(self, market_type) -> frozenset[Capability]:
        try:
            return self._by_market_type.get(MarketType(market_type), frozenset())
        except ValueError:
            return frozenset()

    def supports(self, capability, market_type) -> bool:
        return Capability(capability) in self.for_market_type(market_type)

    def missing(self, needs: Iterable[Capability], market_type) -> list[Capability]:
        declared = self.for_market_type(market_type)
        return [Capability(item) for item in needs if Capability(item) not in declared]
