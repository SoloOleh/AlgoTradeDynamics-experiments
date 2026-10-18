"""Exchange-neutral error vocabulary (ADR-0005).

Adapters translate their exchange's failures into an `ErrorClass`; the engine decides the reaction.
None of these exceptions subclasses `ValueError`, so the engine never mistakes them for a
configuration error.
"""
from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    TIME_SYNC = "time_sync"
    REJECTED_CREDENTIALS = "rejected_credentials"
    PERMISSION_DENIED = "permission_denied"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVALID_SYMBOL = "invalid_symbol"
    INVALID_ORDER = "invalid_order"
    DUPLICATE_CLIENT_TAG = "duplicate_client_tag"
    UNKNOWN_ORDER = "unknown_order"
    ORDER_STATE_CONFLICT = "order_state_conflict"
    RISK_LIMIT = "risk_limit"


class ExchangeError(Exception):
    """A failure reported by an exchange adapter, already classified."""

    def __init__(
        self,
        error_class: ErrorClass,
        *,
        raw_code: str | int | None = None,
        message: str = "",
        http_status: int | None = None,
    ) -> None:
        self.error_class = ErrorClass(error_class)
        self.raw_code = None if raw_code is None else str(raw_code)
        self.message = message
        self.http_status = http_status
        super().__init__(message or self.error_class.value)


class UnclassifiedExchangeFailure(Exception):
    """A transport failure the adapter could not classify; its message is already scrubbed (ADR-0008).

    The engine treats it as internal; `raw_code` is only stored, never interpreted (ADR-0005).
    """

    def __init__(self, message: str, *, raw_code: str | int | None = None) -> None:
        self.raw_code = None if raw_code is None else str(raw_code)
        super().__init__(message)


class GuardRefusal(Exception):
    """A fail-closed guard refusal: never retried, never routed elsewhere (ADR-0004)."""

    code = "exchange.guard_refused"

    @property
    def details(self) -> dict:
        return {}


class VenueNotSupported(GuardRefusal):
    """No exchange adapter is registered for the bot's venue (ADR-0004)."""

    def __init__(self, exchange: str, environment: str) -> None:
        self.exchange = exchange
        self.environment = environment
        super().__init__(f"Venue {exchange}/{environment} has no exchange adapter")

    code = "exchange.venue_unsupported"

    @property
    def details(self) -> dict:
        return {"exchange": self.exchange, "environment": self.environment}


class CapabilityMissing(GuardRefusal):
    """The venue's adapter does not declare a capability the strategy needs (ADR-0006)."""

    def __init__(self, exchange: str, environment: str, capability, market_type) -> None:
        self.exchange = exchange
        self.environment = environment
        self.capability = capability
        self.market_type = market_type
        super().__init__(
            f"Capability {getattr(capability, 'value', capability)} is not available on market type "
            f"{getattr(market_type, 'value', market_type)} for venue {exchange}/{environment}"
        )

    code = "exchange.capability_missing"

    @property
    def details(self) -> dict:
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "capability": getattr(self.capability, "value", self.capability),
            "market_type": getattr(self.market_type, "value", self.market_type),
        }
