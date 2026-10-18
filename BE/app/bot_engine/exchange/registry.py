"""Venue registry and the fail-closed guard (ADR-0004).

A venue is (exchange, environment). Each venue maps to a factory that binds an adapter to the bot's
exchange account. There is no default venue. `exchange_for(bot, needs)` is the only way to obtain
an adapter: it refuses an unknown venue or a missing capability, in memory, before any request.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from app.bot_engine.exchange.capabilities import Capability, MarketType
from app.bot_engine.exchange.errors import CapabilityMissing, VenueNotSupported
from app.bot_engine.exchange.port import ExchangeAdapter


@dataclass(frozen=True, slots=True)
class Account:
    """The exchange account a bot trades on: its own emulator key, or the venue's server keys."""

    emulator_api_key: str | None = None


AdapterFactory = Callable[[Account], ExchangeAdapter]


def _bybit(environment: str) -> AdapterFactory:
    def build(account: Account) -> ExchangeAdapter:
        from app.bot_engine.exchange.bybit.adapter import BybitAdapter

        return BybitAdapter(environment, emulator_api_key=account.emulator_api_key)

    return build


_VENUES: dict[tuple[str, str], AdapterFactory] = {
    ("bybit", "emulator"): _bybit("emulator"),
    ("bybit", "demo"): _bybit("demo"),
    ("bybit", "testnet"): _bybit("testnet"),
    ("bybit", "live"): _bybit("live"),
}


def supported_venues() -> list[tuple[str, str]]:
    return sorted(_VENUES)


@contextmanager
def registered_venue(exchange: str, environment: str, factory: AdapterFactory) -> Iterator[None]:
    """Register a venue for the duration of a block (stand-in adapters in tests)."""
    key = (exchange, environment)
    previous = _VENUES.get(key)
    _VENUES[key] = factory
    try:
        yield
    finally:
        if previous is None:
            _VENUES.pop(key, None)
        else:
            _VENUES[key] = previous


def _account_for(bot) -> Account:
    if getattr(bot, "environment", None) == "emulator":
        key = (getattr(bot, "settings", None) or {}).get("emulator_api_key")
        return Account(emulator_api_key=str(key) if key else None)
    return Account()


def adapter_for_venue(exchange: str, environment: str, *, account: Account | None = None) -> ExchangeAdapter:
    """The venue-only variant (the connection check): no strategy, so no capability needs."""
    factory = _VENUES.get((exchange, environment))
    if factory is None:
        raise VenueNotSupported(exchange, environment)
    return factory(account or Account())


def exchange_for(bot, needs: Iterable[Capability] | None = None) -> ExchangeAdapter:
    """The only way to obtain an adapter for a bot. Refuses with no network call."""
    adapter = adapter_for_venue(bot.exchange, bot.environment, account=_account_for(bot))
    if needs is None:
        from app.bot_engine.strategies import get_strategy

        needs = get_strategy(bot.strategy_type).required_capabilities(bot)
    try:
        market_type = MarketType(bot.category)
    except ValueError:
        market_type = bot.category
    missing = adapter.capabilities().missing(list(needs), market_type)
    if missing:
        raise CapabilityMissing(bot.exchange, bot.environment, missing[0], market_type)
    return adapter
