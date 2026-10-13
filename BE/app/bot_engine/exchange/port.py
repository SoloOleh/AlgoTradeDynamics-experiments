"""The exchange-neutral adapter port and its neutral types (ADR-0001).

Numbers keep today's float conversion (sad.md §8). Order statuses use the stored platform
vocabulary (`New`, `PartiallyFilled`, `Filled`, `Cancelled`, `Rejected`, ... — ADR-0003); every
adapter maps into it. `raw` fields carry the exchange payload only where it is stored as is
(`trading_bot_orders.raw_response`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from app.bot_engine.exchange.capabilities import CapabilitySet
from app.bot_engine.exchange.errors import ExchangeError

FINAL_ORDER_STATUSES = frozenset({"Filled", "Cancelled", "Rejected", "Deactivated"})
ACTIVE_ORDER_STATUSES = frozenset({"New", "Created", "PartiallyFilled", "PendingNew", "Untriggered"})


@dataclass(frozen=True, slots=True)
class Venue:
    exchange: str
    environment: str


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    min_order_qty: float
    qty_step: float
    tick_size: float
    min_notional_value: float


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: float | None
    mark_price: float | None


@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class NewOrder:
    """An order to place, in platform terms. `client_tag` is the frozen `bot-{id}-g{gen}-…` tag."""

    side: str
    order_type: str
    qty: float
    price: float | None
    client_tag: str
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class Order:
    """An exchange order. Absent fields are `None` (a price of `None` means the exchange sent none)."""

    order_id: str | None
    client_tag: str | None
    status: str | None
    side: str | None
    order_type: str | None
    qty: float | None
    filled_qty: float | None
    price: float | None
    avg_price: float | None
    reduce_only: bool
    raw: dict[str, Any] = field(default_factory=dict)
    leaves_qty: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_ORDER_STATUSES


@dataclass(frozen=True, slots=True)
class PlacedOrder:
    """The exchange's answer to a placement.

    `rejection` is set, not raised, when the exchange answered the request with a refusal in its
    body; callers decide what a refused placement means, as they did before the adapter existed.
    """

    order_id: str | None
    client_tag: str | None
    status: str | None
    raw: dict[str, Any] = field(default_factory=dict)
    rejection: ExchangeError | None = None

    @property
    def accepted(self) -> bool:
        return self.rejection is None


@dataclass(frozen=True, slots=True)
class OrderLookup:
    """Answer of a lookup by client tag: `open`, `final` (filled, cancelled, …) or `not_found`."""

    state: Literal["open", "final", "not_found"]
    order: Order | None = None

    @classmethod
    def open(cls, order: Order) -> "OrderLookup":
        return cls("open", order)

    @classmethod
    def final(cls, order: Order) -> "OrderLookup":
        return cls("final", order)

    @classmethod
    def not_found(cls) -> "OrderLookup":
        return cls("not_found", None)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_final(self) -> bool:
        return self.state == "final"

    @property
    def is_not_found(self) -> bool:
        return self.state == "not_found"


@dataclass(frozen=True, slots=True)
class Execution:
    """One fill. Carries what performance needs to rebuild today's numbers exactly."""

    exec_id: str | None
    order_id: str | None
    client_tag: str | None
    side: str | None
    qty: float
    price: float
    fee: float
    realised_pnl: float | None
    exec_time_ms: int | None
    exec_seq: int | None = None


@dataclass(frozen=True, slots=True)
class Position:
    """An open position (size > 0). `side` is the exchange's side text, `""` when it sent none."""

    symbol: str | None
    side: str
    size: float
    avg_entry_price: float | None
    mark_price: float | None = None
    liq_price: float | None = None
    leverage: float | None = None
    margin_mode: Any = None


@dataclass(frozen=True, slots=True)
class Balance:
    """Account balances in the settlement coin; `None` where the exchange sent no usable value."""

    total_available: float | None = None
    total_equity: float | None = None
    total_wallet: float | None = None
    wallet: float | None = None
    coin_available: float | None = None
    coin_equity: float | None = None
    coin_wallet: float | None = None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """A live exchange event in platform terms (order, execution, position, wallet, ticker)."""

    kind: str
    symbol: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    state: Literal["connected", "not_configured", "failing"]
    error_class: str | None = None


@runtime_checkable
class ExchangeAdapter(Protocol):
    """What strategies, runtimes, performance, streams and the connection check may call."""

    def venue(self) -> Venue: ...
    def capabilities(self) -> CapabilitySet: ...

    def instrument_rules(self, market_type: str, symbol: str) -> InstrumentRules: ...
    def ticker(self, market_type: str, symbol: str) -> Ticker: ...
    def candles(self, market_type: str, symbol: str, *, interval: str, end_ms: int, limit: int) -> list[Candle]: ...

    def place_order(self, market_type: str, symbol: str, order: NewOrder) -> PlacedOrder: ...
    def cancel_order(self, market_type: str, symbol: str, *, order_id: str | None = None,
                     client_tag: str | None = None) -> dict[str, Any]: ...
    def open_orders(self, market_type: str, symbol: str) -> list[Order]: ...
    def order_history(self, market_type: str, symbol: str, *, limit: int) -> list[Order]: ...
    def lookup_order(self, market_type: str, symbol: str, client_tag: str) -> OrderLookup: ...

    def executions(self, market_type: str, symbol: str, *, start_ms: int | None = None,
                   end_ms: int | None = None) -> list[Execution]: ...
    def positions(self, market_type: str, symbol: str) -> list[Position]: ...
    def balance(self) -> Balance: ...

    def streams(self) -> Any: ...
    def check_connection(self) -> ConnectionStatus: ...
